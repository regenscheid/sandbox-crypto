# SPDX-License-Identifier: MIT
# ruff: noqa: S101, S607, S608
"""Root-owned Lab30 CP26 acceptance. No operator/service/gateway lifecycle authority.

Adopt unchanged into .agents/labs before use. Root supplies --head for its exact
signed clean integrated source, registered normal/escalation policies, production
assets, services, decisions, idle configuration and sanitized gateway evidence.
The author never executes this main or real HTTP. Private login/config/token bytes
are local only, never hashed or exported. Historical/synthetic cases are not implied.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import hashlib
import json
import os
import re
import signal
import sqlite3
import stat
import subprocess
import tomllib
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, TypeVar
from urllib.parse import urlencode, urlsplit

import aiohttp
from aiohttp import web
from pydantic import BaseModel

from sandbox_science.contracts.provision_observations import (
    Observation,
    ProvisionDetail,
    ProvisionFilePage,
    ProvisionObservation,
    RevisionProbePage,
    RevisionSummaryPage,
)
from sandbox_science.orchestrator.projects import ProjectRecord, ProjectStore
from sandbox_science.orchestrator.worker_client import WorkerClient
from sandbox_science.workspace.app import create_app

SOURCE = Path(__file__).resolve().parents[2]  # Only after reviewed root adoption.
LAB = Path.home() / ".local/state/openshell-lab"
CONTROL = LAB / "orch/provisioning-workspace"
PROJECT = "lab30"
OTHER = "lab30b"
WORKER = "http://127.0.0.1:8100"
MARKER = "provisioning-workspace.created"
PRIVATE = "provisioning-workspace-private.json"
PURPOSE = "disposable CP26 provisioning workspace acceptance"
SESSIONS = ("shared-a", "shared-b", "dedicated")
STAGES = (
    "init",
    "serve",
    "initial",
    "install",
    "permission-wait",
    "permission-finish",
    "idle-inspect",
    "inspect",
)
CODE = (
    "packages/contracts/src/sandbox_science/contracts/provision_observations.py",
    "packages/contracts/src/sandbox_science/contracts/provisioning.py",
    "packages/worker/src/sandbox_science/worker/provision_observations.py",
    "packages/worker/src/sandbox_science/worker/provisioner.py",
    "packages/worker/src/sandbox_science/worker/capabilities.toml",
    "packages/worker/src/sandbox_science/worker/store.py",
    "packages/worker/src/sandbox_science/worker/app.py",
    "packages/orchestrator/src/sandbox_science/orchestrator/projects.py",
    "packages/orchestrator/src/sandbox_science/orchestrator/worker_client.py",
    "packages/workspace/src/sandbox_science/workspace/app.py",
    "packages/workspace/src/sandbox_science/workspace/provisioning.py",
    "packages/workspace/src/sandbox_science/workspace/provisioning_client.py",
    "web/workspace/src/lib/provisioning.ts",
    "web/workspace/src/views/EnvironmentPanel.svelte",
    "web/workspace/src/views/ProjectView.svelte",
    "web/workspace/package-lock.json",
)
Model = TypeVar("Model", bound=Observation)


def utc() -> str:
    return datetime.now(UTC).isoformat()


def identity(info: os.stat_result) -> tuple[int, int]:
    return info.st_dev, info.st_ino


def directory(relative: str = "") -> int:
    held = os.open(LAB, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        assert os.fstat(held).st_uid == os.getuid()
        for name in relative.split("/") if relative else ():
            assert name not in {"", ".", ".."}
            assert re.fullmatch("[a-zA-Z0-9._-]+", name)
            child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=held)
            os.close(held)
            held = child
            assert os.fstat(held).st_uid == os.getuid()
        return held
    except BaseException:
        os.close(held)
        raise


def read_fd(fd: int, limit: int) -> bytes:
    before = os.fstat(fd)
    assert stat.S_ISREG(before.st_mode)
    assert before.st_uid == os.getuid()
    assert before.st_nlink == 1
    assert 0 <= before.st_size <= limit
    raw = os.pread(fd, limit + 1, 0)
    after = os.fstat(fd)
    assert len(raw) == before.st_size
    assert len(raw) <= limit
    assert (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) == (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
    )
    return raw


def bounded(relative: str, limit: int = 128 * 1024, *, private: bool = False) -> bytes:
    parent, _, name = relative.rpartition("/")
    assert name not in {"", ".", ".."}
    assert "/" not in name
    root = directory(parent)
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=root)
        try:
            if private:
                assert stat.S_IMODE(os.fstat(fd).st_mode) == 0o600
            return read_fd(fd, limit)
        finally:
            os.close(fd)
    finally:
        os.close(root)


def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        assert key not in result, "duplicate JSON key"
        result[key] = value
    return result


def decode(raw: bytes, limit: int = 128 * 1024) -> Any:
    assert len(raw) <= limit
    return json.loads(
        raw.decode("utf-8"),
        object_pairs_hook=unique,
        parse_constant=lambda _: (_ for _ in ()).throw(ValueError("nonfinite JSON")),
    )


def error_projection(value: Any) -> dict[str, str | None]:
    error = value.get("error") if type(value) is dict else None
    kind = error.get("kind") if type(error) is dict else None
    allowed = {
        "invalid_request",
        "unsupported",
        "unavailable",
        "policy_denied",
        "not_found",
        "conflict",
        "timed_out",
        "cancelled",
        "kernel_died",
        "exception",
        "internal",
    }
    return {"error_kind": kind if type(kind) is str and kind in allowed else None}


def write_new(
    relative: str,
    value: object,
    cap: int = 128 * 1024,
    *,
    ownership: list[tuple[int, int]] | None = None,
) -> tuple[int, int]:
    raw = (json.dumps(value, ensure_ascii=True, sort_keys=True, indent=2) + "\n").encode("ascii")
    assert len(raw) <= cap
    parent, _, name = relative.rpartition("/")
    root = directory(parent)
    try:
        assert ownership is None or not ownership
        fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=root)
        try:
            owned = identity(os.fstat(fd))
            if ownership is not None:
                ownership.append(owned)  # Capture before wrapping/writes/fsync can fail.
            stream = os.fdopen(fd, "wb")
            fd = -1  # The stream owns it only after successful construction.
            with stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            os.fsync(root)
            return owned
        finally:
            if fd >= 0:
                os.close(fd)
    finally:
        os.close(root)


def remove_private(owned: tuple[int, int]) -> bool:
    root = directory()
    try:
        try:
            info = os.stat(PRIVATE, dir_fd=root, follow_symlinks=False)
        except FileNotFoundError:
            return False
        assert identity(info) == owned
        assert stat.S_ISREG(info.st_mode)
        assert info.st_uid == os.getuid()
        assert info.st_nlink == 1
        os.unlink(PRIVATE, dir_fd=root)
        os.fsync(root)
        return True
    finally:
        os.close(root)


def source_pins(head: str) -> dict[str, Any]:
    assert re.fullmatch(r"[0-9a-f]{40}", head)
    assert (
        subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=SOURCE, text=True).strip() == head
    )
    assert (
        subprocess.check_output(["git", "log", "-1", "--format=%G?"], cwd=SOURCE, text=True).strip()
        == "G"
    )
    assert not subprocess.check_output(["git", "status", "--porcelain"], cwd=SOURCE)
    values = {}
    for name in CODE:
        fd = os.open(SOURCE / name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            raw = read_fd(fd, 2 * 1024 * 1024)
        finally:
            os.close(fd)
        values[name] = {"size": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}
    return values


def assets() -> dict[str, Any]:
    root = SOURCE / "web/workspace/dist"
    assert root.is_dir()
    assert not root.is_symlink()
    values: dict[str, Any] = {}
    total = 0
    for parent, dirs, names in os.walk(root, followlinks=False):
        assert len(values) + len(dirs) + len(names) <= 256
        assert all(not (Path(parent) / name).is_symlink() for name in dirs)
        for name in names:
            path = Path(parent) / name
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            try:
                raw = read_fd(fd, 4 * 1024 * 1024)
            finally:
                os.close(fd)
            total += len(raw)
            assert total <= 32 * 1024 * 1024
            values[path.relative_to(root).as_posix()] = {
                "size": len(raw),
                "sha256": hashlib.sha256(raw).hexdigest(),
            }
    assert "index.html" in values
    assert 1 <= len(values) <= 256
    return dict(sorted(values.items()))


def settings_guard() -> None:
    value = tomllib.loads(bounded("worker/worker.toml", 65536).decode("utf-8"))
    assert value["listen"] == "127.0.0.1:8100"
    assert value["openshell"].get("daemon", True) is True
    assert {tuple(c["projects"]) for c in value["clients"]} == {(PROJECT,), (OTHER,)}
    assert value["openshell"]["environments"]["profiles"]["minimal"].startswith(
        "sandbox-science/minimal:"
    )
    # No settings object, token bytes or settings/config hash escapes this function.


def record(project: str = PROJECT) -> ProjectRecord:
    assert project in (PROJECT, OTHER)
    return ProjectRecord.model_validate_json(
        bounded(f"orch/provisioning-workspace/projects/{project}.json", 1024 * 1024),
        strict=True,
    )


def registry_check(value: dict[str, Any]) -> None:
    project, other = record(), record(OTHER)
    assert project.project_id == PROJECT
    assert other.project_id == OTHER
    assert project.profile == other.profile == "minimal"
    assert project.policy == other.policy == value["policy"]
    assert set(project.sessions) == set(SESSIONS)
    assert set(other.sessions) == {"unserved"}
    assert project.default_environment_id == value["environments"]["shared"]
    assert project.sessions["shared-a"].environment_id == project.default_environment_id
    assert project.sessions["shared-b"].environment_id == project.default_environment_id
    dedicated = project.sessions["dedicated"]
    assert dedicated.environment_id == value["environments"]["dedicated"]
    assert dedicated.dedicated
    assert not project.sessions["shared-a"].dedicated
    assert not project.sessions["shared-b"].dedicated
    assert all(s.closed_at is None for s in project.sessions.values())
    assert other.default_environment_id not in value["environments"].values()
    assert other.default_environment_id == value["other_environment"]


def guard(head: str) -> dict[str, Any]:
    pins = source_pins(head)
    settings_guard()
    value = decode(bounded(MARKER, 32 * 1024))
    assert value["version"] == 1
    assert type(value["version"]) is int
    assert value["project"] == PROJECT
    assert value["purpose"] == PURPOSE
    assert value["source_head"] == head
    assert value["inputs"] == pins
    assert re.fullmatch(r"[0-9a-f]{32}", value["run_id"])
    assert value["helper_sha256"] == hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    for field, name in (
        ("policy", "policy.digest"),
        ("escalation_policy", "lab30-escalate-policy.digest"),
    ):
        assert value[field] == bounded(name, 128).decode("ascii").strip()
        assert re.fullmatch(r"sha256:[0-9a-f]{64}", value[field])
    assert value["policy"] != value["escalation_policy"]
    registry_check(value)
    held = directory("orch/provisioning-workspace")
    try:
        assert stat.S_IMODE(os.fstat(held).st_mode) == 0o700
    finally:
        os.close(held)
    return value


def init(head: str) -> None:
    pins = source_pins(head)
    settings_guard()
    policies = [
        bounded(name, 128).decode("ascii").strip()
        for name in (
            "policy.digest",
            "lab30-escalate-policy.digest",
        )
    ]
    assert all(re.fullmatch(r"sha256:[0-9a-f]{64}", p) for p in policies)
    assert policies[0] != policies[1]
    root = directory()
    try:
        for name in ("orch", "evidence"):
            with contextlib.suppress(FileExistsError):
                os.mkdir(name, 0o700, dir_fd=root)
    finally:
        os.close(root)
    root = directory("orch")
    try:
        os.mkdir("provisioning-workspace", 0o700, dir_fd=root)
    finally:
        os.close(root)
    store = ProjectStore(CONTROL)
    project, created = store.create(PROJECT, "minimal", policies[0])
    assert created
    for session in SESSIONS:
        store.open_session(PROJECT, session, dedicated=session == "dedicated")
    other, created = store.create(OTHER, "minimal", policies[0])
    assert created
    store.open_session(OTHER, "unserved", dedicated=False)
    value = {
        "version": 1,
        "project": PROJECT,
        "purpose": PURPOSE,
        "source_head": head,
        "inputs": pins,
        "run_id": uuid.uuid4().hex,
        "helper_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "policy": policies[0],
        "escalation_policy": policies[1],
        "environments": {
            "shared": project.default_environment_id,
            "dedicated": record().sessions["dedicated"].environment_id,
        },
        "other_environment": other.default_environment_id,
    }
    write_new(MARKER, value, 32 * 1024)
    guard(head)
    save(
        value,
        "init",
        {"registry": [record().model_dump(mode="json"), record(OTHER).model_dump(mode="json")]},
    )


def save(marker: dict[str, Any], name: str, payload: object) -> None:
    assert re.fullmatch(r"[a-z][a-z0-9-]{0,47}", name)
    write_new(
        "evidence/provisioning-workspace-" + name + ".json",
        {
            "source_head": marker["source_head"],
            "run_id": marker["run_id"],
            "project_id": PROJECT,
            "observed_at": utc(),
            "payload": payload,
        },
        2 * 1024 * 1024,
    )


def evidence_row(marker: dict[str, Any], name: str) -> dict[str, Any]:
    assert re.fullmatch(r"[a-z][a-z0-9-]{0,47}", name)
    value = decode(
        bounded("evidence/provisioning-workspace-" + name + ".json", 2 * 1024 * 1024),
        2 * 1024 * 1024,
    )
    assert value["source_head"] == marker["source_head"]
    assert value["run_id"] == marker["run_id"]
    assert value["project_id"] == PROJECT
    when = datetime.fromisoformat(value["observed_at"])
    assert when.tzinfo is not None
    assert when.utcoffset() == timedelta(0)
    return value


def evidence(marker: dict[str, Any], name: str) -> Any:
    return evidence_row(marker, name)["payload"]


async def serve(head: str) -> None:
    marker = guard(head)
    build = assets()
    token = uuid.uuid4().hex + uuid.uuid4().hex
    worker = WorkerClient(
        WORKER, bounded("worker/client-a.token", 4096, private=True).decode().strip()
    )
    app = create_app(
        ProjectStore(CONTROL), SOURCE / "web/workspace/dist", login_token=token, job_worker=worker
    )
    runner = web.AppRunner(app, access_log=None)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    owned: list[tuple[int, int]] = []
    port = None
    errors: list[str] = []
    try:
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        assert len(runner.addresses) == 1
        port = runner.addresses[0][1]
        assert type(port) is int
        assert 1024 < port < 65536
        write_new(
            PRIVATE,
            {
                "source_head": head,
                "run_id": marker["run_id"],
                "login_url": f"http://127.0.0.1:{port}/login?token={token}",
                "port": port,
            },
            4096,
            ownership=owned,
        )
        save(
            marker,
            "server",
            {
                "port": port,
                "pid": os.getpid(),
                "assets": build,
                "inputs": marker["inputs"],
                "helper_sha256": marker["helper_sha256"],
            },
        )
        print(
            json.dumps(
                {
                    "stage": "serve",
                    "port": port,
                    "pid": os.getpid(),
                    "source_head": head,
                    "run_id": marker["run_id"],
                }
            ),
            flush=True,
        )
        await stop.wait()
    finally:
        for close in (runner.cleanup, worker.close):
            try:
                await close()
            except BaseException as error:
                errors.append(type(error).__name__)
        removed = False
        if owned:
            try:
                removed = remove_private(owned[0])
            except BaseException as error:
                errors.append(type(error).__name__)
        save(
            marker,
            "workspace-cleanup",
            {
                "utc": utc(),
                "port": port,
                "runner_cleanup_attempted": True,
                "private_removed": removed,
                "private_created_by_this_process": bool(owned),
                "errors": errors,
            },
        )
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.remove_signal_handler(sig)
        assert not errors, "workspace cleanup did not complete"


def same_observation(left: BaseModel, right: BaseModel) -> None:
    assert type(left) is type(right)
    assert left.model_dump(mode="json", exclude={"observed_at"}) == right.model_dump(
        mode="json", exclude={"observed_at"}
    )


class Checks:
    def __init__(
        self,
        head: str,
        marker: dict[str, Any],
        worker: aiohttp.ClientSession,
        gui: aiohttp.ClientSession,
        base: str,
    ) -> None:
        self.head, self.marker, self.worker, self.gui, self.base = head, marker, worker, gui, base

    def context(self, session: str) -> dict[str, str]:
        assert session in SESSIONS
        found = record().sessions[session]
        return {
            "project_id": PROJECT,
            "session_id": session,
            "environment_id": found.environment_id,
        }

    async def call(
        self,
        which: str,
        method: str,
        path: str,
        *,
        body: object = None,
        expected: tuple[int, ...] = (200,),
        cap: int = 64 * 1024,
    ) -> tuple[int, Any]:
        guard(self.head)
        assert which in ("worker", "gui")
        assert method in ("GET", "POST")
        assert path.startswith("/v1/") if which == "worker" else path.startswith("/api/v0/")
        assert not any(c in path for c in ("#", "\\", "\x00"))
        http, base = (self.worker, WORKER) if which == "worker" else (self.gui, self.base)
        budget = (
            180 if which == "worker" and method == "POST" and path == "/v1/environments" else 12
        )
        async with asyncio.timeout(budget):
            async with http.request(
                method, base + path, json=body, allow_redirects=False, auto_decompress=False
            ) as response:
                raw = bytearray()
                count = 0
                async for chunk in response.content.iter_chunked(8192):
                    count += 1
                    assert count <= 256
                    assert len(raw) + len(chunk) <= cap
                    raw.extend(chunk)
                status = response.status
                assert status in expected, (which, path.split("?")[0], status)
                if which == "gui":
                    assert response.headers.get("Cache-Control") == "no-store"
                value = decode(bytes(raw), cap) if raw else None
                # Never export raw error messages. A status+kind is sufficient.
                if status >= 400:
                    value = error_projection(value)
                return status, value

    def paths(
        self, environment: str, suffix: str = "", query: dict[str, object] | None = None
    ) -> tuple[str, str]:
        assert environment in self.marker["environments"].values()
        worker = f"/v1/environments/{environment}/provisioning/observations" + suffix
        gui = (
            f"/api/v0/projects/{PROJECT}/environments/{environment}/provisioning/observations"
            + suffix
        )
        wquery = {"project_id": PROJECT, **(query or {})}
        return worker + "?" + urlencode(wquery), gui + ("?" + urlencode(query) if query else "")

    async def pair(
        self,
        model: type[Model],
        environment: str,
        suffix: str = "",
        query: dict[str, object] | None = None,
    ) -> Model:
        paths = self.paths(environment, suffix, query)
        values = []
        for which, path in zip(("worker", "gui"), paths, strict=True):
            raw = (await self.call(which, "GET", path))[1]
            typed = model.model_validate_json(json.dumps(raw), strict=True)
            assert typed.project_id == PROJECT
            assert typed.environment_id == environment
            values.append(typed)
        same_observation(values[0], values[1])
        return values[0]

    async def detail(
        self, environment: str, provision: str, seq: int | None = None
    ) -> ProvisionDetail:
        assert re.fullmatch(r"prv-[A-Za-z0-9_-]{1,120}", provision)
        result = await self.pair(
            ProvisionDetail,
            environment,
            "/operations/" + provision,
            None if seq is None else {"seq": seq},
        )
        assert result.summary.provision_id == provision
        assert seq is None or result.summary.seq == seq
        return result

    async def submit(self, name: str, session: str, capability: str) -> str:
        assert (name, session, capability) in (
            ("install", "shared-a", "gp"),
            ("permission", "dedicated", "python-flint"),
            ("sibling", "dedicated", "gp"),
        )
        operation = "lab30-" + name + "-" + self.marker["run_id"]
        _, value = await self.call(
            "worker",
            "POST",
            "/v1/provisioning",
            expected=(201,),
            cap=128 * 1024,
            body={
                "operation_id": operation,
                "context": self.context(session),
                "purpose": PURPOSE + ": " + name,
                "capability": capability,
            },
        )
        assert value["request"]["operation_id"] == operation
        assert value["request"]["context"]["project_id"] == PROJECT
        provision = value["provision_id"]
        assert type(provision) is str
        assert re.fullmatch("prv-[A-Za-z0-9_-]{1,120}", provision)
        save(
            self.marker,
            name + "-submitted",
            {
                "provision_id": provision,
                "operation_id": operation,
                "context": self.context(session),
                "capability": capability,
            },
        )
        return provision

    async def wait_detail(
        self, environment: str, provision: str, *, permission: bool = False
    ) -> ProvisionDetail:
        # Poll one worker-only bounded detail to avoid false parity during a live transition.
        path, _ = self.paths(environment, "/operations/" + provision)
        async with asyncio.timeout(900):
            while True:
                _, value = await self.call("worker", "GET", path)
                found = ProvisionDetail.model_validate_json(json.dumps(value), strict=True)
                assert (found.project_id, found.environment_id, found.summary.provision_id) == (
                    PROJECT,
                    environment,
                    provision,
                )
                if found.summary.state == "finished" or (
                    permission and found.summary.state == "awaiting_permission"
                ):
                    return found
                await asyncio.sleep(0.25)

    async def files(self, environment: str, detail: ProvisionDetail) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        offset = 0
        for _ in range(250):
            page = await self.pair(
                ProvisionFilePage,
                environment,
                "/operations/" + detail.summary.provision_id + "/files",
                {"seq": detail.summary.seq, "offset": offset, "limit": 8},
            )
            assert (page.provision_id, page.seq, page.offset, page.limit, page.total) == (
                detail.summary.provision_id,
                detail.summary.seq,
                offset,
                8,
                detail.summary.files_total,
            )
            rows.extend(f.model_dump(mode="json") for f in page.files)
            if page.next_offset is None:
                assert len(rows) == page.total
                return rows
            offset = page.next_offset
        raise AssertionError("bounded file page count exhausted")

    async def history(self, environment: str) -> dict[str, Any]:
        rows, probes, after, target = [], [], -1, None
        for _ in range(64):
            query: dict[str, object] = {"after": after, "limit": 8}
            if target is not None:
                query["target"] = target
            page = await self.pair(RevisionSummaryPage, environment, "/revisions", query)
            assert page.after == after
            assert page.limit == 8
            if target is None:
                target = page.target
            assert page.target == target
            for revision in page.revisions:
                rows.append(revision.model_dump(mode="json"))
                offset, found = 0, []
                for _ in range(8):
                    selected = await self.pair(
                        RevisionProbePage,
                        environment,
                        f"/revisions/{revision.revision}/probes",
                        {"pin": revision.record_sha256, "offset": offset, "limit": 8},
                    )
                    assert (
                        selected.revision,
                        selected.record_sha256,
                        selected.offset,
                        selected.limit,
                        selected.total,
                    ) == (
                        revision.revision,
                        revision.record_sha256,
                        offset,
                        8,
                        revision.probes_total,
                    )
                    found.extend(p.model_dump(mode="json") for p in selected.probes)
                    if selected.next_offset is None:
                        break
                    offset = selected.next_offset
                assert len(found) == revision.probes_total
                probes.append(
                    {
                        "revision": revision.revision,
                        "record_sha256": revision.record_sha256,
                        "probes": found,
                    }
                )
            if page.next_after is None:
                assert (
                    len(rows) <= page.retained_total
                )  # Older-than-window absence remains explicit.
                return {
                    "target": target,
                    "first_retained": page.first_retained,
                    "retained_total": page.retained_total,
                    "revisions": rows,
                    "probes": probes,
                }
            after = page.next_after
        raise AssertionError("bounded history page count exhausted")

    async def initial(self) -> None:
        for kind, policy in (
            ("shared", self.marker["policy"]),
            ("dedicated", self.marker["escalation_policy"]),
        ):
            await self.call(
                "worker",
                "POST",
                "/v1/environments",
                expected=(201,),
                body={
                    "project_id": PROJECT,
                    "environment_id": self.marker["environments"][kind],
                    "profile": "minimal",
                    "policy": policy,
                },
            )
        _, choices = await self.call(
            "gui", "GET", f"/api/v0/projects/{PROJECT}/provisioning/environments"
        )
        assert choices["project_id"] == PROJECT
        assert set(choices["environment_ids"]) == set(self.marker["environments"].values())
        observations = {}
        for kind, environment in self.marker["environments"].items():
            value = await self.pair(ProvisionObservation, environment)
            assert value.queue_total == 0
            assert value.gate.mode == "ready"
            observations[kind] = value.model_dump(mode="json")
        foreign = self.marker["other_environment"]
        status, _ = await self.call(
            "gui",
            "GET",
            f"/api/v0/projects/{PROJECT}/environments/{foreign}/provisioning/observations",
            expected=(404,),
        )
        other_status, _ = await self.call(
            "gui",
            "GET",
            f"/api/v0/projects/{OTHER}/environments/{foreign}/provisioning/observations",
            expected=(404,),
        )
        save(
            self.marker,
            "initial",
            {
                "choices": choices,
                "observations": observations,
                "foreign_environment_status": status,
                "unserved_project_status": other_status,
                "sql": sql_snapshot(self.marker),
            },
        )

    async def install(self) -> None:
        before = evidence(self.marker, "initial")["observations"]["shared"]["current_revision"]
        environment = self.marker["environments"]["shared"]
        provision = await self.submit("install", "shared-a", "gp")
        done = await self.wait_detail(environment, provision)
        assert done.summary.outcome == "completed"
        assert done.summary.revision_after is not None
        assert done.summary.revision_before is not None
        assert done.summary.revision_after > done.summary.revision_before
        assert before is None or done.summary.revision_after > before
        done = await self.detail(environment, provision, done.summary.seq)
        files = await self.files(environment, done)
        history = await self.history(environment)
        selected = next(
            r for r in history["revisions"] if r["revision"] == done.summary.revision_after
        )
        assert selected["operation"] == provision
        proof = next(p for p in history["probes"] if p["revision"] == selected["revision"])
        assert any(p["probe"] == "gp-factor" and p["status"] == "passed" for p in proof["probes"])
        stale = max(0, done.summary.seq - 1)
        assert stale != done.summary.seq
        statuses = []
        for which, path in zip(
            ("worker", "gui"),
            self.paths(environment, "/operations/" + provision, {"seq": stale}),
            strict=True,
        ):
            statuses.append((await self.call(which, "GET", path, expected=(409,)))[0])
        for which, path in zip(
            ("worker", "gui"),
            self.paths(
                environment,
                "/operations/" + provision + "/files",
                {"seq": stale, "offset": 0, "limit": 8},
            ),
            strict=True,
        ):
            statuses.append((await self.call(which, "GET", path, expected=(409,)))[0])
        other = self.marker["environments"]["dedicated"]
        foreign_statuses = []
        for which, path in zip(
            ("worker", "gui"), self.paths(other, "/operations/" + provision), strict=True
        ):
            foreign_statuses.append((await self.call(which, "GET", path, expected=(404,)))[0])
        retained_sql = sql_snapshot(self.marker)
        assert any(
            row["environment_id"] == environment
            and row["record_sha256"] == selected["record_sha256"]
            for row in retained_sql["revision_entries"]
        )
        pin = selected["record_sha256"]
        wrong = ("0" if pin[0] != "0" else "1") + pin[1:]
        for which, path in zip(
            ("worker", "gui"),
            self.paths(
                environment,
                f"/revisions/{selected['revision']}/probes",
                {"pin": wrong, "offset": 0, "limit": 8},
            ),
            strict=True,
        ):
            statuses.append((await self.call(which, "GET", path, expected=(409,)))[0])
        current = await self.detail(
            environment, provision
        )  # Explicit current retained-history selection.
        assert current.summary.seq == done.summary.seq
        save(
            self.marker,
            "install",
            {
                "detail": done.model_dump(mode="json"),
                "files": files,
                "history": history,
                "selected_current_detail": current.model_dump(mode="json"),
                "stale_pin_statuses": statuses,
                "foreign_operation_statuses": foreign_statuses,
                "sql": retained_sql,
            },
        )

    async def permission_wait(self) -> None:
        evidence(self.marker, "install")
        environment = self.marker["environments"]["dedicated"]
        provision = await self.submit("permission", "dedicated", "python-flint")
        pending = await self.wait_detail(environment, provision, permission=True)
        assert pending.summary.state == "awaiting_permission"
        assert pending.summary.permission_request is not None
        sibling = await self.submit("sibling", "dedicated", "gp")
        queue = await self.pair(ProvisionObservation, environment)
        assert queue.queue_total == 2
        assert [p.provision_id for p in queue.operations] == [provision, sibling]
        assert queue.operations[0].state == "awaiting_permission"
        assert queue.operations[1].state == "queued"
        pending = await self.detail(environment, provision, pending.summary.seq)
        files = await self.files(environment, pending)
        assert any(f["action"] == "escalate" for f in files)
        save(
            self.marker,
            "permission-wait",
            {
                "detail": pending.model_dump(mode="json"),
                "files": files,
                "queue": queue.model_dump(mode="json"),
                "sibling": sibling,
                "operator_decision_required": True,
                "sql": sql_snapshot(self.marker),
            },
        )
        print(
            json.dumps(
                {
                    "stage": "permission-wait",
                    "permission_request": pending.summary.permission_request,
                    "provision_id": provision,
                    "queued_sibling": sibling,
                }
            ),
            flush=True,
        )

    async def permission_finish(self) -> None:
        pending = evidence(self.marker, "permission-wait")
        environment = self.marker["environments"]["dedicated"]
        first = pending["detail"]["summary"]["provision_id"]
        done = await self.wait_detail(environment, first, permission=True)
        if done.summary.state == "awaiting_permission":
            print(
                json.dumps(
                    {
                        "stage": "permission-finish",
                        "waiting": True,
                        "permission_request": done.summary.permission_request,
                    }
                ),
                flush=True,
            )
            return
        assert done.summary.outcome == "completed"
        sibling = await self.wait_detail(environment, pending["sibling"], permission=True)
        if sibling.summary.state == "awaiting_permission":
            # Explicitly record a second root decision; never approve it here.
            save(self.marker, "sibling-permission", {"detail": sibling.model_dump(mode="json")})
            print(
                json.dumps(
                    {
                        "stage": "permission-finish",
                        "waiting": True,
                        "permission_request": sibling.summary.permission_request,
                    }
                ),
                flush=True,
            )
            return
        assert sibling.summary.outcome in ("completed", "already_satisfied")
        done = await self.detail(environment, first, done.summary.seq)
        sibling = await self.detail(environment, pending["sibling"], sibling.summary.seq)
        history = await self.history(environment)
        assert any(
            p["probe"] == "flint-factor" and p["status"] == "passed"
            for row in history["probes"]
            for p in row["probes"]
        )
        queue = await self.pair(ProvisionObservation, environment)
        assert queue.queue_total == 0
        save(
            self.marker,
            "permission-finish",
            {
                "first": done.model_dump(mode="json"),
                "sibling": sibling.model_dump(mode="json"),
                "first_files": await self.files(environment, done),
                "sibling_files": await self.files(environment, sibling),
                "history": history,
                "queue": queue.model_dump(mode="json"),
                "sql": sql_snapshot(self.marker),
            },
        )

    async def idle_inspect(self) -> None:
        # Root invokes once while worker offline, then once after live-worker idle suspension.
        evidence(self.marker, "permission-finish")
        before = sql_snapshot(self.marker)
        proof_path = LAB / "evidence/provisioning-workspace-gateway-stopped.json"
        if proof_path.exists():
            proof = gateway_proof(
                self.marker,
                decode(bounded("evidence/provisioning-workspace-gateway-stopped.json", 16384)),
            )
            calls = []
            for _ in range(30):
                for kind, environment in self.marker["environments"].items():
                    value = await self.pair(ProvisionObservation, environment)
                    calls.append(
                        {"environment": kind, "observation": value.model_dump(mode="json")}
                    )
                await asyncio.sleep(0.1)
            after = sql_snapshot(self.marker)
            assert before == after, "read-only stopped observation changed retained worker state"
            save(
                self.marker,
                "idle-inspect",
                {
                    "phase": "stopped",
                    "gateway": proof,
                    "calls": calls,
                    "before": before,
                    "after": after,
                    "sql_unchanged": True,
                },
            )
        else:
            calls = []
            for environment in self.marker["environments"].values():
                _, path = self.paths(environment)
                status, value = await self.call("gui", "GET", path, expected=(503,))
                calls.append({"environment_id": environment, "status": status, **value})
            after = sql_snapshot(self.marker)
            assert before == after
            save(
                self.marker,
                "offline-inspect",
                {
                    "phase": "worker_offline",
                    "calls": calls,
                    "before": before,
                    "after": after,
                    "sql_unchanged": True,
                },
            )


def sql_snapshot(marker: dict[str, Any]) -> dict[str, Any]:
    """Bounded mode=ro allowlisted facts; no endpoint/auth/config/policy payloads."""
    environments = tuple(sorted(marker["environments"].values()))
    assert len(environments) == 2
    assert len(set(environments)) == 2
    root = directory("worker/state")
    result: dict[str, Any] = {}
    total = 0
    try:
        for name in ("worker.sqlite3", "environments.sqlite3"):
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=root)
            try:
                info = os.fstat(fd)
                assert stat.S_ISREG(info.st_mode)
                assert info.st_uid == os.getuid()
                assert info.st_nlink == 1
                assert 0 < info.st_size <= 128 * 1024 * 1024
                assert os.pread(fd, 16, 0) == b"SQLite format 3\0"
                db = sqlite3.connect(
                    f"file:{LAB / 'worker/state' / name}?mode=ro", uri=True, timeout=0.1
                )
                try:
                    db.execute("PRAGMA query_only=ON")
                    ticks = 0

                    def budget() -> int:
                        nonlocal ticks
                        ticks += 1
                        return int(ticks > 20000)

                    db.set_progress_handler(budget, 1000)
                    db.execute("BEGIN")
                    if name == "environments.sqlite3":
                        rows = db.execute(
                            "SELECT environment_id,substr(CAST(document AS BLOB),1,65537) FROM environments WHERE environment_id IN (?,?) ORDER BY environment_id",
                            environments,
                        ).fetchall()
                        assert len(rows) == 2
                        values = []
                        for env, raw in rows:
                            value = decode(raw, 65536)
                            assert value["project_id"] == PROJECT
                            assert value["environment_id"] == env
                            values.append(
                                {
                                    k: value[k]
                                    for k in (
                                        "project_id",
                                        "environment_id",
                                        "sandbox",
                                        "sandbox_id",
                                        "state",
                                    )
                                }
                            )
                        result["environments"] = values
                    else:
                        tables = (
                            ("provisions", "provision_id", "record"),
                            ("revision_entries", "revision", "record"),
                            ("kernel_start_revisions", "session_id,language", "record"),
                        )
                        for table, order, column in tables:
                            count = db.execute(
                                f"SELECT count(*) FROM {table} WHERE environment_id IN (?,?)",
                                environments,
                            ).fetchone()[0]
                            assert type(count) is int
                            assert count <= 128
                            rows = db.execute(
                                f"SELECT environment_id,substr(CAST({column} AS BLOB),1,65537) FROM {table} WHERE environment_id IN (?,?) ORDER BY environment_id,{order}",
                                environments,
                            ).fetchall()
                            values = []
                            for env, raw in rows:
                                assert type(raw) is bytes
                                assert len(raw) <= 65536
                                total += len(raw)
                                assert total <= 8 * 1024 * 1024
                                values.append(
                                    {
                                        "environment_id": env,
                                        "size": len(raw),
                                        "record_sha256": hashlib.sha256(raw).hexdigest(),
                                    }
                                )
                            result[table] = values
                        for table, columns, order in (
                            (
                                "executor_endpoints",
                                "environment_id,substr(incarnation,1,129),substr(sandbox,1,129),substr(sandbox_id,1,129)",
                                "environment_id",
                            ),
                            (
                                "kernels",
                                "environment_id,substr(session_id,1,129),substr(language,1,16),substr(incarnation,1,129),CASE WHEN typeof(base)='integer' THEN base ELSE NULL END,CASE WHEN typeof(last)='integer' THEN last ELSE NULL END",
                                "environment_id,session_id,language",
                            ),
                            (
                                "package_execution_gates",
                                "environment_id,CASE WHEN typeof(generation)='integer' THEN generation ELSE NULL END,substr(mode,1,32),substr(operation,1,129),CASE WHEN typeof(revision) IN ('integer','null') THEN revision ELSE 'invalid' END",
                                "environment_id",
                            ),
                            (
                                "environment_revisions",
                                "environment_id,CASE WHEN typeof(revision)='integer' THEN revision ELSE NULL END,substr(fingerprint,1,129),substr(changed_by,1,129)",
                                "environment_id",
                            ),
                            (
                                "executions",
                                "environment_id,substr(execution_id,1,129),substr(session_id,1,129),CASE WHEN typeof(terminal)='integer' THEN terminal ELSE NULL END",
                                "environment_id,execution_id",
                            ),
                        ):
                            rows = db.execute(
                                f"SELECT {columns} FROM {table} WHERE environment_id IN (?,?) ORDER BY {order} LIMIT 129",
                                environments,
                            ).fetchall()
                            assert len(rows) <= 128
                            assert all(
                                type(x) in (str, int, type(None)) for row in rows for x in row
                            )
                            assert all(
                                not isinstance(x, str) or len(x) <= 128 for row in rows for x in row
                            )
                            integer_positions = {
                                "kernels": (4, 5),
                                "package_execution_gates": (1,),
                                "environment_revisions": (1,),
                                "executions": (3,),
                            }
                            assert all(
                                type(row[pos]) is int and 0 <= row[pos] <= 2**62
                                for row in rows
                                for pos in integer_positions.get(table, ())
                            )
                            if table == "package_execution_gates":
                                assert all(
                                    row[4] is None or (type(row[4]) is int and 0 <= row[4] <= 2**62)
                                    for row in rows
                                )
                            result[table] = rows
                    db.rollback()
                finally:
                    db.close()
                assert identity(os.fstat(fd)) == identity(
                    os.stat(name, dir_fd=root, follow_symlinks=False)
                )
                fresh = directory("worker/state")
                try:
                    assert identity(os.fstat(fresh)) == identity(os.fstat(root))
                finally:
                    os.close(fresh)
            finally:
                os.close(fd)
    finally:
        os.close(root)
    return result


def gateway_proof(marker: dict[str, Any], value: Any) -> dict[str, Any]:
    assert type(value) is dict
    assert set(value) == {
        "version",
        "source_head",
        "run_id",
        "project_id",
        "observed_at",
        "sandboxes",
    }
    assert type(value["version"]) is int
    assert value["version"] == 1
    assert (value["source_head"], value["run_id"], value["project_id"]) == (
        marker["source_head"],
        marker["run_id"],
        PROJECT,
    )
    when = datetime.fromisoformat(value["observed_at"])
    assert when.tzinfo is not None
    assert when.utcoffset() == timedelta(0)
    finished = datetime.fromisoformat(evidence_row(marker, "permission-finish")["observed_at"])
    now = datetime.now(UTC)
    assert finished <= when <= now + timedelta(seconds=5)
    assert (now - when).total_seconds() <= 300
    assert type(value["sandboxes"]) is list
    assert len(value["sandboxes"]) == 2
    retained = sql_snapshot(marker)["environments"]
    original = evidence(marker, "initial")["sql"]["environments"]
    expected = {(e["environment_id"], e["sandbox"], e["sandbox_id"]) for e in original}
    assert {(e["environment_id"], e["sandbox"], e["sandbox_id"]) for e in retained} == expected
    actual = set()
    for row in value["sandboxes"]:
        assert type(row) is dict
        assert set(row) == {"environment_id", "name", "id", "phase"}
        assert all(type(v) is str and 0 < len(v) <= 128 for v in row.values())
        assert row["phase"] == "Stopped"
        actual.add((row["environment_id"], row["name"], row["id"]))
    assert actual == expected
    return value


def inspect(head: str, marker: dict[str, Any]) -> None:
    server = evidence(marker, "server")
    assert server["assets"] == assets()
    assert server["inputs"] == source_pins(head)
    initial = evidence(marker, "initial")
    installed = evidence(marker, "install")
    finished = evidence(marker, "permission-finish")
    assert installed["detail"]["summary"]["outcome"] == "completed"
    assert finished["first"]["summary"]["outcome"] == "completed"
    assert initial["observations"]["shared"]["queue_total"] == 0
    save(
        marker,
        "inspect",
        {
            "inputs": server["inputs"],
            "assets": server["assets"],
            "sql": sql_snapshot(marker),
            "stage_records_validated": 4,
            "scope": "real provisioning/UI reads; no scientific, recovery or operator-authority claim",
        },
    )


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stage", choices=STAGES)
    parser.add_argument("--head", required=True)
    args = parser.parse_args()
    if args.stage == "init":
        init(args.head)
        return
    marker = guard(args.head)
    if args.stage == "serve":
        await serve(args.head)
        return
    if args.stage == "inspect":
        inspect(args.head, marker)
        return
    info = decode(bounded(PRIVATE, 4096, private=True), 4096)  # Never export/hash.
    assert set(info) == {"source_head", "run_id", "login_url", "port"}
    assert (info["source_head"], info["run_id"]) == (args.head, marker["run_id"])
    parsed = urlsplit(info["login_url"])
    assert parsed.scheme == "http"
    assert parsed.hostname == "127.0.0.1"
    assert parsed.path == "/login"
    assert parsed.port == info["port"]
    assert re.fullmatch("token=[0-9a-f]{64}", parsed.query)
    assert not parsed.fragment
    base = f"http://127.0.0.1:{parsed.port}"
    async with (
        aiohttp.ClientSession(
            headers={
                "Authorization": "Bearer "
                + bounded("worker/client-a.token", 4096, private=True).decode().strip()
            },
            timeout=aiohttp.ClientTimeout(total=12),
        ) as worker,
        aiohttp.ClientSession(
            cookie_jar=aiohttp.CookieJar(unsafe=True), timeout=aiohttp.ClientTimeout(total=12)
        ) as gui,
    ):
        try:
            async with gui.get(info["login_url"], allow_redirects=False) as response:
                assert response.status == 303
                assert len(await response.content.read(4097)) <= 4096
        except (aiohttp.ClientError, TimeoutError):
            raise RuntimeError("private workspace login unavailable") from None
        checks = Checks(args.head, marker, worker, gui, base)
        await getattr(checks, args.stage.replace("-", "_"))()
    guard(args.head)
    print(
        json.dumps(
            {
                "stage": args.stage,
                "result": "PASS",
                "source_head": args.head,
                "run_id": marker["run_id"],
            }
        ),
        flush=True,
    )


if __name__ == "__main__":
    asyncio.run(main())
