# SPDX-License-Identifier: MIT
"""A delegated run's own limits (plans-and-delegation spec, PD-38; run-service spec, RS-6a
and RS-6c). Nothing is reserved for a child from its root (the owner, 2026-10-15): each
child is bounded by its project's check tokens and turns, independent of its parent's remaining
calls, and the project's daily cost limit, if it has one, at every run's safe points.
With no limit, nothing stops or refuses a run for spending."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from sandbox_science.contracts import ExecutionContext
from sandbox_science.orchestrator.chat import ModelBudget, RunChat
from sandbox_science.orchestrator.context.agent import AgentContext
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


def _chat(tmp_path: Path) -> tuple[ProjectStore, RunState, RunChat]:
    projects = ProjectStore(tmp_path)
    projects.create(PROJECT, "minimal", "sha256:" + "a" * 64)
    projects.open_session(PROJECT, SESSION, dedicated=False)
    state = RunState(projects.control)
    context = ExecutionContext(project_id=PROJECT, session_id=SESSION, environment_id="e")
    return projects, state, RunChat(state, context, run="root", run_id="root", task="Go.")


def _priced(cost: float) -> Transcript:
    """A run whose calls cost ``cost``, as the gateway reported it."""
    found = Transcript(context=AgentContext(messages=[{"role": "user", "content": "Go."}]))
    found.turns = 1
    found.usage.append(ModelUsage(model="m", input_tokens=10, output_tokens=1, cost=cost))
    return found


def _sized(tokens: int, *, used: int = 0) -> Transcript:
    """A run whose next call sends about ``tokens`` tokens, having used ``used``."""
    found = Transcript(
        context=AgentContext(messages=[{"role": "user", "content": "x" * (4 * tokens)}])
    )
    found.turns = 1
    if used:
        found.usage.append(ModelUsage(model="m", input_tokens=used, output_tokens=0))
    return found


async def test_with_nothing_set_delegate_wait_and_halt_all_work(delegating: Harness) -> None:
    """Under the run service, with no cost limit and no share, four checks each get the
    project's 20,000,000 tokens, the root waits for them and answers: nothing is reserved,
    refused or stopped for spending."""
    free = asyncio.Event()

    async def held() -> None:
        await free.wait()

    async def release() -> None:
        free.set()

    delegating.gateway.delegated = [Reply(REPORT, before=held) for _ in range(4)]
    RunState(ProjectStore(delegating.state).control).post(
        PROJECT, SESSION, "m-1", "Check C-1 four times.", author="user"
    )
    async with serving(
        delegating,
        Reply(calls=[CHECK, CHECK, CHECK, CHECK]),
        Reply(calls_from=wait().calls_from, before=release),
        Reply("Four checks ran."),
    ):
        await ended(delegating, 5)
    (root,) = [r for r in records(delegating) if r.role == "main"]
    started = [json.loads(r) for r in results(root)[:4]]
    assert [s["limits"]["tokens"] for s in started] == [20_000_000] * 4
    assert root.final_text == "Four checks ran."


async def test_a_check_gets_its_configured_turns_even_when_its_parent_has_fewer_left(
    delegating: Harness,
) -> None:
    root = await delegating.run(
        "root",
        [Reply(calls=[CHECK]), wait(), Reply("Done.")],
        delegated=[Reply(REPORT)],
        max_turns=3,
    )
    started = json.loads(results(root)[0])
    assert started["limits"]["turns"] == 300


async def test_a_child_stops_before_a_call_that_would_pass_its_limit(tmp_path: Path) -> None:
    projects, _, chat = _chat(tmp_path)
    try:
        child = ModelBudget(chat, tokens=60_000)
        assert await child.halt(_sized(10_000, used=40_000)) is None
        assert await child.halt(_sized(20_000, used=40_000)) == "run_budget"
    finally:
        projects.close()


async def test_renewed_child_stops_if_its_required_cost_cap_is_removed(tmp_path: Path) -> None:
    projects, state, chat = _chat(tmp_path)
    try:
        child = ModelBudget(chat, tokens=500_000, require_cost_limit=True)
        assert await child.halt(_sized(10_000)) == "budget"
        state.set_cost_limit(PROJECT, 100)
        assert await child.halt(_sized(10_000)) is None
        state.set_cost_limit(PROJECT, None)
        assert await child.halt(_sized(10_000)) == "budget"
    finally:
        projects.close()


async def test_a_child_makes_the_call_its_tokens_leave_room_for_its_report(
    tmp_path: Path,
) -> None:
    """RS-6f: tokens that leave room for the next call but not for one after it make that
    call the child's report round, so it reports rather than stopping without a word."""
    projects, _, chat = _chat(tmp_path)

    def spread(tokens: int, *, calls: int) -> Transcript:
        """``_sized``, its 40,000 tokens used over ``calls`` calls."""
        found = _sized(tokens)
        found.usage.extend(
            ModelUsage(model="m", input_tokens=40_000 // calls, output_tokens=0)
            for _ in range(calls)
        )
        return found

    try:
        child = ModelBudget(chat, tokens=60_000)
        assert await child.last_call(spread(5_000, calls=8)) is None
        assert await child.last_call(spread(7_000, calls=8)) == "run_budget"
        assert await child.halt(spread(7_000, calls=8)) is None  # the call still fits
        # The next call sends at least what the last one did: a call of 20,000 tokens
        # leaves room for one more, not two.
        assert await child.last_call(spread(1_000, calls=2)) == "run_budget"
        assert await ModelBudget(chat, 0.5).last_call(spread(7_000, calls=8)) is None
    finally:
        projects.close()


async def test_a_root_stops_at_its_share_of_the_cost_limit_and_never_without_one(
    tmp_path: Path,
) -> None:
    projects, state, chat = _chat(tmp_path)
    try:
        root, unbounded = ModelBudget(chat, 0.5), ModelBudget(chat)
        assert await root.halt(_priced(10.0)) is None  # no limit: no share of one
        state.set_cost_limit(PROJECT, 1.0)
        assert await root.halt(_priced(0.49)) is None
        assert await root.halt(_priced(0.5)) == "run_budget"
        assert await unbounded.halt(_priced(0.9)) is None  # no share of its own
    finally:
        projects.close()


async def test_a_run_with_no_turn_left_cannot_delegate(tmp_path: Path) -> None:
    from .test_delegation_lifecycle import _delegations

    async def unused(child: Child, stop: asyncio.Event) -> ChildEnd:
        raise AssertionError

    made, _, projects, documents = _delegations(tmp_path, unused)
    try:
        made.turns_left = 0
        with pytest.raises(WorkerAPIError, match="no turn left"):
            await made.call("delegate", BRIEF)
        assert made.children == {}
    finally:
        documents.close()
        projects.close()
