# SPDX-License-Identifier: MIT
"""Soft caps are check-ins (D102): a child asks its parent, the root checks itself.

A child at a soft cap, or one a loop heuristic caught, waits without model calls for its
parent to extend it by one bounded step, redirect it or stop it. After repeated
extensions without new evidence the parent must stop or redirect. The root decides its
own check-ins while it produces evidence, with a notice to the user, and pauses for the
user only after repeated extensions without any.
"""

from __future__ import annotations

import asyncio

import pytest

from sandbox_science.orchestrator.context.agent import AgentContext
from sandbox_science.orchestrator.continuity.resources import (
    Resource,
    ResourceApprovalRequest,
    ResourceDecisionRequest,
)
from sandbox_science.orchestrator.control import ControlError
from sandbox_science.orchestrator.delegation.records import DelegationRecords
from sandbox_science.orchestrator.hooks import Closing, Notice, TurnHook
from sandbox_science.orchestrator.loop import Transcript, run_task
from sandbox_science.orchestrator.model import ModelTurn, ScriptedModel
from sandbox_science.orchestrator.resource_hooks import (
    IDLE_TURNS,
    SAME_CALL,
    SAME_ERROR,
    SAME_OUTPUT,
    Resources,
)
from sandbox_science.orchestrator.runstate import RunState

from .test_cost_enforcement import spend
from .test_resource_policy import artifact
from .test_resource_requests import decide, granted, setup
from .test_tool_journal import RUN, toolbox
from .test_tool_journal import projects as projects
from .test_work_ownership import CHILD, record


def empty() -> Transcript:
    return Transcript(context=AgentContext(messages=[]))


class Continue(TurnHook):
    """Hold each answer back for another turn until ``turns`` model calls were made."""

    def __init__(self, turns: int) -> None:
        self.turns = turns

    async def closing(self, run, answer):
        if run.turns < self.turns:
            return Closing("continue", (Notice("fixture", (), "another step", {}),))
        return Closing()


async def unused(request):
    raise AssertionError("no tool call expected")


async def as_parent(requests, parent, action, reason="Use the saved counterexample"):
    while not (pending := requests.pending(parent)):
        await asyncio.sleep(0.01)
    return requests.decide(
        parent,
        ResourceDecisionRequest(request_id=pending[0].request_id, action=action, reason=reason),
        operation="op-" + action,
    )


async def test_parent_redirect_reaches_the_waiting_child_as_its_instruction(projects):
    _, parent, child, requests = setup(projects)
    seen: list[str] = []

    class Reading(ScriptedModel):
        async def complete(self, messages, tools):
            seen.extend(str(m.get("content")) for m in messages if m.get("role") == "user")
            return await super().complete(messages, tools)

    hook = Resources(requests, child, turns=5, tokens=20_000_000, delegated=True, poll_seconds=0.01)
    deciding = asyncio.create_task(
        as_parent(requests, parent, "redirect", "Switch to the smaller parameter set")
    )
    result = await run_task(
        Reading([ModelTurn(f"Progress {n}") for n in range(7)]),
        toolbox(unused),
        task="Fixture",
        run_id="child",
        max_turns=5,
        hooks=(hook, Continue(7)),
    )
    decision = await asyncio.wait_for(deciding, 2)
    assert result.stopped == "answered"
    assert decision.total == 305  # a redirect carries one bounded step
    assert any("Switch to the smaller parameter set" in text for text in seen)


async def test_parent_stop_ends_the_child_with_its_report(projects):
    """A stop makes the child's next call its report round; an answer it already gave
    and held for another turn stands as that report instead (RS-6f)."""
    _, parent, child, requests = setup(projects)
    hook = Resources(requests, child, turns=5, tokens=20_000_000, delegated=True, poll_seconds=0.01)
    deciding = asyncio.create_task(as_parent(requests, parent, "stop"))
    result = await run_task(
        ScriptedModel([ModelTurn(f"Progress {n}") for n in range(5)] + [ModelTurn("Never.")]),
        toolbox(unused),
        task="Fixture",
        run_id="child",
        max_turns=5,
        hooks=(hook, Continue(9)),
    )
    decision = await asyncio.wait_for(deciding, 2)
    assert decision.request.action == "stop"
    assert result.stopped == "resource_report"
    assert result.final_text == "Progress 4"
    assert result.turns == 5
    assert await hook.last_call(empty()) == "resource_report"


