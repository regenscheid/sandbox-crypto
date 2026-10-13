# SPDX-License-Identifier: MIT
# ruff: noqa: S101, S603, S607, S608
"""Root-only Lab33 internal dispatcher fixture; default describe does nothing.

Every effectful command requires explicit --admit and exact signed clean source.
Root owns normal
lab infrastructure, synthetic provider prerequisite, all cells/controls/VM effects
and teardown. This uses synthetic host admission with actual canonical records,
not an authenticated controller/model/launcher/grant or filesystem isolation.
"""

from __future__ import annotations

import argparse
import asyncio
import fcntl
import hashlib
import json
import logging
import os
import re
import select
import signal
import sqlite3
import stat
import subprocess
import time
import tomllib
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from types import SimpleNamespace
from typing import Any

ADMISSION_READY = True
SOURCE = Path(__file__).resolve().parents[2]
LAB = Path.home() / ".local/state/openshell-lab"
PROJECT = "lab33"
SESSIONS = ("lab33-session1", "lab33-session2")
MARKER = "run-kernel.created"
CONTROL = "run-kernel-control"
PREFIX = "evidence/run-kernel-"
MAX_BYTES = 256 * 1024
WHOLE_SECONDS = 1800
MAX_CELLS = 20
MAX_CONTROLS = 8
SHARED_TEXT = "lab33 shared\n"
TERMINAL = {"succeeded", "failed", "cancelled", "timed_out", "lost"}
ERRORS = {
    "invalid_request",
    "not_found",
    "conflict",
    "unsupported",
    "unavailable",
    "policy_denied",
    "internal",
    "cancelled",
    "timed_out",
}
# Exact corrected union production inputs; root separately proves full gate-input equivalence.
INPUTS: dict[str, str] = {
    "packages/worker/src/sandbox_science/worker/cli.py": "dda5982432fec71ebb99bc942331f0bae531f0e573c2e1e7ae7143c6164838a9",
    "packages/worker/src/sandbox_science/worker/app.py": "1ae139927746b53272b5e04828d5e8a7ddbdd333bafee1e37c502d0dc2312cb0",
    "packages/worker/src/sandbox_science/worker/manager.py": "fe28a4fe1f62784b4032e7d1c09eace06a80301ba02915b3d20c8f455e68ed6b",
    "packages/worker/src/sandbox_science/worker/store.py": "11f3d72f0f29345221eaf0363513819255dc99422def11b1ae8b5dfd3a864714",
    "packages/worker/src/sandbox_science/worker/run_dispatch.py": "f2e62fa5997dd345a402a7a8cf291aa55004f9bb8487b73f7a06d9334cf78183",
    "packages/worker/src/sandbox_science/worker/run_kernel_state.py": "b827d8916d7b477cd3e192400f8a645ff7ddcd06f682fd649b53ddfeade77cfc",
    "packages/worker/src/sandbox_science/worker/run_kernels.py": "4d2fb7b6be091e0fb72c5fb501372fb065a77f1c3ff1f574e64be7c5b9271561",
    "packages/worker/src/sandbox_science/worker/executor.py": "07badfd038c69f5127ef05f8826e1d15465b0bf1ca60e13235cb6b994f7df713",
    "packages/worker/src/sandbox_science/worker/daemon.py": "38ee52391add355bc34ff852780913eec61c0b444dcb9c86796855d30a57cce5",
    "packages/worker/src/sandbox_science/worker/environments.py": "06a288e586a4a02d99dc25064614a3e35127e5b559dd7b5c515714e8804a35c8",
    "packages/worker/src/sandbox_science/worker/backends.py": "7f22eee4dd47663bc62da668df03f6cb02e01a5a7a5fafebd52d10dc506f4fbf",
    "packages/worker/src/sandbox_science/worker/shared_sync.py": "94ae6ccdf744e700181883924f84f6a00c623220954926b3b4e4dff1c505baa8",
    "packages/worker/src/sandbox_science/worker/provisioner.py": "8aaddce1167f6d43b337aae2c7f3a9e880da5e22fd7d8c7d8e01c8ae083fccd1",
    "packages/worker/src/sandbox_science/worker/settings.py": "ee9ece4daf3b493356876b4711dacdc9a6cdae2d00f9ba0e8dede9ef15e618ef",
    "packages/contracts/src/sandbox_science/contracts/run_cells.py": "c426158dc1dd48058a2cd82ce49e96ebb3757954335e820302aafca9706bc5c0",
    "packages/contracts/src/sandbox_science/contracts/run_kernels.py": "33b407d03bc9c87b1a87aff2127807eabd667d280df0758a9fe88bb724e3e72b",
    "packages/contracts/src/sandbox_science/contracts/worker.py": "dee0d24827e53dd16fb625c4ba5320377fe32ed410fe40ac771e93c09e12a21e",
    "packages/contracts/src/sandbox_science/contracts/kernel_lineage.py": "80e67d15b097eb717d38e087d6ffa0aa0c512472251f9815873541d8a81d6196",
    "packages/contracts/src/sandbox_science/contracts/context.py": "5cfb8320d06febb700fdda653e30909a3fd06f19030a29874544f9e4ac593a62",
    "packages/contracts/src/sandbox_science/contracts/environments.py": "77f70badb5586a1dedf9ff940b04187057fecf14980c459fe69a1db71c6b4d67",
    "packages/orchestrator/src/sandbox_science/orchestrator/projects.py": "b007ec185139de0e103bf36c90ac1adfd6a3d6134619380d07b48e8d357bd97e",
    "packages/orchestrator/src/sandbox_science/orchestrator/delegation_records.py": "98180b4fbcde83dae9d2feb004fb26b7caf72f893489e771433c7c37d7fdc7de",
    "packages/orchestrator/src/sandbox_science/orchestrator/documents/store.py": "f6f5ec25e8f40e58d60306fe5e6a9e5e9de00955572ec20db404108fad6bcc42",
}


class RefusalError(Exception):
    """Fixed local refusal; never carry raw configuration, subprocess or guest errors."""


def utc() -> str:
    return datetime.now(UTC).isoformat()


def identifier(value: object, cap: int = 128) -> str:
    if (
        type(value) is not str
        or not 1 <= len(value) <= cap
        or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", value) is None
    ):
        raise RefusalError("invalid fixed identity")
    return value


def digest(value: object) -> str:
    if type(value) is not str or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise RefusalError("invalid public digest")
    return value


def pairs(items: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, item in items:
        if key in result:
            raise RefusalError("duplicate public key")
        result[key] = item
    return result


def decode(raw: bytes, cap: int = MAX_BYTES) -> Any:
    if type(raw) is not bytes or len(raw) > cap:
        raise RefusalError("public input exceeds cap")
    return json.loads(
        raw.decode("utf-8"),
        object_pairs_hook=pairs,
        parse_constant=lambda _: (_ for _ in ()).throw(RefusalError("nonfinite public input")),
    )


def encoded(value: object, cap: int = MAX_BYTES) -> bytes:
    remaining = cap

    def charge(item: object, depth: int = 0) -> None:
        nonlocal remaining
        if depth > 16:
            raise RefusalError("public nesting exceeds cap")
        remaining -= 2 * depth + 4
        kind = type(item)
        if item is None or kind is bool:
            remaining -= 6
        elif kind is int:
            if not -(2**63) <= item < 2**63:
                raise RefusalError("public integer exceeds cap")
            remaining -= 22
        elif kind is str:
            if len(item) > cap:
                raise RefusalError("public string exceeds cap")
            remaining -= (
                sum(
                    12
                    if ord(char) > 0xFFFF
                    else 6
                    if ord(char) < 32 or ord(char) >= 127
                    else 2
                    if char in '"\\'
                    else 1
                    for char in item
                )
                + 3
            )
        elif kind in (list, tuple):
            if len(item) > 256:
                raise RefusalError("public list exceeds cap")
            remaining -= 2
            for child in item:
                charge(child, depth + 1)
        elif kind is dict:
            if len(item) > 128:
                raise RefusalError("public object exceeds cap")
            remaining -= 2
            for key, child in item.items():
                if type(key) is not str:
                    raise RefusalError("public key type differs")
                charge(key, depth + 1)
                charge(child, depth + 1)
        else:
            raise RefusalError("public scalar type differs")
        if remaining < 0:
            raise RefusalError("public aggregate exceeds cap")

    charge(value)
    raw = (json.dumps(value, sort_keys=True, ensure_ascii=True, indent=2) + "\n").encode("ascii")
    if len(raw) > cap:
        raise RefusalError("public aggregate exceeds cap")
    return raw


def directory(relative: str = "") -> int:
    held = os.open(LAB, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        root = os.fstat(held)
        if root.st_uid != os.getuid() or stat.S_IMODE(root.st_mode) != 0o700:
            raise RefusalError("lab ownership differs")
        for name in relative.split("/") if relative else ():
            identifier(name)
            child = os.open(
                name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=held
            )
            os.close(held)
            held = child
            if os.fstat(held).st_uid != os.getuid():
                raise RefusalError("directory owner differs")
        return held
    except BaseException:
        os.close(held)
        raise


def bounded(relative: str, cap: int = MAX_BYTES, *, mode: int | None = None) -> bytes:
    parent, _, name = relative.rpartition("/")
    identifier(name)
    root = directory(parent)
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=root)
        try:
            before = os.fstat(fd)
            if (
                not stat.S_ISREG(before.st_mode)
                or before.st_uid != os.getuid()
                or before.st_nlink != 1
                or not 0 <= before.st_size <= cap
                or (mode is not None and stat.S_IMODE(before.st_mode) != mode)
            ):
                raise RefusalError("file ownership or cap differs")
            raw = os.pread(fd, cap + 1, 0)
            after = os.fstat(fd)
            if len(raw) != before.st_size or (
                before.st_dev,
                before.st_ino,
                before.st_size,
                before.st_mtime_ns,
                before.st_ctime_ns,
            ) != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns):
                raise RefusalError("file changed during read")
            return raw
        finally:
            os.close(fd)
    finally:
        os.close(root)


