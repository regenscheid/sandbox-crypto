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

With ``--state-dir``, the session runs in its project's default environment, or in
an environment of its own with ``--dedicated`` (W3, projects.py). The harness has
the worker create or revise it first, and keeps the run's record, the same that
``--transcript`` writes, beside the project's (W4). The run ID then names that
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
import json
import math
import sqlite3
import sys
import threading
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import TypeAdapter, ValidationError

from sandbox_science.contracts import ErrorKind, ExecutionContext, Identifier, Location, new_id

from .artifact_publisher import ArtifactPublisher
from .artifacts import ArtifactInventory
from .consistency.controller import ReviewController
from .consistency.mathnotices import MathNotices
from .consistency.notices import FindingNotices
from .consistency.store import ReviewStore
from .documents import USER, Author, DocumentError, DocumentStore, DocumentTool, store_path
from .documents.errors import CONFLICT
from .documents.mathcontroller import MathController
from .documents.mathjobs import threaded
from .documents.tool import view_blocks
from .hooks import TurnHook
from .jobs import JobTools
from .jobstate import JobState
from .kernel_delivery import KernelDeliveryController
from .loop import Interruption, Transcript, run_task
from .mathcheck import MathChecker
from .model import ChatModel, ModelError, OpenAICompatibleModel, ScriptedModel
from .plan_tools import PlanTools
from .projects import ProjectError, ProjectStore, RunRecord, ensure_environment
from .session_plans import SessionPlans
from .stock_prompts import TEMPLATE_IDS
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
    """Keep passive delivery ownership through existing cleanup, durable save and ACK."""
    owners: list[KernelDeliveryController] = []
    interruption = Interruption()
    try:
        try:
            with interruption.protect():
                return await _run_main(args, owners)
        finally:
            for owner in owners:
                try:
                    with interruption.protect():
                        await owner.close()
                except Exception:
                    print(
                        "kernel notices: cleanup unavailable; delivery may remain unresolved",
                        file=sys.stderr,
                    )
    except BaseException:
        if interruption.error is not None:
            raise interruption.error from None
        raise


