# SPDX-License-Identifier: MIT
"""Owned disposable plan-view fixture. Root runs serve/browser; no research executes.

serve --info /tmp/owned-evidence/private.json --cleanup /tmp/owned-evidence/cleanup.json
in-progress|revise|finish|inspect --fixture /tmp/session-plans-browser-...
Only the exclusive 0600 info file contains a synthetic login URL. Public records
contain hashes and synthetic control-store attribution, never that login value.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import secrets
import shutil
import signal
import socket
import stat
import subprocess
import tempfile
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from aiohttp import web

from sandbox_science.orchestrator.documents import DocumentStore, store_path
from sandbox_science.orchestrator.projects import ProjectStore
from sandbox_science.orchestrator.session_plans import (
    GoalOutcome,
    PlanAuthority,
    PlanBody,
    PlanScope,
    SessionPlans,
)
from sandbox_science.workspace.app import create_app

REPO = Path(__file__).resolve().parents[2]
PREFIX = "session-plans-browser-"
MARKER = ".session-plans-fixture"
VERSION = "synthetic-session-plans-browser-v2"
RECEIPT_FAULT = ".next-plan-save-unavailable"
A, B = "plan-project-a", "plan-project-b"
SCOPES = {"active": (A, "a"), "other": (B, "b"), "closed": (A, "closed")}
STEPS = ("inspect", "qualify", "report")
CHANGED = "Synthetic USER changed this step's intent; no research execution occurred."
SOURCES = (
    "packages/orchestrator/src/sandbox_science/orchestrator/session_plans.py",
    "packages/orchestrator/src/sandbox_science/orchestrator/plan_tools.py",
    "packages/orchestrator/src/sandbox_science/orchestrator/loop.py",
    "packages/orchestrator/src/sandbox_science/orchestrator/cli.py",
    "packages/orchestrator/src/sandbox_science/orchestrator/tools.py",
    "packages/workspace/src/sandbox_science/workspace/plans.py",
    "packages/workspace/src/sandbox_science/workspace/app.py",
    "packages/workspace/src/sandbox_science/workspace/plan_edits.py",
    "packages/workspace/tests/test_plan_edits.py",
    "web/workspace/src/lib/plans.ts",
    "web/workspace/src/lib/plans.test.ts",
    "web/workspace/src/views/PlanPanel.svelte",
    "web/workspace/src/views/SessionView.svelte",
    "web/workspace/src/lib/planEdits.ts",
    "web/workspace/src/lib/planEdits.test.ts",
    "web/workspace/src/views/PlanEditor.svelte",
)


def encoded(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, allow_nan=False, indent=2) + "\n").encode()


def regular(path: Path, *, maximum: int = 32 * 1024 * 1024) -> dict:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_size > maximum:
            raise ValueError("Expected a bounded regular file")
        digest = hashlib.sha256()
        remaining = info.st_size
        while remaining:
            chunk = os.read(descriptor, min(65536, remaining))
            if not chunk:
                raise ValueError("Input shortened during hashing")
            digest.update(chunk)
            remaining -= len(chunk)
        after = os.fstat(descriptor)
        fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
        if os.read(descriptor, 1) or any(getattr(after, k) != getattr(info, k) for k in fields):
            raise ValueError("Input changed during hashing")
        return {"bytes": info.st_size, "sha256": digest.hexdigest()}
    finally:
        os.close(descriptor)


def build_manifest(static: Path) -> dict:
    if static.absolute() != REPO / "web/workspace/dist" or static.is_symlink():
        raise ValueError("Use this frozen worktree's production workspace dist")
    assets = {}
    for directory, dirs, files in os.walk(static, followlinks=False):
        if len(dirs) + len(files) > 256 or any((Path(directory) / d).is_symlink() for d in dirs):
            raise ValueError("Invalid production asset tree")
        for name in sorted(files):
            path = Path(directory) / name
            assets[str(path.relative_to(static))] = regular(path)
            if (
                len(assets) > 256
                or sum(item["bytes"] for item in assets.values()) > 64 * 1024 * 1024
            ):
                raise ValueError("Production asset budget exceeded")
    if "index.html" not in assets:
        raise ValueError("Production workspace index is absent")
    head = subprocess.run(
        ["/usr/bin/git", "rev-parse", "HEAD"],
        cwd=REPO,
        check=True,
        capture_output=True,
        text=True,
        timeout=5,
    ).stdout.strip()
    if re.fullmatch(r"[0-9a-f]{40}", head) is None:
        raise ValueError("Cannot pin actual source HEAD")
    return {
        "source_head": head,
        "helper": regular(Path(__file__)),
        "source_inputs": {name: regular(REPO / name) for name in SOURCES},
        "build_assets": dict(sorted(assets.items())),
    }


def exclusive(path: Path, data: bytes, mode: int) -> tuple[int, int]:
    if (
        not path.is_absolute()
        or path.parent.resolve() != path.parent
        or path.parent.stat().st_uid != os.getuid()
    ):
        raise ValueError("Output needs an owned real parent directory and absolute path")
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
    info = os.fstat(descriptor)
    identity = info.st_dev, info.st_ino
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        remove_owned(path, identity)
        raise
    return identity


def remove_owned(
    path: Path, identity: tuple[int, int] | None, *, missing_ok: bool = True
) -> bool:
    if identity is None:
        return False
    try:
        current = path.lstat()
    except FileNotFoundError:
        return missing_ok
    if not stat.S_ISREG(current.st_mode) or (current.st_dev, current.st_ino) != identity:
        raise ValueError("Private output was replaced; refusing to unlink it")
    try:
        path.unlink()
    except FileNotFoundError:
        return missing_ok
    return True


def read_at(directory: int, name: str, maximum: int) -> dict:
    descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_size > maximum:
            raise ValueError("Invalid fixture metadata")
        raw = os.read(descriptor, maximum + 1)
        if len(raw) != info.st_size or len(raw) > maximum:
            raise ValueError("Invalid fixture metadata length")
        result = json.loads(raw.decode("utf-8"))
        if not isinstance(result, dict):
            raise ValueError("Invalid fixture metadata shape")
        return result
    finally:
        os.close(descriptor)


@contextmanager
def checked(root: Path) -> Iterator[tuple[Path, dict]]:
    root = root.absolute()
    if root.parent != Path(tempfile.gettempdir()).resolve() or not root.name.startswith(PREFIX):
        raise ValueError("Not a disposable session-plan fixture path")
    descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        info = os.fstat(descriptor)
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise ValueError("Fixture directory ownership/mode mismatch")
        marker = read_at(descriptor, MARKER, 1024)
        manifest = read_at(descriptor, "fixture.json", 64 * 1024)
        if (
            marker.get("version") != VERSION
            or marker.get("owner_id") != manifest.get("owner_id")
            or re.fullmatch(r"[0-9a-f]{32}", str(marker.get("owner_id"))) is None
            or manifest.get("fixture") != str(root)
            or manifest.get("scopes") != {key: list(scope) for key, scope in SCOPES.items()}
            or manifest.get("helper") != regular(Path(__file__))
        ):
            raise ValueError("Fixture ownership/identity mismatch")
        anchored = Path(f"/proc/self/fd/{descriptor}")
        for suffix in ("", "-wal", "-shm"):
            path = Path(str(store_path(anchored)) + suffix)
            if suffix and not path.exists() and not path.is_symlink():
                continue
            metadata = path.lstat()
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_uid != os.getuid()
                or metadata.st_size > 32 * 1024 * 1024
            ):
                raise ValueError("Fixture control-store ownership mismatch")
        projects = anchored / "projects"
        project_info = projects.lstat()
        if not stat.S_ISDIR(project_info.st_mode) or project_info.st_uid != os.getuid():
            raise ValueError("Fixture project directory is not real")
        for project in (A, B):
            metadata = (projects / f"{project}.json").lstat()
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid():
                raise ValueError("Fixture project record ownership mismatch")
        yield anchored, manifest
    finally:
        os.close(descriptor)


def authority(key: str, *, user: bool = False) -> PlanAuthority:
    project, session = SCOPES[key]
    return PlanAuthority(
        scope=PlanScope(project_id=project, session_id=session),
        role="user" if user else "main",
        user_id="fixture-user" if user else None,
        run_id=None if user else f"fixture-main-{key}",
        alias=None if user else "synthetic-control-fixture",
    )


def plan_body(key: str) -> PlanBody:
    return PlanBody.model_validate_json(
        json.dumps(
            {
                "goal": f"Synthetic {key} plan-view acceptance; no research execution",
                "feasibility": "Control-store fixture only; no model, worker or experiment",
                "outputs": [
                    {
                        "title": "Synthetic audit",
                        "description": "Inspect retained plan assertions",
                        "evidence_standard": "Canonical control-store revisions and CAS",
                    }
                ],
                "phases": [
                    {
                        "phase_id": "fixture",
                        "title": "Synthetic acceptance",
                        "steps": [
                            {
                                "step_id": step,
                                "title": step.title(),
                                "description": f"Synthetic {step} assertion",
                                "evidence_standard": "Synthetic control-store activity, not research evidence",
                            }
                            for step in STEPS
                        ],
                    }
                ],
            }
        )
    )


def seed(root: Path, pins: dict) -> dict:
    owner = secrets.token_hex(16)
    exclusive(root / MARKER, encoded({"version": VERSION, "owner_id": owner}), 0o600)
    projects = ProjectStore(root)
    for project in (A, B):
        projects.create(project, "minimal", "sha256:" + "a" * 64)
    for project, session in SCOPES.values():
        projects.open_session(project, session, dedicated=False)
    projects.open_session(A, "empty", dedicated=False)
    documents = DocumentStore(store_path(root), producer="synthetic-plan-fixture")
    try:
        plans = SessionPlans(documents)
        refs = {}
        for key in SCOPES:
            actor = authority(key)
            receipt = plans.publish(actor, "fixture-seed", plan_body(key), "Synthetic fixture")
            plans.set_status(
                actor, "fixture-active", receipt.ref, 1, 0, "active", "Synthetic fixture active"
            )
            if key == "active":
                plans.progress(
                    actor,
                    "fixture-provisional",
                    receipt.ref,
                    "qualify",
                    0,
                    "completed",
                    "Synthetic initial assertion before USER changes its intent",
                    expected_status_revision=2,
                )
            refs[key] = receipt.ref.model_dump(mode="json")
    finally:
        documents.close()
    projects.close_session(A, "closed")
    manifest = {
        **pins,
        "fixture": str(root),
        "owner_id": owner,
        "scopes": SCOPES,
        "original_refs": refs,
        "no_plan_scope": [A, "empty"],
        "run_ids": {key: authority(key).run_id for key in SCOPES},
        "activity": "synthetic control-store assertions; no research execution",
        "created_at": datetime.now(UTC).isoformat(),
    }
    exclusive(root / "fixture.json", encoded(manifest), 0o600)
    return manifest


def inspect(plans: SessionPlans, manifest: dict) -> dict:
    result = {}
    for key in SCOPES:
        actor = authority(key)
        current = plans.compact_current(actor)
        if current is None:
            raise ValueError("Synthetic fixture plan is absent")
        if current.head.ref.plan_id != manifest["original_refs"][key]["plan_id"]:
            raise ValueError("Fixed fixture plan identity changed")
        result[key] = {
            "current": current.model_dump(mode="json"),
            "history": {
                kind: [
                    item.model_dump(mode="json")
                    for item in plans.history(actor, current.head.ref.plan_id, kind, limit=16).items
                ]
                for kind in ("version", "progress", "status")
            },
        }
    return result


def action(root: Path, name: str) -> dict:
    with checked(root) as (anchored, manifest):
        documents = DocumentStore(store_path(anchored), producer="synthetic-plan-fixture")
        try:
            plans, actor = SessionPlans(documents), authority("active")
            current = plans.compact_current(actor)
            if current is None:
                raise ValueError("Synthetic active plan absent")
            if current.head.ref.plan_id != manifest["original_refs"]["active"]["plan_id"]:
                raise ValueError("Fixed fixture plan identity changed")
            if name != "inspect" and current.head.status != "active":
                raise ValueError("Fixed mutation requires the active fixture plan")
            if name == "in-progress":
                first = current.steps[0]
                plans.progress(
                    actor,
                    "fixture-in-progress",
                    current.head.ref,
                    STEPS[0],
                    first.latest.sequence if first.latest else 0,
                    "in_progress",
                    "Synthetic control-store start; no research executed",
                    expected_status_revision=current.head.status_revision,
                )
            elif name == "revise":
                if not 1 <= current.head.ref.version <= 8:
                    raise ValueError("Fixed USER revision exceeds its fixture version budget")
                body = current.version.body.model_dump(mode="json")
                body["phases"][0]["steps"][1]["description"] = (
                    CHANGED + f" Competing fixture revision {current.head.ref.version}."
                )
                plans.revise(
                    authority("active", user=True),
                    f"fixture-user-revise-{current.head.ref.version}",
                    current.head.ref,
                    PlanBody.model_validate_json(json.dumps(body)),
                    "Synthetic USER intent change; stable IDs retained",
                )
            elif name == "finish":
                if not 2 <= current.head.ref.version <= 9:
                    raise ValueError("Qualified finish requires a bounded USER revision")
                for step, status_ in zip(STEPS, ("completed", "blocked", "skipped"), strict=True):
                    fresh = plans.compact_current(actor)
                    projection = next(item for item in fresh.steps if item.step_id == step)
                    plans.progress(
                        actor,
                        f"fixture-finish-{step}",
                        fresh.head.ref,
                        step,
                        projection.latest.sequence if projection.latest else 0,
                        status_,
                        f"Synthetic {status_} assertion; research remains unperformed",
                        expected_status_revision=fresh.head.status_revision,
                    )
                summary = plans.summary(actor)
                plans.set_status(
                    actor,
                    "fixture-qualified-finish",
                    summary.head,
                    summary.status_revision,
                    summary.progress_cursor,
                    "completed",
                    "Terminal synthetic accounting, not research success",
                    outcome=GoalOutcome(
                        status="partially_achieved",
                        summary="Only synthetic control-store acceptance was exercised",
                        evidence=(),
                        remaining_work=("Perform and verify actual research separately",),
                    ),
                )
            elif name == "lose-save-response":
                descriptor = os.open(
                    anchored / RECEIPT_FAULT,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                    0o600,
                )
                with os.fdopen(descriptor, "wb") as stream:
                    stream.write(encoded({"owner_id": manifest["owner_id"]}))
                    stream.flush()
                    os.fsync(stream.fileno())
            elif name != "inspect":
                raise ValueError("Unsupported fixed fixture action")
            return {
                "owner_id": manifest["owner_id"],
                "action": name,
                "activity": manifest["activity"],
                "plans": inspect(plans, manifest),
            }
        finally:
            documents.close()


def withhold_saved_receipt(
    root: Path, owner: str
) -> Callable[
    [web.Request, Callable[[web.Request], Awaitable[web.StreamResponse]]],
    Awaitable[web.StreamResponse],
]:
    """One owned fixture fault: hide a successful save's response after commit."""

    @web.middleware
    async def middleware(
        request: web.Request, handler: Callable[[web.Request], Awaitable[web.StreamResponse]]
    ) -> web.StreamResponse:
        if (
            request.method != "POST"
            or request.path != f"/api/v0/projects/{A}/sessions/a/plan/changes"
        ):
            return await handler(request)
        path = root / RECEIPT_FAULT
        try:
            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except FileNotFoundError:
            return await handler(request)
        try:
            before = os.fstat(descriptor)
            if (
                not stat.S_ISREG(before.st_mode)
                or before.st_uid != os.getuid()
                or before.st_nlink != 1
                or stat.S_IMODE(before.st_mode) != 0o600
                or not 0 < before.st_size <= 1024
            ):
                raise ValueError("Invalid owned receipt-fault marker")
            raw = os.read(descriptor, 1025)
            if len(raw) != before.st_size or json.loads(raw) != {"owner_id": owner}:
                raise ValueError("Receipt-fault owner mismatch")
            response = await handler(request)
            if response.status != 200:
                return response
            if not remove_owned(path, (before.st_dev, before.st_ino), missing_ok=False):
                return response
            return web.json_response(
                {"error": {"kind": "unavailable", "message": "Synthetic saved receipt withheld"}},
                status=503,
                headers={"Cache-Control": "no-store"},
            )
        finally:
            os.close(descriptor)

    return middleware


