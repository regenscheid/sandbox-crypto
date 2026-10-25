# SPDX-License-Identifier: MIT
"""The Monitor view's route: one project's figures for a window (D103, GUI-native).

``GET /api/v0/projects/{project}/monitor?window=24h|7d`` combines

- the worker's monitor figures (``GET /v1/monitor``, through the workspace's operator
  client): calls by effect class and destination, approvals, open holds, drifted
  servers and the environments' isolation tiers. When the worker is not configured,
  cannot be reached or refuses, that part is null with a short reason; the worker's own
  error text is not passed on;
- the control store's recorded spend and tokens within the window, by role and by model,
  and the project's cost figures as its page shows them (``model_cost``);
- each configured MCP service's latest scheduled canary, as the MCP services view reads
  it.

Network-policy drops are not available: the sandboxes' logs carry none yet. Everything is
platform data, counted by code; the route reads and never writes.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Final, Protocol, cast

from aiohttp import web
from pydantic import Field

from sandbox_science.contracts import Contract, ErrorKind, Identifier
from sandbox_science.contracts.monitor import WINDOWS, MonitorWindow, WorkerMonitor
from sandbox_science.orchestrator.mcp_canaries import CanaryStore
from sandbox_science.orchestrator.projects import ProjectError
from sandbox_science.orchestrator.runstate import COST_ROLES, RunState
from sandbox_science.orchestrator.worker_client import WorkerAPIError, WorkerClient

from .api import ModelCost, model_cost
from .context import CONTEXT, identifier, json_answer
from .mcp_services import CONFIGURATION, Configuration, McpCanary, canary_view
from .requests import dumps

ROUTE: Final = "/api/v0/projects/{project}/monitor"
NETWORK_DROPS: Final = "Network-policy drops are not available: the sandboxes' logs carry none yet."
MAX_MODELS: Final = 100


class MonitorSource(Protocol):
    """The worker client's monitor read, as the workspace's operator client has it."""

    async def monitor(self, project_id: str, window: MonitorWindow) -> WorkerMonitor: ...


SOURCE: Final = web.AppKey("monitor_source", MonitorSource)


class SpendRow(Contract):
    """Recorded model use of one role or one model within the window. ``cost`` is null when
    the gateway priced none of its calls; ``unpriced`` counts calls with tokens and no cost."""

    role: str | None = None
    model: str | None = None
    """The model as the gateway named it; null where the record names none (unknown)."""
    cost: float | None
    tokens: int
    calls: int
    unpriced: int


class MonitorSpend(Contract):
    cost: float | None
    tokens: int
    calls: int
    unpriced: int
    by_role: list[SpendRow] = Field(max_length=16)
    by_model: list[SpendRow] = Field(max_length=MAX_MODELS)
    """The costliest first, then the most tokens."""


class CanaryRow(Contract):
    server: Identifier
    canary: McpCanary | None
    """Null when the deployment recorded none for the service."""


class ProjectMonitor(Contract):
    """``GET …/monitor``: the Monitor view's figures."""

    project_id: Identifier
    window: MonitorWindow
    since: datetime
    at: datetime
    worker: WorkerMonitor | None
    worker_unavailable: str | None = None
    """Why ``worker`` is null, in a few words."""
    spend: MonitorSpend
    model_cost: ModelCost
    canaries: list[CanaryRow]
    network_drops: str = NETWORK_DROPS


def configure(app: web.Application, worker: WorkerClient | None) -> None:
    if worker is not None:
        app[SOURCE] = cast(MonitorSource, worker)


def add_routes(app: web.Application) -> None:
    app.router.add_get(ROUTE, monitor)


def _window(request: web.Request) -> MonitorWindow:
    if set(request.query) - {"window"}:
        raise ProjectError(ErrorKind.INVALID_REQUEST, "the monitor takes only a window")
    window = request.query.get("window", "24h")
    if window not in WINDOWS:
        raise ProjectError(ErrorKind.INVALID_REQUEST, "the window is 24h or 7d")
    return cast(MonitorWindow, window)


def _spend(runs: RunState, project: str, since: datetime) -> MonitorSpend:
    rows = runs.usage_by_model(project, since)

    def total(
        found: list[tuple[str, str | None, float | None, int, int, int]],
        role: str | None = None,
        model: str | None = None,
    ) -> SpendRow:
        costs = [cost for _, _, cost, _, _, _ in found if cost is not None]
        return SpendRow(
            role=role,
            model=model,
            cost=sum(costs) if costs else None,
            tokens=sum(item[3] for item in found),
            calls=sum(item[4] for item in found),
            unpriced=sum(item[5] for item in found),
        )

    order = {role: index for index, role in enumerate(COST_ROLES)}
    roles = sorted({row[0] for row in rows}, key=lambda role: (order.get(role, 99), role))
    models = [
        total([row for row in rows if row[1] == name], model=name)
        for name in {row[1] for row in rows}
    ]
    models.sort(key=lambda item: (-(item.cost or 0.0), -item.tokens, item.model or ""))
    whole = total(rows)
    return MonitorSpend(
        cost=whole.cost,
        tokens=whole.tokens,
        calls=whole.calls,
        unpriced=whole.unpriced,
        by_role=[total([row for row in rows if row[0] == role], role=role) for role in roles],
        by_model=models[:MAX_MODELS],
    )


def _records(
    request: web.Request, project: str, since: datetime
) -> tuple[MonitorSpend, ModelCost, list[CanaryRow]]:
    """The control store's part. Blocking."""
    context = request.app[CONTEXT]
    context.projects.get(project)
    config = request.app.get(CONFIGURATION, Configuration())
    names = [item.name for item in config.services] if config.project == project else []
    latest = CanaryStore.of(context.documents).latest(names) if names else {}
    canaries = [CanaryRow(server=name, canary=canary_view(latest.get(name))) for name in names]
    return (
        _spend(RunState(context.projects), project, since),
        model_cost(context.projects, project),
        canaries,
    )


async def _worker(
    request: web.Request, project: str, window: MonitorWindow
) -> tuple[WorkerMonitor | None, str | None]:
    source = request.app.get(SOURCE)
    if source is None:
        return None, "no worker is configured"
    try:
        async with asyncio.timeout(request.app[CONTEXT].timeouts.call):
            return await source.monitor(project, window), None
    except TimeoutError:
        return None, "the worker did not answer in time"
    except WorkerAPIError as exc:
        if exc.status == 404:
            return None, "the worker has no monitor figures (an older worker)"
        if exc.kind is ErrorKind.UNAVAILABLE and exc.status is None:
            return None, "the worker cannot be reached"
        return None, f"the worker refused ({exc.kind.value})"


async def monitor(request: web.Request) -> web.Response:
    """The project's monitor figures for the window; parts the worker holds are null with
    a reason when it is unavailable."""
    project = identifier(request, "project")
    window = _window(request)
    at = datetime.now(UTC)
    since = at - WINDOWS[window]
    spend, cost, canaries = await asyncio.to_thread(_records, request, project, since)
    worker, unavailable = await _worker(request, project, window)
    answer = ProjectMonitor(
        project_id=project,
        window=window,
        since=since,
        at=at,
        worker=worker,
        worker_unavailable=unavailable,
        spend=spend,
        model_cost=cost,
        canaries=canaries,
    )
    return json_answer(dumps(answer.model_dump(mode="json")))
