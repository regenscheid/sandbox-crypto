# SPDX-License-Identifier: MIT
# ruff: noqa: S101, S603, S607, S608
"""Inert-by-default root-owned Lab35 CP16 observation acceptance.

Only the explicitly admitted root runtime may assemble apps/read private settings
or create/install/delete. This is not a provider/model or operator approval path.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import math
import os
import re
import secrets
import select
import stat
import subprocess
import time
import tomllib
from collections.abc import Callable
from contextlib import suppress
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from typing import Any

SOURCE = Path(__file__).parents[2]
LAB = Path.home() / ".local/state/openshell-lab"
PROJECT = "lab35"
SESSION = "lab35-session"
ENVIRONMENT = "lab35-environment"
PREFIX = "evidence/provisioning-observations-"
MAX_BYTES = 65536
ADMISSION_READY = False
WHOLE_SECONDS = 1800.0
MAX_EXECUTIONS = 16
MAX_NEW_OPERATIONS = 2
MAX_INSTALLS = 2
MAX_REPLAYS = 1
COHERENT_PRODUCT_HEAD = "78a8d5a2db29397979b3f206da263603d83eeafe"
# Root separately proves the complete772-input union at adoption/admission.
INPUTS = {
    "packages/contracts/src/sandbox_science/contracts/provisioning.py": "a3b97b403ac25ac69e1f276e67395780d02264e000259fdcda94cb3a603d20ae",
    "packages/contracts/src/sandbox_science/contracts/worker.py": "dee0d24827e53dd16fb625c4ba5320377fe32ed410fe40ac771e93c09e12a21e",
    "packages/contracts/src/sandbox_science/contracts/environments.py": "77f70badb5586a1dedf9ff940b04187057fecf14980c459fe69a1db71c6b4d67",
    "packages/contracts/src/sandbox_science/contracts/artifacts.py": "a2ffb6d66d71b32b2189642f21441f98addd4652d615036c437aa02d924939bf",
    "packages/orchestrator/src/sandbox_science/orchestrator/worker_client.py": "9341776645a11a91d93eb73aefc6663c5bd6282bb9515286275a308bbe39c182",
    "packages/worker/src/sandbox_science/worker/app.py": "979334aae75d799568247ac4f5036aaa4dd891b372e918bd66236d7eb4d71f9a",
    "packages/worker/src/sandbox_science/worker/cli.py": "dda5982432fec71ebb99bc942331f0bae531f0e573c2e1e7ae7143c6164838a9",
    "packages/worker/src/sandbox_science/worker/settings.py": "2d820c019485e847d94f9d6141a5889ad81849f5baef3c3714aa7420139a61fd",
    "packages/worker/src/sandbox_science/worker/store.py": "32baaa6f897ea2bf419386660071b05b743fb97e988e3ef7c80a5d0d6e81b6aa",
    "packages/worker/src/sandbox_science/worker/manager.py": "fe28a4fe1f62784b4032e7d1c09eace06a80301ba02915b3d20c8f455e68ed6b",
    "packages/worker/src/sandbox_science/worker/backends.py": "7f22eee4dd47663bc62da668df03f6cb02e01a5a7a5fafebd52d10dc506f4fbf",
    "packages/worker/src/sandbox_science/worker/environments.py": "9187c2d0c840ac888a5664a7bdaa04f22e9c8da76bee69fc85f61b6de116c7fc",
    "packages/worker/src/sandbox_science/worker/provisioner.py": "16dcd0dddadfb5dd642e7f0ad0bff53b686adff6b51888c0164dbd5e057aa173",
    "packages/worker/src/sandbox_science/worker/installer_records.py": "c993932a5731cb0db37e0fe44484436878fb0faa87ea72ad1ab6e3b9a8a70c8b",
    "packages/worker/src/sandbox_science/worker/provision_events.py": "7e2c698a80902f8509166ac2b1ddaf4d3b056f91ce66c4045f6b77ad5ef71566",
    "packages/worker/src/sandbox_science/worker/permissions.py": "65c8b780f75763d16a97c7d96a102f10a00c48801f190dde42430712d43fd09c",
    "packages/worker/src/sandbox_science/worker/artifacts.py": "54fdc1fa9ba3ae34946544c4bf490b783725bc6be3fc7971c2a0cc282320c661",
    "packages/worker/src/sandbox_science/worker/resolution.py": "865eb35a27968c89400a06ae595f1224274edd64dd73eea9c6bf2ae4a957d4ca",
    "packages/worker/src/sandbox_science/worker/provisioning.py": "a60b6fcbcbb6ed1d53ddb8ba5a8b36130b7641a619c48630048ad894d6e37b1d",
    "packages/worker/src/sandbox_science/worker/revisions.py": "184a929c383867dfefa78bd15beba556273d733bf3e4e0d69c6f655562a9f50b",
    "packages/worker/src/sandbox_science/worker/catalog.py": "be4d82aada1c935dc4f539aeb0a21d9fa6e922707c74e99f98b78f6d6658d3d1",
    "packages/worker/src/sandbox_science/worker/capabilities.toml": "43e59e1da212ebb3e2c75b28e37fb819257bb6c60952022d0c71812e2d0be1fa",
    "packages/worker/src/sandbox_science/worker/package_clients.py": "3e06d9aa48b618bfc812280e97ac7d760639fec9d9d3473d97b0cedf13d869fd",
    "packages/event-store/src/sandbox_science/event_store/store.py": "da0f4ddfc5a30ca88ecdf03558c41e7842967969a98fa1f082039a133dd80189",
    "packages/event-store/src/sandbox_science/event_store/canonical.py": "4a5199396c2af4663b2945a30ed45caa5e10726cd6585048508b943f5ff82f2b",
    "packages/event-store/src/sandbox_science/event_store/records.py": "4c9a8136ca416adcbc89fcd7808e4d78117fc8c4dd3ad8dfb469c76ac858d7ba",
    "packages/policy/src/sandbox_science/policy/approvals.py": "c5dd7fae57ffa73a5f58dc44cbd714396c0b0547f69379de52c07853648af277",
    "examples/policy.yaml": "46b8fef7686bf41866d22f6d15259b990488840a0ac3dc8fc93dd597d243ec16",
    "images/minimal/environment.yml": "e0af09eb9802e9a473bf77a1a445e0e15e034b3fbf903d22bbf84149e286f864",
    "uv.lock": "e44852f6571a795acaaf2b3aa062bad38d4bae578bd07d8dcce238318164c61a",
}


class RefusalError(Exception):
    """Fixed local refusal; never carry raw configuration, subprocess or guest errors."""


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
        elif kind is float:
            if not math.isfinite(item) or not -(2**63) < item < 2**63:
                raise RefusalError("public float differs")
            remaining -= 32
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
        elif kind is list or kind is tuple:
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
    raw = (
        json.dumps(value, sort_keys=True, ensure_ascii=True, allow_nan=False, indent=2) + "\n"
    ).encode("ascii")
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


def write_new(relative: str, value: object, *, excluded: tuple[str, ...] = ()) -> None:
    if type(excluded) is not tuple or len(excluded) > 3:
        raise RefusalError("private literal boundary differs")
    if type(value) is not dict or "in_memory_client_literals_checked" in value:
        raise RefusalError("public record projection differs")
    raw = encoded({**value, "in_memory_client_literals_checked": len(excluded)})
    for literal in excluded:
        if type(literal) is not str or not 32 <= len(literal) <= 128:
            raise RefusalError("private literal type differs")
        # Only these three owned in-memory synthetic tokens are tested. They are
        # never serialized/hashed, and this is not a general secret detector.
        if literal.encode("ascii") in raw:
            raise RefusalError("owned private literal in public evidence")
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


def finite(value: object, maximum: float) -> float:
    if type(value) is not int and type(value) is not float:
        raise RefusalError("finite budget type differs")
    if not 0 < value <= maximum or not math.isfinite(value):
        raise RefusalError("finite budget differs")
    return float(value)


def remaining(deadline: float, cap: float) -> float:
    return finite(min(deadline - time.monotonic(), finite(cap, WHOLE_SECONDS)), WHOLE_SECONDS)


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
        try:
            if process.poll() is None:
                with suppress(ProcessLookupError):
                    process.kill()
        finally:
            try:
                process.wait()
            finally:
                if process.stdout is not None:
                    process.stdout.close()


def source_guard(head: str, admit: bool) -> dict[str, object]:
    # Literal inert refusal comes before any file/hash/subprocess/private I/O.
    if ADMISSION_READY is not True or admit is not True:
        raise RefusalError("root admission is disabled")
    if type(head) is not str or re.fullmatch(r"[0-9a-f]{40}", head) is None:
        raise RefusalError("source head differs")
    if not SOURCE.is_absolute():
        raise RefusalError("absolute helper path is required")

    if (
        git("rev-parse", "HEAD") != head
        or git("status", "--porcelain")
        or git("show", "-s", "--format=%G?", "HEAD") != "G"
    ):
        raise RefusalError("signed clean source differs")
    for path, expected in INPUTS.items():
        fd = os.open(SOURCE / path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= 2 * 1024 * 1024:
                raise RefusalError("production input bound differs")
            raw = os.pread(fd, 2 * 1024 * 1024 + 1, 0)
            if len(raw) != info.st_size or hashlib.sha256(raw).hexdigest() != expected:
                raise RefusalError("production input differs")
        finally:
            os.close(fd)
    fd = os.open(__file__, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        raw = os.read(fd, 256 * 1024 + 1)
        if len(raw) > 256 * 1024:
            raise RefusalError("helper bound differs")
    finally:
        os.close(fd)
    return {
        "source_head": head,
        "helper_sha256": hashlib.sha256(raw).hexdigest(),
        "product_head": COHERENT_PRODUCT_HEAD,
        "critical_product_inputs": len(INPUTS),
    }


def startup_binding(proof: dict[str, object]) -> None:
    """Pin root's public setup directories before any helper state/app acquisition."""
    fd = os.open(
        "/tmp/lab35-root-owned-startup.json",  # noqa: S108 - fixed nofollow public receipt
        os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
    )
    try:
        before = os.fstat(fd)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != os.getuid()
            or before.st_nlink != 1
            or stat.S_IMODE(before.st_mode) != 0o600
            or not 0 < before.st_size <= MAX_BYTES
        ):
            raise RefusalError("root setup public receipt differs")
        raw = os.pread(fd, MAX_BYTES + 1, 0)
        after = os.fstat(fd)
        if len(raw) != before.st_size or (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
        ) != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
            raise RefusalError("root setup public receipt changed")
    finally:
        os.close(fd)
    value = decode(raw)
    if type(value) is not dict or any(
        type(value.get(key)) is not str or value[key] != proof[key]
        for key in ("source_head", "helper_sha256")
    ):
        raise RefusalError("root setup source binding differs")
    dirs = value.get("owned_directories")
    if type(dirs) is not list or len(dirs) != 2 or type(value.get("LAB")) is not dict:
        raise RefusalError("root setup directory metadata differs")
    rows = [value["LAB"], *dirs]
    for name, row in zip(("", "worker", "evidence"), rows, strict=True):
        if (
            type(row) is not dict
            or type(row.get("path")) is not str
            or row["path"] != str(LAB / name)
        ):
            raise RefusalError("root setup path differs")
        held = directory(name)
        try:
            current = os.fstat(held)
            for key, expected in (
                ("device", current.st_dev),
                ("inode", current.st_ino),
                ("uid", os.getuid()),
                ("mode", 0o700),
            ):
                if type(row.get(key)) is not int or row[key] != expected:
                    raise RefusalError("root setup original directory identity differs")
        finally:
            os.close(held)


