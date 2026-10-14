# SPDX-License-Identifier: MIT
"""Lost transports never substitute for actual capture cleanup/admission proof."""

from __future__ import annotations

import asyncio
import sqlite3
import threading
import tracemalloc
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest

from sandbox_science.contracts import ErrorKind, ExecutionContext, PublishRequest, new_id
from sandbox_science.worker import capture
from sandbox_science.worker.artifacts import Blobs
from sandbox_science.worker.backends import LocalBackend
from sandbox_science.worker.errors import ExecutorRefusalError, WorkerError
from sandbox_science.worker.manager import ExecutionManager
from sandbox_science.worker.settings import LocalBackendSettings
from sandbox_science.worker.store import Store

CONTEXT = ExecutionContext(project_id="p1", session_id="s1", environment_id="env1")


def manager(store: Store, root: Path, *, daemon: bool = False) -> ExecutionManager:
    return ExecutionManager(
        store,
        LocalBackend(LocalBackendSettings(daemon=daemon), root),
        Blobs(root / "artifacts"),
        pool_id="capture-ownership",
        max_artifact_size=1024,
        state_dir=root,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("refusal", [False, True])
async def test_lost_bridge_and_generic_refusal_hold_durable_capacity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    refusal: bool,
) -> None:
    store = Store(tmp_path / "worker.sqlite3")
    first = manager(store, tmp_path)
    second: ExecutionManager | None = None
    await first.start()
    await first.capabilities("env1")
    channel = first._environments["env1"].channel
    assert channel is not None
    original = channel.request
    attempts: list[str] = []

    async def lost(op: str, **fields: Any) -> dict[str, Any]:
        if op in ("capture_read", "capture_cancel"):
            attempts.append(op)
            lease = next(
                item for item in store.capture_leases() if item.capture_id == fields["capture_id"]
            )
            assert lease.incarnation == channel.incarnation
            assert lease.capture_id == fields["capture_id"]
            if op == "capture_read" and refusal:
                raise ExecutorRefusalError(ErrorKind.INTERNAL, "generic failure without proof")
            raise WorkerError(ErrorKind.UNAVAILABLE, "synthetic lost bridge")
        return await original(op, **fields)

    monkeypatch.setattr(channel, "request", lost)
    try:
        for _ in range(2):
            with pytest.raises(WorkerError, match=r"unknown.*held"):
                await first.publish(PublishRequest(context=CONTEXT, path="x"))
        assert attempts == ["capture_read", "capture_cancel"] * 2
        assert not first._captures  # Host work drained; guest admission is still held.
        assert first._capture_counts == {"env1": 2}
        assert all(item.phase == "held" for item in store.capture_leases())
        with pytest.raises(WorkerError, match="admission"):
            await first.publish(PublishRequest(context=CONTEXT, path="x"))
        await first.close()
        store.close()
        store = Store(tmp_path / "worker.sqlite3")
        second = manager(store, tmp_path)
        await second.start()
        assert second._capture_counts == {"env1": 2}
        with pytest.raises(WorkerError, match="admission"):
            await second.publish(PublishRequest(context=CONTEXT, path="x"))
        assert len(store.capture_leases()) == 2
    finally:
        if second is not None:
            await second.close()
        await first.close()
        store.close()


