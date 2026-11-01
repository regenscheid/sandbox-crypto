# SPDX-License-Identifier: MIT
"""Each environment's connection to its executor, for the execution manager.

``EnvironmentConnections`` keeps one ``Connection`` record for each environment
the worker talks to. It opens the executor when a request needs it, checks the
executor's ``hello`` and its isolation, waits while the environment is fenced,
counts requests in flight, reports what the environment is doing for idle
suspension, and closes the executor on release. What happens around an open that
belongs to other parts of the manager, such as the package gate, kernel epochs,
capture leases and the executions of a lost executor, it reaches through
``ConnectionHooks``.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
from collections.abc import AsyncGenerator, Awaitable, Callable, Collection
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from typing import Any, NamedTuple, cast

from sandbox_science.contracts import Clock, ErrorKind, Location
from sandbox_science.contracts.worker import FEATURES, LANGUAGES

from . import capture
from .backends import Backend
from .channel import ExecutorChannel, Message
from .errors import PrefixRecoveryError, WorkerError
from .isolation import probe
from .lifecycle import EnvironmentLifecycle
from .prefix_continuity import hold as hold_prefix
from .store import Store
from .usability import Usability

_log = logging.getLogger(__name__)

MAX_HOSTNAME = 255
MAX_KERNEL_REASON = 300
"""Characters of an executor's reason for offering no Python kernel that the worker keeps."""


class DaemonIncarnationError(WorkerError):
    """The sandbox's daemon is not the one the worker last knew; its sandbox is fenced."""

    def __init__(self) -> None:
        super().__init__(ErrorKind.UNAVAILABLE, "the sandbox executor incarnation was lost")


@dataclass
class Connection:
    """An environment's executor, what it offers, and how it is being used."""

    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    channel: ExecutorChannel | None = None
    location: Location | None = None
    languages: tuple[str, ...] = ()
    features: tuple[str, ...] = ()
    kernel_reason: str | None = None
    """Why the executor offers no Python kernel, as it said, bounded; None when it offers one
    or did not say."""
    running: set[str] = field(default_factory=set[str])
    last_used: float = field(kw_only=True)
    """Last ordinary use, by the manager's clock; passive observers never refresh it."""
    requests: int = 0
    """Ordinary requests in flight: files, publication and explicit kernel operations."""


@dataclass(frozen=True)
class Activity:
    """What an environment is doing, as the worker sees it, for idle suspension (W3)."""

    last_used: float | None
    """When the worker last asked anything of it (monotonic clock); None if never since it
    started."""
    running: int
    cpu_seconds: float | None
    """CPU time used below its executor so far; None without an executor, when no research
    code runs there."""
    kernels: int
    executor: str | None = None
    """The executor's incarnation, so that CPU samples of different executors are not
    compared."""


@dataclass(frozen=True)
class ConnectionHooks:
    """What the rest of the manager does as an environment's executor connects."""

    connect: Callable[[str], Awaitable[Connection]]
    """How a request connects: the manager's way to ``EnvironmentConnections.connect``,
    which ``using`` goes through too."""
    on_event: Callable[[str, Message], Awaitable[None]]
    """An executor's message, by environment."""
    on_closed: Callable[[str, ExecutorChannel], Awaitable[None]]
    """An executor's channel closed, by environment."""
    incarnation_lost: Callable[[str, str], None]
    """The daemon of that incarnation is gone: end the executions it carried."""
    recycle: Callable[[str], asyncio.Task[None]]
    """Start recycling the environment's sandbox, which fences it until done."""
    sync_package_gate: Callable[[str, ExecutorChannel], Awaitable[bool]]
    """Bring a new executor's package admission gate in step with the store."""
    hello: Callable[[str, ExecutorChannel], None]
    """Record what a new executor's ``hello`` says about the environment's packages."""
    connected: Callable[[str, ExecutorChannel], Awaitable[None]]
    """The executor is the environment's now."""