def private_settings() -> Any:
    # Root supplies synthetic isolated settings. Never export/hash these bytes.
    from sandbox_science.worker.settings import Settings

    raw = tomllib.loads(bounded("worker/worker.toml", 65536, mode=0o600).decode("utf-8"))
    if raw.get("listen") != "127.0.0.1:8100" or "local" in raw or raw.get("clients", []) != []:
        raise RefusalError("private configuration scope differs")
    for key in ("state_dir", "token_file"):
        raw[key] = LAB / "worker" / raw[key]
    config = raw.get("openshell")
    if type(config) is not dict or config.get("daemon", True) is not True:
        raise RefusalError("managed daemon required")
    managed = config.get("environments")
    if (
        type(managed) is not dict
        or managed.get("suspend_after") is not None
        or managed.get("admission")
    ):
        raise RefusalError("uncontrolled background owner")
    for key in ("policy_store", "targets", "ceiling", "event_store"):
        if managed.get(key):
            managed[key] = LAB / "worker" / managed[key]
    settings = Settings.model_validate(raw)
    if (
        settings.openshell is None
        or settings.openshell.environments is None
        or settings.state_dir != LAB / "worker/state"
        or settings.token_file != LAB / "worker/worker.token"
        or settings.kernel_idle_timeout is not None
        or settings.openshell.environments.profiles
        != {"minimal": "sandbox-science/minimal:02f85b3"}
        or settings.openshell.environments.event_store_path != LAB / "middleware/state/events.db"
    ):
        raise RefusalError("original private paths/profile differ")
    expected_env = {
        "XDG_CONFIG_HOME": str(LAB / "config"),
        "XDG_STATE_HOME": str(LAB / "state"),
        "XDG_DATA_HOME": str(LAB / "data"),
        "OPENSHELL_LOCAL_TLS_DIR": str(LAB / "state/openshell/tls"),
        "OPENSHELL_TELEMETRY_ENABLED": "false",
    }
    allowed = {
        "HOME",
        "USER",
        "LANG",
        "PATH",
        "XDG_RUNTIME_DIR",
        "DBUS_SESSION_BUS_ADDRESS",
        *expected_env,
    }
    managed = settings.openshell.environments
    if (
        set(os.environ) - allowed
        or any(os.environ.get(k) != v for k, v in expected_env.items())
        or settings.openshell.env != expected_env
        or settings.openshell.cli != str(Path.home() / ".local/opt/openshell-0.1.3/bin/openshell")
        or settings.openshell.python != "/opt/sandbox-science/bin/python"
        or settings.openshell.isolation_tier != 3
        or settings.openshell.package_cache != "http://host.openshell.internal:8090"
        or settings.openshell.sandboxes
        or not settings.shared_sync
        or managed.targets != LAB / "targets.yaml"
        or managed.ceiling != LAB / "middleware/pool-ceiling.yaml"
        or managed.policy_store != LAB / "middleware/state"
    ):
        raise RefusalError("fixed private paths/sanitized direct environment differ")
    return settings


