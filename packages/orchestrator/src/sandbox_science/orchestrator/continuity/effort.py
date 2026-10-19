# SPDX-License-Identifier: MIT
"""Operator-only, request-scoped Resume effort; aliases and API pins never change."""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from typing import TYPE_CHECKING, Self

from pydantic import Field, ValidationError, model_validator

from sandbox_science.contracts.model import StrictContract
from sandbox_science.orchestrator.control import ControlError, ControlStore
from sandbox_science.orchestrator.control.errors import CONFLICT
from sandbox_science.orchestrator.model_profiles import Effort, ModelProfile, ModelRole
from sandbox_science.orchestrator.projects import RunName

if TYPE_CHECKING:
    from .records import Work


class ResumeEffort(StrictContract):
    reasoning_effort: dict[ModelRole, Effort] = Field(min_length=1, max_length=7)
    reason: str = Field(min_length=1, max_length=2000)

    @model_validator(mode="after")
    def _reason(self) -> Self:
        if not self.reason.strip() or "\0" in self.reason:
            raise ValueError("Resume effort requires an explicit operator reason")
        return self


class EffortAudit(ResumeEffort):
    operator: str = Field(min_length=1, max_length=128)


class ResumeRequest(StrictContract):
    root: RunName | None = None
    reasoning_effort: dict[ModelRole, Effort] | None = Field(
        default=None, min_length=1, max_length=7
    )
    reason: str | None = Field(default=None, min_length=1, max_length=2000)

    @model_validator(mode="after")
    def _explicit(self) -> Self:
        if (self.reasoning_effort is None) != (self.reason is None):
            raise ValueError(
                "Resume reasoning_effort and operator reason must be supplied together"
            )
        if self.reasoning_effort is not None:
            self.effort()
        return self

    def effort(self) -> ResumeEffort | None:
        """The validated explicit choice, separate from its trusted operator identity."""
        if self.reasoning_effort is None:
            return None
        return ResumeEffort.model_validate(
            {"reasoning_effort": self.reasoning_effort, "reason": self.reason}
        )


def read_override(
    db: sqlite3.Connection, project: str, session: str, root: str, request: str
) -> EffortAudit | None:
    """Read one exact immutable request's bounded audit, never another tree's choice."""
    row = db.execute(
        "SELECT detail FROM research_observations WHERE project=? AND session=? AND work=? "
        "AND kind='resume_effort' AND identity=?",
        (project, session, root, request),
    ).fetchone()
    if row is None:
        return None
    try:
        return EffortAudit.model_validate_json(row[0])
    except ValidationError as error:
        raise ControlError(CONFLICT, "saved Resume effort audit is invalid") from error


def apply_resume_effort(
    control: ControlStore,
    request: str,
    work: Work,
    pinned: Mapping[ModelRole, ModelProfile],
    *,
    epoch: int,
    lease_run: str,
) -> dict[ModelRole, ModelProfile]:
    """Overlay only active pinned roles after checking current recovery ownership."""
    with control.read() as tx:
        valid = tx.sql.execute(
            "SELECT 1 FROM research_resumes r JOIN research_sessions s USING(project,session) "
            "JOIN research_work w ON w.project=r.project AND w.session=r.session AND w.root=r.root "
            "JOIN research_observations o ON o.project=r.project AND o.session=r.session "
            "AND o.work=r.root AND o.kind='resume_lease' AND o.identity=r.request "
            "JOIN run_leases l ON l.project_id=r.project AND l.session_id=r.session "
            "AND l.run=json_extract(o.detail,'$.run') WHERE r.project=? AND r.session=? "
            "AND r.request=? AND r.status='running' AND s.root=r.root AND s.epoch=? "
            "AND json_extract(o.detail,'$.epoch')=s.epoch AND l.run=? "
            "AND w.work=? AND w.root=? AND w.current=? AND w.state='waiting'",
            (
                work.project,
                work.session,
                request,
                epoch,
                lease_run,
                work.work,
                work.root,
                work.current,
            ),
        ).fetchone()
        if valid is None or work.epoch != epoch:
            raise ControlError(CONFLICT, "Resume effort override lost recovery ownership")
        audit = read_override(tx.sql, work.project, work.session, work.root, request)
    return {
        role: profile.model_copy(update={"reasoning_effort": audit.reasoning_effort[role]})
        if audit is not None and role in audit.reasoning_effort
        else profile
        for role, profile in pinned.items()
    }
