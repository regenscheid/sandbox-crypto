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
sandbox-science-harness document import --state-dir state --project p1 [--session s1] \\
    --name ff [--title "…"] [--reason "…"] [--base N] ff.md
sandbox-science-harness document export DOC [--revision N] [--output ff.md] \\
    --state-dir state --project p1
sandbox-science-harness document (list | show DOC [--revision N] | history DOC) \\
    --state-dir state --project p1
sandbox-science-harness document suggestions DOC (on | off) --state-dir state --project p1
sandbox-science-harness document decide DOC PATCH (accept | reject) --state-dir state \\
    --project p1
sandbox-science-harness document delete DOC --reason "…" --state-dir state --project p1
```

``run`` builds a ``Harness`` (harness.py) from its arguments: the worker client, the
models and the stores. The harness runs the task; the CLI prints its progress and answer.
With ``--state-dir``, the session runs in its project's default environment, or in
an environment of its own with ``--dedicated`` (W3, projects.py). The harness has
the worker create or revise it first, and keeps the run's record, the same that
``--transcript`` writes, with the project's in the control store (W4). The run ID names that
record, so it must be an identifier. The agent also gets the document tools, over the
living documents in the state directory's control store (documents/store.py). The
harness refuses a worker whose isolation tier in effect is below the minimum, so a
misconfigured deployment cannot quietly run research code without a sandbox.

The ``project``, ``session`` and ``document`` commands are in cli_documents.py.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import sys
from collections.abc import Callable
from contextlib import AsyncExitStack
from pathlib import Path

from pydantic import TypeAdapter, ValidationError

from sandbox_science.contracts import Identifier

from .cli_documents import add_parsers, document, project, read_secret, session
from .documents import DocumentError, DocumentStore, store_path
from .harness import (
    Finished,
    Harness,
    IsolationRefusedError,
    Role,
    RunRequest,
    Settings,
    Stores,
    admit,
    closing,
    save,
)
from .jobstate import JobState
from .loop import MAX_INPUT_CHARS, Interruption
from .mathcheck import MathChecker
from .model import (
    MAX_RESPONSE_BYTES,
    ChatModel,
    ModelError,
    OpenAICompatibleModel,
    ScriptedModel,
)
from .projects import ProjectError, ProjectStore
from .stock_prompts import TEMPLATE_IDS
from .tools import ToolOutcome
from .worker_client import WorkerAPIError, WorkerClient


class _Printer:
    """A run's progress on standard output, and its errors on standard error."""

    def note(self, text: str) -> None:
        print(text)

    def error(self, text: str) -> None:
        print(f"error: {text}", file=sys.stderr)

    async def tool(self, name: str, arguments: str, outcome: ToolOutcome) -> None:
        print(f"\n→ {name} {arguments}\n← {outcome.text}")


def _gateway(
    args: argparse.Namespace,
    alias: str,
    max_tokens: int | None = None,
    max_response_bytes: int = MAX_RESPONSE_BYTES,
) -> ChatModel:
    key = read_secret(args.api_key_file)
    return OpenAICompatibleModel(
        args.model_url, alias, key, max_tokens=max_tokens, max_response_bytes=max_response_bytes
    )


def _role(script: Path | None, flag: str, alias: str, gateway: Callable[[], ChatModel]) -> Role:
    if script is None:
        return Role(gateway(), alias)
    try:
        return Role(ScriptedModel.from_json(json.loads(script.read_text())), alias)
    except (OSError, ValueError, TypeError, KeyError, RecursionError) as exc:
        raise ModelError(f"the {flag} file cannot be read: {exc}") from None


async def _close(model: ChatModel) -> None:
    if isinstance(model, OpenAICompatibleModel):
        await model.close()


async def _run(args: argparse.Namespace) -> int:
    """``_run_with`` the state directory's project store, closed when the run is kept."""
    projects = ProjectStore(args.state_dir) if args.state_dir is not None else None
    try:
        return await _run_with(args, projects)
    finally:
        if projects is not None:
            projects.close()


async def _run_with(args: argparse.Namespace, projects: ProjectStore | None) -> int:
    """Run one task against the worker, then save its record and acknowledge its notices.

    The session is resolved and the worker's isolation checked before the stores open
    and the watcher's and reviewer's scripts are read. The worker client, the models and
    the stores close after the run and before its record is saved, in reverse order; a
    failure there is recorded as the run's.
    """
    sink = _Printer()
    alias = "scripted" if args.script is not None else args.model
    main = _role(args.script, "--script", alias, lambda: _gateway(args, args.model))
    request = RunRequest(
        project=args.project,
        session=args.session,
        task=args.task,
        environment=args.environment,
        dedicated=args.dedicated,
        run_id=args.run_id,
        research_template=args.research_template,
    )
    minimum: int | None = None if args.allow_unisolated else args.min_isolation_tier
    finished: Finished | None = None
    state_dir: Path | None = args.state_dir
    interruption = Interruption()
    try:
        async with AsyncExitStack() as stack:
            with interruption.protect():
                client = await stack.enter_async_context(
                    interruption.context(WorkerClient(args.worker, read_secret(args.token_file)))
                )
                closing(stack, interruption, lambda: _close(main.model))
                try:
                    admitted = await admit(
                        client, projects, request, sink, min_isolation_tier=minimum
                    )
                except IsolationRefusedError as refused:
                    sink.error(f"{refused} (--allow-unisolated for development)")
                    return 2
                stores = None
                if projects is not None and state_dir is not None:
                    documents = DocumentStore(store_path(state_dir), producer="harness")
                    closing(stack, interruption, lambda: _closed(documents))
                    stores = Stores(
                        projects, documents, JobState(state_dir, control=documents.control)
                    )
                watcher = reviewer = None
                if args.watcher_model is not None:
                    watcher = _role(
                        args.watcher_script,
                        "--watcher-script",
                        args.watcher_model,
                        lambda: _gateway(
                            args, args.watcher_model, max_tokens=300, max_response_bytes=16384
                        ),
                    )
                    closing(stack, interruption, lambda: _close(watcher.model))
                if args.reviewer_model is not None:
                    reviewer = _role(
                        args.reviewer_script,
                        "--reviewer-script",
                        args.reviewer_model,
                        lambda: _gateway(
                            args, args.reviewer_model, max_tokens=2048, max_response_bytes=65536
                        ),
                    )
                    closing(stack, interruption, lambda: _close(reviewer.model))
                harness = Harness(
                    client,
                    main,
                    stores=stores,
                    watcher=watcher,
                    reviewer=reviewer,
                    checker=(
                        MathChecker(args.math_node, args.math_launcher, args.math_bundle)
                        if args.math_node is not None
                        else None
                    ),
                    settings=Settings(
                        min_isolation_tier=minimum,
                        max_turns=args.max_turns,
                        max_input_chars=args.max_input_chars,
                        watcher_calls_per_hour=args.watcher_calls_per_hour,
                        watcher_first_check=args.watcher_first_check,
                        watcher_interval=args.watcher_interval,
                    ),
                )
                finished = await harness.start(admitted, sink)
    except BaseException as error:
        if interruption.error is not None:
            raise interruption.error from None
        if not isinstance(error, Exception) or finished is None:
            raise
        finished.transcript.fail("cleanup")
    transcript = finished.transcript
    if transcript.failure is not None:
        sink.error(f"run failed during {transcript.failure}")
    print(f"\n{transcript.final_text or '(no answer)'}")
    return await save(
        finished.fields(),
        finished.status,
        finished.hooks,
        projects=projects,
        error=sink.error,
        transcript=args.transcript,
    )


async def _closed(documents: DocumentStore) -> None:
    documents.close()


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
    source.add_argument(
        "--model-url",
        help="OpenAI-compatible base URL (the LiteLLM proxy), as the gateway answers it: "
        "redirects are not followed",
    )
    run.add_argument("--model", default="main", help="model alias")
    run.add_argument("--api-key-file", type=Path)
    run.add_argument("--min-isolation-tier", type=int, default=3)
    run.add_argument("--allow-unisolated", action="store_true")
    run.add_argument("--max-turns", type=int, default=30)
    run.add_argument(
        "--max-input-chars",
        type=int,
        default=MAX_INPUT_CHARS,
        help="stop the run cleanly (context_limit) before MAIN is sent a longer conversation "
        f"(default {MAX_INPUT_CHARS:,})",
    )
    run.add_argument(
        "--research-template",
        choices=TEMPLATE_IDS,
        help="optional original research framing; preserves offered tools and permissions",
    )
    run.add_argument(
        "--run-id",
        help="name the run (default: a new random ID). Under a reused ID, a call identical "
        "to one already made in the same place returns its earlier result, marked replayed",
    )
    run.add_argument(
        "--watcher-model", help="separate job watcher model alias; omitted: no check-ins"
    )
    run.add_argument("--watcher-script", type=Path, help="scripted watcher replies for validation")
    run.add_argument(
        "--watcher-calls-per-hour",
        type=int,
        default=60,
        help="durable per-project watcher call budget (default 60)",
    )
    run.add_argument("--watcher-first-check", type=float, default=300)
    run.add_argument("--watcher-interval", type=float, default=1800)
    run.add_argument(
        "--math-node",
        type=Path,
        help="operator-configured absolute Node path for document math checks",
    )
    run.add_argument("--math-launcher", type=Path, help="operator-configured bounded math launcher")
    run.add_argument(
        "--math-bundle", type=Path, help="operator-configured built math checker bundle"
    )
    run.add_argument("--reviewer-model", help="separate consistency reviewer model alias")
    run.add_argument(
        "--reviewer-script", type=Path, help="scripted reviewer replies for validation"
    )
    run.add_argument("--transcript", type=Path)
    run.add_argument("task")
    add_parsers(commands)
    args = parser.parse_args(argv)
    if args.command == "document":
        try:
            return document(args)
        except (ProjectError, OSError, UnicodeDecodeError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
    match args.command:
        case "run":
            if args.watcher_calls_per_hour < 1:
                parser.error("--watcher-calls-per-hour must be positive")
            if any(
                not math.isfinite(value) or value <= 0
                for value in (args.watcher_first_check, args.watcher_interval)
            ):
                parser.error("watcher intervals must be positive and finite")
            if args.watcher_model is not None:
                if args.state_dir is None:
                    parser.error("--watcher-model needs --state-dir for durable accounting")
                if args.watcher_script is None and args.model_url is None:
                    parser.error("--watcher-model needs --model-url or --watcher-script")
                if args.watcher_model == args.model and args.script is None:
                    parser.error("--watcher-model must be a separate model alias")
            elif args.watcher_script is not None:
                parser.error("--watcher-script needs --watcher-model")
            configured_math = (args.math_node, args.math_launcher, args.math_bundle)
            if any(v is not None for v in configured_math) and (
                any(v is None or not v.is_absolute() for v in configured_math)
                or args.state_dir is None
            ):
                parser.error("math checks need --state-dir and all three absolute --math-* paths")
            if args.reviewer_model is not None:
                if args.state_dir is None:
                    parser.error("--reviewer-model needs --state-dir for durable snapshots")
                if args.reviewer_script is None and args.model_url is None:
                    parser.error("--reviewer-model needs --model-url or --reviewer-script")
                if args.reviewer_model == args.model:
                    parser.error("--reviewer-model must be a separate model alias")
            elif args.reviewer_script is not None:
                parser.error("--reviewer-script needs --reviewer-model")
            if args.model_url is not None and args.api_key_file is None:
                parser.error("--model-url needs --api-key-file")
            if args.max_input_chars < 1:
                parser.error("--max-input-chars must be at least 1")
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
            action = project(args)
        case _:
            action = session(args)
    try:
        return asyncio.run(action)
    except (WorkerAPIError, ModelError, ProjectError, DocumentError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