def write_new(relative: str, value: object) -> None:
    raw = encoded(value)
    parent, _, name = relative.rpartition("/")
    identifier(name)
    root = directory(parent)
    fd: int | None = None
    try:
        fd = os.open(
            name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600,
            dir_fd=root,
        )
        # Ownership starts at O_EXCL; an incomplete evidence file stays an unknown
        # immutable intent. It is never silently removed/replaced/reused for effects.
        info = os.fstat(fd)
        if info.st_uid != os.getuid() or info.st_nlink != 1:
            raise RefusalError("new evidence ownership differs")
        offset = 0
        while offset < len(raw):
            written = os.write(fd, raw[offset:])
            if written <= 0:
                raise RefusalError("evidence write did not progress")
            offset += written
        os.fsync(fd)
        os.fsync(root)
    finally:
        if fd is not None:
            os.close(fd)
        os.close(root)


def git(*args: str) -> str:
    process = subprocess.Popen(
        ["git", *args],
        cwd=SOURCE,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    data = bytearray()
    deadline = time.monotonic() + 10
    try:
        assert process.stdout is not None
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                raise RefusalError("source guard deadline")
            ready, _, _ = select.select([process.stdout], [], [], min(left, 0.2))
            if not ready:
                continue
            chunk = os.read(process.stdout.fileno(), 4096)
            if not chunk:
                break
            data.extend(chunk)
            if len(data) > 65536:
                raise RefusalError("source guard output cap")
        if process.wait(timeout=max(0.01, deadline - time.monotonic())) != 0:
            raise RefusalError("source guard failed")
        return data.decode("ascii").strip()
    finally:
        if process.poll() is None:
            process.kill()
        process.wait()
        if process.stdout is not None:
            process.stdout.close()


def source_guard(head: str) -> dict[str, object]:
    if not ADMISSION_READY:
        raise RefusalError("helper admission is disabled")
    if type(head) is not str or re.fullmatch(r"[0-9a-f]{40}", head) is None:
        raise RefusalError("source pin differs")
    if (
        git("rev-parse", "HEAD") != head
        or git("status", "--porcelain")
        or git("log", "-1", "--format=%G?") != "G"
    ):
        raise RefusalError("source is not exact signed clean head")
    pins = {}
    if not INPUTS:
        raise RefusalError("source inputs are not pinned")
    for name, expected in INPUTS.items():
        fd = os.open(SOURCE / name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
        try:
            info = os.fstat(fd)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.getuid()
                or not 0 <= info.st_size <= 1024 * 1024
            ):
                raise RefusalError("source input ownership differs")
            raw = os.pread(fd, 1024 * 1024 + 1, 0)
            after = os.fstat(fd)
            if len(raw) != info.st_size or (
                info.st_dev,
                info.st_ino,
                info.st_size,
                info.st_mtime_ns,
            ) != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
                raise RefusalError("source input changed")
            observed = hashlib.sha256(raw).hexdigest()
            if observed != expected:
                raise RefusalError("source input pin differs")
            pins[name] = {"bytes": len(raw), "sha256": observed}
        finally:
            os.close(fd)
    fd = os.open(__file__, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    try:
        before = os.fstat(fd)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != os.getuid()
            or before.st_nlink != 1
            or not 0 < before.st_size <= 256 * 1024
        ):
            raise RefusalError("helper ownership or cap differs")
        helper = os.pread(fd, 256 * 1024 + 1, 0)
        after = os.fstat(fd)
        if len(helper) != before.st_size or (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
        ) != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns):
            raise RefusalError("helper changed during read")
    finally:
        os.close(fd)
    return {
        "source_head": head,
        "helper_sha256": hashlib.sha256(helper).hexdigest(),
        "inputs": pins,
    }


def marked(head: str) -> dict[str, Any]:
    pins = source_guard(head)
    value = decode(bounded(MARKER, 65536), 65536)
    if (
        type(value) is not dict
        or value.get("version") != 1
        or type(value.get("version")) is not int
    ):
        raise RefusalError("marker version differs")
    if (
        value.get("project_id") != PROJECT
        or value.get("purpose") != "disposable internal run-kernel acceptance"
        or value.get("source_head") != head
        or value.get("helper_sha256") != pins["helper_sha256"]
        or value.get("inputs") != pins["inputs"]
    ):
        raise RefusalError("marker/source scope differs")
    if (
        type(value.get("policy")) is not str
        or re.fullmatch(r"sha256:[0-9a-f]{64}", value["policy"]) is None
    ):
        raise RefusalError("marker policy differs")
    stamp(value.get("recorded_at"))
    identifier(value.get("run_id"))
    identifier(value.get("environment_id"))
    if (
        value.get("sessions") != list(SESSIONS)
        or type(value.get("children")) is not dict
        or set(value["children"]) != {"A", "B", "C"}
    ):
        raise RefusalError("canonical fixture scope differs")
    for label, child in value["children"].items():
        if type(child) is not dict or set(child) != {
            "session_id",
            "parent_run_id",
            "delegation_id",
            "child_run_id",
            "request_sha256",
        }:
            raise RefusalError("prepared identity shape differs")
        for key in ("session_id", "parent_run_id", "delegation_id", "child_run_id"):
            identifier(child[key])
        digest(child["request_sha256"])
        if child["session_id"] != SESSIONS[1 if label == "C" else 0]:
            raise RefusalError("prepared child session differs")
    return value


def private_settings() -> Any:
    # This function is reachable only after source/owned marker admission. No
    # private bytes/value/hash is returned to public evidence or logging.
    from sandbox_science.worker.settings import Settings

    value = tomllib.loads(bounded("worker/worker.toml", 65536, mode=0o600).decode("utf-8"))
    if value.get("listen") != "127.0.0.1:8100" or "local" in value:
        raise RefusalError("worker configuration scope differs")
    base = LAB / "worker"
    for key in ("token_file", "state_dir"):
        value[key] = base / value[key]
    openshell = value.get("openshell")
    if type(openshell) is not dict or openshell.get("daemon", True) is not True:
        raise RefusalError("managed daemon is required")
    environments = openshell.get("environments")
    if type(environments) is not dict or environments.get("suspend_after") is not None:
        raise RefusalError("uncontrolled suspender configuration")
    for key in ("policy_store", "targets", "ceiling", "event_store"):
        if environments.get(key):
            environments[key] = base / environments[key]
    clients = value.get("clients", [])
    if (
        type(clients) is not list
        or len(clients) > 2
        or any(
            type(client) is not dict or client.get("projects") != [PROJECT] for client in clients
        )
    ):
        raise RefusalError("unused configured client scope differs")
    # Empty clients are valid: the synthetic app guard has no listener/requests.
    settings = Settings.model_validate(value)
    if (
        settings.state_dir != LAB / "worker/state"
        or settings.token_file != LAB / "worker/worker.token"
        or settings.openshell is None
        or settings.kernel_idle_timeout is not None
    ):
        raise RefusalError("worker lifetime/path differs")
    return settings


async def owned(
    work: Any, withdraw: Callable[[], None] | None = None
) -> tuple[Any, asyncio.CancelledError | None]:
    task = asyncio.ensure_future(work)
    first = None
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError as error:
            if first is None:
                first = error
            if withdraw is not None:
                withdraw()
        except BaseException:
            if first is not None:
                raise first from None
            raise
    if first is not None and task.cancelled():
        raise first
    if first is not None:
        try:
            result = task.result()
        except BaseException:
            raise first from None
        return result, first
    return task.result(), first


