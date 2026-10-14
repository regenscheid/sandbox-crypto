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

The ``document`` commands act as the user: an import is attributed to an import the
user started, and the other changes to the user. An import into an existing document
names the revision its file was made from (``--base``), which must be the current one,
so that it never undoes revisions made since.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import math
import sys
from collections.abc import Callable
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any

from pydantic import TypeAdapter, ValidationError

from sandbox_science.contracts import ErrorKind, ExecutionContext, Identifier, new_id

from .documents import USER, Author, DocumentError, DocumentStore, store_path
from .documents.errors import CONFLICT
from .documents.tool import view_blocks
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
from .loop import Interruption
from .mathcheck import MathChecker
from .model import (
    MAX_RESPONSE_BYTES,
    ChatModel,
    ModelError,
    OpenAICompatibleModel,
    ScriptedModel,
)
from .projects import ProjectError, ProjectStore, ensure_environment
from .stock_prompts import TEMPLATE_IDS
from .tools import ToolOutcome
from .worker_client import WorkerAPIError, WorkerClient


def _read_secret(path: Path) -> str:
    return path.read_text().strip()


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
    key = _read_secret(args.api_key_file)
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
                    interruption.context(WorkerClient(args.worker, _read_secret(args.token_file)))
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


async def _project(args: argparse.Namespace) -> int:
    with contextlib.closing(ProjectStore(args.state_dir)) as store:
        return await _project_action(args, store)


async def _project_action(args: argparse.Namespace, store: ProjectStore) -> int:
    match args.action:
        case "create":
            record, created = store.create(args.project, args.profile, args.policy)
            print(f"{'created' if created else 'exists'}: {record.model_dump_json(indent=2)}")
        case "show":
            print(store.get(args.project).model_dump_json(indent=2))
        case "macros":
            from .documents import USER, DocumentStore, store_path
            from .documents.project_macros import ProjectMacros

            store.get(args.project)
            documents = DocumentStore(store_path(args.state_dir), producer="user-cli")
            try:
                macros = ProjectMacros(documents)
                if args.macros_file is None:
                    result = macros.get(args.project)
                else:
                    if args.expected is None or args.operation_id is None:
                        raise ValueError("macro changes need --expected and --operation-id")
                    with args.macros_file.open("rb") as source:
                        raw = source.read(16385)
                    if len(raw) > 16384:
                        raise ValueError("macro JSON exceeds 16384 bytes")
                    result = macros.change(
                        args.project,
                        {
                            "macros": json.loads(raw),
                            "expected": args.expected,
                            "operation_id": args.operation_id,
                        },
                        USER,
                    )
                print(json.dumps(result, ensure_ascii=False))
            except (DocumentError, ValueError, OSError, RecursionError):
                print(
                    "error: project macro request is invalid, stale or unavailable", file=sys.stderr
                )
                return 1
            finally:
                documents.close()
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
    with contextlib.closing(ProjectStore(args.state_dir)) as store:
        return await _session_action(args, store)


async def _session_action(args: argparse.Namespace, store: ProjectStore) -> int:
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


def _document(args: argparse.Namespace) -> int:
    """The user's document commands, on the control store in the state directory."""
    with contextlib.closing(ProjectStore(args.state_dir, readonly=True)) as projects:
        project = projects.get(args.project)
    store = DocumentStore(store_path(args.state_dir), producer="harness-cli")
    try:
        match args.action:
            case "import":
                if args.session is not None and args.session not in project.sessions:
                    raise ProjectError(
                        ErrorKind.NOT_FOUND,
                        f"project {args.project} has no session {args.session}",
                    )
                text = args.file.read_text(encoding="utf-8")
                author = Author(kind="import", user=USER.user, import_id=new_id("import"))
                reason = args.reason or f"imported from {args.file.name}"
                existing = [
                    info
                    for info in store.documents(args.project)
                    if info.name == args.name.removesuffix(".md")
                    and info.session_id == args.session
                ]
                if existing:
                    if args.base is None:
                        # Saving over the current revision would undo what others wrote
                        # after the file was made.
                        raise DocumentError(
                            CONFLICT,
                            f"document {existing[0].name} exists, at revision "
                            f"{existing[0].head}: give --base, the revision the file was "
                            "made from (document export writes one to a file)",
                        )
                    result = store.save_text(
                        args.project,
                        existing[0].document_id,
                        base=args.base,
                        text=text,
                        author=author,
                        reason=reason,
                    )
                else:
                    result = store.create(
                        args.project,
                        name=args.name,
                        title=args.title or args.name,
                        text=text,
                        author=author,
                        session_id=args.session,
                        reason=reason,
                    )
                print(result.model_dump_json(indent=2, exclude_defaults=True))
            case "list":
                for info in store.documents(args.project, deleted=args.deleted):
                    where = f"session {info.session_id}" if info.session_id else "project"
                    state = " (deleted)" if info.deleted else ""
                    print(
                        f"{info.document_id}  {info.name}  r{info.head}  {where}  "
                        f"{info.title}{state}"
                    )
            case "show":
                mapped = store.mapped(args.project, args.document, args.revision)
                print(json.dumps(view_blocks(mapped), indent=2, ensure_ascii=False))
            case "export":
                revision = store.revision(args.project, args.document, args.revision)
                if args.output is None:
                    sys.stdout.write(revision.text)
                else:
                    args.output.write_text(revision.text, encoding="utf-8")
                print(
                    f"revision {revision.info.number} of document {args.document}: import "
                    f"the file again with --base {revision.info.number}",
                    file=sys.stderr,
                )
            case "history":
                print(
                    json.dumps(
                        store.history(args.project, args.document), indent=2, ensure_ascii=False
                    )
                )
            case "suggestions":
                info = store.set_suggestions(
                    args.project, args.document, on=args.state == "on", author=USER
                )
                taken = "as suggestions" if info.takes_suggestions else "directly"
                print(f"document {info.name} takes agent patches {taken}")
            case "decide":
                result = store.decide(
                    args.project,
                    args.document,
                    args.patch,
                    accept=args.decision == "accept",
                    author=USER,
                )
                print(result.model_dump_json(indent=2, exclude_defaults=True))
            case _:
                result = store.delete(args.project, args.document, author=USER, reason=args.reason)
                print(result.model_dump_json(indent=2, exclude_defaults=True))
    except DocumentError as error:
        print(f"error: {error.kind}: {error.message}", file=sys.stderr)
        return 1
    finally:
        store.close()
    return 0


