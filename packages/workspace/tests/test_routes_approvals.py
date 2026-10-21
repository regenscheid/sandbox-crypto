# SPDX-License-Identifier: MIT
"""The user's answers to held connector tool calls (approvals.py), against a stand-in for
the MCP proxy's operator routes: what the chat is shown, what each decision sends, and
the remembered approvals the relay gives the proxy again."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from sandbox_science.workspace import approvals
from sandbox_science.workspace.approvals import ApprovalClient

from .route_support import workspace

ClientFactory = Callable[..., Awaitable[TestClient[Any, Any]]]
OPERATOR = "test-secret-operator-token-0123456789"


class FakeProxy:
    """The proxy's operator routes: its waiting requests, and the answers it was sent."""

    def __init__(self) -> None:
        self.waiting: list[dict[str, Any]] = []
        self.answers: list[tuple[int, dict[str, Any]]] = []
        self.tokens: list[str] = []

    def hold(self, seq: int, tool: str, session: str = "a", server: str = "papers") -> None:
        self.waiting.append(
            {
                "seq": seq, "server": server, "tool": tool, "method": "tools/call",
                "session": session, "rule": f"mcp.servers.{server}.tools.escalate[{tool}]",
                "reason": "escalated", "recorded_at": "2026-10-21T18:00:00+00:00",
                "expires_at": "2026-10-21T18:30:00+00:00", "timed_out": False,
                "on_timeout": "deny",
            }
        )  # fmt: skip

    async def listing(self, request: web.Request) -> web.Response:
        self.tokens.append(request.headers.get("Authorization", ""))
        return web.json_response({"requests": self.waiting})

    async def answer(self, request: web.Request) -> web.Response:
        seq = int(request.match_info["seq"])
        if not any(item["seq"] == seq for item in self.waiting):
            return web.json_response({"error": "no such permission request"}, status=404)
        self.answers.append((seq, await request.json()))
        self.waiting = [item for item in self.waiting if item["seq"] != seq]
        return web.json_response({"answer": 99, "kind": "approval"}, status=201)


@pytest.fixture
async def proxy() -> Any:
    fake = FakeProxy()
    app = web.Application()
    app.router.add_get("/permission-requests", fake.listing)
    app.router.add_post("/permission-requests/{seq}/answer", fake.answer)
    server = TestServer(app, host="127.0.0.1")
    await server.start_server()
    try:
        yield fake, str(server.make_url("")).rstrip("/")
    finally:
        await server.close()


async def client_for(
    aiohttp_client: ClientFactory, state_dir: Path, static_dir: Path, url: str
) -> TestClient[Any, Any]:
    return await workspace(
        aiohttp_client,
        state_dir,
        static_dir,
        mcp_project="p1",
        mcp_approvals=ApprovalClient(url, OPERATOR),
    )


async def post(client: TestClient[Any, Any], path: str, body: object, status: int = 200) -> Any:
    response = await client.post(
        path, json=body, headers={"Origin": str(client.make_url("/")).rstrip("/")}
    )
    assert response.status == status, await response.text()
    return await response.json()


async def test_a_session_sees_its_held_calls_and_each_decision_sends_its_scope(
    aiohttp_client: ClientFactory, state_dir: Path, static_dir: Path, proxy: Any
) -> None:
    fake, url = proxy
    client = await client_for(aiohttp_client, state_dir, static_dir, url)
    for seq, tool, session in (
        (1, "crawl", "a"),
        (2, "fetch", "a"),
        (3, "crawl", "b"),
        (4, "x", "a"),
    ):
        fake.hold(seq, tool, session)
    shown = await (await client.get("/api/v0/projects/p1/sessions/a/permission-requests")).json()
    assert [item["seq"] for item in shown["requests"]] == [1, 2, 4]
    assert fake.tokens[-1] == f"Bearer {OPERATOR}"
    assert OPERATOR not in str(shown)
    path = "/api/v0/projects/p1/permission-requests/{}/answer"
    once = await post(client, path.format(1), {"decision": "once"})
    assert once == {"seq": 1, "decision": "once", "remembered": None}
    await post(client, path.format(2), {"decision": "project"})
    await post(client, path.format(3), {"decision": "server"})
    await post(client, path.format(4), {"decision": "deny"})
    assert fake.answers == [
        (1, {"approve": True, "scope": "call", "hours": approvals.ONCE_HOURS}),
        (2, {"approve": True, "scope": "tool", "hours": approvals.GRANT_HOURS}),
        (3, {"approve": True, "scope": "server", "hours": approvals.GRANT_HOURS}),
        (4, {"approve": False, "scope": "call", "hours": None}),
    ]
    remembered = await (await client.get("/api/v0/projects/p1/approvals/settings")).json()
    assert remembered["grants"] == [
        {"server": "papers", "tool": None},
        {"server": "papers", "tool": "fetch"},
    ]
    await post(client, path.format(1), {"decision": "once"}, status=409)  # answered already
    await post(client, path.format(5), {"decision": "always"}, status=400)


async def test_answers_need_the_same_origin_and_the_configured_project(
    aiohttp_client: ClientFactory, state_dir: Path, static_dir: Path, proxy: Any
) -> None:
    fake, url = proxy
    fake.hold(1, "crawl")
    client = await client_for(aiohttp_client, state_dir, static_dir, url)
    for headers in ({}, {"Origin": "https://untrusted.invalid"}):
        response = await client.post(
            "/api/v0/projects/p1/permission-requests/1/answer",
            json={"decision": "once"},
            headers=headers,
        )
        assert response.status == 403
    await post(
        client, "/api/v0/projects/p2/permission-requests/1/answer", {"decision": "once"}, 404
    )
    # A session the project does not have is not found; another project has no requests.
    assert (await client.get("/api/v0/projects/p1/sessions/x/permission-requests")).status == 404
    assert (await client.get("/api/v0/projects/p2/sessions/x/permission-requests")).status == 404
    assert fake.answers == []


async def test_the_relay_gives_remembered_approvals_again_and_forgets_removed_ones(
    aiohttp_client: ClientFactory,
    state_dir: Path,
    static_dir: Path,
    proxy: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(approvals, "RELAY_SECONDS", 0.05)
    fake, url = proxy
    client = await client_for(aiohttp_client, state_dir, static_dir, url)
    settings = "/api/v0/projects/p1/approvals/settings"
    grants = [{"server": "papers", "tool": "crawl"}, {"server": "web", "tool": None}]
    await post(client, settings, {"expected": 0, "grants": grants, "operation_id": "g1"})
    fake.hold(7, "crawl")  # covered by the tool's grant
    fake.hold(8, "anything", server="web")  # covered by the server's
    fake.hold(9, "fetch")  # not covered: it waits for the user
    for _ in range(100):
        if len(fake.answers) == 2:
            break
        await asyncio.sleep(0.02)
    assert sorted(fake.answers) == [
        (7, {"approve": True, "scope": "tool", "hours": approvals.GRANT_HOURS}),
        (8, {"approve": True, "scope": "server", "hours": approvals.GRANT_HOURS}),
    ]
    await post(client, settings, {"expected": 1, "grants": [], "operation_id": "g2"})
    fake.hold(10, "crawl")  # its grant was removed: the user is asked again
    await asyncio.sleep(0.2)
    assert [seq for seq, _ in fake.answers] == [7, 8] or sorted(s for s, _ in fake.answers) == [
        7,
        8,
    ]