class Owner:
    """One actual CLI/App factory owner; no TCPSite, listener or HTTP client."""

    def __init__(self) -> None:
        self.manager: Any = None
        self.store: Any = None
        self.environments: Any = None
        self.provisioner: Any = None
        self.documents: Any = None
        self.shared: Any = None
        self.runner: Any = None
        self.closed = False
        self.cleanup: dict[str, object] = {"owners_drained": False, "borrowed_stores_closed": False}

    async def open(self, *, marker: dict[str, Any], reopened: bool = False) -> None:
        from aiohttp import web

        from sandbox_science.worker import cli
        from sandbox_science.worker.app import create_app
        from sandbox_science.worker.artifacts import Blobs
        from sandbox_science.worker.backends import OpenShellBackend
        from sandbox_science.worker.documents_catalog import DocumentsCatalog
        from sandbox_science.worker.manager import ExecutionManager
        from sandbox_science.worker.shared_sync import SharedRecords, SharedSync
        from sandbox_science.worker.store import Store

        settings = private_settings()
        logging.disable(logging.CRITICAL)
        self.environments = cli._environments(settings)
        if self.environments is None:
            raise RefusalError("managed environments are required")
        self.store = Store(settings.state_dir / "worker.sqlite3")
        self.documents = DocumentsCatalog(
            settings.state_dir / "documents-catalog.sqlite3", settings.document_catalog
        )
        self.shared = (
            SharedSync(
                SharedRecords(settings.state_dir / "shared.sqlite3"), settings.state_dir / "shared"
            )
            if settings.shared_sync
            else None
        )
        if self.shared is None:
            raise RefusalError("normal shared workspace path is required")
        backend = cli._backend(settings, self.environments)
        if not isinstance(backend, OpenShellBackend):
            raise RefusalError("host fallback is forbidden")
        self.manager = ExecutionManager(
            self.store,
            backend,
            Blobs(settings.state_dir / "artifacts"),
            pool_id=settings.pool_id,
            max_artifact_size=settings.max_artifact_size,
            state_dir=settings.state_dir,
            shared=self.shared,
            package_cache=settings.openshell.package_cache,
        )
        env, m = self.environments, self.manager
        env.release, env.release_idle = m.release, m.release_if_idle
        m.notices = env.notices
        env.deleted.append(self.shared.forget)
        m.confirmed_daemon_loss = partial(cli._confirmed_daemon_loss, settings, m, env, self.store)
        m.recycle_daemon = env.recycle_executor

        def identity(environment_id: str) -> tuple[str, str] | None:
            if not env.manages(environment_id):
                return None
            info = env.get(environment_id)
            return (info.sandbox, info.sandbox_id) if info.sandbox_id else None

        m.daemon_identity = identity
        self.provisioner = cli._provisioner(settings, m, env)
        self.runner = web.AppRunner(
            create_app(
                m,
                "lab33-unused-no-http-synthetic-token",
                env,
                settings.clients,
                self.provisioner,
                self.documents,
            ),
            access_log=None,
        )
        if reopened:
            require_reopen(m, marker)
            m.run_dispatcher.initialize()
        await self.shared.sweep()
        await m.start()
        await self.provisioner.start()
        await self.runner.setup()

    async def close(self) -> None:
        if self.closed:
            return

        async def drain() -> None:
            failed = []
            # A failed close is not proof of quiescence. Still attempt every
            # independent actual owner; retain borrowed stores if any drain fails.
            for name, work in (
                ("runner", self.runner.cleanup if self.runner else None),
                ("provisioner", self.provisioner.close if self.provisioner else None),
                ("manager", self.manager.close if self.manager else None),
            ):
                if work is not None:
                    try:
                        await work()
                    except BaseException:
                        failed.append(name)
            self.cleanup = {
                "owners_drained": not failed,
                "failed_owners": failed,
                "borrowed_stores_closed": False,
            }
            if failed:
                raise RefusalError("host owner drain failed; borrowed stores held")
            if self.provisioner is not None and self.provisioner.permissions is not None:
                self.provisioner.permissions.close()
            for store in (
                self.store,
                self.documents,
                self.shared.records if self.shared else None,
                self.environments,
            ):
                if store is not None:
                    store.close()
            self.closed = True
            self.cleanup["borrowed_stores_closed"] = True

        _, interrupted = await owned(drain())
        if interrupted is not None:
            raise interrupted


def require_reopen(manager: Any, marker: dict[str, Any]) -> None:
    from sandbox_science.contracts.run_kernels import RunKernelScope
    from sandbox_science.worker.run_kernels import RunKernelBindings

    state = manager.run_dispatcher.state
    state.require()
    state.budget()
    records = RunKernelBindings(manager.store)
    enrolled = {}
    for label in ("A", "B", "C"):
        child = marker["children"][label]
        scope = RunKernelScope(
            project_id=PROJECT,
            session_id=child["session_id"],
            environment_id=marker["environment_id"],
            parent_run_id=child["parent_run_id"],
            delegation_id=child["delegation_id"],
            child_run_id=child["child_run_id"],
        )
        saved = records.get(scope)
        receipt = decode(bounded(PREFIX + "enrollment-" + label.lower() + ".json", 16384), 16384)
        if (
            type(receipt) is not dict
            or receipt.get("receipt") != saved.model_dump(mode="json")
            or any(
                receipt.get(key) != marker[key]
                for key in ("source_head", "helper_sha256", "run_id", "project_id")
            )
        ):
            raise RefusalError("reopen original enrollment proof differs")
        enrolled[label] = saved.binding
    for name, label in (("01-main", None), ("02-a", "A"), ("03-b", "B")):
        receipt = decode(bounded(PREFIX + "cell-" + name + ".json", 16384), 16384)
        if (
            type(receipt) is not dict
            or type(receipt.get("result")) is not dict
            or any(
                receipt.get(key) != marker[key]
                for key in ("source_head", "helper_sha256", "run_id", "project_id")
            )
        ):
            raise RefusalError("reopen original attempt proof differs")
        saved = receipt["result"]
        execution_id = identifier(saved.get("execution_id"))
        row = manager.store._db.execute(
            "SELECT typeof(record),length(CAST(record AS BLOB)) FROM executions WHERE execution_id=?",
            (execution_id,),
        ).fetchone()
        if row is None or row[0] != "text" or type(row[1]) is not int or not 0 < row[1] <= 65536:
            raise RefusalError("reopen original attempt record differs")
        execution = (
            manager.store.run_execution(execution_id)
            if label
            else manager.store.execution(execution_id)
        )
        if execution is None or (
            execution.attempt_id,
            execution.operation_id,
            execution.context.project_id,
            execution.context.session_id,
            execution.context.environment_id,
            execution.state.value,
        ) != (
            saved.get("attempt_id"),
            "lab33-" + name,
            PROJECT,
            SESSIONS[0],
            marker["environment_id"],
            "succeeded",
        ):
            raise RefusalError("reopen original attempt identity differs")
        if label and state.request(execution).binding != enrolled[label]:
            raise RefusalError("reopen selected original binding differs")
    receipt = decode(bounded(PREFIX + "receipt-restart-a.json", 16384), 16384)
    original = state.lookup_control(enrolled["A"], "lab33-restart-a", "restart", "python")
    if (
        original is None
        or original.state != "done"
        or type(receipt) is not dict
        or receipt.get("result") != original.model_dump(mode="json")
    ):
        raise RefusalError("reopen completed original control is missing or different")


def make_directory(name: str) -> None:
    identifier(name)
    root = directory()
    try:
        os.mkdir(name, 0o700, dir_fd=root)
        os.fsync(root)
    finally:
        os.close(root)


def prepare(head: str) -> dict[str, object]:
    pins = source_guard(head)
    # Exclusive directory makes an interrupted preparation held, never a reseed.
    make_directory(CONTROL)
    try:
        fd = directory("evidence")
    except FileNotFoundError:
        make_directory("evidence")
    else:
        os.close(fd)
    from sandbox_science.orchestrator.delegation_records import (
        DelegationBrief,
        DelegationRecords,
        DelegationScope,
        ParentAuthority,
        ProfileIntent,
        RequestedLimits,
    )
    from sandbox_science.orchestrator.documents.store import DocumentStore
    from sandbox_science.orchestrator.projects import ProjectStore

    policy = bounded("policy.digest", 128).decode("ascii").strip()
    if re.fullmatch(r"sha256:[0-9a-f]{64}", policy) is None:
        raise RefusalError("public policy pin differs")
    projects = ProjectStore(LAB / CONTROL)
    project, created = projects.create(PROJECT, "minimal", policy)
    if not created:
        raise RefusalError("project is not fresh")
    for session in SESSIONS:
        result = projects.open_session(PROJECT, session, dedicated=False)
        if result.environment_id != project.default_environment_id or result.closed_at is not None:
            raise RefusalError("canonical session differs")
    documents = DocumentStore(LAB / CONTROL / "control.sqlite3", producer="lab33-synthetic-host")
    children = {}
    try:
        documents.initialize()
        records = DelegationRecords(documents, initialize=True)
        for label, session in (("A", SESSIONS[0]), ("B", SESSIONS[0]), ("C", SESSIONS[1])):
            authority = ParentAuthority(
                scope=DelegationScope(
                    project_id=PROJECT,
                    session_id=session,
                    environment_id=project.default_environment_id,
                ),
                parent_run_id="lab33-parent-" + session,
            )
            receipt = records.prepare(
                authority,
                "lab33-prepare-" + label.lower(),
                DelegationBrief(
                    task="Fixed harmless scalar kernel fixture " + label,
                    context_summary="Synthetic host admission; no live controller, model, launcher or grant.",
                    inputs=(),
                    profile=ProfileIntent(
                        profile_id="lab33-synthetic",
                        profile_sha256=hashlib.sha256(b"lab33-synthetic").hexdigest(),
                    ),
                    limits=RequestedLimits(turns=1, wall_seconds=120, output_tokens=128),
                ),
            )
            children[label] = {
                "session_id": session,
                "parent_run_id": receipt.parent_run_id,
                "delegation_id": receipt.delegation_id,
                "child_run_id": receipt.child_run_id,
                "request_sha256": receipt.request_sha256,
            }
    finally:
        documents.close()
    marker = {
        "version": 1,
        "purpose": "disposable internal run-kernel acceptance",
        "project_id": PROJECT,
        "source_head": head,
        "helper_sha256": pins["helper_sha256"],
        "inputs": pins["inputs"],
        "run_id": "lab33-" + uuid.uuid4().hex,
        "environment_id": project.default_environment_id,
        "policy": policy,
        "sessions": list(SESSIONS),
        "children": children,
        "recorded_at": utc(),
    }
    write_new(MARKER, marker)
    return {"stage": "prepared", "marker": marker, "effects_started": False}