def _document_commands(commands: Any) -> None:
    document = commands.add_parser("document", help="living documents, as the user")
    actions = document.add_subparsers(dest="action", required=True)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--state-dir", type=Path, required=True)
    common.add_argument("--project", required=True)
    imported = actions.add_parser(
        "import", parents=[common], help="create a document, or a new revision of it"
    )
    imported.add_argument("--name", required=True)
    imported.add_argument("--title", help="a new document's title (default: its name)")
    imported.add_argument("--session", help="a workbook document of this session")
    imported.add_argument("--reason")
    imported.add_argument(
        "--base", type=int, help="the revision the file was made from, to save it over"
    )
    imported.add_argument("file", type=Path)
    exported = actions.add_parser(
        "export", parents=[common], help="write a revision's text, to edit and import again"
    )
    exported.add_argument("document")
    exported.add_argument("--revision", type=int)
    exported.add_argument("--output", type=Path, help="the file (default: standard output)")
    listed = actions.add_parser("list", parents=[common], help="the project's documents")
    listed.add_argument("--deleted", action="store_true", help="include deleted documents")
    shown = actions.add_parser("show", parents=[common], help="a revision's blocks with IDs")
    shown.add_argument("document")
    shown.add_argument("--revision", type=int)
    history = actions.add_parser("history", parents=[common], help="who changed which blocks")
    history.add_argument("document")
    suggestions = actions.add_parser(
        "suggestions", parents=[common], help="take agent patches as suggestions, or not"
    )
    suggestions.add_argument("document")
    suggestions.add_argument("state", choices=("on", "off"))
    decide = actions.add_parser("decide", parents=[common], help="accept or reject a suggestion")
    decide.add_argument("document")
    decide.add_argument("patch")
    decide.add_argument("decision", choices=("accept", "reject"))
    delete = actions.add_parser("delete", parents=[common], help="delete a document")
    delete.add_argument("document")
    delete.add_argument("--reason", required=True)


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
    project = commands.add_parser("project", help="create, show or revise a project")
    project.add_argument("action", choices=("create", "show", "revise", "macros"))
    project.add_argument("--state-dir", type=Path, required=True)
    project.add_argument("--project", required=True)
    project.add_argument("--profile", help="create: the base profile, such as minimal")
    project.add_argument("--policy", help="create, revise: the policy revision (sha256:...)")
    project.add_argument("--worker", help="revise: also revise the project's environments")
    project.add_argument("--token-file", type=Path)
    project.add_argument(
        "--macros-file", type=Path, help="macros: bounded JSON definitions; omitted to read"
    )
    project.add_argument("--expected", type=int, help="macros: exact current settings revision")
    project.add_argument("--operation-id", help="macros: stable retry identity")
    session = commands.add_parser("session", help="close a session: stop its kernels")
    session.add_argument("action", choices=("close",))
    session.add_argument("--state-dir", type=Path, required=True)
    session.add_argument("--project", required=True)
    session.add_argument("--session", required=True)
    session.add_argument("--worker", required=True)
    session.add_argument("--token-file", type=Path, required=True)
    _document_commands(commands)
    args = parser.parse_args(argv)
    if args.command == "document":
        try:
            return _document(args)
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
    except (WorkerAPIError, ModelError, ProjectError, DocumentError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
