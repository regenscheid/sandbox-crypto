# SPDX-License-Identifier: MIT
# ruff: noqa: S101, S607, S608
"""Lab32 recorded-package reader, gated to exact independently reviewed product pins.

GET-only; root owns ordinary baseline cell, GP operation, services, VM, operator
choices, native browser and teardown. Never run the /tmp draft or enable admission
without independent final source/helper review. No private bytes exported.
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
    RevisionSummaryPage,
)
from sandbox_science.contracts.revision_packages import RevisionPackagePage
from sandbox_science.contracts.revisions import EnvironmentRevision
from sandbox_science.orchestrator.projects import ProjectRecord, ProjectStore
from sandbox_science.orchestrator.worker_client import WorkerClient
from sandbox_science.workspace.app import create_app

SOURCE = Path(__file__).resolve().parents[2]
LAB = Path.home() / ".local/state/openshell-lab"
CONTROL = LAB / "orch/revision-packages"
PROJECT, OTHER = "lab32", "lab32b"
WORKER = "http://127.0.0.1:8100"
MARKER, PRIVATE = "revision-packages.created", "revision-packages-private.json"
PURPOSE = "disposable selected revision recorded package acceptance"
STAGES = ("describe", "init", "serve", "read", "idle-read", "idle-confirm", "inspect")
ADMISSION_READY = True  # Corrected helper and actual-main product pins independently reviewed.
Model = TypeVar("Model", bound=Observation)
EXPECTED = {
    "packages/contracts/src/sandbox_science/contracts/revision_packages.py": "fd91bd842d82da293cabb5944abbc323c3b24e917ea1d1a7d85c2f4cf584c775",
    "packages/worker/src/sandbox_science/worker/lockfile_diffs.py": "55f74237a013d20ae34c91b1bca6bbada9f1a02bef241ef9d07a336d7bbaf4b4",
    "packages/worker/src/sandbox_science/worker/provision_observations.py": "f537b292d9b8530b7e2b7e54517454cdb381c9c77df320003887de8e635604e6",
    "packages/worker/src/sandbox_science/worker/app.py": "1ae139927746b53272b5e04828d5e8a7ddbdd333bafee1e37c502d0dc2312cb0",
    "web/workspace/src/lib/provisioning.ts": "4b8f3ae93193fb25a4bc94ead06d8277829a845037e8f32c27c8083237abde64",
    "web/workspace/src/views/EnvironmentPanel.svelte": "0faa5559853a5af2bf7ac527b35670b6ebf57a65b1b5923de5e84c09839e3adb",
    "packages/workspace/src/sandbox_science/workspace/provisioning.py": "8d5011a36eff0d99ee14cafbfec40938e3ce7a657fb4ea0957ea6745362fc403",
    "packages/workspace/src/sandbox_science/workspace/provisioning_client.py": "916ffb4f25ae946ff6ac3171587f16dc89e4b585736925be3e9b8f3ecc510443",
}
CODE = (
    *tuple(EXPECTED),
    "packages/workspace/src/sandbox_science/workspace/provisioning.py",
    "packages/workspace/src/sandbox_science/workspace/provisioning_client.py",
    "packages/workspace/src/sandbox_science/workspace/app.py",
    "packages/contracts/src/sandbox_science/contracts/provision_observations.py",
    "packages/contracts/src/sandbox_science/contracts/revisions.py",
    "packages/worker/src/sandbox_science/worker/store.py",
    "packages/worker/src/sandbox_science/worker/manager.py",
    "packages/orchestrator/src/sandbox_science/orchestrator/projects.py",
    "packages/orchestrator/src/sandbox_science/orchestrator/worker_client.py",
    "web/workspace/package-lock.json",
)


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
    try:
        return json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=unique,
            parse_constant=lambda _: (_ for _ in ()).throw(ValueError("nonfinite JSON")),
        )
    except (ValueError, RecursionError):
        raise RuntimeError("bounded JSON metadata invalid") from None


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


def encoded(value: object, cap: int) -> bytes:
    raw = (
        json.dumps(value, ensure_ascii=True, sort_keys=True, indent=2, allow_nan=False) + "\n"
    ).encode("ascii")
    assert len(raw) <= cap
    return raw


def write_new(
    relative: str,
    value: object,
    cap: int = 128 * 1024,
    *,
    ownership: list[tuple[int, int]] | None = None,
) -> tuple[int, int]:
    raw = encoded(value, cap)
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


def record(project: str = PROJECT) -> ProjectRecord:
    assert project in (PROJECT, OTHER)
    store = ProjectStore(CONTROL, readonly=True)
    try:
        return store.get(project)
    finally:
        store.close()


def save(marker: dict[str, Any], name: str, payload: object) -> None:
    assert name in {
        "init",
        "server",
        "read",
        "idle-read",
        "idle-confirm",
        "inspect",
        "workspace-cleanup",
    }
    value = {
        "source_head": marker["source_head"],
        "run_id": marker["run_id"],
        "project_id": PROJECT,
        "observed_at": utc(),
        "payload": payload,
    }
    raw = encoded(value, 2 * 1024 * 1024)
    root = directory("evidence")
    try:
        count = total = scanned = 0
        with os.scandir(root) as entries:
            for entry in entries:
                scanned += 1
                assert scanned <= 1024
                if entry.name.startswith("revision-packages-"):
                    info = entry.stat(follow_symlinks=False)
                    assert stat.S_ISREG(info.st_mode)
                    assert info.st_uid == os.getuid()
                    assert info.st_nlink == 1
                    assert 0 <= info.st_size <= 2 * 1024 * 1024
                    count += 1
                    total += info.st_size
        assert count < 16
        assert total + len(raw) <= 8 * 1024 * 1024
    finally:
        os.close(root)
    write_new("evidence/revision-packages-" + name + ".json", value, 2 * 1024 * 1024)


def evidence_row(marker: dict[str, Any], name: str) -> dict[str, Any]:
    assert name in {
        "init",
        "server",
        "read",
        "idle-read",
        "idle-confirm",
        "inspect",
        "workspace-cleanup",
    }
    value = decode(
        bounded("evidence/revision-packages-" + name + ".json", 2 * 1024 * 1024),
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
    worker = WorkerClient(WORKER, worker_token())
    runner: web.AppRunner | None = None
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    owned: list[tuple[int, int]] = []
    port = None
    errors: list[str] = []
    try:
        app = create_app(
            ProjectStore(CONTROL),
            SOURCE / "web/workspace/dist",
            login_token=token,
            job_worker=worker,
        )
        runner = web.AppRunner(app, access_log=None)
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
        closers = [worker.close]
        if runner is not None:
            closers.insert(0, runner.cleanup)
        for close in closers:
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
                "runner_cleanup_attempted": runner is not None,
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


def worker_token() -> str:
    raw = bounded("worker/client-a.token", 4096, private=True)
    assert re.fullmatch(rb"[A-Za-z0-9_-]{43}\n?", raw)
    return raw.strip().decode("ascii")


def source_pins(head: str) -> dict[str, Any]:
    assert ADMISSION_READY, "preliminary helper admission disabled"
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
        sha = hashlib.sha256(raw).hexdigest()
        if name in EXPECTED:
            assert sha == EXPECTED[name]
        values[name] = {"size": len(raw), "sha256": sha}
    return values


def registry_check(marker: dict[str, Any]) -> None:
    project, other = record(), record(OTHER)
    assert project.project_id == PROJECT
    assert other.project_id == OTHER
    assert project.policy == other.policy == marker["policy"]
    assert project.profile == other.profile == "minimal"
    assert set(project.sessions) == {"main", "other"}
    assert set(other.sessions) == {"unserved"}
    assert project.default_environment_id == marker["environment"]
    assert other.default_environment_id == marker["foreign_environment"]
    assert marker["environment"] != marker["foreign_environment"]
    assert all(
        not s.dedicated and s.closed_at is None and s.environment_id == marker["environment"]
        for s in project.sessions.values()
    )


def guard(head: str) -> dict[str, Any]:
    pins = source_pins(head)
    settings_guard()
    marker = decode(bounded(MARKER, 32768))
    assert set(marker) == {
        "version",
        "project",
        "purpose",
        "source_head",
        "inputs",
        "run_id",
        "helper_sha256",
        "policy",
        "environment",
        "foreign_environment",
    }
    assert type(marker["version"]) is int
    assert marker["version"] == 1
    assert (marker["project"], marker["purpose"], marker["source_head"]) == (PROJECT, PURPOSE, head)
    assert marker["inputs"] == pins
    assert re.fullmatch(r"[0-9a-f]{32}", marker["run_id"])
    fd = os.open(__file__, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        helper = hashlib.sha256(read_fd(fd, 256 * 1024)).hexdigest()
    finally:
        os.close(fd)
    assert marker["helper_sha256"] == helper
    assert marker["policy"] == bounded("policy.digest", 128).decode("ascii").strip()
    assert re.fullmatch(r"sha256:[0-9a-f]{64}", marker["policy"])
    registry_check(marker)
    fd = directory("orch/revision-packages")
    try:
        assert stat.S_IMODE(os.fstat(fd).st_mode) == 0o700
    finally:
        os.close(fd)
    return marker


def init(head: str) -> None:
    pins = source_pins(head)
    settings_guard()
    policy = bounded("policy.digest", 128).decode("ascii").strip()
    assert re.fullmatch(r"sha256:[0-9a-f]{64}", policy)
    fd = directory()
    try:
        for name in ("orch", "evidence"):
            with contextlib.suppress(FileExistsError):
                os.mkdir(name, 0o700, dir_fd=fd)
    finally:
        os.close(fd)
    fd = directory("orch")
    try:
        os.mkdir("revision-packages", 0o700, dir_fd=fd)
    finally:
        os.close(fd)
    store = ProjectStore(CONTROL)
    project, created = store.create(PROJECT, "minimal", policy)
    assert created
    for session in ("main", "other"):
        store.open_session(PROJECT, session, dedicated=False)
    other, created = store.create(OTHER, "minimal", policy)
    assert created
    store.open_session(OTHER, "unserved", dedicated=False)
    fd = os.open(__file__, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        helper = hashlib.sha256(read_fd(fd, 256 * 1024)).hexdigest()
    finally:
        os.close(fd)
    marker = {
        "version": 1,
        "project": PROJECT,
        "purpose": PURPOSE,
        "source_head": head,
        "inputs": pins,
        "run_id": uuid.uuid4().hex,
        "helper_sha256": helper,
        "policy": policy,
        "environment": project.default_environment_id,
        "foreign_environment": other.default_environment_id,
    }
    write_new(MARKER, marker, 32768)
    guard(head)
    save(
        marker,
        "init",
        {
            "project_id": PROJECT,
            "environment_id": marker["environment"],
            "foreign_project_id": OTHER,
            "foreign_environment_id": marker["foreign_environment"],
            "session_id": "main",
        },
    )


def sql_sidecars(root: int, name: str) -> None:
    for suffix, cap in (
        ("-wal", 16 * 1024 * 1024),
        ("-shm", 1024 * 1024),
        ("-journal", 16 * 1024 * 1024),
    ):
        try:
            info = os.stat(name + suffix, dir_fd=root, follow_symlinks=False)
        except FileNotFoundError:
            continue
        assert stat.S_ISREG(info.st_mode)
        assert info.st_uid == os.getuid()
        assert info.st_nlink == 1
        assert 0 <= info.st_size <= cap


def environment_projection(value: object, environment: str, endpoints: object) -> dict[str, str]:
    """Only bounded known scalars, bound to this fresh lab's original retained endpoint."""
    pattern = r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}"
    assert type(environment) is str
    assert re.fullmatch(pattern, environment)
    assert type(value) is dict
    fields = ("project_id", "environment_id", "sandbox", "sandbox_id", "state")
    assert all(name in value for name in fields)
    assert all(type(value[name]) is str for name in fields)
    assert all(re.fullmatch(pattern, value[name]) for name in fields[:-1])
    assert value["project_id"] == PROJECT
    assert value["environment_id"] == environment
    assert value["state"] in {"creating", "ready", "stopped", "deleted"}
    assert type(endpoints) is list
    assert len(endpoints) == 1
    row = endpoints[0]
    assert type(row) in (tuple, list)
    assert len(row) == 4
    assert all(type(item) is str and re.fullmatch(pattern, item) for item in row)
    assert (row[0], row[2], row[3]) == (environment, value["sandbox"], value["sandbox_id"])
    return {name: value[name] for name in fields}


