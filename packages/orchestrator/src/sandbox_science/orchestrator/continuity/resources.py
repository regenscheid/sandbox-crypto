# SPDX-License-Identifier: MIT
"""Durable research check-ins and parent decisions (D102), separate from hard limits.

A work's soft caps on tokens, turns, active time and cost, and the loop heuristics, ask
for a check-in: a request with a bounded state summary that its direct parent answers
(extend by one step, redirect, or stop), or that the root answers itself against its
plan, with a notice to the user. Nothing here enforces spending; LiteLLM's budget does.

The root's turn limit is the one soft cap the root does not decide for itself: reaching it
pauses the research and asks the user to continue for another block of the configured
size or to stop (``accept``, ``approve``).
"""

from __future__ import annotations

import json
import math
import sqlite3
from dataclasses import dataclass
from typing import Literal

from pydantic import Field

from sandbox_science.contracts import SYSTEM_CLOCK, Clock, Contract, new_id
from sandbox_science.orchestrator.control import ControlError, ControlStore
from sandbox_science.orchestrator.control.errors import CONFLICT, INVALID_REQUEST, NOT_FOUND
from sandbox_science.orchestrator.delegation.records import DelegationSettings, settings_in
from sandbox_science.orchestrator.documents import DocumentStore
from sandbox_science.orchestrator.projects import RunName

from .records import Ownership, State, check_owner, tree_cancelled
from .resource_policy import (
    CAPS,
    EXTENSIONS,
    GRANTED,
    STOPPING,
    ceiling,
    extension,
    idle_extensions,
    initial_allowance,
    soft_grant,
)
from .resource_policy import (
    Action as Action,
)
from .resource_policy import (
    Resource as Resource,
)
from .resource_usage import (
    produced,
    research_progress,
    retained_bytes,
    sample_elapsed,
    work_progress,
)
from .token_usage import spent_cost, spent_tokens


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
    """The work's evidence counter when decided, for the no-progress bound."""
    resource: Resource | None = None
    """The resource it decided; ``None`` in records made before D102."""
    automatic: bool = False
    """Made by the platform: the root's own check-in, or a parent that did not answer."""


class CheckInSummary(Contract):
    """The bounded state a check-in delivers: the brief, what the work produced and
    what it did last, with its recorded use (D102)."""

    brief: str = Field(default="", max_length=2000)
    produced: tuple[str, ...] = Field(default=(), max_length=12)
    recent: tuple[str, ...] = Field(default=(), max_length=10)
    """Its latest tool calls, oldest first, each its tool and whether it failed."""
    answer: str | None = Field(default=None, max_length=1000)
    """The head of its latest answer text."""
    rule: str | None = Field(default=None, max_length=40)
    """For a loop check-in, the heuristic: ``same_call``, ``same_error``, ``no_progress``
    or ``same_output``."""
    detail: str | None = Field(default=None, max_length=300)
    tokens: int = 0
    turns: int = 0
    seconds: int = 0
    cost: float = 0.0
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
    decision: ResourceDecision | None = None
    scope: Literal["work", "project", "global"] = "work"
    renewable: bool = True
    summary: CheckInSummary | None = None
    step: int = 0
    """For a root's turn limit, the block of turns a user's continue adds: the run's
    configured limit. Zero for every other request, whose step is the resource's own."""


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


@dataclass(frozen=True)
class SafePoint:
    """One safe point's soft caps and recorded use, read in one transaction."""

    caps: tuple[tuple[Resource, int, int], ...]
    """Each enabled soft cap: its resource, cumulative total and revision."""
    used: dict[str, int]
    """Recorded use by resource; turns before this attempt are ``prior_turns``."""
    prior_turns: int
    hard_turns: int
    """A child's hard turn limit across its attempts (``turn_ceiling``)."""
    turns: tuple[int, int] = (0, 0)
    """The cumulative turn allowance and its revision. A child's is a soft cap among
    ``caps``; a root's is its run's ``max_turns``, a soft cap too: at it the user is asked
    (D102), and an accepted block raises it."""
    progress: int = 0
    storage: tuple[tuple[Resource, int, int, int], ...] = ()
    """The optional retained-artifact allowance: resource, used, total, revision."""
    turn_block: int = 0
    """A root's pinned turn limit, the block each accepted continuation adds; 0 for a child."""


