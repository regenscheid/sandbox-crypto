# SPDX-License-Identifier: MIT
"""The one harness for worker tests: a worker on the local backend, reached through its API.

``Harness`` starts an ``ExecutionManager`` with its store, blobs and HTTP app, over
the stdio transport or the daemon, and is a ``WorkerApi`` for that worker. Tests
check behavior through the API or the manager's public methods.

Some tests must look at, or set up, state that has no public API: the executor an
environment is connected to, the observations in flight, a quarantined
environment. They go through ``ManagerView``, the only place in the worker's tests
that touches the manager's private attributes, so the manager can be split (plan
item C-03) without rewriting the tests that pin its behavior.
"""

from __future__ import annotations

import asyncio
import contextlib
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path
from types import TracebackType
from typing import Any, Literal, Self

import pytest
from aiohttp.test_utils import TestClient, TestServer

from sandbox_science.contracts import SYSTEM_CLOCK, Clock, ExecutionContext, ExecutionState
from sandbox_science.contracts.worker import EVENT_HEARTBEAT_SECONDS
from sandbox_science.testing import WorkerApi
from sandbox_science.worker.app import create_app
from sandbox_science.worker.artifacts import Blobs
from sandbox_science.worker.backends import Backend, ClosedHandler, LocalBackend
from sandbox_science.worker.channel import (
    CHANNEL_TIMEOUTS,
    ChannelTimeouts,
    EventHandler,
    ExecutorChannel,
    Message,
)
from sandbox_science.worker.connections import Connection
from sandbox_science.worker.manager import ExecutionManager, _KernelObserver, _Live
from sandbox_science.worker.settings import LocalBackendSettings
from sandbox_science.worker.shared_sync import SharedRecords, SharedSync
from sandbox_science.worker.store import Store
from sandbox_science.worker.timeouts import MANAGER_TIMEOUTS, ManagerTimeouts
from sandbox_science.worker.usability import EnvironmentUse

TOKEN = "test-secret-worker-pool-token-0000000000"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
CONTEXT = {"project_id": "p1", "session_id": "s1", "environment_id": "env1"}
HOSTILE = Path(__file__).with_name("hostile_executor.py")
SYNTHETIC_IDENTITY = ("synthetic-sandbox", "synthetic-id")
"""A trusted daemon binding for the local backend; no VM attests to it."""


def context(**changes: str) -> dict[str, str]:
    """``CONTEXT`` with some of its fields changed."""
    return {**CONTEXT, **changes}


