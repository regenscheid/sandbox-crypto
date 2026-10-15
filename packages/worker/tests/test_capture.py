# SPDX-License-Identifier: MIT
"""Fixed byte-helper bounds and actual HTTP publication ownership."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import sys
import threading
import time
import tracemalloc
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from aiohttp.test_utils import TestClient

from sandbox_science.contracts import (
    Execution,
    ExecutionContext,
    ExecutionState,
    PublishRequest,
    ReadFileRequest,
    Result,
    new_id,
)
from sandbox_science.orchestrator.worker_client import WorkerClient
from sandbox_science.testing import eventually
from sandbox_science.worker import capture
from sandbox_science.worker.app import create_app
from sandbox_science.worker.artifacts import Blobs, BlobWriter
from sandbox_science.worker.backends import LocalBackend
from sandbox_science.worker.errors import WorkerError
from sandbox_science.worker.manager import ExecutionManager
from sandbox_science.worker.settings import LocalBackendSettings
from sandbox_science.worker.store import Store

from .harness import view

pytestmark = pytest.mark.slow

CONTEXT = ExecutionContext(project_id="p1", session_id="s1", environment_id="env1")
TOKEN = "test-secret-capture-" + "x" * 32
AUTH = {"Authorization": f"Bearer {TOKEN}"}
ClientFactory = Callable[..., Awaitable[TestClient[Any, Any]]]


def parameters(root: Path, **changes: object) -> dict[str, Any]:
    return {
        "root": str(root),
        "session": "s1",
        "path": "x",
        "offset": 0,
        "length": 128,
        "max_bytes": 128,
        "hash": True,
        **changes,
    }


@pytest.fixture
def source(tmp_path: Path) -> Path:
    path = tmp_path / "sessions" / "s1"
    path.mkdir(parents=True)
    (path / "x").write_bytes(b"source\r\n\x00\xff")
    return tmp_path


def test_exact_hash_and_ranges_and_empty(source: Path) -> None:
    data = (source / "sessions/s1/x").read_bytes()
    result = capture.read(parameters(source, offset=2, length=4))
    assert base64.b64decode(result["content_b64"]) == data[2:6]
    assert result["sha256"] == hashlib.sha256(data).hexdigest()
    later = capture.read(parameters(source, offset=6, length=128, hash=False))
    assert base64.b64decode(later["content_b64"]) == data[6:]
    assert later["stamp"] == result["stamp"]
    assert later["eof"] is True
    (source / "sessions/s1/x").write_bytes(b"")
    empty = capture.read(parameters(source, max_bytes=0))
    assert empty["size"] == 0
    assert empty["content_b64"] == ""
    assert empty["eof"] is True
    assert empty["sha256"] == hashlib.sha256(b"").hexdigest()


def test_sparse_oversize_refused_before_any_read(
    source: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with (source / "sessions/s1/x").open("wb") as handle:
        handle.truncate(1 << 30)

    def forbidden(*args: object) -> bytes:
        raise AssertionError("oversize source must not be read")

    monkeypatch.setattr(os, "read", forbidden)
    with pytest.raises(capture.CaptureError, match="exceeds the byte limit"):
        capture.read(parameters(source))


def test_growth_hash_consumes_only_cap_plus_one(
    source: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = os.read
    reads: list[int] = []

    def growing(fd: int, count: int) -> bytes:
        if not reads:
            with (source / "sessions/s1/x").open("ab") as handle:
                handle.write(b"z" * 4096)
        reads.append(count)
        return original(fd, count)

    monkeypatch.setattr(os, "read", growing)
    with pytest.raises(capture.CaptureError, match="grew beyond"):
        capture.read(parameters(source, max_bytes=32))
    assert sum(reads) == 33


@pytest.mark.parametrize("entry", ["fifo", "symlink", "parent", "directory"])
def test_special_and_symlink_sources_refused(source: Path, entry: str) -> None:
    file = source / "sessions/s1/x"
    file.unlink()
    if entry == "fifo":
        os.mkfifo(file)
    elif entry == "symlink":
        file.symlink_to("/dev/zero")
    elif entry == "directory":
        file.mkdir()
    else:
        directory = source / "sessions/s1"
        directory.rmdir()
        directory.symlink_to(source)
    with pytest.raises((capture.CaptureError, OSError)):
        capture.read(parameters(source))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("offset", True),
        ("offset", 1.0),
        ("max_bytes", -1),
        ("max_bytes", (1 << 30) + 1),
        ("length", (8 << 20) + 1),
        ("hash", 1),
        ("path", "../x"),
        ("path", "x\x00"),
        ("path", "x/" * 129),
        ("session", "../s1"),
        ("extra", "x"),
    ],
)
def test_metadata_strictly_bounded(source: Path, field: str, value: object) -> None:
    with pytest.raises(capture.CaptureError):
        capture.read(parameters(source, **{field: value}))


@pytest.mark.asyncio
async def test_actual_isolated_helper_has_no_workload_import_or_env(
    source: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Neither sitecustomize nor current-directory imports may run in the helper.
    marker = source / "imported"
    (source / "sitecustomize.py").write_text(f"open({str(marker)!r}, 'w').write('bad')")
    monkeypatch.setenv("PYTHONPATH", str(source))
    monkeypatch.setenv("LD_PRELOAD", "/obviously-synthetic-missing-library")
    pool = capture.Pool(source)
    try:
        result = await pool.read(
            "cap-" + "1" * 24, {k: v for k, v in parameters(source).items() if k != "root"}, 5
        )
        assert result["size"] == 10
        assert not marker.exists()
        with pytest.raises(capture.CaptureError, match="already used"):
            await pool.read(
                "cap-" + "1" * 24, {k: v for k, v in parameters(source).items() if k != "root"}, 5
            )
        await pool.cancel("cap-" + "2" * 24)
        with pytest.raises(capture.CaptureError, match="cancelled"):
            await pool.read(
                "cap-" + "2" * 24, {k: v for k, v in parameters(source).items() if k != "root"}, 5
            )
    finally:
        await pool.close()


@pytest.mark.asyncio
async def test_double_cancel_during_spawn_retains_slot_until_reap(
    source: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_spawn = asyncio.create_subprocess_exec
    created = asyncio.Event()
    release = asyncio.Event()
    processes: list[asyncio.subprocess.Process] = []

    async def delayed(*args: object, **kwargs: Any) -> asyncio.subprocess.Process:
        process = await real_spawn(
            sys.executable, "-I", "-S", "-c", "import time; time.sleep(60)", **kwargs
        )
        processes.append(process)
        created.set()
        await release.wait()
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", delayed)
    pool = capture.Pool(source)
    data = {k: v for k, v in parameters(source).items() if k != "root"}
    task = asyncio.create_task(pool.read("cap-" + "3" * 24, data, 5))
    try:
        await created.wait()
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        assert len(pool.work) == 1
        assert processes[0].returncode is None
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert processes[0].returncode is not None
        assert not pool.work
    finally:
        release.set()
        await pool.close()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_budget_kills_fixed_helper_and_malformed_output_is_bounded(
    source: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_spawn = asyncio.create_subprocess_exec
    processes: list[asyncio.subprocess.Process] = []
    script = "import signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); time.sleep(60)"

    async def replacement(*args: object, **kwargs: Any) -> asyncio.subprocess.Process:
        process = await real_spawn(sys.executable, "-I", "-S", "-c", script, **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", replacement)
    pool = capture.Pool(source)
    data = {k: v for k, v in parameters(source).items() if k != "root"}
    try:
        with pytest.raises(capture.CaptureError, match="budget expired"):
            await pool.read("cap-" + "4" * 24, data, 0.1)
        assert processes[-1].returncode is not None
        script = "import os; os.write(1,b'x'*1000000)"
        with pytest.raises(capture.CaptureError, match="reply is too large"):
            await pool.read("cap-" + "5" * 24, data, 5)
        assert processes[-1].returncode is not None
    finally:
        await pool.close()


@pytest_asyncio.fixture
async def service(
    aiohttp_client: ClientFactory, tmp_path: Path
) -> AsyncIterator[tuple[TestClient[Any, Any], ExecutionManager, Path]]:
    store = Store(tmp_path / "worker.sqlite3")
    manager = ExecutionManager(
        store,
        LocalBackend(LocalBackendSettings(), tmp_path),
        Blobs(tmp_path / "artifacts"),
        pool_id="capture-test",
        max_artifact_size=20 << 20,
        state_dir=tmp_path,
    )
    await manager.start()
    client = await aiohttp_client(create_app(manager, TOKEN))
    path = tmp_path / "environments/env1/project/sessions/s1"
    path.mkdir(parents=True, exist_ok=True)
    (path / "x").write_bytes(b"exact\r\n\x00\xff")
    try:
        yield client, manager, path
    finally:
        await manager.close()
        store.close()


@pytest.mark.asyncio
async def test_real_http_publish_transfer_and_ordinary_read(
    service: tuple[TestClient[Any, Any], ExecutionManager, Path],
) -> None:
    http, manager, path = service
    async with WorkerClient(str(http.make_url("")), TOKEN) as client:
        request = PublishRequest(context=CONTEXT, path="x")
        artifact = await client.publish_snapshot(request)
        data = (path / "x").read_bytes()
        assert artifact.sha256 == hashlib.sha256(data).hexdigest()
        assert await client.verified_artifact_content(artifact) == data
        before = await client.read_file(ReadFileRequest(context=CONTEXT, path="x"))
        assert before.size == len(data)
        channel = view(manager).channel("env1")
        channel.hello.pop("artifact_capture_protocol")
        response = await http.post(
            "/v1/artifacts", json=request.model_dump(mode="json"), headers=AUTH
        )
        assert response.status == 422
        assert (await response.json())["error"]["kind"] == "unsupported"
        assert await client.read_file(ReadFileRequest(context=CONTEXT, path="x")) == before
        assert await client.verified_artifact_content(artifact) == data


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["sparse", "fifo", "symlink"])
async def test_http_oversize_and_special_file_refusals_keep_original_artifact(
    service: tuple[TestClient[Any, Any], ExecutionManager, Path], kind: str
) -> None:
    http, manager, path = service
    async with WorkerClient(str(http.make_url("")), TOKEN) as client:
        original = await client.publish_snapshot(PublishRequest(context=CONTEXT, path="x"))
        data = (path / "x").read_bytes()
        (path / "x").unlink()
        if kind == "sparse":
            with (path / "x").open("wb") as handle:
                handle.truncate(1 << 30)
        elif kind == "fifo":
            os.mkfifo(path / "x")
        else:
            (path / "x").symlink_to("/dev/zero")
        response = await http.post(
            "/v1/artifacts", json={"context": CONTEXT.model_dump(), "path": "x"}, headers=AUTH
        )
        assert response.status == 400
        assert await client.verified_artifact_content(original) == data
        assert list((manager.blobs.root / "tmp").iterdir()) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("corrupt", ["size", "digest", "offset", "stamp", "eof", "huge_base64"])
async def test_http_capture_metadata_refusal_before_allocation(
    service: tuple[TestClient[Any, Any], ExecutionManager, Path],
    monkeypatch: pytest.MonkeyPatch,
    corrupt: str,
) -> None:
    http, manager, _ = service
    await manager.capabilities("env1")
    channel = view(manager).channel("env1")
    original = channel.request
    giant = "x" * (12 << 20)

    async def hostile(op: str, **fields: Any) -> dict[str, Any]:
        reply = await original(op, **fields)
        if op == "capture_read":
            reply.update(
                {
                    "size": {"size": True},
                    "digest": {"sha256": "X" * 64},
                    "offset": {"offset": 1.0},
                    "stamp": {"stamp": "0" * 65},
                    "eof": {"eof": 1},
                    "huge_base64": {"content_b64": giant},
                }[corrupt]
            )
        return reply

    monkeypatch.setattr(channel, "request", hostile)
    tracemalloc.start()
    try:
        response = await http.post(
            "/v1/artifacts", json={"context": CONTEXT.model_dump(), "path": "x"}, headers=AUTH
        )
        assert response.status == 500
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < 3 << 20
    assert list((manager.blobs.root / "tmp").iterdir()) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("lifecycle", ["close", "release"])
async def test_cancelled_blocked_blob_thread_retains_fd_admission_and_shutdown(
    service: tuple[TestClient[Any, Any], ExecutionManager, Path],
    monkeypatch: pytest.MonkeyPatch,
    lifecycle: str,
) -> None:
    _, manager, _ = service
    writing = threading.Event()
    release = threading.Event()
    original = BlobWriter.write
    closed: list[bool] = []

    def blocked(writer: BlobWriter, data: bytes) -> None:
        writing.set()
        release.wait(10)
        closed.append(writer._file.closed)
        original(writer, data)

    monkeypatch.setattr(BlobWriter, "write", blocked)
    task = asyncio.create_task(manager.publish(PublishRequest(context=CONTEXT, path="x")))
    closing: asyncio.Task[None] | None = None
    try:
        while not writing.is_set():
            await asyncio.sleep(0.01)
        assert await asyncio.to_thread(lambda: "default-pool-free") == "default-pool-free"
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        closing = asyncio.create_task(
            manager.close() if lifecycle == "close" else manager.release("env1")
        )
        await asyncio.sleep(0.02)
        assert not task.done()
        assert not closing.done()
        assert view(manager).captures() == 1
        with pytest.raises(WorkerError, match="admission"):
            await manager.publish(PublishRequest(context=CONTEXT, path="x"))
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        await closing
        assert closed == [False]
        assert not view(manager).captures()
        assert not view(manager).capture_counts()
        assert list((manager.blobs.root / "tmp").iterdir()) == []
    finally:
        release.set()
        await asyncio.gather(task, *([closing] if closing else []), return_exceptions=True)


@pytest.mark.asyncio
async def test_http_disconnect_does_not_claim_remote_cancellation(
    service: tuple[TestClient[Any, Any], ExecutionManager, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A client that goes away while the sandbox reads cancels the capture, and the worker
    counts the capture until the sandbox acknowledges its cleanup: it never claims the
    sandbox's read stopped because the HTTP request did.

    Each step waits on the one before, not on a clock. The client leaves once the read
    has begun, and the sandbox's acknowledgement waits for the test. An earlier version
    left on a 50 ms client timeout: a runner slow to connect or to make the capture's
    blob writer (in a thread pool) let the timeout pass before the read began, so the
    worker rightly gave the capture up unread and the test waited in vain (CI, slow tier,
    2026-10-15). The capture's budget is long, so it never ends the capture here."""
    import aiohttp

    http, manager, _ = service
    await manager.capabilities("env1")
    view(manager).set_capture(timeout=600)
    channel = view(manager).channel("env1")
    original = channel.request
    reading, cancel_asked = asyncio.Event(), asyncio.Event()
    release, acknowledged = asyncio.Event(), asyncio.Event()

    async def held(op: str, **fields: Any) -> dict[str, Any]:
        if op == "capture_read":
            reading.set()
            reply = await original(op, **fields)
            await acknowledged.wait()  # the sandbox's read lasts until the cleanup
            return reply
        if op == "capture_cancel":
            cancel_asked.set()
            await release.wait()
            reply = await original(op, **fields)
            acknowledged.set()
            return reply
        return await original(op, **fields)

    monkeypatch.setattr(channel, "request", held)
    session = aiohttp.ClientSession()
    posting = asyncio.create_task(
        session.post(
            http.make_url("/v1/artifacts"),
            json={"context": CONTEXT.model_dump(), "path": "x"},
            headers=AUTH,
        )
    )
    try:
        await asyncio.wait_for(reading.wait(), 60)
        posting.cancel()  # the client goes away while the sandbox reads
        with pytest.raises(asyncio.CancelledError):
            await posting
    finally:
        await session.close()
    await asyncio.wait_for(cancel_asked.wait(), 60)
    # Asked to clean up, and not yet acknowledged: the capture is still the worker's.
    assert view(manager).captures() == 1
    release.set()
    await asyncio.wait_for(acknowledged.wait(), 60)
    await eventually(lambda: view(manager).captures() == 0, timeout=60)
    assert manager.store._db.execute("SELECT count(*) FROM artifacts").fetchone()[0] == 0
    assert list((manager.blobs.root / "tmp").iterdir()) == []