SUPERSEDED = (
    "EXISTS (SELECT 1 FROM research_observations t WHERE t.project={o}.project "  # noqa: S608
    "AND t.session={o}.session AND t.work={o}.work AND t.kind='resource_decision' "
    "AND json_extract(t.detail,'$.request.action') NOT IN ('stop','report','pause') "
    "AND json_extract(t.detail,'$.total')>json_extract({o}.detail,'$.total') AND "
    + GRANTED.format(d="t.detail", r="json_extract({o}.detail,'$.resource')")
    + ")"
)
"""A fixed SQL condition: a later extension of the same resource superseded request
``{o}``."""


def _extended(limits: DelegationSettings, warned: ResourceRequest, action: str) -> int:
    """The request's cap after ``action``: one bounded step for an extension or a redirect
    of a soft cap, its total otherwise. A loop check-in's extension changes no cap."""
    resource, total = warned.resource, warned.total
    if action in STOPPING or action == "pause":
        return total
    extending = action == "extend" or action in EXTENSIONS
    if (action in EXTENSIONS and EXTENSIONS[action][0] != resource) or (
        extending and not warned.renewable
    ):
        raise ControlError(INVALID_REQUEST, "this resource is not renewable by that action")
    if resource == "turns" and warned.step > 0 and warned.parent is None and extending:
        # The root's turn limit: the user accepts another block of the run's own size. No
        # ceiling bounds it; the user is asked again at the end of every block.
        return total + warned.step
    if action == "continue":
        if resource in CAPS and warned.used >= total:
            raise ControlError(INVALID_REQUEST, "a reached soft cap needs extend, redirect or stop")
        return total
    named = extension(resource)
    if named is None or not warned.renewable:
        if extending and resource != "loop":
            raise ControlError(INVALID_REQUEST, "this resource is not renewable by that action")
        return total  # a redirect of hard capacity is guidance only
    if (resource == "elapsed_seconds" and limits.research_seconds is None) or (
        resource == "retained_bytes" and limits.storage_bytes is None
    ):
        raise ControlError(CONFLICT, "the optional resource allowance is disabled")
    _, step, _ = EXTENSIONS[named]
    maximum = ceiling(limits, named)
    if maximum is not None and total >= maximum:
        raise ControlError(
            CONFLICT, "configured cumulative resource ceiling reached: stop this work"
        )
    return total + step if maximum is None else min(total + step, maximum)


