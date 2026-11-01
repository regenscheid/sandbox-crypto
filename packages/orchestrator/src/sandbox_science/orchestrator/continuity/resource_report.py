# SPDX-License-Identifier: MIT
"""Read-only views of a session's soft caps and check-ins, for the usage view (D102).

``ResourceRequests.safe_point`` reads the caps and writes while it does (it samples the
active time and pins a first allowance). These reads never write: a cap not yet pinned is
the one the settings or the child's blueprint would pin, and a cap nothing has pinned for
a root's turns is unknown. Nothing here decides, extends or stops anything.
"""

from __future__ import annotations

import sqlite3
from collections import Counter
from datetime import datetime
from typing import Final, Literal

from pydantic import Field

from sandbox_science.contracts import Contract
from sandbox_science.orchestrator.control import ControlStore
from sandbox_science.orchestrator.delegation.records import DelegationSettings, settings_in
from sandbox_science.orchestrator.documents import DocumentStore

from .records import Ownership
from .resource_policy import soft_grant
from .resource_usage import elapsed_seconds
from .resources import SUPERSEDED, turn_usage
from .token_usage import spent_cost, spent_tokens

MAX_WORKS: Final = 100
"""The most works one report lists."""
CapResource = Literal["tokens", "turns", "elapsed_seconds", "cost"]
Answerer = Literal["user", "parent", "root", "automatic", "none"]
"""Who answered a check-in, as the chat shows it: the user (a Resume included), the
direct parent, the root's own policy, the platform itself, or nobody yet."""


class CapUse(Contract):
    """One soft cap of one work and what it has used, cumulative across its attempts."""

    resource: CapResource
    used: int
    """Tokens, turns, observed active seconds, or cost in hundredths of the gateway's
    currency (as a check-in records it)."""
    total: int | None
    """The cap after recorded extensions; null when the cap is off or nothing records
    it yet."""


class WorkCaps(Contract):
    """A work's soft caps and use."""

    work: str
    parent: str | None
    role: Literal["main", "delegated"]
    state: str
    reason: str | None
    current: str
    runs: tuple[str, ...] = Field(max_length=500)
    """Its attempts, oldest first."""
    task: str = Field(default="", max_length=300)
    caps: tuple[CapUse, ...] = Field(max_length=4)


class CheckInCounts(Contract):
    """How many check-ins a scope recorded and how they went."""

    total: int = 0
    waiting: int = 0
    """Unanswered check-ins of work that is still going: the ones awaiting a decision."""
    by_resource: dict[str, int] = Field(default_factory=dict[str, int])
    loops: dict[str, int] = Field(default_factory=dict[str, int])
    """Loop check-ins by the heuristic that tripped: ``same_call``, ``same_error``,
    ``no_progress``, ``same_output``, or ``unknown`` for a record that names none."""
    answered_by: dict[str, int] = Field(default_factory=dict[str, int])
    actions: dict[str, int] = Field(default_factory=dict[str, int])
    """The decisions' actions (``extend``, ``redirect``, ``stop``, and the older ones)."""


def _pinned(db: sqlite3.Connection, owner: Ownership, resource: str) -> int | None:
    row = db.execute(
        "SELECT detail FROM research_observations WHERE project=? AND session=? AND work=? "
        "AND kind='resource_allowance' AND identity=?",
        (owner.project, owner.session, owner.work, resource),
    ).fetchone()
    return None if row is None else int(row[0])


def _cap(
    db: sqlite3.Connection,
    owner: Ownership,
    resource: CapResource,
    configured: int | None,
    *,
    child_value: int | None,
    settings: DelegationSettings,
) -> int | None:
    """The cap as the resource hook would read it, without pinning anything. A child's
    token and turn caps are its blueprint's; every other is pinned when first observed,
    else the configured value."""
    if child_value is not None and resource in ("tokens", "turns"):
        original: int | None = child_value
    else:
        original = _pinned(db, owner, resource)
        if original is None:
            original = configured
    if original is None:
        return None
    if resource == "elapsed_seconds" and settings.research_seconds is None:
        return None
    if resource == "cost" and settings.soft_cost is None:
        return None
    return soft_grant(db, owner, resource, original)[0]


