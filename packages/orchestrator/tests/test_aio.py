# SPDX-License-Identifier: MIT
"""The lease helper the turn hooks hold their notice leases with, owned waits, and the
executor whose admitted work outlives its callers."""

from __future__ import annotations

import asyncio
import logging
import threading

import pytest

from sandbox_science.orchestrator.aio import OwnedExecutor, held, lease
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


async def test_held_reports_the_callers_cancellation_with_the_result() -> None:
    release = asyncio.Event()

    async def work() -> str:
        await release.wait()
        return "process"

    caller = asyncio.create_task(held(work()))
    await asyncio.sleep(0)
    caller.cancel()  # twice: the work is still waited for
    await asyncio.sleep(0)
    caller.cancel()
    await asyncio.sleep(0)
    release.set()
    assert await caller == ("process", True)


async def test_the_executor_bounds_admission_and_drains_detached_work() -> None:
    executor = OwnedExecutor(2, "test")
    started, finish = threading.Event(), threading.Event()
    done: list[int] = []

    async def work(n: int) -> int:
        def call() -> int:
            started.set()
            finish.wait(timeout=30)
            done.append(n)
            return n

        return await executor.thread(call)

    callers = [asyncio.create_task(executor.submit(work(n))) for n in range(2)]
    await asyncio.to_thread(started.wait, 30)
    assert not executor.admits
    with pytest.raises(RuntimeError, match="admission"):
        await executor.submit(work(9))
    for caller in callers:
        caller.cancel()  # the callers go; the work does not
    await asyncio.gather(*callers, return_exceptions=True)
    closing = asyncio.create_task(executor.aclose())
    await asyncio.sleep(0.05)
    closing.cancel()  # a cancelled close still drains
    finish.set()
    with pytest.raises(asyncio.CancelledError):
        await closing
    await executor.aclose()
    assert sorted(done) == [0, 1]
    assert not executor.admits


async def test_a_detached_callers_later_failure_is_logged(
    caplog: pytest.LogCaptureFixture,
) -> None:
    executor = OwnedExecutor(1, "test")
    release = asyncio.Event()

    async def failing() -> None:
        await release.wait()
        raise RuntimeError("the work fails after its caller left")

    caplog.set_level(logging.ERROR)
    caller = asyncio.create_task(executor.submit(failing()))
    await asyncio.sleep(0)
    caller.cancel()
    await asyncio.gather(caller, return_exceptions=True)
    release.set()
    await executor.aclose()
    (record,) = [r for r in caplog.records if r.exc_info]
    assert "after its caller left" in record.getMessage()
    assert record.exc_info is not None
    assert str(record.exc_info[1]) == "the work fails after its caller left"
    assert executor.pending == 0


async def test_an_awaited_failure_reaches_its_caller_and_is_not_logged(
    caplog: pytest.LogCaptureFixture,
) -> None:
    executor = OwnedExecutor(1, "test")

    async def failing() -> None:
        raise RuntimeError("the caller sees this")

    caplog.set_level(logging.ERROR)
    with pytest.raises(RuntimeError, match="caller sees this"):
        await executor.submit(failing())
    await executor.aclose()
    assert not [r for r in caplog.records if r.exc_info]


async def test_no_work_is_left_after_close() -> None:
    executor = OwnedExecutor(3, "test")
    release = asyncio.Event()

    async def work(n: int) -> int:
        await release.wait()
        return await executor.thread(lambda: n)

    callers = [asyncio.create_task(executor.submit(work(n))) for n in range(3)]
    await asyncio.sleep(0)
    assert executor.pending == 3
    callers[0].cancel()
    await asyncio.sleep(0)
    closing = asyncio.create_task(executor.aclose())
    for _ in range(3):
        await asyncio.sleep(0)
        closing.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await closing
    assert [await callers[1], await callers[2]] == [1, 2]
    await asyncio.sleep(0)
    assert executor.pending == 0
    assert not asyncio.all_tasks() - {asyncio.current_task()}
