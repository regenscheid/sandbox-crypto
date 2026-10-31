# SPDX-License-Identifier: MIT
"""The usage view's routes (D102): what a session or a project has spent, against the soft
caps, with the check-ins and loop detections that followed.

``GET /api/v0/projects/{project}/sessions/{session}/usage/view`` is one session over its
life: its spend and tokens by agent (the main run, each child, with the reviewer's calls
under the run it reviewed) and by model, each agent's soft caps with their use, and the
session's check-ins, how they were answered, and the loop detections among them.

``GET /api/v0/projects/{project}/usage/view?window=24h|7d|all`` is the project over a recent
window (all of its life, with ``all``): its sessions by spend, the check-ins and loop
detections in the window, and the project's optional local cost limit and notices.

Both read records control already keeps; neither writes, decides or stops anything. The
Monitor view stays the place for a window's spend by role and model beside the platform's
other counts; the agents' own ``resource_usage`` tool and ``…/recovery/usage`` read the
unwindowed account.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Final, Literal

from aiohttp import web
from pydantic import Field

from sandbox_science.contracts import Contract, ErrorKind
from sandbox_science.contracts.monitor import WINDOWS
from sandbox_science.orchestrator.accounting import (
    MAX_SCOPES,
    CostNotice,
    ScopeUsage,
    cost_notices,
    scope_usage,
)
from sandbox_science.orchestrator.continuity.resource_report import (
    CapUse,
    CheckInCounts,
    WorkCaps,
    check_in_counts,
    merge_counts,
    work_caps,
)
from sandbox_science.orchestrator.continuity.resources import ResourceRequestPage, ResourceRequests
from sandbox_science.orchestrator.projects import ProjectError
from sandbox_science.orchestrator.runstate import COST_ROLES, RunState

from .context import CONTEXT, identifier, json_answer, no_query, session_record

PROJECT_ROUTE: Final = "/api/v0/projects/{project}/usage/view"
SESSION_ROUTE: Final = "/api/v0/projects/{project}/sessions/{session}/usage/view"
UsageWindow = Literal["24h", "7d", "all"]
MAX_LINES: Final = 64
"""The most role-and-model lines one agent lists."""
UNATTRIBUTED: Final = "unattributed"
"""The key of the usage no run is recorded for (a call outside any run)."""

Breakdown = tuple[str | None, str | None, str, str | None, float | None, int, int, int]
"""One row of ``RunState.usage_breakdown``: session, run, role, model, cost, tokens, calls,
unpriced calls."""


class SpendLine(Contract):
    """One role's use of one model."""

    role: str
    model: str | None
    """The model as the gateway named it; null where the record names none."""
    cost: float | None
    """Known cost; null when none of the calls was priced."""
    tokens: int
    calls: int
    unpriced: int
    """Calls that used tokens and have no known cost."""


class ModelLine(Contract):
    model: str | None
    cost: float | None
    tokens: int
    calls: int
    unpriced: int


class AgentUsage(Contract):
    """One agent's use: a work of the session across its attempts, with the reviewer's
    calls on its runs, or (without a work record) a run on its own."""

    key: str
    work: str | None
    runs: tuple[str, ...] = Field(max_length=500)
    parent: str | None
    role: Literal["main", "delegated"] | None
    state: str | None
    reason: str | None
    task: str = ""
    cost: float | None
    tokens: int
    calls: int
    unpriced: int
    lines: tuple[SpendLine, ...] = Field(max_length=MAX_LINES)
    caps: tuple[CapUse, ...] = Field(max_length=4)
    """Its soft caps and use; empty for a run with no work record."""


class SessionUsageView(Contract):
    project: str
    session: str
    at: datetime
    total: ScopeUsage
    by_model: tuple[ModelLine, ...] = Field(max_length=MAX_SCOPES)
    agents: tuple[AgentUsage, ...] = Field(max_length=MAX_SCOPES)
    """The root first, then the others in the order they began."""
    more_agents: int = 0
    """Agents beyond the first 100, which the totals still cover."""
    check_ins: ResourceRequestPage
    """The latest check-ins; older ones page through ``…/recovery/resources``."""
    counts: CheckInCounts
    daily_cost_limit: float | None
    notices: tuple[CostNotice, ...] = Field(max_length=20)


class SessionLine(Contract):
    """One session's use within the window, and its check-ins in it."""

    session: str
    cost: float | None
    tokens: int
    calls: int
    unpriced: int
    check_ins: int
    loops: int
    waiting: int


