# SPDX-License-Identifier: MIT
"""A delegated run's own limits (plans-and-delegation spec, PD-38; run-service spec, RS-6a
and RS-6c). Nothing is reserved for a child from its root (the owner, 2026-10-15): each
child is bounded by its project's check tokens and turns, no more turns than its parent
has left, and the project's budget at every run's safe points."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from sandbox_science.contracts import ExecutionContext
from sandbox_science.orchestrator.chat import ModelBudget, RunChat
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
    state.set_budget(PROJECT, 1_000_000)
    context = ExecutionContext(project_id=PROJECT, session_id=SESSION, environment_id="e")
    return projects, state, RunChat(state, context, run="root", run_id="root", task="Go.")


def _sized(tokens: int, *, used: int = 0) -> Transcript:
    """A run whose next call sends about ``tokens`` tokens, having used ``used``."""
    found = Transcript(messages=[{"role": "user", "content": "x" * (4 * tokens)}])
    found.turns = 1
    if used:
        found.usage.append(ModelUsage(model="m", input_tokens=used, output_tokens=0))
    return found


async def test_the_run_service_reserves_nothing_for_its_checks(delegating: Harness) -> None:
    """Under the run service, with the root's share of a 1,000,000 budget 500,000, four
    checks each get the project's 150,000 tokens: nothing is reserved from the root and
    nothing refused for the budget."""
    RunState(ProjectStore(delegating.state).control).set_budget(PROJECT, 1_000_000)
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
    assert [s["limits"]["tokens"] for s in started] == [150_000] * 4
    assert root.final_text == "Four checks ran."


async def test_a_check_gets_no_more_turns_than_its_parent_has_left(delegating: Harness) -> None:
    root = await delegating.run(
        "root",
        [Reply(calls=[CHECK]), wait(), Reply("Done.")],
        delegated=[Reply(REPORT)],
        max_turns=3,
    )
    started = json.loads(results(root)[0])
    assert started["limits"]["turns"] == 2


async def test_a_child_stops_before_a_call_that_would_pass_its_limit(tmp_path: Path) -> None:
    projects, _, chat = _chat(tmp_path)
    try:
        child = ModelBudget(chat, tokens=60_000)
        assert await child.halt(_sized(10_000, used=40_000)) is None
        assert await child.halt(_sized(20_000, used=40_000)) == "run_budget"
    finally:
        projects.close()


async def test_a_root_stops_at_its_share_of_the_projects_budget(tmp_path: Path) -> None:
    projects, _, chat = _chat(tmp_path)
    try:
        root = ModelBudget(chat, 0.5)
        assert await root.halt(_sized(0, used=499_999)) is None
        assert await root.halt(_sized(0, used=500_000)) == "run_budget"
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
