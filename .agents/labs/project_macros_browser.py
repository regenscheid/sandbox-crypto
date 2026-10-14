# SPDX-License-Identifier: MIT
"""Disposable LD-28 browser fixture; no browser, model, worker or VM automation.

Run from the frozen project-macros worktree with its locked Python environment.
`serve` prints public fixture metadata; only its exclusive 0600 info file contains
the login URL. `change` is a separate explicit USER settings write. `inspect`
exports current/history contexts. Both commands accept only this marked fixture.
Optional checking requires all three exact paths AND their verified SHA-256s.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import secrets
import signal
import socket
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from aiohttp import web

from sandbox_science.orchestrator import mathchecks
from sandbox_science.orchestrator.consistency.findings import Findings
from sandbox_science.orchestrator.consistency.store import ReviewStore
from sandbox_science.orchestrator.documents import USER, Author, DocumentStore, store_path
from sandbox_science.orchestrator.documents.annotations import Annotations
from sandbox_science.orchestrator.documents.project_macros import ProjectMacros, view
from sandbox_science.orchestrator.mathcheck import MathChecker
from sandbox_science.orchestrator.mathchecks.jobs import MathJobs
from sandbox_science.orchestrator.projects import ProjectStore
from sandbox_science.workspace.app import create_app

SOURCE = Path("~/.codex/worktrees/project-macros/sandbox-crypto")
SOURCE_HEAD = "8797a5565a7b9ce329e272c138fcf2399b87fd19"
MARKER = "disposable-project-macros-browser-v1\n"
A, B = "macro-project-a", "macro-project-b"
MAIN = Author(kind="agent", session_id="a", run_id="fixture-main", role="main", model="fixture")
INITIAL = {r"\F": r"\mathbb{F}", r"\Z": r"\mathbb{Z}"}
CURRENT = {r"\F": r"\mathcal{F}", r"\Z": r"\mathcal{Z}"}
CONCURRENT = {r"\F": r"\mathfrak{F}", r"\Z": r"\mathfrak{Z}"}
OVERRIDE = r"""---
macros:
  '\F': '\mathbf{F}'
---
# Document override

Document F wins: $\F$. Inherited Z: $\Z$.

