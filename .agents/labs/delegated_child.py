# SPDX-License-Identifier: MIT
# ruff: noqa: S101, S603, S607, S608
"""Root-only Lab34 controller/HTTP fixture; describe is inert, admission disabled.

Scripted ChatModel inputs exercise actual restricted controllers, canonical
journals and authenticated worker dispatch. Root alone admits effects, config,
OpenShell infrastructure/gateway proof and complete teardown. No model/provider,
host research fallback, recursive delegation or scientific certification.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import fcntl
import hashlib
import json
import logging
import os
import re
import select
import signal
import stat
import subprocess
import time
import tomllib
import uuid
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from typing import Any

ADMISSION_READY = True
SOURCE = Path(__file__).resolve().parents[2]
LAB = Path.home() / ".local/state/openshell-lab"
PROJECT = "lab34"
SESSION = "lab34-session"
MARKER = "child-controller.created"
CONTROL = "child-controller-control"
PREFIX = "evidence/child-controller-"
MAX_BYTES = 2 * 1024 * 1024
WHOLE_SECONDS = 1800
MAX_CELLS = 12
MAX_SUBMITS = 14
MAX_CONTROLS = 8
SHARED_TEXT = "lab34 shared witness\n"
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
# Frozen production composition, not private settings/build/provider metadata.
INPUTS: dict[str, str] = {
    "packages/worker/src/sandbox_science/worker/cli.py": "dda5982432fec71ebb99bc942331f0bae531f0e573c2e1e7ae7143c6164838a9",
    "packages/worker/src/sandbox_science/worker/app.py": "979334aae75d799568247ac4f5036aaa4dd891b372e918bd66236d7eb4d71f9a",
    "packages/worker/src/sandbox_science/worker/settings.py": "2d820c019485e847d94f9d6141a5889ad81849f5baef3c3714aa7420139a61fd",
    "packages/worker/src/sandbox_science/worker/run_api.py": "9fab8d4893b30d9ee4483a355ec3c53f6b0d31646c5f108fdc060ed857493450",
    "packages/worker/src/sandbox_science/worker/manager.py": "fe28a4fe1f62784b4032e7d1c09eace06a80301ba02915b3d20c8f455e68ed6b",
    "packages/worker/src/sandbox_science/worker/store.py": "11f3d72f0f29345221eaf0363513819255dc99422def11b1ae8b5dfd3a864714",
    "packages/worker/src/sandbox_science/worker/run_dispatch.py": "f2e62fa5997dd345a402a7a8cf291aa55004f9bb8487b73f7a06d9334cf78183",
    "packages/worker/src/sandbox_science/worker/run_kernel_state.py": "b827d8916d7b477cd3e192400f8a645ff7ddcd06f682fd649b53ddfeade77cfc",
    "packages/worker/src/sandbox_science/worker/run_kernels.py": "4d2fb7b6be091e0fb72c5fb501372fb065a77f1c3ff1f574e64be7c5b9271561",
    "packages/worker/src/sandbox_science/worker/executor.py": "07badfd038c69f5127ef05f8826e1d15465b0bf1ca60e13235cb6b994f7df713",
    "packages/worker/src/sandbox_science/worker/daemon.py": "38ee52391add355bc34ff852780913eec61c0b444dcb9c86796855d30a57cce5",
    "packages/worker/src/sandbox_science/worker/environments.py": "9187c2d0c840ac888a5664a7bdaa04f22e9c8da76bee69fc85f61b6de116c7fc",
    "packages/worker/src/sandbox_science/worker/backends.py": "7f22eee4dd47663bc62da668df03f6cb02e01a5a7a5fafebd52d10dc506f4fbf",
    "packages/worker/src/sandbox_science/worker/shared_sync.py": "94ae6ccdf744e700181883924f84f6a00c623220954926b3b4e4dff1c505baa8",
    "packages/worker/src/sandbox_science/worker/provisioner.py": "8aaddce1167f6d43b337aae2c7f3a9e880da5e22fd7d8c7d8e01c8ae083fccd1",
    "packages/worker/src/sandbox_science/worker/suspend.py": "dbafa979b06ded7d75a386566f0da3d1b045f0e0e7625946da35a5d8b6730191",
    "packages/contracts/src/sandbox_science/contracts/run_api.py": "4742f0b9d1303268f0f6c218932ea993e5197497f0a09415a43743bde2252e74",
    "packages/contracts/src/sandbox_science/contracts/run_cells.py": "c426158dc1dd48058a2cd82ce49e96ebb3757954335e820302aafca9706bc5c0",
    "packages/contracts/src/sandbox_science/contracts/run_kernels.py": "33b407d03bc9c87b1a87aff2127807eabd667d280df0758a9fe88bb724e3e72b",
    "packages/contracts/src/sandbox_science/contracts/worker.py": "dee0d24827e53dd16fb625c4ba5320377fe32ed410fe40ac771e93c09e12a21e",
    "packages/contracts/src/sandbox_science/contracts/kernel_lineage.py": "80e67d15b097eb717d38e087d6ffa0aa0c512472251f9815873541d8a81d6196",
    "packages/contracts/src/sandbox_science/contracts/context.py": "5cfb8320d06febb700fdda653e30909a3fd06f19030a29874544f9e4ac593a62",
    "packages/contracts/src/sandbox_science/contracts/environments.py": "77f70badb5586a1dedf9ff940b04187057fecf14980c459fe69a1db71c6b4d67",
    "packages/orchestrator/src/sandbox_science/orchestrator/projects.py": "b007ec185139de0e103bf36c90ac1adfd6a3d6134619380d07b48e8d357bd97e",
    "packages/orchestrator/src/sandbox_science/orchestrator/delegation_records.py": "98180b4fbcde83dae9d2feb004fb26b7caf72f893489e771433c7c37d7fdc7de",
    "packages/orchestrator/src/sandbox_science/orchestrator/child_attempts.py": "386abacc9d35407b29da205f8797e7c75c46957a8d3725c57183a2844d9d0b06",
    "packages/orchestrator/src/sandbox_science/orchestrator/child_prompt.py": "ac8d97c2037fb9b42d2d07900309573096fb8d3698a5ec2d19b5bb98fb52aefc",
    "packages/orchestrator/src/sandbox_science/orchestrator/child_tools.py": "7bbc04ed87fa3074d3964d96fd3bc1977f25b766ee923fc00b35c55d5fec7828",
    "packages/orchestrator/src/sandbox_science/orchestrator/child_controller.py": "7618c5b5ba249d7490ebc2d8fdbbec0ca55afee3a07396d23f3988f2106ad72f",
    "packages/orchestrator/src/sandbox_science/orchestrator/worker_client.py": "9341776645a11a91d93eb73aefc6663c5bd6282bb9515286275a308bbe39c182",
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
        raise RefusalError("public input type or cap differs")
    try:
        return json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=pairs,
            parse_constant=lambda _: (_ for _ in ()).throw(ValueError()),
        )
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise RefusalError("invalid bounded public JSON") from None


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
        if parent == "evidence":
            count = total = 0
            with os.scandir(root) as entries:
                for entry in entries:
                    count += 1
                    info = entry.stat(follow_symlinks=False)
                    if (
                        count >= 128
                        or not stat.S_ISREG(info.st_mode)
                        or info.st_uid != os.getuid()
                        or info.st_nlink != 1
                        or not 0 <= info.st_size <= MAX_BYTES
                    ):
                        raise RefusalError("public evidence count/type cap differs")
                    total += info.st_size
                    if total + len(raw) > 8 * 1024 * 1024:
                        raise RefusalError("public evidence aggregate cap differs")
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
        try:
            if process.poll() is None:
                with contextlib.suppress(ProcessLookupError):
                    process.kill()
            process.wait()
        finally:
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


def make_directory(name: str) -> None:
    identifier(name)
    root = directory()
    try:
        os.mkdir(name, 0o700, dir_fd=root)
        os.fsync(root)
    finally:
        os.close(root)


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


def marked(head: str) -> dict[str, Any]:
    pins = source_guard(head)
    value = decode(bounded(MARKER, 65536), 65536)
    if type(value) is not dict or set(value) != {
        "version",
        "purpose",
        "source_head",
        "helper_sha256",
        "inputs",
        "run_id",
        "project_id",
        "session_id",
        "environment_id",
        "policy",
        "children",
        "recorded_at",
    }:
        raise RefusalError("marker shape differs")
    if type(value["version"]) is not int or value["version"] != 1:
        raise RefusalError("marker version differs")
    if (
        value["purpose"] != "disposable restricted child-controller acceptance"
        or value["source_head"] != head
        or value["helper_sha256"] != pins["helper_sha256"]
        or value["inputs"] != pins["inputs"]
        or value["project_id"] != PROJECT
        or value["session_id"] != SESSION
    ):
        raise RefusalError("marker scope/source differs")
    for key in ("run_id", "environment_id"):
        identifier(value[key])
    stamp(value["recorded_at"])
    if (
        type(value["policy"]) is not str
        or re.fullmatch(r"sha256:[0-9a-f]{64}", value["policy"]) is None
    ):
        raise RefusalError("policy pin differs")
    children = value["children"]
    if type(children) is not dict or set(children) != {"A", "B"}:
        raise RefusalError("original children differ")
    for child in children.values():
        if type(child) is not dict or set(child) != {
            "parent_run_id",
            "delegation_id",
            "child_run_id",
            "request_sha256",
        }:
            raise RefusalError("original child identity differs")
        for key in ("parent_run_id", "delegation_id", "child_run_id"):
            identifier(child[key])
        digest(child["request_sha256"])
    if children["A"]["child_run_id"] == children["B"]["child_run_id"]:
        raise RefusalError("children are not distinct")
    return value


def prepare(head: str) -> dict[str, object]:
    pins = source_guard(head)
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
    session = projects.open_session(PROJECT, SESSION, dedicated=False)
    if (
        not created
        or session.environment_id != project.default_environment_id
        or session.closed_at is not None
    ):
        raise RefusalError("canonical project/session is not fresh")
    documents = DocumentStore(LAB / CONTROL / "control.sqlite3", producer="lab34-scripted-host")
    children = {}
    try:
        documents.initialize()
        records = DelegationRecords(documents, initialize=True)
        for label in ("A", "B"):
            authority = ParentAuthority(
                scope=DelegationScope(
                    project_id=PROJECT, session_id=SESSION, environment_id=session.environment_id
                ),
                parent_run_id="lab34-parent",
            )
            receipt = records.prepare(
                authority,
                "lab34-prepare-" + label.lower(),
                DelegationBrief(
                    task="Fixed harmless scalar fixture " + label,
                    context_summary="Scripted model and positive python only; no provider or scientific claim.",
                    inputs=(),
                    profile=ProfileIntent(
                        profile_id="lab34-scripted",
                        profile_sha256=hashlib.sha256(b"lab34-scripted-v1").hexdigest(),
                        model_alias="lab34-scripted",
                    ),
                    limits=RequestedLimits(turns=6, wall_seconds=1200, output_tokens=3072),
                ),
            )
            children[label] = {
                key: getattr(receipt, key)
                for key in ("parent_run_id", "delegation_id", "child_run_id", "request_sha256")
            }
    finally:
        documents.close()
    marker = {
        "version": 1,
        "purpose": "disposable restricted child-controller acceptance",
        "source_head": head,
        "helper_sha256": pins["helper_sha256"],
        "inputs": pins["inputs"],
        "run_id": "lab34-" + uuid.uuid4().hex,
        "project_id": PROJECT,
        "session_id": SESSION,
        "environment_id": session.environment_id,
        "policy": policy,
        "children": children,
        "recorded_at": utc(),
    }
    write_new(MARKER, marker)
    return {"stage": "prepared", "marker": marker, "effects_started": False}


def private_configuration() -> tuple[Any, str, str, str]:
    from sandbox_science.worker.settings import Settings, token_digest

    value = tomllib.loads(bounded("worker/worker.toml", 65536, mode=0o600).decode("utf-8"))
    if value.get("listen") != "127.0.0.1:8100" or "local" in value:
        raise RefusalError("fixed loopback managed configuration differs")
    base = LAB / "worker"
    for key in ("token_file", "state_dir"):
        if value.get(key) != {"token_file": "worker.token", "state_dir": "state"}[key]:
            raise RefusalError("private path scope differs")
        value[key] = base / value[key]
    openshell = value.get("openshell")
    if type(openshell) is not dict or openshell.get("daemon", True) is not True:
        raise RefusalError("managed daemon required")
    environment = openshell.get("environments")
    if type(environment) is not dict or environment.get("suspend_after") is not None:
        raise RefusalError("uncontrolled suspender differs")
    for key in ("policy_store", "targets", "ceiling", "event_store"):
        if environment.get(key):
            path = Path(environment[key])
            environment[key] = path if path.is_absolute() else base / path
    raw_clients = value.get("clients")
    if (
        type(raw_clients) is not list
        or len(raw_clients) != 2
        or any(
            type(client) is not dict
            or client.get("projects") != [PROJECT]
            or client.get("run_controller", False) is not (index == 0)
            or client.get("run_restart", False) is not False
            or client.get("document_catalog", False) is not False
            for index, client in enumerate(raw_clients)
        )
    ):
        raise RefusalError("original private permission types differ")
    settings = Settings.model_validate(value)
    if (
        settings.openshell is None
        or settings.kernel_idle_timeout is not None
        or not settings.shared_sync
    ):
        raise RefusalError("normal shared managed lifetime differs")
    managed = settings.openshell.environments
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
    if (
        set(os.environ) - allowed
        or any(os.environ.get(key) != item for key, item in expected_env.items())
        or settings.openshell.env != expected_env
        or settings.openshell.cli != str(Path.home() / ".local/opt/openshell-0.1.3/bin/openshell")
        or settings.openshell.python != "/opt/sandbox-science/bin/python"
        or settings.openshell.isolation_tier != 3
        or managed is None
        or managed.profiles != {"minimal": "sandbox-science/minimal:02f85b3"}
        or managed.targets != LAB / "targets.yaml"
        or managed.ceiling != LAB / "middleware/pool-ceiling.yaml"
        or managed.policy_store != LAB / "middleware/state"
        or managed.suspend_after is not None
        or settings.openshell.sandboxes
    ):
        raise RefusalError("fixed managed image/paths/sanitized child environment differs")
    clients = settings.clients
    if len(clients) != 2 or tuple(c.name for c in clients) != (
        "lab34-controller",
        "lab34-ordinary",
    ):
        raise RefusalError("synthetic client set differs")
    tokens = []
    for name in ("worker.token", "controller.token", "ordinary.token"):
        raw = bounded("worker/" + name, 8192, mode=0o600)
        token = raw.decode("ascii").strip()
        if (
            not 32 <= len(token) <= 256
            or not token.startswith("lab34-")
            or re.fullmatch(r"[A-Za-z0-9._-]+", token) is None
        ):
            raise RefusalError("visibly synthetic private token differs")
        tokens.append(token)
    if len(set(tokens)) != 3:
        raise RefusalError("synthetic principals are not distinct")
    for index, client in enumerate(clients):
        if (
            client.projects != (PROJECT,)
            or client.run_restart
            or client.document_catalog
            or client.run_controller is not (index == 0)
            or client.token_sha256 != token_digest(tokens[index + 1])
        ):
            raise RefusalError("explicit private controller permission differs")
    return settings, *tokens


class Owner:
    """Actual ordinary worker CLI factories and one loopback HTTP site."""

    def __init__(self) -> None:
        self.manager: Any = None
        self.store: Any = None
        self.environments: Any = None
        self.provisioner: Any = None
        self.documents: Any = None
        self.shared: Any = None
        self.runner: Any = None
        self.site: Any = None
        self.closed = False
        self.cleanup: dict[str, object] = {"owners_drained": False, "borrowed_stores_closed": False}

    async def open(self) -> None:
        from aiohttp import web

        from sandbox_science.worker import cli
        from sandbox_science.worker.app import create_app
        from sandbox_science.worker.artifacts import Blobs
        from sandbox_science.worker.backends import OpenShellBackend
        from sandbox_science.worker.documents_catalog import DocumentsCatalog
        from sandbox_science.worker.manager import ExecutionManager
        from sandbox_science.worker.shared_sync import SharedRecords, SharedSync
        from sandbox_science.worker.store import Store

        settings, pool, _, _ = private_configuration()
        logging.disable(logging.CRITICAL)
        self.environments = cli._environments(settings)
        if self.environments is None:
            raise RefusalError("managed environments required")
        self.store = Store(settings.state_dir / "worker.sqlite3")
        self.documents = DocumentsCatalog(
            settings.state_dir / "documents-catalog.sqlite3", settings.document_catalog
        )
        self.shared = SharedSync(
            SharedRecords(settings.state_dir / "shared.sqlite3"), settings.state_dir / "shared"
        )
        backend = cli._backend(settings, self.environments)
        if not isinstance(backend, OpenShellBackend):
            raise RefusalError("host fallback forbidden")
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
        env, manager = self.environments, self.manager
        env.release, env.release_idle = manager.release, manager.release_if_idle
        manager.notices = env.notices
        env.deleted.append(self.shared.forget)
        manager.confirmed_daemon_loss = partial(
            cli._confirmed_daemon_loss, settings, manager, env, self.store
        )
        manager.recycle_daemon = env.recycle_executor

        def identity(environment_id: str) -> tuple[str, str] | None:
            if not env.manages(environment_id):
                return None
            info = env.get(environment_id)
            return (info.sandbox, info.sandbox_id) if info.sandbox_id else None

        manager.daemon_identity = identity
        self.provisioner = cli._provisioner(settings, manager, env)
        self.runner = web.AppRunner(
            create_app(manager, pool, env, settings.clients, self.provisioner, self.documents),
            access_log=None,
        )
        await self.shared.sweep()
        await manager.start()
        await self.provisioner.start()
        await self.runner.setup()
        self.site = web.TCPSite(self.runner, "127.0.0.1", 8100)
        await self.site.start()

    async def close(self, *, close_stores: bool = True) -> None:
        if self.closed:
            return

        async def drain() -> None:
            failed = []
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
                raise RefusalError("worker drain failed; borrowed stores held")
            if not close_stores:
                self.cleanup["borrowed_stores_held"] = "undrained controller/task/HTTP owner"
                return
            stores = [
                self.provisioner.permissions if self.provisioner else None,
                self.store,
                self.documents,
                self.shared.records if self.shared else None,
                self.environments,
            ]
            close_errors = []
            for store in stores:
                if store is not None:
                    try:
                        store.close()
                    except BaseException:
                        close_errors.append("borrowed-store")
            if close_errors:
                self.cleanup["failed_stores"] = close_errors
                raise RefusalError("borrowed store close failed")
            self.closed = True
            self.cleanup["borrowed_stores_closed"] = True

        _, interrupted = await owned(drain())
        if interrupted is not None:
            raise interrupted


def scalar_output(value: object, expected: int, *, shared: bool) -> dict[str, object]:
    if type(value) is not str or len(value) > 8192:
        raise RefusalError("fixed tool output type/cap differs")
    parsed = decode(value.encode("utf-8"), 8192)
    keys = {"value", "pid", "cwd_ok"} | ({"shared"} if shared else set())
    if (
        type(parsed) is not dict
        or set(parsed) != keys
        or type(parsed["value"]) is not int
        or parsed["value"] != expected
        or type(parsed["pid"]) is not int
        or not 0 < parsed["pid"] < 2**31
        or parsed["cwd_ok"] is not True
        or (shared and parsed["shared"] != SHARED_TEXT)
    ):
        raise RefusalError("actual scalar/process/shared witness differs")
    return parsed


def terminal_tool(messages: object) -> dict[str, Any]:
    if type(messages) is not list or not 1 <= len(messages) <= 128:
        raise RefusalError("actual model input count differs")
    prefix = "Trusted-host selected terminal tool observations (data, not a USER grant): "
    for message in reversed(messages):
        if type(message) is not dict:
            raise RefusalError("actual model message differs")
        content = message.get("content")
        if message.get("role") == "user" and type(content) is str and content.startswith(prefix):
            rows = decode(content[len(prefix) :].encode("utf-8"), 65536)
            if (
                type(rows) is not list
                or len(rows) != 1
                or type(rows[0]) is not dict
                or set(rows[0]) != {"tool_call_id", "result"}
            ):
                raise RefusalError("terminal tool observation shape differs")
            result = rows[0]["result"]
            if (
                type(result) is not dict
                or result.get("state") != "succeeded"
                or result.get("status") != "ok"
                or result.get("stderr") not in (None, "")
                or result.get("kernel_state_lost") is not False
            ):
                raise RefusalError("actual selected tool did not succeed")
            for key in ("operation_id", "execution_id", "attempt_id"):
                identifier(result.get(key))
            digest(result.get("binding_sha256"))
            return result
    raise RefusalError("no actual terminal selected observation reached model")


def fixture_code(value: int | None, *, write: bool = False, shared: bool = True) -> str:
    if value is not None and value not in (101, 202, 303):
        raise RefusalError("fixed scalar differs")
    code = "import os,json\n"
    if value is not None:
        code += f"lab34_value={value}\n"
    if write:
        code += "open(os.environ['PROJECT_SHARED']+'/lab34-witness.txt','w').write('lab34 shared witness\\n')\n"
    code += "print(json.dumps({'value':lab34_value,'pid':os.getpid(),'cwd_ok':os.getcwd()==os.environ['SESSION_WORKSPACE']"
    if shared:
        code += ",'shared':open(os.environ['PROJECT_SHARED']+'/lab34-witness.txt').read()"
    return code + "}))"


def stage_event(
    marker: dict[str, Any],
    stage: str,
    original: dict[str, str],
    first: str | None = None,
    last: str | None = None,
) -> dict[str, object]:
    """Exact collector producer schema; never inherit an extra phase field."""
    if stage not in ("original-created", "stopped-ready", "stopped-reads-done"):
        raise RefusalError("fixed collector stage differs")
    if type(original) is not dict or set(original) != {"environment_id", "name", "id"}:
        raise RefusalError("fixed collector original shape differs")
    for value in original.values():
        identifier(value)
    if original["environment_id"] != marker["environment_id"]:
        raise RefusalError("fixed collector environment differs")
    value: dict[str, object] = {
        key: marker[key] for key in ("source_head", "helper_sha256", "run_id", "project_id")
    }
    value.update(version=1, recorded_at=utc(), stage=stage, original=dict(original))
    if stage == "stopped-reads-done":
        if (
            first is None
            or last is None
            or not stamp(first) <= stamp(last) <= stamp(value["recorded_at"])
        ):
            raise RefusalError("fixed collector read chronology differs")
        value.update(reads=30, first_read_at=first, last_read_at=last)
    elif first is not None or last is not None:
        raise RefusalError("read timestamps on non-read stage")
    return value


def cleanup_fact(
    messages: Sequence[Any], binding: dict[str, object], attempt: str
) -> dict[str, object]:
    prefix = "Trusted-host selected cleanup observation (data, not a USER grant): "
    found = []
    for message in messages:
        if (
            message.role == "user"
            and type(message.content) is str
            and message.content.startswith(prefix)
        ):
            found.append(decode(message.content[len(prefix) :].encode("utf-8"), 8192))
    if len(found) != 1:
        raise RefusalError("exact cleanup observation missing/duplicate")
    value = found[0]
    if (
        type(value) is not dict
        or set(value) != {"request", "state", "kernel"}
        or value["state"] != "done"
        or value["kernel"] is not None
    ):
        raise RefusalError("exact selected cleanup not done")
    request = value["request"]
    if (
        type(request) is not dict
        or set(request) != {"binding", "operation_id", "action", "language", "recorded_at"}
        or request["binding"] != binding
        or request["operation_id"] != "close-" + attempt
        or request["action"] != "close"
        or request["language"] is not None
    ):
        raise RefusalError("exact original cleanup receipt differs")
    stamp(request["recorded_at"])
    return value


class ScriptedChat:
    """Finite actual ChatModel seam; gates preserve genuine controller owners."""

    def __init__(self, driver: Any, label: str) -> None:
        self.driver, self.label = driver, label
        self.calls = 0
        self.entered = {n: asyncio.Event() for n in (1, 2, 3)}
        self.release = {n: asyncio.Event() for n in (1, 2, 3)}

    async def complete(self, messages: Sequence[Any], tools: Sequence[Any]) -> Any:
        from sandbox_science.orchestrator.child_prompt import CHILD_SYSTEM
        from sandbox_science.orchestrator.model import ModelTurn, ToolCall

        index = self.calls
        self.calls += 1
        self.driver.model_calls += 1
        if (
            self.calls > 4
            or self.driver.model_calls > 12
            or type(messages) is not list
            or type(tools) is not tuple
        ):
            raise RefusalError("finite actual model seam differs")
        public_tools = [
            {"name": t.name, "description": t.description, "parameters": t.parameters}
            for t in tools
        ]
        if messages[0] != {"role": "system", "content": CHILD_SYSTEM} or [
            t["name"] for t in public_tools
        ] != ["python"]:
            raise RefusalError("actual positive child bundle differs")
        controller = self.driver.controllers[self.label]
        state = self.driver.journal.get(
            self.driver.authorities[self.label], controller._attempt_id()
        )
        if (
            state.pending is None
            or state.pending.body.kind != "reserve"
            or state.calls != self.calls
        ):
            raise RefusalError("actual reservation was not durable before model entry")
        write_new(
            PREFIX + f"model-{self.label.lower()}-{index}.json",
            {
                **envelope(self.driver.marker, "actual-model-input"),
                "label": self.label,
                "call": index + 1,
                "reservation": state.pending.model_dump(mode="json"),
                "messages": messages,
                "tools": public_tools,
                "model_kind": "scripted-no-provider",
            },
        )
        if index:
            result = terminal_tool(messages)
            self.driver.execution_ids.add(result["execution_id"])
            fact = scalar_output(
                result.get("stdout"), 202 if self.label == "A" else 303, shared=True
            )
            self.driver.facts[(self.label, index)] = {"fixture_output": fact, "selected": result}
            self.entered[index].set()
            await self.release[index].wait()
        if self.label == "A" and index == 2:
            code = "import time\nprint('lab34 started',flush=True)\ntime.sleep(30)"
        elif index == 0:
            code = fixture_code(202 if self.label == "A" else 303, write=self.label == "A")
        else:
            code = fixture_code(None)
        return ModelTurn(
            None,
            (ToolCall(f"lab34-{self.label}-{index}", "python", json.dumps({"code": code})),),
            "tool_calls",
            "lab34-scripted-known",
            None,
            32,
        )


def complete_sql_memory(db: Any) -> bytes:
    """Bound before serialization; private bytes stay in memory and are never hashed."""
    pages = db.execute("PRAGMA page_count").fetchone()
    sizes = db.execute("PRAGMA page_size").fetchone()
    if (
        len(pages) != 1
        or len(sizes) != 1
        or type(pages[0]) is not int
        or type(sizes[0]) is not int
        or not 1 <= sizes[0] <= 65536
        or not 0 < pages[0] * sizes[0] <= 32 * 1024 * 1024
    ):
        raise RefusalError("owned SQL image page budget differs")
    names = db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name LIMIT 65"
    ).fetchall()
    if len(names) > 64:
        raise RefusalError("owned SQL table budget differs")
    for (name,) in names:
        if type(name) is not str or re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,127}", name) is None:
            raise RefusalError("owned SQL table identity differs")
        count = db.execute(
            'SELECT count(*) FROM (SELECT 1 FROM "' + name + '" LIMIT 513)'
        ).fetchone()[0]
        if type(count) is not int or not 0 <= count <= 512:
            raise RefusalError("owned SQL row budget differs")
    raw = db.serialize()
    if type(raw) is not bytes or not 0 < len(raw) <= 32 * 1024 * 1024:
        raise RefusalError("owned SQL image exceeds budget")
    return raw


def public_snapshot(
    owner: Owner,
    marker: dict[str, Any],
    original: dict[str, str],
    bindings: object = None,
    execution_ids: object = None,
) -> dict[str, object]:
    # Raw retained record/result strings are deliberately not public projections.
    # Complete private SQL bytes are compared only in memory elsewhere.
    if (
        type(bindings) is not dict
        or set(bindings) != {"A", "B"}
        or type(execution_ids) is not set
        or not 1 <= len(execution_ids) <= MAX_CELLS
    ):
        raise RefusalError("public snapshot lacks original accepted anchors")
    for execution_id in execution_ids:
        if type(execution_id) is not str or re.fullmatch(r"exe-[0-9a-f]{24}", execution_id) is None:
            raise RefusalError("public execution anchor differs")
    binding_keys = set()
    incarnation = None
    child_scopes = {}
    for label, binding in bindings.items():
        if type(binding) is not dict or set(binding) != {
            "scope",
            "sandbox",
            "sandbox_id",
            "incarnation",
        }:
            raise RefusalError("public original binding shape differs")
        child = marker["children"][label]
        expected_scope = {
            "project_id": PROJECT,
            "session_id": SESSION,
            "environment_id": marker["environment_id"],
            **{key: child[key] for key in ("parent_run_id", "delegation_id", "child_run_id")},
        }
        if (
            binding["scope"] != expected_scope
            or binding["sandbox"] != original["sandbox"]
            or binding["sandbox_id"] != original["sandbox_id"]
            or type(binding["incarnation"]) is not str
            or re.fullmatch(r"inc-[0-9a-f]{24}", binding["incarnation"]) is None
        ):
            raise RefusalError("public original binding scope differs")
        if incarnation is not None and incarnation != binding["incarnation"]:
            raise RefusalError("original bindings disagree on daemon")
        incarnation = binding["incarnation"]
        binding_keys.add(
            hashlib.sha256(
                b"run-kernel-binding-v1\0"
                + json.dumps(
                    binding, sort_keys=True, separators=(",", ":"), ensure_ascii=True
                ).encode("ascii")
            ).hexdigest()
        )
        child_scopes[child["child_run_id"]] = expected_scope
    if len(binding_keys) != 2:
        raise RefusalError("original child bindings are not distinct")
    omitted = {
        "record",
        "result",
        "fingerprint",
        "changed_by",
        "request_digest",
        "request_sha",
        "operation",
        "operation_id",
        "bytes",
    }
    worker = {}
    for table, columns in TABLES.items():
        public = tuple(column for column in columns if column[0] not in omitted)
        rows = sql_rows(owner.store._db, table, public)
        for row in rows:
            values = dict(zip((column[0] for column in public), row, strict=True))
            for key, wanted in (
                ("environment_id", marker["environment_id"]),
                ("environment", marker["environment_id"]),
                ("session_id", SESSION),
                ("session", SESSION),
                ("project", PROJECT),
                ("sandbox", original["sandbox"]),
                ("sandbox_id", original["sandbox_id"]),
                ("incarnation", incarnation),
                ("language", "python"),
            ):
                if key in values and values[key] != wanted:
                    raise RefusalError("unexpected public retained scope/identity")
            for key in ("execution_id", "execution"):
                if key in values and values[key] not in execution_ids:
                    raise RefusalError("unexpected public retained execution")
            if "binding" in values and values["binding"] not in binding_keys:
                raise RefusalError("unexpected public retained binding")
            if "child" in values:
                wanted = child_scopes.get(values["child"])
                if (
                    wanted is None
                    or values["parent"] != wanted["parent_run_id"]
                    or values["delegation"] != wanted["delegation_id"]
                ):
                    raise RefusalError("unexpected public retained original child tuple")
            for key, allowed in (
                ("terminal", (0, 1)),
                ("cancelled", (0, 1)),
                ("kernel_scope", ("legacy", "run-v1")),
                ("action", ("restart", "close")),
                ("state", ("pending", "done")),
            ):
                if key in values and values[key] not in allowed:
                    raise RefusalError("unexpected public retained enum")
            for key in ("base", "last"):
                if key in values and not 0 <= values[key] < 2**31:
                    raise RefusalError("public retained generation exceeds bound")
            if "recorded" in values:
                stamp(values["recorded"])
        worker[table] = rows
    if (
        worker["executor_endpoints"]
        or {row[0] for row in worker["executions"]} != execution_ids
        or len(worker["executions"]) != len(execution_ids)
        or len(worker["run_kernel_bindings"]) != 2
    ):
        raise RefusalError("public selected fixture coverage differs")
    raw = owner.environments.records._db.execute(
        "SELECT CASE WHEN typeof(document)='text' AND length(CAST(document AS BLOB))<=65536 THEN CAST(document AS BLOB) END FROM environments WHERE environment_id=?",
        (marker["environment_id"],),
    ).fetchall()
    if len(raw) != 1:
        raise RefusalError("owned managed row absent")
    value = decode(raw[0][0], 65536)
    keys = ("environment_id", "project_id", "sandbox", "sandbox_id", "state")
    if type(value) is not dict or any(
        type(value.get(k)) is not str or not 0 < len(value[k]) <= 128 for k in keys
    ):
        raise RefusalError("environment public scalar differs")
    expected = {
        "environment_id": original["environment_id"],
        "project_id": PROJECT,
        "sandbox": original["sandbox"],
        "sandbox_id": original["sandbox_id"],
    }
    if any(value[k] != item for k, item in expected.items()) or value["state"] != "stopped":
        raise RefusalError("environment original stopped binding differs")
    return {
        "worker": worker,
        "environment": {k: value[k] for k in keys},
        "raw_records_exported": False,
    }


class Driver:
    """Single finite controller lifetime; no replay or automatic retry admission."""

    def __init__(self, marker: dict[str, Any]) -> None:
        from sandbox_science.orchestrator.child_attempts import ChildAttempts
        from sandbox_science.orchestrator.delegation_records import DelegationRecords
        from sandbox_science.orchestrator.documents.store import DocumentStore
        from sandbox_science.orchestrator.projects import ProjectStore

        self.marker = marker
        self.active = True
        self.started = time.monotonic()
        self.owner = Owner()
        self.owner_generations = [self.owner]
        self.projects = ProjectStore(LAB / CONTROL)
        self.documents = DocumentStore(
            LAB / CONTROL / "control.sqlite3", producer="lab34-scripted-host"
        )
        try:
            self.records = DelegationRecords(self.documents)
            self.journal = ChildAttempts(
                self.documents, self.records, self.projects, initialize=True
            )
        except BaseException:
            self.documents.close()
            raise
        self.snapshots: dict[str, Any] = {}
        self.prepared: dict[str, Any] = {}
        self.authorities: dict[str, Any] = {}
        self.controllers: dict[str, Any] = {}
        self.models: dict[str, ScriptedChat] = {}
        self.tasks: dict[str, asyncio.Task[Any]] = {}
        self.bindings: dict[str, Any] = {}
        self.facts: dict[tuple[str, int], dict[str, Any]] = {}
        self.execution_ids: set[str] = set()
        self.clients: list[Any] = []
        self.session: Any = None
        self.model_calls = self.submits = self.accepted = self.controls = self.enrolls = (
            self.http_calls
        ) = 0
        self.original: dict[str, str] = {}
        self.suspender: asyncio.Task[Any] | None = None
        self.control_closed = False

    def withdraw(self) -> None:
        self.active = False

    def admitted(self, label: str) -> bool:
        if not self.active or time.monotonic() - self.started >= WHOLE_SECONDS:
            return False
        old = self.snapshots[label]
        current = self.projects.session_snapshot(PROJECT, SESSION)
        try:
            return (
                old.matches(current)
                and current.context.environment_id == self.marker["environment_id"]
            )
        finally:
            current.close()

    def context(self) -> Any:
        from sandbox_science.contracts.context import ExecutionContext

        return ExecutionContext(
            project_id=PROJECT,
            session_id=SESSION,
            environment_id=self.marker["environment_id"],
            policy_revision=self.marker["policy"],
        )

    async def effect(
        self, label: str, kind: str, call: Callable[[], Any], projection: Callable[[Any], object]
    ) -> Any:
        identifier(label)
        write_new(
            PREFIX + "intent-" + label + ".json",
            {**envelope(self.marker, label), "kind": kind, "attempt": 1},
        )
        value, interrupted = await owned(call(), self.withdraw)
        write_new(
            PREFIX + "receipt-" + label + ".json",
            {
                **envelope(self.marker, label),
                "kind": kind,
                "value": projection(value),
                "interrupted_after_admission": interrupted is not None,
            },
        )
        if interrupted is not None:
            raise interrupted
        return value

    async def open_clients(self) -> None:
        import aiohttp

        from sandbox_science.orchestrator.worker_client import WorkerClient

        _, pool, controller, ordinary = private_configuration()
        trace = aiohttp.TraceConfig()

        async def started(_session: Any, _context: Any, params: Any) -> None:
            path, method = params.url.path, params.method
            self.http_calls += 1
            if self.http_calls > 2048 or params.url.host != "127.0.0.1" or params.url.port != 8100:
                raise RefusalError("fixed actual HTTP count/scope differs")
            if method == "POST" and path in ("/v1/executions", "/v1/run-kernels/submit"):
                self.submits += 1
                if self.submits > MAX_SUBMITS or self.accepted >= MAX_CELLS:
                    raise RefusalError("finite submit budget exhausted")
            if method == "POST" and path in (
                "/v1/run-kernels/cancel",
                "/v1/run-kernels/close",
                "/v1/run-kernels/restart",
            ):
                self.controls += 1
                if self.controls > MAX_CONTROLS:
                    raise RefusalError("finite selected control budget exhausted")
            if method == "POST" and path == "/v1/run-kernels/enroll":
                self.enrolls += 1
                if self.enrolls > 2:
                    raise RefusalError("finite original enrollment budget exhausted")

        async def ended(_session: Any, _context: Any, params: Any) -> None:
            if (
                params.method == "POST"
                and params.url.path in ("/v1/executions", "/v1/run-kernels/submit")
                and params.response.status == 201
            ):
                self.accepted += 1

        trace.on_request_start.append(started)
        trace.on_request_end.append(ended)
        self.session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(sock_connect=10), trace_configs=[trace]
        )
        self.pool = WorkerClient("http://127.0.0.1:8100", pool, session=self.session)
        self.worker = WorkerClient("http://127.0.0.1:8100", controller, session=self.session)
        self.ordinary = WorkerClient("http://127.0.0.1:8100", ordinary, session=self.session)
        self.clients = [self.pool, self.worker, self.ordinary]

    async def main_cell(
        self, label: str, code: str, expected: int, *, shared: bool = True
    ) -> dict[str, object]:
        from sandbox_science.contracts.worker import CellRequest

        if type(code) is not str or len(code) > 1024:
            raise RefusalError("fixed MAIN source differs")
        request = CellRequest(
            operation_id="lab34-" + label,
            context=self.context(),
            code=code,
            timeout_seconds=90.0,
            output_limit_bytes=8192,
        )
        value = await self.effect(
            label,
            "ordinary-main-cell",
            lambda: self.pool.run(request),
            lambda v: {
                "execution_id": v.execution_id,
                "attempt_id": v.attempt_id,
                "state": v.state.value,
            },
        )
        if (
            value.state.value != "succeeded"
            or value.result is None
            or value.result.status != "ok"
            or value.result.stdout is None
            or value.result.stdout.truncated
            or value.result.stdout.total_bytes > 8192
            or (value.result.stderr and value.result.stderr.text)
        ):
            raise RefusalError("ordinary MAIN pilot did not succeed")
        fact = scalar_output(value.result.stdout.text, expected, shared=shared)
        self.execution_ids.add(value.execution_id)
        write_new(
            PREFIX + "main-" + label + ".json",
            {
                **envelope(self.marker, label),
                "execution": value.model_dump(mode="json"),
                "fixture_output": fact,
            },
        )
        return fact

    async def start_child(self, label: str) -> None:
        from sandbox_science.orchestrator.child_attempts import (
            AppliedBudget,
            AttemptAuthority,
            RequestedProfile,
        )
        from sandbox_science.orchestrator.child_controller import (
            AppliedChildModel,
            ChildConfiguration,
            ChildController,
        )
        from sandbox_science.orchestrator.child_tools import ChildToolSettings
        from sandbox_science.orchestrator.delegation_records import DelegationScope, ParentAuthority

        original = self.projects.session_snapshot(PROJECT, SESSION)
        self.snapshots[label] = original
        child = self.marker["children"][label]
        scope = DelegationScope(
            project_id=PROJECT, session_id=SESSION, environment_id=self.marker["environment_id"]
        )
        parent = ParentAuthority(scope=scope, parent_run_id=child["parent_run_id"])
        saved = self.records.get(parent, child["delegation_id"])
        if (
            saved.outcome is not None
            or saved.prepared.child_run_id != child["child_run_id"]
            or saved.prepared.request_sha256 != child["request_sha256"]
        ):
            raise RefusalError("canonical original prepared child differs")
        self.prepared[label] = saved.prepared
        authority = AttemptAuthority(
            scope=scope,
            parent_run_id=child["parent_run_id"],
            delegation_id=child["delegation_id"],
            child_run_id=child["child_run_id"],
            controller_id=self.marker["run_id"] + "-" + label,
        )
        self.authorities[label] = authority
        configuration = ChildConfiguration(
            RequestedProfile(
                profile_id="lab34-scripted",
                profile_sha256=hashlib.sha256(b"lab34-scripted-v1").hexdigest(),
                alias="lab34-scripted",
            ),
            AppliedBudget(
                max_calls=6,
                per_call_output_tokens=512,
                total_output_tokens=3072,
                cumulative_input_bytes=1048576,
                wall_seconds=1200,
            ),
            ChildToolSettings(("python",), 60.0, 8192, 20.0, 0.1, 512, 1),
        )
        model = ScriptedChat(self, label)
        controller = ChildController(
            self.journal,
            self.records,
            self.worker,
            AppliedChildModel(model, 512, None),
            configuration,
            plans=None,
        )
        self.models[label], self.controllers[label] = model, controller
        write_new(
            PREFIX + "intent-controller-" + label.lower() + ".json",
            {
                **envelope(self.marker, "controller-start"),
                "label": label,
                "child_run_id": child["child_run_id"],
                "model_kind": "scripted-no-provider",
                "tools": ["python"],
            },
        )
        self.tasks[label] = asyncio.create_task(
            controller.run(
                authority,
                saved.prepared,
                original,
                "lab34-controller-" + label.lower(),
                admitted=partial(self.admitted, label),
            )
        )

    async def gate(self, label: str, index: int) -> None:
        async with asyncio.timeout(120):
            event = asyncio.create_task(self.models[label].entered[index].wait())
            try:
                done, _ = await asyncio.wait(
                    (event, self.tasks[label]), return_when=asyncio.FIRST_COMPLETED
                )
                if self.tasks[label] in done:
                    with contextlib.suppress(BaseException):
                        self.tasks[label].result()
                    raise RefusalError("controller ended before fixed genuine model gate")
                await event
            finally:
                event.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await event

    def quiescent_http(self) -> None:
        from sandbox_science.worker.run_api import RUN_API

        if (
            self.worker._run_work
            or self.owner.runner.app[RUN_API]._work
            or self.owner.manager.run_dispatcher._controls
        ):
            raise RefusalError("worker detached with actual selected request/control in flight")

    async def observations(self, label: str) -> dict[str, object]:
        result = {}
        for child in ("A", "B"):
            binding = self.controllers[child]._binding
            if binding is None:
                raise RefusalError("actual controller did not retain enrollment")
            observation = await self.worker.observe_run(binding, timeout_seconds=20)
            if (
                observation.binding != binding
                or len(observation.kernels) != 1
                or observation.kernels[0].kernel.language != "python"
                or observation.kernels[0].birth is None
                or observation.kernels[0].lineage_status != "proved"
            ):
                raise RefusalError("actual selected birth/generation proof missing")
            self.bindings[child] = binding
            result[child] = observation.model_dump(mode="json")
        write_new(
            PREFIX + "observations-" + label + ".json",
            {**envelope(self.marker, label), "observations": result},
        )
        return result

    async def permission_refusals(self) -> None:
        from sandbox_science.orchestrator.worker_client import WorkerAPIError

        selected = self.facts[("A", 1)]["selected"]
        checks = (
            (
                "ordinary-selected",
                lambda: self.ordinary.observe_run(self.bindings["A"], timeout_seconds=20),
                403,
            ),
            (
                "pool-selected",
                lambda: self.pool.observe_run(self.bindings["A"], timeout_seconds=20),
                403,
            ),
            (
                "pool-legacy-get",
                lambda: self.pool.get(selected["execution_id"], project_id=PROJECT),
                404,
            ),
            (
                "restart-not-granted",
                lambda: self.worker.restart_run(
                    self.bindings["A"], "lab34-denied-restart", "python", timeout_seconds=20
                ),
                403,
            ),
        )
        facts = []
        for label, call, status in checks:
            try:
                await call()
            except WorkerAPIError as error:
                if error.status != status:
                    raise RefusalError("actual HTTP permission classification differs") from None
                facts.append({"check": label, "status": status, "error_kind": error.kind.value})
            else:
                raise RefusalError("ungranted HTTP path was allowed")
        write_new(
            PREFIX + "permission-refusals.json",
            {**envelope(self.marker, "permission-refusals"), "checks": facts},
        )

    async def stopped(self) -> None:
        from sandbox_science.orchestrator.worker_client import WorkerAPIError
        from sandbox_science.worker.suspend import Suspender

        environment_id = self.marker["environment_id"]
        old = self.owner.manager._environments.get(environment_id)
        if old is None or old.requests != 0:
            raise RefusalError("original idle owner not settled")
        self.quiescent_http()
        suspender = Suspender(self.owner.environments, self.owner.manager, 3.0, interval=1.0)
        self.suspender = asyncio.create_task(suspender.run())
        try:
            async with asyncio.timeout(120):
                while self.owner.environments.get(environment_id).state.value != "stopped":
                    await asyncio.sleep(0.25)
        finally:
            self.suspender.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.suspender
            self.suspender = None
        original = {
            "environment_id": self.original["environment_id"],
            "name": self.original["sandbox"],
            "id": self.original["sandbox_id"],
        }
        ready = stage_event(self.marker, "stopped-ready", original)
        write_new(PREFIX + "stopped-ready.json", ready)
        before_gateway = await wait_gateway(
            self.marker, self.original, "before", str(ready["recorded_at"])
        )
        connections = (
            self.owner.store._db,
            self.owner.environments.records._db,
            self.documents._db,
        )
        before_private = tuple(complete_sql_memory(db) for db in connections)
        before = public_snapshot(
            self.owner,
            self.marker,
            self.original,
            {label: binding.model_dump(mode="json") for label, binding in self.bindings.items()},
            self.execution_ids,
        )
        if (
            before["worker"]["executor_endpoints"] != []
            or before["environment"]["state"] != "stopped"
        ):
            raise RefusalError("actual endpoint retirement/stop differs")
        counts = dict.fromkeys(("SELECT", "BEGIN", "COMMIT", "ROLLBACK", "OTHER"), 0)

        def trace(sql: str) -> None:
            prefix = sql[:64].lstrip()
            verb = prefix.split(None, 1)[0].upper() if prefix else "OTHER"
            counts[verb if verb in counts else "OTHER"] += 1
            if sum(counts.values()) > 10000:
                self.withdraw()

        attached = []
        reads = []
        activity = old.last_used
        first = utc()
        try:
            for db in connections:
                db.set_trace_callback(trace)
                attached.append(db)
            for index in range(30):
                try:
                    await self.worker.observe_run(self.bindings["B"], timeout_seconds=20)
                except WorkerAPIError as error:
                    if error.kind.value != "unavailable" or error.status != 503:
                        raise RefusalError("stopped HTTP classification differs") from None
                else:
                    raise RefusalError("stopped original selected HTTP observation succeeded")
                reads.append({"read": index + 1, "status": 503, "error_kind": "unavailable"})
            last = utc()
        finally:
            for db in attached:
                db.set_trace_callback(None)
        after_private = tuple(complete_sql_memory(db) for db in connections)
        after = public_snapshot(
            self.owner,
            self.marker,
            self.original,
            {label: binding.model_dump(mode="json") for label, binding in self.bindings.items()},
            self.execution_ids,
        )
        unchanged = before_private == after_private
        del before_private, after_private
        if (
            not unchanged
            or before != after
            or old.last_used != activity
            or old.requests != 0
            or self.owner.manager._environments.get(environment_id) is not None
            or counts["OTHER"]
            or not self.active
        ):
            raise RefusalError("stopped passive HTTP changed SQL/activity/owner or non-read verb")
        done = stage_event(self.marker, "stopped-reads-done", original, first, last)
        write_new(PREFIX + "stopped-reads-done.json", done)
        after_gateway = await wait_gateway(
            self.marker, self.original, "after", str(done["recorded_at"])
        )
        if stamp(before_gateway["observed_at"]) > stamp(first) or stamp(
            after_gateway["observed_at"]
        ) < stamp(last):
            raise RefusalError("actual stopped proofs do not bracket reads")
        write_new(
            PREFIX + "stopped-result.json",
            {
                **envelope(self.marker, "stopped-result"),
                "before_gateway": before_gateway,
                "after_gateway": after_gateway,
                "before": before,
                "after": after,
                "reads": reads,
                "complete_sql_equal_in_memory": True,
                "private_sql_exported_or_hashed": False,
                "trace_verb_counts": counts,
                "sql_connection_scope": ["worker", "environment", "canonical-control"],
                "no_sql_writes_observed": True,
                "zero_all_sql_claim": False,
                "continuous_stopped_claim": False,
            },
        )

    async def run(self) -> dict[str, object]:
        from sandbox_science.contracts.environments import EnvironmentRequest

        await self.effect(
            "worker-open",
            "host-loopback-worker-open",
            self.owner.open,
            lambda _: {"loopback_http": True},
        )
        await self.open_clients()
        created = await self.effect(
            "environment-create",
            "ordinary-environment-create",
            lambda: self.pool.create_environment(
                EnvironmentRequest(
                    environment_id=self.marker["environment_id"],
                    project_id=PROJECT,
                    profile="minimal",
                    policy=self.marker["policy"],
                )
            ),
            lambda v: {
                "environment_id": v.environment_id,
                "project_id": v.project_id,
                "sandbox": v.sandbox,
                "sandbox_id": v.sandbox_id,
            },
        )
        if (
            created.environment_id != self.marker["environment_id"]
            or created.project_id != PROJECT
            or created.sandbox_id is None
        ):
            raise RefusalError("actual created managed binding differs")
        self.original = {
            "environment_id": identifier(created.environment_id),
            "sandbox": identifier(created.sandbox),
            "sandbox_id": identifier(created.sandbox_id),
        }
        original = stage_event(
            self.marker,
            "original-created",
            {
                "environment_id": self.original["environment_id"],
                "name": self.original["sandbox"],
                "id": self.original["sandbox_id"],
            },
        )
        write_new(PREFIX + "original.json", original)
        main = await self.main_cell(
            "main-pilot", fixture_code(101, shared=False), 101, shared=False
        )
        await self.start_child("A")
        await self.gate("A", 1)
        await self.start_child("B")
        await self.gate("B", 1)
        if (
            len(
                {
                    main["pid"],
                    self.facts[("A", 1)]["fixture_output"]["pid"],
                    self.facts[("B", 1)]["fixture_output"]["pid"],
                }
            )
            != 3
        ):
            raise RefusalError("three simultaneously live interpreters are not distinct")
        before = await self.observations("before-reconnect")
        await self.permission_refusals()
        self.quiescent_http()
        before_sql = tuple(
            complete_sql_memory(db)
            for db in (self.owner.store._db, self.owner.environments.records._db)
        )
        await self.effect(
            "worker-detach",
            "host-worker-detach-only",
            self.owner.close,
            lambda _: self.owner.cleanup,
        )
        self.owner = Owner()
        self.owner_generations.append(self.owner)
        await self.effect(
            "worker-reopen",
            "same-original-worker-reopen",
            self.owner.open,
            lambda _: {"same_sql_configuration": True, "daemon_detach_only": True},
        )
        # Same original context, no automatic controller enrollment after reconnect.
        after_sql = tuple(
            complete_sql_memory(db)
            for db in (self.owner.store._db, self.owner.environments.records._db)
        )
        # The ordinary manager may refresh bridge recovery state while reconnecting;
        # exact selected bindings/births and globals, rather than raw DB bytes, are authoritative here.
        reopen_sql_equal = before_sql == after_sql
        del before_sql, after_sql
        self.models["A"].release[1].set()
        self.models["B"].release[1].set()
        await self.gate("A", 2)
        await self.gate("B", 2)
        after = await self.observations("after-reconnect")
        for label in ("A", "B"):
            left, right = before[label], after[label]
            if (
                left["binding"] != right["binding"]
                or left["kernels"] != right["kernels"]
                or self.facts[(label, 1)]["fixture_output"]
                != self.facts[(label, 2)]["fixture_output"]
            ):
                raise RefusalError("same-daemon selected globals/birth/generation changed")
        write_new(
            PREFIX + "reconnect.json",
            {
                **envelope(self.marker, "reconnect"),
                "before": before,
                "after": after,
                "fixture_facts": {
                    label: [self.facts[(label, index)] for index in (1, 2)] for label in ("A", "B")
                },
                "same_daemon_proved": True,
                "raw_sql_equal_during_reopen": reopen_sql_equal,
            },
        )
        self.models["A"].release[2].set()
        async with asyncio.timeout(20):
            while True:
                pin = self.controllers["A"]._active_effect
                if pin is not None:
                    from sandbox_science.contracts.run_api import (
                        RunAttemptRequest,
                        RunEventsRequest,
                    )
                    from sandbox_science.worker.run_api import RUN_API

                    # Read exact owned metadata without competing with the
                    # controller's physical per-scope HTTP polling owner.
                    lane = self.owner.runner.app[RUN_API]
                    target = lane._get(
                        RunAttemptRequest(
                            binding=self.bindings["A"],
                            execution_id=pin.execution_id,
                            attempt_id=pin.attempt_id,
                        )
                    )
                    events = lane._events(
                        RunEventsRequest(
                            binding=self.bindings["A"],
                            execution_id=pin.execution_id,
                            attempt_id=pin.attempt_id,
                            after=0,
                            limit=32,
                        )
                    )
                    started = any(
                        event.type == "stream"
                        and event.name == "stdout"
                        and event.text == "lab34 started\n"
                        for event in events.events
                    )
                    if (
                        target.execution.state.value == "running"
                        and target.execution.kernel is not None
                        and started
                    ):
                        break
                    if target.execution.state.value in TERMINAL:
                        raise RefusalError("A sleep ended before exact cancel target")
                await asyncio.sleep(0.05)
        self.execution_ids.add(target.execution.execution_id)
        write_new(
            PREFIX + "cancel-target.json",
            {
                **envelope(self.marker, "cancel-target"),
                "selected": target.model_dump(mode="json"),
                "accepted_pin": pin.model_dump(mode="json"),
                "actual_started_events": events.model_dump(mode="json"),
                "target_observation": "bounded in-process exact selection; no extra competing HTTP poll",
            },
        )
        self.tasks["A"].cancel("lab34 original A owner cancellation")
        try:
            await self.tasks["A"]
        except asyncio.CancelledError as error:
            if error.args != ("lab34 original A owner cancellation",):
                raise RefusalError("original A cancellation identity changed") from None
        else:
            raise RefusalError("A owner cancellation did not propagate")
        state_a = self.journal.get(self.authorities["A"], self.controllers["A"]._attempt_id())
        if state_a.sealed is None or state_a.sealed.body.ended != "cancelled":
            raise RefusalError("A cancellation host ending not durable")
        close_a = cleanup_fact(
            state_a.messages,
            self.bindings["A"].model_dump(mode="json"),
            self.controllers["A"]._attempt_id(),
        )
        cancelled = lane._get(
            RunAttemptRequest(
                binding=self.bindings["A"], execution_id=pin.execution_id, attempt_id=pin.attempt_id
            )
        )
        if cancelled.execution.state.value != "cancelled":
            raise RefusalError("original A attempt cancellation did not settle")
        self.models["B"].release[2].set()
        await self.gate("B", 3)
        if self.facts[("B", 3)]["fixture_output"] != self.facts[("B", 1)]["fixture_output"]:
            raise RefusalError("B did not survive exact A cancellation")
        await self.main_cell("main-after-a", fixture_code(None), 101)
        write_new(
            PREFIX + "cancel-result.json",
            {
                **envelope(self.marker, "cancel-result"),
                "a_sealed": state_a.sealed.model_dump(mode="json"),
                "a_exact_close": close_a,
                "a_cancelled_execution": cancelled.model_dump(mode="json"),
                "b_fact": self.facts[("B", 3)],
                "whole_session_close_used": False,
                "original_cancel_propagated": True,
            },
        )
        await self.stopped()
        old_binding = self.bindings["B"]
        accepted_before = self.accepted
        resumed = await self.main_cell("main-resume", fixture_code(101), 101)
        fresh = self.owner.store.lineage_endpoint(self.marker["environment_id"])
        if (
            fresh is None
            or fresh[0] == old_binding.incarnation
            or (fresh[1], fresh[2]) != (self.original["sandbox"], self.original["sandbox_id"])
        ):
            raise RefusalError("ordinary resume did not replace daemon under same VM")
        before_count = self.owner.store._db.execute("SELECT count(*) FROM executions").fetchone()[0]
        self.models["B"].release[3].set()
        result_b = await self.tasks["B"]
        after_count = self.owner.store._db.execute("SELECT count(*) FROM executions").fetchone()[0]
        if (
            result_b.held is None
            or result_b.ending not in ("interrupted", "failed")
            or after_count != before_count
            or self.accepted != accepted_before + 1
            or self.enrolls != 2
        ):
            raise RefusalError("old B binding was repeated/re-enrolled or not held")
        write_new(
            PREFIX + "resume-refusal.json",
            {
                **envelope(self.marker, "resume-refusal"),
                "same_immutable_vm": True,
                "old_incarnation": old_binding.incarnation,
                "new_incarnation": fresh[0],
                "main_fixture": resumed,
                "child_result": {
                    "attempt_id": result_b.attempt_id,
                    "ending": result_b.ending,
                    "held": result_b.held,
                    "cleanup": result_b.cleanup,
                },
                "execution_count_before": before_count,
                "execution_count_after": after_count,
                "automatic_resubmit": False,
                "automatic_reenrollment": False,
            },
        )
        # Controllers have ended; exact owned VM deletion is an ordinary authorized
        # pool action, not guest cleanup inferred from transport timeouts.
        current = await self.pool.environment(self.original["environment_id"])
        if (current.sandbox, current.sandbox_id) != (
            self.original["sandbox"],
            self.original["sandbox_id"],
        ):
            raise RefusalError("original VM changed before owned deletion")
        deleted = await self.effect(
            "environment-delete",
            "ordinary-exact-owned-delete",
            lambda: self.pool.delete_environment(self.original["environment_id"]),
            lambda v: {
                "environment_id": v.environment_id,
                "sandbox": v.sandbox,
                "sandbox_id": v.sandbox_id,
                "state": v.state.value,
            },
        )
        if deleted.state.value != "deleted" or (deleted.sandbox, deleted.sandbox_id) != (
            self.original["sandbox"],
            self.original["sandbox_id"],
        ):
            raise RefusalError("original VM deletion not confirmed")
        return {
            **envelope(self.marker, "run-complete"),
            "status": "PASS",
            "scope": "scripted actual canonical controller and authenticated HTTP/OpenShell composition",
            "original_vm": self.original,
            "accepted_cells": self.accepted,
            "submit_requests": self.submits,
            "selected_controls": self.controls,
            "enrollments": self.enrolls,
            "scripted_model_calls": self.model_calls,
            "stopped_passive_observations": 30,
            "real_model_provider": False,
            "scientific_certification": False,
            "filesystem_isolation": False,
        }

    async def close(self) -> None:
        self.withdraw()
        failed = []
        if self.suspender is not None:
            self.suspender.cancel()
            try:
                await self.suspender
            except asyncio.CancelledError:
                pass
            except BaseException:
                failed.append("suspender")
        for name, controller in self.controllers.items():
            try:
                await controller.close()
            except BaseException:
                failed.append("controller-" + name)
        if not failed and self.tasks:
            await asyncio.gather(*tuple(self.tasks.values()), return_exceptions=True)
        for client in self.clients:
            try:
                await client.close()
            except BaseException:
                failed.append("http-client")
        if self.session is not None and not failed:
            try:
                await self.session.close()
            except BaseException:
                failed.append("http-session")
        for owner in reversed(self.owner_generations):
            try:
                await owner.close(close_stores=not failed)
            except BaseException:
                failed.append("worker-owner")
        if failed:
            raise RefusalError("actual owner drain failed; canonical stores held")
        close_errors = []
        for original in self.snapshots.values():
            try:
                original.close()
            except BaseException:
                close_errors.append("snapshot")
        try:
            self.documents.close()
        except BaseException:
            close_errors.append("control-store")
        if close_errors:
            raise RefusalError("canonical descriptor/store close failed")
        self.control_closed = True


async def run_fixture(head: str) -> dict[str, object]:
    marker = marked(head)
    lock = driver_lock()
    driver = None
    result = None
    interrupted = None
    loop = asyncio.get_running_loop()
    try:
        # Never rerun this whole effectful composition after any earlier intent.
        write_new(PREFIX + "run-intent.json", {**envelope(marker, "run-intent"), "attempt": 1})
        driver = Driver(marker)
        task = asyncio.create_task(driver.run())

        def stop() -> None:
            driver.withdraw()
            task.cancel("lab34 root interruption")

        for signum in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(signum, stop)
        try:
            async with asyncio.timeout(WHOLE_SECONDS):
                result = await task
        finally:
            _, interrupted = await owned(driver.close())
            write_new(
                PREFIX + "host-cleanup.json",
                {
                    **envelope(marker, "host-cleanup"),
                    "owners_drained": True,
                    "canonical_stores_closed": driver.control_closed,
                    "worker_generations": [owner.cleanup for owner in driver.owner_generations],
                    "infrastructure_cleanup": "root-owned separate exact teardown",
                },
            )
        if interrupted is not None:
            raise interrupted
        if result is None:
            raise RefusalError("actual run has no result")
        write_new(PREFIX + "result.json", result)
        return result
    finally:
        for signum in (signal.SIGTERM, signal.SIGINT):
            loop.remove_signal_handler(signum)
        os.close(lock)


def retained_inspection(
    marker: dict[str, Any],
    result: object,
    cleanup: object,
    original: object,
    deleted: object,
    stopped: object,
) -> None:
    """Retained receipt shape/count/qualification, not complete historical bracket or SQL-row revalidation."""
    common = {
        "version",
        "source_head",
        "helper_sha256",
        "run_id",
        "project_id",
        "phase",
        "recorded_at",
    }

    def check(value: object, phase: str, extra: set[str]) -> dict[str, Any]:
        if (
            type(value) is not dict
            or set(value) != common | extra
            or type(value["version"]) is not int
            or value["version"] != 1
            or value["phase"] != phase
            or any(
                value[key] != marker[key]
                for key in ("source_head", "helper_sha256", "run_id", "project_id")
            )
        ):
            raise RefusalError("retained public receipt shape/scope differs")
        stamp(value["recorded_at"])
        return value

    result = check(
        result,
        "run-complete",
        {
            "status",
            "scope",
            "original_vm",
            "accepted_cells",
            "submit_requests",
            "selected_controls",
            "enrollments",
            "scripted_model_calls",
            "stopped_passive_observations",
            "real_model_provider",
            "scientific_certification",
            "filesystem_isolation",
        },
    )
    cleanup = check(
        cleanup,
        "host-cleanup",
        {
            "owners_drained",
            "canonical_stores_closed",
            "worker_generations",
            "infrastructure_cleanup",
        },
    )
    deleted = check(deleted, "environment-delete", {"kind", "value", "interrupted_after_admission"})
    stopped = check(
        stopped,
        "stopped-result",
        {
            "before_gateway",
            "after_gateway",
            "before",
            "after",
            "reads",
            "complete_sql_equal_in_memory",
            "private_sql_exported_or_hashed",
            "trace_verb_counts",
            "sql_connection_scope",
            "no_sql_writes_observed",
            "zero_all_sql_claim",
            "continuous_stopped_claim",
        },
    )
    if result["status"] != "PASS" or any(
        result[key] is not False
        for key in ("real_model_provider", "scientific_certification", "filesystem_isolation")
    ):
        raise RefusalError("retained qualification differs")
    for key, minimum, maximum in (
        ("accepted_cells", 9, MAX_CELLS),
        ("submit_requests", 10, MAX_SUBMITS),
        ("selected_controls", 3, MAX_CONTROLS),
        ("enrollments", 2, 2),
        ("scripted_model_calls", 7, 12),
        ("stopped_passive_observations", 30, 30),
    ):
        if type(result[key]) is not int or not minimum <= result[key] <= maximum:
            raise RefusalError("retained actual finite count differs")
    if (
        type(original) is not dict
        or set(original) != common - {"phase"} | {"stage", "original"}
        or original["stage"] != "original-created"
        or type(original["version"]) is not int
        or original["version"] != 1
        or any(
            original[key] != marker[key]
            for key in ("source_head", "helper_sha256", "run_id", "project_id")
        )
    ):
        raise RefusalError("retained original producer differs")
    stamp(original["recorded_at"])
    vm = original["original"]
    if type(vm) is not dict or set(vm) != {"environment_id", "name", "id"}:
        raise RefusalError("retained original VM shape differs")
    for item in vm.values():
        identifier(item)
    wanted = {"environment_id": vm["environment_id"], "sandbox": vm["name"], "sandbox_id": vm["id"]}
    if (
        vm["environment_id"] != marker["environment_id"]
        or result["original_vm"] != wanted
        or deleted["value"] != {**wanted, "state": "deleted"}
        or deleted["interrupted_after_admission"] is not False
        or deleted["kind"] != "ordinary-exact-owned-delete"
    ):
        raise RefusalError("retained exact VM deletion differs")
    generations = cleanup["worker_generations"]
    if (
        cleanup["owners_drained"] is not True
        or cleanup["canonical_stores_closed"] is not True
        or type(generations) is not list
        or len(generations) != 2
    ):
        raise RefusalError("retained owner completion differs")
    for owner in generations:
        if (
            type(owner) is not dict
            or set(owner) != {"owners_drained", "failed_owners", "borrowed_stores_closed"}
            or owner["owners_drained"] is not True
            or owner["failed_owners"] != []
            or owner["borrowed_stores_closed"] is not True
        ):
            raise RefusalError("retained borrowed stores not drained/closed")
    if (
        stopped["complete_sql_equal_in_memory"] is not True
        or stopped["no_sql_writes_observed"] is not True
        or any(
            stopped[key] is not False
            for key in (
                "private_sql_exported_or_hashed",
                "zero_all_sql_claim",
                "continuous_stopped_claim",
            )
        )
        or stopped["before"] != stopped["after"]
    ):
        raise RefusalError("retained passive proof qualification differs")
    snapshot = stopped["before"]
    if (
        stopped["sql_connection_scope"] != ["worker", "environment", "canonical-control"]
        or type(snapshot) is not dict
        or set(snapshot) != {"worker", "environment", "raw_records_exported"}
        or snapshot["raw_records_exported"] is not False
        or type(snapshot["worker"]) is not dict
        or set(snapshot["worker"]) != set(TABLES)
        or snapshot["worker"]["executor_endpoints"] != []
        or snapshot["environment"] != {**wanted, "project_id": PROJECT, "state": "stopped"}
    ):
        raise RefusalError("retained public SQL scope/retirement differs")
    reads = stopped["reads"]
    if (
        type(reads) is not list
        or len(reads) != 30
        or any(
            type(row) is not dict
            or set(row) != {"read", "status", "error_kind"}
            or type(row["read"]) is not int
            or row["read"] != index + 1
            or type(row["status"]) is not int
            or row["status"] != 503
            or row["error_kind"] != "unavailable"
            for index, row in enumerate(reads)
        )
    ):
        raise RefusalError("retained passive actual reads differ")
    counts = stopped["trace_verb_counts"]
    if (
        type(counts) is not dict
        or set(counts) != {"SELECT", "BEGIN", "COMMIT", "ROLLBACK", "OTHER"}
        or any(type(n) is not int or not 0 <= n <= 10000 for n in counts.values())
        or counts["OTHER"] != 0
        or counts["SELECT"] == 0
    ):
        raise RefusalError("retained SQL verb proof differs")
    for phase in ("before", "after"):
        proof = stopped[phase + "_gateway"]
        if (
            type(proof) is not dict
            or set(proof)
            != {
                "version",
                "source_head",
                "helper_sha256",
                "run_id",
                "project_id",
                "phase",
                "observed_at",
                "sandboxes",
            }
            or type(proof["version"]) is not int
            or proof["version"] != 1
            or proof["phase"] != phase
            or any(
                proof[key] != marker[key]
                for key in ("source_head", "helper_sha256", "run_id", "project_id")
            )
            or proof["sandboxes"] != [{**vm, "phase": "Stopped"}]
        ):
            raise RefusalError("retained stopped gateway original differs")
        stamp(proof["observed_at"])


def inspect(head: str) -> dict[str, object]:
    marker = marked(head)
    result = decode(bounded(PREFIX + "result.json"))
    cleanup = decode(bounded(PREFIX + "host-cleanup.json"))
    original = decode(bounded(PREFIX + "original.json"))
    deleted = decode(bounded(PREFIX + "receipt-environment-delete.json"))
    stopped = decode(bounded(PREFIX + "stopped-result.json"))
    retained_inspection(marker, result, cleanup, original, deleted, stopped)
    return {
        **envelope(marker, "inspect"),
        "scope": "retained receipt shape/count/qualification only; no full historical bracket or SQL-row revalidation, startup, current VM/SQL authority or new effects",
        "result": result,
        "cleanup": cleanup,
    }


def describe() -> dict[str, object]:
    return {
        "helper": "Lab34 restricted child controller over HTTP/OpenShell",
        "admission_ready": ADMISSION_READY,
        "default": "describe-only; no application/configuration/model/worker effect",
        "commands": [
            "describe",
            "prepare --head EXACT --admit",
            "run --head EXACT --admit",
            "inspect --head EXACT --admit",
        ],
        "limits": {
            "accepted_cells": MAX_CELLS,
            "submit_requests": MAX_SUBMITS,
            "selected_controls": MAX_CONTROLS,
            "scripted_calls": 12,
            "stopped_observations": 30,
            "whole_seconds": WHOLE_SECONDS,
        },
        "runtime": "not performed by author; independent source/root admission and teardown required",
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
        kind = "cancelled" if isinstance(error, asyncio.CancelledError) else "held_unknown"
        print(
            json.dumps({"status": "HELD", "error_kind": kind, "reconcile_original_intents": True})
        )
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
