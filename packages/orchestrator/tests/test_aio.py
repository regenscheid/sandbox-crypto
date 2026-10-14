# SPDX-License-Identifier: MIT
"""The lease helper the turn hooks hold their notice leases with."""

from __future__ import annotations

import asyncio
import threading

from sandbox_science.orchestrator.aio import lease
from sandbox_science.testing import FakeClock, eventually


async def test_a_failed_renewal_is_retried_and_never_raised_at_exit() -> None:
    clock = FakeClock()
    calls: list[int] = []

    def renew() -> None:
        calls.append(len(calls))
        if len(calls) == 1:
            raise OSError("the store is busy")

    async with lease(renew, interval=30, name="test", clock=clock):
        for expected in (1, 2):
            await eventually(lambda: clock.sleepers == 1)
            clock.advance(30)
            await eventually(lambda expected=expected: len(calls) == expected)
    assert calls == [0, 1]


async def test_leaving_waits_for_a_renewal_in_progress() -> None:
    clock = FakeClock()
    started, finish = threading.Event(), threading.Event()
    renewed: list[str] = []

    def renew() -> None:
        started.set()
        finish.wait(timeout=30)
        renewed.append("renewed")

    held = lease(renew, interval=30, name="test", clock=clock)
    await held.__aenter__()
    await eventually(lambda: clock.sleepers == 1)
    clock.advance(30)
    await eventually(started.is_set)
    leaving = asyncio.ensure_future(held.__aexit__(None, None, None))
    await asyncio.sleep(0.01)
    assert not leaving.done()
    finish.set()
    await leaving
    assert renewed == ["renewed"]
    clock.advance(60)
    await asyncio.sleep(0.01)
    assert renewed == ["renewed"]
