# SPDX-License-Identifier: MIT
"""Daemon recovery for the execution manager: reattaching executions after a lost bridge.

With the daemon transport an execution outlives its bridge, and the worker itself.
``DaemonRecovery`` reattaches each environment's unfinished executions when their
bridge is lost or the worker restarts: one recovery at a time for an environment,
with a capped backoff, never resubmitting a command. It withdraws daemon tasks whose
ownership did not survive, polls each bridge for the events of its executions, and
fences an environment whose daemon was replaced until its sandbox is recycled.
What it does to an execution, such as losing it or watching its deadline, it asks
of the manager through ``RecoveryHooks``.
"""

from __future__ import annotations

import asyncio
import itertools
import logging
import re
from collections.abc import Awaitable, Callable, Coroutine, Iterable
from dataclasses import dataclass
from typing import Any, cast

from sandbox_science.contracts import TERMINAL_STATES, Clock, ErrorKind

from .channel import ExecutorChannel
from .connections import Connection, EnvironmentConnections
from .errors import WorkerError
from .live import REQUEST, Live, time_limit
from .publication import Publication
from .store import Store
from .timeouts import ManagerTimeouts
from .usability import Usability

_log = logging.getLogger(__name__)

STARTUP_RECOVERY_ATTEMPTS = 5
"""Recovery attempts at startup before the worker refuses to start."""

ConfirmedLoss = Callable[[str], Awaitable[bool]]
"""Whether the environment's sandbox is proven deleted, stopped or replaced."""
Recycle = Callable[[str, str, str], Awaitable[None]]
"""Stop and start the environment's original sandbox, by its name and ID."""


@dataclass(frozen=True)
class RecoveryHooks:
    """What recovery asks of the rest of the manager."""

    lives: Callable[[], Iterable[Live]]
    """The executions the manager has in memory."""
    lose: Callable[[Live, str], None]
    """End an execution whose outcome is unknown, because the named thing stopped."""
    spawn: Callable[[Coroutine[Any, Any, Any]], asyncio.Task[Any]]
    """Start a background task that the manager owns and cancels when it closes."""
    watch: Callable[[Live, float], Coroutine[Any, Any, None]]
    """An execution's watchdog, for the seconds it has left."""
    watch_cancel: Callable[[Live], Coroutine[Any, Any, None]]
    """The watchdog of an execution's cancellation."""
    send_cancel: Callable[[Live], Awaitable[None]]
    """Ask the executor an execution was sent to to cancel it."""
    connect: Callable[[str], Awaitable[Connection]]
    """How a request connects to an environment's executor: the manager's way."""