async def test_a_parent_that_does_not_answer_gets_the_roots_rule(projects):
    _, parent, child, requests = setup(projects)
    hook = Resources(
        requests,
        child,
        turns=5,
        tokens=20_000_000,
        delegated=True,
        poll_seconds=0.01,
        wait_seconds=0.05,
    )
    result = await run_task(
        ScriptedModel([ModelTurn(f"Progress {n}") for n in range(6)]),
        toolbox(unused),
        task="Fixture",
        run_id="child",
        max_turns=5,
        hooks=(hook, Continue(6)),
    )
    assert result.stopped == "answered"
    (request,) = requests.page("p", "s").requests
    assert request.decision is not None
    assert request.decision.automatic
    assert (request.decision.request.action, request.decision.total) == ("extend", 305)
    assert parent.run != request.decision.parent_run  # the child's own attempt decided


def test_extensions_without_progress_require_stop_or_redirect(projects):
    _, parent, child, requests = setup(projects)
    DelegationRecords(projects.control).configure("p", max_idle_extensions=2)

    def reached(total):
        found = requests.observe(
            child, "turns", used=total, total=total, revision=total if total > 5 else 0, force=True
        )
        assert found is not None
        return found

    assert decide(requests, parent, reached(5), "extend", "one").total == 305
    assert decide(requests, parent, reached(305), "extend", "two").total == 605
    third = reached(605)
    with pytest.raises(ControlError, match="2 extensions in a row without new evidence"):
        decide(requests, parent, third, "extend", "three")
    with pytest.raises(ControlError, match="soft cap needs extend, redirect or stop"):
        decide(requests, parent, third, "continue", "three-continue")
    assert decide(requests, parent, third, "redirect", "redirect").total == 905
    assert decide(requests, parent, reached(905), "extend", "after-redirect").total == 1205
    assert decide(requests, parent, reached(1205), "extend", "five").total == 1505
    sixth = reached(1505)
    with pytest.raises(ControlError, match="without new evidence"):
        decide(requests, parent, sixth, "extend", "six")
    artifact(projects, CHILD, b"a new lemma", "lemma")  # new evidence resets the bound
    assert decide(requests, parent, sixth, "extend", "six-after-evidence").total == 1805
    assert granted(projects, child, "turns", 5) == (1805, 1805)


def test_each_soft_cap_resource_extends_by_its_step(projects):
    _, parent, child, requests = setup(projects)
    cases: tuple[tuple[Resource, int, int], ...] = (
        ("tokens", 20_000_000, 40_000_000),
        ("elapsed_seconds", 14_400, 18_000),
        ("cost", 2_500, 5_000),
    )
    for resource, total, after in cases:
        found = requests.observe(child, resource, used=total, total=total, revision=0, force=True)
        assert found is not None
        assert decide(requests, parent, found, "extend", resource).total == after


async def test_root_checks_itself_and_notifies_the_user_then_pauses_without_progress(projects):
    works, parent, _, requests = setup(projects)
    DelegationRecords(projects.control).configure("p", max_idle_extensions=2, soft_cost=1.0)
    hook = Resources(requests, parent, turns=300, tokens=None, delegated=False)
    run = empty()
    spend(projects, RUN, 1.0)
    assert await hook.halt(run) is None  # at its soft cost cap: one step, no stop
    spend(projects, RUN, 25.0, 2)
    assert await hook.halt(run) is None
    events = [e for e in RunState(projects.control).events("p", "s", RUN) if e.kind == "notice"]
    assert len(events) == 2
    assert all(e.data["source"] == "resources" for e in events)
    assert "nothing was stopped" in str(events[0].data["text"])
    notices = await hook.notices("turn", run)
    assert any("Compare your progress with the session plan" in n.framing for n in notices)
    spend(projects, RUN, 25.0, 3)
    assert await hook.halt(run) == "resource_pause"  # the user is asked now
    events = [e for e in RunState(projects.control).events("p", "s", RUN) if e.kind == "notice"]
    assert "paused" in str(events[-1].data["text"])
    waiting = [r for r in requests.page("p", "s").requests if r.decision is None]
    assert len(waiting) == 1
    works.finish(record(CHILD))
    works.finish(record(RUN, stopped="resource_pause"))
    approved = requests.approve(
        "p",
        "s",
        ResourceApprovalRequest(
            request_id=waiting[0].request_id,
            action="extend",
            reason="Continue: the next lemma is close",
            current=RUN,
            epoch=1,
            total=5100,
        ),
        operator="user",
    )
    assert (approved.operator, approved.total) == ("user", 7600)


