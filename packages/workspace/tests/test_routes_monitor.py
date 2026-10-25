# SPDX-License-Identifier: MIT
"""The Monitor view's route (D103): the worker's figures, the control store's spend and
tokens for the window by role and model, and each service's latest canary; the worker's
part is null with a reason when it cannot answer, and its error text stays out."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from sandbox_science.contracts import EnvironmentState, ExecutionContext
from sandbox_science.contracts.monitor import (
    ApprovalCounts,
    CallRow,
    EnvironmentTier,
    MonitorRecords,
    WorkerMonitor,
)
from sandbox_science.orchestrator.jobstate import JobState
from sandbox_science.orchestrator.mcp_canaries import CanaryResult, CanaryStore
from sandbox_science.orchestrator.projects import ModelUsage, ProjectStore
from sandbox_science.testing import fake_worker_app
from sandbox_science.workspace.mcp_services import ServiceConfig
from sandbox_science.workspace.monitor import NETWORK_DROPS, ProjectMonitor

from .route_support import WORKER_SECRET, error_of, refusing_port, workspace

ClientFactory = Callable[..., Awaitable[TestClient[Any, Any]]]
ServerFactory = Callable[..., Awaitable[TestServer]]
SERVICES = (
    ServiceConfig(name="papers", purpose="Retrieve research papers"),
    ServiceConfig(name="web", purpose="Fetch pages"),
)


def figures(project: str, window: str) -> WorkerMonitor:
    now = datetime.now(UTC)
    return WorkerMonitor.model_validate(
        {
            "project_id": project,
            "window": window,
            "since": now - timedelta(days=1),
            "at": now,
            "records": MonitorRecords(
                sources=["policy-middleware"],
                scanned=12,
                truncated=False,
                calls=[CallRow(effect="read", server="papers", allowed=9, denied=1)],
                approvals=[ApprovalCounts(kind="mcp", approved=1, median_wait_seconds=42.0)],
                holds=[],
                drift=[],
            ),
            "environments": [
                EnvironmentTier(
                    environment_id="env1",
                    kind="session",
                    profile="minimal",
                    state=EnvironmentState.READY,
                    declared_tier=3,
                    tier=3,
                )
            ],
        }
    )


async def worker_at(
    aiohttp_server: ServerFactory, *, refuse: int | None = None
) -> tuple[str, list[dict[str, str]]]:
    """A fake worker answering ``/v1/monitor``, or refusing it with ``refuse``."""
    asked: list[dict[str, str]] = []

    async def monitor(request: web.Request) -> web.Response:
        asked.append(dict(request.query))
        if refuse is not None:
            body = {"error": {"kind": "unavailable", "message": WORKER_SECRET}}
            return web.json_response(body, status=refuse)
        found = figures(request.query["project"], request.query["window"])
        return web.json_response(text=found.model_dump_json())

    app = fake_worker_app()
    app.router.add_get("/v1/monitor", monitor)
    server = await aiohttp_server(app)
    return str(server.make_url("")).rstrip("/"), asked


def seed(state_dir: Path) -> None:
    """Model calls of the last day, of the week and older, and two canaries."""
    store = ProjectStore(state_dir)
    now = datetime.now(UTC)
    base = store.run("p1", "a", store.runs("p1", "a")[0].name)
    priced = ModelUsage(model="gpt-x", input_tokens=10, output_tokens=2, cost=0.125)
    unknown = ModelUsage(input_tokens=5, output_tokens=0)
    week = ModelUsage(model="gpt-x", input_tokens=100, output_tokens=20, cost=1.0)
    old = ModelUsage(model="old", input_tokens=1, output_tokens=1, cost=9.0)
    for run_id, at, usage in (
        ("run-day", now - timedelta(hours=2), [priced, unknown]),
        ("run-week", now - timedelta(days=3), [week]),
        ("run-old", now - timedelta(days=9), [old]),
    ):
        store.save_run(
            base.model_copy(
                update={"run_id": run_id, "started_at": at, "ended_at": at, "usage": usage}
            )
        )
    jobs = JobState(state_dir)
    try:
        context = ExecutionContext(project_id="p1", session_id="a", environment_id="e")
        call = jobs.reserve(context, "run-day", "exe-1", "watcher", {}, 60, 0)
        assert call is not None
        jobs.finish_call(call, "fine", "small", 10, 2, 0.5, "gateway")
    finally:
        jobs.close()
    canaries = CanaryStore(store.control)
    canaries.record(CanaryResult("papers", now, "listed", 12, "d" * 64, 340, None))
    canaries.record(CanaryResult("other", now, "listed", 1, "e" * 64, 5, None))
    store.close()


async def test_the_monitor_combines_the_workers_figures_spend_and_canaries(
    aiohttp_client: ClientFactory, aiohttp_server: ServerFactory, state_dir: Path, static_dir: Path
) -> None:
    seed(state_dir)
    url, asked = await worker_at(aiohttp_server)
    client = await workspace(
        aiohttp_client, state_dir, static_dir, worker_url=url, mcp_project="p1",
        mcp_inventory=SERVICES,
    )  # fmt: skip
    response = await client.get("/api/v0/projects/p1/monitor?window=24h")
    assert response.status == 200, await response.text()
    assert response.headers["Cache-Control"] == "no-store"
    found = ProjectMonitor.model_validate(await response.json())
    assert asked == [{"project": "p1", "window": "24h"}]
    assert found.worker is not None
    assert found.worker_unavailable is None
    assert found.worker.records is not None
    assert found.worker.records.calls[0].allowed == 9
    assert found.network_drops == NETWORK_DROPS
    # The last day's calls: the main run's two (one unpriced) and the watcher's one.
    spend = found.spend
    assert (spend.cost, spend.tokens, spend.calls, spend.unpriced) == (0.625, 29, 3, 1)
    assert [(row.role, row.cost, row.tokens) for row in spend.by_role] == [
        ("main", 0.125, 17),
        ("watcher", 0.5, 12),
    ]
    assert [(row.model, row.cost, row.tokens, row.unpriced) for row in spend.by_model] == [
        ("small", 0.5, 12, 0),
        ("gpt-x", 0.125, 12, 0),
        (None, None, 5, 1),  # the gateway named no model: unknown
    ]
    assert [(item.server, item.canary is not None) for item in found.canaries] == [
        ("papers", True),
        ("web", False),
    ]
    week = ProjectMonitor.model_validate(
        await (await client.get("/api/v0/projects/p1/monitor?window=7d")).json()
    )
    assert week.spend.cost == 1.625
    assert asked[-1] == {"project": "p1", "window": "7d"}
    # The project page's figures, beside the window's.
    assert found.model_cost.total == 10.625


async def test_the_workers_part_is_null_with_a_reason_when_it_cannot_answer(
    aiohttp_client: ClientFactory, aiohttp_server: ServerFactory, state_dir: Path, static_dir: Path
) -> None:
    with refusing_port() as url:
        client = await workspace(aiohttp_client, state_dir, static_dir, worker_url=url)
        response = await client.get("/api/v0/projects/p1/monitor")
        assert response.status == 200, await response.text()
        found = ProjectMonitor.model_validate(await response.json())
    assert (found.window, found.worker) == ("24h", None)
    assert found.worker_unavailable == "the worker cannot be reached"
    assert found.spend.calls == 0
    assert found.canaries == []
    refusing, _ = await worker_at(aiohttp_server, refuse=503)
    client = await workspace(aiohttp_client, state_dir, static_dir, worker_url=refusing)
    text = await (await client.get("/api/v0/projects/p1/monitor")).text()
    assert WORKER_SECRET not in text
    found = ProjectMonitor.model_validate_json(text)
    assert found.worker is None
    assert found.worker_unavailable == "the worker refused (unavailable)"
    older, _ = await worker_at(aiohttp_server, refuse=404)
    client = await workspace(aiohttp_client, state_dir, static_dir, worker_url=older)
    found = ProjectMonitor.model_validate(
        await (await client.get("/api/v0/projects/p1/monitor")).json()
    )
    assert found.worker_unavailable == "the worker has no monitor figures (an older worker)"
    client = await workspace(aiohttp_client, state_dir, static_dir)
    found = ProjectMonitor.model_validate(
        await (await client.get("/api/v0/projects/p1/monitor")).json()
    )
    assert (found.worker, found.worker_unavailable) == (None, "no worker is configured")


async def test_the_monitor_refuses_bad_windows_and_unknown_projects(
    aiohttp_client: ClientFactory, state_dir: Path, static_dir: Path
) -> None:
    client = await workspace(aiohttp_client, state_dir, static_dir)
    await error_of(
        await client.get("/api/v0/projects/p1/monitor?window=30d"), 400, "invalid_request"
    )
    await error_of(
        await client.get("/api/v0/projects/p1/monitor?project=p2"), 400, "invalid_request"
    )
    await error_of(await client.get("/api/v0/projects/nope/monitor"), 404, "not_found")
    anonymous = await workspace(aiohttp_client, state_dir, static_dir, login=False)
    assert (await anonymous.get("/api/v0/projects/p1/monitor")).status == 401