def stamp(value: object) -> datetime:
    if type(value) is not str or not 1 <= len(value) <= 40:
        raise RefusalError("timestamp shape differs")
    result = datetime.fromisoformat(value)
    offset = result.utcoffset()
    if offset is None or offset.total_seconds() != 0:
        raise RefusalError("timestamp is not aware UTC")
    return result


def envelope(marker: dict[str, Any], phase: str) -> dict[str, object]:
    return {
        "version": 1,
        "source_head": marker["source_head"],
        "helper_sha256": marker["helper_sha256"],
        "run_id": marker["run_id"],
        "project_id": PROJECT,
        "phase": phase,
        "recorded_at": utc(),
    }


def gateway(
    marker: dict[str, Any], original: dict[str, str], phase: str, since: str
) -> dict[str, Any]:
    value = decode(bounded(PREFIX + "stopped-" + phase + ".json", 8192), 8192)
    keys = {
        "version",
        "source_head",
        "helper_sha256",
        "run_id",
        "project_id",
        "phase",
        "observed_at",
        "sandboxes",
    }
    if (
        type(value) is not dict
        or len(value) != len(keys)
        or set(value) != keys
        or type(value["version"]) is not int
        or value["version"] != 1
    ):
        raise RefusalError("gateway proof shape differs")
    for key in ("source_head", "helper_sha256", "run_id", "project_id", "phase"):
        if type(value[key]) is not str or len(value[key]) > 128:
            raise RefusalError("gateway proof scalar differs")
    if (
        any(
            value[key] != marker[key]
            for key in ("source_head", "helper_sha256", "run_id", "project_id")
        )
        or value["phase"] != phase
    ):
        raise RefusalError("gateway proof scope differs")
    rows = value["sandboxes"]
    if (
        type(rows) is not list
        or len(rows) != 1
        or type(rows[0]) is not dict
        or len(rows[0]) != 4
        or set(rows[0]) != {"environment_id", "name", "id", "phase"}
    ):
        raise RefusalError("gateway original VM proof differs")
    for item in rows[0].values():
        identifier(item)
    if rows[0] != {
        "environment_id": original["environment_id"],
        "name": original["sandbox"],
        "id": original["sandbox_id"],
        "phase": "Stopped",
    }:
        raise RefusalError("gateway replaced VM or phase differs")
    observed = stamp(value["observed_at"])
    now = datetime.now(UTC)
    if (
        observed < stamp(since)
        or (now - observed).total_seconds() > 120
        or (observed - now).total_seconds() > 2
    ):
        raise RefusalError("gateway chronological bracket differs")
    return value


async def wait_gateway(
    marker: dict[str, Any], original: dict[str, str], phase: str, since: str
) -> dict[str, Any]:
    async with asyncio.timeout(150):
        while True:
            try:
                return gateway(marker, original, phase, since)
            except FileNotFoundError:
                await asyncio.sleep(0.25)


# SQL identifiers/columns below are fixed source text, never request selectors.
# Only public engineering metadata is selected. No request/event/daemon token,
# provider/config/auth fingerprint, controller lifetime or guest output is read.
TABLES: dict[str, tuple[tuple[str, str, int], ...]] = {
    "executions": (
        ("execution_id", "text", 128),
        ("operation_id", "text", 128),
        ("request_digest", "text", 64),
        ("environment_id", "text", 128),
        ("session_id", "text", 128),
        ("terminal", "integer", 0),
        ("kernel_scope", "text", 8),
    ),
    "execution_recovery": (
        ("execution_id", "text", 128),
        ("incarnation", "text", 128),
        ("source_seq", "integer", 0),
        ("cancelled", "integer", 0),
    ),
    "executor_endpoints": (
        ("environment_id", "text", 128),
        ("incarnation", "text", 128),
        ("sandbox", "text", 128),
        ("sandbox_id", "text", 128),
    ),
    "kernels": (
        ("environment_id", "text", 128),
        ("session_id", "text", 128),
        ("language", "text", 8),
        ("incarnation", "text", 128),
        ("base", "integer", 0),
        ("last", "integer", 0),
    ),
    "kernel_start_revisions": (
        ("environment_id", "text", 128),
        ("session_id", "text", 128),
        ("language", "text", 8),
        ("record", "text", 2048),
    ),
    "kernel_package_epochs": (
        ("environment_id", "text", 128),
        ("incarnation", "text", 128),
        ("epoch", "integer", 0),
        ("revision", "integer", 0),
        ("fingerprint", "text", 128),
    ),
    "environment_revisions": (
        ("environment_id", "text", 128),
        ("revision", "integer", 0),
        ("fingerprint", "text", 128),
        ("changed_by", "text", 128),
    ),
    "run_cell_selections": (
        ("execution", "text", 128),
        ("project", "text", 128),
        ("record", "text", 8192),
    ),
    "run_kernel_bindings": (
        ("project", "text", 128),
        ("session", "text", 128),
        ("environment", "text", 128),
        ("parent", "text", 128),
        ("delegation", "text", 128),
        ("child", "text", 128),
        ("operation", "text", 128),
        ("record", "text", 4096),
        ("bytes", "integer", 0),
    ),
    "run_kernel_generations": (
        ("binding", "text", 64),
        ("project", "text", 128),
        ("language", "text", 8),
        ("last", "integer", 0),
    ),
    "run_kernel_births": (
        ("binding", "text", 64),
        ("project", "text", 128),
        ("language", "text", 8),
        ("record", "text", 2048),
    ),
    "run_kernel_controls": (
        ("binding", "text", 64),
        ("project", "text", 128),
        ("operation", "text", 128),
        ("action", "text", 8),
        ("request_sha", "text", 64),
        ("record", "text", 8192),
        ("state", "text", 8),
        ("result", "nullable_text", 4096),
    ),
    "run_kernel_closures": (
        ("binding", "text", 64),
        ("project", "text", 128),
        ("recorded", "text", 40),
    ),
}


def sql_rows(db: Any, table: str, columns: tuple[tuple[str, str, int], ...]) -> list[list[object]]:
    terms = []
    for name, kind, cap in columns:
        if kind == "integer":
            terms.extend((f"typeof({name})", f"CASE WHEN typeof({name})='integer' THEN {name} END"))
        else:
            terms.extend(
                (
                    f"typeof({name})",
                    f"length(CAST({name} AS BLOB))",
                    f"substr(CAST({name} AS BLOB),1,{cap + 1})",
                )
            )
    rows = db.execute(
        "SELECT " + ",".join(terms) + " FROM " + table + " ORDER BY " + columns[0][0] + " LIMIT 65"
    ).fetchall()
    if len(rows) > 64:
        raise RefusalError("SQL row count exceeds fixture cap")
    result = []
    for row in rows:
        out = []
        offset = 0
        for _, kind, cap in columns:
            tag = row[offset]
            if kind == "integer":
                value = row[offset + 1]
                offset += 2
                if tag != "integer" or type(value) is not int or not 0 <= value < 2**62:
                    raise RefusalError("SQL numeric scalar differs")
            else:
                size, raw = row[offset + 1 : offset + 3]
                offset += 3
                if kind == "nullable_text" and tag == "null" and size is None and raw is None:
                    value = None
                else:
                    if tag != "text" or type(size) is not int or not 0 <= size <= cap:
                        raise RefusalError("SQL original text type or byte cap differs")
                    # SQLite substr(empty BLOB) is NULL on pinned SQLite. Preserve
                    # genuine empty TEXT only after original type/length checks.
                    if size == 0 and raw is None:
                        raw = b""
                    if type(raw) is not bytes or len(raw) != size:
                        raise RefusalError("SQL bounded text projection differs")
                    value = raw.decode("utf-8")
                    if "\0" in value:
                        raise RefusalError("SQL text contains NUL")
            out.append(value)
        result.append(out)
    return result


