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


async def test_explicit_metadata_check_is_same_origin_and_does_not_execute_tools(
    aiohttp_client: ClientFactory, state_dir: Path, static_dir: Path
) -> None:
    from aiohttp import web

    from sandbox_science.workspace.mcp_metadata import MetadataClient

    from .route_support import origin

    calls: list[str] = []
    healthy = True

    async def rpc(request: web.Request) -> web.Response:
        assert request.headers["Authorization"] == "Bearer test-secret-metadata-caller"
        body = await request.json()
        method = body["method"]
        calls.append(method)
        assert method != "tools/call"
        if not healthy:
            return web.Response(status=503)
        if method == "notifications/initialized":
            return web.Response(status=202)
        result = (
            {
                "protocolVersion": "2025-11-25",
                "capabilities": {},
                "serverInfo": {"name": "fixture", "version": "1"},
            }
            if method == "initialize"
            else {"tools": []}
        )
        return web.json_response(
            {"jsonrpc": "2.0", "id": body["id"], "result": result},
            headers={"Mcp-Session-Id": "metadata-session"},
        )

    async def metadata(request: web.Request) -> web.Response:
        assert request.headers["Authorization"] == "Bearer test-secret-metadata-caller"
        return web.Response(status=503)

    app = web.Application()
    app.router.add_post("/mcp/papers", rpc)
    app.router.add_get("/metadata/papers", metadata)
    proxy = await aiohttp_client(app)
    source = MetadataClient(str(proxy.make_url("")).rstrip("/"), "test-secret-metadata-caller")
    client = await workspace(
        aiohttp_client,
        state_dir,
        static_dir,
        mcp_project="p1",
        mcp_inventory=(ServiceConfig(name="papers", purpose="Research"),),
        mcp_metadata=source,
    )
    path = "/api/v0/projects/p1/mcp-services"
    first = await (await client.get(path)).json()
    assert first["services"][0]["proxy_status"] == "unavailable"
    assert calls == []
    assert (await client.post(path + "/papers/check", json={})).status == 403
    checked = await client.post(path + "/papers/check", json={}, headers=origin(client))
    assert checked.status == 200
    assert (await checked.json())["status"] == "listed"
    assert set(calls) == {"initialize", "notifications/initialized", "tools/list"}
    assert (
        await client.post(path + "/unknown/check", json={}, headers=origin(client))
    ).status == 404
    healthy = False
    assert (
        await (await client.post(path + "/papers/check", json={}, headers=origin(client))).json()
    )["status"] == "unavailable"
    healthy = True
    assert (
        await (await client.post(path + "/papers/check", json={}, headers=origin(client))).json()
    )["status"] == "listed"
    anonymous = await workspace(aiohttp_client, state_dir, static_dir, login=False)
    assert (
        await anonymous.post(path + "/papers/check", json={}, headers=origin(anonymous))
    ).status == 401


async def test_the_user_turns_a_configured_connector_off_and_back_on(
    aiohttp_client: ClientFactory, state_dir: Path, static_dir: Path
) -> None:
    client = await workspace(
        aiohttp_client,
        state_dir,
        static_dir,
        mcp_project="p1",
        mcp_inventory=(
            ServiceConfig(name="papers", purpose="Retrieve research papers"),
            ServiceConfig(name="web", purpose="Fetch web pages"),
        ),
    )
    path = "/api/v0/projects/p1/mcp-services/settings"
    origin = {"Origin": str(client.make_url("/")).rstrip("/")}
    first = await (await client.get(path)).json()
    assert (first["revision"], first["configured"], first["disabled"]) == (0, ["papers", "web"], [])

    async def change(body: dict[str, object], status: int = 200) -> Any:
        response = await client.post(path, json=body, headers=origin)
        assert response.status == status, await response.text()
        return await response.json()

    off = await change({"expected": 0, "disabled": ["web"], "operation_id": "off-1"})
    assert (off["revision"], off["disabled"], off["replayed"]) == (1, ["web"], False)
    again = await change({"expected": 0, "disabled": ["web"], "operation_id": "off-1"})
    assert (again["revision"], again["replayed"]) == (1, True)
    await change({"expected": 0, "disabled": [], "operation_id": "on-1"}, status=409)
    on = await change({"expected": 1, "disabled": [], "operation_id": "on-1"})
    assert (on["revision"], on["disabled"]) == (2, [])
    # Narrowing only: a service the launcher does not configure cannot be named.
    refused = await change(
        {"expected": 2, "disabled": ["elsewhere"], "operation_id": "x"}, status=400
    )
    assert "not a configured connector" in refused["error"]["message"]
    await change({"expected": 2, "disabled": ["web"], "operation_id": "y", "author": 1}, 400)
    for headers in ({}, {"Origin": "https://untrusted.invalid"}):
        response = await client.post(
            path, json={"expected": 2, "disabled": ["web"], "operation_id": "z"}, headers=headers
        )
        assert response.status == 403
    # Another project has no configured connectors to turn off.
    other = await (await client.get("/api/v0/projects/p2/mcp-services/settings")).json()
    assert other["configured"] == []
