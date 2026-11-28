# SPDX-License-Identifier: MIT
"""Root claims renew without model calls and remain owned until release."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

import pytest

from sandbox_science.contracts import ErrorKind, ExecutionContext
from sandbox_science.contracts.environment_claims import (
    MAX_CLAIM_SECONDS,
    WORKER_OUTAGE_SECONDS,
    EnvironmentClaim,
    EnvironmentClaimResult,
)
from sandbox_science.orchestrator.environment_claim import (
    ENDED,
    LAPSED,
    UNREACHABLE,
    environment_claim,
)
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
    lost: list[str] = []

    async def root() -> None:
        async with environment_claim(
            client, CONTEXT, "root", clock=clock, lost=lambda reason, *_: lost.append(reason)
        ):
            entered.set()
            await forever.wait()

    task = asyncio.create_task(root())
    await entered.wait()
    await eventually(lambda: clock.sleepers == 1)
    client.broken = failure == "unavailable"
    if failure == "replacement":
        client.sandbox = "new-sandbox"
    if failure == "unavailable":
        # Retried, with a growing wait, until the outage window from the last renewal.
        while not task.done():
            await eventually(lambda: task.done() or clock.sleepers == 1)
            if not task.done():
                clock.advance(5)
        assert clock.elapsed >= WORKER_OUTAGE_SECONDS
    else:
        clock.advance(MAX_CLAIM_SECONDS if failure == "lapsed" else 30)
    with pytest.raises(WorkerAPIError, match="claim was lost"):
        await task
    # Only a worker away past the window is told as unreachable, a transient loss.
    assert lost == [{"unavailable": UNREACHABLE, "replacement": ENDED, "lapsed": LAPSED}[failure]]
    assert client.calls[-1].seconds == 0
    assert client.calls[-1].sandbox_id == "original"
    assert clock.sleepers == 0


async def test_the_first_grant_rides_out_an_unreachable_worker() -> None:
    """A run that starts while the worker restarts waits for it within the window."""
    clock = FakeClock()
    client = Client(clock)
    client.broken = True
    entered = asyncio.Event()

    async def root() -> None:
        async with environment_claim(client, CONTEXT, "root", clock=clock):
            entered.set()

    task = asyncio.create_task(root())
    while clock.elapsed < 120:  # two minutes of a restarting worker
        await eventually(lambda: clock.sleepers == 1)
        clock.advance(10)
    assert not entered.is_set()
    client.broken = False
    while not task.done():
        await eventually(lambda: task.done() or clock.sleepers == 1)
        if not task.done():
            clock.advance(10)
    await task
    assert entered.is_set()
    assert clock.elapsed < WORKER_OUTAGE_SECONDS


async def test_a_first_grant_that_fails_past_the_window_never_enters_the_root() -> None:
    clock = FakeClock()
    client = Client(clock)
    client.broken = True
    task = asyncio.create_task(_enter(client, clock))
    while not task.done():
        await eventually(lambda: task.done() or clock.sleepers == 1)
        if not task.done():
            clock.advance(10)
    with pytest.raises(OSError, match="unavailable"):
        await task
    assert clock.elapsed >= WORKER_OUTAGE_SECONDS


async def _enter(client: Client, clock: FakeClock) -> None:
    async with environment_claim(client, CONTEXT, "root", clock=clock):
        raise AssertionError("a root must not start without its claim")


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
    assert [call.seconds for call in client.calls] == [MAX_CLAIM_SECONDS, MAX_CLAIM_SECONDS, 0]


async def test_unmanaged_backend_reports_no_managed_idle_suspension() -> None:
    clock = FakeClock()
    client = Client(clock)
    client.unmanaged = True
    async with environment_claim(client, CONTEXT, "root", clock=clock):
        assert clock.sleepers == 0
    assert len(client.calls) == 1


class PolicyClient(Client):
    """A worker whose environment moves to another policy revision mid-claim."""

    def __init__(self, clock: FakeClock) -> None:
        super().__init__(clock)
        self.policy = "sha256:a"

    async def claim_environment(self, request: EnvironmentClaim) -> EnvironmentClaimResult:
        if request.seconds and request.context.policy_revision != self.policy:
            self.calls.append(request)
            raise WorkerAPIError(ErrorKind.POLICY_DENIED, "environment claim's policy is stale")
        return await super().claim_environment(request)


async def test_a_claim_follows_the_projects_new_policy_and_the_run_goes_on() -> None:
    clock = FakeClock()
    client = PolicyClient(clock)
    project = {"policy": "sha256:a"}
    followed: list[str] = []

    async def follow() -> str | None:
        return project["policy"]

    context = CONTEXT.model_copy(update={"policy_revision": "sha256:a"})
    async with environment_claim(
        client, context, "root", clock=clock, follow=follow, followed=followed.append
    ):
        await eventually(lambda: clock.sleepers == 1)
        # The operator changes the project's policy, and the environment is revised to it.
        project["policy"] = client.policy = "sha256:b"
        clock.advance(30)
        await eventually(lambda: followed == ["sha256:b"])
        await eventually(lambda: clock.sleepers == 1)
        clock.advance(30)
        await eventually(lambda: client.calls[-1].context.policy_revision == "sha256:b")
    assert client.calls[-1].seconds == 0
    assert client.calls[-1].context.policy_revision == "sha256:b"


async def test_a_policy_refusal_the_project_does_not_explain_ends_the_run() -> None:
    clock = FakeClock()
    client = PolicyClient(clock)

    async def follow() -> str | None:
        return "sha256:a"  # the project did not move: the environment's change is not its own

    context = CONTEXT.model_copy(update={"policy_revision": "sha256:a"})
    entered, forever = asyncio.Event(), asyncio.Event()

    async def root() -> None:
        async with environment_claim(client, context, "root", clock=clock, follow=follow):
            entered.set()
            await forever.wait()

    task = asyncio.create_task(root())
    await entered.wait()
    await eventually(lambda: clock.sleepers == 1)
    client.policy = "sha256:other"
    clock.advance(30)
    with pytest.raises(WorkerAPIError, match="claim was lost"):
        await task


async def test_an_outage_inside_the_window_is_ridden_out_and_said() -> None:
    """A worker that stops answering, as one whose Pod restarts does, and answers again
    within the outage window: the root goes on, each renewal asks for the longest claim,
    and the run hears when the worker went away, until when it waits, and that it is back."""
    clock = FakeClock()
    client = Client(clock)
    heard: list[tuple[object, object]] = []
    lost: list[str] = []
    entered, done = asyncio.Event(), asyncio.Event()

    async def root() -> None:
        async with environment_claim(
            client,
            CONTEXT,
            "root",
            clock=clock,
            outage=lambda since, until: heard.append((since, until)),
            lost=lambda reason, *_: lost.append(reason),
        ):
            entered.set()
            await done.wait()

    task = asyncio.create_task(root())
    await entered.wait()
    await eventually(lambda: clock.sleepers == 1)
    client.broken = True
    started = clock.now()
    while clock.elapsed < 150:  # two and a half minutes away
        await eventually(lambda: clock.sleepers == 1)
        clock.advance(5)
    await eventually(lambda: clock.sleepers == 1)
    assert not task.done()
    ((since, until),) = heard
    assert isinstance(since, datetime)
    assert isinstance(until, datetime)
    assert since >= started
    assert until == started + timedelta(seconds=WORKER_OUTAGE_SECONDS)
    client.broken = False
    clock.advance(30)
    await eventually(lambda: len(heard) == 2)
    assert heard[-1] == (None, None)
    assert not lost
    assert all(call.seconds == MAX_CLAIM_SECONDS for call in client.calls if call.seconds)
    done.set()
    await task
