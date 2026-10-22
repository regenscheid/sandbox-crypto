# SPDX-License-Identifier: MIT
"""Operator reconciliation retains uncertainty without dispatch or invented responses."""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from sandbox_science.contracts import SYSTEM_CLOCK
from sandbox_science.orchestrator.context.history import Archive
from sandbox_science.orchestrator.context.history.operations import ToolJournal
from sandbox_science.orchestrator.continuity.models import ModelIntents
from sandbox_science.orchestrator.continuity.records import WorkRecords
from sandbox_science.orchestrator.continuity.resolutions import RecoveryControls, Resolution
from sandbox_science.orchestrator.continuity.status import RecoveryStatus
from sandbox_science.orchestrator.control import ControlError
from sandbox_science.orchestrator.projects import ProjectStore
from sandbox_science.orchestrator.runstate import RunState

from .test_tool_journal import CALL, CONTEXT, RUN
from .test_tool_journal import projects as projects
from .test_work_ownership import record

CHILD = "20261016T030001000000Z-child-1"


def body(kind: str, identity: str, **changes) -> Resolution:
    return Resolution(
        **{
            "work": RUN,
            "current": RUN,
            "run": RUN,
            "epoch": 1,
            "kind": kind,
            "identity": identity,
            "reason": "Provider evidence could not be recovered.",
            **changes,
        }
    )


def paused(projects: ProjectStore):
    works = WorkRecords(projects.control)
    owner = works.admit("p", "s", RUN)
    ToolJournal(projects.control).start(CONTEXT, RUN, "original-operation", CALL)
    identity = ModelIntents(projects.control).begin(owner, kind="main", alias="fixture")
    works.interrupt("p", "s", RUN)
    return works, identity


def test_operator_cost_is_explicit_exact_once_and_visible_without_fake_response(projects):
    _, identity = paused(projects)
    controls = RecoveryControls(projects.control)
    request = body("model_usage", identity, cost=3.5)
    decision = controls.resolve("p", "s", request, operator="user")
    assert controls.resolve("p", "s", request, operator="user") == decision
    assert ModelIntents(projects.control).pending("p", "s", RUN) == ()
    message = Archive(projects.control).message("p", decision.message_id)
    assert message.entry.kind == "notice"
    assert message.entry.source == "operator_recovery"
    assert "supplied cost 3.5" in (message.entry.content or "")
    assert RunState(projects.control).model_cost("p") == 3.5
    state = RunState(projects.control)
    state.set_cost_limit("p", 1)
    assert state.over_cost_limit("p", SYSTEM_CLOCK.now())
    with pytest.raises(ControlError, match="another operator decision"):
        controls.resolve("p", "s", request.model_copy(update={"cost": 0}), operator="user")
    assert RunState(projects.control).model_cost("p") == 3.5


@pytest.mark.parametrize("cost", [None, -1, float("inf"), float("nan")])
def test_unknown_model_cost_never_defaults_to_zero(cost):
    with pytest.raises(ValidationError):
        body("model_usage", "model-unknown", cost=cost)


def test_operation_resolution_preserves_original_call_and_never_claims_success(projects):
    paused(projects)
    controls = RecoveryControls(projects.control)
    request = body("operation", "original-operation")
    decision = controls.resolve("p", "s", request, operator="user")
    assert controls.resolve("p", "s", request, operator="user") == decision
    assert ToolJournal(projects.control).pending("p", "s", RUN) == []
    archived = Archive(projects.control).message("p", decision.message_id)
    assert archived.entry.call_id == CALL.id
    assert archived.entry.source == CALL.name
    assert "effects remain unknown" in (archived.entry.content or "")
    record = ToolJournal(projects.control).record("p", decision.message_id)
    assert record["arguments"] == CALL.arguments
    assert record["tool"] == CALL.name
    assert record["call_id"] == CALL.id
    assert record["operator_uncertainty"]["operation_id"] == "original-operation"
    assert "success" not in record


def test_kernel_recompute_archives_acknowledgement_and_only_clears_saved_observation(projects):
    paused(projects)
    with projects.control.write() as tx:
        tx.sql.execute(
            "INSERT INTO research_observations VALUES ('p','s',?,'kernels','latest',0,?)",
            (RUN, json.dumps([{"language": "python", "kernel_id": "lost", "generation": 1}])),
        )
    identity = RecoveryStatus(projects.control).pending("p", "s", RUN).kernel_identity
    assert identity is not None
    request = body("kernel_recompute", identity)
    controls = RecoveryControls(projects.control)
    decision = controls.resolve("p", "s", request, operator="user")
    assert controls.resolve("p", "s", request, operator="user") == decision
    assert not RecoveryStatus(projects.control).pending("p", "s", RUN).saved_kernel
    assert "No code was executed" in (
        Archive(projects.control).message("p", decision.message_id).entry.content or ""
    )