async def serve(args: argparse.Namespace) -> None:
    if args.info == args.cleanup or args.info.exists() or args.cleanup.exists():
        raise ValueError("Private info/public cleanup must be distinct and absent")
    pins = build_manifest(args.static)
    stopped = asyncio.Event()
    for signum in (signal.SIGINT, signal.SIGTERM):
        asyncio.get_running_loop().add_signal_handler(signum, stopped.set)
    root = Path(tempfile.mkdtemp(prefix=PREFIX))
    original = root.lstat()
    runner, port, private_id = None, None, None
    cleanup_errors = []
    runners_closed = 0
    try:
        os.chmod(root, 0o700)
        manifest = seed(root, pins)
        token = secrets.token_urlsafe(32)
        app = create_app(ProjectStore(root), args.static, login_token=token)
        app.middlewares.append(withhold_saved_receipt(root, manifest["owner_id"]))
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        await web.TCPSite(runner, "127.0.0.1", args.port).start()
        port = int(runner.addresses[0][1])
        private_id = exclusive(
            args.info,
            encoded(
                {
                    "fixture": str(root),
                    "owner_id": manifest["owner_id"],
                    "login_url": f"http://127.0.0.1:{port}/login?token={token}",
                }
            ),
            0o600,
        )
        print(json.dumps({**manifest, "port": port}), flush=True)
        await stopped.wait()
    finally:
        if runner is not None:
            results = await asyncio.gather(runner.cleanup(), return_exceptions=True)
            cleanup_errors.extend(
                type(item).__name__ for item in results if isinstance(item, BaseException)
            )
            runners_closed = int(not any(isinstance(item, BaseException) for item in results))
        try:
            removed_info = remove_owned(args.info, private_id)
        except (OSError, ValueError):
            removed_info = False
            cleanup_errors.append("private_cleanup_failed")
        try:
            current = root.lstat()
            if not stat.S_ISDIR(current.st_mode) or (current.st_dev, current.st_ino) != (
                original.st_dev,
                original.st_ino,
            ):
                raise ValueError("Fixture path replaced; refusing removal")
            shutil.rmtree(root)
        except FileNotFoundError:
            pass
        except (OSError, ValueError):
            cleanup_errors.append("fixture_cleanup_failed")
        port_free = True
        if port is not None:
            with socket.socket() as probe:
                probe.settimeout(1)
                port_free = probe.connect_ex(("127.0.0.1", port)) != 0
        exclusive(
            args.cleanup,
            encoded(
                {
                    "checked_utc": datetime.now(UTC).isoformat(),
                    "fixture_removed": not root.exists(),
                    "private_info_removed": removed_info,
                    "port": port,
                    "port_free": port_free,
                    "runners_closed": runners_closed,
                    "cleanup_errors": cleanup_errors,
                    "activity": "synthetic control store only",
                }
            ),
            0o644,
        )
        if cleanup_errors or not port_free:
            raise RuntimeError("Disposable fixture cleanup did not verify")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="action", required=True)
    server = commands.add_parser("serve")
    server.add_argument("--static", type=Path, default=REPO / "web/workspace/dist")
    server.add_argument("--info", type=Path, required=True)
    server.add_argument("--cleanup", type=Path, required=True)
    server.add_argument("--port", type=int, default=0, choices=range(65536))
    for name in ("in-progress", "revise", "finish", "inspect", "lose-save-response"):
        commands.add_parser(name).add_argument("--fixture", type=Path, required=True)
    args = parser.parse_args()
    if args.action == "serve":
        asyncio.run(serve(args))
    else:
        print(json.dumps(action(args.fixture, args.action), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
