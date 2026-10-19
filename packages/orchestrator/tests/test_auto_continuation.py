# SPDX-License-Identifier: MIT
"""An answered checkpoint continues only while its current research plan is active."""

from __future__ import annotations

import pytest

from sandbox_science.orchestrator.continuity.auto_continuation import AutoContinuation
from sandbox_science.orchestrator.continuity.records import WorkRecords
from sandbox_science.orchestrator.documents import DocumentStore
from sandbox_science.orchestrator.projects import ProjectStore
from sandbox_science.orchestrator.runstate import RunState
from sandbox_science.orchestrator.session_plans import SessionPlans

from .loop_harness import Harness
from .loop_harness import gateway as gateway
from .loop_harness import harness as harness
from .loop_harness import worker as worker
from .test_run_service import serving
from .test_session_plans import actor, made, outcome, status, update
from .test_tool_journal import RUN
from .test_tool_journal import projects as projects
from .test_work_ownership import CHILD, CONTINUATION, record


def setup(
    projects: ProjectStore, *, active: bool = True, child: bool = False
) -> tuple[AutoContinuation, SessionPlans]:
    documents = DocumentStore(projects.control.path)
    plans = SessionPlans(documents)
    plan = made(plans)
    if active:
        status(plans, plan.ref, "active")
    works = WorkRecords(projects.control)
    works.admit("p", "s", RUN)
    if child:
        works.admit("p", "s", CHILD, parent=RUN)
    answer = record(RUN)
    projects.save_run(answer)
    works.finish(answer)
    return AutoContinuation(projects.control, documents, RunState(projects.control)), plans


def test_answered_root_queues_one_continuation_across_restart(projects: ProjectStore):
    supervisor, plans = setup(projects)
    assert supervisor.candidates() == (("p", "s", RUN),)
    message_id = supervisor.queue("p", "s", RUN)
    assert message_id is not None
    messages = RunState(projects.control).messages("p", "s")
    assert len(messages) == 1
    assert messages[0].message_id == message_id
    assert messages[0].author == "research-supervisor"
    restarted = AutoContinuation(projects.control, plans.documents, RunState(projects.control))
    assert restarted.queue("p", "s", RUN) is None
    runs = RunState(projects.control)
    runs.deliver("p", "s", [message_id], "synthetic-next-root")
    assert restarted.queue("p", "s", RUN) is None
    assert len(runs.messages("p", "s")) == 1


def test_same_plan_state_cannot_start_endless_answered_roots(projects: ProjectStore):
    supervisor, _ = setup(projects)
    first = supervisor.queue("p", "s", RUN)
    assert first is not None
    runs = RunState(projects.control)
    runs.deliver("p", "s", [first], CONTINUATION)
    works = WorkRecords(projects.control)
    works.admit("p", "s", CONTINUATION)
    answer = record(CONTINUATION)
    projects.save_run(answer)
    works.finish(answer)
    assert supervisor.candidates() == (("p", "s", CONTINUATION),)
    assert supervisor.queue("p", "s", CONTINUATION) is None
    assert len(runs.messages("p", "s")) == 1


def claim(supervisor: AutoContinuation, projects: ProjectStore) -> tuple[str, ...]:
    runs = RunState(projects.control)
    runs.acquire("p", "s", CONTINUATION, "fixture")
    try:
        return supervisor.claim_queued("p", "s", CONTINUATION)
    finally:
        runs.release("p", "s", "fixture")


def test_claim_without_queued_messages_returns_empty_selection(projects: ProjectStore):
    supervisor, _ = setup(projects)
    assert claim(supervisor, projects) == ()


def test_plan_progress_allows_one_more_continuation(projects: ProjectStore):
    supervisor, plans = setup(projects)
    first = supervisor.queue("p", "s", RUN)
    assert first is not None
    RunState(projects.control).deliver("p", "s", [first], "synthetic-next-root")
    current = plans.current(actor())
    assert current is not None
    update(plans, current.head.ref, status="in_progress")
    second = supervisor.queue("p", "s", RUN)
    assert second is not None
    assert second != first
    assert supervisor.queue("p", "s", RUN) is None