@pytest.mark.parametrize("state", ["running", "completed", "stopped"])
def test_active_or_terminal_work_cannot_be_resolved(projects, state):
    paused(projects)
    with projects.control.write() as tx:
        tx.sql.execute("UPDATE research_work SET state=?", (state,))
    with pytest.raises(ControlError, match="inactive unfinished"):
        RecoveryControls(projects.control).resolve(
            "p", "s", body("operation", "original-operation"), operator="user"
        )
    assert len(ToolJournal(projects.control).pending("p", "s", RUN)) == 1


def test_live_lease_pending_resume_and_stale_scope_are_refused(projects):
    works, identity = paused(projects)
    controls = RecoveryControls(projects.control)
    request = body("model_usage", identity, cost=1)
    for changed in ({"epoch": 2}, {"current": CHILD}):
        with pytest.raises(ControlError, match="changed"):
            controls.resolve("p", "s", request.model_copy(update=changed), operator="user")
    for project, session in (("other", "s"), ("p", "other")):
        with pytest.raises(ControlError, match="scope"):
            controls.resolve(project, session, request, operator="user")
    state = RunState(projects.control)
    state.acquire("p", "s", "run-active", "holder")
    with pytest.raises(ControlError, match="active"):
        controls.resolve("p", "s", request, operator="user")
    state.release("p", "s", "holder")
    works.request("p", "s")
    with pytest.raises(ControlError, match="Resume"):
        controls.resolve("p", "s", request, operator="user")


def test_original_run_must_belong_to_selected_work(projects):
    works = WorkRecords(projects.control)
    works.admit("p", "s", RUN)
    child = works.admit("p", "s", CHILD, parent=RUN)
    identity = ModelIntents(projects.control).begin(child, kind="delegated", alias="fixture")
    works.interrupt("p", "s", RUN)
    with pytest.raises(ControlError, match="outside this work"):
        RecoveryControls(projects.control).resolve(
            "p", "s", body("model_usage", identity, cost=1, run=CHILD), operator="user"
        )
    assert ModelIntents(projects.control).pending("p", "s", CHILD) == (identity,)


def test_archive_failure_rolls_back_pending_settlement_and_decision(projects, monkeypatch):
    _, identity = paused(projects)
    original = Archive.append

    def failed(self, *args, **kwargs):
        original(self, *args, **kwargs)
        raise RuntimeError("synthetic archive acknowledgement failure")

    with monkeypatch.context() as patch:
        patch.setattr(Archive, "append", failed)
        with pytest.raises(RuntimeError, match="synthetic"):
            RecoveryControls(projects.control).resolve(
                "p", "s", body("model_usage", identity, cost=2), operator="user"
            )
    assert ModelIntents(projects.control).pending("p", "s", RUN) == (identity,)
    with projects.control.read() as tx:
        assert tx.sql.execute("SELECT count(*) FROM archive_messages").fetchone()[0] == 0
        assert tx.sql.execute("SELECT count(*) FROM research_observations").fetchone()[0] == 0


def test_status_keeps_older_resumable_tree_visible_after_new_root_finishes(projects):
    works, _ = paused(projects)
    newer = "20261017T030000000000Z-new-root"
    works.admit("p", "s", newer)
    status = RecoveryStatus(projects.control)
    assert status.page("p", "s").active
    works.finish(record(newer))
    page = status.page("p", "s")
    assert page.root == RUN
    assert page.unfinished == 1
    assert not page.active
    assert [work.work for work in page.works] == [RUN]
    request = works.request("p", "s")
    assert request is not None
    pending = status.page("p", "s")
    assert pending.root == RUN
    assert pending.active
    assert pending.resumes[0].root == RUN


def test_status_pages_original_ids_and_resume_reasons(projects):
    works, identity = paused(projects)
    request = works.request("p", "s")
    with projects.control.write() as tx:
        tx.sql.execute(
            "UPDATE research_resumes SET status='blocked',reason='unknown_model_outcome' "
            "WHERE request=?",
            (request,),
        )
    status = RecoveryStatus(projects.control)
    page = status.page("p", "s", limit=1)
    assert page.works[0].work == RUN
    assert page.root == RUN
    assert page.unfinished == 1
    assert not page.active
    assert page.resumes[0].reason == "unknown_model_outcome"
    pending = status.pending("p", "s", RUN, limit=1)
    assert pending.models[0].identity == identity
    assert pending.models[0].run == RUN
    assert pending.operations[0].identity == "original-operation"
    assert pending.next_models is not None
    assert pending.next_operation is not None
    assert (
        status.pending(
            "p", "s", RUN, model_after=pending.next_models, operation_after=pending.next_operation
        ).models
        == ()
    )


@pytest.mark.parametrize(
    ("kind", "role"),
    [
        ("reviewer", "reviewer"),
        ("watcher", "watcher"),
        ("embedding", "embeddings"),
        ("skills", "metadata"),
    ],
)
def test_operator_supplied_auxiliary_cost_retains_role(projects, kind, role):
    works = WorkRecords(projects.control)
    owner = works.admit("p", "s", RUN)
    identity = ModelIntents(projects.control).begin(owner, kind=kind, alias="fixture")
    works.interrupt("p", "s", RUN)
    RecoveryControls(projects.control).resolve(
        "p", "s", body("model_usage", identity, cost=2.25), operator="user"
    )
    assert RunState(projects.control).model_costs("p") == {role: 2.25}


