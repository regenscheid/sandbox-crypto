# SPDX-License-Identifier: MIT
"""A delegated run's own limits (plans-and-delegation spec, PD-38; D102). Nothing is
reserved for a child from its root (the owner, 2026-10-15): each child gets its project's
check tokens and turns as soft caps, independent of its parent's remaining calls. No hook
keeps a budget: with no project limit, nothing stops or refuses a run for spending, and a
configured one is the model-call journal's alone (``test_cost_enforcement.py``)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from sandbox_science.orchestrator.delegation.hook import Child, ChildEnd
from sandbox_science.orchestrator.projects import ProjectStore
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