class Owner:
    """Normal CLI/App assembly, one owned listener and private in-memory clients."""

    def __init__(self) -> None:
        self.manager: Any = None
        self.store: Any = None
        self.environments: Any = None
        self.provisioner: Any = None
        self.documents: Any = None
        self.shared: Any = None
        self.shared_records: Any = None
        self.runner: Any = None
        self.http: Any = None
        self.pool: Any = None
        self.client: Any = None
        self.foreign: Any = None
        self.private_literals: tuple[str, ...] = ()
        self.original_vm: dict[str, str] | None = None
        self.port = 0
        self.closed = False
        self.cleanup: dict[str, object] = {"owners_drained": False, "borrowed_stores_closed": False}

    async def open(self, deadline: float) -> None:
        import aiohttp
        from aiohttp import web

        from sandbox_science.orchestrator.worker_client import WorkerClient
        from sandbox_science.worker import cli
        from sandbox_science.worker.app import create_app
        from sandbox_science.worker.artifacts import Blobs
        from sandbox_science.worker.backends import OpenShellBackend
        from sandbox_science.worker.documents_catalog import DocumentsCatalog
        from sandbox_science.worker.errors import WorkerError
        from sandbox_science.worker.manager import ExecutionManager
        from sandbox_science.worker.settings import ClientSettings, token_digest
        from sandbox_science.worker.shared_sync import SharedRecords, SharedSync
        from sandbox_science.worker.store import Store

        settings = private_settings()
        logging.disable(logging.CRITICAL)
        self.environments = cli._environments(settings)
        if self.environments is None:
            raise RefusalError("managed environment required")
        if (
            self.environments.records._db.execute("SELECT 1 FROM environments LIMIT 1").fetchone()
            is not None
        ):
            raise RefusalError("fresh canonical environment state is required")
        self.store = Store(settings.state_dir / "worker.sqlite3")
        self.documents = DocumentsCatalog(
            settings.state_dir / "documents-catalog.sqlite3", settings.document_catalog
        )
        self.shared_records = SharedRecords(settings.state_dir / "shared.sqlite3")
        self.shared = SharedSync(self.shared_records, settings.state_dir / "shared")
        backend = cli._backend(settings, self.environments)
        if not isinstance(backend, OpenShellBackend):
            raise RefusalError("host fallback forbidden")

        class CappedManager(ExecutionManager):
            """Lab-only pre-dispatch counter; production execution path stays unchanged."""

            async def submit(self, request, *, origin=None):
                from sandbox_science.contracts import ErrorKind

                async with submit_lane:
                    if (
                        request.context.project_id != PROJECT
                        or request.context.session_id != SESSION
                        or request.context.environment_id != ENVIRONMENT
                    ):
                        raise WorkerError(ErrorKind.UNAVAILABLE, "lab execution scope refused")
                    if (
                        lab_owner.original_vm is None
                        or original_environment(lab_owner.environments.get(ENVIRONMENT))
                        != lab_owner.original_vm
                    ):
                        raise WorkerError(ErrorKind.UNAVAILABLE, "lab original VM changed")
                    found = self.store.find_operation(PROJECT, request.operation_id)
                    if found is None:
                        row = self.store._db.execute("SELECT count(*) FROM executions").fetchone()
                        if (
                            row is None
                            or type(row[0]) is not int
                            or not 0 <= row[0] < MAX_EXECUTIONS
                        ):
                            raise WorkerError(ErrorKind.UNAVAILABLE, "lab execution cap reached")
                    return await super().submit(request, origin=origin)

        submit_lane = asyncio.Lock()
        lab_owner = self
        self.manager = CappedManager(
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

        def identity(environment: str):
            if not env.manages(environment):
                return None
            info = env.get(environment)
            return (info.sandbox, info.sandbox_id) if info.sandbox_id else None

        m.daemon_identity = identity
        self.provisioner = cli._provisioner(settings, m, env)
        pool_token, project_token, foreign_token = (secrets.token_urlsafe(48) for _ in range(3))
        self.private_literals = (pool_token, project_token, foreign_token)
        clients = [
            ClientSettings(
                name="lab35",
                token_sha256=token_digest(project_token),
                projects=(PROJECT,),
                max_environments=1,
            ),
            ClientSettings(
                name="lab35-foreign",
                token_sha256=token_digest(foreign_token),
                projects=("lab35-foreign",),
                max_environments=1,
            ),
        ]
        self.runner = web.AppRunner(
            create_app(m, pool_token, env, clients, self.provisioner, self.documents),
            access_log=None,
        )
        await self.shared.sweep()
        await m.start()
        await self.provisioner.start()
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        addresses = self.runner.addresses
        if (
            len(addresses) != 1
            or addresses[0][0] != "127.0.0.1"
            or type(addresses[0][1]) is not int
        ):
            raise RefusalError("owned listener differs")
        self.port = addresses[0][1]
        self.http = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15))

        class BoundedClient(WorkerClient):
            async def _request(
                self,
                method,
                path,
                model,
                body=None,
                params=None,
                max_bytes=None,
                *,
                total_timeout=None,
                allow_redirects=True,
            ):
                # Production transport already supports these finite/capped kwargs.
                cap = (
                    180.0
                    if path.startswith("/v1/environments") and method in ("POST", "DELETE")
                    else 15.0
                )
                return await super()._request(
                    method,
                    path,
                    model,
                    body,
                    params,
                    min(max_bytes, 1024 * 1024) if max_bytes is not None else 1024 * 1024,
                    total_timeout=remaining(deadline, cap),
                    allow_redirects=False,
                )

        url = f"http://127.0.0.1:{self.port}"
        self.pool = BoundedClient(url, pool_token, session=self.http)
        self.client = BoundedClient(url, project_token, session=self.http)
        self.foreign = BoundedClient(url, foreign_token, session=self.http)

    async def close(self) -> None:
        if self.closed:
            return

        async def drain():
            failed = []
            for name, work in (
                ("http", self.http.close if self.http else None),
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
                raise RefusalError("host drain failed; borrowed stores held")
            if self.provisioner is not None and self.provisioner.permissions is not None:
                try:
                    self.provisioner.permissions.close()
                except BaseException:
                    self.cleanup["failed_owners"] = ["event-writer"]
                    raise RefusalError("event writer close failed; borrowed stores held") from None
            failed_stores = []
            for name, store in (
                ("worker", self.store),
                ("documents", self.documents),
                ("shared", self.shared_records),
                ("environments", self.environments),
            ):
                if store is not None:
                    try:
                        store.close()
                    except BaseException:
                        failed_stores.append(name)
            if failed_stores:
                self.cleanup["failed_stores"] = failed_stores
                raise RefusalError("store close failed; root reconciliation required")
            self.closed = True
            self.cleanup["borrowed_stores_closed"] = True

        _, interrupted = await owned(drain())
        if interrupted is not None:
            raise interrupted


def original_environment(info: Any, *, deleted: bool = False) -> dict[str, str]:
    from sandbox_science.contracts import EnvironmentInfo, EnvironmentState

    if type(info) is not EnvironmentInfo or type(deleted) is not bool:
        raise RefusalError("environment DTO type differs")
    for key, expected in (
        ("environment_id", ENVIRONMENT),
        ("project_id", PROJECT),
        ("profile", "minimal"),
        ("image", "sandbox-science/minimal:02f85b3"),
        ("policy", "sha256:" + INPUTS["examples/policy.yaml"]),
    ):
        value = getattr(info, key)
        if type(value) is not str or value != expected:
            raise RefusalError("original VM scope differs")
    if type(info.state) is not EnvironmentState or info.state is not (
        EnvironmentState.DELETED if deleted else EnvironmentState.READY
    ):
        raise RefusalError("original VM state differs")
    if (
        type(info.providers) is not tuple
        or len(info.providers) != 1
        or any(type(value) is not str or value != "github-readonly" for value in info.providers)
    ):
        raise RefusalError("fixed synthetic provider attachment differs")
    return {
        "environment_id": ENVIRONMENT,
        "project_id": PROJECT,
        "name": identifier(info.sandbox),
        "id": identifier(info.sandbox_id),
    }


def scoped_execution(value: Any, operation: str, origin: str | None) -> dict[str, object]:
    from sandbox_science.contracts import Execution, ExecutionContext, ExecutionState

    if type(value) is not Execution or type(value.state) is not ExecutionState:
        raise RefusalError("execution DTO type differs")
    ctx = value.context
    if type(ctx) is not ExecutionContext or any(
        type(getattr(ctx, name)) is not str or getattr(ctx, name) != expected
        for name, expected in (
            ("project_id", PROJECT),
            ("session_id", SESSION),
            ("environment_id", ENVIRONMENT),
        )
    ):
        raise RefusalError("original execution context differs")
    if (
        type(value.operation_id) is not str
        or value.operation_id != operation
        or (value.origin is not None and type(value.origin) is not str)
        or value.origin != origin
    ):
        raise RefusalError("original execution operation/origin differs")
    return {
        "execution_id": identifier(value.execution_id),
        "attempt_id": identifier(value.attempt_id),
        "operation_id": operation,
        "state": value.state.value,
    }


def original_location(value: Any, original: dict[str, str] | None) -> None:
    from sandbox_science.contracts import Execution
    from sandbox_science.contracts.worker import Location

    if type(value) is not Execution or type(original) is not dict:
        raise RefusalError("original terminal location scope missing")
    location = value.location
    if (
        type(location) is not Location
        or type(location.backend) is not str
        or location.backend != "openshell"
        or type(location.sandbox_id) is not str
        or location.sandbox_id != original["name"]
        or type(location.isolation_tier) is not int
        or location.isolation_tier != 3
        or type(location.declared_isolation_tier) is not int
        or location.declared_isolation_tier != 3
    ):
        raise RefusalError("terminal work is not on original tier3 OpenShell sandbox")


def observed_events(owner: Owner, originals: dict[str, Any]) -> list[dict[str, object]]:
    """Read selected bounded rows on the actual borrowed writer; never reopen/seed it."""
    from sandbox_science.event_store import canonical_payload, record_hash
    from sandbox_science.worker.provision_events import checked_payload, project

    permissions = owner.provisioner.permissions
    if permissions is None:
        raise RefusalError("original event writer missing")
    with permissions._lock:
        writer = permissions._writer
    if writer is None or not writer._lock.acquire(timeout=5):
        raise RefusalError("original event writer unavailable")
    try:
        if writer._db is None:
            raise RefusalError("original event connection missing")
        # Guards execute inside SQLite BEFORE materializing a selected payload.
        safe = "CASE WHEN typeof(payload)='text' AND length(CAST(payload AS BLOB)) BETWEEN 1 AND 4096 THEN payload ELSE '{}' END"
        names = ("time", "kind", "source", "payload", "prev_hash", "hash")
        caps = (40, 64, 64, 4096, 64, 64)
        terms = ["typeof(seq)", "CASE WHEN typeof(seq)='integer' THEN seq END"]
        for name, cap in zip(names, caps, strict=True):
            terms.extend(
                (
                    f"typeof({name})",
                    f"length(CAST({name} AS BLOB))",
                    f"substr(CAST({name} AS BLOB),1,{cap + 1})",
                )
            )
        ids = tuple(originals)
        if not 1 <= len(ids) <= 2:
            raise RefusalError("event operation selection differs")
        query = (
            "SELECT " + ",".join(terms) + " FROM events WHERE kind IN (?,?,?) AND "
            f"json_extract({safe},'$.context.project_id')=? AND "
            f"json_extract({safe},'$.context.session_id')=? AND "
            f"json_extract({safe},'$.context.environment_id')=? AND "
            f"json_extract({safe},'$.provision_id') IN ("
            + ",".join("?" for _ in ids)
            + ") ORDER BY seq LIMIT 7"
        )
        rows = writer._db.execute(
            query,
            (
                "provision.requested",
                "provision.awaiting_permission",
                "provision.finished",
                PROJECT,
                SESSION,
                ENVIRONMENT,
                *ids,
            ),
        ).fetchall()
        if len(rows) > 6:
            raise RefusalError("event row cap exceeded")
        result = []
        seen = set()
        for row in rows:
            if row[0] != "integer" or type(row[1]) is not int or not 0 < row[1] < 2**63:
                raise RefusalError("event sequence differs")
            values = []
            for index, cap in enumerate(caps):
                tag, size, raw = row[2 + 3 * index : 5 + 3 * index]
                if (
                    tag != "text"
                    or type(size) is not int
                    or not 0 < size <= cap
                    or type(raw) is not bytes
                    or len(raw) != size
                ):
                    raise RefusalError("event original byte/type bound differs")
                values.append(raw.decode("utf-8"))
            moment, kind, source, payload, previous, hash_ = values
            parsed = datetime.fromisoformat(moment)
            if parsed.utcoffset() is None:
                raise RefusalError("event time is not aware")
            if source != "worker":
                raise RefusalError("event writer differs")
            body = checked_payload(kind, decode(payload.encode("utf-8"), 4096))
            if payload != canonical_payload(body) or record_hash(
                prev_hash=digest(previous),
                seq=row[1],
                time=moment,
                kind=kind,
                source=source,
                payload=payload,
            ) != digest(hash_):
                raise RefusalError("selected event hash differs")
            provision = identifier(body["provision_id"])
            key = (provision, kind)
            if key in seen or provision not in originals:
                raise RefusalError("duplicate/foreign event")
            seen.add(key)
            initial, terminal = originals[provision]
            if kind == "provision.requested" and body != project(initial, kind):
                raise RefusalError("original requested event differs")
            if kind == "provision.finished" and body != project(terminal, kind):
                raise RefusalError("original finished event differs")
            if kind == "provision.awaiting_permission" and (
                body["permission_request"] != terminal.permission_request
                or not 0 < body["operation_seq"] < terminal.seq
            ):
                raise RefusalError("original permission event differs")
            # Chain predecessor/self hashes remain in memory, never public output.
            result.append(
                {
                    "writer_seq": row[1],
                    "recorded_at": moment,
                    "kind": kind,
                    "source": source,
                    "observation": body,
                }
            )
        for provision in originals:
            if not {(provision, "provision.requested"), (provision, "provision.finished")} <= seen:
                raise RefusalError("required original events unavailable")
        return result
    finally:
        writer._lock.release()


class Driver:
    def __init__(self, owner: Owner, marker: dict[str, object], deadline: float) -> None:
        self.owner, self.marker, self.deadline = owner, marker, deadline
        self.original: dict[str, str] | None = None
        self.accepted: set[str] = set()
        self.originals: dict[str, Any] = {}
        self.operations = 0
        self.installs = 0
        self.replays = 0
        self.deleted = False

    def save(self, stage: str, value: dict[str, object]) -> None:
        write_new(
            PREFIX + identifier(stage) + ".json",
            {**self.marker, "stage": stage, "recorded_at": datetime.now(UTC).isoformat(), **value},
            excluded=self.owner.private_literals,
        )

    def execution_count(self) -> int:
        row = self.owner.store._db.execute("SELECT count(*) FROM executions").fetchone()
        if row is None or type(row[0]) is not int or not 0 <= row[0] <= MAX_EXECUTIONS:
            raise RefusalError("canonical execution count differs")
        return row[0]

    async def require_original(self) -> None:
        remaining(self.deadline, 15)
        remote = original_environment(await self.owner.pool.environment(ENVIRONMENT))
        local = original_environment(self.owner.environments.get(ENVIRONMENT))
        if remote != self.original or local != self.original:
            raise RefusalError("original VM changed; new work refused")

    async def known_execution(self, execution: Any, operation: str, origin: str | None) -> Any:
        initial = scoped_execution(execution, operation, origin)
        self.accepted.add(execution.execution_id)
        if len(self.accepted) > MAX_EXECUTIONS:
            raise RefusalError("accepted execution cap exceeded")
        for _ in range(120):
            if execution.state.value in {"succeeded", "failed", "cancelled", "timed_out", "lost"}:
                original_location(execution, self.original)
                return execution
            remaining(self.deadline, 15)
            await asyncio.sleep(0.25)
            execution = await self.owner.client.get(execution.execution_id, project_id=PROJECT)
            current = scoped_execution(execution, operation, origin)
            if any(current[key] != initial[key] for key in ("execution_id", "attempt_id")):
                raise RefusalError("original execution/attempt changed")
        raise RefusalError("execution result unknown; no retry")

    async def operation(self, request: Any, label: str) -> None:
        from sandbox_science.contracts import ProvisionOutcome, ProvisionState
        from sandbox_science.worker.installer_records import prepare

        if self.operations >= MAX_NEW_OPERATIONS:
            raise RefusalError("original operation cap exceeded")
        self.save(
            label + "-intent", {"operation_id": request.operation_id, "kind": "new-provision"}
        )
        remaining(self.deadline, 15)
        await self.require_original()
        remaining(self.deadline, 15)
        self.operations += 1
        initial, created = await self.owner.client.provision(request)
        if not created or initial.request != request:
            raise RefusalError("unexpected prior/foreign operation")
        current = initial
        permission_saved = False
        for _ in range(256):
            remaining(self.deadline, 15)
            if current.request != request:
                raise RefusalError("selected operation request differs")
            if current.state is ProvisionState.FINISHED:
                break
            if current.state is ProvisionState.AWAITING_PERMISSION and not permission_saved:
                self.save(
                    label + "-permission-held",
                    {
                        "provision_id": identifier(current.provision_id),
                        "permission_request": current.permission_request,
                        "operator_answered_by_helper": False,
                    },
                )
                permission_saved = True
            current = await self.owner.client.provision_status(
                current.provision_id, after=current.seq, wait=5, project_id=PROJECT
            )
        else:
            raise RefusalError("operation wait exhausted; no retry")
        pid = identifier(current.provision_id)
        row = self.owner.store._db.execute(
            "SELECT typeof(record),length(CAST(record AS BLOB)) FROM provisions WHERE provision_id=?",
            (pid,),
        ).fetchone()
        if (
            row is None
            or row[0] != "text"
            or type(row[1]) is not int
            or not 0 < row[1] <= 1024 * 1024
        ):
            raise RefusalError("canonical operation bound differs")
        canonical = self.owner.store.provision(pid)
        if canonical != current or current.request != request:
            raise RefusalError("canonical operation differs")
        task = self.owner.provisioner._ops[pid].task
        if task is None:
            raise RefusalError("original operation owner missing")
        _, interrupted = await owned(task)
        if interrupted is not None:
            raise interrupted
        self.originals[pid] = (initial, current)
        self.save(
            label + "-terminal",
            {
                "provision_id": pid,
                "outcome": current.outcome.value if current.outcome else None,
                "operation_stage": current.stage,
                "reason": current.reason,
                "revision_before": current.revision_before,
                "revision_after": current.revision_after,
                "permission_observed": permission_saved,
            },
        )
        if current.outcome is not ProvisionOutcome.COMPLETED:
            raise RefusalError("operation not completed; root reconciliation required")
        steps = current.installer_steps
        wanted = [f"version-{label}", f"resolve-{label}", f"install-{label}"]
        if steps is None or [s.label for s in steps] != wanted:
            raise RefusalError("installer step coverage differs")
        if label == "pip" and any(
            f.ecosystem != "pypi" or not f.file.endswith(".whl") for f in current.files
        ):
            raise RefusalError("wheel-only operation differs")
        self.installs += 1
        if self.installs > MAX_INSTALLS:
            raise RefusalError("installer count exceeded")
        logs = []
        for step in steps:
            if (
                step.execution_id is None
                or step.attempt_id is None
                or step.log is None
                or step.log_status != "stored"
            ):
                raise RefusalError("installer log unavailable; no backfill")
            execution = await self.owner.client.get(step.execution_id, project_id=PROJECT)
            scoped = scoped_execution(execution, step.operation_id, pid)
            original_location(execution, self.original)
            if execution.attempt_id != step.attempt_id:
                raise RefusalError("original installer attempt differs")
            prepared = prepare(current, execution, step.label)
            raw = await self.owner.client.verified_artifact_content(step.log, max_bytes=65536)
            if (
                raw != prepared.raw
                or step != prepared.step
                or self.owner.store.installer_artifact(step.log) != step.log
            ):
                raise RefusalError("original sanitized artifact differs")
            try:
                await self.owner.foreign.verified_artifact_content(step.log, max_bytes=65536)
            except Exception as error:
                if getattr(error, "status", None) not in (403, 404):
                    raise RefusalError("foreign artifact refusal unknown") from None
            else:
                raise RefusalError("foreign project read was accepted")
            logs.append(
                {
                    "execution": scoped,
                    "label": step.label,
                    "version": step.observed_version,
                    "artifact_id": step.log.artifact_id,
                    "sha256": step.log.sha256,
                    "size": step.log.size,
                    "sanitized_log": decode(raw),
                    "foreign_project_refused": True,
                }
            )
        for execution_id in current.executions:
            self.accepted.add(identifier(execution_id))
        if len(self.accepted) > MAX_EXECUTIONS:
            raise RefusalError("accepted execution cap exceeded")
        self.save(
            label + "-artifacts",
            {"provision_id": pid, "logs": logs, "accepted_execution_count": len(self.accepted)},
        )
        if label == "conda" and not any(
            p.probe == "flint-factor" and p.kind == "functional" and p.status == "passed"
            for p in current.probes
        ):
            raise RefusalError("actual functional probe not passed")
        if steps[0].observed_version is None:
            raise RefusalError("installer version remains unknown")

    async def run(self) -> None:
        from sandbox_science.contracts import (
            CellRequest,
            EnvironmentRequest,
            ExecutionContext,
            ExecutionState,
            ProvisionRequest,
        )

        policy = bounded("policy.digest", 128).decode("ascii").strip()
        if policy != "sha256:" + INPUTS["examples/policy.yaml"]:
            raise RefusalError("original policy reference differs")
        self.save("create-intent", {"environment_id": ENVIRONMENT, "profile": "minimal"})
        remaining(self.deadline, 180)
        created = await self.owner.pool.create_environment(
            EnvironmentRequest(
                environment_id=ENVIRONMENT, project_id=PROJECT, profile="minimal", policy=policy
            )
        )
        self.original = original_environment(created)
        self.owner.original_vm = self.original
        self.save(
            "original",
            {
                "original": self.original,
                "owned_port": self.owner.port,
                "synthetic_provider_attachment": "github-readonly",
                "provider_or_model_request": False,
            },
        )
        context = ExecutionContext(
            project_id=PROJECT, session_id=SESSION, environment_id=ENVIRONMENT
        )
        self.save("pilot-intent", {"operation_id": "lab35-pilot", "kind": "python-scalar"})
        remaining(self.deadline, 15)
        await self.require_original()
        remaining(self.deadline, 15)
        submitted = await self.owner.client.submit(
            CellRequest(
                operation_id="lab35-pilot",
                context=context,
                language="python",
                code="print(35)",
                timeout_seconds=10,
                output_limit_bytes=4096,
            )
        )
        pilot = await self.known_execution(submitted, "lab35-pilot", None)
        if (
            pilot.state is not ExecutionState.SUCCEEDED
            or pilot.result is None
            or pilot.result.stdout is None
            or pilot.result.stdout.text != "35\n"
            or pilot.result.stdout.truncated
        ):
            raise RefusalError("small actual scalar pilot failed")
        self.save(
            "pilot",
            {
                "execution": scoped_execution(pilot, "lab35-pilot", None),
                "scalar": 35,
                "before_any_install": True,
            },
        )
        conda = ProvisionRequest(
            operation_id="lab35-conda",
            context=context,
            capability="python-flint",
            purpose="Lab35 installer observation acceptance",
        )
        pip = ProvisionRequest(
            operation_id="lab35-pip",
            context=context,
            pip=("ecdsa==0.19.1",),
            pip_reason="A small pure Python wheel fixture through the normal package policy",
            purpose="Lab35 installer observation acceptance",
        )
        await self.operation(conda, "conda")
        await self.operation(pip, "pip")
        terminal_pip = next(final for _, final in self.originals.values() if final.request == pip)
        revision = terminal_pip.revision_after
        if type(revision) is not int or revision < 0:
            raise RefusalError("original pip revision is unknown")
        history = await self.owner.client.revisions(ENVIRONMENT, after=revision - 1, limit=1)
        if (
            history.environment_id != ENVIRONMENT
            or len(history.revisions) != 1
            or history.revisions[0].revision != revision
            or history.revisions[0].lockfile is None
        ):
            raise RefusalError("selected pip lockfile reference is unknown")
        lockfile = await self.owner.client.lockfile(ENVIRONMENT, revision)
        distributions = [entry for entry in lockfile.pip if entry.name == "ecdsa"]
        if (
            len(distributions) != 1
            or distributions[0].version != "0.19.1"
            or distributions[0].license is None
        ):
            raise RefusalError("installed pip declaration remains unknown")
        self.save(
            "pip-declaration",
            {
                "revision": revision,
                "name": "ecdsa",
                "version": "0.19.1",
                "declared_license": distributions[0].license,
                "scope": "bounded selected lockfile API; declaration, not verified license/permission",
            },
        )
        before = observed_events(self.owner, self.originals)
        before_count = self.execution_count()
        if self.replays >= MAX_REPLAYS:
            raise RefusalError("known replay cap exceeded")
        self.save("replay-intent", {"operation_id": conda.operation_id, "known_terminal": True})
        remaining(self.deadline, 15)
        self.replays += 1
        again, created = await self.owner.client.provision(conda)
        terminal = next(
            final for initial, final in self.originals.values() if final.request == conda
        )
        if (
            created
            or again != terminal
            or observed_events(self.owner, self.originals) != before
            or self.execution_count() != before_count
        ):
            raise RefusalError("known replay changed retained operation/events")
        self.save(
            "events",
            {
                "events": before,
                "awaiting_permission_covered": any(
                    e["kind"] == "provision.awaiting_permission" for e in before
                ),
                "new_operations": self.operations,
                "known_replays": self.replays,
                "install_steps": self.installs,
                "accepted_executions": len(self.accepted),
                "canonical_execution_count": before_count,
                "no_model_or_provider_calls": True,
            },
        )
        self.save("delete-intent", {"original": self.original})
        remaining(self.deadline, 180)
        current = original_environment(await self.owner.pool.environment(ENVIRONMENT))
        if (
            current != self.original
            or original_environment(self.owner.environments.get(ENVIRONMENT)) != self.original
        ):
            raise RefusalError("original VM changed; deletion refused")
        removed = await self.owner.pool.delete_environment(ENVIRONMENT)
        if original_environment(removed, deleted=True) != self.original:
            raise RefusalError("original deletion receipt differs")
        self.deleted = True
        self.save("deleted", {"original": self.original, "confirmed": True})


async def runtime(head: str, admit: bool) -> None:
    proof = source_guard(head, admit)
    startup_binding(proof)
    evidence = directory("evidence")
    try:
        if stat.S_IMODE(os.fstat(evidence).st_mode) != 0o700:
            raise RefusalError("root-owned evidence mode differs")
    finally:
        os.close(evidence)
    marker = {
        "version": 1,
        **proof,
        "run_id": secrets.token_hex(16),
        "project_id": PROJECT,
        "session_id": SESSION,
        "environment_id": ENVIRONMENT,
        "whole_seconds": 1800,
        "max_executions": MAX_EXECUTIONS,
        "max_new_operations": MAX_NEW_OPERATIONS,
        "max_installs": MAX_INSTALLS,
        "max_known_replays": MAX_REPLAYS,
    }
    write_new("lab35.created", marker)
    deadline = time.monotonic() + finite(WHOLE_SECONDS, 1800)
    owner = Owner()
    driver = Driver(owner, marker, deadline)
    failure = None
    try:
        async with asyncio.timeout(remaining(deadline, 1800)):
            await owner.open(deadline)
            await driver.run()
    except BaseException as error:
        failure = error
    finally:
        try:
            await owner.close()
        except BaseException as error:
            failure = failure or error
        write_new(
            PREFIX + "cleanup.json",
            {
                **marker,
                "owners": owner.cleanup,
                "original_vm": driver.original,
                "vm_deletion_confirmed": driver.deleted,
                "root_reconciliation_required": failure is not None or not driver.deleted,
            },
            excluded=owner.private_literals,
        )
    if failure is not None:
        raise RefusalError("acceptance held; use original root reconciliation") from None
    print("PASS Lab35 bounded CP16 observations", flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stage", choices=("describe", "run"), nargs="?", default="describe")
    parser.add_argument("--head")
    parser.add_argument("--admit", action="store_true")
    args = parser.parse_args()
    if args.stage == "describe":
        print(
            json.dumps(
                {
                    "project": PROJECT,
                    "status": "source-only; root admission disabled",
                    "vm": 1,
                    "executions": MAX_EXECUTIONS,
                    "new_operations": MAX_NEW_OPERATIONS,
                    "install_steps": MAX_INSTALLS,
                    "known_replays": MAX_REPLAYS,
                    "whole_seconds": 1800,
                    "model_provider_calls": 0,
                    "private_configuration_inferred": False,
                },
                sort_keys=True,
            )
        )
        return 0
    try:
        asyncio.run(runtime(args.head, args.admit))
    except BaseException:
        print("HELD Lab35; no automatic effect retry", flush=True)
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
