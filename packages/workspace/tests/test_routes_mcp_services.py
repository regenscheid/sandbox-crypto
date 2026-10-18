# SPDX-License-Identifier: MIT
"""MCP inventory is read-only, project-scoped and never a live availability claim."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from aiohttp.test_utils import TestClient

from sandbox_science.orchestrator.projects import ProjectStore
from sandbox_science.workspace.mcp_services import McpInventory, ServiceConfig

from .route_support import workspace

ClientFactory = Callable[..., Awaitable[TestClient[Any, Any]]]


async def test_mcp_inventory_scopes_config_and_dates_run_observations(
    aiohttp_client: ClientFactory, state_dir: Path, static_dir: Path
) -> None:
    projects = ProjectStore(state_dir)
    earlier = next(projects.iter_runs("p1", "a"))
    record = projects.run("p1", "a", earlier.name)
    when = datetime(2026, 10, 18, 12, tzinfo=UTC)
    projects.save_run(
        record.model_copy(
            update={
                "run_id": "mcp-inventory-test",
                "started_at": when,
                "ended_at": when,
                "summaries": {
                    "connectors": {
                        "servers": {"papers": 3},
                        "unavailable": {"private": "test-secret-never-forwarded"},
                        "loaded": [{"name": "fetch", "sha256": "test-secret-never-forwarded"}],
                    }
                },
            }
        )
    )
    client = await workspace(
        aiohttp_client,
        state_dir,
        static_dir,
        mcp_project="p1",
        mcp_inventory=(
            ServiceConfig(name="papers", purpose="Retrieve research papers"),
            ServiceConfig(name="private", purpose="Private archive"),
        ),
    )
    response = await client.get("/api/v0/projects/p1/mcp-services")
    assert response.status == 200
    assert response.headers["Cache-Control"] == "no-store"
    body = await response.text()
    assert "test-secret-never-forwarded" not in body
    found = McpInventory.model_validate_json(body)
    assert [service.name for service in found.services] == ["papers", "private"]
    assert [
        (service.observation.status, service.observation.listed_tools)
        for service in found.services
        if service.observation
    ] == [("listed", 3), ("unavailable", None)]
    assert all(
        service.observation and service.observation.session == "a" for service in found.services
    )
    assert found.inspected_runs >= 1
    other = McpInventory.model_validate_json(
        await (await client.get("/api/v0/projects/p2/mcp-services")).text()
    )
    assert other.services == []
    assert (await client.get("/api/v0/projects/no-such-project/mcp-services")).status == 404
    assert (await client.get("/api/v0/projects/p1/mcp-services?probe=1")).status == 400


async def test_unconfigured_mcp_inventory_does_not_infer_services_from_runs(
    aiohttp_client: ClientFactory, state_dir: Path, static_dir: Path
) -> None:
    client = await workspace(aiohttp_client, state_dir, static_dir)
    found = McpInventory.model_validate_json(
        await (await client.get("/api/v0/projects/p1/mcp-services")).text()
    )
    assert found.services == []
    assert found.inspected_runs == 0


async def test_recent_delegated_runs_do_not_hide_main_service_observation(
    aiohttp_client: ClientFactory, state_dir: Path, static_dir: Path
) -> None:
    projects = ProjectStore(state_dir)
    earlier = next(projects.iter_runs("p1", "a"))
    record = projects.run("p1", "a", earlier.name)
    when = datetime(2026, 10, 18, 12, tzinfo=UTC)
    main = projects.save_run(
        record.model_copy(
            update={
                "run_id": "mcp-main",
                "started_at": when,
                "ended_at": when,
                "summaries": {"connectors": {"servers": {"papers": 4}}},
            }
        )
    )
    for index in range(9):
        started = when + timedelta(seconds=index + 1)
        projects.save_run(
            record.model_copy(
                update={
                    "run_id": f"mcp-delegated-{index}",
                    "role": "delegated",
                    "parent": main.name,
                    "delegation": f"delegation-{index}",
                    "started_at": started,
                    "ended_at": started,
                    "summaries": {},
                }
            )
        )
    client = await workspace(
        aiohttp_client,
        state_dir,
        static_dir,
        mcp_project="p1",
        mcp_inventory=(ServiceConfig(name="papers", purpose="Find papers"),),
    )
    found = McpInventory.model_validate_json(
        await (await client.get("/api/v0/projects/p1/mcp-services")).text()
    )
    assert found.services[0].observation is not None
    assert found.services[0].observation.run == main.name
    assert found.services[0].observation.listed_tools == 4