class ManagerView:
    """What tests read from, and force on, an ``ExecutionManager`` beyond its public API."""

    def __init__(self, manager: ExecutionManager) -> None:
        self.manager = manager

    # Environment connections.

    def connected(self) -> set[str]:
        """The environments the manager holds a connection record for."""
        return set(self.manager._connections.records)

    def connection(self, environment_id: str) -> Connection:
        """The connection record: its lock, channel, last use and requests in flight."""
        return self.manager._connections.records[environment_id]

    def channel(self, environment_id: str = "env1") -> ExecutorChannel:
        """The executor the environment is connected to; there must be one."""
        channel = self.manager._connections.records[environment_id].channel
        assert channel is not None, f"{environment_id} has no executor"
        return channel

    def channel_or_none(self, environment_id: str = "env1") -> ExecutorChannel | None:
        environment = self.manager._connections.records.get(environment_id)
        return environment.channel if environment is not None else None

    def executor_process(self, environment_id: str = "env1") -> asyncio.subprocess.Process:
        """The process of the executor the environment is connected to."""
        process = self.channel(environment_id)._process
        assert process is not None, f"{environment_id}'s executor has no process"
        return process

    def put_connection(self, environment_id: str, record: Connection) -> None:
        """Install a connection record, as if the environment had connected."""
        self.manager._connections.records[environment_id] = record

    def drop_connection(self, environment_id: str) -> None:
        self.manager._connections.records.pop(environment_id)

    # Executions in flight.

    def live(self, execution_id: str) -> _Live:
        """The manager's record of an execution that has not ended."""
        return self.manager._live[execution_id]

    def live_channel(self, execution_id: str) -> ExecutorChannel:
        """The executor the execution was sent to."""
        channel = self.manager._live[execution_id].channel
        assert channel is not None, f"{execution_id} was not dispatched"
        return channel

    def live_executions(self) -> set[str]:
        return set(self.manager._live)

    # Kernel observations, captures and lockfile fetches.

    def observations(self) -> dict[str, _KernelObserver]:
        """The kernel observations in flight, by environment."""
        return dict(self.manager._kernel_observations)

    def closing_sessions(self) -> dict[tuple[str, str, str], int]:
        """Session closes in progress, by project, environment and session."""
        return {
            (project_id, environment_id, session_id): count
            for environment_id, use in self.manager._usability._uses.items()
            for (project_id, session_id), count in use.closing_sessions.items()
        }

    def captures(self) -> int:
        """Artifact captures running on the worker's side."""
        return self.manager._publication.running()

    def capture_counts(self) -> dict[str, int]:
        """Captures holding admission, by environment, as the store records them."""
        counts: dict[str, int] = {}
        for lease in self.manager.store.capture_leases():
            counts[lease.environment_id] = counts.get(lease.environment_id, 0) + 1
        return counts

    async def reconcile_captures(self, environment_id: str, channel: ExecutorChannel) -> None:
        """Ask the executor to confirm the cleanup of held captures, as a reconnect does."""
        await self.manager._publication.reconcile(environment_id, channel)

    def fetching_lockfiles(self) -> set[str]:
        return set(self.manager._fetching)

    # Faults and states a test forces.

    async def deliver(self, environment_id: str, message: Message) -> None:
        """Hand the manager a message as if the environment's executor had sent it."""
        await self.manager._on_event(environment_id, message)

    async def recover(self, environment_id: str, *, startup: bool = False) -> None:
        """Run the daemon recovery of an environment now."""
        await self.manager._recover(environment_id, startup=startup)

    def quarantine(self, environment_id: str) -> None:
        self.manager._usability.quarantine(environment_id)

    def release_quarantine(self, environment_id: str) -> None:
        self.manager._usability.lift_quarantine(environment_id)

    def quarantined(self, environment_id: str) -> bool:
        return self.manager._usability.peek(environment_id).quarantined

    def reopen(self) -> None:
        """Let a closed manager start again, as a new worker process would."""
        self.manager._usability.closing = False

    def make_unusable(
        self,
        patch: pytest.MonkeyPatch,
        condition: Literal[
            "shutdown", "recovering", "fence", "quarantined", "released", "session_closing"
        ],
        context: ExecutionContext,
    ) -> None:
        """Put the environment of ``context`` in a state where it may not be used."""
        usability = self.manager._usability
        if condition == "shutdown":
            patch.setattr(usability, "closing", True)
            return
        use = EnvironmentUse()
        if condition == "recovering":
            use.recovering = True
        elif condition == "quarantined":
            use.quarantined = True
        elif condition == "released":
            use.releases = 1
        elif condition == "fence":
            use.fence = asyncio.current_task()
        else:
            use.closing_sessions[context.project_id, context.session_id] = 1
        patch.setitem(usability._uses, context.environment_id, use)

    def wrap_apply(
        self,
        patch: pytest.MonkeyPatch,
        wrapper: Callable[[Callable[[_Live, str, Message], None], _Live, str, Message], None],
    ) -> None:
        """Route every executor message the manager applies through ``wrapper``."""
        original = self.manager._apply
        patch.setattr(
            self.manager, "_apply", lambda live, env, message: wrapper(original, live, env, message)
        )

    def wrap_on_event(
        self,
        patch: pytest.MonkeyPatch,
        wrapper: Callable[
            [Callable[[str, Message], Awaitable[None]], str, Message], Awaitable[None]
        ],
    ) -> Callable[[str, Message], Awaitable[None]]:
        """Route every executor message the manager receives through ``wrapper``.

        Returns the manager's own handler, which applies a message without the wrapper.
        """
        original = self.manager._on_event

        async def wrapped(environment_id: str, message: Message) -> None:
            await wrapper(original, environment_id, message)

        patch.setattr(self.manager, "_on_event", wrapped)
        return original

    def watch_connects(self, patch: pytest.MonkeyPatch) -> list[str]:
        """Record every environment a request connects to; the list fills as they do."""
        connects: list[str] = []
        original = self.manager._environment

        async def watched(environment_id: str) -> Connection:
            connects.append(environment_id)
            return await original(environment_id)

        patch.setattr(self.manager, "_environment", watched)
        return connects

    def watch_package_gate_syncs(self, patch: pytest.MonkeyPatch) -> list[ExecutorChannel]:
        """Record every executor whose package gate the manager syncs as it opens."""
        synced: list[ExecutorChannel] = []
        original = self.manager._sync_package_gate

        async def watched(environment_id: str, channel: ExecutorChannel) -> bool:
            synced.append(channel)
            return await original(environment_id, channel)

        patch.setattr(self.manager, "_sync_package_gate", watched)
        return synced

    def poller(self, environment_id: str) -> tuple[ExecutorChannel, asyncio.Task[Any]]:
        """The daemon poller of an environment: the channel it polls and its task."""
        return self.manager._pollers[environment_id]

    def put_poller(
        self, environment_id: str, channel: ExecutorChannel, task: asyncio.Task[Any]
    ) -> None:
        self.manager._pollers[environment_id] = (channel, task)

    def poll(self, environment_id: str, channel: ExecutorChannel) -> None:
        """Start (or replace) the daemon poller of an environment."""
        self.manager._poll(environment_id, channel)