async def test_root_progress_keeps_its_check_ins_automatic(projects):
    _, parent, _, requests = setup(projects)
    DelegationRecords(projects.control).configure("p", max_idle_extensions=1, soft_cost=1.0)
    hook = Resources(requests, parent, turns=300, tokens=None, delegated=False)
    run = empty()
    for ordinal, operation in ((1, "a"), (2, "b"), (3, "c")):
        artifact(projects, RUN, operation.encode(), operation)
        spend(projects, RUN, 1.0 if ordinal == 1 else 25.0, ordinal)
        assert await hook.halt(run) is None
    decisions = [r.decision for r in requests.page("p", "s").requests]
    assert all(d is not None and d.automatic for d in decisions)


async def loop_check_in(projects, feed):
    """Feed a root hook; the check-in a loop heuristic asked for."""
    _, parent, _, requests = setup(projects)
    hook = Resources(requests, parent, turns=300, tokens=None, delegated=False)
    run = empty()
    await feed(hook, run)
    assert await hook.halt(run) is None
    (request,) = requests.page("p", "s").requests
    assert request.resource == "loop"
    assert request.summary is not None
    assert request.decision is not None
    assert request.decision.automatic
    assert hook.tripped is None  # the counters start again after the check-in
    return request.summary


async def test_repeated_identical_tool_calls_ask_for_a_check_in(projects):
    async def feed(hook, run):
        for _ in range(SAME_CALL):
            run.tool_records.append({"tool": "shell", "arguments": '{"cmd": "make"}'})
            await hook.observe_tool(run, {})

    summary = await loop_check_in(projects, feed)
    assert summary.rule == "same_call"
    assert summary.recent[-1] == "shell: ok"


async def test_the_same_error_repeated_asks_for_a_check_in(projects):
    async def feed(hook, run):
        for n in range(SAME_ERROR):
            run.tool_records.append({"tool": "read_file", "arguments": f'{{"n": {n}}}'})
            said = '{"error": {"kind": "not_found", "message": "no such file"}}'
            run.context.messages.append({"role": "tool", "content": said})
            await hook.observe_tool(run, {"error": "not_found"})

    summary = await loop_check_in(projects, feed)
    assert summary.rule == "same_error"


async def test_turns_without_new_evidence_ask_for_a_check_in(projects):
    async def feed(hook, run):
        for _ in range(IDLE_TURNS):
            assert await hook.halt(run) is None

    summary = await loop_check_in(projects, feed)
    assert summary.rule == "no_progress"


async def test_unchanging_output_asks_for_a_check_in(projects):
    async def feed(hook, run):
        run.context.messages.append({"role": "assistant", "content": "Still checking."})
        for _ in range(SAME_OUTPUT):
            await hook.model_returned(run)

    summary = await loop_check_in(projects, feed)
    assert summary.rule == "same_output"
    assert summary.answer == "Still checking."


async def test_a_childs_loop_check_in_reaches_its_parent(projects):
    _, parent, child, requests = setup(projects)
    hook = Resources(
        requests, child, turns=300, tokens=20_000_000, delegated=True, poll_seconds=0.01
    )
    run = empty()
    for _ in range(SAME_CALL):
        run.tool_records.append({"tool": "shell", "arguments": '{"cmd": "make"}'})
        await hook.observe_tool(run, {})
    deciding = asyncio.create_task(as_parent(requests, parent, "extend"))
    assert await asyncio.wait_for(hook.halt(run), 2) is None
    decision = await deciding
    assert decision.resource == "loop"
    assert decision.total == SAME_CALL  # a loop check-in changes no cap


async def test_errors_of_one_kind_with_different_messages_do_not_trip(projects):
    _, parent, _, requests = setup(projects)
    hook = Resources(requests, parent, turns=300, tokens=None, delegated=False)
    run = empty()
    for n in range(SAME_ERROR * 2):
        run.tool_records.append({"tool": f"tool_{n}", "arguments": "{}"})
        said = f'{{"error": {{"kind": "policy_denied", "message": "not_permitted: tool_{n}"}}}}'
        run.context.messages.append({"role": "tool", "content": said})
        await hook.observe_tool(run, {"error": "policy_denied"})
    assert hook.tripped is None


async def test_polling_and_varied_calls_do_not_trip_a_heuristic(projects):
    _, parent, _, requests = setup(projects)
    hook = Resources(requests, parent, turns=300, tokens=None, delegated=False)
    run = empty()
    for n in range(SAME_CALL * 2):
        run.tool_records.append({"tool": "wait_jobs", "arguments": "{}"})
        await hook.observe_tool(run, {})
        run.tool_records.append({"tool": "shell", "arguments": f'{{"n": {n}}}'})
        await hook.observe_tool(run, {})
    assert hook.tripped is None
    assert await hook.halt(run) is None
    assert requests.page("p", "s").requests == ()
