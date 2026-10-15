# SPDX-License-Identifier: MIT
"""The user's project, session and document commands of ``sandbox-science-harness``.

``project`` creates, shows or revises a project's record, its macros and its settings:
its model budget, its files and its delegation. ``session close`` stops a session's
kernels; the ``document`` commands act on the living documents in the state directory's
control store, as the user: an import is attributed to an import the user started, and
the other changes to the user. An import into an existing document names the revision
its file was made from (``--base``), which must be the current one, so that it never
undoes revisions made since.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import sys
from pathlib import Path
from typing import Any

from sandbox_science.contracts import ErrorKind, ExecutionContext, new_id

from .documents import USER, Author, DocumentError, DocumentStore, store_path
from .documents.agent_text import visible
from .documents.errors import CONFLICT
from .documents.tool import view_blocks
from .ledger import Ledger
from .mathchecks import subscribe as check_math
from .projects import ProjectError, ProjectStore, ensure_environment
from .worker_client import WorkerClient


def read_secret(path: Path) -> str:
    return path.read_text().strip()


async def project(args: argparse.Namespace) -> int:
    with contextlib.closing(ProjectStore(args.state_dir)) as store:
        return await _project_action(args, store)


async def _project_action(args: argparse.Namespace, store: ProjectStore) -> int:
    match args.action:
        case "create":
            record, created = store.create(args.project, args.profile, args.policy)
            print(f"{'created' if created else 'exists'}: {record.model_dump_json(indent=2)}")
        case "show":
            print(store.get(args.project).model_dump_json(indent=2))
        case "budget":
            from .runstate import RunState

            store.get(args.project)
            runs = RunState(store.control)
            if args.main_tokens is not None:
                runs.set_budget(args.project, args.main_tokens)
            print(f"project {args.project}: {runs.budget(args.project)} MAIN tokens a day")
        case "delegation":
            from .delegation.records import DelegationRecords

            store.get(args.project)
            delegations = DelegationRecords(store.control)
            changes: dict[str, object] = {
                name: value
                for name in ("depth", "cap", "check_tokens", "check_turns")
                if (value := getattr(args, name)) is not None
            }
            if args.models is not None:  # empty: no choice
                changes["models"] = tuple(m.strip() for m in args.models.split(",") if m.strip())
            if args.fallback is not None:  # empty: none
                changes["fallback"] = args.fallback.strip() or None
            found = (
                delegations.configure(args.project, **changes)
                if changes
                else delegations.settings(args.project)
            )
            state = "off" if found.depth == 0 else "main runs delegate checks"
            models = ", ".join(found.models) or "none"
            print(
                f"project {args.project}: delegation depth {found.depth} ({state}); at most "
                f"{found.cap} delegated runs at once; a check gets {found.check_tokens} "
                f"tokens and {found.check_turns} turns; a brief may choose the models: "
                f"{models}; fallback: {found.fallback or 'none'}"
            )
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
        case "files":
            return _files(args, store)
        case _:
            record = store.set_policy(args.project, args.policy)
            print(f"project {record.project_id} now uses {record.policy}")
            if args.worker is not None:
                async with WorkerClient(args.worker, read_secret(args.token_file)) as client:
                    for environment_id in record.environments():
                        info = await ensure_environment(client, record, environment_id)
                        state = "not managed by the worker" if info is None else info.policy
                        print(f"environment {environment_id}: {state}")
    return 0


def _files(args: argparse.Namespace, store: ProjectStore) -> int:
    """Show or change the project's file settings (research artifacts, RA-29, RA-39)."""
    from .file_settings import DEFAULT_IGNORE, ProjectFiles

    store.get(args.project)
    documents = DocumentStore(store_path(args.state_dir), producer="user-cli")
    try:
        files = ProjectFiles(documents)
        current = files.get(args.project)
        changing = args.recording or args.ignore or args.ignore_defaults or args.no_ignore
        if changing:
            if args.expected is None or args.operation_id is None:
                raise ValueError("file settings changes need --expected and --operation-id")
            if args.no_ignore and (args.ignore or args.ignore_defaults):
                raise ValueError("--no-ignore takes no patterns")
            patterns = (
                []
                if args.no_ignore
                else [*(DEFAULT_IGNORE if args.ignore_defaults else ()), *(args.ignore or ())]
            )
            if not (args.ignore or args.ignore_defaults or args.no_ignore):
                patterns = list(current.ignore)
            current, _ = files.change(
                args.project,
                {
                    "expected": args.expected,
                    "recording": args.recording or current.recording,
                    "ignore": patterns,
                    "operation_id": args.operation_id,
                },
                USER,
            )
        print(current.model_dump_json(indent=2))
    except (DocumentError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    finally:
        documents.close()
    return 0


async def session(args: argparse.Namespace) -> int:
    with contextlib.closing(ProjectStore(args.state_dir)) as store:
        return await _session_action(args, store)


async def _session_action(args: argparse.Namespace, store: ProjectStore) -> int:
    if args.action == "open":
        opened = store.open_session(args.project, args.session, dedicated=args.dedicated)
        print(f"session {args.session} is open, in environment {opened.environment_id}")
        return 0
    session = store.get(args.project).sessions.get(args.session)
    if session is None:
        print(f"error: project {args.project} has no session {args.session}", file=sys.stderr)
        return 1
    context = ExecutionContext(
        project_id=args.project, session_id=args.session, environment_id=session.environment_id
    )
    async with WorkerClient(args.worker, read_secret(args.token_file)) as client:
        closed = await client.close_session(context)
    store.close_session(args.project, args.session)
    kernels = ", ".join(f"{k.language} {k.kernel_id}" for k in closed.kernels) or "none"
    print(f"closed session {args.session}; kernels stopped: {kernels}")
    return 0


def document(args: argparse.Namespace) -> int:
    """The user's document commands, on the control store in the state directory."""
    with contextlib.closing(ProjectStore(args.state_dir, readonly=True)) as projects:
        project = projects.get(args.project)
    store = DocumentStore(store_path(args.state_dir), producer="harness-cli")
    check_math(store)
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
                if args.decision == "accept" and args.base is None:
                    # What the user accepts is what agents read: the text first, exactly,
                    # each hidden character a code point (threat model, A3).
                    preview = store.preview_patch(args.project, args.document, args.patch)
                    if preview["proposed"] is None:
                        why = preview["message"] or "it changes blocks it does not name"
                        raise DocumentError(
                            CONFLICT,
                            f"suggestion {args.patch} does not apply to revision "
                            f"{preview['base']}: {why}",
                        )
                    sys.stdout.write(visible(preview["proposed"]))
                    print(
                        f"error: the text above is revision {preview['base']} with suggestion "
                        f"{args.patch}, exactly as it would be saved, each hidden character "
                        "shown as its code point; to accept it, run again with --base "
                        f"{preview['base']}",
                        file=sys.stderr,
                    )
                    return 1
                result = store.decide(
                    args.project,
                    args.document,
                    args.patch,
                    accept=args.decision == "accept",
                    author=USER,
                    base=args.base,
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


def ledger(args: argparse.Namespace) -> int:
    """The user's ledger commands: ``check`` records that the user checked an entry at a
    revision, which the gate's independent check counts (LG-17), or a reference, which
    vouches for the PDF the revision names (LG-16); ``clear`` clears open
    flags on an entry with a reason, and ``reopen`` opens cleared ones again (LG-29);
    ``drop`` drops a ``dominates`` link an entry holds, with a reason (LG-22)."""
    with contextlib.closing(ProjectStore(args.state_dir, readonly=True)) as projects:
        projects.get(args.project)
    store = DocumentStore(store_path(args.state_dir), producer="harness-cli")
    try:
        result = (
            Ledger(store).check(
                args.project,
                args.entry,
                args.revision,
                USER,
                operation_id=args.operation_id or new_id("check"),
                note=args.note,
            )
            if args.action == "check"
            else Ledger(store).clear_flags(
                args.project,
                args.entry,
                args.flag,
                USER,
                reason=args.reason,
                operation_id=args.operation_id or new_id("clear"),
                cause=args.cause,
            )
            if args.action == "clear"
            else Ledger(store).drop_dominates(
                args.project,
                args.entry,
                args.dominates,
                args.revision,
                USER,
                reason=args.reason,
                operation_id=args.operation_id or new_id("drop"),
            )
            if args.action == "drop"
            else Ledger(store).reopen_flags(
                args.project,
                args.entry,
                args.flag,
                USER,
                reason=args.reason,
                operation_id=args.operation_id or new_id("reopen"),
            )
        )
        print(result.model_dump_json(indent=2))
    except DocumentError as error:
        print(f"error: {error.kind}: {error.message}", file=sys.stderr)
        return 1
    finally:
        store.close()
    return 0


def add_parsers(commands: Any) -> None:
    projects = commands.add_parser("project", help="create, show or revise a project")
    projects.add_argument(
        "action",
        choices=("create", "show", "revise", "macros", "budget", "files", "delegation"),
    )
    projects.add_argument("--state-dir", type=Path, required=True)
    projects.add_argument("--project", required=True)
    projects.add_argument("--profile", help="create: the base profile, such as minimal")
    projects.add_argument("--policy", help="create, revise: the policy revision (sha256:...)")
    projects.add_argument("--worker", help="revise: also revise the project's environments")
    projects.add_argument("--token-file", type=Path)
    projects.add_argument(
        "--macros-file", type=Path, help="macros: bounded JSON definitions; omitted to read"
    )
    projects.add_argument(
        "--expected", type=int, help="macros, files: exact current settings revision"
    )
    projects.add_argument("--operation-id", help="macros, files: stable retry identity")
    projects.add_argument(
        "--recording",
        choices=("all", "results"),
        help="files: record every written file, or only results and published files",
    )
    projects.add_argument(
        "--ignore", action="append", metavar="PATTERN", help="files: an ignore pattern (repeat)"
    )
    projects.add_argument(
        "--ignore-defaults", action="store_true", help="files: start from the default patterns"
    )
    projects.add_argument("--no-ignore", action="store_true", help="files: no ignore patterns")
    projects.add_argument(
        "--main-tokens",
        type=int,
        help="budget: the MAIN tokens the run service may spend over a rolling 24 hours; "
        "omitted to read",
    )
    projects.add_argument(
        "--depth",
        type=int,
        choices=(0, 1),
        help="delegation: 1 lets main runs delegate checks, 0 (the default) does not",
    )
    projects.add_argument(
        "--cap", type=int, help="delegation: delegated runs a session runs at once (10)"
    )
    projects.add_argument(
        "--check-tokens", type=int, help="delegation: a check's tokens, at most (150000)"
    )
    projects.add_argument(
        "--check-turns", type=int, help="delegation: a check's turns, at most (12)"
    )
    projects.add_argument(
        "--models",
        help="delegation: the model aliases a brief may choose, comma-separated; empty for "
        "none (the default)",
    )
    projects.add_argument(
        "--fallback",
        help="delegation: the alias a delegated run calls when the gateway lists none of "
        "its own; empty for none (the default)",
    )
    sessions = commands.add_parser(
        "session", help="open a session, so the GUI can chat in it; or close one: stop its kernels"
    )
    sessions.add_argument("action", choices=("open", "close"))
    sessions.add_argument("--state-dir", type=Path, required=True)
    sessions.add_argument("--project", required=True)
    sessions.add_argument("--session", required=True)
    sessions.add_argument(
        "--dedicated", action="store_true", help="open: in an environment of its own"
    )
    sessions.add_argument("--worker", help="close: the worker that runs its kernels")
    sessions.add_argument("--token-file", type=Path)
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
    decide.add_argument(
        "--base",
        type=int,
        help="the revision the suggestion's text was shown against; accepting needs it",
    )
    delete = actions.add_parser("delete", parents=[common], help="delete a document")
    delete.add_argument("document")
    delete.add_argument("--reason", required=True)
    ledger = commands.add_parser("ledger", help="the project ledger, as the user")
    ledger_actions = ledger.add_subparsers(dest="action", required=True)
    checked = ledger_actions.add_parser(
        "check",
        parents=[common],
        help="record that you checked an entry at the revision you read (LG-17); "
        "a reference's check vouches for its PDF (LG-16)",
    )
    checked.add_argument("entry", help="the entry's handle (such as C-12) or ID")
    checked.add_argument("--revision", type=int, required=True, help="the revision you checked")
    checked.add_argument("--note", help="what you checked, as the check's scope")
    checked.add_argument("--operation-id", help="retry with the same ID (default: a new one)")
    clear = ledger_actions.add_parser(
        "clear", parents=[common], help="clear open flags on an entry, with the reason (LG-29)"
    )
    clear.add_argument("entry", help="the entry's handle (such as C-12) or ID")
    which = clear.add_mutually_exclusive_group(required=True)
    which.add_argument("--flag", action="append", help="a flag's ID (repeat for more)")
    which.add_argument("--cause", help="every open flag of this cause on the entry")
    which.add_argument("--all", action="store_true", help="every open flag on the entry")
    clear.add_argument("--reason", required=True, help="why the entry still holds")
    clear.add_argument("--operation-id", help="retry with the same ID (default: a new one)")
    reopen = ledger_actions.add_parser(
        "reopen", parents=[common], help="open cleared flags on an entry again (LG-29)"
    )
    reopen.add_argument("entry", help="the entry's handle (such as C-12) or ID")
    reopen.add_argument(
        "--flag", action="append", required=True, help="a cleared flag's ID (repeat for more)"
    )
    reopen.add_argument("--reason", required=True, help="why it needs reconsidering")
    reopen.add_argument("--operation-id", help="retry with the same ID (default: a new one)")
    drop = ledger_actions.add_parser(
        "drop", parents=[common], help="drop a dominates link an entry holds (LG-22)"
    )
    drop.add_argument("entry", help="the result that holds the link (such as R-1)")
    drop.add_argument("--dominates", required=True, help="the result it names (such as R-3)")
    drop.add_argument("--revision", type=int, required=True, help="the revision you read")
    drop.add_argument("--reason", required=True, help="why the link does not hold")
    drop.add_argument("--operation-id", help="retry with the same ID (default: a new one)")