@pytest.mark.asyncio
async def test_restart_same_daemon_exact_ack_reconciles_only_held_rows(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = Store(tmp_path / "worker.sqlite3")
    first = manager(store, tmp_path, daemon=True)
    second: ExecutionManager | None = None
    await first.start()
    await first.capabilities("env1")
    channel = first._environments["env1"].channel
    assert channel is not None
    incarnation = channel.incarnation
    original = channel.request

    async def lost(op: str, **fields: Any) -> dict[str, Any]:
        if op in ("capture_read", "capture_cancel"):
            raise WorkerError(ErrorKind.UNAVAILABLE, "synthetic lost reply")
        return await original(op, **fields)

    monkeypatch.setattr(channel, "request", lost)
    try:
        with pytest.raises(WorkerError, match="unknown"):
            await first.publish(PublishRequest(context=CONTEXT, path="x"))
        # Another worker's active host work cannot be reconciled by any guest ACK.
        active = store.reserve_capture(new_id("pub"), "env1")
        store.dispatch_capture(active, incarnation, new_id("cap"))
        await first.close()
        second = manager(store, tmp_path, daemon=True)
        await second.start()
        recovered = second._environments["env1"].channel
        assert recovered is not None
        assert recovered.incarnation == incarnation
        assert [item.phase for item in store.capture_leases()] == ["active"]
        assert second._capture_counts == {"env1": 1}
    finally:
        if second is not None:
            await second.release("env1")
            await second.close()
        else:
            monkeypatch.setattr(channel, "request", original)
            await first.release("env1")
        await first.close()
        store.close()


@pytest.mark.asyncio
async def test_cancel_ack_deadline_includes_actual_channel_write_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = Store(tmp_path / "worker.sqlite3")
    worker = ExecutionManager(
        store,
        LocalBackend(LocalBackendSettings(daemon=False), tmp_path),
        Blobs(tmp_path / "artifacts"),
        pool_id="capture-ownership",
        max_artifact_size=1024,
        state_dir=tmp_path,
        capture_ack_timeout=0.05,
    )
    await worker.start()
    await worker.capabilities("env1")
    channel = worker._environments["env1"].channel
    assert channel is not None
    dispatched = asyncio.Event()
    dispatch = store.dispatch_capture

    def dispatch_and_tell(*args: Any, **kwargs: Any) -> Any:
        lease = dispatch(*args, **kwargs)
        dispatched.set()
        return lease

    monkeypatch.setattr(store, "dispatch_capture", dispatch_and_tell)
    await channel._write_lock.acquire()
    try:
        publish = asyncio.create_task(worker.publish(PublishRequest(context=CONTEXT, path="x")))
        await asyncio.wait_for(dispatched.wait(), 10)
        # The read waits for the write lock; its cleanup request has to wait for it too.
        publish.cancel()
        async with asyncio.timeout(10):  # a hang guard only
            with pytest.raises(WorkerError, match="unknown"):
                await publish
        assert not worker._captures
        assert len(store.capture_leases()) == 1
        assert store.capture_leases()[0].phase == "held"
        assert not channel._pending
    finally:
        channel._write_lock.release()
        await worker.close()
        store.close()


def test_atomic_admission_across_connections_and_reused_helper_id(tmp_path: Path) -> None:
    path = tmp_path / "worker.sqlite3"
    store = Store(path)
    try:
        for index in range(3):
            store.reserve_capture(new_id("pub"), f"env{index}")
        barrier = threading.Barrier(2)

        def reserve(environment: str) -> bool:
            other = Store(path)
            try:
                barrier.wait(5)
                try:
                    other.reserve_capture(new_id("pub"), environment)
                    return True
                except ValueError:
                    return False
            finally:
                other.close()

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(reserve, f"env{index}") for index in (4, 5)]
            assert sorted(future.result(10) for future in futures) == [False, True]
        assert len(store.capture_leases()) == 4
        one, two = store.capture_leases()[:2]
        helper = new_id("cap")
        store.dispatch_capture(one, "inc-" + "1" * 24, helper)
        with pytest.raises(sqlite3.IntegrityError):
            store.dispatch_capture(two, "inc-" + "1" * 24, helper)
    finally:
        store.close()


def test_vm_fence_matches_original_binding_and_never_frees_active_host_owner(
    tmp_path: Path,
) -> None:
    store = Store(tmp_path / "worker.sqlite3")
    try:
        lease = store.reserve_capture(new_id("pub"), "env1")
        lease = store.dispatch_capture(
            lease, "inc-" + "1" * 24, new_id("cap"), ("ss-old", "immutable-old")
        )
        store.fenced_captures("env1", lease.incarnation, "ss-old", "replacement")
        assert store.capture_leases() == (lease,)
        store.fenced_captures("env1", "inc-" + "2" * 24, "ss-old", "immutable-old")
        assert store.capture_leases() == (lease,)
        store.fenced_captures("env1", lease.incarnation, "ss-old", "immutable-old")
        assert len(store.capture_leases()) == 1  # Guest clean; real host task still owns admission.
        assert store.capture_leases()[0].guest_clean == 1
        assert store.finish_capture(lease, certain=False)
        assert not store.capture_leases()
    finally:
        store.close()


