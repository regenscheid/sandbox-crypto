# SPDX-License-Identifier: MIT
"""Durable research check-ins and parent decisions, separate from hard budgets."""

from __future__ import annotations

import json
import math
import sqlite3
from typing import Literal

from pydantic import Field

from sandbox_science.contracts import SYSTEM_CLOCK, Clock, Contract, new_id
from sandbox_science.orchestrator.control import ControlError, ControlStore
from sandbox_science.orchestrator.control.errors import CONFLICT, INVALID_REQUEST, NOT_FOUND
from sandbox_science.orchestrator.delegation.records import settings_in
from sandbox_science.orchestrator.documents import DocumentStore
from sandbox_science.orchestrator.projects import RunName

from .grants import grant_total
from .records import Ownership, State, check_owner, tree_cancelled
from .resource_policy import (
    EXTENSIONS,
    ceiling,
    extension,
    initial_allowance,
    soft_grant,
)
from .resource_policy import (
    Action as Action,
)
from .resource_policy import (
    Resource as Resource,
)
from .resource_usage import research_progress, retained_bytes, sample_elapsed
from .token_usage import spent_tokens


class ResourceDecisionRequest(Contract):
    request_id: str = Field(min_length=1, max_length=128)
    action: Action
    reason: str = Field(min_length=1, max_length=2000)


class ResourceDecision(Contract):
    request: ResourceDecisionRequest
    parent_run: RunName
    operation: str
    total: int
    observed: float
    operator: str | None = None
    progress: int = 0


class ResourceApprovalRequest(ResourceDecisionRequest):
    """The USER confirms the exact paused attempt and allowance shown in the UI."""

    current: RunName
    epoch: int = Field(ge=1)
    total: int = Field(ge=1)


class ResourceRequest(Contract):
    request_id: str
    work: RunName
    current: RunName
    epoch: int
    parent: RunName | None
    resource: Resource
    grant_revision: int
    used: int
    total: int
    checkpoint_message_id: str | None
    created_at: float
    task: str = ""
    work_state: State = "running"
    superseded: bool = False
    user_grant_revision: int | None = None
    decision: ResourceDecision | None = None
    scope: Literal["work", "project", "global"] = "work"
    renewable: bool = True


class ResourceRequestPage(Contract):
    requests: tuple[ResourceRequest, ...] = Field(max_length=100)
    next_after: str | None = None


def turn_usage(db: sqlite3.Connection, owner: Ownership, *, previous: bool = False) -> int:
    """Foreground calls across attempts; unknown dispatches count, folds do not.

    A callback's canonical intent and its archive observation represent one call.
    Legacy/synthetic attempts without intents use their observed response count.
    """
    row = db.execute(
        "SELECT coalesce(sum(max((SELECT count(*) FROM research_model_calls c "
        "WHERE c.project=a.project AND c.session=a.session AND c.run=a.run "
        "AND c.kind IN ('main','delegated')), (SELECT count(*) FROM archive_model_usage u "
        "WHERE u.project=a.project AND u.session=a.session AND u.run=a.run))),0) "
        "FROM research_attempts a WHERE a.project=? AND a.session=? AND a.work=? "
        "AND (?=0 OR a.run!=?)",
        (owner.project, owner.session, owner.work, previous, owner.run),
    ).fetchone()
    return int(row[0])


def turn_grant(db: sqlite3.Connection, owner: Ownership, original: int) -> tuple[int, int]:
    return soft_grant(db, owner, "turns", original)


