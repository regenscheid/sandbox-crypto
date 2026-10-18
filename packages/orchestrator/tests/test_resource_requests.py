# SPDX-License-Identifier: MIT
"""Research check-ins renew turns, preserve tokens and survive root-level Resume."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest

from sandbox_science.contracts import new_id
from sandbox_science.orchestrator.continuity.auto_recovery import AutoRecovery
from sandbox_science.orchestrator.continuity.blueprints import Blueprint, Blueprints
from sandbox_science.orchestrator.continuity.grants import TokenGrantRequest, TokenGrants
from sandbox_science.orchestrator.continuity.models import ModelIntents
from sandbox_science.orchestrator.continuity.records import WorkRecords
from sandbox_science.orchestrator.continuity.resources import (
    Action,
    ResourceDecisionRequest,
    ResourceRequests,
    turn_usage,
)
from sandbox_science.orchestrator.control import ControlError
from sandbox_science.orchestrator.delegation.records import Delegation, DelegationRecords
from sandbox_science.orchestrator.history import Archive, Entry
from sandbox_science.orchestrator.history.usage import AnsweredUsage
from sandbox_science.orchestrator.hooks import Closing, Notice, TurnHook
from sandbox_science.orchestrator.loop import Transcript, run_task
from sandbox_science.orchestrator.model import ModelTurn, ScriptedModel
from sandbox_science.orchestrator.projects import ModelUsage
from sandbox_science.orchestrator.resource_hooks import Resources
from sandbox_science.orchestrator.runstate import RunState

from .test_tool_journal import CONTEXT, RUN, toolbox
from .test_tool_journal import projects as projects
from .test_work_ownership import CHILD, GRANDCHILD, record


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
    assert requests.limits(child, turns=5, tokens=20_000_000)[:2] == (305, 305)
    works.stop("p", "s", CHILD)
    assert requests.observe(child, "tokens", used=19_000_000, total=20_000_000, revision=0) is None


@pytest.mark.parametrize("action", ["pause", "report", "redirect", "continue"])
async def test_parent_actions_are_durable_and_token_grants_do_not_change(projects, action):
    _, parent, child, requests = setup(projects)
    warning = requests.observe(child, "tokens", used=16_000_000, total=20_000_000, revision=0)
    assert warning is not None
    with pytest.raises(ControlError, match="only turn allowances"):
        decide(requests, parent, warning)
    decide(requests, parent, warning, action=action)
    projects.control.close()
    hook = Resources(requests, child, turns=5, tokens=20_000_000, delegated=True)
    assert (await hook.halt(Transcript(messages=[]))) == (
        "resource_pause" if action == "pause" else None
    )
    assert (await hook.last_call(Transcript(messages=[]))) == (
        "resource_report" if action == "report" else None
    )
    assert TokenGrants(projects.control).total("p", "s", CHILD, 20_000_000) == 20_000_000


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
    _, parent, child, requests = setup(projects)

    class ParentAware(ScriptedModel):
        async def complete(self, messages, tools):
            pending = requests.pending(parent)
            if pending:
                decide(requests, parent, pending[0])
            return await super().complete(messages, tools)

    class Continue(TurnHook):
        async def closing(self, run, answer):
            return (
                Closing("continue", (Notice("fixture", (), "another step", {}),))
                if run.turns < 7
                else Closing()
            )

    async def unused(request):
        raise AssertionError("no tool call expected")

    model = ParentAware([ModelTurn("Progress")] * 7)
    hook = Resources(requests, child, turns=5, tokens=20_000_000, delegated=True)
    result = await run_task(
        model,
        toolbox(unused),
        task="Fixture",
        run_id="child",
        max_turns=5,
        hooks=(hook, Continue()),
    )
    assert result.stopped == "answered"
    assert result.turns == 7
    decision = requests.page("p", "s").requests[0].decision
    assert decision is not None
    assert decision.total == 305


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


def test_exact_user_increment_and_resolution_projection(projects):
    works, _, child, requests = setup(projects)
    warning = requests.observe(child, "tokens", used=19_000_000, total=20_000_000, revision=0)
    assert warning is not None
    grants = TokenGrants(projects.control)
    assert grants.status("p", "s", CHILD).next_total_tokens is None
    works.interrupt("p", "s", RUN)
    RunState(projects.control).set_cost_limit("p", 100)
    status = grants.status("p", "s", CHILD)
    assert status.next_total_tokens == 30_000_000
    request = TokenGrantRequest(
        work=CHILD,
        current=CHILD,
        epoch=1,
        expected_revision=0,
        total_tokens=30_000_000,
        reason="USER approved the next experiment",
    )
    with pytest.raises(ControlError, match="exactly 10,000,000"):
        grants.renew_next(
            "p", "s", request.model_copy(update={"total_tokens": 25_000_000}), operator="user"
        )
    first = grants.renew_next("p", "s", request, operator="user")
    assert grants.renew_next("p", "s", request, operator="user") == first
    assert grants.status("p", "s", CHILD).total_tokens == 30_000_000
    assert requests.page("p", "s").requests[0].user_grant_revision == 1


def test_nested_user_increment_never_overallocates_parent(projects):
    works, _, _child, _ = setup(projects)
    leaf = works.admit("p", "s", GRANDCHILD, parent=CHILD)
    Blueprints(projects.control).save(
        leaf,
        Blueprint(
            context=CONTEXT,
            request={
                "project": "p",
                "session": "s",
                "task": "Check lemma",
                "tokens": 20_000_000,
                "max_turns": 5,
            },
        ),
    )
    works.interrupt("p", "s", RUN)
    RunState(projects.control).set_cost_limit("p", 100)
    status = TokenGrants(projects.control).status("p", "s", GRANDCHILD)
    assert status.next_total_tokens is None
    assert status.blocked_reason is not None
    assert "Ancestor" in status.blocked_reason
    assert status.blocked_ancestor_work == CHILD


def test_completed_sibling_descendants_still_consume_parent_tokens(projects):
    works, _, _, _ = setup(projects, tokens=40_000_000)
    sibling = "20261016T030003000000Z-sibling-1"
    descendant = "20261016T030004000000Z-descendant-1"
    for work, parent, tokens in (
        (GRANDCHILD, CHILD, 20_000_000),
        (sibling, CHILD, 20_000_000),
        (descendant, sibling, 15_000_000),
    ):
        owner = works.admit("p", "s", work, parent=parent)
        Blueprints(projects.control).save(
            owner,
            Blueprint(
                context=CONTEXT,
                request={"project": "p", "session": "s", "task": "Lemma", "tokens": tokens},
            ),
        )
    for work, used in ((CHILD, 1_000_000), (sibling, 1_000_000), (descendant, 15_000_000)):
        Archive(projects.control).append(
            "p",
            "s",
            work,
            Entry("assistant", "assistant", "Observed work"),
            model_usage=AnsweredUsage(
                ordinal=1,
                observed_at=datetime.now(UTC),
                usage=ModelUsage(input_tokens=used, output_tokens=0),
            ),
        )
    works.finish(record(descendant))
    works.finish(record(sibling))
    works.interrupt("p", "s", RUN)
    RunState(projects.control).set_cost_limit("p", 100)
    status = TokenGrants(projects.control).status("p", "s", GRANDCHILD)
    assert status.next_total_tokens is None
    assert status.blocked_ancestor_work == CHILD


def test_late_receipts_do_not_count_the_same_parent_commitment_twice(projects):
    works, _, _, _ = setup(projects, tokens=30_000_000)
    owner = works.admit("p", "s", GRANDCHILD, parent=CHILD)
    Blueprints(projects.control).save(
        owner,
        Blueprint(
            context=CONTEXT,
            request={"project": "p", "session": "s", "task": "Lemma", "tokens": 20_000_000},
        ),
    )
    Archive(projects.control).append(
        "p",
        "s",
        GRANDCHILD,
        Entry("assistant", "assistant", "Late usage receipt"),
        model_usage=AnsweredUsage(
            ordinal=1,
            observed_at=datetime.now(UTC),
            usage=ModelUsage(input_tokens=25_000_000, output_tokens=0),
        ),
    )
    works.interrupt("p", "s", RUN)
    RunState(projects.control).set_cost_limit("p", 100)
    status = TokenGrants(projects.control).status("p", "s", GRANDCHILD)
    assert status.next_total_tokens == 30_000_000
    assert status.blocked_ancestor_work is None


def test_nested_increment_uses_existing_parent_allocation_only_once(projects):
    works, _, _, _ = setup(projects, tokens=40_000_000)
    great_grandchild = "20261016T030003000000Z-greatgrandchild-1"
    for work, parent, tokens in (
        (GRANDCHILD, CHILD, 40_000_000),
        (great_grandchild, GRANDCHILD, 20_000_000),
    ):
        owner = works.admit("p", "s", work, parent=parent)
        Blueprints(projects.control).save(
            owner,
            Blueprint(
                context=CONTEXT,
                request={"project": "p", "session": "s", "task": "Lemma", "tokens": tokens},
            ),
        )
        DelegationRecords(projects.control).start(
            "p",
            "s",
            Delegation(
                delegation_id=new_id("dlg"),
                parent=parent,
                run=work,
                kind="task",
                brief={},
                started_at=datetime.now(UTC),
            ),
        )
    works.interrupt("p", "s", RUN)
    RunState(projects.control).set_cost_limit("p", 100)
    grants = TokenGrants(projects.control)
    status = grants.status("p", "s", great_grandchild)
    assert status.next_total_tokens == 30_000_000
    assert status.blocked_reason is None
    decision = grants.renew_next(
        "p",
        "s",
        TokenGrantRequest(
            work=great_grandchild,
            current=great_grandchild,
            epoch=1,
            expected_revision=0,
            total_tokens=30_000_000,
            reason="USER approved one more increment within the parent's existing allocation",
        ),
        operator="user",
    )
    assert decision.request.total_tokens == 30_000_000


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
        ScriptedModel([ModelTurn("Progress")] * 4),
        toolbox(unused),
        task="Fixture",
        run_id="child",
        max_turns=5,
        hooks=(Resources(requests, child, turns=5, tokens=20_000_000, delegated=True), Continue()),
    )
    assert result.stopped == "answered"
    assert result.turns == 4
    assert len(requests.pending(parent)) == 1
