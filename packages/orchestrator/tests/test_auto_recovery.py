# SPDX-License-Identifier: MIT
"""Durable, bounded recovery of saved model-phase failures."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from sandbox_science.orchestrator.context.history import Archive, Entry
from sandbox_science.orchestrator.context.history.operations import ToolJournal
from sandbox_science.orchestrator.context.history.usage import AnsweredUsage
from sandbox_science.orchestrator.continuity.auto_recovery import AutoRecovery, mark_transient
from sandbox_science.orchestrator.continuity.blueprints import Blueprint, Blueprints
from sandbox_science.orchestrator.continuity.models import ModelIntents
from sandbox_science.orchestrator.continuity.records import WorkRecords
from sandbox_science.orchestrator.control import ControlError
from sandbox_science.orchestrator.model import ToolCall
from sandbox_science.orchestrator.projects import ModelUsage, ProjectStore
from sandbox_science.orchestrator.runstate import RunState
from sandbox_science.testing import FakeClock, eventually

from .loop_harness import Harness
from .loop_harness import gateway as gateway
from .loop_harness import harness as harness
from .loop_harness import worker as worker
from .test_run_service import serving
from .test_tool_journal import CONTEXT, RUN
from .test_tool_journal import projects as projects
from .test_work_ownership import CHILD, CONTINUATION, record


def failed_model(projects: ProjectStore, *, child: bool = False) -> None:
    works = WorkRecords(projects.control)
    owner = works.admit("p", "s", RUN)
    Blueprints(projects.control).save(
        owner,
        Blueprint(context=CONTEXT, request={"project": "p", "session": "s", "task": "Fixture"}),
    )
    if child:
        child_owner = works.admit("p", "s", CHILD, parent=RUN)
        Blueprints(projects.control).save(
            child_owner,
            Blueprint(context=CONTEXT, request={"project": "p", "session": "s", "task": "Child"}),
        )
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
    assert WorkRecords(projects.control).resume_source("p", "s", request) == "automatic"
    projects.control.close()
    assert AutoRecovery(projects.control, clock=clock).queue("p", "s", RUN) is None
    assert WorkRecords(projects.control).queued() == [("p", "s", request, RUN)]


def test_valid_automatic_resume_can_claim_the_original_tree(projects: ProjectStore):
    failed_model(projects)
    works = WorkRecords(projects.control)
    request = AutoRecovery(projects.control).queue("p", "s", RUN)
    assert request is not None
    state = RunState(projects.control)
    state.acquire("p", "s", CONTINUATION, "fixture")
    try:
        root, epoch = works.claim("p", "s", request, lease_run=CONTINUATION)
    finally:
        state.release("p", "s", "fixture")
    assert (root, epoch) == (RUN, 2)
    assert works.get("p", "s", RUN).state == "waiting"
    assert works.queued() == []


def test_candidate_scan_pages_many_paused_sessions(projects: ProjectStore):
    for index in range(101):
        session = f"paused-{index:03}"
        projects.open_session("p", session, dedicated=False)
        with projects.control.write() as tx:
            tx.sql.execute("INSERT INTO research_sessions VALUES (?,?,?,1)", ("p", session, RUN))
            tx.sql.execute(
                "INSERT INTO research_work VALUES (?,?,?,?,?,?,?,?,?,?)",
                ("p", session, RUN, RUN, None, RUN, "main", None, "paused", "error"),
            )
    recovery = AutoRecovery(projects.control)
    first = recovery.candidates()
    second = recovery.candidates(after=first[-1])
    assert len(first) == 100
    assert len(second) == 1
    assert {session for _, session, _ in (*first, *second)} == {
        f"paused-{index:03}" for index in range(101)
    }


def test_child_model_failure_recovers_parent_tree_without_replaying_completed_child(
    projects: ProjectStore,
):
    failed_model(projects, child=True)
    request = AutoRecovery(projects.control).queue("p", "s", RUN)
    assert request is not None
    assert WorkRecords(projects.control).get("p", "s", RUN).reason == "unfinished_children"


def test_parent_waiting_after_successful_child_does_not_qualify_as_model_failure(
    projects: ProjectStore,
):
    works = WorkRecords(projects.control)
    works.admit("p", "s", RUN)
    works.admit("p", "s", CHILD, parent=RUN)
    root = record(RUN)
    projects.save_run(root)
    works.finish(root)
    child = record(CHILD).model_copy(
        update={"role": "delegated", "parent": RUN, "delegation": "dlg-fixture"}
    )
    projects.save_run(child)
    works.finish(child)
    assert works.get("p", "s", RUN).reason == "unfinished_children"
    assert works.get("p", "s", CHILD).state == "completed"
    assert AutoRecovery(projects.control).queue("p", "s", RUN) is None


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


def test_irreconcilable_effects_are_not_auto_resumed(projects: ProjectStore):
    failed_model(projects)
    recovery = AutoRecovery(projects.control)
    ToolJournal(projects.control).start(
        CONTEXT, RUN, "pending-effect", ToolCall("call-1", "mcp__write_remote", "{}")
    )
    assert recovery.queue("p", "s", RUN) is None


def test_worker_effects_are_auto_resumed_for_reconciliation_by_identity(projects: ProjectStore):
    """D100: a pending worker operation is reconciled by its recorded identity, not replayed."""
    failed_model(projects)
    ToolJournal(projects.control).start(
        CONTEXT, RUN, "pending-effect", ToolCall("call-1", "shell", "{}")
    )
    assert AutoRecovery(projects.control).queue("p", "s", RUN) is not None
    assert len(ToolJournal(projects.control).pending("p", "s", RUN)) == 1


@pytest.mark.parametrize(
    ("tool", "allowed"),
    [("sage", True), ("wait_agent_message", True), ("mcp__write_remote", False)],
)
def test_infrastructure_policy_admits_only_reconcilable_original_effects(
    projects: ProjectStore,
    tool: str,
    allowed: bool,
):
    works = WorkRecords(projects.control)
    works.admit("p", "s", RUN)
    ToolJournal(projects.control).start(
        CONTEXT, RUN, "original-effect", ToolCall("call-original", tool, "{}")
    )
    works.interrupt("p", "s", RUN)
    assert AutoRecovery(projects.control).candidates() == ()
    assert AutoRecovery(projects.control).queue("p", "s", RUN) is None
    recovery = AutoRecovery(projects.control, interrupted=True)
    assert recovery.candidates() == (("p", "s", RUN),)
    assert (recovery.queue("p", "s", RUN) is not None) is allowed
    assert len(ToolJournal(projects.control).pending("p", "s", RUN)) == 1


def test_infrastructure_policy_cannot_override_user_stop(projects: ProjectStore):
    works = WorkRecords(projects.control)
    works.admit("p", "s", RUN)
    works.interrupt_run("p", "s", RUN, stopped=True)
    recovery = AutoRecovery(projects.control, interrupted=True)
    assert recovery.candidates() == ()
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


def test_cancelled_prior_attempt_does_not_repeat_auto_queue(projects: ProjectStore):
    failed_model(projects)
    works = WorkRecords(projects.control)
    request = works.request("p", "s")
    assert request is not None
    state = RunState(projects.control)
    state.acquire("p", "s", CONTINUATION, "fixture")
    try:
        _, epoch = works.claim("p", "s", request, lease_run=CONTINUATION)
    finally:
        state.release("p", "s", "fixture")
    works.admit("p", "s", CONTINUATION, work=RUN, predecessor=RUN, epoch=epoch)
    failed = record(CONTINUATION, stopped="error").model_copy(
        update={"failure": "model", "final_text": None}
    )
    projects.save_run(failed)
    works.finish(failed)
    state.cancel("p", "s", RUN)
    assert works.get("p", "s", RUN).current == CONTINUATION
    assert AutoRecovery(projects.control).queue("p", "s", RUN) is None
    assert works.queued() == []


def test_spent_original_tokens_no_longer_block_automatic_recovery(projects: ProjectStore):
    """D102: tokens are a soft cap that checks in; no local token grant blocks Resume."""
    works = WorkRecords(projects.control)
    owner = works.admit("p", "s", RUN)
    Blueprints(projects.control).save(
        owner,
        Blueprint(
            context=CONTEXT,
            request={"project": "p", "session": "s", "task": "Fixture", "tokens": 50_000},
        ),
    )
    Archive(projects.control).append(
        "p",
        "s",
        RUN,
        Entry("assistant", "assistant", "Earlier answer"),
        model_usage=AnsweredUsage(
            ordinal=1,
            observed_at=datetime.now(UTC),
            usage=ModelUsage(input_tokens=49_999, output_tokens=1, cost=1),
        ),
    )
    failed = record(RUN, stopped="error").model_copy(update={"failure": "model"})
    projects.save_run(failed)
    works.finish(failed)
    assert AutoRecovery(projects.control).queue("p", "s", RUN) is not None


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


def test_old_root_cannot_preempt_a_new_research_tree(projects: ProjectStore):
    failed_model(projects)
    newer = "20261018T030000000000Z-new-root"
    WorkRecords(projects.control).admit("p", "s", newer)
    recovery = AutoRecovery(projects.control)
    assert recovery.candidates() == ()
    assert recovery.queue("p", "s", RUN) is None


@pytest.mark.parametrize("change", ["stop", "new_user", "new_root"])
def test_auto_request_revalidates_authority_at_claim(projects: ProjectStore, change: str):
    failed_model(projects)
    works = WorkRecords(projects.control)
    request = AutoRecovery(projects.control).queue("p", "s", RUN)
    assert request is not None
    if change == "stop":
        works.stop("p", "s", RUN)
    elif change == "new_user":
        RunState(projects.control).post(
            "p", "s", "new-user-message", "Correct the plan", author="user"
        )
    else:
        works.admit("p", "s", "20261018T030000000000Z-new-root")
    before = works.get("p", "s", RUN).epoch
    state = RunState(projects.control)
    state.acquire("p", "s", CONTINUATION, "fixture")
    try:
        with pytest.raises(ControlError, match="automatic Resume invalidated"):
            works.claim("p", "s", request, lease_run=CONTINUATION)
    finally:
        state.release("p", "s", "fixture")
    assert works.queued() == []
    assert works.get("p", "s", RUN).epoch == before


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


async def test_service_queues_even_with_a_reached_local_cost_limit(harness: Harness):
    """D102: no local cost check blocks automatic Resume; a configured limit refuses the
    first paid call instead, and that budget stop is not retried."""
    async with serving(harness, serve=False) as service:
        failed_model(service.stores.projects)
        service.runs.set_cost_limit("p", 0)
        await service.auto_recovery_tick()
        assert len(WorkRecords(service.stores.projects.control).queued()) == 1


@pytest.mark.parametrize("child", [False, True])
async def test_service_end_preserves_saved_model_failure_for_automatic_recovery(
    harness: Harness, child: bool
):
    """The service cleanup path runs after save, not just after process loss."""
    async with serving(harness, serve=False) as service:
        failed_model(service.stores.projects, child=child)
        works = WorkRecords(service.stores.projects.control)
        before = works.page("p", "s", RUN)
        await service._ended("p", "s", RUN, hold=False)
        assert works.page("p", "s", RUN) == before
        await service.auto_recovery_tick()
        queued = works.queued()
        assert len(queued) == 1
        assert queued[0][:2] == ("p", "s")
        assert queued[0][3] == RUN


def test_process_loss_preserves_paused_diagnoses_but_user_stop_overrides_them(
    projects: ProjectStore,
):
    works = WorkRecords(projects.control)
    works.admit("p", "s", RUN)
    works.admit("p", "s", CHILD, parent=RUN)
    works.finish(record(CHILD, stopped="budget"))
    works.interrupt_run("p", "s", RUN)
    root, child = works.get("p", "s", RUN), works.get("p", "s", CHILD)
    assert (root.state, root.reason) == ("paused", "interrupted")
    assert (child.state, child.reason) == ("paused", "budget")
    assert AutoRecovery(projects.control).queue("p", "s", RUN) is None
    works.block("p", "s", CHILD, "unresolved_tool_effect")
    works.interrupt_run("p", "s", RUN)
    assert works.get("p", "s", CHILD).reason == "unresolved_tool_effect"
    works.interrupt_run("p", "s", RUN, stopped=True)
    assert all(
        (item.state, item.reason) == ("stopped", "user_stop") for item in works.page("p", "s", RUN)
    )
    assert AutoRecovery(projects.control).queue("p", "s", RUN) is None


async def test_late_user_correction_does_not_trigger_failure_backoff(harness: Harness):
    async with serving(harness, serve=False) as service:
        failed_model(service.stores.projects)
        request = service.auto_recovery.queue("p", "s", RUN)
        assert request is not None
        service.runs.post("p", "s", "late-user", "Correct the plan", author="user")
        await service.tick()
        await eventually(
            lambda: WorkRecords(service.stores.projects.control).queued() == [], timeout=10
        )

        async def ended() -> bool:
            # The tick is what drops a finished run from ``active``; ``waiting_work``
            # leaves out a session with a run in flight, and ``waiting_until`` is set only
            # as the run ends, so both checks below mean something only after this.
            await service.maintenance_tick()
            return ("p", "s") not in service.active

        await eventually(ended, describe="the resumed run to end")
        assert ("p", "s") not in service.waiting_until
        assert ("p", "s") in await service.waiting_work()


def failed_with(projects: ProjectStore, phase: str, transient: str | None) -> None:
    """A run that failed after the loop classified its failure (D100)."""
    works = WorkRecords(projects.control)
    owner = works.admit("p", "s", RUN)
    Blueprints(projects.control).save(
        owner,
        Blueprint(context=CONTEXT, request={"project": "p", "session": "s", "task": "Fixture"}),
    )
    failed = record(RUN, stopped="error").model_copy(
        update={
            "failure": phase,
            "final_text": None,
            "summaries": {"failure": {"phase": phase, "transient": transient}},
        }
    )
    projects.save_run(failed)
    works.finish(failed)


@pytest.mark.parametrize(
    ("phase", "transient", "queued"),
    [
        ("model", "gateway_timeout", True),
        ("model", "incomplete_stream", True),
        ("model", "network", True),
        ("tool", "worker_loss", True),
        ("model", None, False),  # classified permanent, such as a malformed answer
        ("tool", None, False),
        ("harness", None, False),
    ],
)
def test_only_failures_classified_transient_are_retried(
    projects: ProjectStore, phase: str, transient: str | None, queued: bool
):
    failed_with(projects, phase, transient)
    assert (AutoRecovery(projects.control).queue("p", "s", RUN) is not None) is queued


def test_each_retry_decision_is_persisted_with_its_cause(projects: ProjectStore):
    failed_with(projects, "model", "gateway_timeout")
    request = AutoRecovery(projects.control).queue("p", "s", RUN)
    with projects.control.read() as tx:
        (detail,) = tx.sql.execute(
            "SELECT detail FROM research_observations WHERE kind='auto_resume' AND identity=?",
            (request,),
        ).fetchone()
    saved = json.loads(detail)
    assert saved["cause"] == "gateway_timeout"
    assert (saved["streak"], saved["attempts"], saved["delay"]) == (1, 1, 0.0)
    assert len(saved["progress"]) == 4


def test_exhausted_retries_block_the_root_with_a_durable_reason(projects: ProjectStore):
    failed_with(projects, "model", "gateway_timeout")
    clock = FakeClock(datetime(2026, 10, 23, tzinfo=UTC))
    works = WorkRecords(projects.control)
    for seconds in (0, 30, 60, 120):
        clock.advance(seconds)
        request = AutoRecovery(projects.control, clock=clock).queue("p", "s", RUN)
        if request is None:
            break
        works.reject_queued("p", "s", request, "synthetic gateway timeout again")
    root = works.get("p", "s", RUN)
    assert root.state == "blocked"
    assert root.reason is not None
    assert root.reason.startswith("retries_exhausted: no progress")
    assert "gateway_timeout" in root.reason
    with projects.control.read() as tx:
        assert tx.sql.execute(
            "SELECT count(*) FROM research_observations WHERE kind='auto_resume_stopped'"
        ).fetchone() == (1,)
    assert AutoRecovery(projects.control).candidates() == ()
    # The user's one Resume still continues the tree.
    assert works.request("p", "s") is not None


def test_persistence_error_is_retried(projects: ProjectStore):
    works = WorkRecords(projects.control)
    works.admit("p", "s", RUN)
    saved = record(RUN)
    works.finish(saved, persisted=False)
    assert works.get("p", "s", RUN).reason == "persistence_error"
    recovery = AutoRecovery(projects.control)
    assert recovery.candidates() == (("p", "s", RUN),)
    assert recovery.queue("p", "s", RUN) is not None


def test_bridge_loss_retries_interrupted_work_once_per_loss(projects: ProjectStore):
    works = WorkRecords(projects.control)
    works.admit("p", "s", RUN)
    works.interrupt_run("p", "s", RUN)
    assert AutoRecovery(projects.control).queue("p", "s", RUN) is None  # a plain restart
    mark_transient(projects.control, "p", "s", RUN, "bridge_loss")
    recovery = AutoRecovery(projects.control)
    assert recovery.candidates() == (("p", "s", RUN),)
    request = recovery.queue("p", "s", RUN)
    assert request is not None
    works.reject_queued("p", "s", request, "interrupted")
    # The loss was retried; a later interruption without a new loss is not that loss.
    assert AutoRecovery(projects.control).queue("p", "s", RUN) is None


def test_a_resume_branch_ended_by_worker_loss_is_retried(projects: ProjectStore):
    works = WorkRecords(projects.control)
    works.admit("p", "s", RUN)
    works.block("p", "s", RUN, "transient:worker_loss: WorkerAPIError: cannot reach the worker")
    assert AutoRecovery(projects.control).queue("p", "s", RUN) is not None


def test_explicit_stop_stays_stopped_after_a_transient_failure(projects: ProjectStore):
    failed_with(projects, "model", "gateway_timeout")
    works = WorkRecords(projects.control)
    works.stop("p", "s", RUN)
    recovery = AutoRecovery(projects.control, interrupted=True)
    assert recovery.candidates() == ()
    assert recovery.queue("p", "s", RUN) is None
    assert works.get("p", "s", RUN).state == "stopped"


def test_a_saved_record_whose_acknowledgement_failed_is_not_run_again(projects: ProjectStore):
    works = WorkRecords(projects.control)
    works.admit("p", "s", RUN)
    saved = record(RUN)
    projects.save_run(saved)
    works.finish(saved, persisted=False)
    assert works.get("p", "s", RUN).reason == "persistence_error"
    assert AutoRecovery(projects.control).queue("p", "s", RUN) is None
