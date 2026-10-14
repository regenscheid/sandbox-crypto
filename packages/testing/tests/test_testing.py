# SPDX-License-Identifier: MIT
"""The fake clock and the waiting helper."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from sandbox_science.contracts import Clock, wait_or_sleep
from sandbox_science.testing import FakeClock, eventually


def test_time_moves_only_when_advanced() -> None:
    start = datetime(2026, 5, 1, 12, tzinfo=UTC)
    clock = FakeClock(start, monotonic=50)
    seam: Clock = clock
    assert (seam.now(), seam.monotonic()) == (start, 50)
    clock.advance(2.5)
    assert clock.now() == start + timedelta(seconds=2.5)
    assert clock.monotonic() == 52.5
    assert clock.elapsed == 2.5
    with pytest.raises(ValueError, match="backwards"):
        clock.advance(-1)
    with pytest.raises(ValueError, match="timezone-aware"):
        FakeClock(datetime(2026, 1, 1))


async def test_advance_wakes_only_due_sleepers_in_one_step() -> None:
    clock = FakeClock()
    woke: list[float] = []

    async def sleeper(seconds: float) -> None:
        await clock.sleep(seconds)
        woke.append(seconds)

    tasks = [asyncio.create_task(sleeper(s)) for s in (1, 5, 10)]
    await eventually(lambda: clock.sleepers == 3)
    clock.advance(4.9)
    await eventually(lambda: woke == [1])
    assert clock.sleepers == 2
    clock.advance(0.1)
    await eventually(lambda: woke == [1, 5])
    clock.advance(100)
    await asyncio.gather(*tasks)
    assert woke == [1, 5, 10]
    assert clock.sleepers == 0


async def test_a_loop_that_sleeps_again_starts_from_the_new_time() -> None:
    clock = FakeClock()
    ticks: list[float] = []

    async def heartbeat() -> None:
        while True:
            await clock.sleep(5)
            ticks.append(clock.elapsed)

    task = asyncio.create_task(heartbeat())
    try:
        await eventually(lambda: clock.sleepers == 1)
        clock.advance(12)  # one wake per sleeper and call
        await eventually(lambda: ticks == [12] and clock.sleepers == 1)
        clock.advance(4)
        await asyncio.sleep(0)
        assert ticks == [12]
        clock.advance(1)
        await eventually(lambda: ticks == [12, 17])
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert clock.sleepers == 0


async def test_cancelled_sleep_and_zero_sleep() -> None:
    clock = FakeClock()
    task = asyncio.create_task(clock.sleep(1))
    await eventually(lambda: clock.sleepers == 1)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert clock.sleepers == 0
    await clock.sleep(0)
    await clock.sleep(-1)
    assert clock.elapsed == 0


async def test_wait_or_sleep_returns_on_the_event_or_the_clock() -> None:
    clock = FakeClock()
    stop = asyncio.Event()
    waiting = asyncio.create_task(wait_or_sleep(clock, stop, 10))
    await eventually(lambda: clock.sleepers == 1)
    clock.advance(10)
    assert await waiting is False
    waiting = asyncio.create_task(wait_or_sleep(clock, stop, 10))
    await eventually(lambda: clock.sleepers == 1)
    stop.set()
    assert await waiting is True
    assert clock.sleepers == 0
    assert await wait_or_sleep(clock, stop, 10) is True


async def test_eventually_returns_the_value_and_reports_a_timeout() -> None:
    values = iter([0, None, "ready"])
    assert await eventually(lambda: next(values)) == "ready"

    async def answer() -> int:
        return 7

    assert await eventually(answer) == 7
    with pytest.raises(AssertionError, match=r"the queue drained did not hold within 0\.02 s"):
        await eventually(lambda: False, timeout=0.02, describe="the queue drained")