def view(manager: ExecutionManager) -> ManagerView:
    return ManagerView(manager)


async def running(manager: ExecutionManager, execution_id: str, timeout: float = 15) -> None:
    """Until the manager's events say the execution runs."""
    async with asyncio.timeout(timeout):
        async for event in manager.events(execution_id, 0):
            if getattr(event, "state", None) is ExecutionState.RUNNING:
                return
    raise AssertionError("execution never ran")


async def stop_daemon(manager: ExecutionManager, environment_id: str = "env1") -> None:
    """Stop an environment's daemon for good.

    ``close()`` detaches from daemons on purpose, so a test that started one stops it
    explicitly, even when an assertion failed before the manager connected.
    """
    channel: ExecutorChannel | None = None
    try:
        async with asyncio.timeout(35):
            await manager.capabilities(environment_id)
            channel = view(manager).channel_or_none(environment_id)
            await manager.release(environment_id)
    finally:
        if channel is not None:
            await asyncio.shield(channel.shutdown())


async def shut_down_daemon(manager: ExecutionManager, environment_id: str = "env1") -> None:
    """Stop an environment's daemon, then close the manager."""
    try:
        await stop_daemon(manager, environment_id)
    finally:
        await manager.close()


class HostileBackend:
    """Starts hostile_executor.py, a stand-in for an executor research code took over."""

    name: Literal["local"] = "local"

    def __init__(
        self,
        mode: str,
        env: dict[str, str] | None = None,
        timeouts: ChannelTimeouts = CHANNEL_TIMEOUTS,
    ) -> None:
        self.mode = mode
        self.env = env
        self.timeouts = timeouts
        self.channels: list[ExecutorChannel] = []

    async def prepare(self, environment_id: str) -> None:
        return None

    async def open(
        self, environment_id: str, on_event: EventHandler, on_closed: ClosedHandler
    ) -> ExecutorChannel:
        argv = [sys.executable, "-I", str(HOSTILE), self.mode]
        channel = ExecutorChannel(argv, on_event, on_closed, env=self.env, timeouts=self.timeouts)
        self.channels.append(channel)
        await channel.start()
        return channel

    def sandbox_id(self, environment_id: str) -> str | None:
        return None

    @property
    def isolation_tier(self) -> int:
        return 0