def retained_stopped_binding(
    marker: dict[str, Any], current: object, phase: str, gateway: object = None
) -> dict[str, str]:
    """Prove only the original retired binding; never manufacture a current endpoint."""
    assert phase in ("before", "after")
    accepted = evidence(marker, "read")
    assert type(accepted) is dict
    assert accepted["sql_unchanged"] is True
    before, after = accepted["before"], accepted["after"]
    assert type(before) is dict
    assert type(after) is dict
    assert before == after
    originals = before["environments"]
    assert type(originals) is list
    assert len(originals) == 1
    original = originals[0]
    assert type(original) is dict
    assert set(original) == {"project_id", "environment_id", "sandbox", "sandbox_id", "state"}
    assert original["state"] == "ready"
    endpoints = before["executor_endpoints"]
    retained = environment_projection(original, marker["environment"], endpoints)
    actual = environment_projection(current, marker["environment"], endpoints)
    assert actual["state"] == "stopped"
    assert all(actual[key] == retained[key] for key in retained if key != "state")
    sql = {"environments": [actual]}
    if gateway is None:
        gateway_proof(marker, phase, sql)
    else:
        validate_stopped_gateway(marker, sql, gateway)
    return actual


def sql_snapshot(
    marker: dict[str, Any], *, stopped_phase: str | None = None, stopped_gateway: object = None
) -> dict[str, Any]:
    """Bounded mode=ro facts; retired endpoints require explicit fresh original-VM proof."""
    assert stopped_phase is None or stopped_phase in ("before", "after")
    assert stopped_gateway is None or stopped_phase is not None
    environments = (marker["environment"], marker["environment"])
    assert len(environments) == 2
    assert len(set(environments)) == 1
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
                sql_sidecars(root, name)
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
                            "SELECT environment_id,typeof(document),length(CAST(document AS BLOB)),substr(CAST(document AS BLOB),1,65537) FROM environments WHERE environment_id IN (?,?) ORDER BY environment_id",
                            environments,
                        ).fetchall()
                        assert len(rows) == 1
                        values = []
                        for env, kind, size, raw in rows:
                            assert kind == "text"
                            assert type(size) is int
                            assert 0 < size <= 65536
                            value = decode(raw, 65536)
                            assert env == marker["environment"]
                            if result["executor_endpoints"] == [] and stopped_phase is not None:
                                projected = retained_stopped_binding(
                                    marker, value, stopped_phase, stopped_gateway
                                )
                            else:
                                projected = environment_projection(
                                    value, env, result["executor_endpoints"]
                                )
                            values.append(projected)
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
                            assert count <= 32
                            rows = db.execute(
                                f"SELECT environment_id,typeof({column}),length(CAST({column} AS BLOB)),substr(CAST({column} AS BLOB),1,65537) FROM {table} WHERE environment_id IN (?,?) ORDER BY environment_id,{order}",
                                environments,
                            ).fetchall()
                            values = []
                            for env, kind, size, raw in rows:
                                assert kind == "text"
                                assert type(size) is int
                                assert 0 < size <= 65536
                                assert type(raw) is bytes
                                assert len(raw) <= 65536
                                total += len(raw)
                                assert total <= 2 * 1024 * 1024
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
                                f"SELECT {columns} FROM {table} WHERE environment_id IN (?,?) ORDER BY {order} LIMIT 33",
                                environments,
                            ).fetchall()
                            assert len(rows) <= 32
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
                sql_sidecars(root, name)
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
    return decode(json.dumps(result, allow_nan=False).encode("utf-8"), 2 * 1024 * 1024)