def snapshot(owner: Owner, marker: dict[str, Any], original: dict[str, str]) -> dict[str, object]:
    worker = {name: sql_rows(owner.store._db, name, columns) for name, columns in TABLES.items()}
    # JSON environment document is never exported. A capped selected typed
    # projection excludes providers, annotations, admission hosts and private data.
    db = owner.environments.records._db
    columns = tuple(
        (name, "text", 128)
        for name in ("environment_id", "project_id", "sandbox", "sandbox_id", "state")
    )
    terms = []
    for name in ("project_id", "sandbox", "sandbox_id", "state"):
        terms.append(f"json_extract(document,'$.{name}') AS {name}")
    projected = (
        "SELECT environment_id," + ",".join(terms) + " FROM environments WHERE environment_id=?"
    )
    # json_extract runs only after original JSON document type+size preflight.
    raw = db.execute(
        "SELECT typeof(document),length(CAST(document AS BLOB)) FROM environments WHERE environment_id=?",
        (marker["environment_id"],),
    ).fetchall()
    if (
        len(raw) != 1
        or raw[0][0] != "text"
        or type(raw[0][1]) is not int
        or not 0 < raw[0][1] <= 65536
    ):
        raise RefusalError("environment document type or cap differs")
    environment = sql_rows(
        db,
        "(" + projected.replace("?", "'" + identifier(marker["environment_id"]) + "'") + ")",
        columns,
    )
    if (
        len(environment) != 1
        or environment[0][:4]
        != [original["environment_id"], PROJECT, original["sandbox"], original["sandbox_id"]]
        or environment[0][4] not in ("ready", "stopped", "deleted", "creating")
    ):
        raise RefusalError("environment original binding differs")
    canonical = sql_rows(
        db, "environments", (("environment_id", "text", 128), ("sandbox", "text", 128))
    )
    if canonical != [[original["environment_id"], original["sandbox"]]]:
        raise RefusalError("canonical environment row differs")
    result = {"worker": worker, "environment": environment}
    encoded(result)
    return result


def evidence_names(root: int) -> list[str]:
    names = []
    with os.scandir(root) as entries:
        for entry in entries:
            if len(names) >= 256:
                raise RefusalError("evidence namespace exceeds cap")
            names.append(entry.name)
    return names


def check_pending() -> None:
    root = directory("evidence")
    try:
        names = evidence_names(root)
        for entry in names:
            if entry.startswith("run-kernel-intent-") and entry.endswith(".json"):
                suffix = entry[len("run-kernel-intent-") :]
                if "run-kernel-receipt-" + suffix not in names:
                    raise RefusalError("earlier effect is unknown; reconcile original intent")
    finally:
        os.close(root)


def driver_lock() -> int:
    root = directory(CONTROL)
    try:
        fd = os.open(
            "driver.lock",
            os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
            0o600,
            dir_fd=root,
        )
        try:
            info = os.fstat(fd)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.getuid()
                or info.st_nlink != 1
                or stat.S_IMODE(info.st_mode) != 0o600
                or info.st_size != 0
            ):
                raise RefusalError("driver lock ownership differs")
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return fd
        except BaseException:
            os.close(fd)
            raise
    finally:
        os.close(root)


