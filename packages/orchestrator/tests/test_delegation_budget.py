# SPDX-License-Identifier: MIT
"""A run tree's share of the project's model budget (plans-and-delegation spec, PD-38;
run-service spec, RS-6a and RS-6c): reservations from the root's share, the floor it
keeps, refusals, what returns when a child ends, and calls in flight."""

from __future__ import annotations

import asyncio
import contextlib
import json
from pathlib import Path
from typing import Any

import pytest

from sandbox_science.contracts import ExecutionContext
from sandbox_science.orchestrator.chat import ModelBudget, RunChat
from sandbox_science.orchestrator.delegation.budget import FLOOR, MIN_CHILD, TreeBudget
from sandbox_science.orchestrator.delegation.hook import Child, ChildEnd
from sandbox_science.orchestrator.loop import Transcript
from sandbox_science.orchestrator.projects import ModelUsage, ProjectStore
from sandbox_science.orchestrator.runstate import RunState
from sandbox_science.orchestrator.worker_client import WorkerAPIError

from .loop_harness import PROJECT, SESSION, Harness, Reply
from .loop_harness import gateway as gateway
from .loop_harness import harness as harness
from .loop_harness import worker as worker
from .test_delegation import CHECK, REPORT, results, wait
from .test_delegation import delegating as delegating
from .test_delegation_lifecycle import BRIEF
from .test_run_service import ended, records, serving


def tree(allowance: int) -> TreeBudget:
    async def read() -> int:
        return allowance

    return TreeBudget(read)


async def test_children_are_reserved_from_the_roots_share_above_its_floor() -> None:
    found = tree(500_000)
    found.used(None, 10_000)  # the root's own calls
    assert await found.reserve("a", 150_000) == 150_000
    assert await found.reserve("b", 150_000) == 150_000
    third = await found.reserve("c", 150_000)
    assert third == 500_000 - 10_000 - 300_000 - FLOOR
    assert await found.reserve("d", 150_000) is None  # less than MIN_CHILD is left
    assert found.spent() == 500_000 - FLOOR
    # A child that used little returns the rest when it ends.
    found.ended("a", 20_000)
    assert found.spent() == 500_000 - FLOOR - 130_000
    assert await found.reserve("e", 150_000) == 130_000


async def test_a_running_child_counts_as_its_reservation_or_what_it_used() -> None:
    found = tree(1_000_000)
    await found.reserve("a", 150_000)
    found.used("a", 40_000)
    assert found.spent() == 150_000
    found.used("a", 170_000)  # one call past its limit
    assert found.spent() == 170_000
    found.ended("a")
    assert found.spent() == 170_000


async def test_calls_in_flight_count_toward_a_reservation() -> None:
    found = tree(400_000)
    found.calling(None, 60_000)
    found.calling("a", 50_000)
    assert found.in_flight() == 110_000
    assert found.in_flight(besides="a") == 60_000
    assert found.in_flight(root=True) == 50_000
    assert await found.reserve("b", 150_000) == 150_000  # 190,000 were left
    found.calling(None, 200_000)  # the root's next call is larger
    assert await found.reserve("c", 150_000) is None
    found.called(None)
    found.called("a")
    assert found.in_flight() == 0


def _chat(tmp_path: Path) -> tuple[ProjectStore, RunState, RunChat]:
    projects = ProjectStore(tmp_path)
    projects.create(PROJECT, "minimal", "sha256:" + "a" * 64)
    projects.open_session(PROJECT, SESSION, dedicated=False)
    state = RunState(projects.control)
    state.set_budget(PROJECT, 1_000_000)
    context = ExecutionContext(project_id=PROJECT, session_id=SESSION, environment_id="e")
    return projects, state, RunChat(state, context, run="root", run_id="root", task="Go.")


def _run(tokens: int) -> Transcript:
    found = Transcript(messages=[{"role": "user", "content": "Go."}])
    found.usage.append(ModelUsage(model="m", input_tokens=tokens, output_tokens=0))
    return found


async def test_the_roots_budget_is_its_trees(tmp_path: Path) -> None:
    """The root stops once its calls and its children's shares reach its share."""
    projects, _, chat = _chat(tmp_path)
    try:
        found = tree(500_000)
        root = ModelBudget(chat, 0.5, tree=found)
        assert await root.halt(_run(100_000)) is None
        await found.reserve("a", 150_000)
        await found.reserve("b", 150_000)
        assert await root.halt(_run(150_000)) is None
        assert await root.halt(_run(200_000)) == "run_budget"
        found.ended("b", 0)
        assert await root.halt(_run(200_000)) is None
    finally:
        projects.close()


