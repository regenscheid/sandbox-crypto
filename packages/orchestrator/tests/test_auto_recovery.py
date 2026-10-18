# SPDX-License-Identifier: MIT
"""Durable, bounded recovery of saved model-phase failures."""

from __future__ import annotations

from datetime import UTC, datetime

from sandbox_science.orchestrator.continuity.auto_recovery import AutoRecovery
from sandbox_science.orchestrator.continuity.models import ModelIntents
from sandbox_science.orchestrator.continuity.records import WorkRecords
from sandbox_science.orchestrator.history.operations import ToolJournal
from sandbox_science.orchestrator.model import ToolCall
from sandbox_science.orchestrator.projects import ProjectStore
from sandbox_science.orchestrator.runstate import RunState
from sandbox_science.testing import FakeClock

from .loop_harness import Harness
from .loop_harness import gateway as gateway
from .loop_harness import harness as harness
from .loop_harness import worker as worker
from .test_run_service import serving
from .test_tool_journal import CONTEXT, RUN
from .test_tool_journal import projects as projects
from .test_work_ownership import CHILD, record


def failed_model(projects: ProjectStore, *, child: bool = False) -> None:
    works = WorkRecords(projects.control)
    works.admit("p", "s", RUN)
    if child:
        works.admit("p", "s", CHILD, parent=RUN)
        root = record(RUN)
        projects.save_run(root)
        works.finish(root)
        failed = record(CHILD, stopped="error").model_copy(
            update={
                "role": "delegated",
                "parent": RUN,
                "delegation": "dlg-fixture",
                "failure": "model",
                "final_text": None,
            }
        )
        projects.save_run(failed)
        works.finish(failed)
    else:
        failed = record(RUN, stopped="error").model_copy(
            update={"failure": "model", "final_text": None}
        )
        projects.save_run(failed)
        works.finish(failed)


def test_saved_model_failure_queues_one_resume_across_service_restart(projects: ProjectStore):
    failed_model(projects)
    clock = FakeClock(datetime(2026, 10, 18, tzinfo=UTC))
    recovery = AutoRecovery(projects.control, clock=clock)
    assert recovery.candidates() == (("p", "s", RUN),)
    request = recovery.queue("p", "s", RUN)
    assert request is not None
    projects.control.close()
    assert AutoRecovery(projects.control, clock=clock).queue("p", "s", RUN) is None
    assert WorkRecords(projects.control).queued() == [("p", "s", request, RUN)]


def test_child_model_failure_recovers_parent_tree_without_replaying_completed_child(
    projects: ProjectStore,
):
    failed_model(projects, child=True)
    request = AutoRecovery(projects.control).queue("p", "s", RUN)
    assert request is not None
    assert WorkRecords(projects.control).get("p", "s", RUN).reason == "unfinished_children"


def test_retries_have_durable_backoff_and_stop_after_three_without_progress(
    projects: ProjectStore,
):
    failed_model(projects)
    clock = FakeClock(datetime(2026, 10, 18, tzinfo=UTC))
    recovery = AutoRecovery(projects.control, clock=clock)
    works = WorkRecords(projects.control)
    first = recovery.queue("p", "s", RUN)
    assert first is not None
    for index, seconds in enumerate((30, 60, 120), start=1):
        works.reject_queued("p", "s", first, "synthetic gateway timeout")
        assert recovery.queue("p", "s", RUN) is None
        clock.advance(seconds)
        following = AutoRecovery(projects.control, clock=clock).queue("p", "s", RUN)
        if index == 3:
            assert following is None
        else:
            assert following is not None
            assert following != first
            first = following


def test_uncertain_effects_are_not_auto_resumed(projects: ProjectStore):
    failed_model(projects)
    recovery = AutoRecovery(projects.control)
    ToolJournal(projects.control).start(
        CONTEXT, RUN, "pending-effect", ToolCall("call-1", "shell", "{}")
    )
    assert recovery.queue("p", "s", RUN) is None


def test_grant_exhaustion_and_cancellation_are_not_auto_resumed(projects: ProjectStore):
    failed_model(projects)
    recovery = AutoRecovery(projects.control)
    works = WorkRecords(projects.control)
    with projects.control.write() as tx:
        tx.sql.execute("UPDATE research_work SET reason='run_budget' WHERE work=?", (RUN,))
    assert recovery.queue("p", "s", RUN) is None
    with projects.control.write() as tx:
        tx.sql.execute("UPDATE research_work SET reason='error' WHERE work=?", (RUN,))
    works.stop("p", "s", RUN)
    assert recovery.queue("p", "s", RUN) is None


def test_non_model_failure_does_not_auto_resume(projects: ProjectStore):
    works = WorkRecords(projects.control)
    works.admit("p", "s", RUN)
    failed = record(RUN, stopped="error").model_copy(update={"failure": "tool"})
    projects.save_run(failed)
    works.finish(failed)
    assert AutoRecovery(projects.control).queue("p", "s", RUN) is None


def test_fresh_user_correction_takes_precedence_over_automatic_resume(projects: ProjectStore):
    failed_model(projects)
    RunState(projects.control).post("p", "s", "new-user-message", "Correct the plan", author="user")
    assert AutoRecovery(projects.control).queue("p", "s", RUN) is None


def test_unknown_model_charge_remains_visible_without_blocking_recovery(projects: ProjectStore):
    works = WorkRecords(projects.control)
    owner = works.admit("p", "s", RUN)
    identity = ModelIntents(projects.control).begin(owner, kind="main", alias="fixture")
    failed = record(RUN, stopped="error").model_copy(update={"failure": "model"})
    projects.save_run(failed)
    works.finish(failed)
    assert ModelIntents(projects.control).pending("p", "s", RUN) == (identity,)
    assert AutoRecovery(projects.control).queue("p", "s", RUN) is not None
    assert ModelIntents(projects.control).pending("p", "s", RUN) == (identity,)


async def test_service_queues_only_when_cost_gate_allows(harness: Harness):
    async with serving(harness, serve=False) as service:
        failed_model(service.stores.projects)
        service.runs.set_cost_limit("p", 0)
        await service.auto_recovery_tick()
        assert WorkRecords(service.stores.projects.control).queued() == []
        service.runs.set_cost_limit("p", 100)
        await service.auto_recovery_tick()
        assert len(WorkRecords(service.stores.projects.control).queued()) == 1