class ProjectUsageView(Contract):
    project: str
    window: UsageWindow
    since: datetime | None
    at: datetime
    total: ScopeUsage
    """The window's use, by role."""
    all_time: ScopeUsage
    """The project's use over its whole life, whatever the window."""
    unassigned: ScopeUsage
    """The window's calls no session is recorded for, which only the totals count."""
    by_model: tuple[ModelLine, ...] = Field(max_length=MAX_SCOPES)
    sessions: tuple[SessionLine, ...] = Field(max_length=MAX_SCOPES)
    more_sessions: int = 0
    counts: CheckInCounts
    daily_cost_limit: float | None
    last_day_cost: float | None
    """What the project spent in the last 24 hours (known costs); null with no limit."""
    notices: tuple[CostNotice, ...] = Field(max_length=20)


def add_routes(app: web.Application) -> None:
    app.router.add_get(PROJECT_ROUTE, project_usage)
    app.router.add_get(SESSION_ROUTE, session_usage)


# --- Aggregation (pure) -------------------------------------------------------------------


def _sum(rows: list[Breakdown]) -> tuple[float | None, int, int, int]:
    costs = [row[4] for row in rows if row[4] is not None]
    return (
        sum(costs) if costs else None,
        sum(row[5] for row in rows),
        sum(row[6] for row in rows),
        sum(row[7] for row in rows),
    )


def _by_model(rows: list[Breakdown]) -> tuple[ModelLine, ...]:
    groups: dict[str | None, list[Breakdown]] = {}
    for row in rows:
        groups.setdefault(row[3], []).append(row)
    lines = [
        ModelLine(model=name, cost=cost, tokens=tokens, calls=calls, unpriced=unpriced)
        for name, found in groups.items()
        for cost, tokens, calls, unpriced in [_sum(found)]
    ]
    lines.sort(key=lambda item: (-(item.cost or 0.0), -item.tokens, item.model or ""))
    return tuple(lines[:MAX_SCOPES])


def _lines(rows: list[Breakdown]) -> tuple[SpendLine, ...]:
    order = {role: index for index, role in enumerate(COST_ROLES)}
    groups: dict[tuple[str, str | None], list[Breakdown]] = {}
    for row in rows:
        groups.setdefault((row[2], row[3]), []).append(row)
    lines = [
        SpendLine(role=role, model=name, cost=cost, tokens=tokens, calls=calls, unpriced=unpriced)
        for (role, name), found in groups.items()
        for cost, tokens, calls, unpriced in [_sum(found)]
    ]
    lines.sort(
        key=lambda item: (order.get(item.role, len(order)), -(item.cost or 0.0), item.model or "")
    )
    return tuple(lines[:MAX_LINES])


def agent_usage(rows: list[Breakdown], works: tuple[WorkCaps, ...]) -> list[AgentUsage]:
    """Usage by agent: each work (the root first), its attempts' runs together with the
    reviewer's calls on them, then the runs no work record names, the costliest first."""
    owner = {run: item.work for item in works for run in item.runs}
    grouped: dict[str, list[Breakdown]] = {}
    for row in rows:
        key = UNATTRIBUTED if row[1] is None else owner.get(row[1], row[1])
        grouped.setdefault(key, []).append(row)

    def build(
        key: str, found: list[Breakdown], work: WorkCaps | None, runs: tuple[str, ...]
    ) -> AgentUsage:
        cost, tokens, calls, unpriced = _sum(found)
        return AgentUsage(
            key=key,
            work=None if work is None else work.work,
            runs=runs,
            parent=None if work is None else work.parent,
            role=None if work is None else work.role,
            state=None if work is None else work.state,
            reason=None if work is None else work.reason,
            task="" if work is None else work.task,
            cost=cost,
            tokens=tokens,
            calls=calls,
            unpriced=unpriced,
            lines=_lines(found),
            caps=() if work is None else work.caps,
        )

    agents = [build(item.work, grouped.pop(item.work, []), item, item.runs) for item in works]
    loose = [
        build(key, found, None, () if key == UNATTRIBUTED else (key,))
        for key, found in grouped.items()
    ]
    loose.sort(key=lambda item: (-(item.cost or 0.0), -item.tokens, item.key))
    return agents + loose