async def test_the_projects_budget_counts_the_trees_calls_in_flight(tmp_path: Path) -> None:
    projects, _, chat = _chat(tmp_path)
    try:
        found = tree(500_000)
        await found.reserve("a", 150_000)
        root = ModelBudget(chat, 0.5, tree=found)
        child = ModelBudget(chat, tokens=150_000, tree=found, member="a")
        assert await root.halt(_run(0)) is None
        # The child's call, as long as the project's whole budget, is in flight.
        await child.notices(
            "turn", Transcript(messages=[{"role": "user", "content": "x" * 4_000_000}])
        )
        assert await root.halt(_run(0)) == "budget"
        assert await child.halt(_run(0)) is None  # its own call does not count twice
        await child.model_returned(_run(10))
        assert await root.halt(_run(0)) is None
        async with child.lease():
            await child.notices(
                "turn", Transcript(messages=[{"role": "user", "content": "x" * 4_000_000}])
            )
        assert found.in_flight(root=True) == 0  # a run that ends calls no more
    finally:
        projects.close()


async def test_the_run_service_reserves_each_check_from_the_roots_share(
    delegating: Harness,
) -> None:
    """With a 1,000,000 budget the root's share is 500,000: two checks of 150,000, one of
    what is left above the floor, and a fourth refused."""
    RunState(ProjectStore(delegating.state).control).set_budget(PROJECT, 1_000_000)
    free = asyncio.Event()

    async def held() -> None:
        await free.wait()

    async def release() -> None:
        free.set()

    delegating.gateway.delegated = [Reply(REPORT, before=held) for _ in range(3)]
    RunState(ProjectStore(delegating.state).control).post(
        PROJECT, SESSION, "m-1", "Check C-1 four times.", author="user"
    )
    async with serving(
        delegating,
        Reply(calls=[CHECK, CHECK, CHECK, CHECK]),
        Reply(calls_from=wait().calls_from, before=release),
        Reply("Three checks ran."),
    ):
        await ended(delegating, 4)
    (root,) = [r for r in records(delegating) if r.role == "main"]
    started = [json.loads(r) for r in results(root)[:4]]
    tokens = [s["limits"]["tokens"] for s in started[:3]]
    assert tokens[:2] == [150_000, 150_000]
    assert MIN_CHILD <= tokens[2] < 100_000
    refused = started[3]["error"]
    assert refused["kind"] == "unavailable"
    assert refused["message"].startswith("limit_reached")
    assert root.final_text == "Three checks ran."


async def test_a_check_gets_no_more_turns_than_its_parent_has_left(delegating: Harness) -> None:
    root = await delegating.run(
        "root",
        [Reply(calls=[CHECK]), wait(), Reply("Done.")],
        delegated=[Reply(REPORT)],
        max_turns=3,
    )
    started = json.loads(results(root)[0])
    assert started["limits"]["turns"] == 2


def _sized(tokens: int, *, used: int = 0) -> Transcript:
    """A run whose next call sends about ``tokens`` tokens, having used ``used``."""
    found = Transcript(messages=[{"role": "user", "content": "x" * (4 * tokens)}])
    found.turns = 1
    if used:
        found.usage.append(ModelUsage(model="m", input_tokens=used, output_tokens=0))
    return found


async def test_runs_checking_at_once_cannot_all_pass(tmp_path: Path) -> None:
    """Eleven children each about to send 200,000 tokens against a 1,000,000 budget:
    each registers its call as it passes, so only as many pass as fit in the budget
    together: four."""
    projects, _, chat = _chat(tmp_path)
    try:
        found = tree(100_000_000)
        budgets = []
        for n in range(11):
            member = f"c{n}"
            found.children[member] = _share(10_000_000)
            budgets.append(ModelBudget(chat, tokens=10_000_000, tree=found, member=member))
        stops = await asyncio.gather(*(b.halt(_sized(200_000)) for b in budgets))
        passed = [stop for stop in stops if stop is None]
        assert len(passed) == 4
        assert found.in_flight() < 1_000_000
        assert stops.count("budget") == 11 - len(passed)
    finally:
        projects.close()


def _share(tokens: int) -> Any:
    from sandbox_science.orchestrator.delegation import budget

    return budget._Share(tokens)


