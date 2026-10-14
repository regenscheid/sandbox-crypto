# SPDX-License-Identifier: MIT
"""What stands in the way of using an environment, through ``Usability``'s own methods."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from pathlib import Path

import pytest

from sandbox_science.contracts import ExecutionContext
from sandbox_science.worker.store import Store
from sandbox_science.worker.usability import EnvironmentUse, Usability

CONTEXT = ExecutionContext(project_id="p1", session_id="s1", environment_id="env1")
INCARNATION = "inc-" + "1" * 24


@pytest.fixture
def store(tmp_path: Path) -> Iterator[Store]:
    store = Store(tmp_path / "worker.sqlite")
    yield store
    store.close()


def test_an_environment_with_nothing_in_its_way_is_usable(store: Store) -> None:
    usability = Usability(store)
    assert usability.usable(CONTEXT)
    assert usability.peek("env1") == EnvironmentUse()
    assert usability.peek("env1").is_clear
    usability.close()
    assert not usability.usable(CONTEXT)


def test_one_recovery_at_a_time_and_none_while_closing(store: Store) -> None:
    usability = Usability(store)
    assert usability.begin_recovery("env1")
    assert not usability.begin_recovery("env1")
    assert usability.begin_recovery("env2")
    assert not usability.usable(CONTEXT)
    assert not usability.fenced("env1")
    usability.end_recovery("env1")
    assert usability.usable(CONTEXT)
    assert usability.peek("env1").is_clear
    usability.close()
    assert not usability.begin_recovery("env1")


def test_overlapping_releases_hold_until_the_last_ends(store: Store) -> None:
    usability = Usability(store)
    other_session = CONTEXT.model_copy(update={"session_id": "s2"})
    with usability.releasing_executor("env1"):
        with usability.releasing_executor("env1"):
            assert usability.peek("env1").releases == 2
        assert usability.releasing("env1")
        assert usability.winding_down(CONTEXT)
        assert usability.winding_down(other_session)
        assert not usability.usable(CONTEXT)
    assert not usability.releasing("env1")
    assert usability.usable(CONTEXT)
    assert usability.peek("env1").is_clear


def test_a_session_close_holds_only_that_session(store: Store) -> None:
    usability = Usability(store)
    other_session = CONTEXT.model_copy(update={"session_id": "s2"})
    other_project = CONTEXT.model_copy(update={"project_id": "p2"})
    with usability.closing_session(CONTEXT):
        with usability.closing_session(CONTEXT):
            assert usability.peek("env1").closing_sessions == {("p1", "s1"): 2}
        assert usability.winding_down(CONTEXT)
        assert not usability.usable(CONTEXT)
        assert usability.usable(other_session)
        assert usability.usable(other_project)
        assert not usability.releasing("env1")
    assert usability.usable(CONTEXT)
    assert usability.peek("env1").is_clear


def test_a_persisted_recycle_intent_fences_until_the_sandbox_stops(
    store: Store, tmp_path: Path
) -> None:
    usability = Usability(store)
    store.remember_endpoint("env1", INCARNATION, ("sandbox", "sandbox-id"))
    usability.require_recycle("env1", INCARNATION)
    assert usability.fenced("env1")
    assert usability.recycle_pending("env1")
    assert usability.peek("env1").quarantined

    # A restarted worker reads the intent from the store alone.
    store.close()
    reopened = Store(tmp_path / "worker.sqlite")
    try:
        restarted = Usability(reopened)
        assert restarted.fenced("env1")
        assert not restarted.peek("env1").quarantined
        restarted.sandbox_stopped("env1")
        assert not restarted.fenced("env1")
        assert reopened.endpoint_incarnation("env1") is None
    finally:
        reopened.close()


def test_a_quarantine_alone_fences(store: Store) -> None:
    usability = Usability(store)
    usability.quarantine("env1")
    assert usability.fenced("env1")
    assert not usability.recycle_pending("env1")
    usability.lift_quarantine("env1")
    assert not usability.fenced("env1")
    assert usability.peek("env1").is_clear
    usability.lift_quarantine("env2")  # nothing to lift
    assert usability.peek("env2").is_clear


async def test_a_fence_runs_once_and_ends_with_its_recycle(store: Store) -> None:
    usability = Usability(store)
    started: list[asyncio.Task[None]] = []
    release = asyncio.Event()

    async def recycling() -> None:
        await release.wait()

    def recycle() -> asyncio.Task[None]:
        task = asyncio.create_task(recycling())
        started.append(task)
        return task

    fence = usability.start_fence("env1", recycle)
    assert usability.start_fence("env1", recycle) is fence
    assert len(started) == 1
    assert usability.fence("env1") is fence
    assert usability.fenced("env1")
    release.set()
    await fence
    await asyncio.sleep(0)  # the done callback runs on the next loop iteration
    assert usability.fence("env1") is None
    assert usability.peek("env1").quarantined  # lifted only once the sandbox is proven stopped
    usability.sandbox_stopped("env1")
    assert usability.peek("env1").is_clear


async def test_a_finished_fence_is_not_waited_on_before_its_callback_runs(store: Store) -> None:
    """Awaiting a finished task never yields; a request that kept finding one would spin."""
    usability = Usability(store)

    async def recycling() -> None:
        return None

    finished = asyncio.create_task(recycling())
    await finished
    fence = usability.start_fence("env1", lambda: finished)
    # Done, and its done callback is only scheduled: nothing has yielded since.
    assert fence.done()
    assert usability.fence("env1") is None
    assert usability.fenced("env1")  # still quarantined until the sandbox is proven stopped
    second = usability.start_fence("env1", lambda: asyncio.create_task(recycling()))
    assert second is not fence
    assert usability.fence("env1") is second
    await second
    await asyncio.sleep(0)
    assert usability.fence("env1") is None
    usability.sandbox_stopped("env1")
    assert usability.peek("env1").is_clear


async def test_overlapping_facts_hold_the_record_until_the_last_clears(store: Store) -> None:
    usability = Usability(store)
    release = asyncio.Event()

    async def recycling() -> None:
        await release.wait()

    store.remember_endpoint("env1", INCARNATION, ("sandbox", "sandbox-id"))
    usability.require_recycle("env1", INCARNATION)
    assert usability.begin_recovery("env1")
    fence = usability.start_fence("env1", lambda: asyncio.create_task(recycling()))
    with usability.releasing_executor("env1"), usability.closing_session(CONTEXT):
        use = usability.peek("env1")
        assert use.recovering
        assert use.fence is fence
        assert use.quarantined
        assert use.releases == 1
        assert use.closing_sessions == {("p1", "s1"): 1}
    usability.end_recovery("env1")
    usability.sandbox_stopped("env1")  # inside the fence, as the manager's recycle does
    assert usability.fenced("env1")  # the fence still runs
    release.set()
    await fence
    await asyncio.sleep(0)
    assert usability.peek("env1").is_clear
    assert usability.usable(CONTEXT)


async def test_a_cancelled_release_or_session_close_still_unwinds(store: Store) -> None:
    usability = Usability(store)
    entered = asyncio.Event()

    async def releasing() -> None:
        with usability.releasing_executor("env1"), usability.closing_session(CONTEXT):
            entered.set()
            await asyncio.Event().wait()

    task = asyncio.create_task(releasing())
    await entered.wait()
    assert usability.winding_down(CONTEXT)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert usability.peek("env1").is_clear
    assert usability.usable(CONTEXT)


def test_peek_is_a_copy(store: Store) -> None:
    usability = Usability(store)
    usability.peek("env1").quarantined = True
    assert not usability.fenced("env1")
    with usability.closing_session(CONTEXT):
        usability.peek("env1").closing_sessions.clear()
        assert usability.winding_down(CONTEXT)