async def _run_main(args: argparse.Namespace, notice_owners: list[KernelDeliveryController]) -> int:
    model: ChatModel
    if args.script is not None:
        model = ScriptedModel.from_json(json.loads(args.script.read_text()))
    else:
        model = OpenAICompatibleModel(args.model_url, args.model, _read_secret(args.api_key_file))
    run_id = args.run_id or new_id("run")
    started = datetime.now(UTC)
    transcript: Transcript | None = None
    context: ExecutionContext | None = None
    hooks: Sequence[TurnHook] = ()
    interruption = Interruption()
    try:
        async with interruption.context(
            WorkerClient(args.worker, _read_secret(args.token_file))
        ) as client:
            with interruption.protect():
                context = await _context(args, client)
                capabilities = await client.capabilities(context.environment_id)
                location = capabilities.location
                tier = tier_in_effect(location)
                if not args.allow_unisolated and (tier is None or tier < args.min_isolation_tier):
                    print(
                        f"error: the worker offers {describe_isolation(location)}. At least tier "
                        f"{args.min_isolation_tier} is required "
                        "(--allow-unisolated for development)",
                        file=sys.stderr,
                    )
                    return 2
                print(
                    f"worker: pool {location.pool_id}, {location.backend} backend, sandbox "
                    f"{location.sandbox_id}, host {location.hostname}, "
                    f"{describe_isolation(location)}"
                )

                async def show(name: str, arguments: str, outcome: ToolOutcome) -> None:
                    print(f"\n→ {name} {arguments}\n← {outcome.text}")

                documents = None
                if args.state_dir is not None:
                    documents = DocumentTool(
                        DocumentStore(store_path(args.state_dir), producer="harness"),
                        context,
                        run_id=run_id,
                        alias="scripted" if args.script is not None else args.model,
                    )
                job_state = JobState(args.state_dir) if args.state_dir is not None else None
                watcher: ChatModel | None = None
                watcher_alias = args.watcher_model
                if watcher_alias is not None:
                    if args.watcher_script is not None:
                        watcher = ScriptedModel.from_json(
                            json.loads(args.watcher_script.read_text())
                        )
                    else:
                        watcher = OpenAICompatibleModel(
                            args.model_url,
                            watcher_alias,
                            _read_secret(args.api_key_file),
                            max_tokens=300,
                            max_response_bytes=16384,
                        )
                reviewer: ChatModel | None = None
                if args.reviewer_model is not None:
                    reviewer = (
                        ScriptedModel.from_json(json.loads(args.reviewer_script.read_text()))
                        if args.reviewer_script is not None
                        else OpenAICompatibleModel(
                            args.model_url,
                            args.reviewer_model,
                            _read_secret(args.api_key_file),
                            max_tokens=2048,
                            max_response_bytes=65536,
                        )
                    )
                inventory = ArtifactInventory(documents.store) if documents is not None else None
                reviews = (
                    ReviewController(
                        ReviewStore(documents.store, artifacts=inventory),
                        context,
                        run_id=run_id,
                        model=reviewer,
                        alias=args.reviewer_model,
                        author_alias="scripted" if args.script is not None else args.model,
                    )
                    if documents is not None
                    else None
                )
                checker = (
                    MathChecker(args.math_node, args.math_launcher, args.math_bundle)
                    if args.math_node is not None
                    else None
                )
                math_checks = (
                    MathController(
                        reviews.store,
                        context.project_id,
                        context.session_id,
                        run_id,
                        checker=checker,
                    )
                    if reviews is not None
                    else None
                )
                jobs = JobTools(
                    client,
                    context,
                    run_id=run_id,
                    state=job_state,
                    watcher=watcher,
                    watcher_alias=watcher_alias,
                    watcher_calls_per_hour=args.watcher_calls_per_hour,
                    first_check_seconds=args.watcher_first_check,
                    check_interval_seconds=args.watcher_interval,
                )
                artifacts = (
                    ArtifactPublisher(
                        inventory,
                        client,
                        context,
                        run_id=run_id,
                        alias="scripted" if args.script is not None else args.model,
                    )
                    if documents is not None and inventory is not None
                    else None
                )
                plans = None
                kernel_notices = None
                if documents is not None and args.state_dir is not None:
                    kernel_notices = KernelDeliveryController(
                        ProjectStore(args.state_dir), context, run_id, client
                    )
                    # Publish ownership before initialization can persist a baseline
                    # or cancellation can discard any threaded return value.
                    notice_owners.append(kernel_notices)
                    await kernel_notices.initialize()
                plan_owners: list[PlanTools] = []

                def make_plans() -> PlanTools:
                    if documents is None:
                        raise RuntimeError("planning requires its recorded context")
                    owner = PlanTools(
                        SessionPlans(documents.store),
                        ProjectStore(args.state_dir),
                        context,
                        run_id=run_id,
                        alias="scripted" if args.script is not None else args.model,
                    )
                    # Publish ownership before cancellation-owned threaded()
                    # can discard its return value while propagating an interrupt.
                    plan_owners.append(owner)
                    return owner

                try:
                    with interruption.protect():
                        if documents is not None:
                            plans = await threaded(make_plans)
                        toolbox = Toolbox(
                            client,
                            context,
                            languages=capabilities.languages,
                            features=capabilities.features,
                            documents=documents,
                            jobs=jobs,
                            reviews=reviews,
                            math_checks=math_checks,
                            artifacts=artifacts,
                            plans=plans,
                            kernel_notices=kernel_notices,
                        )
                        hooks = toolbox.turn_hooks()
                        transcript = await run_task(
                            model,
                            toolbox,
                            args.task,
                            run_id=run_id,
                            max_turns=args.max_turns,
                            on_tool=show,
                            research_template=getattr(args, "research_template", None),
                            hooks=hooks,
                        )
                finally:
                    try:
                        if plan_owners:
                            with interruption.protect():
                                await threaded(plan_owners[0].close)
                    finally:
                        try:
                            try:
                                if artifacts is not None:
                                    with interruption.protect():
                                        await artifacts.aclose()
                            finally:
                                if reviews is not None:
                                    with interruption.protect():
                                        await reviews.close()
                        finally:
                            try:
                                if isinstance(reviewer, OpenAICompatibleModel):
                                    with interruption.protect():
                                        await reviewer.close()
                            finally:
                                try:
                                    if isinstance(watcher, OpenAICompatibleModel):
                                        with interruption.protect():
                                            await watcher.close()
                                finally:
                                    try:
                                        if documents is not None:
                                            with interruption.protect():
                                                documents.store.close()
                                    finally:
                                        if isinstance(model, OpenAICompatibleModel):
                                            with interruption.protect():
                                                await model.close()
    except BaseException as error:
        if interruption.error is not None:
            raise interruption.error from None
        if not isinstance(error, Exception):
            raise
        if transcript is None:
            raise
        transcript.fail("cleanup")
    if transcript is None or context is None:
        raise RuntimeError("the run did not start")
    if transcript.failure is not None:
        print(f"error: run failed during {transcript.failure}", file=sys.stderr)
    print(f"\n{transcript.final_text or '(no answer)'}")
    fields: dict[str, object] = {
        "run_id": run_id,
        "context": context.model_dump(mode="json"),
        "task": args.task,
        "started_at": started,
        "ended_at": datetime.now(UTC),
        "stopped": transcript.stopped,
        "failure": transcript.failure,
        "final_text": transcript.final_text,
        "messages": transcript.messages,
        "prompt_provenance": (
            transcript.prompt_provenance.model_dump(mode="json")
            if transcript.prompt_provenance is not None
            else None
        ),
        # Keep the original model until the bounded exact-type validator sees it;
        # dumping first could hide undeclared fields on copied host metadata.
        "kernel_notice_delivery": transcript.kernel_notice_delivery,
        "tools": transcript.tool_records,
        "delivered": {source: sorted(ids) for source, ids in transcript.delivered.items() if ids},
        "summaries": transcript.summaries,
    }
    status = 1 if transcript.failure is not None else 0 if transcript.stopped == "answered" else 3
    return await _save(args, fields, status, hooks, notice_owners[0] if notice_owners else None)