class Driver:
    def __init__(self, marker: dict[str, Any]) -> None:
        from sandbox_science.orchestrator.delegation_records import DelegationRecords
        from sandbox_science.orchestrator.documents.store import DocumentStore
        from sandbox_science.orchestrator.projects import ProjectStore

        self.marker = marker
        self.projects = ProjectStore(LAB / CONTROL)
        self.owner = Owner()
        self.bindings: dict[str, Any] = {}
        self.lifetimes: dict[str, str] = {}
        self.control_closed = False
        self.active = True
        self.cells = 0
        self.controls = 0
        self.original: dict[str, str] = {}
        self.started = time.monotonic()
        self.documents = DocumentStore(
            LAB / CONTROL / "control.sqlite3", producer="lab33-synthetic-host"
        )
        try:
            self.records = DelegationRecords(self.documents)
            for session in SESSIONS:
                held = self.projects.session_snapshot(PROJECT, session)
                try:
                    lifetime = held.session.kernel_notice_lifetime
                    if lifetime is None:
                        raise RefusalError("canonical session lifetime is unknown")
                    self.lifetimes[session] = lifetime
                finally:
                    held.close()
        except BaseException:
            self.documents.close()
            self.control_closed = True
            raise

    def withdraw(self) -> None:
        self.active = False

    def context(self, session: str) -> Any:
        from sandbox_science.contracts.context import ExecutionContext

        return ExecutionContext(
            project_id=PROJECT,
            environment_id=self.marker["environment_id"],
            session_id=session,
            policy_revision=self.marker["policy"],
        )

    def admitted(self, label: str) -> bool:
        if not self.active or time.monotonic() - self.started >= WHOLE_SECONDS:
            return False
        from sandbox_science.orchestrator.delegation_records import DelegationScope, ParentAuthority

        child = self.marker["children"][label]
        session = child["session_id"]
        held = self.projects.session_snapshot(PROJECT, session)
        try:
            if (
                held.context.environment_id != self.marker["environment_id"]
                or held.session.kernel_notice_lifetime != self.lifetimes[session]
            ):
                return False
        finally:
            held.close()
        authority = ParentAuthority(
            scope=DelegationScope(
                project_id=PROJECT, session_id=session, environment_id=self.marker["environment_id"]
            ),
            parent_run_id=child["parent_run_id"],
        )
        saved = self.records.get(authority, child["delegation_id"])
        return (
            saved.outcome is None
            and saved.prepared.child_run_id == child["child_run_id"]
            and saved.prepared.request_sha256 == child["request_sha256"]
        )

    async def effect(
        self, name: str, kind: str, work: Callable[[], Any], projected: Callable[[Any], object]
    ) -> Any:
        # Every earlier intent needs its immutable receipt before a new effect.
        identifier(name)
        identifier(kind)
        check_pending()
        root = directory("evidence")
        try:
            names = evidence_names(root)
            for entry in names:
                if entry.startswith("run-kernel-intent-") and entry.endswith(".json"):
                    suffix = entry[len("run-kernel-intent-") :]
                    if "run-kernel-receipt-" + suffix not in names:
                        raise RefusalError("earlier effect is unknown; reconcile original intent")
        finally:
            os.close(root)
        write_new(
            PREFIX + "intent-" + name + ".json",
            {**envelope(self.marker, name), "kind": kind, "operation_id": "lab33-" + name},
        )
        async with asyncio.timeout(180):
            result, interrupted = await owned(work(), self.withdraw)
        write_new(
            PREFIX + "receipt-" + name + ".json",
            {**envelope(self.marker, name), "kind": kind, "result": projected(result)},
        )
        if interrupted is not None:
            raise interrupted
        return result

    def execution(self, value: Any) -> dict[str, object]:
        execution = value.execution if hasattr(value, "execution") else value
        context = execution.context
        if (
            context.project_id != PROJECT
            or context.environment_id != self.marker["environment_id"]
            or context.session_id not in SESSIONS
        ):
            raise RefusalError("execution scope differs")
        return {
            "execution_id": identifier(execution.execution_id),
            "attempt_id": identifier(execution.attempt_id),
            "operation_id": identifier(execution.operation_id),
            "session_id": context.session_id,
            "state": execution.state.value,
            "last_seq": execution.last_seq,
            "kernel": execution.kernel.model_dump(mode="json") if execution.kernel else None,
            "result_kernel": execution.result.kernel.model_dump(mode="json")
            if execution.result and execution.result.kernel
            else None,
            "state_lost": execution.result.state_lost if execution.result else None,
        }

    async def cell(
        self,
        name: str,
        label: str | None,
        code: str,
        expected: dict[str, object] | None,
        session: str = SESSIONS[0],
        *,
        cancel_target: bool = False,
    ) -> Any:
        from sandbox_science.contracts.run_cells import RunCellRequest
        from sandbox_science.contracts.worker import CellRequest

        if type(code) is not str or len(code.encode("utf-8")) > 512 or self.cells >= MAX_CELLS:
            raise RefusalError("fixed cell budget differs")
        self.cells += 1
        context = self.context(self.marker["children"][label]["session_id"] if label else session)

        async def submit() -> Any:
            if label:
                result, new = await self.owner.manager.run_dispatcher.submit(
                    RunCellRequest(
                        operation_id="lab33-" + name,
                        context=context,
                        binding=self.bindings[label],
                        code=code,
                        timeout_seconds=45.0,
                        output_limit_bytes=1024,
                    ),
                    admitted=partial(self.admitted, label),
                )
            else:
                result, new = await self.owner.manager.submit(
                    CellRequest(
                        operation_id="lab33-" + name,
                        context=context,
                        code=code,
                        timeout_seconds=45.0,
                        output_limit_bytes=1024,
                    )
                )
            if not new:
                raise RefusalError("fixed cell was already admitted")
            return result

        selected = await self.effect(
            name, "child-cell" if label else "main-cell", submit, self.execution
        )
        original = selected.execution if label else selected
        if cancel_target:
            async with asyncio.timeout(20):
                while True:
                    current = self.owner.manager.get(original.execution_id)
                    if current.state.value == "running" and current.kernel is not None:
                        break
                    if current.state.value in TERMINAL:
                        raise RefusalError("cancel target ended before original started proof")
                    await asyncio.sleep(0.05)
            return original
        async with asyncio.timeout(60):
            result = await self.owner.manager.wait(original.execution_id)
        if (
            result.state.value != "succeeded"
            or result.result is None
            or result.result.status != "ok"
            or (result.result.stderr is not None and result.result.stderr.text)
        ):
            raise RefusalError("fixed cell did not succeed")
        stream = result.result.stdout
        if (
            stream is None
            or type(stream.text) is not str
            or len(stream.text) > 1024
            or stream.truncated
            or stream.total_bytes > 1024
        ):
            raise RefusalError("fixed cell output differs")
        raw = stream.text.encode("utf-8")
        if stream.total_bytes != len(raw):
            raise RefusalError("fixed output byte count differs")
        output = decode(raw, 1024)
        if (
            type(output) is not dict
            or type(output.get("pid")) is not int
            or not 1 <= output["pid"] < 2**31
            or output.get("cwd_ok") is not True
        ):
            raise RefusalError("fixed scalar output shape differs")
        wanted = expected or {}
        if set(output) != {"pid", "cwd_ok", *wanted} or any(
            type(output[key]) is not type(value) or output[key] != value
            for key, value in wanted.items()
        ):
            raise RefusalError("fixed scalar/output witness differs")
        safe = {**self.execution(result), "fixture_output": output}
        write_new(
            PREFIX + "cell-" + name + ".json", {**envelope(self.marker, name), "result": safe}
        )
        return safe

    async def control(self, name: str, label: str, action: str, target: Any = None) -> Any:
        if type(action) is not str or action not in ("cancel", "restart", "close"):
            raise RefusalError("fixed child control differs")
        if self.controls >= MAX_CONTROLS:
            raise RefusalError("control budget differs")
        self.controls += 1
        dispatcher = self.owner.manager.run_dispatcher

        async def actual() -> Any:
            if action == "cancel":
                return await dispatcher.cancel(
                    self.bindings[label],
                    target.execution_id,
                    target.attempt_id,
                    admitted=partial(self.admitted, label),
                )
            if action == "restart":
                return await dispatcher.restart(
                    self.bindings[label],
                    "lab33-" + name,
                    "python",
                    admitted=partial(self.admitted, label),
                )
            return await dispatcher.close(
                self.bindings[label], "lab33-" + name, admitted=partial(self.admitted, label)
            )

        def project(value: Any) -> object:
            return self.execution(value) if action == "cancel" else value.model_dump(mode="json")

        receipt = await self.effect(name, "child-" + action, actual, project)
        if action != "cancel" and receipt.state != "done":
            raise RefusalError("control effect remains unknown")
        return receipt

    async def refused(self, name: str, label: str) -> None:
        from sandbox_science.contracts.run_cells import RunCellRequest
        from sandbox_science.worker.errors import WorkerError

        before = self.owner.store._db.execute("SELECT count(*) FROM executions").fetchone()[0]
        try:
            await self.owner.manager.run_dispatcher.submit(
                RunCellRequest(
                    operation_id="lab33-refused-" + name,
                    context=self.context(self.marker["children"][label]["session_id"]),
                    binding=self.bindings[label],
                    code="print('must not execute')",
                    timeout_seconds=5.0,
                    output_limit_bytes=128,
                ),
                admitted=partial(self.admitted, label),
            )
        except WorkerError as error:
            if error.kind.value not in ERRORS:
                raise RefusalError("refusal error enum differs") from None
            kind = error.kind.value
        else:
            raise RefusalError("old closed/original binding admitted a new effect")
        after = self.owner.store._db.execute("SELECT count(*) FROM executions").fetchone()[0]
        if before != after:
            raise RefusalError("refused selection created an execution")
        write_new(
            PREFIX + "refused-" + name + ".json",
            {
                **envelope(self.marker, name),
                "error_kind": kind,
                "execution_count_before": before,
                "execution_count_after": after,
            },
        )

    async def observations(self, name: str, labels: tuple[str, ...]) -> dict[str, object]:
        result = {}
        for label in labels:
            if label == "MAIN":
                from sandbox_science.contracts.worker import KernelStatusRequest

                value = await self.owner.manager.observe_kernel_lineage(
                    KernelStatusRequest(context=self.context(SESSIONS[0]))
                )
            else:
                value = await self.owner.manager.run_dispatcher.observe(self.bindings[label])
            if (
                len(value.kernels) != 1
                or value.kernels[0].lineage_status != "proved"
                or value.kernels[0].birth is None
            ):
                raise RefusalError("current child lineage is not exactly proved")
            result[label] = value.model_dump(mode="json")
        write_new(PREFIX + name + ".json", {**envelope(self.marker, name), "observations": result})
        return result

    def control_snapshot(self) -> dict[str, object]:
        if self.documents._db is None:
            raise RefusalError("existing canonical control connection is absent")
        columns = (
            ("project", "text", 128),
            ("session", "text", 128),
            ("environment", "text", 128),
            ("parent", "text", 128),
            ("sequence", "integer", 0),
            ("action", "text", 8),
            ("delegation", "text", 128),
            ("child", "text", 128),
            ("operation", "text", 128),
            ("request_sha", "text", 64),
            ("bytes", "integer", 0),
        )
        rows = sql_rows(self.documents._db, "delegation_record_events", columns)
        if len(rows) != 3 or any(
            row[0] != PROJECT
            or row[1] not in SESSIONS
            or row[2] != self.marker["environment_id"]
            or row[5] != "prepare"
            for row in rows
        ):
            raise RefusalError("canonical prepared control rows differ")
        value = decode(bounded(CONTROL + "/projects/" + PROJECT + ".json", 65536), 65536)
        if (
            type(value) is not dict
            or value.get("project_id") != PROJECT
            or value.get("default_environment_id") != self.marker["environment_id"]
            or type(value.get("sessions")) is not dict
            or set(value["sessions"]) != set(SESSIONS)
        ):
            raise RefusalError("canonical project scope differs")
        public = []
        for name in SESSIONS:
            session = value["sessions"][name]
            if (
                type(session) is not dict
                or session.get("session_id") != name
                or session.get("environment_id") != self.marker["environment_id"]
                or session.get("dedicated") is not False
            ):
                raise RefusalError("canonical session scope differs")
            stamp(session.get("opened_at"))
            closed = session.get("closed_at")
            if closed is not None:
                stamp(closed)
            if (name == SESSIONS[0]) != (closed is not None):
                raise RefusalError("canonical session lifecycle differs")
            public.append(
                {
                    "session_id": name,
                    "environment_id": session["environment_id"],
                    "dedicated": False,
                    "opened_at": session["opened_at"],
                    "closed_at": closed,
                }
            )
        # Opaque session lifetime and all raw project bytes are intentionally
        # excluded from public serialization and hashes.
        return {"delegation_headers": rows, "project_id": PROJECT, "sessions": public}

    async def stopped(self) -> None:
        environment_id = self.marker["environment_id"]
        current = self.owner.manager._environments.get(environment_id)
        if current is None or current.requests != 0:
            raise RefusalError("idle owner is not settled")
        idle_since = current.last_used
        await asyncio.sleep(0.05)
        result = await self.effect(
            "suspend",
            "environment-suspend",
            lambda: self.owner.environments.suspend(environment_id, idle_since),
            lambda value: {"suspended": value},
        )
        if result is not True:
            raise RefusalError("normal idle suspend did not succeed")
        original = {
            "environment_id": self.original["environment_id"],
            "name": self.original["sandbox"],
            "id": self.original["sandbox_id"],
        }
        ready = {
            **envelope(self.marker, "stopped-ready"),
            "stage": "stopped-ready",
            "original": original,
        }
        ready.pop("phase")
        write_new(PREFIX + "stopped-ready.json", ready)
        before_proof = await wait_gateway(
            self.marker, self.original, "before", str(ready["recorded_at"])
        )
        before = snapshot(self.owner, self.marker, self.original)
        before["control"] = self.control_snapshot()
        if before["worker"]["executor_endpoints"] != [] or before["environment"][0][4] != "stopped":
            raise RefusalError("normal endpoint retirement is not proved")
        old_owner = self.owner
        activity = current.last_used
        if self.owner.manager._environments.get(environment_id) is not None:
            raise RefusalError("retired managed owner remains installed")
        counts = dict.fromkeys(
            (
                "SELECT",
                "BEGIN",
                "COMMIT",
                "ROLLBACK",
                "PRAGMA",
                "INSERT",
                "UPDATE",
                "DELETE",
                "CREATE",
                "ALTER",
                "DROP",
                "REPLACE",
                "OTHER",
            ),
            0,
        )

        def trace(sql: str) -> None:
            # SQL/parameters are ephemeral: retain ONLY a bounded fixed verb count.
            prefix = sql[:64].lstrip()
            head = prefix.split(None, 1)[0].upper() if prefix else "OTHER"
            verb = head if head in counts else "OTHER"
            counts[verb] += 1
            if sum(counts.values()) > 10000:
                self.withdraw()

        connections = (
            self.owner.store._db,
            self.owner.environments.records._db,
            self.documents._db,
        )
        attached = []
        first = utc()
        reads = []
        try:
            for connection in connections:
                connection.set_trace_callback(trace)
                attached.append(connection)
            from sandbox_science.worker.errors import WorkerError

            for index in range(30):
                try:
                    await self.owner.manager.run_dispatcher.observe(self.bindings["C"])
                except WorkerError as error:
                    if error.kind.value != "unavailable":
                        raise RefusalError("stopped observation classification differs") from None
                else:
                    raise RefusalError("stopped observer unexpectedly returned readable kernels")
                reads.append({"read": index + 1, "error_kind": "unavailable"})
            last = utc()
        finally:
            for connection in attached:
                connection.set_trace_callback(None)
        after = snapshot(self.owner, self.marker, self.original)
        after["control"] = self.control_snapshot()
        if (
            self.owner is not old_owner
            or self.owner.manager._environments.get(environment_id) is not None
            or current.last_used != activity
            or current.requests != 0
            or (current.channel is not None and not current.channel.closed)
            or before != after
        ):
            raise RefusalError("passive stopped reads changed owner/activity/retained state")
        if any(
            counts[verb] for verb in counts if verb not in ("SELECT", "BEGIN", "COMMIT", "ROLLBACK")
        ):
            raise RefusalError("passive bracket observed non-read SQL")
        done = {
            **envelope(self.marker, "stopped-reads-done"),
            "stage": "stopped-reads-done",
            "original": original,
            "reads": 30,
            "first_read_at": first,
            "last_read_at": last,
        }
        done.pop("phase")
        write_new(PREFIX + "stopped-reads-done.json", done)
        after_proof = await wait_gateway(
            self.marker, self.original, "after", str(done["recorded_at"])
        )
        if stamp(before_proof["observed_at"]) > stamp(first) or stamp(
            after_proof["observed_at"]
        ) < stamp(last):
            raise RefusalError("gateway proofs do not bracket all reads")
        write_new(
            PREFIX + "stopped-result.json",
            {
                **envelope(self.marker, "stopped-result"),
                "before_gateway": before_proof,
                "after_gateway": after_proof,
                "before": before,
                "after": after,
                "reads": reads,
                "trace_verb_counts": counts,
                "no_sql_writes_observed": True,
                "owner_restarted_inside_bracket": False,
            },
        )

    async def run(self) -> dict[str, object]:
        from sandbox_science.contracts.environments import EnvironmentRequest
        from sandbox_science.contracts.run_kernels import RunKernelScope
        from sandbox_science.contracts.worker import SessionCloseRequest

        await self.owner.open(marker=self.marker)

        async def create() -> Any:
            value, new = await self.owner.environments.create(
                EnvironmentRequest(
                    environment_id=self.marker["environment_id"],
                    project_id=PROJECT,
                    profile="minimal",
                    policy=self.marker["policy"],
                )
            )
            if not new or value.sandbox_id is None:
                raise RefusalError("managed environment was not freshly created")
            return value

        def original(value: Any) -> dict[str, str]:
            if value.project_id != PROJECT or value.environment_id != self.marker["environment_id"]:
                raise RefusalError("created environment scope differs")
            return {
                "environment_id": identifier(value.environment_id),
                "sandbox": identifier(value.sandbox),
                "sandbox_id": identifier(value.sandbox_id),
            }

        created = await self.effect("environment", "environment-create", create, original)
        self.original = original(created)
        write_new(
            PREFIX + "original.json",
            {
                **envelope(self.marker, "original-created"),
                "stage": "original-created",
                "original": {
                    "environment_id": self.original["environment_id"],
                    "name": self.original["sandbox"],
                    "id": self.original["sandbox_id"],
                },
            },
        )
        prefix = "import os,json\n"

        def code(value: str, *, assign: str = "", shared: bool = False, write: bool = False) -> str:
            body = prefix + assign
            if write:
                body += "open(os.environ['PROJECT_SHARED']+'/lab33-shared.txt','w').write('lab33 shared\\n')\n"
            body += (
                "print(json.dumps({'value':"
                + value
                + ",'pid':os.getpid(),'cwd_ok':os.getcwd()==os.environ['SESSION_WORKSPACE']"
            )
            if shared:
                body += ",'shared':open(os.environ['PROJECT_SHARED']+'/lab33-shared.txt').read()"
            return body + "}))"

        main = await self.cell(
            "01-main", None, code("lab33_value", assign="lab33_value=111\n"), {"value": 111}
        )
        for label in ("A", "B", "C"):
            child = self.marker["children"][label]
            if not self.admitted(label):
                raise RefusalError("canonical prepared child admission differs")
            scope = RunKernelScope(
                project_id=PROJECT,
                session_id=child["session_id"],
                environment_id=self.marker["environment_id"],
                parent_run_id=child["parent_run_id"],
                delegation_id=child["delegation_id"],
                child_run_id=child["child_run_id"],
            )
            # S2 uses same existing managed channel; no synthetic session alias.
            receipt = self.owner.manager.enroll_run_kernel(
                scope, "lab33-enroll-" + label.lower(), initialize=label == "A"
            )
            self.bindings[label] = receipt.binding
            write_new(
                PREFIX + "enrollment-" + label.lower() + ".json",
                {
                    **envelope(self.marker, "enrollment-" + label.lower()),
                    "receipt": receipt.model_dump(mode="json"),
                },
            )
        self.owner.manager.run_dispatcher.initialize()
        a = await self.cell(
            "02-a", "A", code("lab33_value", assign="lab33_value=222\n", write=True), {"value": 222}
        )
        b = await self.cell(
            "03-b",
            "B",
            code("lab33_value", assign="lab33_value=333\n", shared=True),
            {"value": 333, "shared": SHARED_TEXT},
        )
        if (
            len(
                {
                    main["fixture_output"]["pid"],
                    a["fixture_output"]["pid"],
                    b["fixture_output"]["pid"],
                }
            )
            != 3
        ):
            raise RefusalError("simultaneously live MAIN/A/B kernel processes are not distinct")
        await self.cell(
            "04-main-shared",
            None,
            code("lab33_value", shared=True),
            {"value": 111, "shared": SHARED_TEXT},
        )
        pilot = await self.observations("pilot", ("A", "B", "MAIN"))
        target = await self.cell(
            "05-a-sleep",
            "A",
            "import time\nprint('lab33-started',flush=True)\ntime.sleep(30)",
            None,
            cancel_target=True,
        )
        await self.control("cancel-a", "A", "cancel", target)
        async with asyncio.timeout(60):
            cancelled = await self.owner.manager.wait(target.execution_id)
        if cancelled.state.value != "cancelled" or cancelled.attempt_id != target.attempt_id:
            raise RefusalError("exact original cancel did not terminate its attempt")
        write_new(
            PREFIX + "cancelled.json",
            {**envelope(self.marker, "cancelled"), "execution": self.execution(cancelled)},
        )
        await self.cell("06-b-preserved", "B", code("lab33_value"), {"value": 333})
        await self.cell("07-main-preserved", None, code("lab33_value"), {"value": 111})
        await self.control("restart-a", "A", "restart")
        await self.cell("08-a-reset", "A", code("globals().get('lab33_value')"), {"value": None})
        await self.cell("09-b-preserved", "B", code("lab33_value"), {"value": 333})
        await self.cell("10-main-preserved", None, code("lab33_value"), {"value": 111})
        restarted = await self.observations("restarted", ("A", "B", "MAIN"))
        before_a = pilot["A"]["kernels"][0]
        after_a = restarted["A"]["kernels"][0]
        if (
            before_a["birth"]["birth_id"] == after_a["birth"]["birth_id"]
            or before_a["kernel"]["generation"] >= after_a["kernel"]["generation"]
        ):
            raise RefusalError("explicit child restart did not establish a new birth/generation")
        before_reconnect = await self.observations("before-reconnect", ("A", "B", "MAIN"))

        async def reconnect() -> Any:
            await self.owner.close()
            if not self.owner.closed:
                raise RefusalError("original host owner did not fully drain")
            self.owner = Owner()
            await self.owner.open(marker=self.marker, reopened=True)
            return await self.observations("after-reconnect", ("A", "B", "MAIN"))

        after_reconnect = await self.effect(
            "reconnect-owner", "host-owner-reconnect", reconnect, lambda value: value
        )

        def pins(observation: Any) -> dict[str, object]:
            return {
                key: {
                    "binding": value.get("binding"),
                    "kernels": value["kernels"],
                    "current_revision": value["current_revision"],
                }
                for key, value in observation.items()
            }

        if pins(before_reconnect) != pins(after_reconnect):
            raise RefusalError("same-incarnation owner reconnect changed original child pins")
        await self.cell("11-a-reset", "A", code("globals().get('lab33_value')"), {"value": None})
        await self.cell("12-b-preserved", "B", code("lab33_value"), {"value": 333})
        await self.cell("13-main-preserved", None, code("lab33_value"), {"value": 111})
        await self.control("close-a", "A", "close")
        await self.refused("closed-a", "A")
        await self.cell("14-b-preserved", "B", code("lab33_value"), {"value": 333})
        await self.cell("15-main-preserved", None, code("lab33_value"), {"value": 111})
        await self.cell(
            "16-main-s2",
            None,
            code("lab33_value", assign="lab33_value=555\n"),
            {"value": 555},
            SESSIONS[1],
        )
        await self.cell(
            "17-c",
            "C",
            code("lab33_value", assign="lab33_value=444\n", shared=True),
            {"value": 444, "shared": SHARED_TEXT},
        )
        closed = await self.effect(
            "close-session1",
            "session-close",
            lambda: self.owner.manager.close_session(
                SessionCloseRequest(context=self.context(SESSIONS[0]))
            ),
            lambda value: {"kernels": [item.model_dump(mode="json") for item in value.kernels]},
        )
        from sandbox_science.contracts.worker import KernelStatusRequest

        stopped_b = await self.owner.manager.run_dispatcher.observe(self.bindings["B"])
        stopped_main = await self.owner.manager.observe_kernel_lineage(
            KernelStatusRequest(context=self.context(SESSIONS[0]))
        )
        if stopped_b.kernels or stopped_main.kernels:
            raise RefusalError("session1 close left MAIN or selected B kernels visible")
        write_new(
            PREFIX + "session1-closed-observations.json",
            {
                **envelope(self.marker, "session1-closed"),
                "B": stopped_b.model_dump(mode="json"),
                "MAIN": stopped_main.model_dump(mode="json"),
            },
        )
        self.projects.close_session(PROJECT, SESSIONS[0])
        if not closed.kernels:
            raise RefusalError("session close did not report its ordinary MAIN kernel")
        await self.cell("18-c-preserved", "C", code("lab33_value"), {"value": 444})
        await self.observations("session2-preserved", ("C",))
        await self.stopped()
        resumed = await self.cell(
            "19-main-resume",
            None,
            code("globals().get('lab33_value')"),
            {"value": None},
            SESSIONS[1],
        )
        await self.refused("old-incarnation-c", "C")
        endpoint = self.owner.store.lineage_endpoint(self.marker["environment_id"])
        if (
            endpoint is None
            or endpoint[0] == self.bindings["C"].incarnation
            or endpoint[1:] != (self.original["sandbox"], self.original["sandbox_id"])
        ):
            raise RefusalError("ordinary resume did not establish a new daemon incarnation")
        now_info = self.owner.environments.get(self.marker["environment_id"])
        if (now_info.sandbox, now_info.sandbox_id) != (
            self.original["sandbox"],
            self.original["sandbox_id"],
        ):
            raise RefusalError("ordinary resume replaced the original VM")

        def deleted_original(value: Any) -> dict[str, str]:
            if (value.environment_id, value.sandbox, value.sandbox_id, value.state.value) != (
                self.original["environment_id"],
                self.original["sandbox"],
                self.original["sandbox_id"],
                "deleted",
            ):
                raise RefusalError("delete receipt differs from original VM loss")
            return {
                "environment_id": value.environment_id,
                "sandbox": value.sandbox,
                "sandbox_id": value.sandbox_id,
                "state": "deleted",
            }

        await self.effect(
            "delete-original",
            "environment-delete",
            lambda: self.owner.environments.delete(self.marker["environment_id"]),
            deleted_original,
        )
        await self.refused("original-vm-loss-c", "C")
        result = {
            **envelope(self.marker, "completed"),
            "status": "PASS",
            "cells": self.cells,
            "child_controls": self.controls,
            "session_closes": 1,
            "original": self.original,
            "resumed_execution": self.execution(resumed)
            if hasattr(resumed, "execution")
            else {"execution_id": resumed["execution_id"], "attempt_id": resumed["attempt_id"]},
            "synthetic_host_admission": True,
            "live_controller_model_launcher_verified": False,
            "security_isolation_verified": False,
        }
        write_new(PREFIX + "completed.json", result)
        return result

    async def close(self) -> None:
        try:
            await self.owner.close()
        finally:
            if self.owner.closed:
                self.documents.close()
                self.control_closed = True


