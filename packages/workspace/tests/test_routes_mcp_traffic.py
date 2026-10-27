# SPDX-License-Identifier: MIT
"""Inspected MCP traffic for the run page and the chat's tool rows: the worker's page as
it is, through the operator client, or null with a short reason; the worker's own error
text stays out."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from sandbox_science.contracts.mcp_traffic import WorkerMcpTraffic
from sandbox_science.testing import fake_worker_app
from sandbox_science.workspace.mcp_traffic import ProjectMcpTraffic

from .route_support import WORKER_SECRET, error_of, workspace

ClientFactory = Callable[..., Awaitable[TestClient[Any, Any]]]
ServerFactory = Callable[..., Awaitable[TestServer]]

PAGE = {
    "project_id": "p1",
    "calls": [
        {
            "seq": 4, "at": "2026-10-26T12:00:00Z", "request_id": "r1", "session": "a",
            "run": "run-1", "server": "papers", "method": "tools/call", "tool": "search",
            "decision": {"outcome": "admitted", "action": "allow"},
            "arguments": {"text": '{"q":"x"}', "truncated": False, "bytes": 9,
                          "sha256": "a" * 64},
            "answer": {
                "seq": 5, "at": "2026-10-26T12:00:01Z", "request_id": "r1", "status": 200,
                "content_type": "application/json", "mode": "whole", "bytes": 20,
                "complete": True, "sha256": "b" * 64, "excerpt": "you are now",
                "findings": [{"name": "you-are-now", "action": "alert", "offset": 0,
                              "excerpt": "you are now"}],
                "decision": {"action": "passed"},
            },
        }
    ],
    "next_after": None,
    "scanned": 2,
}  # fmt: skip


async def worker_at(
    aiohttp_server: ServerFactory, *, refuse: int | None = None
) -> tuple[str, list[dict[str, str]]]:
    asked: list[dict[str, str]] = []

    async def traffic(request: web.Request) -> web.Response:
        asked.append(dict(request.query))
        if refuse is not None:
            body = {"error": {"kind": "unavailable", "message": WORKER_SECRET}}
            return web.json_response(body, status=refuse)
        return web.json_response(PAGE)

    app = fake_worker_app()
    app.router.add_get("/v1/mcp-traffic", traffic)
    server = await aiohttp_server(app)
    return str(server.make_url("")).rstrip("/"), asked


async def test_the_route_passes_the_workers_page_with_its_filters(
    aiohttp_client: ClientFactory, aiohttp_server: ServerFactory, state_dir: Path, static_dir: Path
) -> None:
    url, asked = await worker_at(aiohttp_server)
    client = await workspace(aiohttp_client, state_dir, static_dir, worker_url=url)
    response = await client.get(
        "/api/v0/projects/p1/mcp-traffic?session=a&run=run-1&tool=search&after=3"
    )
    assert response.status == 200, await response.text()
    assert response.headers["Cache-Control"] == "no-store"
    found = ProjectMcpTraffic.model_validate(await response.json())
    assert asked == [
        {"project": "p1", "session": "a", "run": "run-1", "tool": "search", "after": "3"}
    ]
    assert found.traffic == WorkerMcpTraffic.model_validate(PAGE)
    assert found.unavailable is None


async def test_the_page_is_null_with_a_reason_when_the_worker_cannot_answer(
    aiohttp_client: ClientFactory, aiohttp_server: ServerFactory, state_dir: Path, static_dir: Path
) -> None:
    refusing, _ = await worker_at(aiohttp_server, refuse=503)
    client = await workspace(aiohttp_client, state_dir, static_dir, worker_url=refusing)
    text = await (await client.get("/api/v0/projects/p1/mcp-traffic")).text()
    assert WORKER_SECRET not in text
    found = ProjectMcpTraffic.model_validate_json(text)
    assert (found.traffic, found.unavailable) == (None, "the worker refused (unavailable)")
    client = await workspace(aiohttp_client, state_dir, static_dir)
    found = ProjectMcpTraffic.model_validate(
        await (await client.get("/api/v0/projects/p1/mcp-traffic")).json()
    )
    assert (found.traffic, found.unavailable) == (None, "no worker is configured")


async def test_the_route_refuses_malformed_queries_and_unknown_projects(
    aiohttp_client: ClientFactory, state_dir: Path, static_dir: Path
) -> None:
    client = await workspace(aiohttp_client, state_dir, static_dir)
    for query in ("project=p2", "after=-1", "run=..", "tool=a%20b", "session=a&session=b"):
        await error_of(
            await client.get(f"/api/v0/projects/p1/mcp-traffic?{query}"), 400, "invalid_request"
        )
    await error_of(await client.get("/api/v0/projects/nope/mcp-traffic"), 404, "not_found")
    anonymous = await workspace(aiohttp_client, state_dir, static_dir, login=False)
    assert (await anonymous.get("/api/v0/projects/p1/mcp-traffic")).status == 401