class ResourceRequests:
    """One check-in per cumulative cap, durable across process loss and Resume."""

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
        summary: CheckInSummary | None = None,
        step: int = 0,
    ) -> ResourceRequest | None:
        """Record one check-in per cumulative cap, attempt and revision; a capacity
        warning at 80%, and every soft cap only when reached (``force``)."""
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
                summary=summary,
                step=step,
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
                "SELECT r.detail,d.detail,w.state,w.current,"  # noqa: S608 - fixed SQL
                + SUPERSEDED.format(o="r")
                + " FROM research_observations r "
                "JOIN research_work w USING(project,session,work) LEFT JOIN "
                "research_observations d ON d.project=r.project AND d.session=r.session "
                "AND d.work=r.work AND d.kind='resource_decision' "
                "AND d.identity=json_extract(r.detail,'$.request_id') "
                "WHERE r.project=? AND r.session=? AND r.kind='resource_request' "
                "AND (r.observed,json_extract(r.detail,'$.request_id'))<(?,?) "
                "ORDER BY r.observed DESC,json_extract(r.detail,'$.request_id') DESC LIMIT 101",
                (project, session, stamp, cursor),
            ).fetchall()
        items: list[ResourceRequest] = []
        for r, d, state, current, superseded in rows[:100]:
            item = ResourceRequest.model_validate_json(r)
            items.append(
                item.model_copy(
                    update={
                        "decision": ResourceDecision.model_validate_json(d) if d else None,
                        "work_state": state,
                        "superseded": item.current != current or bool(superseded),
                    }
                )
            )
        return ResourceRequestPage(
            requests=tuple(items),
            next_after=f"{items[-1].created_at}|{items[-1].request_id}"
            if len(rows) > 100
            else None,
        )

    def pending(
        self,
        owner: Ownership,
        *,
        seen: frozenset[str] = frozenset(),
        children_only: bool = False,
    ) -> list[ResourceRequest]:
        with self.control.read() as tx:
            rows = tx.sql.execute(
                "SELECT o.detail FROM research_observations o JOIN research_work w "  # noqa: S608
                "USING(project,session,work) WHERE o.project=? AND o.session=? "
                "AND o.kind='resource_request' AND w.state NOT IN ('completed','stopped') "
                "AND (w.parent=? OR w.work=?) AND json_extract(o.detail,'$.current')=w.current "
                "AND (?=0 OR w.parent=?) "
                "AND json_extract(o.detail,'$.request_id') NOT IN (SELECT value FROM json_each(?)) "
                "AND NOT EXISTS (SELECT 1 FROM research_observations d WHERE d.project=o.project "
                "AND d.session=o.session AND d.work=o.work AND d.kind='resource_decision' "
                "AND d.identity=json_extract(o.detail,'$.request_id')) "
                "AND NOT " + SUPERSEDED.format(o="o") + " "
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
        if not operator.strip() or (
            request.action not in EXTENSIONS and request.action not in ("extend", "stop")
        ):
            raise ControlError(INVALID_REQUEST, "USER decision must be a soft extension or stop")
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
            if (
                request.action == "stop"
                and not db.execute(
                    "SELECT 1 FROM research_observations WHERE project=? AND session=? AND work=? "
                    "AND kind='resource_request' AND json_extract(detail,'$.request_id')=?",
                    (project, session, owner.work, request.request_id),
                ).fetchone()
            ):
                raise ControlError(INVALID_REQUEST, "a USER stop answers the root's check-in")
            decision = self._decide(
                db,
                owner,
                ResourceDecisionRequest(
                    request_id=request.request_id, action=request.action, reason=request.reason
                ),
                operation="user-resource:" + request.request_id,
                operator=operator,
                approval=request,
            )
            if request.action == "stop":
                self._stopped(db, owner)
            return decision

    def _stopped(self, db: sqlite3.Connection, owner: Ownership) -> None:
        """The user's Stop of a paused tree, as the Stop button records it: the tree is
        ``stopped`` and its root attempt carries an explicit cancel, so Resume and automatic
        recovery treat it as stopped."""
        db.execute(
            "WITH RECURSIVE tree(work) AS (SELECT work FROM research_work "
            "WHERE project=? AND session=? AND work=? UNION ALL SELECT w.work "
            "FROM research_work w JOIN tree p ON w.parent=p.work WHERE w.project=? "
            "AND w.session=?) UPDATE research_work SET state='stopped',reason='user_stop' "
            "WHERE project=? AND session=? AND work IN tree AND state!='completed'",
            (
                owner.project,
                owner.session,
                owner.work,
                owner.project,
                owner.session,
                owner.project,
                owner.session,
            ),
        )
        db.execute(
            "INSERT OR IGNORE INTO run_cancels VALUES (?,?,?,?)",
            (owner.project, owner.session, owner.run, self.clock.now().isoformat()),
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
        target = Ownership(
            project=owner.project, session=owner.session, work=work, run=current, epoch=owner.epoch
        )
        _, latest = soft_grant(db, target, warned.resource, warned.total)
        if latest and latest != warned.total:
            raise ControlError(CONFLICT, "resource grant revision changed")
        limits = settings_in(db, owner.project)
        progress = work_progress(db, target)
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
        total = _extended(limits, warned, request.action)
        if (
            request.action not in STOPPING
            and request.action not in ("pause", "redirect")
            and operator is None
            and idle_extensions(db, target, progress) >= limits.max_idle_extensions
        ):
            raise ControlError(
                CONFLICT,
                f"{limits.max_idle_extensions} extensions in a row without new evidence: "
                "stop or redirect this work",
            )
        return self._record(
            db,
            target,
            warned,
            request,
            parent_run=owner.run,
            operation=operation,
            total=total,
            progress=progress,
            operator=operator,
        )

    def _record(
        self,
        db: sqlite3.Connection,
        target: Ownership,
        warned: ResourceRequest,
        request: ResourceDecisionRequest,
        *,
        parent_run: str,
        operation: str,
        total: int,
        progress: int,
        operator: str | None = None,
        automatic: bool = False,
    ) -> ResourceDecision:
        decision = ResourceDecision(
            request=request,
            parent_run=parent_run,
            operation=operation,
            total=total,
            observed=self.clock.now().timestamp(),
            operator=operator,
            progress=progress,
            resource=warned.resource,
            automatic=automatic,
        )
        db.execute(
            "INSERT INTO research_observations VALUES (?,?,?,'resource_decision',?,?,?)",
            (
                target.project,
                target.session,
                target.work,
                request.request_id,
                decision.observed,
                decision.model_dump_json(),
            ),
        )
        return decision

    def safe_point(
        self, owner: Ownership, *, seconds: int, turns: int, tokens: int | None
    ) -> SafePoint:
        """Sample active time, then read every soft cap and its recorded use at once.

        ``turns`` and ``tokens`` are the attempt's configured caps; a child's original
        blueprint names its own, and a root's are pinned when first observed.
        """
        with self.control.write() as tx:
            db = tx.sql
            elapsed = sample_elapsed(db, owner, seconds, self.clock.now().timestamp())
            settings = settings_in(db, owner.project)
            progress = work_progress(db, owner)
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
            found = db.execute(
                "SELECT w.parent,json_extract(b.snapshot,'$.request.tokens'),"
                "json_extract(b.snapshot,'$.request.max_turns') FROM research_work w "
                "LEFT JOIN research_blueprints b USING(project,session,work) "
                "WHERE w.project=? AND w.session=? AND w.work=?",
                (owner.project, owner.session, owner.work),
            ).fetchone()
            child = found is not None and found[0] is not None
            if found is not None and child:
                tokens = int(found[1]) if found[1] else tokens
                turns = int(found[2]) if found[2] else turns
            configured: list[tuple[Resource, int | None]] = [
                ("tokens", tokens if child else settings.root_tokens),
                ("turns", turns),
                ("elapsed_seconds", settings.research_seconds),
                (
                    "cost",
                    None if settings.soft_cost is None else round(settings.soft_cost * 100),
                ),
            ]
            block = 0 if child else initial_allowance(db, owner, "turns", turns)
            caps: list[tuple[Resource, int, int]] = []
            for resource, value in configured:
                if value is None:
                    continue
                pinned = child and resource in ("tokens", "turns")  # the blueprint's own
                original = value if pinned else initial_allowance(db, owner, resource, value)
                total, revision = soft_grant(db, owner, resource, original)
                caps.append((resource, total, revision))
            storage: tuple[tuple[Resource, int, int, int], ...] = ()
            if settings.storage_bytes is not None:
                original = initial_allowance(db, owner, "retained_bytes", settings.storage_bytes)
                total, revision = soft_grant(db, owner, "retained_bytes", original)
                storage = (("retained_bytes", retained_bytes(db, owner), total, revision),)
            turn_total, turn_revision = soft_grant(db, owner, "turns", turns if child else block)
            return SafePoint(
                caps=tuple(caps),
                used={
                    "tokens": spent_tokens(db, owner.project, owner.session, owner.work),
                    "elapsed_seconds": elapsed,
                    "cost": round(spent_cost(db, owner.project, owner.session, owner.work) * 100),
                },
                prior_turns=turn_usage(db, owner, previous=True),
                hard_turns=max(settings.turn_ceiling, turn_total),
                turns=(turn_total, turn_revision),
                progress=progress,
                storage=storage,
                turn_block=block,
            )

    def summary(
        self,
        owner: Ownership,
        *,
        recent: tuple[str, ...] = (),
        answer: str | None = None,
        rule: str | None = None,
        detail: str | None = None,
        used: dict[str, int] | None = None,
        turns: int = 0,
    ) -> CheckInSummary:
        """The bounded state a check-in delivers to the parent: its brief from the
        work's blueprint, what it produced, and the caller's recent activity."""
        with self.control.read() as tx:
            db = tx.sql
            brief = db.execute(
                "SELECT json_extract(snapshot,'$.request.task') FROM research_blueprints "
                "WHERE project=? AND session=? AND work=?",
                (owner.project, owner.session, owner.work),
            ).fetchone()
            made = produced(db, owner)
            progress = work_progress(db, owner)
        used = used or {}
        return CheckInSummary(
            brief=str(brief[0] or "")[:2000] if brief else "",
            produced=made,
            recent=tuple(item[:300] for item in recent[-10:]),
            answer=None if answer is None else answer[:1000],
            rule=rule,
            detail=None if detail is None else detail[:300],
            tokens=used.get("tokens", 0),
            turns=turns,
            seconds=used.get("elapsed_seconds", 0),
            cost=used.get("cost", 0) / 100,
            progress=progress,
        )

    def awaited(
        self, owner: Ownership, request_id: str
    ) -> ResourceDecision | Literal["ended"] | None:
        """The decision a waiting child's check-in got, ``"ended"`` once its work stopped
        or its tree was cancelled, or ``None`` while it waits."""
        with self.control.read() as tx:
            db = tx.sql
            row = db.execute(
                "SELECT detail FROM research_observations WHERE project=? AND session=? "
                "AND work=? AND kind='resource_decision' AND identity=?",
                (owner.project, owner.session, owner.work, request_id),
            ).fetchone()
            if row is not None:
                return ResourceDecision.model_validate_json(row[0])
            state = db.execute(
                "SELECT state,root FROM research_work WHERE project=? AND session=? AND work=?",
                (owner.project, owner.session, owner.work),
            ).fetchone()
            if (
                state is None
                or state[0] not in ("running", "waiting")
                or tree_cancelled(db, owner.project, owner.session, state[1])
            ):
                return "ended"
            return None

    def self_check(
        self, owner: Ownership, request: ResourceRequest, *, ask_user: bool
    ) -> ResourceDecision | None:
        """Decide a check-in nobody else will: the root's own, or a child's whose parent
        did not answer in time. One bounded step while the work keeps producing evidence;
        after ``max_idle_extensions`` without, ``None`` asks the user (``ask_user``), and
        otherwise the work stops with its report."""
        if ask_user and request.resource == "turns" and request.parent is None:
            return None  # the root's turn limit is the user's to accept, evidence or not
        with self.control.write() as tx:
            db = tx.sql
            check_owner(db, owner)
            existing = db.execute(
                "SELECT detail FROM research_observations WHERE project=? AND session=? "
                "AND work=? AND kind='resource_decision' AND identity=?",
                (owner.project, owner.session, owner.work, request.request_id),
            ).fetchone()
            if existing is not None:
                return ResourceDecision.model_validate_json(existing[0])
            limits = settings_in(db, owner.project)
            progress = work_progress(db, owner)
            action: Action = "extend"
            total = request.total
            if idle_extensions(db, owner, progress) < limits.max_idle_extensions:
                try:
                    total = _extended(limits, request, "extend")
                except ControlError:
                    action = "stop"
            else:
                action = "stop"
            if action == "stop" and ask_user:
                return None
            reason = (
                "Automatic check-in: the work recorded new evidence or has extensions left "
                "without it; one bounded step."
                if action == "extend"
                else f"Automatic check-in: {limits.max_idle_extensions} extensions in a row "
                "without new evidence, or a ceiling; report and stop."
            )
            return self._record(
                db,
                owner,
                request,
                ResourceDecisionRequest(
                    request_id=request.request_id, action=action, reason=reason
                ),
                parent_run=owner.run,
                operation="check-in:" + request.request_id,
                total=total,
                progress=progress,
                automatic=True,
            )

    def accept(self, owner: Ownership, request: ResourceRequest, reason: str) -> ResourceDecision:
        """The user's explicit Resume of a root at its turn limit: the same decision as the
        card's continue, one more block, recorded against the new attempt's check-in."""
        with self.control.write() as tx:
            db = tx.sql
            check_owner(db, owner)
            existing = db.execute(
                "SELECT detail FROM research_observations WHERE project=? AND session=? "
                "AND work=? AND kind='resource_decision' AND identity=?",
                (owner.project, owner.session, owner.work, request.request_id),
            ).fetchone()
            if existing is not None:
                return ResourceDecision.model_validate_json(existing[0])
            return self._record(
                db,
                owner,
                request,
                ResourceDecisionRequest(
                    request_id=request.request_id, action="extend", reason=reason
                ),
                parent_run=owner.run,
                operation="resume:" + request.request_id,
                total=_extended(settings_in(db, owner.project), request, "extend"),
                progress=work_progress(db, owner),
                operator="resume",
            )

    def notify(self, owner: Ownership, request: ResourceRequest, note: str) -> None:
        """Tell the user, as a run event the session's chat shows: a notice, never a
        question (D102)."""
        with self.control.write() as tx:
            db = tx.sql
            (last,) = db.execute(
                "SELECT coalesce(max(seq),0) FROM run_events "
                "WHERE project_id=? AND session_id=? AND run=?",
                (owner.project, owner.session, owner.run),
            ).fetchone()
            db.execute(
                "INSERT INTO run_events VALUES (?,?,?,?,?,'notice',?)",
                (
                    owner.project,
                    owner.session,
                    owner.run,
                    last + 1,
                    self.clock.now().isoformat(),
                    json.dumps(
                        {
                            "source": "resources",
                            "ids": [request.request_id],
                            "text": note[:500],
                            "request_id": request.request_id,
                            "resource": request.resource,
                            "used": request.used,
                            "total": request.total,
                        }
                    ),
                ),
            )

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
