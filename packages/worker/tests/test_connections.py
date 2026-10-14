# SPDX-License-Identifier: MIT
"""Opening an environment's executor, through ``EnvironmentConnections``' own methods.

The connections run a real executor on the local backend, with hooks that only
record what they are asked, so that a failing or cancelled open can be watched. The
last tests check, on a whole worker, that the manager's hooks stay the seams its
tests replace.
"""

from __future__ import annotations

import asyncio
import sys
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import Any

import pytest

from sandbox_science.contracts import (
    SYSTEM_CLOCK,
    ErrorKind,
    ExecutionContext,
    KernelRestartRequest,
    KernelStatusRequest,
    ListFilesRequest,
)
from sandbox_science.worker import connections as connections_module
from sandbox_science.worker.backends import LocalBackend
from sandbox_science.worker.channel import ExecutorChannel, Message
from sandbox_science.worker.connections import (
    Connection,
    ConnectionHooks,
    EnvironmentConnections,
)
from sandbox_science.worker.errors import WorkerError
from sandbox_science.worker.settings import LocalBackendSettings
from sandbox_science.worker.store import Store
from sandbox_science.worker.usability import Usability

from .harness import CONTEXT, Harness, view

pytestmark = pytest.mark.slow


class Opened:
    """The channels the backend opened, so a test can see whether each was closed."""

    def __init__(self) -> None:
        self.channels: list[ExecutorChannel] = []

    async def sync_package_gate(self, environment_id: str, channel: ExecutorChannel) -> bool:
        self.channels.append(channel)
        return True

    async def wait_closed(self, channel: ExecutorChannel) -> None:
        async with asyncio.timeout(10):
            while not channel.closed:
                await asyncio.sleep(0.05)


def connections_for(
    tmp_path: Path,
    opened: Opened,
    *,
    sync_package_gate: Callable[[str, ExecutorChannel], Awaitable[bool]] | None = None,
) -> tuple[EnvironmentConnections, Store]:
    store = Store(tmp_path / "worker.sqlite")

    async def on_event(environment_id: str, message: Message) -> None:
        return None

    async def on_closed(environment_id: str, channel: ExecutorChannel) -> None:
        return None

    async def connected(environment_id: str, channel: ExecutorChannel) -> None:
        return None

    def recycle(environment_id: str) -> asyncio.Task[None]:
        raise AssertionError("no recycle is expected")

    connections: EnvironmentConnections | None = None

    async def connect(environment_id: str) -> Connection:
        assert connections is not None
        return await connections.connect(environment_id)

    hooks = ConnectionHooks(
        connect=connect,
        on_event=on_event,
        on_closed=on_closed,
        daemon_identity=lambda _: None,
        incarnation_lost=lambda *_: None,
        recycle=recycle,
        sync_package_gate=sync_package_gate or opened.sync_package_gate,
        hello=lambda *_: None,
        connected=connected,
    )
    connections = EnvironmentConnections(
        LocalBackend(LocalBackendSettings(python=sys.executable), tmp_path),
        store,
        Usability(store),
        hooks,
        clock=SYSTEM_CLOCK,
        pool_id="test",
        state_dir=tmp_path,
    )
    return connections, store


@pytest.fixture
async def opened() -> AsyncIterator[Opened]:
    opened = Opened()
    yield opened
    for channel in opened.channels:
        await channel.close()


async def test_a_request_arriving_as_the_fence_ends_does_not_spin(
    tmp_path: Path, opened: Opened, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A fence that finished before its done callback ran must not be awaited again and
    again: awaiting a finished task never yields, so the worker would hang."""
    connections, store = connections_for(tmp_path, opened)

    async def recycling() -> None:
        return None

    finished = asyncio.create_task(recycling())
    await finished
    connections.usability.start_fence("env1", lambda: finished)
    looked = 0
    fence = connections.usability.fence

    def counted(environment_id: str) -> asyncio.Task[None] | None:
        nonlocal looked
        looked += 1
        if looked > 1000:
            raise AssertionError("connect spins on a finished fence")
        return fence(environment_id)

    monkeypatch.setattr(connections.usability, "fence", counted)
    try:
        environment = await connections.connect("env1")
        assert environment.channel is not None
        assert looked < 10
    finally:
        await connections.release("env1")
        store.close()


async def test_a_cancelled_open_closes_the_executor_it_started(
    tmp_path: Path, opened: Opened, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A capture's deadline can cancel a request while its executor is being probed."""
    connections, store = connections_for(tmp_path, opened)
    probing = asyncio.Event()

    async def hanging(channel: ExecutorChannel, *args: Any) -> Any:
        probing.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(connections_module, "probe", hanging)
    try:
        request = asyncio.create_task(connections.connect("env1"))
        await asyncio.wait_for(probing.wait(), 30)
        request.cancel()
        with pytest.raises(asyncio.CancelledError):
            await request
        (channel,) = opened.channels
        await opened.wait_closed(channel)
        record = connections.get("env1")
        assert record is None or record.channel is None
    finally:
        store.close()


async def test_a_failed_package_gate_sync_closes_the_executor_it_started(
    tmp_path: Path, opened: Opened
) -> None:
    async def failing(environment_id: str, channel: ExecutorChannel) -> bool:
        opened.channels.append(channel)
        raise WorkerError(ErrorKind.UNAVAILABLE, "invalid package admission acknowledgement")

    connections, store = connections_for(tmp_path, opened, sync_package_gate=failing)
    try:
        with pytest.raises(WorkerError):
            await connections.connect("env1")
        (channel,) = opened.channels
        await opened.wait_closed(channel)
        record = connections.get("env1")
        assert record is None or record.channel is None
    finally:
        store.close()


async def test_every_request_connects_through_the_managers_connect(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The kernel observation tests replace the manager's connect to prove nothing
    connects; every other request must go through it too."""
    context = ExecutionContext.model_validate(CONTEXT)
    async with await Harness.start(tmp_path) as worker:
        connects = view(worker.manager).watch_connects(monkeypatch)
        await worker.manager.kernel_status(KernelStatusRequest(context=context))
        await worker.manager.list_files(ListFilesRequest(context=context))
        await worker.manager.restart_kernel(KernelRestartRequest(context=context))
        assert connects == ["env1", "env1", "env1"]


async def test_a_hook_replaced_on_the_manager_after_start_is_used_on_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with await Harness.start(tmp_path) as worker:
        synced = view(worker.manager).watch_package_gate_syncs(monkeypatch)
        await worker.manager.kernel_status(
            KernelStatusRequest(context=ExecutionContext.model_validate(CONTEXT))
        )
        assert synced == [view(worker.manager).channel("env1")]
