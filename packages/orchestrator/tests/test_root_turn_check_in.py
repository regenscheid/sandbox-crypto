# SPDX-License-Identifier: MIT
"""The root's turn limit is a check-in the user answers (D102, owner decision 2026-10-24).

At its limit a root records a check-in, pauses without further model calls and waits for
the user: continue for another block of the same size, or stop. An explicit Resume is the
same acceptance; a Stop is the user's Stop. A run without durable records keeps its hard
limit and its report round (``test_long_runs.py``).
"""

from __future__ import annotations

import json

import pytest

from sandbox_science.orchestrator.continuity.auto_recovery import AutoRecovery
from sandbox_science.orchestrator.continuity.models import ModelIntents
from sandbox_science.orchestrator.continuity.records import Ownership, WorkRecords
from sandbox_science.orchestrator.continuity.resources import (
    Action,
    ResourceApprovalRequest,
    ResourceRequest,
    ResourceRequests,
)
from sandbox_science.orchestrator.continuity.status import RecoveryStatus
from sandbox_science.orchestrator.control import ControlError
from sandbox_science.orchestrator.loop import Transcript, run_task
from sandbox_science.orchestrator.model import ModelTurn
from sandbox_science.orchestrator.projects import ProjectStore
from sandbox_science.orchestrator.resource_hooks import Resources
from sandbox_science.orchestrator.runstate import RunState
from sandbox_science.orchestrator.tools import ToolOutcome

from .scripted import ScriptedModel, ToolCall
from .test_resource_requests import setup
from .test_tool_journal import RUN, toolbox
from .test_tool_journal import projects as projects
from .test_work_ownership import CHILD, CONTINUATION, record

BLOCK = 3


async def unused(request):
    raise AssertionError("no tool call expected")


async def working(request):
    return ToolOutcome("ok", {})


async def attempt(
    projects: ProjectStore,
    owner: Ownership,
    *,
    source=None,
    turns: int = BLOCK,
) -> tuple[ScriptedModel, Resources, Transcript]:
    """One root attempt of a scripted model that never stops working."""
    store = ResourceRequests(projects.control)
    hook = Resources(store, owner, turns=turns, tokens=None, delegated=False, resume_source=source)
    model = ScriptedModel(
        [
            ModelTurn(f"Progress {n}", (ToolCall(f"c{n}", "arithmetic", json.dumps({"n": n})),))
            for n in range(40)
        ]
    )
    result = await run_task(
        model, toolbox(working), task="Research", run_id="root", max_turns=turns, hooks=(hook,)
    )
    return model, hook, result


def journal(projects: ProjectStore, owner: Ownership, calls: int) -> None:
    """The attempt's model calls, as the model-call journal keeps them across attempts."""
    intents = ModelIntents(projects.control)
    for _ in range(calls):
        intents.begin(owner, kind="main", alias="fixture")


def paused(projects: ProjectStore) -> tuple[WorkRecords, ResourceRequests, ResourceRequest]:
    """The root's first attempt, stopped at its limit and not yet decided."""
    works = WorkRecords(projects.control)
    store = ResourceRequests(projects.control)
    (request,) = (r for r in store.page("p", "s").requests if r.decision is None)
    return works, store, request


def decision_for(request: ResourceRequest, *, action: Action = "extend", total: int = BLOCK):
    return ResourceApprovalRequest(
        request_id=request.request_id,
        action=action,
        reason="The next lemma is close" if action == "extend" else "Enough for now",
        current=request.current,
        epoch=request.epoch,
        total=total,
    )


async def reach_the_limit(projects: ProjectStore) -> ScriptedModel:
    works, owner, _, _ = setup(projects)
    model, _, result = await attempt(projects, owner)
    assert (result.stopped, result.turns) == ("turn_limit", BLOCK)
    journal(projects, owner, BLOCK)
    works.finish(record(CHILD))
    works.finish(record(RUN, stopped="turn_limit"))
    return model


async def test_the_root_pauses_at_its_turn_limit_and_asks_the_user(projects):
    model = await reach_the_limit(projects)
    assert len(model.requests) == BLOCK  # no model call after the limit, and no report round
    works, _, request = paused(projects)
    assert (request.resource, request.used, request.total, request.step) == (
        "turns",
        BLOCK,
        BLOCK,
        BLOCK,
    )
    assert request.parent is None
    assert request.work_state == "paused"
    assert request.summary is not None
    assert request.summary.rule == "turns"
    assert request.summary.answer == "Progress 2"  # its latest answer
    assert request.summary.turns == BLOCK
    assert works.get("p", "s", RUN).reason == "turn_limit"
    notices = [e for e in RunState(projects.control).events("p", "s", RUN) if e.kind == "notice"]
    assert "paused" in str(notices[-1].data["text"])
    assert "3 more turns" in str(notices[-1].data["text"])
    # Nothing continues it by itself.
    recovery = AutoRecovery(projects.control)
    assert recovery.candidates() == (("p", "s", RUN),)
    assert recovery.queue("p", "s", RUN) is None


async def test_the_card_survives_a_restart_while_paused(projects):
    await reach_the_limit(projects)
    projects.control.close()  # the process ends; the records are what remain
    _, store, request = paused(projects)
    assert request.decision is None
    assert request.work_state == "paused"
    assert not request.superseded
    assert [r.request_id for r in store.page("p", "s").requests] == [request.request_id]


