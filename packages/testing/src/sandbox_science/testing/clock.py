# SPDX-License-Identifier: MIT
"""A ``Clock`` whose time moves only when a test says so."""

from __future__ import annotations

import asyncio
import threading
from datetime import UTC, datetime, timedelta

START: datetime = datetime(2026, 1, 1, tzinfo=UTC)


class FakeClock:
    """Implements ``sandbox_science.contracts.Clock`` for tests.

    ``now()`` starts at ``start`` and ``monotonic()`` at ``monotonic``; both move
    only by ``advance()``. ``sleep()`` blocks until ``advance()`` reaches its
    deadline. ``advance()`` sets the new time first and then wakes every sleeper
    that is due, each once: a loop that sleeps again after waking starts its next
    interval at the new time. ``sleepers`` counts the sleeps still waiting, so a
    test can wait until a component has reached its sleep before advancing.

    ``now()`` and ``monotonic()`` may be read from threads; ``sleep()`` and
    ``advance()`` run on the event loop.
    """

    def __init__(self, start: datetime = START, *, monotonic: float = 1000.0) -> None:
        if start.tzinfo is None:
            raise ValueError("a fake clock starts at a timezone-aware time")
        self._start = start.astimezone(UTC)
        self._origin = monotonic
        self._elapsed = 0.0
        self._lock = threading.Lock()
        self._waiting: list[tuple[float, asyncio.Future[None]]] = []

    def monotonic(self) -> float:
        with self._lock:
            return self._origin + self._elapsed

    def now(self) -> datetime:
        with self._lock:
            return self._start + timedelta(seconds=self._elapsed)

    @property
    def elapsed(self) -> float:
        """Seconds advanced since the clock was made."""
        with self._lock:
            return self._elapsed

    @property
    def sleepers(self) -> int:
        """Sleeps that have not yet been woken or cancelled."""
        with self._lock:
            return sum(1 for _, future in self._waiting if not future.done())

    async def sleep(self, seconds: float) -> None:
        if seconds <= 0:
            await asyncio.sleep(0)
            return
        future: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        with self._lock:
            self._waiting.append((self._elapsed + seconds, future))
        try:
            await future
        finally:
            with self._lock:
                self._waiting = [entry for entry in self._waiting if entry[1] is not future]

    def advance(self, seconds: float) -> None:
        """Move time forward by ``seconds`` and wake the sleepers now due."""
        if seconds < 0:
            raise ValueError("a clock does not go backwards")
        with self._lock:
            self._elapsed += seconds
            due = [future for deadline, future in self._waiting if deadline <= self._elapsed]
            self._waiting = [entry for entry in self._waiting if entry[1] not in due]
        for future in due:
            future.get_loop().call_soon_threadsafe(_wake, future)


def _wake(future: asyncio.Future[None]) -> None:
    if not future.done():
        future.set_result(None)