class DaemonRecovery:
    """Each environment's recovery, bridge poller and daemon fence."""

    def __init__(
        self,
        store: Store,
        connections: EnvironmentConnections,
        usability: Usability,
        publication: Publication,
        hooks: RecoveryHooks,
        *,
        clock: Clock,
        timeouts: Callable[[], ManagerTimeouts],
        confirmed_daemon_loss: Callable[[], ConfirmedLoss | None],
        recycle_daemon: Callable[[], Recycle | None],
    ) -> None:
        self.store = store
        self.connections = connections
        self.usability = usability
        self.publication = publication
        self.hooks = hooks
        self.clock = clock
        self.timeouts = timeouts
        """The manager's timeouts, as they are when each wait starts."""
        self.confirmed_daemon_loss = confirmed_daemon_loss
        """The manager's proof of sandbox loss, which is set after it is built."""
        self.recycle_daemon = recycle_daemon
        """The manager's sandbox recycle, which is set after it is built."""
        self._pollers: dict[str, tuple[ExecutorChannel, asyncio.Task[Any]]] = {}

    def bridge_lost(self, environment_id: str, channel: ExecutorChannel) -> None:
        """A daemon bridge closed: stop its poller and, unless the worker is closing,
        recover the executions it carried."""
        poller = self._pollers.get(environment_id)
        if poller is not None and poller[0] is channel:
            self._pollers.pop(environment_id, None)
            if poller[1] is not asyncio.current_task():
                poller[1].cancel()
        self.connections.closed(environment_id, channel)
        if not self.usability.closing:
            self.hooks.spawn(self.recover(environment_id))

    async def recover(self, environment_id: str, *, startup: bool = False) -> None:
        """Reattach the unfinished executions of an environment that lost their bridge.

        One recovery runs for an environment at a time. Each attempt collects the
        executions again, so one whose bridge was lost while a recovery ran is not left
        behind, and attempts repeat with a capped backoff until none is left. Startup
        waits for one attempt that reaches the daemon, and refuses to start without one.
        """
        if not self.usability.begin_recovery(environment_id):
            return
        delay = self.timeouts().recovery_retry
        try:
            for attempt in itertools.count(1):
                try:
                    reached = await self._recover_once(environment_id, startup=startup)
                except Exception:
                    if startup:
                        raise
                    _log.exception("recovering the executions of %s failed", environment_id)
                    reached = False
                if self.usability.closing or (
                    reached and (startup or not self.unattached(environment_id))
                ):
                    break
                if startup and attempt == STARTUP_RECOVERY_ATTEMPTS:
                    raise WorkerError(
                        ErrorKind.UNAVAILABLE,
                        "cannot reconcile the sandbox daemon; startup refused",
                    )
                await asyncio.sleep(delay)
                delay = min(2 * delay, self.timeouts().recovery_retry_max)
        finally:
            self.usability.end_recovery(environment_id)
        if startup and not self.usability.closing and self.unattached(environment_id):
            self.hooks.spawn(self.recover(environment_id))

    def unattached(self, environment_id: str) -> list[Live]:
        """An environment's unfinished dispatched executions that no open bridge carries."""
        return [
            live
            for live in self.hooks.lives()
            if live.environment_id == environment_id
            and live.dispatched
            and live.execution.state not in TERMINAL_STATES
            and (live.channel is None or live.channel.closed)
        ]

    def _keep_watchdog(self, live: Live) -> None:
        """Keep an existing watchdog across reconnects.

        At startup, reconstruct one absolute deadline, including its original grace,
        rather than refreshing it.
        """
        live.reattached = True
        saved = self.store.recovery(live.execution.execution_id)
        if saved is not None and (live.watchdog is None or live.watchdog.done()):
            request = REQUEST.validate_json(saved[0])
            elapsed = max(
                0.0,
                (
                    self.clock.now() - (live.execution.started_at or live.execution.submitted_at)
                ).total_seconds(),
            )
            live.watchdog = self.hooks.spawn(self.hooks.watch(live, time_limit(request) - elapsed))

    async def _recover_once(self, environment_id: str, *, startup: bool) -> bool:
        """One recovery attempt; whether it reached the daemon or found the sandbox gone."""
        for live in self.unattached(environment_id):
            self._keep_watchdog(live)
        confirmed_loss = self.confirmed_daemon_loss()
        if confirmed_loss is not None and await confirmed_loss(environment_id):
            self.usability.sandbox_stopped(environment_id)
            for live in self.unattached(environment_id):
                self.hooks.lose(live, "the sandbox that was deleted, stopped or replaced")
            return True
        try:
            channel = (await self.hooks.connect(environment_id)).channel
        except WorkerError:
            return False
        if channel is None:
            return False
        try:
            await self._reconcile_unknown(channel)
        except WorkerError as exc:
            await channel.shutdown()
            if startup:
                raise WorkerError(
                    ErrorKind.UNAVAILABLE,
                    "cannot quiesce unknown daemon tasks; startup refused",
                ) from exc
            for live in self.unattached(environment_id):
                self.hooks.lose(live, "the sandbox executor incarnation")
            return True
        for live in self.unattached(environment_id):
            if live.execution.state in TERMINAL_STATES:
                continue
            self._keep_watchdog(live)
            saved = self.store.recovery(live.execution.execution_id)
            if saved is None or channel.incarnation != saved[1]:
                self.hooks.lose(live, "the sandbox executor incarnation")
                continue
            live.channel = channel
            live.dispatched = True
            environment = self.connections.get(environment_id)
            if environment is not None:
                environment.running.add(live.execution.execution_id)
            if live.cancel_requested:
                self.store.cancel_intent(live.execution.execution_id)
                if live.cancel_watchdog is None or live.cancel_watchdog.done():
                    live.cancel_watchdog = self.hooks.spawn(self.hooks.watch_cancel(live))
                try:
                    async with asyncio.timeout(self.timeouts().cancel_grace):
                        await self.hooks.send_cancel(live)
                        if live.execution.origin is not None:
                            # Withdrawn provisioning ownership cannot leave an
                            # installer running while the service starts accepting work.
                            while live.execution.state not in TERMINAL_STATES:
                                await self._replay(live, channel)
                                if live.execution.state not in TERMINAL_STATES:
                                    await asyncio.sleep(0.05)
                except (WorkerError, TimeoutError):
                    await channel.shutdown()
                    self.hooks.lose(live, "the executor that did not confirm recovery cancellation")
                    continue
            try:
                await self._replay(live, channel)
            except WorkerError:
                await channel.close()
                break
        self.poll(environment_id, channel)
        return True

    async def _reconcile_unknown(self, channel: ExecutorChannel) -> None:
        """Withdraw control-created tasks whose ownership did not survive the worker.

        Reconciliation can only cancel IDs; no sandbox claim can create work or widen
        the saved limits of a known attempt.
        """
        known = {
            live.execution.execution_id
            for live in self.hooks.lives()
            if live.execution.state not in TERMINAL_STATES
        }
        try:
            async with asyncio.timeout(self.timeouts().cancel_grace):
                reply = await channel.request("active")
                raw: Any = reply.get("executions")
                if (
                    not isinstance(raw, list)
                    or len(cast(list[object], raw)) > 128
                    or any(
                        not isinstance(key, str) or re.fullmatch(r"exe-[0-9a-f]{24}", key) is None
                        for key in cast(list[object], raw)
                    )
                ):
                    raise WorkerError(ErrorKind.UNAVAILABLE, "invalid executor reconciliation")
                unknown = set(cast(list[str], raw)) - known
                for key in unknown:
                    await channel.request("cancel", execution=key)
                while unknown:
                    reply = await channel.request("active")
                    raw = reply.get("executions")
                    if not isinstance(raw, list) or len(cast(list[object], raw)) > 128:
                        raise WorkerError(ErrorKind.UNAVAILABLE, "invalid executor reconciliation")
                    unknown.intersection_update(
                        item for item in cast(list[object], raw) if isinstance(item, str)
                    )
                    if unknown:
                        await asyncio.sleep(0.05)
        except TimeoutError as exc:
            raise WorkerError(ErrorKind.UNAVAILABLE, "unknown executor tasks did not stop") from exc

    def poll(self, environment_id: str, channel: ExecutorChannel) -> None:
        """Replay the bridge's executions until it closes; one poller per environment."""
        previous = self._pollers.get(environment_id)
        if previous is not None and not previous[1].done():
            if previous[0] is channel:
                return
            previous[1].cancel()
        self._pollers[environment_id] = (
            channel,
            self.hooks.spawn(self._poll_daemon(environment_id, channel)),
        )

    async def _replay(self, live: Live, channel: ExecutorChannel) -> None:
        saved = self.store.recovery(live.execution.execution_id)
        reply = await channel.request(
            "replay",
            execution=live.execution.execution_id,
            after=saved[2] if saved else 0,
            reply_timeout=30,
        )
        if reply.get("found") is not True and live.execution.state not in TERMINAL_STATES:
            self.hooks.lose(live, "the executor with no retained execution record")

    async def _poll_daemon(self, environment_id: str, channel: ExecutorChannel) -> None:
        """Replay the bridge's executions until it closes.

        A failed bridge is closed, which starts a recovery. Any other failure, such as
        one of the store, is logged and the poller goes on, waiting longer while it lasts.
        """
        delay = self.timeouts().poll_interval
        while not self.usability.closing and not channel.closed:
            try:
                for live in list(self.hooks.lives()):
                    if (
                        live.channel is channel
                        and live.execution.state not in TERMINAL_STATES
                        and live.final is None
                    ):
                        await self._replay(live, channel)
                delay = self.timeouts().poll_interval
            except WorkerError:
                await channel.close()
                return
            except Exception:
                _log.exception("polling the executor of %s failed", environment_id)
                delay = min(2 * delay, self.timeouts().poll_retry_max)
            await asyncio.sleep(delay)

    async def recycle(self, environment_id: str) -> None:
        """Fence the whole VM, outside the execution lock, before publishing a replacement.

        OpenShell can refuse a peer's /proc/environ after its ancestor daemon dies.
        Owner-mark scans alone cannot prove its escaped children have stopped. Keep
        the old endpoint durable until the managed lifecycle confirms stop/start.
        """
        recycle_daemon = self.recycle_daemon()
        if recycle_daemon is None:
            raise WorkerError(
                ErrorKind.UNAVAILABLE,
                "executor incarnation lost; replacement refused until the sandbox is recycled",
            )
        binding = self.store.recycle_binding(environment_id)
        if binding is None:
            raise WorkerError(
                ErrorKind.UNAVAILABLE,
                "original sandbox identity was not recorded; replacement refused until "
                "an operator confirms cleanup of the original sandbox",
            )
        old_incarnation = self.store.endpoint_incarnation(environment_id)
        await recycle_daemon(environment_id, *binding)
        if old_incarnation is not None:
            # This callback proved termination of the original immutable VM
            # binding. Incarnation change/transport failure alone never clears.
            self.publication.sandbox_stopped(environment_id, old_incarnation, *binding)
        self.usability.sandbox_stopped(environment_id)

    def incarnation_lost(self, environment_id: str, previous: str) -> None:
        """End the executions sent to the daemon of incarnation ``previous``, now gone."""
        for live in list(self.hooks.lives()):
            saved = self.store.recovery(live.execution.execution_id)
            if (
                live.environment_id == environment_id
                and live.dispatched
                and saved is not None
                and saved[1] == previous
                and live.execution.state not in TERMINAL_STATES
            ):
                self.hooks.lose(live, "the sandbox executor incarnation")
