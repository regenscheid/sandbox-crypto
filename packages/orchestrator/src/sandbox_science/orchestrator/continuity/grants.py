# SPDX-License-Identifier: MIT
"""Explicit USER token extensions for paused, direct children of a research root.

This is an operator recovery override. It does not infer a parent's decision from a
project setting or substitute for the planned parent-request protocol.
"""

from __future__ import annotations

import sqlite3
from typing import Annotated

from pydantic import Field

from sandbox_science.contracts import SYSTEM_CLOCK, Clock, Contract
from sandbox_science.contracts.model import Revision
from sandbox_science.orchestrator.control import ControlError, ControlStore
from sandbox_science.orchestrator.control.errors import CONFLICT, INVALID_REQUEST, NOT_FOUND
from sandbox_science.orchestrator.delegation.records import MAX_TOKENS, settings_in
from sandbox_science.orchestrator.documents import DocumentStore
from sandbox_science.orchestrator.history import Archive, Entry
from sandbox_science.orchestrator.projects import RunName
from sandbox_science.orchestrator.runstate import LEASE_SECONDS

GRANT_EXHAUSTED = "model_budget: this work's token grant is exhausted"


class TokenGrantRequest(Contract):
    work: RunName
    current: RunName
    epoch: Revision
    expected_revision: Annotated[int, Field(strict=True, ge=0, le=10_000)]
    total_tokens: Annotated[int, Field(strict=True, ge=50_000, le=MAX_TOKENS)]
    reason: str = Field(min_length=1, max_length=2000)


class TokenGrantDecision(Contract):
    request: TokenGrantRequest
    revision: Annotated[int, Field(ge=1)]
    previous_total: Annotated[int, Field(ge=1)]
    operator: str
    message_id: str
    observed: float


def _latest(
    db: sqlite3.Connection, project: str, session: str, work: str
) -> TokenGrantDecision | None:
    row = db.execute(
        "SELECT detail FROM research_observations WHERE project=? AND session=? AND work=? "
        "AND kind='token_grant' ORDER BY CAST(identity AS INTEGER) DESC LIMIT 1",
        (project, session, work),
    ).fetchone()
    return TokenGrantDecision.model_validate_json(row[0]) if row else None