def test_endpoint_binding_cannot_be_combined_with_another_incarnation(tmp_path: Path) -> None:
    store = Store(tmp_path / "worker.sqlite3")
    try:
        incarnation = "inc-" + "1" * 24
        store.remember_endpoint("env1", incarnation, ("ss-original", "immutable-original"))
        assert store.capture_binding("env1", incarnation) == ("ss-original", "immutable-original")
        store.remember_endpoint("env1", "inc-" + "2" * 24, ("ss-replacement", "new-id"))
        with pytest.raises(ValueError, match="incarnation changed"):
            store.capture_binding("env1", incarnation)
        assert not store.capture_leases()
    finally:
        store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("proof", ["wrong-id", "false", "missing"])
async def test_reconnect_ack_requires_exact_helper_and_explicit_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, proof: str
) -> None:
    store = Store(tmp_path / "worker.sqlite3")
    worker = manager(store, tmp_path)
    await worker.start()
    await worker.capabilities("env1")
    channel = worker._environments["env1"].channel
    assert channel is not None
    lease = store.reserve_capture(new_id("pub"), "env1")
    lease = store.dispatch_capture(lease, channel.incarnation, new_id("cap"))
    store.finish_capture(lease, certain=False)
    expected = store.capture_leases()

    async def forged(op: str, **fields: Any) -> dict[str, Any]:
        assert op == "capture_cancel"
        return {
            "capture_id": new_id("cap") if proof == "wrong-id" else fields["capture_id"],
            **({} if proof == "missing" else {"cleaned": proof != "false"}),
        }

    original = channel.request
    monkeypatch.setattr(channel, "request", forged)
    try:
        await worker._reconcile_captures("env1", channel)
        assert store.capture_leases() == expected
        monkeypatch.setattr(channel, "request", original)
        # A fresh incarnation never provides proof for the old tuple.
        old_incarnation, channel.incarnation = channel.incarnation, new_id("inc")
        await worker._reconcile_captures("env1", channel)
        channel.incarnation = old_incarnation
        assert store.capture_leases() == expected
    finally:
        monkeypatch.setattr(channel, "request", original)
        await worker.close()
        store.close()


@pytest.mark.parametrize("column", ["guest_clean", "phase", "environment_id", "sandbox_id"])
def test_corrupt_lease_metadata_refuses_before_large_allocation(
    tmp_path: Path, column: str
) -> None:
    store = Store(tmp_path / "worker.sqlite3")
    try:
        store.reserve_capture(new_id("pub"), "env1")
        # The parameterized test fixes this identifier; values remain SQL parameters.
        store._db.execute(
            f"UPDATE artifact_capture_leases SET {column}=?",
            ("x" * (20 << 20),),
        )
        tracemalloc.start()
        try:
            with pytest.raises(ValueError, match="corrupt"):
                store.capture_leases()
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        assert peak < 64 << 10
        with pytest.raises(ValueError, match="corrupt"):
            store.reserve_capture(new_id("pub"), "env2")
    finally:
        store.close()


@pytest.mark.asyncio
async def test_pipe_drain_error_waits_for_actual_process_reap() -> None:
    finished = asyncio.Event()

    class BadPipe:
        async def read(self, size: int) -> bytes:
            raise OSError("synthetic pipe failure")

    class Process:
        returncode: int | None = None
        stdout = BadPipe()

        def terminate(self) -> None:
            pass

        def kill(self) -> None:
            pass

        async def wait(self) -> int:
            await finished.wait()
            self.returncode = 0
            return 0

    task = asyncio.create_task(capture._reap(Process()))  # type: ignore[arg-type]
    try:
        await asyncio.sleep(0.25)
        assert not task.done()
        finished.set()
        with pytest.raises(OSError, match="pipe failure"):
            await task
    finally:
        finished.set()
        await asyncio.gather(task, return_exceptions=True)