async def run_fixture(head: str) -> dict[str, object]:
    marker = marked(head)
    lock = driver_lock()
    try:
        check_pending()
        root = directory("evidence")
        try:
            if any(name.startswith("run-kernel-intent-") for name in evidence_names(root)):
                raise RefusalError("driver has already started; inspect original immutable effects")
        finally:
            os.close(root)
        driver = Driver(marker)
    except BaseException:
        os.close(lock)
        raise
    task = asyncio.current_task()
    loop = asyncio.get_running_loop()

    def stop() -> None:
        driver.withdraw()
        if task is not None:
            task.cancel()

    installed = []
    try:
        for signum in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(signum, stop)
            installed.append(signum)
        async with asyncio.timeout(WHOLE_SECONDS):
            return await driver.run()
    finally:
        driver.withdraw()
        try:
            await driver.close()
        finally:
            try:
                write_new(
                    PREFIX + "host-cleanup.json",
                    {
                        **envelope(marker, "host-cleanup"),
                        "owner_closed": driver.owner.closed,
                        "control_store_closed": driver.control_closed,
                        "cleanup": driver.owner.cleanup,
                    },
                )
            finally:
                for signum in installed:
                    loop.remove_signal_handler(signum)
                os.close(lock)


def inspect(head: str) -> dict[str, object]:
    marker = marked(head)
    original_record = decode(bounded(PREFIX + "original.json", 8192), 8192)
    original_value = original_record.get("original") if type(original_record) is dict else None
    if type(original_value) is not dict or set(original_value) != {"environment_id", "name", "id"}:
        raise RefusalError("original created identity is absent")
    original = {
        "environment_id": identifier(original_value["environment_id"]),
        "sandbox": identifier(original_value["name"]),
        "sandbox_id": identifier(original_value["id"]),
    }
    if original["environment_id"] != marker["environment_id"] or any(
        original_record.get(key) != marker[key]
        for key in ("source_head", "helper_sha256", "run_id", "project_id")
    ):
        raise RefusalError("original created receipt scope differs")
    connections = []
    held = []
    identities = []
    try:
        for relative in ("worker/state/worker.sqlite3", "worker/state/environments.sqlite3"):
            parent, _, name = relative.rpartition("/")
            root = directory(parent)
            held.append(root)
            fd = os.open(
                name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=root
            )
            held.append(fd)
            before = os.fstat(fd)
            if (
                not stat.S_ISREG(before.st_mode)
                or before.st_uid != os.getuid()
                or before.st_nlink != 1
                or not 0 < before.st_size <= 32 * 1024 * 1024
            ):
                raise RefusalError("readonly store ownership/cap differs")
            path = LAB / relative
            db = sqlite3.connect(
                path.as_uri() + "?mode=ro", uri=True, timeout=0, isolation_level=None
            )
            connections.append(db)
            identities.append((root, name, before.st_dev, before.st_ino))
            now = os.stat(name, dir_fd=root, follow_symlinks=False)
            if (now.st_dev, now.st_ino) != (before.st_dev, before.st_ino):
                raise RefusalError("readonly store binding changed")
        owner = SimpleNamespace(
            store=SimpleNamespace(_db=connections[0]),
            environments=SimpleNamespace(records=SimpleNamespace(_db=connections[1])),
        )
        result = snapshot(owner, marker, original)
        for root, name, dev, ino in identities:
            now = os.stat(name, dir_fd=root, follow_symlinks=False)
            if not stat.S_ISREG(now.st_mode) or (now.st_dev, now.st_ino) != (dev, ino):
                raise RefusalError("readonly store pathname changed during inspection")
        return {
            **envelope(marker, "inspect"),
            "scope": "retained metadata only; no startup/schema/RPC/new effects",
            "snapshot": result,
        }
    finally:
        for db in connections:
            db.close()
        for fd in reversed(held):
            os.close(fd)