async def _save(
    args: argparse.Namespace,
    fields: dict[str, object],
    status: int,
    hooks: Sequence[TurnHook],
    owner: KernelDeliveryController | None = None,
) -> int:
    """Keep the run's record, then let each hook acknowledge what the saved record names.

    A crash between the save and an acknowledgement safely repeats those notices. A hook
    that fails to acknowledge is reported; the others still acknowledge theirs.
    """
    saved: list[RunRecord] = []
    if owner is None:
        status = _keep(args, fields, status, saved=saved.append)
    else:
        cancelled = threading.Event()
        status = await owner.save(
            lambda: _keep(
                args, fields, status, saved=saved.append, may_ack=lambda: not cancelled.is_set()
            ),
            cancelled=cancelled,
        )
    if not saved:
        return status
    for hook in hooks:
        try:
            await hook.acknowledge(saved[0])
        except Exception as exc:
            print(f"error: the run's {hook.name} could not be acknowledged: {exc}", file=sys.stderr)
            status = 1
    if owner is not None and saved[0].kernel_notice_delivery is not None:
        try:
            acknowledged = await owner.acknowledge_saved()
        except Exception:
            acknowledged = False
        if not acknowledged:
            print("kernel notices: saved delivery remains unresolved", file=sys.stderr)
    return status


def _keep(
    args: argparse.Namespace,
    fields: dict[str, object],
    status: int,
    *,
    saved: Callable[[RunRecord], None] | None = None,
    may_ack: Callable[[], bool] | None = None,
) -> int:
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
            if saved is not None:
                saved(record)
            if may_ack is not None and not may_ack():
                return status
            # Save first: a crash before this acknowledgement safely repeats notices.
            if review_ids := record.delivered.get("reviews"):
                review_documents = DocumentStore(store_path(args.state_dir), producer="harness")
                try:
                    FindingNotices(ReviewStore(review_documents)).acknowledge(
                        record.context.project_id,
                        record.context.session_id,
                        record.run_id,
                        review_ids,
                    )
                finally:
                    review_documents.close()
            if math_ids := record.delivered.get("math"):
                math_documents = DocumentStore(store_path(args.state_dir), producer="harness")
                try:
                    MathNotices(ReviewStore(math_documents)).acknowledge(
                        record.context.project_id,
                        record.context.session_id,
                        record.run_id,
                        math_ids,
                        finalize=record.failure is None,
                    )
                finally:
                    math_documents.close()
        except (
            ProjectError,
            DocumentError,
            OSError,
            ValueError,
            RecursionError,
            sqlite3.Error,
        ) as exc:
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


def _document(args: argparse.Namespace) -> int:
    """The user's document commands, on the control store in the state directory."""
    projects = ProjectStore(args.state_dir)
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
    except (WorkerAPIError, ModelError, ProjectError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
