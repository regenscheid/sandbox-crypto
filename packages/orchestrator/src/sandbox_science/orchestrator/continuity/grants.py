# SPDX-License-Identifier: MIT
"""Audited USER token extensions for paused delegated work in the research tree."""

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

TOKEN_INCREMENT = 10_000_000

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


class TokenGrantStatus(Contract):
    work: RunName
    current: RunName
    epoch: Revision
    revision: int
    total_tokens: int
    used_tokens: int
    next_total_tokens: int | None
    blocked_reason: str | None
    blocked_ancestor_work: RunName | None = None


def _blocked_ancestor(db: sqlite3.Connection, project: str, session: str, work: str) -> str | None:
    from .token_usage import spent_tokens

    increase = TOKEN_INCREMENT
    while True:
        row = db.execute(
            "SELECT p.work,json_extract(b.snapshot,'$.request.tokens') FROM research_work w "
            "JOIN research_work p ON p.project=w.project AND p.session=w.session AND "
            "p.work=w.parent "
            "LEFT JOIN research_blueprints b ON b.project=p.project AND b.session=p.session "
            "AND b.work=p.work "
            "WHERE w.project=? AND w.session=? AND w.work=?",
            (project, session, work),
        ).fetchone()
        if row is None or row[1] is None:
            return None
        parent, original = row
        committed = spent_tokens(db, project, session, parent)
        for child, initial, state in db.execute(
            "SELECT w.work,json_extract(b.snapshot,'$.request.tokens'),w.state FROM "
            "research_work w "
            "LEFT JOIN research_blueprints b USING(project,session,work) "
            "WHERE w.project=? AND w.session=? AND w.parent=?",
            (project, session, parent),
        ).fetchall():
            used = spent_tokens(db, project, session, child, subtree=True)
            committed += (
                max(used, grant_total(db, project, session, child, initial or 0))
                if state not in ("completed", "stopped")
                else used
            )
        if committed + increase > grant_total(db, project, session, parent, original):
            return str(parent)
        # The immediate parent contains this increase inside its existing grant.
        # Its ancestors retain the same commitment; verify, but do not add it twice.
        increase = 0
        work = parent


def _latest(
    db: sqlite3.Connection, project: str, session: str, work: str
) -> TokenGrantDecision | None:
    row = db.execute(
        "SELECT detail FROM research_observations WHERE project=? AND session=? AND work=? "
        "AND kind='token_grant' ORDER BY CAST(identity AS INTEGER) DESC LIMIT 1",
        (project, session, work),
    ).fetchone()
    return TokenGrantDecision.model_validate_json(row[0]) if row else None


def grant_total(
    db: sqlite3.Connection, project: str, session: str, work: str, original: int
) -> int:
    """Original or USER-renewed cumulative ceiling within the caller's transaction."""
    last = _latest(db, project, session, work)
    return original if last is None else last.request.total_tokens


