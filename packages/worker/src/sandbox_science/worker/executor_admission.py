# SPDX-License-Identifier: MIT
"""Executor launch permits and ordered package observation transitions (stdlib only)."""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
import secrets
import sys
import time
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from . import provisioning
    from .executor import Clock, Running

Message = dict[str, Any]


@dataclass
class _Change:
    """A change to the environment's packages, as one ``revision`` event reports it."""

    fingerprint: str
    managed: bool
    cause: str | None
    """The execution that ran its package commands, or after which it was seen."""
    commands: list[Message] = field(default_factory=list[Message])
    lockfile: object = None
    names: list[str] = field(default_factory=list[str])
    """The journal records it accounts for, which leave the journal once it is sent."""


def _own_limit(running: Running, fallback: float) -> float:
    """An install bound that knows no other attempts: the waiting attempt's own limit."""
    del running
    return fallback


class Admission:
    """Own gate generations, permits, observation serialization and revision identity.

    A permit spans actual launch/readiness and cleanup. Package observation I/O is
    supplied by the coordinator; only this owner publishes and consumes transitions.
    """

    def __init__(
        self,
        clock: Clock,
        *,
        closed: Callable[[], bool],
        error: Callable[[str, str], Exception],
        busy: Callable[[float, Message | None], Exception],
        not_started: type[Exception],
        send: Callable[[Message], Awaitable[None]],
        remove_records: Callable[[Iterable[str]], None],
        usable: Callable[[Message], bool],
        max_lockfile_event: int,
        max_commands: int,
        max_owners: int,
        max_runs: int,
        install_bound: Callable[[Running, float], float] | None = None,
    ) -> None:
        self.clock = clock
        self.install_bound: Callable[[Running, float], float] = install_bound or _own_limit
        """Until when an attempt may wait while a package operation holds the gate (CP-12)."""
        self.closed = closed
        self.error = error
        self.busy = busy
        self.not_started = not_started
        self.send = send
        self.remove_records = remove_records
        self.usable = usable
        self.max_lockfile_event = max_lockfile_event
        self.max_commands = max_commands
        self.max_owners = max_owners
        self.max_runs = max_runs
        self.incarnation = "gate-" + secrets.token_hex(12)
        self.generation: int = -1
        self.mode: str = "hold"
        self.operation: str = ""
        self.revision: int | None = None
        self.launch_lock = asyncio.Lock()
        self.observing = asyncio.Lock()
        self.owners: dict[str, list[tuple[float, str]]] = {}
        self.reported = ""
        self.epoch = 0

    def baseline(self, fingerprint: str) -> str:
        """Record a baseline under observing without inventing revision events."""
        if self.reported and self.reported != fingerprint:
            self.epoch += 1
        self.reported = fingerprint
        return fingerprint

    async def update(
        self, request: Message, restore: Callable[[Message, Message], Awaitable[None]] | None = None
    ) -> Message:
        """ACK a hold only after every launch ordered before it has completed admission.

        A ``workspace_prefix_restore`` baseline is checked by ``restore`` under the launch
        permit, after the generation check and before the new state is recorded. It
        rechecks the controller before settling anything; the ACK then carries the settled
        ``package_epoch`` and ``environment``.
        """
        generation, mode = request.get("generation"), request.get("mode")
        operation, revision = request.get("operation"), request.get("revision")
        if (
            request.get("incarnation") != self.incarnation
            or type(generation) is not int
            or not 0 <= generation < 2**62
            or mode not in ("ready", "hold", "needs_recovery")
            or not isinstance(operation, str)
            or len(operation) > 128
            or (operation and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", operation) is None)
            or (mode == "ready" and operation)
            or (mode == "needs_recovery" and not operation)
            or (revision is not None and (type(revision) is not int or not 0 <= revision < 2**62))
        ):
            raise self.error("invalid_request", "invalid package admission identity or state")
        async with self.launch_lock:
            self.controller_present(request)
            existing = (
                self.generation,
                self.mode,
                self.operation,
                self.revision,
            )
            incoming = (generation, mode, operation, revision)
            if generation < self.generation or (
                generation == self.generation and incoming != existing
            ):
                raise self.error("conflict", "stale package admission generation")
            baseline = request.get("workspace_prefix_restore")
            if baseline is not None:
                if restore is None or mode != "ready" or not isinstance(baseline, dict):
                    raise self.error("invalid_request", "unexpected prefix restoration baseline")
                await restore(cast(Message, baseline), request)
            self.generation, self.mode = generation, mode
            self.operation, self.revision = operation, revision
        return {
            "incarnation": self.incarnation,
            "generation": generation,
            "mode": mode,
            "operation": operation,
            "revision": revision,
            **(
                {"package_epoch": self.epoch, "environment": self.reported}
                if baseline is not None
                else {}
            ),
        }

    def controller_present(self, request: Message) -> None:
        guard = request.get("_admission_guard")
        if callable(guard) and not guard():
            raise self.error("unavailable", "controller detached before admission")

    @contextlib.asynccontextmanager
    async def launch(self, running: Running, timeout: float):
        """Wait without retaining the launch lock; serialize the final launch with hold ACK.

        Waiting while a package operation holds the gate does not count against the
        attempt's time limit (CP-12, ``running.install_waited``); it ends at
        ``install_bound``.
        """
        held_since: float | None = None
        while True:
            await self.launch_lock.acquire()
            if self.closed():
                self.launch_lock.release()
                raise self.error("unavailable", "executor is closing; new launch refused")
            if (
                (running.package_operation or running.kind == "restart")
                and running.controller
                and not running.controller()
            ):
                self.launch_lock.release()
                raise self.error("unavailable", "provisioning controller detached before launch")
            if self.mode == "needs_recovery":
                self.launch_lock.release()
                raise self.error("unavailable", "environment needs_recovery; new work is refused")
            exempt = (
                running.package_operation
                and running.package_operation == self.operation
                and running.package_generation == self.generation
            )
            if self.mode == "ready" or exempt:
                break
            self.launch_lock.release()
            if running.cancel_requested:
                raise self.not_started
            now = self.clock.monotonic()
            held_since = now if held_since is None else held_since
            charged = held_since - running.admitted - running.install_waited
            if charged >= timeout or now >= self.install_bound(running, held_since + timeout):
                raise self.busy(now - running.admitted, None)
            await self.clock.sleep(0.05)
        try:
            if held_since is not None:
                running.install_waited += self.clock.monotonic() - held_since
            if running.cancel_requested:
                raise self.not_started
            running.waited = max(
                running.waited,
                self.clock.monotonic() - running.admitted - running.install_waited,
            )
            if running.waited >= timeout:
                raise self.busy(running.waited, None)
            yield
        finally:
            self.launch_lock.release()

    @contextlib.asynccontextmanager
    async def restart_permit(self, request: Message):
        async with self.launch_lock:
            self.controller_present(request)
            if self.closed() or self.mode != "ready":
                raise self.error(
                    "unavailable", "package admission is closed; kernel restart refused"
                )
            yield

    async def report(
        self, execution: str, observed: provisioning.Observation, cause: str | None
    ) -> None:
        """Send a ``revision`` event, as part of ``execution``, for each change, in order.

        The records of one attempt's package commands, one after another, make one
        managed change, set against that attempt, with its last commands. A change before
        a record, or one no record accounts for, is unmanaged (CP-4), set against
        ``cause``: the attempt after which it was seen, if any. The last change of each
        attempt carries a lockfile, the packages as that attempt left them; the worker
        keeps one for each attempt. A record leaves the journal once its event is sent,
        so that one a cancelled attempt did not send is read again by the next. Hold
        ``observing``.
        """
        changes, spare = self.changes(observed, cause)
        self.remove_records(spare)  # records that changed nothing
        last = {change.cause: index for index, change in enumerate(changes)}
        for index, change in enumerate(changes):
            raw = (
                change.lockfile
                if change.cause is not None and last[change.cause] == index
                else None
            )
            lockfile = cast(Message, raw) if isinstance(raw, dict) else None
            if lockfile is not None:
                size = len(json.dumps(lockfile, separators=(",", ":")))
                if size > self.max_lockfile_event:
                    print(f"executor: a lockfile of {size} bytes is too large", file=sys.stderr)
                    lockfile = None
            self.epoch += 1
            await self.send(
                {
                    "type": "event",
                    "execution": execution,
                    "event": "revision",
                    "fingerprint": change.fingerprint,
                    "package_epoch": self.epoch,
                    "managed": change.managed,
                    "cause": change.cause,
                    "commands": change.commands[-self.max_commands :],
                    "omitted_commands": max(0, len(change.commands) - self.max_commands),
                    "lockfile": lockfile,
                }
            )
            self.reported = change.fingerprint
            self.remove_records(change.names)

    def changes(
        self, observed: provisioning.Observation, cause: str | None
    ) -> tuple[list[_Change], list[str]]:
        """The changes an observation shows, in order, and the records that changed nothing."""
        changes: list[_Change] = []
        spare: list[str] = []
        reported = self.reported
        for name, record in observed.records:
            if not self.usable(record):
                spare.append(name)
                continue
            before, after = str(record["before"]), str(record["after"])
            if before != reported:  # changed meanwhile by something no record accounts for
                changes.append(_Change(before, False, cause))
                reported = before
            if after == reported:
                spare.append(name)
                continue
            command = {
                key: record.get(key)
                for key in ("command", "via", "exit_code", "signal", "started", "ended")
            }
            who = self.cause(record)
            if changes and changes[-1].managed and changes[-1].cause == who:
                change = changes[-1]
                change.fingerprint = after
                change.lockfile = record.get("lockfile")
            else:
                change = _Change(after, True, who, lockfile=record.get("lockfile"))
                changes.append(change)
            change.commands.append(command)
            change.names.append(name)
            reported = after
        if observed.fingerprint is not None and observed.fingerprint != reported:
            changes.append(_Change(observed.fingerprint, False, cause, lockfile=observed.lockfile))
        return changes, spare

    def own(self, owner: str, execution: str) -> None:
        """Remember that ``execution`` runs under ``owner``, a call's or a kernel's mark."""
        runs = self.owners.pop(owner, [])
        runs.append((time.time(), execution))
        self.owners[owner] = runs[-self.max_runs :]
        while len(self.owners) > self.max_owners:
            del self.owners[next(iter(self.owners))]

    def cause(self, record: Message) -> str | None:
        """The attempt that ran a package command: the latest of its owner's to start before it."""
        owner, invoked = record.get("owner"), record.get("invoked")
        runs = self.owners.get(owner) if isinstance(owner, str) else None
        if not runs:
            return None
        if not isinstance(invoked, int | float) or isinstance(invoked, bool):
            return runs[-1][1]
        earlier = [execution for started, execution in runs if started <= invoked]
        return earlier[-1] if earlier else runs[0][1]