def session_lines(
    rows: list[Breakdown], counts: dict[str, CheckInCounts]
) -> tuple[list[SessionLine], ScopeUsage]:
    """Each session's use and check-ins, the costliest first, and the use of the calls
    no session is recorded for."""
    grouped: dict[str, list[Breakdown]] = {}
    for row in rows:
        if row[0] is not None:
            grouped.setdefault(row[0], []).append(row)
    lines: list[SessionLine] = []
    for name in sorted(set(grouped) | set(counts)):
        cost, tokens, calls, unpriced = _sum(grouped.get(name, []))
        found = counts.get(name, CheckInCounts())
        lines.append(
            SessionLine(
                session=name,
                cost=cost,
                tokens=tokens,
                calls=calls,
                unpriced=unpriced,
                check_ins=found.total,
                loops=sum(found.loops.values()),
                waiting=found.waiting,
            )
        )
    lines.sort(key=lambda item: (-(item.cost or 0.0), -item.tokens, item.session))
    return lines, scope_usage([_row(row) for row in rows if row[0] is None])


def _row(row: Breakdown) -> tuple[str | None, str | None, str, float | None, int, int, int]:
    return row[0], row[1], row[2], row[4], row[5], row[6], row[7]


# --- Reads (blocking) ---------------------------------------------------------------------


def read_session(request: web.Request, project: str, session: str) -> SessionUsageView:
    context = request.app[CONTEXT]
    session_record(context.projects, project, session)
    runs = RunState(context.projects)
    rows = runs.usage_breakdown(project, session)
    works = work_caps(context.documents, project, session)
    agents = agent_usage(rows, works)
    counts = check_in_counts(context.documents, project, session=session).get(
        session, CheckInCounts()
    )
    return SessionUsageView(
        project=project,
        session=session,
        at=datetime.now(UTC),
        total=scope_usage([_row(row) for row in rows], session=session),
        by_model=_by_model(rows),
        agents=tuple(agents[:MAX_SCOPES]),
        more_agents=max(0, len(agents) - MAX_SCOPES),
        check_ins=ResourceRequests(context.documents).page(project, session),
        counts=counts,
        daily_cost_limit=runs.cost_limit(project),
        notices=cost_notices(runs, project),
    )


def read_project(request: web.Request, project: str, window: UsageWindow) -> ProjectUsageView:
    context = request.app[CONTEXT]
    context.projects.get(project)
    runs = RunState(context.projects)
    at = datetime.now(UTC)
    since = None if window == "all" else at - WINDOWS[window]
    rows = runs.usage_breakdown(project, None, since)
    counts = check_in_counts(context.documents, project, since=since)
    lines, unassigned = session_lines(rows, counts)
    limit = runs.cost_limit(project)
    return ProjectUsageView(
        project=project,
        window=window,
        since=since,
        at=at,
        total=scope_usage([_row(row) for row in rows]),
        all_time=scope_usage(
            [_row(row) for row in rows]
            if since is None
            else [_row(row) for row in runs.usage_breakdown(project)]
        ),
        unassigned=unassigned,
        by_model=_by_model(rows),
        sessions=tuple(lines[:MAX_SCOPES]),
        more_sessions=max(0, len(lines) - MAX_SCOPES),
        counts=merge_counts(list(counts.values())),
        daily_cost_limit=limit,
        last_day_cost=None if limit is None else runs.model_cost(project, at - WINDOWS["24h"]),
        notices=cost_notices(runs, project),
    )


# --- Routes -------------------------------------------------------------------------------


async def session_usage(request: web.Request) -> web.Response:
    """One session's spend by agent and model, its soft caps and its check-ins."""
    project, session = identifier(request, "project"), identifier(request, "session")
    no_query(request, "the usage view")
    return json_answer(
        await asyncio.to_thread(lambda: read_session(request, project, session).model_dump_json())
    )


async def project_usage(request: web.Request) -> web.Response:
    """The project's spend over a window, by session, with the window's check-ins."""
    project = identifier(request, "project")
    if set(request.query) - {"window"} or request.can_read_body:
        raise ProjectError(ErrorKind.INVALID_REQUEST, "the usage view takes only a window")
    window = request.query.get("window", "24h")
    if window not in ("24h", "7d", "all"):
        raise ProjectError(ErrorKind.INVALID_REQUEST, "the window is 24h, 7d or all")
    return json_answer(
        await asyncio.to_thread(lambda: read_project(request, project, window).model_dump_json())
    )
