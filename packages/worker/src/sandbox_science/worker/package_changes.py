# SPDX-License-Identifier: MIT
"""Package changes an executor reports, for the execution manager (W3).

An environment's packages change through package commands, managed or not. The
executor reports each change in a ``revision`` event, with a lockfile for each
attempt's last one, and an environment fingerprint with every result.
``PackageChanges`` counts the environment's revisions from them, keeps a lockfile
for each, asks the executor for the current lockfile when a revision has none, and
records the package epoch kernels start in. Everything an executor sends here is
untrusted and bounded per execution.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Callable
from typing import Any

from pydantic import ValidationError

from sandbox_science.contracts import Location

from .channel import ExecutorChannel, Message
from .connections import Connection, EnvironmentConnections
from .errors import WorkerError
from .journal import ExecutionJournal
from .live import Live
from .revisions import Prepared, Revisions, package_commands
from .store import Store
from .timeouts import ManagerTimeouts

_log = logging.getLogger(__name__)

MAX_REVISIONS = 64
"""``revision`` events one execution may report; later ones are dropped."""
MAX_EXECUTION_LOCKFILES = 4
"""Lockfiles one execution's ``revision`` events may bring or have asked for: one for each
attempt whose package commands made a change. A revision left without one gets the
current lockfile asked for instead, while it is current."""
EXECUTION = re.compile(r"exe-[0-9a-f]{24}")


class PackageChanges:
    """Each environment's revisions as its executor reports them, with their lockfiles."""

    def __init__(
        self,
        store: Store,
        revisions: Revisions,
        connections: EnvironmentConnections,
        journal: ExecutionJournal,
        *,
        spawn: Callable[[Any], asyncio.Task[Any]],
        timeouts: Callable[[], ManagerTimeouts],
    ) -> None:
        self.store = store
        self.revisions = revisions
        self.connections = connections
        self.journal = journal
        self.spawn = spawn
        """Starts a background task that the manager owns and cancels when it closes."""
        self.timeouts = timeouts
        """The manager's timeouts, as they are when used."""
        self._lockfile_locks: dict[str, asyncio.Lock] = {}
        """One lockfile at a time is checked for each environment, in a thread."""
        self._fetching: set[str] = set()
        """Environments whose executor is asked for the current lockfile."""

    def hello(self, environment_id: str, channel: ExecutorChannel) -> None:
        """Note the package state a new executor reports."""
        self.observe(environment_id, channel.hello.get("environment"), "")
        self.store.kernel_starts.epoch(
            environment_id,
            channel.incarnation,
            channel.hello.get("package_epoch"),
            channel.hello.get("environment"),
            remember=True,
        )

    def want_lockfile(self, environment_id: str) -> bool:
        """Ask the executor for the current lockfile if the current revision has none, as
        revision 0 has none at first; return whether it was asked. One request at a time
        for each environment, and nothing waits for it. Executions ask (ask_lockfile),
        each within its budget: revision 0 gets its lockfile as the first one starts."""
        environment = self.connections.get(environment_id)
        channel = environment.channel if environment is not None else None
        if (
            channel is None
            or channel.closed
            or environment_id in self._fetching
            or not self.revisions.needs_lockfile(environment_id)
        ):
            return False
        self._fetching.add(environment_id)
        task = self.spawn(self._fetch_lockfile(environment_id, channel))
        task.add_done_callback(lambda _: self._fetching.discard(environment_id))
        return True

    async def _fetch_lockfile(self, environment_id: str, channel: ExecutorChannel) -> None:
        try:
            reply = await channel.request("lockfile", reply_timeout=self.timeouts().lockfile)
        except WorkerError as error:
            _log.info("environment %s: no lockfile from its executor: %s", environment_id, error)
            return
        if not self.revisions.needs_lockfile(environment_id):
            return  # a revision event brought one meanwhile
        prepared = await self._prepare(environment_id, reply.get("lockfile"))
        if prepared is None or not self.revisions.attach(
            environment_id, reply.get("fingerprint"), prepared
        ):
            _log.info("environment %s: the executor's lockfile was not kept", environment_id)

    async def _prepare(self, environment_id: str, raw: object) -> Prepared | None:
        """Check and store a lockfile from the executor, in a thread, one at a time for each
        environment: the event loop never waits for one (CP-18)."""
        if raw is None:
            return None
        lock = self._lockfile_locks.setdefault(environment_id, asyncio.Lock())
        async with lock:
            try:
                return await asyncio.to_thread(self.revisions.prepare, raw)
            except OSError as exc:
                _log.warning("environment %s: cannot store a lockfile: %s", environment_id, exc)
                return None

    def located(self, environment_id: str, environment: Connection) -> Location | None:
        """The environment's location, with its current revision."""
        location, record = environment.location, self.store.revision(environment_id)
        if location is None or record is None:
            return location
        return location.model_copy(update={"environment_revision": str(record.revision)})

    def revision_after(self, environment_id: str, live: Live, raw: dict[str, Any]) -> str | None:
        """The environment's revision once the attempt's fingerprint is counted. A change
        seen only here is unmanaged, so it is no provisioning operation's, even when the
        execution is one's step."""
        revision = self.observe(environment_id, raw.get("environment"), live.execution.attempt_id)
        self.ask_lockfile(environment_id, live)
        return None if revision is None else str(revision)

    def observe(self, environment_id: str, fingerprint: object, changed_by: str) -> int | None:
        """The environment's revision after the executor reported ``fingerprint`` (W3).

        The first fingerprint is revision 0, and each different one counts one up,
        recorded against the attempt that reported it (empty between executors). The
        executor reports each change in a ``revision`` event first, so a new fingerprint
        here is one it could not account for. The executor is untrusted: research code
        can only make its own environment's revisions count up.
        """
        return self.revisions.observe(environment_id, fingerprint, changed_by)

    async def revised(self, environment_id: str, live: Live, message: Message) -> None:
        """A change to the environment's packages that the executor saw (CP-4, CP-16).

        A managed change names the attempt that ran its package commands, which must be
        one of this environment's; an unmanaged one, the attempt after which it was seen.
        The executor sends a lockfile with each attempt's last change. The worker keeps
        one for each attempt, and checks at most MAX_EXECUTION_LOCKFILES for each
        execution, those it asks for included. A revision left without one gets the
        current lockfile asked for, while it is current.
        """
        live.revisions += 1
        if live.revisions > MAX_REVISIONS:
            if live.revisions == MAX_REVISIONS + 1:
                _log.warning(
                    "execution %s: over %d revisions; dropping the rest",
                    live.execution.execution_id,
                    MAX_REVISIONS,
                )
            return
        managed = message.get("managed")
        cause = message.get("cause")
        attempt, origin = (
            self._attempt(environment_id, cause) if isinstance(cause, str) else ("", None)
        )
        prepared: Prepared | None = None
        raw = message.get("lockfile")
        if (
            raw is not None
            and attempt
            and attempt not in live.lockfile_attempts
            and live.lockfiles < MAX_EXECUTION_LOCKFILES
        ):
            live.lockfiles += 1
            live.lockfile_attempts.add(attempt)
            prepared = await self._prepare(environment_id, raw)
        commands, omitted = (
            package_commands(message.get("commands"), message.get("omitted_commands"))
            if managed is True
            else ((), 0)
        )
        self.revisions.observe(
            environment_id,
            message.get("fingerprint"),
            attempt,
            managed=managed if isinstance(managed, bool) else None,
            commands=commands,
            omitted=omitted,
            lockfile=prepared,
            # Only a change its own package commands made is the operation's.
            operation=origin if managed is True else None,
        )
        if (
            live.channel is not None
            and (connection := self.connections.get(environment_id)) is not None
            and connection.channel is live.channel
        ):
            self.store.kernel_starts.epoch(
                environment_id,
                live.channel.incarnation,
                message.get("package_epoch"),
                message.get("fingerprint"),
                remember=True,
            )
        self.ask_lockfile(environment_id, live)

    def ask_lockfile(self, environment_id: str, live: Live) -> None:
        """Ask for the current lockfile on behalf of ``live``, within its budget: every
        lockfile checked counts, those an execution's start and end ask for too."""
        if live.lockfiles < MAX_EXECUTION_LOCKFILES and self.want_lockfile(environment_id):
            live.lockfiles += 1

    def _attempt(self, environment_id: str, execution_id: str) -> tuple[str, str | None]:
        """The current attempt of one of the environment's executions, and the provisioning
        operation it is a step of; empty for any other execution."""
        if not EXECUTION.fullmatch(execution_id):
            return "", None
        live = self.journal.live.get(execution_id)
        try:
            execution = live.execution if live is not None else self.store.execution(execution_id)
        except ValidationError:
            return "", None
        if execution is None or execution.context.environment_id != environment_id:
            return "", None
        return execution.attempt_id, execution.origin
