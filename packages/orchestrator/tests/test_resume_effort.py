# SPDX-License-Identifier: MIT
"""Explicit effort choices are atomic, scoped, replay-safe and ownership-fenced."""

from __future__ import annotations

import sqlite3

import pytest
from pydantic import ValidationError

from sandbox_science.orchestrator.continuity.effort import (
    ResumeEffort,
    ResumeRequest,
    apply_resume_effort,
    read_override,
)
from sandbox_science.orchestrator.continuity.records import WorkRecords
from sandbox_science.orchestrator.control import ControlError
from sandbox_science.orchestrator.model_profiles import ModelProfile, ModelRole
from sandbox_science.orchestrator.projects import ProjectStore
from sandbox_science.orchestrator.runstate import RunState

from .test_tool_journal import RUN
from .test_tool_journal import projects as projects
from .test_work_ownership import CHILD, CONTINUATION


def choice(**values):
    return ResumeEffort.model_validate(
        {
            "reasoning_effort": values or {"main": "high"},
            "reason": "Operator requests deeper analysis.",
        }
    )


def paused(projects):
    works = WorkRecords(projects.control)
    works.admit("p", "s", RUN)
    works.admit("p", "s", CHILD, parent=RUN)
    works.interrupt("p", "s", RUN)
    return works


def claim(projects, works, request):
    RunState(projects.control).acquire("p", "s", CONTINUATION, "fixture")
    return works.claim("p", "s", request, lease_run=CONTINUATION)[1]


@pytest.mark.parametrize(
    "body",
    [
        {"reasoning_effort": {"main": "high"}},
        {"reason": "No choice"},
        {"reasoning_effort": {}, "reason": "Empty"},
        {"reasoning_effort": {"unknown": "high"}, "reason": "Unknown role"},
        {"reasoning_effort": {"main": None}, "reason": "Null"},
        {"reasoning_effort": {"main": "HIGH"}, "reason": "Invalid effort"},
        {"reasoning_effort": {"main": "a" * 33}, "reason": "Oversized"},
        {"reasoning_effort": {"main": True}, "reason": "Wrong type"},
        {"reasoning_effort": {"main": "high"}, "reason": " "},
        {"reasoning_effort": {"main": "high"}, "reason": "a" * 2001},
        {"reasoning_effort": {"main": "high"}, "reason": "Explicit", "alias": "other"},
        {"reasoning_effort": {"main": "high"}, "reason": "Explicit", "operator": "USER"},
    ],
)
def test_resume_validates_only_bounded_explicit_effort_changes(body):
    with pytest.raises(ValidationError):
        ResumeRequest.model_validate(body)


def test_queued_and_running_replays_require_exact_override_audit(projects: ProjectStore):
    works = paused(projects)
    requested = choice()
    request = works.request("p", "s", RUN, effort=requested, operator="user")
    assert request
    for running in (False, True):
        if running:
            claim(projects, works, request)
        assert works.request("p", "s", effort=requested, operator="user") == request
        for different, operator in (
            (None, None),
            (choice(main="medium"), "user"),
            (requested, "other"),
        ):
            with pytest.raises(ControlError, match="override differs"):
                works.request("p", "s", RUN, effort=different, operator=operator)
        changed_reason = requested.model_copy(update={"reason": "A different explicit reason"})
        with pytest.raises(ControlError, match="override differs"):
            works.request("p", "s", RUN, effort=changed_reason, operator="user")
    projects.control.close()
    with projects.control.read() as tx:
        audit = read_override(tx.sql, "p", "s", RUN, request)
        assert audit is not None
        assert audit.reasoning_effort == {"main": "high"}
        assert audit.operator == "user"
        assert read_override(tx.sql, "q", "s", RUN, request) is None
        assert read_override(tx.sql, "p", "s", CHILD, request) is None


def test_a_plain_pending_resume_cannot_gain_an_override(projects):
    works = paused(projects)
    assert works.request("p", "s")
    with pytest.raises(ControlError, match="override differs"):
        works.request("p", "s", effort=choice(), operator="user")


def test_audit_failure_rolls_back_the_resume_request(projects):
    works = paused(projects)
    with projects.control.write() as tx:
        tx.sql.execute(
            "CREATE TRIGGER refuse_effort BEFORE INSERT ON research_observations "
            "WHEN new.kind='resume_effort' BEGIN SELECT RAISE(ABORT,'audit refused'); END"
        )
    with pytest.raises(sqlite3.DatabaseError, match="audit refused"):
        works.request("p", "s", effort=choice(), operator="user")
    assert works.queued() == []


def test_overlay_preserves_pins_dormant_roles_and_original_work(projects):
    works = paused(projects)
    request = works.request("p", "s", effort=choice(main="high", watcher="high"), operator="user")
    assert request
    epoch = claim(projects, works, request)
    work = works.get("p", "s", RUN)
    pinned: dict[ModelRole, ModelProfile] = {
        "main": ModelProfile(alias="original", api="responses", reasoning_effort="medium"),
        "fold": ModelProfile(alias="original-fold", api="chat"),
    }
    effective = apply_resume_effort(
        projects.control, request, work, pinned, epoch=epoch, lease_run=CONTINUATION
    )
    assert effective["main"] == ModelProfile(
        alias="original", api="responses", reasoning_effort="high"
    )
    assert effective["fold"] == pinned["fold"]
    assert "watcher" not in effective
    assert pinned["main"].reasoning_effort == "medium"
    assert works.attempts("p", "s", RUN) == [RUN]
    child = works.get("p", "s", CHILD)
    assert (
        apply_resume_effort(
            projects.control, request, child, pinned, epoch=epoch, lease_run=CONTINUATION
        )
        == effective
    )


@pytest.mark.parametrize("lost", ["queued", "epoch", "lease", "current", "stopped", "settled"])
def test_stale_resume_or_attempt_cannot_adopt_override(projects, lost):
    works = paused(projects)
    request = works.request("p", "s", effort=choice(), operator="user")
    assert request
    epoch = 1 if lost == "queued" else claim(projects, works, request)
    work = works.get("p", "s", RUN)
    if lost == "epoch":
        work = work.model_copy(update={"epoch": epoch - 1})
    elif lost == "lease":
        RunState(projects.control).release("p", "s", "fixture")
    elif lost == "current":
        work = work.model_copy(update={"current": CONTINUATION})
    elif lost == "stopped":
        works.stop("p", "s", RUN)
    elif lost == "settled":
        works.complete_request("p", "s", request)
    with pytest.raises(ControlError, match="lost recovery ownership"):
        apply_resume_effort(
            projects.control,
            request,
            work,
            {"main": ModelProfile(alias="original")},
            epoch=epoch,
            lease_run=CONTINUATION,
        )