class ResourceRequests:
    """One warning per cumulative grant, durable across process loss and Resume."""

    def __init__(self, control: ControlStore | DocumentStore, *, clock: Clock = SYSTEM_CLOCK):
        self.control = control.control if isinstance(control, DocumentStore) else control
        self.clock = clock

    def observe(
        self,
        owner: Ownership,
        resource: Resource,
        *,
        used: int,
        total: int,
        revision: int,
        force: bool = False,
        scope: Literal["work", "project", "global"] = "work",
        renewable: bool = True,
    ) -> ResourceRequest | None:
        if total <= 0 or (not force and used * 5 < total * 4):
            return None
        identity = f"{resource}-{scope}-{revision}-{owner.run}" + ("-exhausted" if force else "")
        with self.control.write() as tx:
            db = tx.sql
            check_owner(db, owner)
            row = db.execute(
                "SELECT detail FROM research_observations WHERE project=? AND session=? "
                "AND work=? AND kind='resource_request' AND identity=?",
                (owner.project, owner.session, owner.work, identity),
            ).fetchone()
            if row is not None:
                return ResourceRequest.model_validate_json(row[0])
            state = db.execute(
                "SELECT parent,state FROM research_work WHERE project=? AND session=? AND work=?",
                (owner.project, owner.session, owner.work),
            ).fetchone()
            if state is None or state[1] not in ("running", "waiting"):
                return None
            last = db.execute(
                "SELECT message_id FROM archive_messages WHERE project=? AND session=? AND run=? "
                "ORDER BY position DESC LIMIT 1",
                (owner.project, owner.session, owner.run),
            ).fetchone()
            brief = db.execute(
                "SELECT json_extract(snapshot,'$.request.task') FROM research_blueprints "
                "WHERE project=? AND session=? AND work=?",
                (owner.project, owner.session, owner.work),
            ).fetchone()
            request = ResourceRequest(
                request_id=new_id("resource"),
                work=owner.work,
                current=owner.run,
                epoch=owner.epoch,
                parent=state[0],
                resource=resource,
                grant_revision=revision,
                used=used,
                total=total,
                checkpoint_message_id=last[0] if last else None,
                created_at=self.clock.now().timestamp(),
                task=str(brief[0] or "")[:300] if brief else "",
                scope=scope,
                renewable=renewable,
            )
            db.execute(
                "INSERT INTO research_observations VALUES (?,?,?,'resource_request',?,?,?)",
                (
                    owner.project,
                    owner.session,
                    owner.work,
                    identity,
                    request.created_at,
                    request.model_dump_json(),
                ),
            )
            return request

    def page(self, project: str, session: str, *, after: str = "") -> ResourceRequestPage:
        """Latest first; the opaque cursor retains timestamp and ID together."""
        stamp, cursor = float("inf"), ""
        if after:
            try:
                raw, cursor = after.split("|", 1)
                stamp = float(raw)
                if not math.isfinite(stamp) or not cursor or len(cursor) > 128:
                    raise ValueError
            except ValueError:
                raise ControlError(INVALID_REQUEST, "invalid resource page cursor") from None
        with self.control.read() as tx:
            rows = tx.sql.execute(
                "SELECT r.detail,d.detail,w.state,w.current FROM research_observations r "
                "JOIN research_work w USING(project,session,work) LEFT JOIN "
                "research_observations d ON d.project=r.project AND d.session=r.session "
                "AND d.work=r.work AND d.kind='resource_decision' "
                "AND d.identity=json_extract(r.detail,'$.request_id') "
                "WHERE r.project=? AND r.session=? AND r.kind='resource_request' "
                "AND (r.observed,json_extract(r.detail,'$.request_id'))<(?,?) "
                "ORDER BY r.observed DESC,json_extract(r.detail,'$.request_id') DESC LIMIT 101",
                (project, session, stamp, cursor),
            ).fetchall()
        items = tuple(
            ResourceRequest.model_validate_json(r).model_copy(
                update={
                    "decision": ResourceDecision.model_validate_json(d) if d else None,
                    "work_state": state,
                    "superseded": ResourceRequest.model_validate_json(r).current != current,
                }
            )
            for r, d, state, current in rows[:100]
        )
        with self.control.read() as tx:
            items = tuple(
                item.model_copy(
                    update={
                        "user_grant_revision": self._resolved(tx.sql, project, session, item),
                        "superseded": item.superseded
                        or (
                            extension(item.resource) is not None
                            and tx.sql.execute(
                                "SELECT 1 FROM research_observations WHERE project=? "
                                "AND session=? AND work=? AND kind='resource_decision' "
                                "AND json_extract(detail,'$.request.action')=? "
                                "AND json_extract(detail,'$.total')>? LIMIT 1",
                                (project, session, item.work, extension(item.resource), item.total),
                            ).fetchone()
                            is not None
                        ),
                    }
                )
                for item in items
            )
        return ResourceRequestPage(
            requests=items,
            next_after=f"{items[-1].created_at}|{items[-1].request_id}"
            if len(rows) > 100
            else None,
        )

    @staticmethod
    def _resolved(
        db: sqlite3.Connection, project: str, session: str, item: ResourceRequest
    ) -> int | None:
        if item.resource != "tokens":
            return None
        row = db.execute(
            "SELECT max(CAST(identity AS INTEGER)) FROM research_observations WHERE project=? "
            "AND session=? AND work=? AND kind='token_grant' "
            "AND json_extract(detail,'$.request.total_tokens')>?",
            (project, session, item.work, item.total),
        ).fetchone()
        return int(row[0]) if row and row[0] is not None else None

    def pending(
        self,
        owner: Ownership,
        *,
        seen: frozenset[str] = frozenset(),
        children_only: bool = False,
    ) -> list[ResourceRequest]:
        with self.control.read() as tx:
            rows = tx.sql.execute(
                "SELECT o.detail FROM research_observations o JOIN research_work w "
                "USING(project,session,work) WHERE o.project=? AND o.session=? "
                "AND o.kind='resource_request' AND w.state NOT IN ('completed','stopped') "
                "AND (w.parent=? OR w.work=?) AND json_extract(o.detail,'$.current')=w.current "
                "AND (?=0 OR w.parent=?) "
                "AND json_extract(o.detail,'$.request_id') NOT IN (SELECT value FROM json_each(?)) "
                "AND NOT EXISTS (SELECT 1 FROM research_observations d WHERE d.project=o.project "
                "AND d.session=o.session AND d.work=o.work AND d.kind='resource_decision' "
                "AND d.identity=json_extract(o.detail,'$.request_id')) "
                "AND NOT EXISTS (SELECT 1 FROM research_observations t WHERE t.project=o.project "
                "AND t.session=o.session AND t.work=o.work AND t.kind='resource_decision' "
                "AND json_extract(t.detail,'$.request.action')=CASE "
                "json_extract(o.detail,'$.resource') WHEN 'turns' THEN 'extend_turns' "
                "WHEN 'elapsed_seconds' THEN 'extend_time' WHEN 'retained_bytes' "
                "THEN 'extend_storage' END "
                "AND json_extract(t.detail,'$.total')>json_extract(o.detail,'$.total')) "
                "AND NOT EXISTS (SELECT 1 FROM research_observations g WHERE g.project=o.project "
                "AND g.session=o.session AND g.work=o.work AND g.kind='token_grant' "
                "AND json_extract(o.detail,'$.resource')='tokens' "
                "AND json_extract(g.detail,'$.request.total_tokens')>"
                "json_extract(o.detail,'$.total')) "
                "ORDER BY o.observed LIMIT 4",
                (
                    owner.project,
                    owner.session,
                    owner.work,
                    owner.work,
                    children_only,
                    owner.work,
                    json.dumps(sorted(seen)),
                ),
            ).fetchall()
        return [ResourceRequest.model_validate_json(row[0]) for row in rows]

    def decide(
        self, owner: Ownership, request: ResourceDecisionRequest, *, operation: str
    ) -> ResourceDecision:
        """A direct parent decides, or an opted-in root renews with saved progress."""
        with self.control.write() as tx:
            return self._decide(tx.sql, owner, request, operation=operation)

    def approve(
        self, project: str, session: str, request: ResourceApprovalRequest, *, operator: str
    ) -> ResourceDecision:
        """Explicit USER extension of paused work; never dispatches or resumes it."""
        if not operator.strip() or request.action not in EXTENSIONS:
            raise ControlError(INVALID_REQUEST, "USER approval must name a soft extension")
        with self.control.write() as tx:
            db = tx.sql
            root = db.execute(
                "SELECT w.work,w.current,s.epoch FROM research_sessions s "
                "JOIN research_work w ON w.project=s.project AND w.session=s.session "
                "AND w.work=s.root WHERE s.project=? AND s.session=?",
                (project, session),
            ).fetchone()
            if root is None:
                raise ControlError(NOT_FOUND, "research session was not found")
            if db.execute(
                "SELECT 1 FROM run_leases WHERE project_id=? AND session_id=? UNION ALL "
                "SELECT 1 FROM research_resumes WHERE project=? AND session=? "
                "AND status IN ('queued','running') UNION ALL SELECT 1 FROM research_work "
                "WHERE project=? AND session=? AND state IN ('running','waiting') LIMIT 1",
                (project, session, project, session, project, session),
            ).fetchone():
                raise ControlError(CONFLICT, "wait for the active research tree before approving")
            owner = Ownership(
                project=project, session=session, work=root[0], run=root[1], epoch=root[2]
            )
            return self._decide(
                db,
                owner,
                ResourceDecisionRequest(
                    request_id=request.request_id, action=request.action, reason=request.reason
                ),
                operation="user-resource:" + request.request_id,
                operator=operator,
                approval=request,
            )

    def _decide(
        self,
        db: sqlite3.Connection,
        owner: Ownership,
        request: ResourceDecisionRequest,
        *,
        operation: str,
        operator: str | None = None,
        approval: ResourceApprovalRequest | None = None,
    ) -> ResourceDecision:
        if not request.reason.strip():
            raise ControlError(INVALID_REQUEST, "a progress-based reason is required")
        check_owner(db, owner)
        row = db.execute(
            "SELECT o.work,o.detail,w.parent,w.current,w.state,w.root FROM research_observations o "
            "JOIN research_work w USING(project,session,work) WHERE o.project=? AND "
            "o.session=? AND o.kind='resource_request' AND json_extract(o.detail,'$.request_id')=?",
            (owner.project, owner.session, request.request_id),
        ).fetchone()
        if row is None:
            raise ControlError(NOT_FOUND, "resource request was not found")
        work, detail, parent, current, state, root = row
        warned = ResourceRequest.model_validate_json(detail)
        if approval is not None and (
            approval.current != warned.current
            or approval.epoch != warned.epoch
            or approval.total != warned.total
        ):
            raise ControlError(CONFLICT, "paused resource attempt or allowance changed")
        existing = db.execute(
            "SELECT detail FROM research_observations WHERE project=? AND session=? AND work=? "
            "AND kind='resource_decision' AND identity=?",
            (owner.project, owner.session, work, request.request_id),
        ).fetchone()
        if existing:
            prior = ResourceDecision.model_validate_json(existing[0])
            if (
                prior.request != request
                or prior.operation != operation
                or prior.parent_run != owner.run
                or prior.operator != operator
            ):
                raise ControlError(CONFLICT, "resource request already has another decision")
            return prior
        self_renewal = parent is None and work == owner.work and operator is None
        if (
            (parent != owner.work and not self_renewal and operator is None)
            or current != warned.current
            or owner.epoch != warned.epoch
        ):
            raise ControlError(CONFLICT, "resource request parent, attempt or epoch changed")
        if state not in ("running", "waiting", "paused", "blocked") or tree_cancelled(
            db, owner.project, owner.session, root
        ):
            raise ControlError(CONFLICT, "resource recipient already ended or was stopped")
        if approval is not None and (
            state not in ("paused", "blocked")
            or approval.current != current
            or approval.epoch != warned.epoch
            or approval.total != warned.total
        ):
            raise ControlError(CONFLICT, "paused resource attempt or allowance changed")
        total, progress = warned.total, 0
        target = Ownership(
            project=owner.project, session=owner.session, work=work, run=current, epoch=owner.epoch
        )
        _, latest = soft_grant(db, target, warned.resource, total)
        if latest and latest != total:
            raise ControlError(CONFLICT, "resource grant revision changed")
        limits = settings_in(db, owner.project)
        if self_renewal:
            if not limits.root_resource_renewal or request.action not in EXTENSIONS:
                raise ControlError(CONFLICT, "root self-renewal is not enabled for this action")
            progress = research_progress(db, owner)
            prior_progress = db.execute(
                "SELECT coalesce(max(json_extract(detail,'$.progress')),0) "
                "FROM research_observations "
                "WHERE project=? AND session=? AND work=? AND kind='resource_decision' "
                "AND json_extract(detail,'$.request.action')=? "
                "AND json_extract(detail,'$.operator') IS NULL",
                (owner.project, owner.session, owner.work, request.action),
            ).fetchone()[0]
            baseline = db.execute(
                "SELECT detail FROM research_observations WHERE project=? AND session=? "
                "AND work=? AND kind='resource_progress' AND identity='initial'",
                (owner.project, owner.session, owner.work),
            ).fetchone()
            prior_progress = max(prior_progress, int(baseline[0]) if baseline else 0)
            if warned.checkpoint_message_id is None or progress <= prior_progress:
                raise ControlError(
                    CONFLICT, "save a checkpoint and new research evidence before self-renewal"
                )
        if request.action in EXTENSIONS:
            kind, increment, _ = EXTENSIONS[request.action]
            if warned.resource != kind or not warned.renewable:
                raise ControlError(INVALID_REQUEST, "this resource is not renewable by that action")
            if (kind == "elapsed_seconds" and limits.research_seconds is None) or (
                kind == "retained_bytes" and limits.storage_bytes is None
            ):
                raise ControlError(CONFLICT, "the optional resource allowance is disabled")
            maximum = ceiling(limits, request.action)
            if total >= maximum:
                raise ControlError(CONFLICT, "configured cumulative resource ceiling reached")
            total = min(total + increment, maximum)
        decision = ResourceDecision(
            request=request,
            parent_run=owner.run,
            operation=operation,
            total=total,
            observed=self.clock.now().timestamp(),
            operator=operator,
            progress=progress,
        )
        db.execute(
            "INSERT INTO research_observations VALUES (?,?,?,'resource_decision',?,?,?)",
            (
                owner.project,
                owner.session,
                work,
                request.request_id,
                decision.observed,
                decision.model_dump_json(),
            ),
        )
        return decision

    def sample(
        self, owner: Ownership, *, seconds: int
    ) -> tuple[tuple[Resource, int, int, int], ...]:
        """Observe active time and optional logical retained-byte allowances safely."""
        with self.control.write() as tx:
            db = tx.sql
            elapsed = sample_elapsed(db, owner, seconds, self.clock.now().timestamp())
            settings = settings_in(db, owner.project)
            if not db.execute(
                "SELECT 1 FROM research_observations WHERE project=? AND session=? "
                "AND work=? AND kind='resource_progress' AND identity='initial'",
                (owner.project, owner.session, owner.work),
            ).fetchone():
                db.execute(
                    "INSERT INTO research_observations "
                    "VALUES (?,?,?,'resource_progress','initial',?,?)",
                    (
                        owner.project,
                        owner.session,
                        owner.work,
                        self.clock.now().timestamp(),
                        str(research_progress(db, owner)),
                    ),
                )
            samples: list[tuple[Resource, int, int, int]] = []
            for kind, configured in (
                ("elapsed_seconds", settings.research_seconds),
                ("retained_bytes", settings.storage_bytes),
            ):
                if configured is not None:
                    original = initial_allowance(db, owner, kind, configured)
                    total, revision = soft_grant(db, owner, kind, original)
                    used = elapsed if kind == "elapsed_seconds" else retained_bytes(db, owner)
                    samples.append(
                        (
                            "elapsed_seconds" if kind == "elapsed_seconds" else "retained_bytes",
                            used,
                            total,
                            revision,
                        )
                    )
            return tuple(samples)

    def root_enabled(self, owner: Ownership) -> bool:
        with self.control.read() as tx:
            return settings_in(tx.sql, owner.project).root_resource_renewal

    def limits(
        self, owner: Ownership, *, turns: int, tokens: int | None
    ) -> tuple[int, int, int, int, int]:
        """Cumulative grants, revisions and prior-attempt turns for one current owner."""
        with self.control.read() as tx:
            db = tx.sql
            original = db.execute(
                "SELECT json_extract(snapshot,'$.request.tokens'),"
                "json_extract(snapshot,'$.request.max_turns') FROM research_blueprints "
                "WHERE project=? AND session=? AND work=?",
                (owner.project, owner.session, owner.work),
            ).fetchone()
            if original:
                tokens, turns = original[0] or tokens, original[1] or turns
            turn_total, turn_revision = turn_grant(db, owner, turns)
            token_total = grant_total(db, owner.project, owner.session, owner.work, tokens or 0)
            revision = db.execute(
                "SELECT count(*) FROM research_observations WHERE project=? AND session=? "
                "AND work=? "
                "AND kind='token_grant'",
                (owner.project, owner.session, owner.work),
            ).fetchone()[0]
            return (
                turn_total,
                turn_revision,
                token_total,
                int(revision),
                turn_usage(db, owner, previous=True),
            )

    def used_tokens(self, owner: Ownership) -> int:
        with self.control.read() as tx:
            return spent_tokens(tx.sql, owner.project, owner.session, owner.work)

    def decisions(self, owner: Ownership) -> list[ResourceDecision]:
        with self.control.read() as tx:
            rows = tx.sql.execute(
                "SELECT d.detail FROM research_observations d JOIN research_observations r "
                "ON r.project=d.project AND r.session=d.session AND r.work=d.work "
                "AND r.kind='resource_request' AND "
                "json_extract(r.detail,'$.request_id')=d.identity "
                "WHERE d.project=? AND d.session=? AND d.work=? AND d.kind='resource_decision' "
                "AND json_extract(r.detail,'$.current')=? AND json_extract(r.detail,'$.epoch')=? "
                "ORDER BY json_extract(d.detail,'$.request.action') IN ('pause','report') DESC, "
                "d.observed DESC LIMIT 4",
                (owner.project, owner.session, owner.work, owner.run, owner.epoch),
            ).fetchall()
        return [ResourceDecision.model_validate_json(row[0]) for row in rows]
