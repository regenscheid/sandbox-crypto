# SPDX-License-Identifier: MIT
"""The clock that components with deadlines, leases or polling read time from.

Components take a ``Clock`` and default to ``SYSTEM_CLOCK``. Tests pass a fake
clock instead, so that a lease expiry or a renewal interval happens when the test
advances time, not after a real wait.
"""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime
from typing import Final, Protocol


class Clock(Protocol):
    """Time as a component sees it."""

    def monotonic(self) -> float:
        """Seconds for measuring intervals; only differences are meaningful."""
        ...

    def now(self) -> datetime:
        """The current UTC time, timezone-aware."""
        ...

    async def sleep(self, seconds: float) -> None:
        """Wait ``seconds`` of this clock's time."""
        ...


class SystemClock:
    """The real clock: ``time.monotonic``, ``datetime.now(UTC)`` and ``asyncio.sleep``."""

    def monotonic(self) -> float:
        return time.monotonic()

    def now(self) -> datetime:
        return datetime.now(UTC)

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)


SYSTEM_CLOCK: Final[Clock] = SystemClock()


async def wait_or_sleep(clock: Clock, event: asyncio.Event, seconds: float) -> bool:
    """Wait until ``event`` is set or ``seconds`` of ``clock`` time pass.

    Returns whether the event was set. A renewal loop uses it to sleep between
    renewals and still stop at once when its owner asks.
    """
    if event.is_set():
        return True
    stopped = asyncio.ensure_future(event.wait())
    slept = asyncio.ensure_future(clock.sleep(seconds))
    try:
        await asyncio.wait((stopped, slept), return_when=asyncio.FIRST_COMPLETED)
    finally:
        for task in (stopped, slept):
            task.cancel()
        await asyncio.gather(stopped, slept, return_exceptions=True)
    return event.is_set()
