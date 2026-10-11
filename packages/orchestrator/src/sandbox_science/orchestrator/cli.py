# SPDX-License-Identifier: MIT
"""Command line: run one task through the harness against a worker, and keep projects.

```
sandbox-science-harness run --worker http://127.0.0.1:8100 --token-file worker.token \\
    --project p1 --session s1 (--environment env-1 | --state-dir state [--dedicated]) \\
    (--script turns.json | --model-url http://127.0.0.1:4000 --model main --api-key-file key) \\
    [--min-isolation-tier 3 | --allow-unisolated] [--transcript out.json] "the task"
sandbox-science-harness project create --state-dir state --project p1 --profile minimal \\
    --policy sha256:...
sandbox-science-harness project show --state-dir state --project p1
sandbox-science-harness project revise --state-dir state --project p1 --policy sha256:... \\
    [--worker URL --token-file worker.token]
sandbox-science-harness session close --state-dir state --project p1 --session s1 \\
    --worker URL --token-file worker.token
```

With ``--state-dir``, the session runs in its project's default environment, or in
an environment of its own with ``--dedicated`` (W3, projects.py). The harness has
the worker create or revise it first, and keeps the run's record, the same that
``--transcript`` writes, beside the project's (W4). The run ID then names that
record, so it must be an identifier. The harness refuses a worker whose isolation
tier in effect is below the minimum, so a misconfigured deployment cannot quietly
run research code without a sandbox.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

from pydantic import TypeAdapter, ValidationError

from sandbox_science.contracts import ErrorKind, ExecutionContext, Identifier, Location, new_id

from .loop import run_task
from .model import ChatModel, ModelError, OpenAICompatibleModel, ScriptedModel
from .projects import ProjectError, ProjectStore, RunRecord, ensure_environment
from .tools import Toolbox, ToolOutcome
from .worker_client import WorkerAPIError, WorkerClient


def _read_secret(path: Path) -> str:
    return path.read_text().strip()


def tier_in_effect(location: Location) -> int | None:
    """The isolation tier the worker's probe left standing (D43); ``None`` without a probe.

    The worker lowers the declared tier when its probe finds less isolation, so the
    declared tier alone never counts.
    """
    return location.isolation_tier if location.isolation_evidence is not None else None


def describe_isolation(location: Location) -> str:
    """The tier in effect, the declared one if it differs, and what limits it."""
    evidence = location.isolation_evidence
    if evidence is None:
        return "no isolation evidence"
    text = f"isolation tier {location.isolation_tier}"
    if location.declared_isolation_tier != location.isolation_tier:
        text += f" (declared {location.declared_isolation_tier})"
    if evidence.findings:
        text += ": " + "; ".join(evidence.findings)
    return text


async def _context(args: argparse.Namespace, client: WorkerClient) -> ExecutionContext:
    """The session's execution context; from the project's record with ``--state-dir``."""
    if args.state_dir is None:
        return ExecutionContext(
            project_id=args.project, session_id=args.session, environment_id=args.environment
        )
    store = ProjectStore(args.state_dir)
    project = store.get(args.project)
    session = store.open_session(args.project, args.session, dedicated=args.dedicated)
    info = await ensure_environment(client, project, session.environment_id)
    if info is not None:
        print(
            f"environment: {info.environment_id} ({info.profile}, {info.policy}), "
            f"sandbox {info.sandbox}"
        )
    return ExecutionContext(
        project_id=args.project,
        session_id=args.session,
        environment_id=session.environment_id,
        policy_revision=project.policy,
    )


async def _run(args: argparse.Namespace) -> int:
    model: ChatModel
    if args.script is not None:
        model = ScriptedModel.from_json(json.loads(args.script.read_text()))
    else:
        model = OpenAICompatibleModel(args.model_url, args.model, _read_secret(args.api_key_file))
    run_id = args.run_id or new_id("run")
    started = datetime.now(UTC)
    async with WorkerClient(args.worker, _read_secret(args.token_file)) as client:
        context = await _context(args, client)
        capabilities = await client.capabilities(context.environment_id)
        location = capabilities.location
        tier = tier_in_effect(location)
        if not args.allow_unisolated and (tier is None or tier < args.min_isolation_tier):
            print(
                f"error: the worker offers {describe_isolation(location)}. At least tier "
                f"{args.min_isolation_tier} is required (--allow-unisolated for development)",
                file=sys.stderr,
            )
            return 2
        print(
            f"worker: pool {location.pool_id}, {location.backend} backend, sandbox "
            f"{location.sandbox_id}, host {location.hostname}, {describe_isolation(location)}"
        )

        async def show(name: str, arguments: str, outcome: ToolOutcome) -> None:
            print(f"\n→ {name} {arguments}\n← {outcome.text}")

        transcript = await run_task(
            model,
            Toolbox(
                client,
                context,
                languages=capabilities.languages,
                features=capabilities.features,
            ),
            args.task,
            run_id=run_id,
            max_turns=args.max_turns,
            on_tool=show,
        )
    if isinstance(model, OpenAICompatibleModel):
        await model.close()
    print(f"\n{transcript.final_text or '(no answer)'}")
    fields: dict[str, object] = {
        "run_id": run_id,
        "context": context.model_dump(mode="json"),
        "task": args.task,
        "started_at": started,
        "ended_at": datetime.now(UTC),
        "stopped": transcript.stopped,
        "final_text": transcript.final_text,
        "messages": transcript.messages,
        "tools": transcript.tool_records,
    }
    return _keep(args, fields, 0 if transcript.stopped == "answered" else 3)


def _keep(args: argparse.Namespace, fields: dict[str, object], status: int) -> int:
    """Write the run's transcript, then keep its record in the project's; ``status``
    unless either fails.

    A record that does not validate, such as a tool result nested deeper than pydantic
    follows, still reaches ``--transcript`` as the harness has it.
    """
    try:
        record: RunRecord | None = RunRecord.model_validate(fields)
        invalid = ""
    except (ValidationError, RecursionError) as exc:
        record, invalid = None, str(exc)[:300]
    if args.transcript is not None:
        try:
            text = record.to_json() if record is not None else json.dumps(fields, default=str)
            args.transcript.write_text(text)
        except (OSError, ValueError, RecursionError) as exc:
            print(f"error: the transcript could not be written: {exc}", file=sys.stderr)
            status = 1
    if args.state_dir is not None:
        try:
            if record is None:
                raise ProjectError(ErrorKind.INVALID_REQUEST, f"the record is invalid: {invalid}")
            ProjectStore(args.state_dir).save_run(record)
        except (ProjectError, OSError, ValueError, RecursionError) as exc:
            print(f"error: the run's record could not be kept: {exc}", file=sys.stderr)
            status = 1
    return status


async def _project(args: argparse.Namespace) -> int:
    store = ProjectStore(args.state_dir)
    match args.action:
        case "create":
            record, created = store.create(args.project, args.profile, args.policy)
            print(f"{'created' if created else 'exists'}: {record.model_dump_json(indent=2)}")
        case "show":
            print(store.get(args.project).model_dump_json(indent=2))
        case _:
            record = store.set_policy(args.project, args.policy)
            print(f"project {record.project_id} now uses {record.policy}")
            if args.worker is not None:
                async with WorkerClient(args.worker, _read_secret(args.token_file)) as client:
                    for environment_id in record.environments():
                        info = await ensure_environment(client, record, environment_id)
                        state = "not managed by the worker" if info is None else info.policy
                        print(f"environment {environment_id}: {state}")
    return 0


async def _session(args: argparse.Namespace) -> int:
    store = ProjectStore(args.state_dir)
    session = store.get(args.project).sessions.get(args.session)
    if session is None:
        print(f"error: project {args.project} has no session {args.session}", file=sys.stderr)
        return 1
    context = ExecutionContext(
        project_id=args.project, session_id=args.session, environment_id=session.environment_id
    )
    async with WorkerClient(args.worker, _read_secret(args.token_file)) as client:
        closed = await client.close_session(context)
    store.close_session(args.project, args.session)
    kernels = ", ".join(f"{k.language} {k.kernel_id}" for k in closed.kernels) or "none"
    print(f"closed session {args.session}; kernels stopped: {kernels}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="sandbox-science-harness")
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run", help="run one task to an answer")
    run.add_argument("--worker", required=True, help="worker API base URL")
    run.add_argument("--token-file", type=Path, required=True)
    run.add_argument("--project", required=True)
    run.add_argument("--session", required=True)
    where = run.add_mutually_exclusive_group(required=True)
    where.add_argument("--environment", help="run in this environment as it is")
    where.add_argument("--state-dir", type=Path, help="the projects' records (W3)")
    run.add_argument(
        "--dedicated",
        action="store_true",
        help="with --state-dir: a new session gets an environment of its own",
    )
    source = run.add_mutually_exclusive_group(required=True)
    source.add_argument("--script", type=Path, help="replay model turns from a JSON file")
    source.add_argument("--model-url", help="OpenAI-compatible base URL (the LiteLLM proxy)")
    run.add_argument("--model", default="main", help="model alias")
    run.add_argument("--api-key-file", type=Path)
    run.add_argument("--min-isolation-tier", type=int, default=3)
    run.add_argument("--allow-unisolated", action="store_true")
    run.add_argument("--max-turns", type=int, default=30)
    run.add_argument(
        "--run-id",
        help="name the run (default: a new random ID). Under a reused ID, a call identical "
        "to one already made in the same place returns its earlier result, marked replayed",
    )
    run.add_argument("--transcript", type=Path)
    run.add_argument("task")
    project = commands.add_parser("project", help="create, show or revise a project")
    project.add_argument("action", choices=("create", "show", "revise"))
    project.add_argument("--state-dir", type=Path, required=True)
    project.add_argument("--project", required=True)
    project.add_argument("--profile", help="create: the base profile, such as minimal")
    project.add_argument("--policy", help="create, revise: the policy revision (sha256:...)")
    project.add_argument("--worker", help="revise: also revise the project's environments")
    project.add_argument("--token-file", type=Path)
    session = commands.add_parser("session", help="close a session: stop its kernels")
    session.add_argument("action", choices=("close",))
    session.add_argument("--state-dir", type=Path, required=True)
    session.add_argument("--project", required=True)
    session.add_argument("--session", required=True)
    session.add_argument("--worker", required=True)
    session.add_argument("--token-file", type=Path, required=True)
    args = parser.parse_args(argv)
    match args.command:
        case "run":
            if args.model_url is not None and args.api_key_file is None:
                parser.error("--model-url needs --api-key-file")
            if args.dedicated and args.state_dir is None:
                parser.error("--dedicated needs --state-dir")
            if args.state_dir is not None and args.run_id is not None:
                try:
                    TypeAdapter(Identifier).validate_python(args.run_id)
                except ValidationError:
                    parser.error(
                        "with --state-dir, --run-id names the run's record: use letters, "
                        "digits, '.', '_' and '-', starting with a letter or digit"
                    )
            action = _run(args)
        case "project":
            if args.action == "create" and (args.profile is None or args.policy is None):
                parser.error("project create needs --profile and --policy")
            if args.action == "revise" and args.policy is None:
                parser.error("project revise needs --policy")
            if args.worker is not None and args.token_file is None:
                parser.error("--worker needs --token-file")
            action = _project(args)
        case _:
            action = _session(args)
    try:
        return asyncio.run(action)
    except (WorkerAPIError, ModelError, ProjectError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