async def test_a_child_stops_before_a_call_that_would_pass_its_limit(tmp_path: Path) -> None:
    projects, _, chat = _chat(tmp_path)
    try:
        found = tree(1_000_000)
        await found.reserve("a", 60_000)
        child = ModelBudget(chat, tokens=60_000, tree=found, member="a")
        assert await child.halt(_sized(10_000, used=40_000)) is None
        assert await child.halt(_sized(20_000, used=40_000)) == "run_budget"
        assert found.in_flight() == 0  # a call it will not make is not in flight
    finally:
        projects.close()


async def test_a_calls_estimate_stays_until_its_event_holds_its_usage(tmp_path: Path) -> None:
    projects, _, chat = _chat(tmp_path)
    try:
        found = tree(1_000_000)
        root = ModelBudget(chat, 0.5, tree=found)
        run = _sized(10_000)
        assert await root.halt(run) is None
        coming = found.in_flight()
        assert coming >= 10_000
        run.usage.append(ModelUsage(model="m", input_tokens=10_000, output_tokens=500))
        await root.model_returned(run)
        assert found.in_flight() == coming  # its assistant event is not written yet
        await root.observe_tool(run, {})
        assert found.in_flight() == 0
        assert (found.root, found.root_last) == (10_500, 10_500)
    finally:
        projects.close()


async def test_a_runs_lease_clears_its_call_whatever_ends_it(tmp_path: Path) -> None:
    projects, _, chat = _chat(tmp_path)
    try:
        found = tree(1_000_000)
        child = ModelBudget(chat, tokens=150_000, tree=found, member="a")

        async def failed() -> None:
            async with child.lease():
                await child.notices("turn", Transcript(messages=[]))
                raise RuntimeError("the call failed")

        with pytest.raises(RuntimeError):
            await failed()
        assert found.in_flight() == 0

        async def call() -> None:
            async with child.lease():
                await child.notices("turn", Transcript(messages=[]))
                await asyncio.Event().wait()

        task = asyncio.create_task(call())
        await asyncio.sleep(0)
        assert found.in_flight() > 0
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert found.in_flight() == 0
    finally:
        projects.close()


async def test_the_root_keeps_twice_its_last_call() -> None:
    found = tree(1_000_000)
    found.used(None, 120_000, last=120_000)
    assert await found.reserve("a", 1_000_000) == 1_000_000 - 120_000 - 240_000


async def test_a_childs_call_counts_only_past_its_reservation() -> None:
    found = tree(1_000_000)
    await found.reserve("a", 150_000)
    found.calling("a", 100_000)  # within its reservation
    assert found.committed() == 150_000
    found.used("a", 100_000)
    assert found.committed() == 200_000  # 100,000 used and 100,000 more in flight


async def _tree_delegations(tmp_path: Path, runner: Any) -> tuple[Any, TreeBudget, Any, Any]:
    from .test_delegation_lifecycle import _delegations

    made, _, projects, documents = _delegations(tmp_path, runner)
    found = tree(1_000_000)
    made.tree = found
    return made, found, projects, documents


async def test_a_reservation_returns_however_its_child_ends(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sandbox_science.orchestrator.delegation import hook

    async def failing(child: Child, stop: asyncio.Event) -> ChildEnd:
        raise RuntimeError("the runner failed")

    made, found, projects, documents = await _tree_delegations(tmp_path, failing)
    try:
        async with made.lease():
            await made.call("delegate", BRIEF)
            await asyncio.gather(*(c.task for c in made.children.values()))
        assert found.spent() == 0  # a failed runner returns all of it
        async with made.lease():
            await made.call("delegate", BRIEF)  # cancelled before it starts
        assert found.spent() == 0
        monkeypatch.setattr(hook, "STOP_SECONDS", 0.05)
        never = asyncio.Event()

        async def stubborn(child: Child, stop: asyncio.Event) -> ChildEnd:
            while not never.is_set():
                with contextlib.suppress(asyncio.CancelledError):
                    await never.wait()
            return ChildEnd("cancelled", False, None, used=7)

        made.runner = stubborn
        async with made.lease():
            await made.call("delegate", BRIEF)
            await asyncio.sleep(0.01)
        assert found.spent() == 0  # interrupted: what it used so far, nothing
        never.set()
    finally:
        documents.close()
        projects.close()


async def test_a_run_with_no_turn_left_cannot_delegate(tmp_path: Path) -> None:
    async def unused(child: Child, stop: asyncio.Event) -> ChildEnd:
        raise AssertionError

    made, _, projects, documents = await _tree_delegations(tmp_path, unused)
    try:
        made.turns_left = 0
        with pytest.raises(WorkerAPIError, match="no turn left"):
            await made.call("delegate", BRIEF)
        assert made.children == {}
    finally:
        documents.close()
        projects.close()