def work_caps(
    control: ControlStore | DocumentStore, project: str, session: str
) -> tuple[WorkCaps, ...]:
    """Every work of the session with its soft caps and use, the root first."""
    store = control.control if isinstance(control, DocumentStore) else control
    with store.read() as tx:
        db = tx.sql
        epoch = db.execute(
            "SELECT epoch FROM research_sessions WHERE project=? AND session=?",
            (project, session),
        ).fetchone()
        if epoch is None:
            return ()
        settings = settings_in(db, project)
        rows = db.execute(
            "SELECT w.work,w.parent,w.role,w.state,w.reason,w.current,"
            "json_extract(b.snapshot,'$.request.task'),"
            "json_extract(b.snapshot,'$.request.tokens'),"
            "json_extract(b.snapshot,'$.request.max_turns') "
            "FROM research_work w LEFT JOIN research_blueprints b USING(project,session,work) "
            "WHERE w.project=? AND w.session=? ORDER BY w.parent IS NOT NULL,w.work LIMIT ?",
            (project, session, MAX_WORKS),
        ).fetchall()
        found: list[WorkCaps] = []
        for work, parent, role, state, reason, current, task, tokens, turns in rows:
            owner = Ownership(
                project=project, session=session, work=work, run=current, epoch=int(epoch[0])
            )
            child = parent is not None
            attempts = db.execute(
                "SELECT run FROM research_attempts WHERE project=? AND session=? AND work=? "
                "ORDER BY run LIMIT 500",
                (project, session, work),
            ).fetchall()
            configured: dict[CapResource, int | None] = {
                "tokens": None if child else settings.root_tokens,
                "turns": None,
                "elapsed_seconds": settings.research_seconds,
                "cost": None if settings.soft_cost is None else round(settings.soft_cost * 100),
            }
            blueprint = {
                "tokens": int(tokens) if child and tokens else None,
                "turns": int(turns) if child and turns else None,
            }
            used: dict[CapResource, int] = {
                "tokens": spent_tokens(db, project, session, work),
                "turns": turn_usage(db, owner),
                "elapsed_seconds": elapsed_seconds(db, owner),
                "cost": round(spent_cost(db, project, session, work) * 100),
            }
            caps = tuple(
                CapUse(
                    resource=resource,
                    used=used[resource],
                    total=_cap(
                        db,
                        owner,
                        resource,
                        configured[resource],
                        child_value=blueprint.get(resource),
                        settings=settings,
                    ),
                )
                for resource in ("tokens", "turns", "elapsed_seconds", "cost")
            )
            found.append(
                WorkCaps(
                    work=work,
                    parent=parent,
                    role=role,
                    state=state,
                    reason=reason,
                    current=current,
                    runs=tuple(str(row[0]) for row in attempts),
                    task=str(task or "")[:300],
                    caps=caps,
                )
            )
    return tuple(found)


def _answerer(decided: bool, operator: object, automatic: object, parent: object) -> Answerer:
    if not decided:
        return "none"
    if operator:
        return "user"
    if automatic:
        return "automatic"
    return "parent" if parent else "root"


def check_in_counts(
    control: ControlStore | DocumentStore,
    project: str,
    *,
    session: str | None = None,
    since: datetime | None = None,
) -> dict[str, CheckInCounts]:
    """Check-ins recorded after ``since`` (all, if none) by session, counted by resource,
    loop rule, who answered and the answer's action. ``session`` limits it to one."""
    store = control.control if isinstance(control, DocumentStore) else control
    with store.read() as tx:
        rows = tx.sql.execute(
            "SELECT r.session,json_extract(r.detail,'$.resource'),"
            "json_extract(r.detail,'$.summary.rule'),json_extract(r.detail,'$.parent'),"
            "d.detail IS NOT NULL,json_extract(d.detail,'$.operator'),"
            "coalesce(json_extract(d.detail,'$.automatic'),0),"
            "json_extract(d.detail,'$.request.action'),"
            "w.state IN ('completed','stopped') OR json_extract(r.detail,'$.current')!=w.current "
            "OR " + SUPERSEDED.format(o="r") + ",count(*) "
            "FROM research_observations r JOIN research_work w USING(project,session,work) "
            "LEFT JOIN research_observations d ON d.project=r.project AND d.session=r.session "
            "AND d.work=r.work AND d.kind='resource_decision' "
            "AND d.identity=json_extract(r.detail,'$.request_id') "
            "WHERE r.project=? AND r.kind='resource_request' AND (? IS NULL OR r.session=?) "
            "AND r.observed>=? GROUP BY 1,2,3,4,5,6,7,8,9",
            (project, session, session, 0.0 if since is None else since.timestamp()),
        ).fetchall()
    parts: dict[str, dict[str, Counter[str]]] = {}
    waiting: Counter[str] = Counter()
    for name, resource, rule, parent, decided, operator, automatic, action, past, count in rows:
        got = parts.setdefault(
            name,
            {
                key: Counter[str]()
                for key in ("total", "by_resource", "loops", "answered_by", "actions")
            },
        )
        got["total"]["all"] += count
        got["by_resource"][str(resource)] += count
        if resource == "loop":
            got["loops"][str(rule or "unknown")] += count
        got["answered_by"][_answerer(bool(decided), operator, automatic, parent)] += count
        if decided and action:
            got["actions"][str(action)] += count
        if not decided and not past:
            waiting[name] += count
    return {
        name: CheckInCounts(
            total=got["total"]["all"],
            waiting=waiting[name],
            by_resource=dict(got["by_resource"]),
            loops=dict(got["loops"]),
            answered_by=dict(got["answered_by"]),
            actions=dict(got["actions"]),
        )
        for name, got in parts.items()
    }


def merge_counts(counts: list[CheckInCounts]) -> CheckInCounts:
    """The sum of several scopes' counts."""
    fields = ("by_resource", "loops", "answered_by", "actions")
    sums = {field: Counter[str]() for field in fields}
    for item in counts:
        for field in fields:
            sums[field].update(getattr(item, field))
    return CheckInCounts(
        total=sum(item.total for item in counts),
        waiting=sum(item.waiting for item in counts),
        **{field: dict(value) for field, value in sums.items()},
    )
