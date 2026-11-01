# SPDX-License-Identifier: MIT
"""Research check-ins (D102): soft caps and loop heuristics ask the parent, never stop."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest

from sandbox_science.contracts import new_id
from sandbox_science.orchestrator.context.agent import AgentContext
from sandbox_science.orchestrator.context.history import Archive, Entry
from sandbox_science.orchestrator.context.history.usage import AnsweredUsage
from sandbox_science.orchestrator.continuity.auto_recovery import AutoRecovery
from sandbox_science.orchestrator.continuity.blueprints import Blueprint, Blueprints
from sandbox_science.orchestrator.continuity.models import ModelIntents
from sandbox_science.orchestrator.continuity.records import WorkRecords
from sandbox_science.orchestrator.continuity.resource_policy import soft_grant
from sandbox_science.orchestrator.continuity.resources import (
    Action,
    ResourceDecisionRequest,
    ResourceRequests,
    turn_usage,
)
from sandbox_science.orchestrator.control import ControlError
from sandbox_science.orchestrator.delegation.records import Delegation, DelegationRecords
from sandbox_science.orchestrator.hooks import Closing, Notice, TurnHook
from sandbox_science.orchestrator.loop import Transcript, run_task
from sandbox_science.orchestrator.model import ModelTurn, ScriptedModel
from sandbox_science.orchestrator.projects import ModelUsage
from sandbox_science.orchestrator.resource_hooks import Resources

from .test_tool_journal import CONTEXT, RUN, toolbox
from .test_tool_journal import projects as projects
from .test_work_ownership import CHILD, record


def setup(projects, *, turns=5, tokens=20_000_000):
    works = WorkRecords(projects.control)
    parent = works.admit("p", "s", RUN)
    child = works.admit("p", "s", CHILD, parent=RUN)
    Blueprints(projects.control).save(
        child,
        Blueprint(
            context=CONTEXT,
            request={
                "project": "p",
                "session": "s",
                "task": "Derive and verify a lemma",
                "tokens": tokens,
                "max_turns": turns,
            },
        ),
    )
    DelegationRecords(projects.control).start(
        "p",
        "s",
        Delegation(
            delegation_id=new_id("dlg"),
            parent=RUN,
            run=CHILD,
            kind="task",
            brief={},
            started_at=datetime.now(UTC),
        ),
    )
    return works, parent, child, ResourceRequests(projects.control)


def granted(projects, owner, resource, original):
    with projects.control.read() as tx:
        return soft_grant(tx.sql, owner, resource, original)


def decide(store, parent, request, action: Action = "extend_turns", operation="op-resource"):
    return store.decide(
        parent,
        ResourceDecisionRequest(
            request_id=request.request_id,
            action=action,
            reason="The saved counterexample narrows the next experiment",
        ),
        operation=operation,
    )


def test_warning_checkpoint_deduplication_decision_and_ownership(projects):
    works, parent, child, requests = setup(projects)
    checkpoint = Archive(projects.control).append(
        "p", "s", CHILD, Entry("assistant", "assistant", "Verified a counterexample")
    )
    assert requests.observe(child, "turns", used=3, total=5, revision=0) is None
    first = requests.observe(child, "turns", used=4, total=5, revision=0)
    assert first is not None
    assert first.checkpoint_message_id == checkpoint.message_id
    assert requests.observe(child, "turns", used=5, total=5, revision=0) == first
    assert requests.page("p", "s").requests == (first,)
    assert requests.pending(parent) == [first]
    with pytest.raises(ControlError, match="parent, attempt or epoch"):
        decide(requests, child, first)
    decision = decide(requests, parent, first)
    assert decision.total == 305
    assert decide(requests, parent, first) == decision
    with pytest.raises(ControlError, match="another decision"):
        decide(requests, parent, first, action="pause")
    assert requests.pending(parent) == []
    assert granted(projects, child, "turns", 5) == (305, 305)
    works.stop("p", "s", CHILD)
    assert requests.observe(child, "tokens", used=19_000_000, total=20_000_000, revision=0) is None


@pytest.mark.parametrize("action", ["pause", "report", "redirect", "continue"])
async def test_parent_actions_are_durable_and_token_grants_do_not_change(projects, action):
    _, parent, child, requests = setup(projects)
    warning = requests.observe(child, "tokens", used=16_000_000, total=20_000_000, revision=0)
    assert warning is not None
    with pytest.raises(ControlError, match="not renewable"):
        decide(requests, parent, warning)
    decide(requests, parent, warning, action=action)
    projects.control.close()
    hook = Resources(requests, child, turns=5, tokens=20_000_000, delegated=True)
    assert (await hook.halt(Transcript(context=AgentContext(messages=[])))) == (
        "resource_pause" if action == "pause" else None
    )
    assert (await hook.last_call(Transcript(context=AgentContext(messages=[])))) == (
        "resource_report" if action == "report" else None
    )
    redirected = 40_000_000 if action == "redirect" else 20_000_000
    assert granted(projects, child, "tokens", 20_000_000)[0] == redirected


async def test_waiting_parent_wakes_without_extra_model_calls(projects):
    _, parent, child, requests = setup(projects)
    hook = Resources(requests, parent, turns=300, tokens=None, delegated=False)
    never = asyncio.Event()

    async def ordinary():
        await never.wait()
        return "ordinary"

    task = asyncio.create_task(hook.wake(ordinary))
    warning = requests.observe(child, "turns", used=4, total=5, revision=0)
    assert warning is not None
    assert await asyncio.wait_for(task, 2) == "resource_request"


def test_exhaustion_can_be_decided_after_keep_current_allowance(projects):
    _, parent, child, requests = setup(projects)
    warning = requests.observe(child, "turns", used=4, total=5, revision=0)
    assert warning is not None
    decide(requests, parent, warning, action="continue")
    exhausted = requests.observe(child, "turns", used=5, total=5, revision=0, force=True)
    assert exhausted is not None
    assert exhausted.request_id != warning.request_id
    assert requests.pending(parent) == [exhausted]
    assert decide(requests, parent, exhausted, operation="op-exhaustion").total == 305
    assert requests.pending(parent) == []
    assert all(r.superseded for r in requests.page("p", "s").requests)


def test_extension_supersedes_other_requests_for_the_old_allowance(projects):
    _, parent, child, requests = setup(projects)
    warning = requests.observe(child, "turns", used=4, total=5, revision=0)
    exhausted = requests.observe(child, "turns", used=5, total=5, revision=0, force=True)
    assert warning is not None
    assert exhausted is not None
    decide(requests, parent, warning)
    assert requests.pending(parent) == []
    assert all(r.superseded for r in requests.page("p", "s").requests)


async def test_parent_turn_extension_reaches_running_loop(projects):
    """At its soft turn cap the child checks in and waits, making no model call, until its
    parent extends it by one bounded step; then it goes on and answers."""
    _, parent, child, requests = setup(projects)
    seen: list[int] = []

    class Continue(TurnHook):
        async def closing(self, run, answer):
            return (
                Closing("continue", (Notice("fixture", (), "another step", {}),))
                if run.turns < 7
                else Closing()
            )

    async def unused(request):
        raise AssertionError("no tool call expected")

    class Counting(ScriptedModel):
        async def complete(self, messages, tools):
            seen.append(len(requests.pending(parent)))
            return await super().complete(messages, tools)

    async def as_parent():
        while not (pending := requests.pending(parent)):
            await asyncio.sleep(0.01)
        decide(requests, parent, pending[0], action="extend")

    hook = Resources(requests, child, turns=5, tokens=20_000_000, delegated=True, poll_seconds=0.01)
    parent_task = asyncio.create_task(as_parent())
    result = await run_task(
        Counting([ModelTurn(f"Progress {n}") for n in range(7)]),
        toolbox(unused),
        task="Fixture",
        run_id="child",
        max_turns=5,
        hooks=(hook, Continue()),
    )
    await asyncio.wait_for(parent_task, 2)
    assert result.stopped == "answered"
    assert result.turns == 7
    assert seen == [0] * 7  # no model call while the check-in waited
    (request,) = requests.page("p", "s").requests
    assert request.resource == "turns"
    assert request.summary is not None
    assert request.summary.brief == "Derive and verify a lemma"
    assert request.summary.turns == 5
    assert request.decision is not None
    assert request.decision.total == 305


def test_turn_accounting_counts_unknown_once_and_excludes_fold(projects):
    _, _, child, _ = setup(projects)
    intents = ModelIntents(projects.control)
    identity = intents.begin(child, kind="delegated", alias="fixture")
    Archive(projects.control).append(
        "p",
        "s",
        CHILD,
        Entry("assistant", "assistant", "Observed"),
        model_usage=AnsweredUsage(
            ordinal=1,
            observed_at=datetime.now(UTC),
            usage=ModelUsage(input_tokens=4, output_tokens=1),
        ),
    )
    intents.begin(child, kind="delegated", alias="fixture")
    intents.begin(child, kind="fold", alias="fixture")
    with projects.control.read() as tx:
        assert turn_usage(tx.sql, child) == 2
    assert identity


def test_paused_turn_grant_can_queue_same_tree_once(projects):
    works, parent, child, requests = setup(projects)
    warning = requests.observe(child, "turns", used=5, total=5, revision=0)
    assert warning is not None
    works.finish(record(CHILD, stopped="turn_limit"))
    assert AutoRecovery(projects.control).queue("p", "s", RUN) is None
    decide(requests, parent, warning)
    works.finish(record(RUN))
    assert works.get("p", "s", RUN).reason == "unfinished_children"
    queued = AutoRecovery(projects.control).queue("p", "s", RUN)
    assert queued is not None
    assert AutoRecovery(projects.control).queue("p", "s", RUN) is None


async def test_own_warning_does_not_spend_an_extra_closing_call(projects):
    _, parent, child, requests = setup(projects)

    class Continue(TurnHook):
        async def closing(self, run, answer):
            return (
                Closing("continue", (Notice("fixture", (), "another step", {}),))
                if run.turns < 4
                else Closing()
            )

    async def unused(request):
        raise AssertionError("no tool call expected")

    result = await run_task(
        ScriptedModel([ModelTurn(f"Progress {n}") for n in range(4)]),
        toolbox(unused),
        task="Fixture",
        run_id="child",
        max_turns=5,
        hooks=(Resources(requests, child, turns=5, tokens=20_000_000, delegated=True), Continue()),
    )
    assert result.stopped == "answered"
    assert result.turns == 4
    assert requests.pending(parent) == []  # below its soft caps: no check-in at all