class EnvironmentConnections:
    """The connection record of each environment the worker talks to."""

    def __init__(
        self,
        backend: Backend,
        store: Store,
        usability: Usability,
        hooks: ConnectionHooks,
        lifecycle: EnvironmentLifecycle,
        *,
        clock: Clock,
        pool_id: str,
        state_dir: Path,
    ) -> None:
        self.backend = backend
        self.store = store
        self.usability = usability
        self.hooks = hooks
        self.lifecycle = lifecycle
        """The managed environments' bindings, which an executor must keep while it opens."""
        self.clock = clock
        self.pool_id = pool_id
        self.state_dir = state_dir
        """The isolation probe's sentinel goes here, among the worker's own files."""
        self.records: dict[str, Connection] = {}
        self.channels: set[ExecutorChannel] = set()
        """Every executor channel the worker opened and has not seen close."""

    def _identity(self, environment_id: str) -> tuple[str, str] | None:
        daemon_identity = self.lifecycle.daemon_identity
        return daemon_identity(environment_id) if daemon_identity is not None else None

    def get(self, environment_id: str) -> Connection | None:
        """The environment's record, without connecting."""
        return self.records.get(environment_id)

    async def connect(self, environment_id: str) -> Connection:
        """The environment's record, with its executor open; waits while it is fenced."""
        while True:
            fence = self.usability.fence(environment_id)
            if fence is not None:
                await asyncio.shield(fence)
                continue
            environment = self.records.setdefault(
                environment_id, Connection(last_used=self.clock.monotonic())
            )
            if self.usability.recycle_pending(environment_id):
                async with environment.lock:
                    if not self.usability.recycle_pending(environment_id):
                        continue
                    fence = self._fence(environment_id)
                await asyncio.shield(fence)
                continue
            # Marked used before anything waits, so that suspension leaves it alone.
            environment.last_used = self.clock.monotonic()
            if environment.channel is None or environment.channel.closed:
                # Outside the lock: preparing may start a stopped sandbox under the
                # environments' own lock, which suspension holds while it takes this one.
                await self.backend.prepare(environment_id)
            try:
                async with environment.lock:
                    if (
                        self.records.get(environment_id) is not environment
                        or self.usability.fence(environment_id) is not None
                    ):
                        continue  # released or fenced while this request waited
                    await self._open(environment_id, environment)
            except DaemonIncarnationError:
                fence = self._fence(environment_id)
                await asyncio.shield(fence)
                continue
            return environment

    def _fence(self, environment_id: str) -> asyncio.Task[None]:
        return self.usability.start_fence(
            environment_id, lambda: self.hooks.recycle(environment_id)
        )

    async def _open(self, environment_id: str, environment: Connection) -> None:
        """Start the environment's executor unless one runs. Hold its lock."""
        if environment.channel is not None and not environment.channel.closed:
            marked = self.store.package_status(environment_id)
            required = self.lifecycle.requires_workspace_prefix
            if (
                (
                    (marked is not None and marked[1] == "workspace-prefix")
                    or self.store.workspace_prefix_restore_pending(environment_id) is not None
                )
                and required is not None
                and required(environment_id)
            ):
                # Capabilities/files can explicitly reconnect while execution admission
                # is held. Recheck the surviving daemon; ordinary jobs never clear it.
                channel = environment.channel
                reply = await channel.request("hello")
                if reply.get("daemon_incarnation") != channel.incarnation:
                    raise WorkerError(ErrorKind.UNAVAILABLE, "prefix recheck daemon changed")
                channel.hello = reply
                self.hooks.hello(environment_id, channel)
                try:
                    await self.hooks.sync_package_gate(environment_id, channel)
                except WorkerError:
                    hold_prefix(self.store, environment_id)
                    raise
            return
        identity = self._identity(environment_id)
        try:
            channel = await self.backend.open(
                environment_id,
                partial(self.hooks.on_event, environment_id),
                partial(self.hooks.on_closed, environment_id),
            )
        except PrefixRecoveryError:
            hold_prefix(self.store, environment_id)
            raise
        self.channels.add(channel)
        if self._identity(environment_id) != identity:
            await channel.shutdown()
            raise WorkerError(ErrorKind.UNAVAILABLE, "environment binding changed during open")
        previous = self.store.endpoint_incarnation(environment_id)
        previous_binding = self.store.endpoint_binding(environment_id)
        if (
            self.backend.name == "openshell"
            and channel.daemon_token is not None
            and previous is not None
            and (
                channel.incarnation != previous
                or (previous_binding is not None and previous_binding != identity)
            )
        ):
            self.usability.require_recycle(environment_id, previous)
            await channel.shutdown()
            self.hooks.incarnation_lost(environment_id, previous)
            raise DaemonIncarnationError()
        try:
            # Continuity must be checked before a fresh executor can ACK ready.
            self.hooks.hello(environment_id, channel)
            await self.hooks.sync_package_gate(environment_id, channel)
            offered = describe(environment_id, channel.hello)
            declared = self.backend.isolation_tier
            isolation = await probe(
                channel, declared, self.state_dir, self.backend.node_kernel_releases
            )
        except BaseException:
            # Never leave an executor running unused, even when the open is cancelled,
            # as a capture's deadline can; the shutdown outlasts further cancels.
            await capture.owned(channel.shutdown())
            raise
        if isolation.tier < declared:
            _log.warning(
                "environment %s: isolation tier %d declared, %d in effect: %s",
                environment_id,
                declared,
                isolation.tier,
                "; ".join(isolation.evidence.findings),
            )
        environment.location = Location(
            pool_id=self.pool_id,
            backend=self.backend.name,
            sandbox_id=self.backend.sandbox_id(environment_id),
            hostname=offered.hostname,
            isolation_tier=isolation.tier,
            declared_isolation_tier=declared,
            isolation_evidence=isolation.evidence,
        )
        environment.languages = offered.languages
        environment.features = offered.features
        environment.kernel_reason = offered.kernel_reason
        environment.channel = channel
        if channel.daemon_token is not None:
            self.store.remember_endpoint(environment_id, channel.incarnation, identity)
        await self.hooks.connected(environment_id, channel)

    @contextlib.asynccontextmanager
    async def using(self, environment_id: str) -> AsyncGenerator[ExecutorChannel]:
        """The environment's executor, counted as in use until the block ends."""
        environment = await self.hooks.connect(environment_id)
        channel = environment.channel
        if channel is None:
            raise WorkerError(
                ErrorKind.UNAVAILABLE, f"environment {environment_id} has no executor"
            )
        environment.requests += 1
        try:
            yield channel
        finally:
            environment.requests -= 1
            environment.last_used = self.clock.monotonic()

    def closed(self, environment_id: str, channel: ExecutorChannel) -> None:
        """The channel closed: the environment's record no longer names it."""
        environment = self.records.get(environment_id)
        if environment is not None and environment.channel is channel:
            environment.channel = None

    async def release(self, environment_id: str) -> None:
        """Forget the environment's record and close its executor."""
        environment = self.records.get(environment_id)
        if environment is None:
            return
        async with environment.lock:
            await self._drop(environment_id, environment)

    async def release_if_idle(self, environment_id: str, idle_since: float) -> bool:
        """Forget the record and close the executor if nothing used it since ``idle_since``.

        Returns whether it did, or there was none.
        """
        environment = self.records.get(environment_id)
        if environment is None:
            return True
        async with environment.lock:
            if environment.running or environment.requests or environment.last_used > idle_since:
                return False
            await self._drop(environment_id, environment)
        return True

    async def _drop(self, environment_id: str, environment: Connection) -> None:
        """Forget an environment's record, then close its executor. Hold its lock.

        The record goes first, so that a request arriving meanwhile starts with a
        new one instead of opening an executor nobody tracks.
        """
        if self.records.get(environment_id) is environment:
            self.records.pop(environment_id)
        channel = environment.channel
        if channel is not None:
            self.channels.discard(channel)
            await channel.shutdown()
            if not self.usability.recycle_pending(environment_id):
                self.store.forget_endpoint(environment_id)

    async def activity(self, environment_id: str, jobs: Collection[str]) -> Activity | None:
        """What an environment is doing now, with ``jobs`` its running jobs; None when its
        executor cannot say."""
        environment = self.records.get(environment_id)
        if environment is None:
            return Activity(last_used=None, running=len(jobs), cpu_seconds=None, kernels=0)
        busy = len(environment.running) + environment.requests
        busy += sum(1 for job in jobs if job not in environment.running)
        channel = environment.channel
        if channel is None or channel.closed:
            return Activity(environment.last_used, busy, None, 0)
        try:
            reply = await channel.request("usage", reply_timeout=10)
        except WorkerError:
            return None
        raw, kernels = reply.get("cpu_seconds"), reply.get("kernels")
        if (
            isinstance(raw, bool)
            or not isinstance(raw, int | float)
            or not isinstance(kernels, int)
        ):
            return None
        try:
            cpu = float(raw)  # an executor research code took over may send 10**400
        except OverflowError:
            return None
        if not math.isfinite(cpu) or cpu < 0:
            return None
        return Activity(environment.last_used, busy, cpu, kernels, channel.incarnation)