class TokenGrants:
    def __init__(
        self, control: ControlStore | DocumentStore, *, clock: Clock = SYSTEM_CLOCK
    ) -> None:
        self.control = control.control if isinstance(control, DocumentStore) else control
        self.clock = clock

    def total(self, project: str, session: str, work: str, original: int) -> int:
        """The cumulative ceiling follows the work through all later attempts."""
        with self.control.read() as tx:
            last = _latest(tx.sql, project, session, work)
        return original if last is None else last.request.total_tokens

    def extended(self, project: str, session: str, work: str) -> bool:
        """Whether this work must retain its spending cap on every paid safe point."""
        with self.control.read() as tx:
            return _latest(tx.sql, project, session, work) is not None

    def renew(
        self, project: str, session: str, request: TokenGrantRequest, *, operator: str
    ) -> TokenGrantDecision:
        """Record one idempotent USER override before a root-level Resume is queued."""
        request = TokenGrantRequest.model_validate(request.model_dump())
        if not request.reason.strip() or not operator or len(operator) > 128:
            raise ControlError(INVALID_REQUEST, "a reason and authenticated operator are required")
        now = self.clock.now().timestamp()
        with self.control.write() as tx:
            db = tx.sql
            found = db.execute(
                "SELECT w.root,w.parent,w.current,w.state,w.reason,s.epoch "
                "FROM research_work w JOIN research_sessions s USING(project,session) "
                "WHERE w.project=? AND w.session=? AND w.work=?",
                (project, session, request.work),
            ).fetchone()
            if found is None:
                raise ControlError(NOT_FOUND, "research work was not found in this scope")
            root, parent, current, state, reason, epoch = found
            if parent != root:
                raise ControlError(
                    CONFLICT, "USER token overrides support direct root children only"
                )
            revision = request.expected_revision + 1
            existing = db.execute(
                "SELECT detail FROM research_observations WHERE project=? AND session=? "
                "AND work=? AND kind='token_grant' AND identity=?",
                (project, session, request.work, str(revision)),
            ).fetchone()
            if existing is not None:
                decision = TokenGrantDecision.model_validate_json(existing[0])
                if decision.request != request or decision.operator != operator:
                    raise ControlError(CONFLICT, "this grant revision has another USER decision")
                return decision
            if current != request.current or epoch != request.epoch:
                raise ControlError(CONFLICT, "research attempt or epoch changed")
            outcome = db.execute(
                "SELECT outcome FROM research_attempts WHERE project=? AND session=? "
                "AND work=? AND run=?",
                (project, session, request.work, request.current),
            ).fetchone()
            limited = reason == "run_budget" or (outcome is not None and outcome[0] == "run_budget")
            if state not in {"paused", "blocked"} or not limited:
                raise ControlError(
                    CONFLICT, "only a child paused by its token grant can be renewed"
                )
            if db.execute(
                "SELECT 1 FROM run_leases WHERE project_id=? AND session_id=? AND renewed_at>? "
                "UNION ALL SELECT 1 FROM research_work WHERE project=? AND session=? "
                "AND state='running' UNION ALL SELECT 1 FROM research_resumes "
                "WHERE project=? AND session=? AND status IN ('queued','running') LIMIT 1",
                (project, session, now - LEASE_SECONDS, project, session, project, session),
            ).fetchone():
                raise ControlError(CONFLICT, "research is active or Resume is pending")
            row = db.execute(
                "SELECT json_extract(b.snapshot,'$.request.tokens'),d.kind "
                "FROM research_blueprints b JOIN delegations d ON d.project_id=b.project "
                "AND d.session_id=b.session AND d.run=b.work "
                "WHERE b.project=? AND b.session=? AND b.work=?",
                (project, session, request.work),
            ).fetchone()
            if row is None or not isinstance(row[0], int) or row[1] not in {"check", "task"}:
                raise ControlError(CONFLICT, "original delegated token grant is unavailable")
            original, kind = row
            latest = _latest(db, project, session, request.work)
            prior_revision = 0 if latest is None else latest.revision
            previous = original if latest is None else latest.request.total_tokens
            if prior_revision != request.expected_revision or request.total_tokens <= previous:
                raise ControlError(CONFLICT, "token grant revision changed or did not increase")
            limits = settings_in(db, project)
            cap = limits.check_tokens if kind == "check" else limits.task_tokens
            if request.total_tokens > cap:
                raise ControlError(
                    INVALID_REQUEST, "token grant exceeds the configured child ceiling"
                )
            if (
                db.execute(
                    "SELECT 1 FROM project_cost_limits WHERE project_id=? AND daily_cost>0",
                    (project,),
                ).fetchone()
                is None
            ):
                raise ControlError(CONFLICT, "USER token override requires a project spending cap")
            text = (
                f"USER recovery token grant revision {revision}: cumulative ceiling "
                f"{request.total_tokens} tokens (previous {previous}). Reason: {request.reason} "
                "This does not authorize spending beyond the project's cost limit."
            )
            current_parent = db.execute(
                "SELECT parent FROM delegations WHERE project_id=? AND session_id=? AND run=?",
                (project, session, request.current),
            ).fetchone()
            if current_parent is None:
                raise ControlError(CONFLICT, "current child attempt has no recorded parent")
            archived = Archive(self.control, clock=self.clock).append(
                project,
                session,
                request.current,
                Entry("user", "notice", text, source="operator_token_grant"),
                parent=current_parent[0],
                transaction=tx,
            )
            decision = TokenGrantDecision(
                request=request,
                revision=revision,
                previous_total=previous,
                operator=operator,
                message_id=archived.message_id,
                observed=now,
            )
            detail = decision.model_dump_json()
            if len(detail.encode()) > 16384:
                raise ControlError(INVALID_REQUEST, "token grant decision exceeds 16 KiB")
            db.execute(
                "INSERT INTO research_observations VALUES (?,?,?,'token_grant',?,?,?)",
                (project, session, request.work, str(revision), now, detail),
            )
            return decision
