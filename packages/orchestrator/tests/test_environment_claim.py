# SPDX-License-Identifier: MIT
"""Root claims renew without model calls and remain owned until release."""

from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest

from sandbox_science.contracts import ExecutionContext
from sandbox_science.contracts.environment_claims import EnvironmentClaim, EnvironmentClaimResult
from sandbox_science.orchestrator.environment_claim import environment_claim
from sandbox_science.orchestrator.worker_client import WorkerAPIError
from sandbox_science.testing import FakeClock, eventually

CONTEXT = ExecutionContext(project_id="p", session_id="s", environment_id="e")


class Client:
    def __init__(self, clock: FakeClock) -> None:
        self.clock = clock
        self.calls: list[EnvironmentClaim] = []
        self.broken = False
        self.unmanaged = False
        self.sandbox = "original"
        self.renewing: asyncio.Event | None = None
        self.free: asyncio.Event | None = None

    async def claim_environment(self, request: EnvironmentClaim) -> EnvironmentClaimResult:
        self.calls.append(request)
        if not request.seconds:
            return EnvironmentClaimResult(environment_id="e", state="released")
        if self.broken:
            raise OSError("fixture worker unavailable")
        if self.unmanaged:
            return EnvironmentClaimResult(environment_id="e", state="unmanaged")
        if request.sandbox_id is not None and self.renewing is not None and self.free is not None:
            self.renewing.set()
            await self.free.wait()
        return EnvironmentClaimResult(
            environment_id="e",
            state="held",
            sandbox_id=self.sandbox,
            expires_at=self.clock.now() + timedelta(seconds=request.seconds),
        )


@pytest.mark.parametrize("holder", ["root", "20261017T010000000000Z-" + "a" * 128])
async def test_root_keeps_the_same_claim_during_model_only_work_and_releases_it(
    holder: str,
) -> None:
    clock = FakeClock()
    client = Client(clock)
    async with environment_claim(client, CONTEXT, holder, clock=clock):
        for count in range(2, 6):
            await eventually(lambda: clock.sleepers == 1)
            clock.advance(30)
            await eventually(lambda count=count: len(client.calls) == count)
        assert all(call.holder == holder for call in client.calls)
        assert all(call.sandbox_id == "original" for call in client.calls[1:])
    assert client.calls[-1].seconds == 0
    assert client.calls[-1].sandbox_id == "original"
    assert clock.sleepers == 0


@pytest.mark.parametrize("failure", ["unavailable", "replacement", "lapsed"])
async def test_loss_interrupts_the_root_instead_of_silently_losing_its_kernel(failure: str) -> None:
    clock = FakeClock()
    client = Client(clock)
    entered, forever = asyncio.Event(), asyncio.Event()

    async def root() -> None:
        async with environment_claim(client, CONTEXT, "root", clock=clock):
            entered.set()
            await forever.wait()

    task = asyncio.create_task(root())
    await entered.wait()
    await eventually(lambda: clock.sleepers == 1)
    client.broken = failure == "unavailable"
    if failure == "replacement":
        client.sandbox = "new-sandbox"
    clock.advance(120 if failure == "lapsed" else 30)
    with pytest.raises(WorkerAPIError, match="claim was lost"):
        await task
    assert client.calls[-1].seconds == 0
    assert client.calls[-1].sandbox_id == "original"
    assert clock.sleepers == 0


async def test_stop_joins_an_inflight_renewal_before_release() -> None:
    clock = FakeClock()
    client = Client(clock)
    client.renewing, client.free = asyncio.Event(), asyncio.Event()
    entered, forever = asyncio.Event(), asyncio.Event()

    async def root() -> None:
        async with environment_claim(client, CONTEXT, "root", clock=clock):
            entered.set()
            await forever.wait()

    task = asyncio.create_task(root())
    await entered.wait()
    await eventually(lambda: clock.sleepers == 1)
    clock.advance(30)
    await client.renewing.wait()
    for _ in range(2):
        task.cancel()
        await asyncio.sleep(0)  # deliver each cancellation while cleanup retains the renewal
    assert not task.done()
    assert all(call.seconds > 0 for call in client.calls)
    client.free.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert [call.seconds for call in client.calls] == [120, 120, 0]


async def test_unmanaged_backend_reports_no_managed_idle_suspension() -> None:
    clock = FakeClock()
    client = Client(clock)
    client.unmanaged = True
    async with environment_claim(client, CONTEXT, "root", clock=clock):
        assert clock.sleepers == 0
    assert len(client.calls) == 1


async def test_failed_initial_claim_never_enters_the_root() -> None:
    clock = FakeClock()
    client = Client(clock)
    client.broken = True
    with pytest.raises(OSError, match="worker unavailable"):
        async with environment_claim(client, CONTEXT, "root", clock=clock):
            raise AssertionError("a root must not start without its claim")