async def test_continue_runs_another_block_and_records_the_acceptance(projects):
    await reach_the_limit(projects)
    works, store, request = paused(projects)
    accepted = store.approve("p", "s", decision_for(request), operator="user")
    assert (accepted.operator, accepted.request.action, accepted.total) == ("user", "extend", 6)
    assert store.approve("p", "s", decision_for(request), operator="user") == accepted
    # The accepted block is what automatic recovery needs to run it again.
    queued = AutoRecovery(projects.control).queue("p", "s", RUN)
    assert queued is not None
    RunState(projects.control).acquire("p", "s", CONTINUATION, "fixture")
    _, epoch = works.claim("p", "s", queued, lease_run=CONTINUATION)
    second = works.admit("p", "s", CONTINUATION, work=RUN, predecessor=RUN, epoch=epoch)
    model, _, result = await attempt(projects, second, source="automatic")
    assert (result.stopped, result.turns) == ("turn_limit", BLOCK)
    assert len(model.requests) == BLOCK  # a block of three more, and no more
    requests = {r.total: r for r in store.page("p", "s").requests}
    assert set(requests) == {3, 6}
    assert requests[3].decision == accepted
    assert requests[6].decision is None  # asked again at the end of the block
    assert requests[6].step == BLOCK


async def test_stop_is_the_users_stop_and_resume_shows_it(projects):
    await reach_the_limit(projects)
    works, store, request = paused(projects)
    stopped = store.approve("p", "s", decision_for(request, action="stop"), operator="user")
    assert (stopped.operator, stopped.request.action) == ("user", "stop")
    assert works.get("p", "s", RUN).state == "stopped"
    assert works.get("p", "s", RUN).reason == "user_stop"
    assert RunState(projects.control).cancelled("p", "s", RUN)
    assert works.request("p", "s") is None  # Resume finds nothing unfinished to recover
    with pytest.raises(ControlError, match="explicitly stopped"):
        works.request("p", "s", RUN)
    assert [(w.work, w.state) for w in RecoveryStatus(projects.control).page("p", "s").works] == [
        (RUN, "stopped"),
        (CHILD, "completed"),
    ]
    recovery = AutoRecovery(projects.control)
    assert recovery.candidates() == ()
    assert recovery.queue("p", "s", RUN) is None
    assert store.approve("p", "s", decision_for(request, action="stop"), operator="user") == stopped


async def test_resume_is_the_same_decision_as_the_card(projects):
    await reach_the_limit(projects)
    works, store, _ = paused(projects)
    resumed = works.request("p", "s")
    assert resumed is not None
    assert works.resume_source("p", "s", resumed) == "operator"
    RunState(projects.control).acquire("p", "s", CONTINUATION, "fixture")
    _, epoch = works.claim("p", "s", resumed, lease_run=CONTINUATION)
    second = works.admit("p", "s", CONTINUATION, work=RUN, predecessor=RUN, epoch=epoch)
    model, _, result = await attempt(projects, second, source="operator")
    assert (result.stopped, result.turns) == ("turn_limit", BLOCK)
    assert len(model.requests) == BLOCK
    (first,) = (r for r in store.page("p", "s").requests if r.total == BLOCK and r.decision)
    assert first.decision is not None
    assert (first.decision.operator, first.decision.request.action, first.decision.total) == (
        "resume",
        "extend",
        6,
    )
    assert first.decision.automatic is False


@pytest.mark.parametrize("limit", [2, 7])
async def test_max_turns_means_ask_at_n(projects, limit):
    _, owner, _, _ = setup(projects)
    model, hook, result = await attempt(projects, owner, turns=limit)
    assert (result.stopped, result.turns, len(model.requests)) == ("turn_limit", limit, limit)
    (request,) = ResourceRequests(projects.control).page("p", "s").requests
    assert (request.total, request.step, request.decision) == (limit, limit, None)
    # The limit stays pinned for the work: a later setting does not move it.
    pinned = ResourceRequests(projects.control).safe_point(
        owner, seconds=0, turns=limit * 10, tokens=None
    )
    assert pinned.turns == (limit, 0)
    assert hook.paused is not None


async def test_an_answer_at_the_limit_is_not_a_check_in(projects):
    _, owner, _, _ = setup(projects)
    model = ScriptedModel([ModelTurn("Done.")])
    store = ResourceRequests(projects.control)
    hook = Resources(store, owner, turns=BLOCK, tokens=None, delegated=False)
    result = await run_task(
        model, toolbox(unused), task="Research", run_id="root", max_turns=BLOCK, hooks=(hook,)
    )
    assert (result.stopped, result.turns) == ("answered", 1)
    assert store.page("p", "s").requests == ()


async def test_a_user_stop_answers_only_the_roots_check_in(projects):
    works, _, child, store = setup(projects)
    asked = store.observe(child, "turns", used=5, total=5, revision=0, force=True)
    assert asked is not None
    works.finish(record(CHILD, stopped="turn_limit"))
    works.finish(record(RUN))
    with pytest.raises(ControlError, match="root's check-in"):
        store.approve("p", "s", decision_for(asked, action="stop", total=5), operator="user")
    assert works.get("p", "s", CHILD).state == "paused"