class TokenGrants:
    def __init__(
        self, control: ControlStore | DocumentStore, *, clock: Clock = SYSTEM_CLOCK
    ) -> None:
        self.control = control.control if isinstance(control, DocumentStore) else control
        self.clock = clock

    def total(self, project: str, session: str, work: str, original: int) -> int:
        """The cumulative ceiling follows the work through all later attempts."""
        with self.control.read() as tx:
            return grant_total(tx.sql, project, session, work, original)

    def extended(self, project: str, session: str, work: str) -> bool:
        """Whether this work must retain its spending cap on every paid safe point."""
        with self.control.read() as tx:
            return _latest(tx.sql, project, session, work) is not None

    def status(self, project: str, session: str, work: str) -> TokenGrantStatus:
        """Current grant and exact next USER increment, including admission blockers."""
        from .token_usage import spent_tokens

        with self.control.read() as tx:
            db = tx.sql
            row = db.execute(
                "SELECT "
                "w.current,s.epoch,w.state,w.reason,w.parent,"
                "json_extract(b.snapshot,'$.request.tokens') "
                "FROM research_work w JOIN research_sessions s USING(project,session) "
                "JOIN research_blueprints b USING(project,session,work) WHERE w.project=? "
                "AND w.session=? AND w.work=?",
                (project, session, work),
            ).fetchone()
            if row is None or row[4] is None or not isinstance(row[5], int):
                raise ControlError(NOT_FOUND, "delegated token grant was not found")
            current, epoch, state, reason, _, original = row
            latest = _latest(db, project, session, work)
            total = grant_total(db, project, session, work, original)
            used = spent_tokens(db, project, session, work)
            blocker = self._next_blocker(db, project, session, work, state, reason, total)
            return TokenGrantStatus(
                work=work,
                current=current,
                epoch=epoch,
                revision=latest.revision if latest else 0,
                total_tokens=total,
                used_tokens=used,
                next_total_tokens=total + TOKEN_INCREMENT if blocker is None else None,
                blocked_reason=blocker[0] if blocker else None,
                blocked_ancestor_work=blocker[1] if blocker else None,
            )

    def _next_blocker(
        self,
        db: sqlite3.Connection,
        project: str,
        session: str,
        work: str,
        state: str,
        reason: str | None,
        total: int,
    ) -> tuple[str, str | None] | None:
        if state not in ("paused", "blocked"):
            return "Wait until the research tree pauses before approving a token extension", None
        if db.execute(
            "SELECT 1 FROM run_leases WHERE project_id=? AND session_id=? AND renewed_at>? "
            "UNION ALL SELECT 1 FROM research_work WHERE project=? AND session=? AND "
            "state='running' "
            "UNION ALL SELECT 1 FROM research_resumes WHERE project=? AND session=? AND "
            "status IN ('queued','running') LIMIT 1",
            (
                project,
                session,
                self.clock.now().timestamp() - LEASE_SECONDS,
                project,
                session,
                project,
                session,
            ),
        ).fetchone():
            return "Research is active or Resume is pending; wait for its safe pause", None
        if total + TOKEN_INCREMENT > settings_in(db, project).max_token_grant:
            return "The next 10,000,000 tokens exceed the configured USER grant ceiling", None
        if not db.execute(
            "SELECT 1 FROM project_cost_limits WHERE project_id=? AND daily_cost>0", (project,)
        ).fetchone():
            return "A positive project spending cap is required", None
        ancestor = _blocked_ancestor(db, project, session, work)
        return (
            (f"Ancestor {ancestor} needs its own approved token extension first", ancestor)
            if ancestor is not None
            else None
        )

    def renew_next(
        self, project: str, session: str, request: TokenGrantRequest, *, operator: str
    ) -> TokenGrantDecision:
        """One authenticated USER approval authorizes exactly ten million more tokens."""
        return self._renew(project, session, request, operator=operator, incremental=True)

    def renew(
        self, project: str, session: str, request: TokenGrantRequest, *, operator: str
    ) -> TokenGrantDecision:
        """Legacy explicit operator override, retained for already scripted recovery."""
        return self._renew(project, session, request, operator=operator, incremental=False)

    def _renew(
        self,
        project: str,
        session: str,
        request: TokenGrantRequest,
        *,
        operator: str,
        incremental: bool,
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
            if parent is None or (not incremental and parent != root):
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
            if state not in {"paused", "blocked"} or (not incremental and not limited):
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
            cap = (
                limits.max_token_grant
                if incremental
                else (limits.check_tokens if kind == "check" else limits.task_tokens)
            )
            if incremental:
                if request.total_tokens != previous + TOKEN_INCREMENT:
                    raise ControlError(
                        INVALID_REQUEST, "USER approval covers exactly 10,000,000 additional tokens"
                    )
                blocker = self._next_blocker(
                    db, project, session, request.work, state, reason, previous
                )
                if blocker is not None:
                    raise ControlError(CONFLICT, blocker[0])
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