def describe() -> dict[str, object]:
    return {
        "helper": "Lab33 internal child-kernel dispatcher",
        "admission_ready": ADMISSION_READY,
        "default": "describe-only; no application import, configuration, model, worker or VM effect",
        "scope": "synthetic host admission with canonical prepared children; no live controller/launcher/model/grant proof",
        "commands": [
            "describe",
            "prepare --head EXACT --admit",
            "run --head EXACT --admit",
            "inspect --head EXACT --admit",
        ],
        "limits": {
            "cells": MAX_CELLS,
            "controls": MAX_CONTROLS,
            "session_closes": 1,
            "whole_seconds": WHOLE_SECONDS,
        },
        "runtime": "not performed by author; root admission and complete teardown required",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "stage", choices=("describe", "prepare", "run", "inspect"), nargs="?", default="describe"
    )
    parser.add_argument("--head")
    parser.add_argument("--admit", action="store_true")
    args = parser.parse_args()
    if args.stage == "describe":
        print(encoded(describe()).decode("ascii"), end="")
        return 0
    if not args.admit or not args.head:
        print('{"status":"REFUSED","reason":"explicit root admission and exact head required"}')
        return 2
    try:
        result = (
            prepare(args.head)
            if args.stage == "prepare"
            else inspect(args.head)
            if args.stage == "inspect"
            else asyncio.run(run_fixture(args.head))
        )
        print(encoded(result).decode("ascii"), end="")
        return 0
    except BaseException as error:
        # Never stringify exceptions: configuration/provider/guest text is private.
        kind = "cancelled" if isinstance(error, asyncio.CancelledError) else "held_unknown"
        print(
            json.dumps({"status": "HELD", "error_kind": kind, "reconcile_original_intents": True})
        )
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