Stable agent-owned diagnostic: $\MissingMacro{x}$.
"""


def digest(path: Path) -> str:
    if not path.is_file() or path.stat().st_size > 256 * 1024 * 1024:
        raise ValueError("Expected a bounded regular input file")
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def checker(args: argparse.Namespace) -> MathChecker | None:
    supplied = [getattr(args, key) for key in ("node", "launcher", "bundle")]
    hashes = [getattr(args, key + "_sha256") for key in ("node", "launcher", "bundle")]
    if not any(supplied + hashes):
        return None
    if not all(supplied + hashes):
        raise ValueError("Supply all checker paths and SHA-256s, or none")
    for path, expected in zip(supplied, hashes, strict=True):
        if digest(path) != expected:
            raise ValueError("Explicit checker input SHA-256 mismatch")
    if not os.access(args.node, os.X_OK):
        raise ValueError("Explicit Node binary is not executable")
    return MathChecker(args.node, args.launcher, args.bundle)


async def drain(documents: DocumentStore, renderer: MathChecker | None) -> None:
    if renderer is None:
        return
    jobs = MathJobs(ReviewStore(documents))
    async with asyncio.timeout(60):
        for project in (A, B):
            jobs.retry_unavailable(project, renderer.identity())
            for _ in range(40):
                check = jobs.claim(project, "project-macros-browser")
                if check is None:
                    break
                for _ in range(40):
                    if await jobs.step(check, "project-macros-browser", renderer):
                        break
                else:
                    raise RuntimeError("Fixture math attempt exceeded its step budget")
            else:
                raise RuntimeError("Fixture math queue exceeded its admission budget")


def checked(root: Path) -> tuple[Path, dict]:
    root = root.absolute()
    if (
        root.is_symlink()
        or root.parent.resolve() != Path(tempfile.gettempdir()).resolve()
        or not root.name.startswith("project-macros-browser-")
        or (root / ".project-macros-fixture").is_symlink()
        or (root / ".project-macros-fixture").read_text() != MARKER
    ):
        raise ValueError("Not the marked disposable project-macros fixture")
    record = root / "fixture.json"
    if record.is_symlink() or record.stat().st_size > 16_384:
        raise ValueError("Invalid fixture metadata")
    info = json.loads(record.read_text())
    if info["source_head"] != SOURCE_HEAD or set(info["documents"]) != {
        "override",
        "inheritance",
        "other_project",
    }:
        raise ValueError("Fixture metadata does not match this helper")
    return root, info


def snapshot(documents: DocumentStore, info: dict, *, checked_math: bool) -> dict:
    store = ReviewStore(documents)
    jobs, settings = MathJobs(store), ProjectMacros(documents)
    contexts = {}
    for name, document in info["documents"].items():
        project = B if name == "other_project" else A
        head = documents.document(project, document).head
        contexts[name] = {
            "document": document,
            "head": head,
            "current": view(documents, project, document=document, revision=head),
            "historical_1": view(
                documents, project, document=document, revision=1, historical=True
            ),
        }
    return {
        "source_head": SOURCE_HEAD,
        "checker_invoked": checked_math,
        "settings": {p: settings.get(p) for p in (A, B)},
        "contexts": contexts,
        "checks": {p: jobs.listing(p, session=s) for p, s in ((A, "a"), (B, "b"))},
        "findings": {p: Findings(store).listing(p, s) for p, s in ((A, "a"), (B, "b"))},
    }


async def seed(root: Path, renderer: MathChecker | None) -> dict:
    projects = ProjectStore(root)
    for project, session in ((A, "a"), (B, "b")):
        projects.create(project, "minimal", "sha256:" + "a" * 64)
        projects.open_session(project, session, dedicated=False)
    documents = DocumentStore(store_path(root), producer="browser-fixture")
    mathchecks.subscribe(documents)  # as the harness and the workspace do
    try:
        settings = ProjectMacros(documents)
        settings.change(A, {"expected": 0, "macros": INITIAL, "operation_id": "seed-a-1"}, USER)
        settings.change(
            B, {"expected": 0, "macros": {r"\F": r"\mathsf{F}"}, "operation_id": "seed-b-1"}, USER
        )
        ids = {}
        for name, project, text, author in (
            ("override", A, OVERRIDE, MAIN),
            ("inheritance", A, "# Project inheritance\n\n$\\F$ and $\\Z$.\n", MAIN),
            ("other_project", B, "# Other project\n\nOwn F: $\\F$. Z is undefined: $\\Z$.\n", USER),
        ):
            ids[name] = documents.create(
                project, name=name, title=name.replace("_", " ").title(), text=text, author=author
            ).document_id
        annotation = Annotations(documents).change(
            A,
            ids["override"],
            {
                "action": "create",
                "anchor": {
                    "revision": 1,
                    "block": documents.mapped(A, ids["override"]).block_ids[-1],
                },
                "body": r"MAIN comment: $\F$ and $\Z$.",
                "operation_id": "seed-comment",
            },
            MAIN,
        )
        await drain(documents, renderer)
        settings.change(A, {"expected": 1, "macros": CURRENT, "operation_id": "seed-a-2"}, USER)
        documents.edit_source(
            A,
            ids["override"],
            base=1,
            text=OVERRIDE + "\nRevision two keeps the math unchanged.\n",
            author=USER,
            reason="Create explicit historical source pin",
            operation_id="seed-source-2",
        )
        await drain(documents, renderer)
        info = {
            "source_head": SOURCE_HEAD,
            "fixture": str(root),
            "projects": {A: "a", B: "b"},
            "documents": ids,
            "comment": {k: annotation[k] for k in ("thread_id", "entry_id")},
            "initial_project_revision": 2,
            "expected_override_F": r"\mathbf{F}",
            "expected_current_Z": r"\mathcal{Z}",
            "expected_historical_1_Z": r"\mathbb{Z}",
            "other_project_has_no_Z": True,
        }
        (root / "fixture.json").write_text(json.dumps(info, indent=2) + "\n")
        return {**info, "inspection": snapshot(documents, info, checked_math=renderer is not None)}
    finally:
        documents.close()


async def serve(args: argparse.Namespace) -> None:
    static = args.static.resolve()
    if static != (SOURCE / "web/workspace/dist").resolve() or not (static / "index.html").is_file():
        raise ValueError("Use the frozen worktree's actual production workspace dist")
    if (
        args.info.absolute() == args.cleanup.absolute()
        or args.info.exists()
        or args.cleanup.exists()
    ):
        raise ValueError("Private info and public cleanup paths must be distinct and absent")
    renderer = checker(args)
    stopped = asyncio.Event()
    for signum in (signal.SIGINT, signal.SIGTERM):
        asyncio.get_running_loop().add_signal_handler(signum, stopped.set)
    fixture = tempfile.TemporaryDirectory(prefix="project-macros-browser-")
    root = Path(fixture.name)
    runner, port, owned_info = None, None, False
    errors = []
    try:
        (root / ".project-macros-fixture").write_text(MARKER)
        info = await seed(root, renderer)
        token = secrets.token_urlsafe(32)
        runner = web.AppRunner(
            create_app(ProjectStore(root), static, login_token=token), access_log=None
        )
        await runner.setup()
        await web.TCPSite(runner, "127.0.0.1", 0).start()
        port = int(runner.addresses[0][1])
        public = {
            **info,
            "port": port,
            "pid": os.getpid(),
            "static": str(static),
            "build_files": {
                str(path.relative_to(static)): digest(path)
                for path in sorted(static.rglob("*"))
                if path.is_file()
            },
        }
        descriptor = os.open(args.info, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        owned_info = True
        with os.fdopen(descriptor, "w") as stream:
            stream.write(
                json.dumps({**public, "login_url": f"http://127.0.0.1:{port}/login?token={token}"})
                + "\n"
            )
        print(json.dumps(public), flush=True)
        await stopped.wait()
    finally:
        if runner is not None:
            try:
                await runner.cleanup()
            except BaseException as error:
                errors.append(error)
        if owned_info:
            try:
                args.info.unlink(missing_ok=True)
            except OSError as error:
                errors.append(error)
        try:
            fixture.cleanup()
        except OSError as error:
            errors.append(error)
        with socket.socket() as probe:
            port_free = port is None or probe.connect_ex(("127.0.0.1", port)) != 0
        record = {
            "checked_utc": datetime.now(UTC).isoformat(),
            "port": port,
            "port_free": port_free,
            "fixture_removed": not root.exists(),
            "private_info_removed": not owned_info or not args.info.exists(),
            "cleanup_errors": len(errors),
        }
        with args.cleanup.open("x") as stream:
            stream.write(json.dumps(record, indent=2) + "\n")
        if errors:
            raise errors[0]
        if not all(record[k] for k in ("port_free", "fixture_removed", "private_info_removed")):
            raise RuntimeError("Owned browser fixture cleanup is incomplete")


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("serve", "change", "inspect"))
    parser.add_argument("--static", type=Path)
    parser.add_argument("--info", type=Path)
    parser.add_argument("--cleanup", type=Path)
    parser.add_argument("--fixture", type=Path)
    for name in ("node", "launcher", "bundle"):
        parser.add_argument("--" + name, type=Path)
        parser.add_argument("--" + name + "-sha256")
    args = parser.parse_args()
    if args.action == "serve":
        if not all((args.static, args.info, args.cleanup)):
            parser.error("serve requires --static, --info and --cleanup")
        await serve(args)
        return
    if args.fixture is None:
        parser.error("change/inspect require --fixture")
    root, info = checked(args.fixture)
    renderer = checker(args)
    documents = DocumentStore(store_path(root), producer="browser-fixture")
    mathchecks.subscribe(documents)  # as the harness and the workspace do
    try:
        if args.action == "change":
            api = ProjectMacros(documents)
            api.change(
                A,
                {
                    "expected": api.get(A)["revision"],
                    "macros": CONCURRENT,
                    "operation_id": "concurrent-" + secrets.token_hex(8),
                },
                USER,
            )
        await drain(documents, renderer)
        print(json.dumps(snapshot(documents, info, checked_math=renderer is not None)), flush=True)
    finally:
        documents.close()


if __name__ == "__main__":
    asyncio.run(main())