def test_draft_and_completed_plans_do_not_continue(projects: ProjectStore):
    supervisor, plans = setup(projects, active=False)
    assert supervisor.queue("p", "s", RUN) is None
    current = plans.current(actor())
    assert current is not None
    status(plans, current.head.ref, "active")
    update(plans, current.head.ref, status="completed")
    status(plans, current.head.ref, "completed", operation="finish", result=outcome())
    assert supervisor.queue("p", "s", RUN) is None


def test_stopped_root_does_not_continue(projects: ProjectStore):
    supervisor, _ = setup(projects)
    RunState(projects.control).cancel("p", "s", RUN)
    assert supervisor.queue("p", "s", RUN) is None


def test_user_input_and_unsettled_children_take_precedence(projects: ProjectStore):
    supervisor, _ = setup(projects)
    runs = RunState(projects.control)
    runs.post("p", "s", "user-correction", "Change direction", author="user")
    assert supervisor.queue("p", "s", RUN) is None


def test_user_correction_after_queue_withdraws_synthetic_input(projects: ProjectStore):
    supervisor, _ = setup(projects)
    message_id = supervisor.queue("p", "s", RUN)
    assert message_id is not None
    runs = RunState(projects.control)
    runs.post("p", "s", "user-correction", "Change direction", author="user")
    assert claim(supervisor, projects) == ("user-correction",)
    messages = runs.messages("p", "s")
    assert [(m.message_id, m.state) for m in messages] == [
        (message_id, "refused"),
        ("user-correction", "queued"),
    ]


def test_plan_change_and_late_cancellation_withdraw_queued_continuation(
    projects: ProjectStore,
):
    supervisor, plans = setup(projects)
    message_id = supervisor.queue("p", "s", RUN)
    assert message_id is not None
    current = plans.current(actor())
    assert current is not None
    update(plans, current.head.ref, status="completed")
    status(plans, current.head.ref, "completed", operation="finish", result=outcome())
    assert claim(supervisor, projects) == ()
    assert RunState(projects.control).messages("p", "s")[0].state == "refused"


def test_late_cancellation_withdraws_queued_continuation(projects: ProjectStore):
    supervisor, _ = setup(projects)
    message_id = supervisor.queue("p", "s", RUN)
    assert message_id is not None
    RunState(projects.control).cancel("p", "s", RUN)
    assert claim(supervisor, projects) == ()
    assert RunState(projects.control).messages("p", "s")[0].state == "refused"


def test_input_after_claim_waits_for_next_turn(projects: ProjectStore):
    supervisor, _ = setup(projects)
    synthetic = supervisor.queue("p", "s", RUN)
    assert synthetic is not None
    selected = claim(supervisor, projects)
    assert selected == (synthetic,)
    RunState(projects.control).post("p", "s", "later-correction", "Change direction", author="user")
    assert selected == (synthetic,)
    assert [m.message_id for m in RunState(projects.control).messages("p", "s")] == [
        synthetic,
        "later-correction",
    ]


def test_unsettled_child_prevents_next_root(projects: ProjectStore):
    supervisor, _ = setup(projects, child=True)
    assert supervisor.queue("p", "s", RUN) is None


async def test_service_continues_only_below_existing_cost_gate(harness: Harness):
    async with serving(harness, serve=False) as service:
        setup(service.stores.projects)
        service.runs.set_cost_limit("p", 0)
        await service.auto_continuation_tick()
        assert service.runs.messages("p", "s") == []
        service.runs.set_cost_limit("p", 100)
        await service.auto_continuation_tick()
        messages = service.runs.messages("p", "s")
        assert len(messages) == 1
        assert messages[0].author == "research-supervisor"


async def test_conflicting_message_id_does_not_stop_service(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
):
    async with serving(harness, serve=False) as service:
        setup(service.stores.projects)
        runs = service.runs
        runs.post("p", "s", "occupied-id", "User's earlier note", author="user")
        runs.deliver("p", "s", ["occupied-id"], "synthetic-earlier-root")
        monkeypatch.setattr(
            "sandbox_science.orchestrator.continuity.auto_continuation.new_id",
            lambda prefix: "occupied-id",
        )
        await service.auto_continuation_tick()
        assert len(runs.messages("p", "s")) == 1
