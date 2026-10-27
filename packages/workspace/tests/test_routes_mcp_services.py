# SPDX-License-Identifier: MIT
"""MCP inventory is read-only, project-scoped and never a live availability claim; its
tools are the agent runtime's latest reported listing, which the worker keeps (D98)."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from aiohttp.test_utils import TestClient

from sandbox_science.contracts.classifications import ListedToolView, ReportedListing
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


async def test_each_service_shows_its_latest_canary_beside_the_users_check(
    aiohttp_client: ClientFactory, state_dir: Path, static_dir: Path
) -> None:
    from sandbox_science.orchestrator.mcp_canaries import CanaryResult, CanaryStore

    projects = ProjectStore(state_dir)
    canaries = CanaryStore(projects.control)
    when = datetime(2026, 10, 25, 12, tzinfo=UTC)
    canaries.record(CanaryResult("papers", when, "listed", 12, "d" * 64, 340, None))
    later = when + timedelta(minutes=15)
    canaries.record(CanaryResult("papers", later, "timeout", None, None, 15000, "timeout"))
    canaries.record(CanaryResult("unconfigured", later, "listed", 1, "e" * 64, 5, None))
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
    raw = await response.json()
    assert raw["services"][0]["canary"] == {
        "at": "2026-10-25T12:15:00Z",
        "status": "timeout",
        "tool_count": None,
        "elapsed_ms": 15000,
    }
    found = McpInventory.model_validate(raw)
    papers, private = found.services
    assert papers.canary is not None
    assert papers.canary.at == later
    # No canary for a service yet; the user's own check is a separate field, untouched.
    assert private.canary is None
    assert papers.availability is None
    # A server the project does not configure stays out, canary or not.
    assert [service.name for service in found.services] == ["papers", "private"]
    assert "digest" not in await response.text()


async def test_the_users_check_is_same_origin_and_asks_the_control_service(
    aiohttp_client: ClientFactory, state_dir: Path, static_dir: Path
) -> None:
    """Check availability: one listing by the agent runtime's client, asked of the control
    service with the workspace's token (D98); it never calls a tool, and only its status
    reaches the browser."""
    from aiohttp import web

    from sandbox_science.workspace.mcp_metadata import AvailabilityClient

    from .route_support import origin

    token = "test-secret-workspace-check-0123456789abcdef"
    asked: list[str] = []
    healthy = True

    async def check(request: web.Request) -> web.Response:
        assert request.headers["Authorization"] == f"Bearer {token}"
        asked.append((await request.json())["server"])
        if not healthy:
            return web.Response(status=503)
        return web.json_response(
            {"status": "listed", "tool_count": 4, "digest": "d" * 64, "error_kind": None}
        )

    app = web.Application()
    app.router.add_post("/mcp-check", check)
    control = await aiohttp_client(app)
    checks = AvailabilityClient(str(control.make_url("")).rstrip("/"), token, interval=0)
    client = await workspace(
        aiohttp_client,
        state_dir,
        static_dir,
        mcp_project="p1",
        mcp_inventory=(ServiceConfig(name="papers", purpose="Research"),),
        mcp_checks=checks,
    )
    path = "/api/v0/projects/p1/mcp-services"
    first = await (await client.get(path)).json()
    assert first["checks"] is True
    assert first["services"][0]["availability"] is None
    assert asked == []  # viewing checks nothing
    assert (await client.post(path + "/papers/check", json={})).status == 403
    checked = await client.post(path + "/papers/check", json={}, headers=origin(client))
    assert checked.status == 200
    said = await checked.json()
    assert (said["status"], said["tool_count"]) == ("listed", 4)
    assert asked == ["papers"]
    shown = await (await client.get(path)).json()
    assert shown["services"][0]["availability"]["status"] == "listed"
    assert token not in str(shown)
    assert "digest" not in str(shown)
    assert (
        await client.post(path + "/unknown/check", json={}, headers=origin(client))
    ).status == 404
    healthy = False
    assert (
        await (await client.post(path + "/papers/check", json={}, headers=origin(client))).json()
    )["status"] == "unavailable"
    anonymous = await workspace(aiohttp_client, state_dir, static_dir, login=False)
    assert (
        await anonymous.post(path + "/papers/check", json={}, headers=origin(anonymous))
    ).status == 401
    # Without a check configured, the check says so; the view still shows the services.
    unchecked = await workspace(
        aiohttp_client,
        state_dir,
        static_dir,
        mcp_project="p1",
        mcp_inventory=(ServiceConfig(name="papers", purpose="Research"),),
    )
    refused = await unchecked.post(path + "/papers/check", json={}, headers=origin(unchecked))
    assert refused.status == 503
    assert (await (await unchecked.get(path)).json())["checks"] is False


class Listings:
    """The worker's reported listings, as its client reads them."""

    def __init__(self, found: list[ReportedListing] | Exception) -> None:
        self.found = found
        self.asked: list[str] = []

    async def mcp_listings(self, project_id: str) -> list[ReportedListing]:
        self.asked.append(project_id)
        if isinstance(self.found, Exception):
            raise self.found
        return self.found