class Offered(NamedTuple):
    """What an executor's ``hello`` says it offers."""

    hostname: str
    languages: tuple[str, ...]
    features: tuple[str, ...]
    kernel_reason: str | None


def describe(environment_id: str, hello: Message) -> Offered:
    """The host name, kernel languages, features and the reason for no Python kernel from an
    executor's ``hello``, checked."""
    where, languages = hello.get("location"), hello.get("languages", [])
    if not isinstance(where, dict) or not isinstance(languages, list):
        raise WorkerError(ErrorKind.UNAVAILABLE, "the executor sent a malformed hello")
    hostname = cast(dict[str, Any], where).get("hostname")
    if not isinstance(hostname, str):
        hostname = ""
    hostname = "".join(char for char in hostname[:MAX_HOSTNAME] if char.isprintable())
    if hello.get("not_dumpable") is False:
        _log.info(
            "the executor for %s stays dumpable, so research code in that environment can "
            "reach its file descriptors through /proc (the sandbox refuses sockets to a "
            "process that is not dumpable)",
            environment_id,
        )
    if hello.get("subreaper") is False:
        _log.warning(
            "the executor for %s cannot adopt orphaned processes; a shell call's processes "
            "that leave its session and clear their environment can outlive it",
            environment_id,
        )
    names = (name for name in cast(list[object], languages) if isinstance(name, str))
    raw: object = hello.get("features", [])
    offered = cast(list[object], raw) if isinstance(raw, list) else []
    features = tuple(dict.fromkeys(f for f in offered if isinstance(f, str) and f in FEATURES))
    reason: object = hello.get("kernel_reason")
    kernel_reason = None
    if isinstance(reason, str):
        # The executor may be research code's to impersonate: keep one bounded printable line.
        kernel_reason = "".join(c for c in reason[:MAX_KERNEL_REASON] if c.isprintable()) or None
    languages_offered = tuple(dict.fromkeys(n for n in names if n in LANGUAGES))
    if "python" not in languages_offered:
        _log.warning(
            "the executor for %s offers no Python kernel, so runs there have no python tool: %s",
            environment_id,
            kernel_reason or "no reason given",
        )
    return Offered(hostname or "unknown", languages_offered, features, kernel_reason)
