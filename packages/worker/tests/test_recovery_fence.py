# SPDX-License-Identifier: MIT
"""Durable ownership and final trusted-channel admission, without research processes."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, cast

import pytest

from sandbox_science.contracts import ExecutionContext
from sandbox_science.contracts.context import ExecutionAuthority
from sandbox_science.contracts.recovery import RecoveryFence
from sandbox_science.worker.channel import ExecutorChannel
from sandbox_science.worker.errors import WorkerError
from sandbox_science.worker.recovery_fence import check_dispatch, operator_scope
from sandbox_science.worker.store import Store

CONTEXT = ExecutionContext(project_id="p", session_id="s", environment_id="e")


def owner(epoch: int) -> ExecutionContext:
    return CONTEXT.model_copy(
        update={"authority": ExecutionAuthority(work="work", run="run", epoch=epoch)}
    )


def fence(epoch: int) -> RecoveryFence:
    return RecoveryFence(project_id="p", session_id="s", epoch=epoch)


def test_fence_is_persistent_monotonic_and_scoped(tmp_path: Path) -> None:
    path = tmp_path / "worker.sqlite3"
    store = Store(path)
    store.recovery_fences.check(CONTEXT)
    with pytest.raises(WorkerError, match="not installed"):
        store.recovery_fences.check(owner(1))
    store.recovery_fences.install(fence(2))
    store.close()
    store = Store(path)
    try:
        assert store.recovery_fences.install(fence(2)) == fence(2)
        with pytest.raises(WorkerError, match="cannot decrease"):
            store.recovery_fences.install(fence(1))
        for context in (CONTEXT, owner(1), owner(3), CONTEXT.model_copy(update={"actor": "user"})):
            with pytest.raises(WorkerError, match="missing or superseded"):
                store.recovery_fences.check(context)
        store.recovery_fences.check(owner(2))
        with store.recovery_fences.bind(owner(2)):
            store.recovery_fences.install(fence(3))
            for operation in ("active", "latest", "replay", "capture_cancel"):
                check_dispatch(operation)
            with pytest.raises(WorkerError, match="superseded"):
                check_dispatch("write")
        store.recovery_fences.check(CONTEXT.model_copy(update={"session_id": "other"}))
        with operator_scope(True), store.recovery_fences.bind(CONTEXT):
            pass
        with pytest.raises(WorkerError), store.recovery_fences.bind(CONTEXT):
            pass
    finally:
        store.close()


def test_legacy_context_serialization_is_unchanged() -> None:
    assert CONTEXT.model_dump() == {
        "project_id": "p",
        "session_id": "s",
        "environment_id": "e",
        "policy_revision": None,
        "actor": "agent",
    }
    assert "authority" not in CONTEXT.model_dump_json()
    assert owner(1).model_dump()["authority"] == {"work": "work", "run": "run", "epoch": 1}
    longest = "20261017T123456123456Z-" + "r" * 128
    assert ExecutionAuthority(work=longest, run=longest, epoch=1).run == longest


def test_legacy_project_admission_expires_when_first_root_is_fenced(tmp_path: Path) -> None:
    store = Store(tmp_path / "worker.sqlite3")
    try:
        with store.recovery_fences.bind_project("p"):
            check_dispatch("configure")
            store.recovery_fences.install(fence(1))
            with pytest.raises(WorkerError, match="requires current context"):
                check_dispatch("configure")
    finally:
        store.close()


async def test_epoch_advance_while_waiting_on_channel_write_lock_refuses_dispatch(
    tmp_path: Path,
) -> None:
    store = Store(tmp_path / "worker.sqlite3")
    store.recovery_fences.install(fence(1))
    writes: list[bytes] = []

    class Input:
        def write(self, data: bytes) -> None:
            writes.append(data)

        async def drain(self) -> None:
            pytest.fail("superseded command cannot reach drain")

    async def closed(channel):
        pass

    async def on_event(message):
        pass

    channel = ExecutorChannel([], on_event, closed)
    channel._process = cast(Any, type("Process", (), {"stdin": Input()})())
    await channel._write_lock.acquire()
    try:
        with store.recovery_fences.bind(owner(1)):
            pending = asyncio.create_task(channel.request("shell", script="forbidden"))
        await asyncio.sleep(0)
        store.recovery_fences.install(fence(2))
        channel._write_lock.release()
        with pytest.raises(WorkerError, match="superseded"):
            await pending
        assert not writes
        assert not channel._pending
    finally:
        store.close()


async def test_command_written_before_fence_is_still_an_observation_obligation(
    tmp_path: Path,
) -> None:
    store = Store(tmp_path / "worker.sqlite3")
    store.recovery_fences.install(fence(1))
    written = asyncio.Event()
    finish = asyncio.Event()
    request_id = None

    class Input:
        def write(self, data: bytes) -> None:
            nonlocal request_id
            request_id = json.loads(data)["id"]
            written.set()

        async def drain(self) -> None:
            await finish.wait()

    async def closed(channel):
        pass

    async def on_event(message):
        pass

    channel = ExecutorChannel([], on_event, closed)
    channel._process = cast(Any, type("Process", (), {"stdin": Input()})())
    try:
        with store.recovery_fences.bind(owner(1)):
            pending = asyncio.create_task(channel.request("shell", script="admitted"))
        await written.wait()
        store.recovery_fences.install(fence(2))
        assert request_id is not None
        channel._pending[request_id].set_result({"ok": True})
        finish.set()
        assert await pending == {"ok": True}
    finally:
        finish.set()
        store.close()


async def test_shared_commit_cannot_wait_in_a_thread_across_epoch_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sandbox_science.worker.shared_sync import SharedRecords, SharedSync, _Batch

    store = Store(tmp_path / "worker.sqlite3")
    records = SharedRecords(tmp_path / "shared.sqlite3")
    sync = SharedSync(records, tmp_path / "blobs")
    queued = asyncio.Event()
    release = asyncio.Event()
    original = asyncio.to_thread

    async def delayed(function, *args, **kwargs):
        if function == records.apply:
            queued.set()
            await release.wait()
        return await original(function, *args, **kwargs)

    monkeypatch.setattr(asyncio, "to_thread", delayed)
    store.recovery_fences.install(fence(1))
    try:
        with store.recovery_fences.bind(owner(1)):
            pending = asyncio.create_task(sync._apply("p", _Batch("p", "e", counter=1)))
        await asyncio.sleep(0)
        admitted_before_fence = pending.done()
        store.recovery_fences.install(fence(2))
        release.set()
        if admitted_before_fence:
            await pending  # a bounded SQL commit admitted before the fence is retained
            assert records.state("p", "e").version == 1
        else:
            assert queued.is_set()
            with pytest.raises(WorkerError, match="superseded"):
                await pending
            assert records.state("p", "e").version == 0
    finally:
        release.set()
        records.close()
        store.close()


async def test_policy_commit_rechecks_after_live_filesystem_observation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from types import SimpleNamespace

    from sandbox_science.worker.environments import Environments

    store = Store(tmp_path / "worker.sqlite3")
    store.recovery_fences.install(fence(1))
    observing, release = asyncio.Event(), asyncio.Event()
    environments = object.__new__(Environments)

    async def observe(sandbox):
        observing.set()
        await release.wait()
        return {}

    def write(*args):
        pytest.fail("superseded policy admission must not prepare a policy write")

    monkeypatch.setattr(environments, "_live_filesystem", observe)
    monkeypatch.setattr(environments, "_write", write)
    try:
        with store.recovery_fences.bind(owner(1)):
            pending = asyncio.create_task(
                environments._set_policy(
                    cast(Any, SimpleNamespace(sandbox="fake")), "", cast(Any, None)
                )
            )
        await observing.wait()
        store.recovery_fences.install(fence(2))
        release.set()
        with pytest.raises(WorkerError, match="superseded"):
            await pending
    finally:
        release.set()
        store.close()
