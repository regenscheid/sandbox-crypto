# SPDX-License-Identifier: MIT
"""The system clock behind the ``Clock`` seam."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from sandbox_science.contracts import SYSTEM_CLOCK, wait_or_sleep


async def test_the_system_clock_reads_real_time() -> None:
    before = datetime.now(UTC)
    now = SYSTEM_CLOCK.now()
    assert now.tzinfo is not None
    assert before <= now <= datetime.now(UTC)
    first = SYSTEM_CLOCK.monotonic()
    await SYSTEM_CLOCK.sleep(0)
    assert SYSTEM_CLOCK.monotonic() >= first


async def test_wait_or_sleep_on_the_system_clock() -> None:
    stop = asyncio.Event()
    assert await wait_or_sleep(SYSTEM_CLOCK, stop, 0) is False
    stop.set()
    assert await wait_or_sleep(SYSTEM_CLOCK, stop, 3600) is True