REPORTED = ReportedListing(
    server="papers",
    reported_at=datetime(2026, 10, 26, 9, tzinfo=UTC),
    session="a",
    run="main-1",
    listing="f" * 64,
    drifted=False,
    tools=[
        ListedToolView(name="search", hint="read", effect="read", classified=True, action="allow"),
        ListedToolView(
            name="submit", hint="write", effect="write", classified=True, action="escalate"
        ),
        ListedToolView(
            name="new_tool", hint="read", effect="read", classified=False, action="deny"
        ),
    ],
)


async def test_the_tools_are_the_runtimes_latest_reported_listing(
    aiohttp_client: ClientFactory, state_dir: Path, static_dir: Path
) -> None:
    """The Tools tab reads the latest listing the agent runtime's client reported, which the
    worker keeps, each tool with its class in force and its decision; a service with none
    says that no listing has been reported yet."""
    source = Listings([REPORTED])
    client = await workspace(
        aiohttp_client,
        state_dir,
        static_dir,
        mcp_project="p1",
        mcp_inventory=(
            ServiceConfig(name="papers", purpose="Research"),
            ServiceConfig(name="private", purpose="Private archive"),
        ),
        mcp_listings=source,
    )
    found = McpInventory.model_validate_json(
        await (await client.get("/api/v0/projects/p1/mcp-services")).text()
    )
    papers, private = found.services
    assert papers.listing_state == "reported"
    assert papers.listing == REPORTED
    assert papers.listing is not None
    assert [(tool.name, tool.action) for tool in papers.listing.tools] == [
        ("search", "allow"),
        ("submit", "escalate"),
        ("new_tool", "deny"),
    ]
    assert (private.listing_state, private.listing) == ("none", None)
    assert source.asked == ["p1"]
    # Another project's view never asks the worker for this one's.
    await client.get("/api/v0/projects/p2/mcp-services")
    assert source.asked == ["p1"]


async def test_a_worker_that_cannot_be_asked_leaves_the_listing_unavailable(
    aiohttp_client: ClientFactory, state_dir: Path, static_dir: Path
) -> None:
    from sandbox_science.contracts import ErrorKind
    from sandbox_science.orchestrator.worker_client import WorkerAPIError

    for source in (Listings(WorkerAPIError(ErrorKind.UNAVAILABLE, "down", 503)), None):
        client = await workspace(
            aiohttp_client,
            state_dir,
            static_dir,
            mcp_project="p1",
            mcp_inventory=(ServiceConfig(name="papers", purpose="Research"),),
            mcp_listings=source,
        )
        response = await client.get("/api/v0/projects/p1/mcp-services")
        assert response.status == 200
        (papers,) = McpInventory.model_validate_json(await response.text()).services
        assert (papers.listing_state, papers.listing) == ("unavailable", None)


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
