# SPDX-License-Identifier: MIT
"""Conservative operator reservations authorize no invented usage or response."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import cast

import pytest
from pydantic import ValidationError

from sandbox_science.contracts import SYSTEM_CLOCK
from sandbox_science.orchestrator.continuity.blueprints import Blueprint, Blueprints
from sandbox_science.orchestrator.continuity.models import ModelIntents
from sandbox_science.orchestrator.continuity.plans import Plans
from sandbox_science.orchestrator.continuity.reconcile import Reconciler
from sandbox_science.orchestrator.continuity.records import WorkRecords
from sandbox_science.orchestrator.continuity.resolutions import RecoveryControls
from sandbox_science.orchestrator.continuity.status import RecoveryStatus
from sandbox_science.orchestrator.control import ControlError
from sandbox_science.orchestrator.delegation.records import DelegationRecords
from sandbox_science.orchestrator.history import Archive
from sandbox_science.orchestrator.runstate import RunState
from sandbox_science.orchestrator.worker_client import WorkerClient
from sandbox_science.testing import FakeClock

from .test_delegation_restart import _delegation
from .test_recovery_controls import CHILD, body
from .test_tool_journal import CONTEXT, RUN
from .test_tool_journal import projects as projects
from .test_work_ownership import record


def unknown(projects, *, child=False, clock=SYSTEM_CLOCK):
    works = WorkRecords(projects.control)
    owner = works.admit("p", "s", RUN)
    work = RUN
    if child:
        owner = works.admit("p", "s", CHILD, parent=RUN)
        work = CHILD
        DelegationRecords(projects.control).start(
            "p", "s", _delegation(0).model_copy(update={"parent": RUN, "run": CHILD})
        )
        Blueprints(projects.control).save(
            owner,
            Blueprint(
                context=CONTEXT,
                request={"project": "p", "session": "s", "task": "Bounded", "tokens": 100},
            ),
        )
    identity = ModelIntents(projects.control, clock=clock).begin(
        owner, kind="delegated" if child else "main", alias="fixture"
    )
    works.interrupt("p", "s", RUN)
    request = body(
        "model_reservation",
        identity,
        work=work,
        current=work,
        run=work,
        cost=2.0,
        input_tokens=20,
        output_tokens=5,
    )
    return works, owner, identity, request


def test_reservation_is_exact_once_visible_and_separate_from_observed_spend(projects):
    _works, _owner, identity, request = unknown(projects)
    controls = RecoveryControls(projects.control)
    decision = controls.resolve("p", "s", request, operator="user")
    assert controls.resolve("p", "s", request, operator="user") == decision
    assert ModelIntents(projects.control).pending("p", "s", RUN) == ()
    pending = RecoveryStatus(projects.control).pending("p", "s", RUN)
    assert len(pending.models) == 1  # actual usage is still UNKNOWN, not hidden
    reserved = pending.models[0].reservation
    assert reserved is not None
    assert (reserved.cost, reserved.input_tokens, reserved.output_tokens) == (2, 20, 5)
    assert reserved.operator == "user"
    assert reserved.message_id == decision.message_id
    state = RunState(projects.control)
    assert state.model_cost("p") is None
    assert state.model_reservations("p") == {"main": 2}
    state.set_cost_limit("p", 2)
    assert state.over_cost_limit("p", SYSTEM_CLOCK.now())
    state.set_cost_limit("p", 3)
    assert not state.over_cost_limit("p", SYSTEM_CLOCK.now())
    messages = Archive(projects.control).messages("p", "s", run=RUN)
    assert len(messages) == 1
    assert messages[0].entry.kind == "notice"
    assert "actual usage and charge remain UNKNOWN" in (messages[0].entry.content or "")
    assert "No response was recovered or synthesized" in (messages[0].entry.content or "")
    with projects.control.read() as tx:
        assert tx.sql.execute(
            "SELECT usage FROM research_model_calls WHERE identity=?", (identity,)
        ).fetchone() == (None,)
        assert tx.sql.execute("SELECT count(*) FROM archive_model_usage").fetchone() == (0,)


@pytest.mark.parametrize("change", [{"cost": 3}, {"input_tokens": 21}, {"kind": "model_usage"}])
def test_reservation_conflict_cannot_change_bounds_or_claim_observation(projects, change):
    _works, _owner, _identity, request = unknown(projects)
    controls = RecoveryControls(projects.control)
    controls.resolve("p", "s", request, operator="user")
    with pytest.raises(ControlError, match="another operator decision"):
        controls.resolve("p", "s", request.model_copy(update=change), operator="user")
    assert RunState(projects.control).model_cost("p") is None
    assert RunState(projects.control).model_reservations("p") == {"main": 2}


@pytest.mark.parametrize("scope", [("other", "s"), ("p", "other")])
def test_reservation_wrong_scope_cannot_settle_intent(projects, scope):
    _works, _owner, identity, request = unknown(projects)
    with pytest.raises(ControlError, match="no research work"):
        RecoveryControls(projects.control).resolve(scope[0], scope[1], request, operator="user")
    assert ModelIntents(projects.control).pending("p", "s", RUN) == (identity,)


def test_reservation_requires_current_epoch_original_attempt_and_pending_intent(projects):
    _works, _owner, identity, request = unknown(projects)
    controls = RecoveryControls(projects.control)
    for changes, reason in [
        ({"epoch": 2}, "epoch changed"),
        ({"run": CHILD}, "outside this work"),
        ({"identity": "not-a-call"}, "not pending"),
    ]:
        with pytest.raises(ControlError, match=reason):
            controls.resolve("p", "s", request.model_copy(update=changes), operator="user")
    assert ModelIntents(projects.control).pending("p", "s", RUN) == (identity,)


@pytest.mark.parametrize(
    "bounds",
    [
        {"cost": 0},
        {"input_tokens": 0},
        {"output_tokens": 0},
        {"input_tokens": None, "output_tokens": None},
        {"cost": float("inf")},
    ],
)
def test_reservation_requires_explicit_positive_bounds(projects, bounds):
    _works, _owner, _identity, request = unknown(projects)
    with pytest.raises(ValidationError):
        type(request).model_validate({**request.model_dump(), **bounds})


async def test_child_reservation_reduces_original_grant_and_preserves_unknown(projects):
    works, _owner, _identity, request = unknown(projects, child=True)
    controls = RecoveryControls(projects.control)
    controls.resolve("p", "s", request, operator="user")
    plans = Plans(projects.control, "p", "s", "unused")
    blueprint = Blueprints(projects.control).get("p", "s", CHILD)
    assert blueprint.request["tokens"] == 100
    assert plans.spent_tokens(CHILD) == 25
    assert 100 - plans.spent_tokens(CHILD) == 75
    assert plans.spent_tokens(RUN) == 0
    # No tool effects in this fixture: reconciliation needs no worker methods.
    await Reconciler(projects.control, cast(WorkerClient, object())).work(
        works.get("p", "s", CHILD)
    )
    assert (
        RecoveryStatus(projects.control).pending("p", "s", CHILD).models[0].reservation is not None
    )
    works.finish(record(CHILD))
    assert works.get("p", "s", CHILD).state == "completed"


def test_reservation_budget_window_uses_original_call_time(projects):
    started = datetime(2026, 10, 10, tzinfo=UTC)
    _works, _owner, _identity, request = unknown(projects, clock=FakeClock(started))
    RecoveryControls(projects.control, clock=FakeClock(started + timedelta(days=2))).resolve(
        "p", "s", request, operator="user"
    )
    state = RunState(projects.control)
    assert state.model_reservations("p") == {"main": 2}
    assert state.model_reservations("p", started + timedelta(days=1)) == {}
    assert state.model_cost("p") is None


def test_copied_invalid_reservation_cannot_bypass_positive_bound_validation(projects):
    _works, _owner, identity, request = unknown(projects)
    with pytest.raises(ValidationError):
        RecoveryControls(projects.control).resolve(
            "p", "s", request.model_copy(update={"cost": 0}), operator="user"
        )
    assert ModelIntents(projects.control).pending("p", "s", RUN) == (identity,)
    assert RunState(projects.control).model_reservations("p") == {}
    assert Archive(projects.control).messages("p", "s", run=RUN) == []
