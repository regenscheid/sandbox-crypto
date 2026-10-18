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
from sandbox_science.orchestrator.projects import RunName

from .grants import grant_total
from .records import Ownership, State, check_owner
from .token_usage import spent_tokens

Resource = Literal["tokens", "turns"]
Action = Literal["continue", "redirect", "report", "pause", "extend_turns"]


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
    row = db.execute(
        "SELECT detail FROM research_observations WHERE project=? AND session=? AND work=? "
        "AND kind='resource_decision' AND json_extract(detail,'$.request.action')='extend_turns' "
        "ORDER BY json_extract(detail,'$.total') DESC LIMIT 1",
        (owner.project, owner.session, owner.work),
    ).fetchone()
    if row is None:
        return original, 0
    decision = ResourceDecision.model_validate_json(row[0])
    return decision.total, decision.total


class ResourceRequests:
    """One warning per cumulative grant, durable across process loss and Resume."""

    def __init__(self, control: ControlStore, *, clock: Clock = SYSTEM_CLOCK):
        self.control, self.clock = control, clock

    def observe(
        self,
        owner: Ownership,
        resource: Resource,
        *,
        used: int,
        total: int,
        revision: int,
        force: bool = False,
    ) -> ResourceRequest | None:
        if total <= 0 or (not force and used * 5 < total * 4):
            return None
        identity = f"{resource}-{revision}-{owner.run}" + ("-exhausted" if force else "")
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
                            item.resource == "turns"
                            and tx.sql.execute(
                                "SELECT 1 FROM research_observations WHERE project=? "
                                "AND session=? AND work=? AND kind='resource_decision' "
                                "AND json_extract(detail,'$.request.action')='extend_turns' "
                                "AND json_extract(detail,'$.total')>? LIMIT 1",
                                (project, session, item.work, item.total),
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
                "AND json_extract(o.detail,'$.resource')='turns' "
                "AND json_extract(t.detail,'$.request.action')='extend_turns' "
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
        """Only the current direct parent decides; an agent never increases tokens."""
        if not request.reason.strip():
            raise ControlError(INVALID_REQUEST, "a progress-based reason is required")
        with self.control.write() as tx:
            db = tx.sql
            check_owner(db, owner)
            row = db.execute(
                "SELECT o.work,o.detail,w.parent,w.current,w.state FROM research_observations o "
                "JOIN research_work w USING(project,session,work) WHERE o.project=? AND "
                "o.session=? "
                "AND o.kind='resource_request' AND json_extract(o.detail,'$.request_id')=?",
                (owner.project, owner.session, request.request_id),
            ).fetchone()
            if row is None:
                raise ControlError(NOT_FOUND, "resource request was not found")
            work, detail, parent, current, state = row
            warned = ResourceRequest.model_validate_json(detail)
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
                ):
                    raise ControlError(CONFLICT, "resource request already has another decision")
                return prior
            if parent != owner.work or current != warned.current or owner.epoch != warned.epoch:
                raise ControlError(CONFLICT, "resource request parent, attempt or epoch changed")
            if state not in ("running", "waiting", "paused", "blocked"):
                raise ControlError(CONFLICT, "resource recipient already ended")
            total = warned.total
            latest = db.execute(
                "SELECT max(json_extract(detail,'$.total')) FROM research_observations WHERE "
                "project=? "
                "AND session=? AND work=? AND kind='resource_decision' "
                "AND json_extract(detail,'$.request.action')='extend_turns'",
                (owner.project, owner.session, work),
            ).fetchone()[0]
            if warned.resource == "turns" and latest is not None and latest != total:
                raise ControlError(CONFLICT, "resource grant revision changed")
            if request.action == "extend_turns":
                if warned.resource != "turns":
                    raise ControlError(INVALID_REQUEST, "only turn allowances are parent-renewable")
                limits = settings_in(db, owner.project)
                if total >= limits.turn_ceiling:
                    raise ControlError(CONFLICT, "configured cumulative turn ceiling reached")
                total = min(total + 300, limits.turn_ceiling)
            decision = ResourceDecision(
                request=request,
                parent_run=owner.run,
                operation=operation,
                total=total,
                observed=self.clock.now().timestamp(),
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
                "ORDER BY d.observed DESC LIMIT 4",
                (owner.project, owner.session, owner.work, owner.run, owner.epoch),
            ).fetchall()
        return [ResourceDecision.model_validate_json(row[0]) for row in rows]
