# SPDX-License-Identifier: MIT
"""Which of the files that changed while a call ran are the call's own.

An executor reports the files of the session workspace and of ``shared/`` that changed
between the start and the end of an attempt. All the sessions of a project can share
one environment, and the platform writes into it while calls run, so not every change
is the attempt's. ``Activity`` keeps, for each environment, what else could have changed
its files while a call ran there: the writes of file requests (an upload, a hand-back,
a job's copy back, a saved output, the agent's own ``write_file``), which never come
from a call's processes, and the attempts of every session. ``attribute`` then sorts
the attempt's list:

- a file in another session's upload area is left out: the platform delivered it to
  that session (``contracts.context.other_sessions_upload``);
- a file a file request wrote while the call ran is unattributed: the worker cannot
  tell whether the call wrote it too;
- a file in ``shared/`` is unattributed when an attempt of another session ran in the
  environment meanwhile, since both write the same directory and the worker cannot
  tell their writes apart;
- the rest is the attempt's own.

A session's own workspace is its own: an attempt of another session could write there
only by naming the directory, and the worker attributes it to the session's attempts.
What the worker knows lasts while it runs: an attempt reattached after a restart, or one
past ``MAX_KEPT`` file requests or attempts in its environment, has an account with a
gap, and its ``shared/`` files are then unattributed too. The sync of ``shared/`` with
the project's other environments takes out its own writes first (``SharedSync``).
"""

from __future__ import annotations

import math
from collections import deque
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Final

from sandbox_science.contracts import TERMINAL_STATES, Clock
from sandbox_science.contracts.context import other_sessions_upload

from .live import Live

MAX_KEPT: Final = 4096
"""File requests' writes, and ended attempts, an environment's account keeps; past it,
the oldest go and the attempts running then have a gap in theirs."""


@dataclass
class _Account:
    """One environment's writes by file requests and ended attempts, oldest first."""

    writes: deque[tuple[float, float, str]] = field(default_factory=deque[tuple[float, float, str]])
    """When each began and ended, by the monotonic clock, and its path."""
    attempts: deque[tuple[float, float, str]] = field(
        default_factory=deque[tuple[float, float, str]]
    )
    """When each was dispatched and ended, and its session."""
    forgotten: float = -math.inf
    """The latest end of an entry dropped while an attempt could still need it."""


@dataclass(frozen=True)
class Attributed:
    """An attempt's changed files, sorted."""

    own: tuple[str, ...]
    unattributed: tuple[str, ...]


def _shared(path: str) -> bool:
    return path == "shared" or path.startswith("shared/")


class Activity:
    """What changed each environment's files besides its calls, while calls ran there."""

    def __init__(self, live: Mapping[str, Live], clock: Clock) -> None:
        self.live = live
        """The journal's executions: the running ones say how far back to keep."""
        self.clock = clock
        self._accounts: dict[str, _Account] = {}

    def _running(self, environment_id: str) -> list[Live]:
        return [
            live
            for live in self.live.values()
            if live.environment_id == environment_id
            and live.dispatched
            and live.execution.state not in TERMINAL_STATES
        ]

    def _keep(self, environment_id: str) -> _Account | None:
        """The environment's account, trimmed to what its running attempts can need;
        ``None``, and none kept, when no attempt runs there."""
        running = self._running(environment_id)
        if not running:
            self._accounts.pop(environment_id, None)
            return None
        since = min(live.dispatched_at for live in running)
        account = self._accounts.setdefault(environment_id, _Account())
        for entries in (account.writes, account.attempts):
            while entries and entries[0][1] < since:
                entries.popleft()
        return account

    @staticmethod
    def _bound(account: _Account, entries: deque[tuple[float, float, str]]) -> None:
        while len(entries) > MAX_KEPT:
            account.forgotten = max(account.forgotten, entries.popleft()[1])

    def wrote(self, environment_id: str, path: str, began: float) -> None:
        """A file request wrote ``path`` from ``began`` until now (monotonic clock)."""
        account = self._keep(environment_id)
        if account is not None:
            account.writes.append((began, self.clock.monotonic(), path))
            self._bound(account, account.writes)

    def ended(self, live: Live) -> None:
        """``live`` ended; the attempts still running beside it may need to know."""
        if not live.dispatched:
            return
        account = self._keep(live.environment_id)
        if account is not None:
            ended = live.ended_at or self.clock.monotonic()
            account.attempts.append((live.dispatched_at, ended, live.execution.context.session_id))
            self._bound(account, account.attempts)

    def attribute(
        self, live: Live, written: Iterable[str], deleted: Iterable[str]
    ) -> tuple[Attributed, Attributed]:
        """Sort the files ``live``'s attempt wrote and deleted, as the module says."""
        session = live.execution.context.session_id
        since, until = live.dispatched_at, live.ended_at or self.clock.monotonic()
        account = self._accounts.get(live.environment_id) or _Account()
        requested = {path for began, end, path in account.writes if began <= until and end >= since}
        others = any(
            other is not live
            and other.environment_id == live.environment_id
            and other.dispatched
            and other.execution.state not in TERMINAL_STATES
            and other.execution.context.session_id != session
            and other.dispatched_at <= until
            for other in self.live.values()
        ) or any(
            owner != session and began <= until and end >= since
            for began, end, owner in account.attempts
        )
        gap = live.restored or account.forgotten >= since

        def sort(paths: Iterable[str]) -> Attributed:
            own: list[str] = []
            unattributed: list[str] = []
            for path in paths:
                if other_sessions_upload(session, path):
                    continue
                if path in requested or (_shared(path) and (others or gap)):
                    unattributed.append(path)
                else:
                    own.append(path)
            return Attributed(tuple(own), tuple(unattributed))

        return sort(written), sort(deleted)