class Harness(WorkerApi):
    """A worker under test: its manager, store and HTTP API, on one state directory.

    Make it with ``await Harness.start(root, ...)`` and end it with ``close()``, or use
    it as an async context manager. ``restart()`` closes the worker and starts a new
    one on the same state, as a worker process restart does.
    """

    def __init__(
        self,
        root: Path,
        *,
        backend: Backend | None = None,
        python: str = sys.executable,
        daemon: bool = False,
        identity: tuple[str, str] | None = None,
        executor_args: tuple[str, ...] = (),
        shared_bytes: int | None = None,
        shared_total: int = 1 << 30,
        package_cache: str | None = None,
        max_artifact_size: int = 1 << 20,
        capture_timeout: float = 30.0,
        capture_ack_timeout: float = 1.0,
        timeouts: ManagerTimeouts = MANAGER_TIMEOUTS,
        clock: Clock = SYSTEM_CLOCK,
        heartbeat: float = EVENT_HEARTBEAT_SECONDS,
        token: str = TOKEN,
        pool_id: str = "test-pool",
        store_name: str = "worker.sqlite3",
    ) -> None:
        self.root = root
        self.daemon = daemon
        self.backend: Backend = backend or LocalBackend(
            LocalBackendSettings(python=python, daemon=daemon), root, executor_args
        )
        self.identity = identity if identity is not None or not daemon else SYNTHETIC_IDENTITY
        self._shared_bytes = shared_bytes
        self._shared_total = shared_total
        self._options: dict[str, Any] = {
            "pool_id": pool_id,
            "max_artifact_size": max_artifact_size,
            "state_dir": root,
            "package_cache": package_cache,
            "capture_timeout": capture_timeout,
            "capture_ack_timeout": capture_ack_timeout,
            "timeouts": timeouts,
            "clock": clock,
        }
        self.clock = clock
        self.heartbeat = heartbeat
        self.store_path = root / store_name
        self.store = Store(self.store_path)
        self.shared = self._new_shared()
        self.manager = self.new_manager()
        self._client: TestClient[Any, Any] | None = None
        self.token = token
        self.context = dict(CONTEXT)

    @classmethod
    async def start(cls, root: Path, **options: Any) -> Self:
        harness = cls(root, **options)
        await harness.open()
        return harness

    def _new_shared(self) -> SharedSync | None:
        if self._shared_bytes is None:
            return None
        return SharedSync(
            SharedRecords(self.root / "shared.sqlite3"),
            self.root / "shared",
            max_file_bytes=self._shared_bytes,
            max_project_bytes=self._shared_total,
            clock=self.clock,
        )

    def new_manager(self) -> ExecutionManager:
        """A manager on the harness's store and backend, not started."""
        manager = ExecutionManager(
            self.store, self.backend, Blobs(self.root / "artifacts"), shared=self.shared,
            **self._options,
        )  # fmt: skip
        if self.identity is not None:
            identity = self.identity
            manager.daemon_identity = lambda _environment: identity
        return manager

    @property
    def client(self) -> TestClient[Any, Any]:
        """The test client of the worker's API, for requests ``WorkerApi`` does not make."""
        assert self._client is not None, "the harness is not open"
        return self._client

    @property
    def view(self) -> ManagerView:
        return ManagerView(self.manager)

    @property
    def workspace(self) -> Path:
        """The session directory of ``CONTEXT`` on the local backend."""
        return self.root / "environments" / "env1" / "project" / "sessions" / "s1"

    async def open(self) -> None:
        """Start the manager and serve its API."""
        await self.manager.start()
        app = create_app(self.manager, self.token, heartbeat=self.heartbeat)
        client = TestClient(TestServer(app))
        await client.start_server()
        self._client = client
        super().__init__(client.session, str(client.make_url("")), self.token, self.context)

    async def close(self) -> None:
        """Close the worker; with the daemon transport, stop its daemons first."""
        try:
            if self.daemon:
                for environment in self._daemon_environments():
                    with contextlib.suppress(Exception):
                        await stop_daemon(self.manager, environment)
            await self.manager.close()
        finally:
            if self._client is not None:
                await self._client.close()
                self._client = None
            self.store.close()
            if self.shared is not None:
                self.shared.records.close()

    def _daemon_environments(self) -> list[str]:
        root = self.root / "environments"
        return sorted(path.name for path in root.iterdir()) if root.is_dir() else []

    async def restart(self) -> None:
        """Close the worker without stopping daemons, and start a new one on its state."""
        await self.manager.close()
        if self._client is not None:
            await self._client.close()
            self._client = None
        self.store.close()
        if self.shared is not None:
            self.shared.records.close()
        self.store = Store(self.store_path)
        self.shared = self._new_shared()
        self.manager = self.new_manager()
        await self.open()

    async def __aenter__(self) -> Self:
        if self._client is None:
            await self.open()
        return self

    async def __aexit__(
        self,
        kind: type[BaseException] | None,
        value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.close()