def selected_sql_proof(
    marker: dict[str, Any], selected: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Two exact original rows/refs, bounded before decode; no full lockfile parse."""
    assert len(selected) == 2
    root = directory("worker/state")
    fd = -1
    try:
        name = "worker.sqlite3"
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=root)
        info = os.fstat(fd)
        assert stat.S_ISREG(info.st_mode)
        assert info.st_uid == os.getuid()
        assert info.st_nlink == 1
        assert 0 < info.st_size <= 128 * 1024 * 1024
        assert os.pread(fd, 16, 0) == b"SQLite format 3\0"
        sql_sidecars(root, name)
        result = []
        with contextlib.closing(
            sqlite3.connect(f"file:{LAB / 'worker/state' / name}?mode=ro", uri=True, timeout=0.1)
        ) as db:
            db.execute("PRAGMA query_only=ON")
            ticks = 0

            def budget() -> int:
                nonlocal ticks
                ticks += 1
                return int(ticks > 2000)

            db.set_progress_handler(budget, 1000)
            db.execute("BEGIN")
            try:
                for item in selected:
                    revision = item["revision"]
                    assert type(revision) is int
                    assert 0 <= revision <= 2**53 - 1
                    args = (marker["environment"], revision)
                    meta = db.execute(
                        "SELECT typeof(record),length(CAST(record AS BLOB)),typeof(lockfile),substr(CAST(lockfile AS BLOB),1,65),CASE WHEN typeof(lockfile_size)='integer' THEN lockfile_size END FROM revision_entries WHERE environment_id=? AND revision=?",
                        args,
                    ).fetchone()
                    assert meta is not None
                    kind, size, refkind, refsha, refsize = meta
                    assert kind == "text"
                    assert type(size) is int
                    assert 0 < size <= 65536
                    assert type(refsize) is int
                    assert 0 <= refsize <= 8 * 1024 * 1024
                    if refkind == "null":
                        assert refsha is None
                        assert refsize == 0
                    else:
                        assert refkind == "text"
                        assert type(refsha) is bytes
                        assert re.fullmatch(rb"[0-9a-f]{64}", refsha)
                    row = db.execute(
                        "SELECT substr(CAST(record AS BLOB),1,65537) FROM revision_entries WHERE environment_id=? AND revision=?",
                        args,
                    ).fetchone()
                    assert row is not None
                    assert type(row[0]) is bytes
                    assert len(row[0]) == size
                    raw = row[0]
                    decode(raw, 65536)  # Duplicate decoded keys refuse before typed normalization.
                    try:
                        record = EnvironmentRevision.model_validate_json(raw, strict=True)
                    except ValueError:
                        raise RuntimeError("selected revision metadata invalid") from None
                    ref = (
                        record.lockfile.model_dump(mode="json")
                        if record.lockfile is not None
                        else None
                    )
                    assert record.revision == revision
                    if ref is None:
                        assert refkind == "null"
                    else:
                        assert refkind == "text"
                        assert ref["sha256"].encode("ascii") == refsha
                        assert ref["size"] == refsize
                    proof = {
                        "revision": revision,
                        "record_sha256": hashlib.sha256(raw).hexdigest(),
                        "lockfile": ref,
                    }
                    assert proof == item
                    result.append(proof)
            finally:
                db.rollback()
        sql_sidecars(root, name)
        assert identity(os.fstat(fd)) == identity(os.stat(name, dir_fd=root, follow_symlinks=False))
        fresh = directory("worker/state")
        try:
            assert identity(os.fstat(fresh)) == identity(os.fstat(root))
        finally:
            os.close(fresh)
        return result
    finally:
        if fd >= 0:
            os.close(fd)
        os.close(root)


def blob_proofs(selections: list[dict[str, Any]]) -> list[dict[str, Any] | None]:
    """Root-only local byte proof. No raw bytes, credentials or content export."""
    assert len(selections) == 2
    result: list[dict[str, Any] | None] = []
    held = []
    with contextlib.ExitStack() as stack:
        for selection in selections:
            ref = selection["lockfile"]
            if ref is None:
                result.append(None)
                continue
            sha, size = ref["sha256"], ref["size"]
            assert type(sha) is str
            assert re.fullmatch(r"[0-9a-f]{64}", sha)
            assert type(size) is int
            assert 0 < size <= 8 * 1024 * 1024
            root = directory("worker/state/lockfiles/blobs/" + sha[:2])
            stack.callback(os.close, root)
            fd = os.open(sha, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=root)
            stack.callback(os.close, fd)
            before = os.fstat(fd)
            assert stat.S_ISREG(before.st_mode)
            assert before.st_uid == os.getuid()
            assert before.st_nlink == 1
            assert before.st_size == size
            digest = hashlib.sha256()
            for offset in range(0, size, 8192):
                chunk = os.pread(fd, min(8192, size - offset), offset)
                assert len(chunk) == min(8192, size - offset)
                digest.update(chunk)
            assert digest.hexdigest() == sha
            held.append((root, fd, sha, before))
            result.append({"sha256": sha, "size": size, "actual_bytes_hashed": True})
        for root, fd, sha, before in held:
            fresh = directory("worker/state/lockfiles/blobs/" + sha[:2])
            try:
                assert identity(os.fstat(fresh)) == identity(os.fstat(root))
            finally:
                os.close(fresh)
            current = os.stat(sha, dir_fd=root, follow_symlinks=False)
            after = os.fstat(fd)
            fields = (
                "st_dev",
                "st_ino",
                "st_size",
                "st_mtime_ns",
                "st_ctime_ns",
                "st_uid",
                "st_nlink",
            )
            assert all(
                getattr(before, k) == getattr(current, k) == getattr(after, k) for k in fields
            )
    return result


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

    def paths(
        self, suffix: str = "", query: dict[str, Any] | None = None, *, foreign: bool = False
    ) -> tuple[str, str]:
        env = self.marker["environment"]
        project = OTHER if foreign else PROJECT
        base = f"/environments/{env}/provisioning/observations"
        return "/v1" + base + suffix + "?" + urlencode(
            {"project_id": project, **(query or {})}
        ), f"/api/v0/projects/{project}" + base + suffix + ("?" + urlencode(query) if query else "")

    async def call(self, which: str, path: str, expected: int = 200) -> Any:
        guard(self.head)
        assert which in ("worker", "gui")
        assert path.startswith("/v1/" if which == "worker" else "/api/v0/")
        assert not any(c in path for c in ("#", "\\", "\x00"))
        http, base = (self.worker, WORKER) if which == "worker" else (self.gui, self.base)
        async with asyncio.timeout(12):
            async with http.get(
                base + path,
                allow_redirects=False,
                auto_decompress=False,
                timeout=aiohttp.ClientTimeout(total=12),
            ) as response:
                assert response.status == expected, (which, path.split("?")[0], response.status)
                assert not response.headers.get("Content-Encoding")
                assert response.content_type == "application/json"
                if which == "gui":
                    assert response.headers.get("Cache-Control") == "no-store"
                raw = bytearray()
                chunks = 0
                async for chunk in response.content.iter_chunked(8192):
                    chunks += 1
                    assert chunks <= 256
                    assert len(raw) + len(chunk) <= 65536
                    raw.extend(chunk)
                value = decode(bytes(raw), 65536)
                return error_projection(value) if expected >= 400 else value

    async def pair(
        self, model: type[Model], suffix: str = "", query: dict[str, Any] | None = None
    ) -> Model:
        values = []
        for which, path in zip(("worker", "gui"), self.paths(suffix, query), strict=True):
            try:
                value = model.model_validate_json(
                    json.dumps(await self.call(which, path)), strict=True
                )
            except ValueError:
                raise RuntimeError("bounded observation metadata invalid") from None
            assert (value.project_id, value.environment_id) == (PROJECT, self.marker["environment"])
            values.append(value)
        same_observation(*values)
        return values[0]

    async def selected_revisions(self, before: int, after: int) -> list[dict[str, Any]]:
        assert type(before) is type(after) is int
        assert 0 <= before < after <= 2**53 - 1
        rows = []
        cursor = before - 1
        for _ in range(16):
            page = await self.pair(
                RevisionSummaryPage, "/revisions", {"after": cursor, "target": after, "limit": 8}
            )
            assert page.target == after
            rows.extend(r.model_dump(mode="json") for r in page.revisions)
            assert len(rows) <= 64
            if page.next_after is None:
                break
            assert page.next_after > cursor
            cursor = page.next_after
        else:
            raise RuntimeError("selected history exceeds helper bound")
        selected = [next(r for r in rows if r["revision"] == n) for n in (before, after)]
        selected = [{k: r[k] for k in ("revision", "record_sha256", "lockfile")} for r in selected]
        assert selected_sql_proof(self.marker, selected) == selected
        return selected

    async def packages(self, selected: dict[str, Any], *, all_pages: bool = True) -> dict[str, Any]:
        query = {"pin": selected["record_sha256"], "offset": 0, "limit": 8}
        pages: list[dict[str, Any]] = []
        keys: list[tuple[str, str, str]] = []
        first = None
        byte_count = 0
        for _ in range(64):
            page = await self.pair(
                RevisionPackagePage, f"/revisions/{selected['revision']}/packages", query
            )
            value = page.model_dump(mode="json")
            assert value["selection"] == selected
            assert page.offset == query["offset"]
            assert page.limit == 8
            assert page.status == "complete", "actual inventory source is held"
            assert page.total is not None
            assert 0 < page.total <= 512
            current = {
                "selection": value["selection"],
                "header": value["header"],
                "total": value["total"],
            }
            if first is None:
                first = current
            else:
                assert current == first
            for row in page.packages:
                key = (row.ecosystem, row.location or "", row.name)
                assert not keys or key > keys[-1]
                keys.append(key)
            assert len(keys) <= 512
            byte_count += len(page.model_dump_json().encode("utf-8"))
            assert byte_count <= 768 * 1024
            pages.append(value)
            if not all_pages or page.next_offset is None:
                break
            assert page.next_offset == query["offset"] + len(page.packages)
            assert page.next_offset > query["offset"]
            query["offset"] = page.next_offset
        else:
            raise RuntimeError("inventory exceeds helper page bound")
        assert first is not None
        if all_pages:
            assert len(keys) == first["total"]
        return {"pages": pages, "all_pages": all_pages, "recorded_total": first["total"]}

    async def read(self) -> None:
        operation = decode(bounded("evidence/revision-packages-operation.json", 4096), 4096)
        assert set(operation) == {"source_head", "run_id", "gp"}
        assert (operation["source_head"], operation["run_id"]) == (self.head, self.marker["run_id"])
        provision = operation["gp"]
        assert type(provision) is str
        assert re.fullmatch(r"prv-[A-Za-z0-9_-]{1,120}", provision)
        before = sql_snapshot(self.marker)
        detail = await self.pair(ProvisionDetail, "/operations/" + provision)
        assert detail.request.capability == "gp"
        assert detail.request.operation_id == "lab32-gp-" + self.marker["run_id"]
        assert detail.request.context.session_id == "main"
        assert detail.summary.state == "finished"
        assert detail.summary.outcome == "completed"
        a, b = detail.summary.revision_before, detail.summary.revision_after
        assert a is not None
        assert b is not None
        assert type(a) is type(b) is int
        assert a >= 0
        assert b == a + 1
        selected = await self.selected_revisions(a, b)
        byte_proof = blob_proofs(selected)
        assert all(proof is not None for proof in byte_proof)
        inventories = [await self.packages(side) for side in selected]
        facts = inventory_facts(inventories)
        query = {"pin": selected[1]["record_sha256"], "offset": 0, "limit": 8}
        suffix = f"/revisions/{b}/packages"
        negative = []
        for name, changes, status, foreign in (
            (
                "wrong_pin",
                {"pin": ("0" if query["pin"][0] != "0" else "1") + query["pin"][1:]},
                409,
                False,
            ),
            ("foreign_project", {}, 404, True),
            ("invalid_limit", {"limit": 9}, 400, False),
        ):
            for which, path in zip(
                ("worker", "gui"),
                self.paths(suffix, {**query, **changes}, foreign=foreign),
                strict=True,
            ):
                error = await self.call(which, path, status)
                negative.append(
                    {
                        "case": name,
                        "route": which,
                        "status": status,
                        "error_kind": error["error_kind"],
                    }
                )
        for which, path in zip(("worker", "gui"), self.paths(suffix, query), strict=True):
            path = path.replace(
                "/environments/" + self.marker["environment"] + "/",
                "/environments/" + self.marker["foreign_environment"] + "/",
            )
            error = await self.call(which, path, 404)
            negative.append(
                {
                    "case": "foreign_environment",
                    "route": which,
                    "status": 404,
                    "error_kind": error["error_kind"],
                }
            )
        after = sql_snapshot(self.marker)
        assert before == after, "inventory reads changed allowlisted worker state"
        save(
            self.marker,
            "read",
            {
                "operation": detail.model_dump(mode="json"),
                "selected": selected,
                "inventories": inventories,
                "facts": facts,
                "selected_row_sql_verified": True,
                "actual_blob_proof": byte_proof,
                "negative": negative,
                "before": before,
                "after": after,
                "sql_unchanged": True,
                "synthetic_agpl_positive_runtime": False,
            },
        )

    async def idle_read(self) -> None:
        accepted = evidence(self.marker, "read")
        selected = accepted["selected"][1]
        expected = accepted["inventories"][1]["pages"][0]
        before = sql_snapshot(self.marker, stopped_phase="before")
        proof = gateway_proof(self.marker, "before", before)
        calls = []
        for _ in range(30):
            value = await self.packages(selected, all_pages=False)
            page = value["pages"][0]
            assert page == {**expected, "observed_at": page["observed_at"]}
            calls.append(
                {"observed_at": page["observed_at"], "status": 200, "total": page["total"]}
            )
            await asyncio.sleep(0.1)
        after = sql_snapshot(self.marker, stopped_phase="before")
        assert before == after
        save(
            self.marker,
            "idle-read",
            {
                "gateway_before": proof,
                "calls": calls,
                "before": before,
                "after": after,
                "sql_unchanged": True,
                "requires_fresh_post_read_gateway": True,
            },
        )


def inventory_facts(inventories: list[dict[str, Any]]) -> dict[str, Any]:
    """A real cross-revision unchanged entry and Conda addition, never an empty witness."""
    assert type(inventories) is list
    assert len(inventories) == 2
    rows, selections = [], []
    for inventory in inventories:
        assert inventory["all_pages"] is True
        pages = inventory["pages"]
        assert type(pages) is list
        assert 1 <= len(pages) <= 64
        selection = pages[0]["selection"]
        assert type(selection["revision"]) is int
        assert selection["revision"] >= 0
        selections.append(selection)
        row_map = {}
        for page in pages:
            assert page["status"] == "complete"
            assert page["selection"] == selection
            for row in page["packages"]:
                key = (row["ecosystem"], row["location"] or "", row["name"])
                assert key not in row_map
                row_map[key] = row
        assert 0 < len(row_map) == inventory["recorded_total"] <= 512
        rows.append(row_map)
    assert selections[1]["revision"] == selections[0]["revision"] + 1
    assert selections[0]["record_sha256"] != selections[1]["record_sha256"]
    old, new = rows
    unchanged = sorted(key for key in old.keys() & new.keys() if old[key] == new[key])
    added = sorted(new.keys() - old.keys())
    assert unchanged, "no actual unchanged package witness"
    assert any(key[0] == "conda" for key in added), "no actual Conda addition"
    return {
        "unchanged_witness": new[unchanged[0]],
        "unchanged_count": len(unchanged),
        "added": [list(key) for key in added],
        "before_total": len(old),
        "after_total": len(new),
        "declared_agpl_names_after": [
            row["name"] for row in new.values() if row["license"]["agpl"] is True
        ],
        "unknown_license_count_after": sum(
            row["license"]["declared"] is None for row in new.values()
        ),
        "installed_inventory_certification": False,
    }


def gateway_proof(marker: dict[str, Any], phase: str, sql: dict[str, Any]) -> dict[str, Any]:
    assert phase in ("before", "after")
    value = decode(bounded("evidence/revision-packages-gateway-" + phase + ".json", 16384), 16384)
    value = validate_stopped_gateway(marker, sql, value)
    stage = evidence_row(marker, "read" if phase == "before" else "idle-read")
    assert datetime.fromisoformat(value["observed_at"]) >= datetime.fromisoformat(
        stage["observed_at"]
    )
    return value


def validate_stopped_gateway(
    marker: dict[str, Any], sql: dict[str, Any], value: object
) -> dict[str, Any]:
    """Fresh original-VM identity only; qualified runners prove their own read chronology."""
    assert type(value) is dict
    assert set(value) == {
        "version",
        "source_head",
        "run_id",
        "helper_sha256",
        "project_id",
        "observed_at",
        "sandboxes",
    }
    assert type(value["version"]) is int
    assert value["version"] == 1
    metadata = {
        "source_head": (40, r"[0-9a-f]{40}"),
        "run_id": (32, r"[0-9a-f]{32}"),
        "helper_sha256": (64, r"[0-9a-f]{64}"),
        "project_id": (128, r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}"),
    }
    assert all(type(value[key]) is str for key in metadata)
    assert all(0 < len(value[key]) <= cap for key, (cap, _) in metadata.items())
    assert all(re.fullmatch(pattern, value[key]) for key, (_, pattern) in metadata.items())
    assert (value["source_head"], value["run_id"], value["helper_sha256"], value["project_id"]) == (
        marker["source_head"],
        marker["run_id"],
        marker["helper_sha256"],
        PROJECT,
    )
    assert type(value["observed_at"]) is str
    assert 0 < len(value["observed_at"]) <= 64
    when = datetime.fromisoformat(value["observed_at"])
    now = datetime.now(UTC)
    assert when.tzinfo is not None
    assert when.utcoffset() == timedelta(0)
    assert now - timedelta(seconds=300) <= when <= now + timedelta(seconds=5)
    stage = evidence_row(marker, "read")
    assert when >= datetime.fromisoformat(stage["observed_at"])
    assert type(value["sandboxes"]) is list
    assert len(value["sandboxes"]) == 1
    row = value["sandboxes"][0]
    assert type(row) is dict
    assert set(row) == {"environment_id", "name", "id", "phase"}
    assert all(type(x) is str and 0 < len(x) <= 128 for x in row.values())
    original = evidence(marker, "read")["before"]["environments"][0]
    current = sql["environments"][0]
    assert (row["environment_id"], row["name"], row["id"], row["phase"]) == (
        marker["environment"],
        original["sandbox"],
        original["sandbox_id"],
        "Stopped",
    )
    assert (current["environment_id"], current["sandbox"], current["sandbox_id"]) == (
        row["environment_id"],
        row["name"],
        row["id"],
    )
    return value


def final_stage(head: str, marker: dict[str, Any], stage: str) -> None:
    server = evidence(marker, "server")
    assert server["assets"] == assets()
    assert server["inputs"] == source_pins(head)
    if stage == "idle-confirm":
        prior = evidence(marker, "idle-read")
        current = sql_snapshot(
            marker, stopped_phase="after"
        )  # JSON-normalized tuples/lists at the producer boundary.
        assert prior["before"] == prior["after"] == current
        proof = gateway_proof(marker, "after", current)
        assert sql_snapshot(marker, stopped_phase="after") == current
        save(
            marker,
            "idle-confirm",
            {
                "gateway_after": proof,
                "sql_unchanged": True,
                "sql": current,
                "bounded_no_wake_paired_reads": 30,
                "continuous_lifecycle_proof": False,
            },
        )
    else:
        assert stage == "inspect"
        evidence(marker, "idle-confirm")
        save(
            marker,
            "inspect",
            {
                "source_pins": server["inputs"],
                "assets": server["assets"],
                "sql": sql_snapshot(marker, stopped_phase="after"),
                "actual_catalog_operations": 1,
                "inventory_stage": "passed",
                "idle_stage": "passed",
                "synthetic_missing_corruption_agpl_runtime": False,
            },
        )


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stage", choices=STAGES)
    parser.add_argument("--head")
    args = parser.parse_args()
    if args.stage == "describe":
        print(
            json.dumps(
                {
                    "admission_ready": ADMISSION_READY,
                    "project": PROJECT,
                    "stages": STAGES,
                    "effects": "none; root supplies one real baseline cell and GP operation",
                    "page_limit": 8,
                    "max_pages": 64,
                    "max_entries": 512,
                    "source": "exact reviewed source; root owns separate runtime admission and all effects",
                }
            )
        )
        return
    assert type(args.head) is str
    assert re.fullmatch(r"[0-9a-f]{40}", args.head)
    if args.stage == "init":
        init(args.head)
        return
    marker = guard(args.head)
    if args.stage == "serve":
        await serve(args.head)
        return
    if args.stage in ("idle-confirm", "inspect"):
        final_stage(args.head, marker, args.stage)
        return
    info = decode(bounded(PRIVATE, 4096, private=True), 4096)
    assert set(info) == {"source_head", "run_id", "login_url", "port"}
    assert (info["source_head"], info["run_id"]) == (args.head, marker["run_id"])
    login = urlsplit(info["login_url"])
    assert login.scheme == "http"
    assert login.hostname == "127.0.0.1"
    assert login.path == "/login"
    assert type(info["port"]) is int
    assert 1024 < info["port"] < 65536
    assert login.port == info["port"]
    assert not login.fragment
    assert re.fullmatch(r"token=[0-9a-f]{64}", login.query)
    base = f"http://127.0.0.1:{login.port}"
    async with (
        aiohttp.ClientSession(
            headers={"Authorization": "Bearer " + worker_token()},
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
        await (checks.read() if args.stage == "read" else checks.idle_read())
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