def test_operator_cost_window_uses_original_dispatch_not_resolution_date(projects):
    from datetime import UTC, datetime, timedelta

    from sandbox_science.testing import FakeClock

    started = datetime(2026, 10, 10, tzinfo=UTC)
    works = WorkRecords(projects.control)
    owner = works.admit("p", "s", RUN)
    identity = ModelIntents(projects.control, clock=FakeClock(started)).begin(
        owner, kind="main", alias="fixture"
    )
    works.interrupt("p", "s", RUN)
    RecoveryControls(projects.control, clock=FakeClock(started + timedelta(days=2))).resolve(
        "p", "s", body("model_usage", identity, cost=4), operator="user"
    )
    assert RunState(projects.control).model_cost("p") == 4
    assert RunState(projects.control).model_cost("p", started + timedelta(days=1)) is None


def test_large_original_arguments_are_not_loaded_or_silently_truncated(projects):
    from sandbox_science.orchestrator.model import ToolCall

    paused(projects)
    ToolJournal(projects.control).start(
        CONTEXT, RUN, "large-original", ToolCall("call-large", "shell", "x" * (1024 * 1024))
    )
    with pytest.raises(ControlError, match="exceeds 1 MiB"):
        RecoveryControls(projects.control).resolve(
            "p", "s", body("operation", "large-original"), operator="user"
        )
    page = RecoveryStatus(projects.control).pending("p", "s", RUN)
    assert {item.identity for item in page.operations} == {"original-operation", "large-original"}
    assert "xxx" not in page.model_dump_json()


def test_pending_operation_pages_never_exceed100_and_keep_original_ids(projects):
    paused(projects)
    journal = ToolJournal(projects.control)
    for index in range(100):
        journal.start(CONTEXT, RUN, f"original-{index:03}", CALL)
    status = RecoveryStatus(projects.control)
    first = status.pending("p", "s", RUN)
    assert len(first.operations) == 100
    assert first.next_operation is not None
    second = status.pending("p", "s", RUN, operation_after=first.next_operation)
    assert len(second.operations) == 1
    assert second.next_operation is None
    assert len({item.identity for item in (*first.operations, *second.operations)}) == 101


def test_zero_cost_requires_an_explicit_operator_declaration(projects):
    _, identity = paused(projects)
    decision = RecoveryControls(projects.control).resolve(
        "p", "s", body("model_usage", identity, cost=0), operator="user"
    )
    assert decision.resolution.cost == 0
    assert RunState(projects.control).model_cost("p") == 0


def test_kernel_acknowledgement_cannot_clear_a_changed_observation(projects):
    paused(projects)
    with projects.control.write() as tx:
        tx.sql.execute(
            "INSERT INTO research_observations VALUES ('p','s',?,'kernels','latest',0,'[]')", (RUN,)
        )
    identity = RecoveryStatus(projects.control).pending("p", "s", RUN).kernel_identity
    assert identity is not None
    with projects.control.write() as tx:
        tx.sql.execute("UPDATE research_observations SET detail='[1]' WHERE kind='kernels'")
    with pytest.raises(ControlError, match="observation changed"):
        RecoveryControls(projects.control).resolve(
            "p", "s", body("kernel_recompute", identity), operator="user"
        )
    assert RecoveryStatus(projects.control).pending("p", "s", RUN).saved_kernel


def test_child_resolution_uses_exact_original_parent_attempt(projects):
    from sandbox_science.orchestrator.delegation.records import DelegationRecords

    from .test_delegation_restart import _delegation

    works = WorkRecords(projects.control)
    works.admit("p", "s", RUN)
    child = works.admit("p", "s", CHILD, parent=RUN)
    original_parent = "20261016T020001000000Z-original-parent"
    made = _delegation(0).model_copy(update={"parent": original_parent, "run": CHILD})
    DelegationRecords(projects.control).start("p", "s", made)
    identity = ModelIntents(projects.control).begin(child, kind="delegated", alias="fixture")
    works.interrupt("p", "s", RUN)
    decision = RecoveryControls(projects.control).resolve(
        "p",
        "s",
        body(
            "model_usage",
            identity,
            work=CHILD,
            current=CHILD,
            run=CHILD,
            cost=1,
            input_tokens=12,
            output_tokens=3,
        ),
        operator="user",
    )
    assert Archive(projects.control).message("p", decision.message_id).parent == original_parent
    with projects.control.read() as tx:
        usage = tx.sql.execute(
            "SELECT json_extract(usage,'$.input_tokens'),json_extract(usage,'$.output_tokens') "
            "FROM research_model_calls "
            "WHERE project='p' AND run=? AND identity=?",
            (CHILD, identity),
        ).fetchone()
    assert usage == (12, 3)


@pytest.mark.parametrize(
    "tokens", [{"input_tokens": 1}, {"output_tokens": 2}, {"input_tokens": -1, "output_tokens": 2}]
)
def test_operator_tokens_require_both_explicit_nonnegative_counts(tokens):
    with pytest.raises(ValidationError):
        body("model_usage", "unknown", cost=1, **tokens)