@pytest.mark.asyncio
async def test_two_helpers_reserve_before_spawn_and_no_reap_no_capacity(
    source: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_spawn = asyncio.create_subprocess_exec
    release = asyncio.Event()
    processes: list[asyncio.subprocess.Process] = []

    async def delayed(*args: object, **kwargs: Any) -> asyncio.subprocess.Process:
        process = await real_spawn(
            sys.executable, "-I", "-S", "-c", "import time; time.sleep(60)", **kwargs
        )
        processes.append(process)
        await release.wait()
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", delayed)
    pool = capture.Pool(source)
    data = {k: v for k, v in parameters(source).items() if k != "root"}
    tasks = [asyncio.create_task(pool.read("cap-" + str(i) * 24, data, 5)) for i in (6, 7)]
    try:
        while len(processes) < 2:
            await asyncio.sleep(0.01)
        with pytest.raises(capture.CaptureError, match="admission"):
            await pool.read("cap-" + "8" * 24, data, 5)
        closing = asyncio.create_task(pool.close())
        await asyncio.sleep(0)
        closing.cancel()
        await asyncio.sleep(0)
        closing.cancel()
        await asyncio.sleep(0)
        assert len(pool.work) == 2
        assert not closing.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await closing
        assert all(process.returncode is not None for process in processes)
        assert not pool.work
    finally:
        release.set()
        await pool.close()
        await asyncio.gather(*tasks, return_exceptions=True)


def execution(context: ExecutionContext = CONTEXT, paths: tuple[str, ...] = ("x",)) -> Execution:
    return Execution(
        execution_id=new_id("exe"),
        attempt_id=new_id("att"),
        operation_id=new_id("op"),
        kind="shell",
        context=context,
        state=ExecutionState.SUCCEEDED,
        submitted_at=datetime.now(UTC),
        ended_at=datetime.now(UTC),
        result=Result(written_files=paths),
    )


def test_producer_hint_scoped_bounded_and_unknown_after_horizon(tmp_path: Path) -> None:
    store = Store(tmp_path / "worker.sqlite3")
    try:
        old = execution()
        store.insert_execution(old, "0" * 64)
        args = ("p1", "env1", "s1", "x", time.monotonic() + 10)
        assert store.producer_hint(*args) == old.attempt_id
        other = execution(CONTEXT.model_copy(update={"project_id": "p2"}))
        store.insert_execution(other, "0" * 64)
        other_session = execution(CONTEXT.model_copy(update={"session_id": "s2"}))
        store.insert_execution(other_session, "0" * 64)
        assert store.producer_hint(*args) == old.attempt_id
        for _ in range(256):
            store.insert_execution(execution(paths=()), "0" * 64)
        assert store.producer_hint(*args) is None
    finally:
        store.close()


def test_producer_hint_never_spools_large_valid_execution_records(tmp_path: Path) -> None:
    store = Store(tmp_path / "worker.sqlite3")
    try:
        for _ in range(64):
            item = execution()
            data = item.model_dump(mode="json")
            data["result"]["stdout"] = {"name": "stdout", "text": "x" * (512 << 10)}
            store.insert_execution(item, "0" * 64)
            store._db.execute(
                "UPDATE executions SET record=? WHERE execution_id=?",
                (json.dumps(data), item.execution_id),
            )
        queries: list[str] = []
        store._db.set_trace_callback(queries.append)
        assert store._producer_hint(store._db, "p1", "env1", "s1", "x", False) is None
        select = next(query for query in queries if query.startswith("WITH recent"))
        plan = store._db.execute("EXPLAIN QUERY PLAN " + select).fetchall()
        materialized = [row[3] for row in plan if "MATERIALIZE" in row[3]]
        assert materialized == ["MATERIALIZE recent"]  # Only its <=256 integer row IDs.
        assert "SELECT rowid AS execution_row" in select
        assert "<= 65536" in select
    finally:
        store.close()


@pytest.mark.parametrize("corrupt", ["record", "attempt", "ended", "context"])
def test_producer_hint_corrupt_large_metadata_never_allocated(tmp_path: Path, corrupt: str) -> None:
    store = Store(tmp_path / "worker.sqlite3")
    try:
        item = execution()
        store.insert_execution(item, "0" * 64)
        data = item.model_dump(mode="json")
        if corrupt == "record":
            data["large"] = "x" * (20 << 20)
        elif corrupt == "attempt":
            data["attempt_id"] = "x" * (2 << 20)
        elif corrupt == "ended":
            data["ended_at"] = "x" * (2 << 20)
        else:
            data["context"]["environment_id"] = "foreign"
        store._db.execute("UPDATE executions SET record = ?", (json.dumps(data),))
        tracemalloc.start()
        try:
            assert store.producer_hint("p1", "env1", "s1", "x", time.monotonic() + 5) is None
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        assert peak < 128 << 10
    finally:
        store.close()
