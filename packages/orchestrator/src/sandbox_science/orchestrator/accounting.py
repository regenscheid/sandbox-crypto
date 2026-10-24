# SPDX-License-Identifier: MIT
"""Recorded model spend and tokens by project, session, run and role (D102).

Accounting only: LiteLLM's budget is the one spending control by default, and a
configured project cost limit is checked by the model-call journal alone
(``continuity/models.py``). Nothing here stops a run. A threshold notice says when the
project's recorded cost first passed it. Costs are what the gateway reported or priced
from its model information; a call without one counts as unpriced, never as zero.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import Field

from sandbox_science.contracts import Contract

from .runstate import COST_ROLES, RunState

MAX_SCOPES = 100
"""The most sessions or runs one account lists; the totals cover every one."""


class RoleUsage(Contract):
    role: str
    """``main``, ``delegated``, ``reviewer``, ``watcher``, ``embeddings`` or ``metadata``."""
    cost: float | None
    """Known cost; null when the gateway priced none of its calls."""
    tokens: int
    calls: int
    unpriced: int
    """Calls that used tokens and have no known cost."""


class ScopeUsage(Contract):
    """One scope's recorded usage: the whole project, a session or a run."""

    session: str | None = None
    run: str | None = None
    cost: float | None
    tokens: int
    calls: int
    unpriced: int
    by_role: tuple[RoleUsage, ...] = Field(max_length=16)


class CostNotice(Contract):
    threshold: float
    crossed_at: datetime | None
    spent: float | None


class UsageAccount(Contract):
    """What a project, or one of its sessions, has spent, as recorded."""

    project: str
    session: str | None
    total: ScopeUsage
    sessions: tuple[ScopeUsage, ...] = Field(max_length=MAX_SCOPES)
    """The costliest sessions first, then by name."""
    runs: tuple[ScopeUsage, ...] = Field(max_length=MAX_SCOPES)
    """The costliest runs first; a reviewer's calls are under the run it reviewed."""
    daily_cost_limit: float | None
    """A configured local limit, which the model-call journal enforces; null, as by
    default, when only LiteLLM's budget enforces spending."""
    notices: tuple[CostNotice, ...] = Field(max_length=20)


_Row = tuple[str | None, str | None, str, float | None, int, int, int]


def _scope(rows: list[_Row], *, session: str | None = None, run: str | None = None) -> ScopeUsage:
    costs: dict[str, float] = {}
    counts: dict[str, tuple[int, int, int]] = {}
    for _, _, role, cost, tokens, calls, unpriced in rows:
        if cost is not None:
            costs[role] = costs.get(role, 0.0) + cost
        used, made, unknown = counts.get(role, (0, 0, 0))
        counts[role] = (used + tokens, made + calls, unknown + unpriced)
    order = {role: index for index, role in enumerate(COST_ROLES)}
    by_role = tuple(
        RoleUsage(
            role=role,
            cost=costs.get(role),
            tokens=used,
            calls=made,
            unpriced=unknown,
        )
        for role, (used, made, unknown) in sorted(
            counts.items(), key=lambda item: (order.get(item[0], len(order)), item[0])
        )
    )
    return ScopeUsage(
        session=session,
        run=run,
        cost=sum(costs.values()) if costs else None,
        tokens=sum(item.tokens for item in by_role),
        calls=sum(item.calls for item in by_role),
        unpriced=sum(item.unpriced for item in by_role),
        by_role=by_role,
    )


def _ranked(scopes: list[ScopeUsage]) -> tuple[ScopeUsage, ...]:
    ranked = sorted(
        scopes, key=lambda item: (-(item.cost or 0.0), -item.tokens, item.session, item.run)
    )
    return tuple(ranked[:MAX_SCOPES])


def account(runs: RunState, project: str, session: str | None = None) -> UsageAccount:
    """The project's account, or one session's; reads only, in a few snapshots."""
    rows = runs.usage_rows(project, session)
    sessions: dict[str, list[_Row]] = {}
    by_run: dict[tuple[str, str], list[_Row]] = {}
    for row in rows:
        if row[0] is not None:
            sessions.setdefault(row[0], []).append(row)
            if row[1] is not None:
                by_run.setdefault((row[0], row[1]), []).append(row)
    return UsageAccount(
        project=project,
        session=session,
        total=_scope(rows, session=session),
        sessions=_ranked([_scope(found, session=name) for name, found in sessions.items()]),
        runs=_ranked([_scope(found, session=key[0], run=key[1]) for key, found in by_run.items()]),
        daily_cost_limit=runs.cost_limit(project),
        notices=tuple(
            CostNotice(
                threshold=threshold,
                crossed_at=None if crossed is None else datetime.fromisoformat(crossed),
                spent=spent,
            )
            for threshold, crossed, spent in runs.cost_notices(project)
        ),
    )
