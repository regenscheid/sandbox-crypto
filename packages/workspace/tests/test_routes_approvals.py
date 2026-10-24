# SPDX-License-Identifier: MIT
"""The user's answers to permission requests (approvals.py), against stand-ins for the MCP
proxy's operator routes and the worker's permission routes: what the chat is shown, what
each decision sends, the grants it stores within the request's ceiling, and the requests
the relay answers from grants (D97)."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from sandbox_science.contracts import new_id
from sandbox_science.orchestrator.delegation.records import Delegation, DelegationRecords
from sandbox_science.orchestrator.delegation.requests import ChildRequests
from sandbox_science.orchestrator.projects import ProjectStore
from sandbox_science.testing.worker_api import fake_worker_app
from sandbox_science.workspace import approvals
from sandbox_science.workspace.approvals import ApprovalClient

from .route_support import WORKER_TOKEN, workspace

ClientFactory = Callable[..., Awaitable[TestClient[Any, Any]]]
ServerFactory = Callable[..., Awaitable[TestServer]]
OPERATOR = "test-secret-operator-token-0123456789"
ANSWER = "/api/v0/projects/p1/permission-requests/{}/answer"


class FakeProxy:
    """The proxy's operator routes: its waiting requests, and the answers it was sent."""

    def __init__(self) -> None:
        self.waiting: list[dict[str, Any]] = []
        self.answers: list[tuple[int, dict[str, Any]]] = []
        self.tokens: list[str] = []

    def hold(
        self,
        seq: int,
        tool: str,
        session: str = "a",
        server: str = "papers",
        *,
        effect: str = "write",
        max_grant: str = "project",
        caller_kind: str = "agent",
        summary: dict[str, str] | None = None,
    ) -> None:
        self.waiting.append(
            {
                # Every field the proxy's listing sends (mcp_proxy.proxy), no fewer.
                "seq": seq, "server": server, "tool": tool, "method": "tools/call",
                "policy": "sha256:" + "a" * 64,
                "session": session, "rule": None, "reason": "escalated",
                "recorded_at": "2026-10-21T18:00:00+00:00",
                "expires_at": "2026-10-21T18:30:00+00:00", "timed_out": False,
                "on_timeout": "deny", "caller_kind": caller_kind, "effect": effect,
                "max_grant": max_grant, "summary": summary or {},
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


class FakeWorker:
    """The worker's permission routes: a project's waiting network and package requests."""

    def __init__(self) -> None:
        self.waiting: list[dict[str, Any]] = []
        self.answers: list[tuple[int, dict[str, Any]]] = []

    def network(self, seq: int, host: str, method: str = "GET", path: str = "/data") -> None:
        self._add(
            seq, "network", None, "project" if method in ("GET", "HEAD") else "once",
            http={"method": method, "host": host, "port": 443, "path": path, "size": 0,
                  "content_type": None, "fields": {}},
        )  # fmt: skip

    def packages(
        self, seq: int, sources: list[tuple[str, str | None]], session: str | None = "a"
    ) -> None:
        self._add(
            seq, "packages", session, "project", caller="agent",
            packages={"sources": [{"ecosystem": e, "channel": c} for e, c in sources],
                      "files": 3, "asked": "Conda: sage", "purpose": "the agent's words",
                      "package": None},
        )  # fmt: skip

    def _add(
        self, seq: int, kind: str, session: str | None, max_grant: str, *,
        caller: str = "code", **cards: object,
    ) -> None:  # fmt: skip
        self.waiting.append(
            {
                "seq": seq, "kind": kind, "project_id": "p1", "environment_id": "env1",
                "session_id": session, "caller_kind": caller, "policy": "sha256:" + "b" * 64,
                "rule": "r", "reason": "escalated", "max_grant": max_grant,
                "http": None, "packages": None, **cards,
                "recorded_at": "2026-10-21T18:00:00+00:00",
                "expires_at": "2026-10-21T18:30:00+00:00", "timed_out": False,
                "on_timeout": "deny",
            }
        )  # fmt: skip

    def application(self) -> web.Application:
        app = fake_worker_app()
        app.router.add_get("/v1/permission-requests", self.listing)
        app.router.add_post("/v1/permission-requests/{seq}/answer", self.answer)
        return app

    async def listing(self, request: web.Request) -> web.Response:
        assert request.headers["Authorization"] == f"Bearer {WORKER_TOKEN}"
        assert dict(request.query) == {"project": "p1"}
        return web.json_response({"requests": self.waiting})

    async def answer(self, request: web.Request) -> web.Response:
        seq = int(request.match_info["seq"])
        body = await request.json()
        self.answers.append((seq, body))
        self.waiting = [item for item in self.waiting if item["seq"] != seq]
        kind = "approval" if body["approve"] else "denial"
        return web.json_response({"seq": seq, "answer": 50 + seq, "kind": kind}, status=201)


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


@pytest.fixture
async def worker(aiohttp_server: ServerFactory) -> tuple[FakeWorker, str]:
    fake = FakeWorker()
    server = await aiohttp_server(fake.application())
    return fake, str(server.make_url("/")).rstrip("/")


@pytest.fixture(autouse=True)
def quick_relay(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(approvals, "RELAY_SECONDS", 0.05)


async def client_for(
    aiohttp_client: ClientFactory,
    state_dir: Path,
    static_dir: Path,
    url: str,
    worker_url: str | None = None,
) -> TestClient[Any, Any]:
    return await workspace(
        aiohttp_client,
        state_dir,
        static_dir,
        mcp_project="p1",
        mcp_approvals=ApprovalClient(url, OPERATOR),
        worker_url=worker_url,
    )


async def post(client: TestClient[Any, Any], path: str, body: object, status: int = 200) -> Any:
    response = await client.post(
        path, json=body, headers={"Origin": str(client.make_url("/")).rstrip("/")}
    )
    assert response.status == status, await response.text()
    return await response.json()


async def shown(client: TestClient[Any, Any], session: str = "a") -> dict[int, Any]:
    response = await client.get(f"/api/v0/projects/p1/sessions/{session}/permission-requests")
    assert response.status == 200, await response.text()
    return {item["seq"]: item for item in (await response.json())["requests"]}


async def grants(client: TestClient[Any, Any]) -> list[dict[str, Any]]:
    return (await (await client.get("/api/v0/projects/p1/grants")).json())["grants"]


async def until(condition: Callable[[], bool]) -> None:
    for _ in range(200):
        if condition():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("the condition never held")


async def test_a_card_offers_only_the_scopes_its_ceiling_allows(
    aiohttp_client: ClientFactory, state_dir: Path, static_dir: Path, proxy: Any
) -> None:
    fake, url = proxy
    client = await client_for(aiohttp_client, state_dir, static_dir, url)
    fake.hold(1, "submit_feedback", summary={"title": "A note"})
    fake.hold(2, "notify", effect="communicate", max_grant="session")
    fake.hold(3, "mystery", effect="unclassified", max_grant="project")  # never past once
    fake.hold(4, "refresh", effect="admin", max_grant="once")
    fake.hold(5, "submit_feedback", caller_kind="code")  # research code's call
    fake.hold(6, "crawl", session="b")
    found = await shown(client)
    assert sorted(found) == [1, 2, 3, 4, 5]
    assert found[1]["scopes"] == ["once", "session", "project"]
    assert found[1]["summary"] == {"title": "A note"}
    assert (found[1]["effect"], found[1]["kind"]) == ("write", "mcp")
    assert found[2]["scopes"] == ["once", "session"]
    assert found[3]["scopes"] == ["once"]
    assert found[4]["scopes"] == ["once"]
    assert found[5]["scopes"] == ["once"]  # no class grant rides on research code
    assert fake.tokens[-1] == f"Bearer {OPERATOR}"
    assert OPERATOR not in str(found)


async def test_each_decision_sends_its_answer_and_stores_its_grant(
    aiohttp_client: ClientFactory, state_dir: Path, static_dir: Path, proxy: Any
) -> None:
    fake, url = proxy
    client = await client_for(aiohttp_client, state_dir, static_dir, url)
    fake.hold(1, "crawl")
    fake.hold(2, "submit_feedback")
    fake.hold(3, "notify", effect="communicate", max_grant="session")
    fake.hold(4, "x")
    once = await post(client, ANSWER.format(1), {"decision": "once"})
    assert (once["decision"], once["granted"]) == ("once", [])
    project = await post(client, ANSWER.format(2), {"decision": "project"})
    [granted] = project["granted"]
    assert (granted["kind"], granted["server"], granted["effect"], granted["scope"]) == (
        "mcp", "papers", "write", "project",
    )  # fmt: skip
    session = await post(client, ANSWER.format(3), {"decision": "session", "hours": 2})
    assert session["granted"][0]["session"] == "a"
    assert session["granted"][0]["expires_at"] is not None
    await post(client, ANSWER.format(4), {"decision": "deny"})
    assert fake.answers == [
        (1, {"approve": True, "scope": "call", "hours": approvals.ONCE_HOURS}),
        # A grant answers the call itself: other calls are the relay's, one by one.
        (2, {"approve": True, "scope": "call", "hours": approvals.GRANT_HOURS}),
        (3, {"approve": True, "scope": "call", "hours": approvals.GRANT_HOURS}),
        (4, {"approve": False, "scope": "call", "hours": None}),
    ]
    listed = await grants(client)
    assert sorted((g["effect"], g["scope"]) for g in listed) == [
        ("communicate", "session"), ("write", "project"),
    ]  # fmt: skip
    assert all(g["last_used_at"] is not None and g["uses"] == 1 for g in listed)
    await post(client, ANSWER.format(1), {"decision": "once"}, status=409)  # answered already
    await post(client, ANSWER.format(5), {"decision": "server"}, status=400)  # no such scope
    await post(client, ANSWER.format(5), {"decision": "always"}, status=400)


async def test_a_scope_above_the_ceiling_is_refused_whatever_the_client_sends(
    aiohttp_client: ClientFactory, state_dir: Path, static_dir: Path, proxy: Any
) -> None:
    fake, url = proxy
    client = await client_for(aiohttp_client, state_dir, static_dir, url)
    fake.hold(1, "notify", effect="communicate", max_grant="session")
    fake.hold(2, "mystery", effect="unclassified", max_grant="project")
    fake.hold(3, "submit_feedback", caller_kind="code")
    await post(client, ANSWER.format(1), {"decision": "project"}, status=403)
    await post(client, ANSWER.format(2), {"decision": "session"}, status=403)
    await post(client, ANSWER.format(2), {"decision": "project"}, status=403)
    await post(client, ANSWER.format(3), {"decision": "project"}, status=403)
    assert fake.answers == []
    assert await grants(client) == []
    await post(client, ANSWER.format(2), {"decision": "once"})  # once is always open


async def test_a_class_grant_covers_a_new_tool_of_that_class_only(
    aiohttp_client: ClientFactory, state_dir: Path, static_dir: Path, proxy: Any
) -> None:
    fake, url = proxy
    client = await client_for(aiohttp_client, state_dir, static_dir, url)
    fake.hold(1, "submit_feedback")
    await post(client, ANSWER.format(1), {"decision": "project"})
    fake.hold(2, "added_later")  # a tool the server added, which the policy classes write
    fake.hold(3, "notify", effect="communicate", max_grant="session")  # another class
    fake.hold(4, "submit_feedback", server="web")  # another server
    fake.hold(5, "submit_feedback", caller_kind="code")  # research code's
    fake.hold(6, "mystery", effect="unclassified", max_grant="project")
    fake.hold(7, "submit_feedback", effect="write", max_grant="once")  # ceiling lowered
    await until(lambda: len(fake.answers) == 2)
    await asyncio.sleep(0.2)  # the relay had time for the others too
    assert [seq for seq, _ in fake.answers] == [1, 2]
    assert fake.answers[1] == (
        2, {"approve": True, "scope": "call", "hours": approvals.GRANT_HOURS},
    )  # fmt: skip
    [grant] = await grants(client)
    assert grant["uses"] == 2


async def test_a_session_grant_covers_its_session_only(
    aiohttp_client: ClientFactory, state_dir: Path, static_dir: Path, proxy: Any
) -> None:
    fake, url = proxy
    client = await client_for(aiohttp_client, state_dir, static_dir, url)
    fake.hold(1, "notify", effect="communicate", max_grant="session")
    await post(client, ANSWER.format(1), {"decision": "session"})
    fake.hold(2, "notify", effect="communicate", max_grant="session", session="b")
    fake.hold(3, "notify", effect="communicate", max_grant="session")
    await until(lambda: len(fake.answers) == 2)
    await asyncio.sleep(0.2)
    assert [seq for seq, _ in fake.answers] == [1, 3]


async def test_revoking_a_grant_stops_it_covering_at_once(
    aiohttp_client: ClientFactory, state_dir: Path, static_dir: Path, proxy: Any
) -> None:
    fake, url = proxy
    client = await client_for(aiohttp_client, state_dir, static_dir, url)
    fake.hold(1, "submit_feedback")
    [grant] = (await post(client, ANSWER.format(1), {"decision": "project"}))["granted"]
    revoke = f"/api/v0/projects/p1/grants/{grant['grant_id']}/revoke"
    assert (await post(client, revoke, {}))["grants"] == []
    assert (await post(client, revoke, {}))["grants"] == []  # again: nothing changes
    await post(client, "/api/v0/projects/p1/grants/grant-nope/revoke", {}, status=404)
    fake.hold(2, "submit_feedback")
    await asyncio.sleep(0.3)
    assert [seq for seq, _ in fake.answers] == [1]  # the user is asked again
    assert 2 in await shown(client)


async def test_answers_need_the_same_origin_and_the_configured_project(
    aiohttp_client: ClientFactory, state_dir: Path, static_dir: Path, proxy: Any
) -> None:
    fake, url = proxy
    fake.hold(1, "crawl")
    client = await client_for(aiohttp_client, state_dir, static_dir, url)
    for headers in ({}, {"Origin": "https://untrusted.invalid"}):
        response = await client.post(ANSWER.format(1), json={"decision": "once"}, headers=headers)
        assert response.status == 403
    await post(
        client, "/api/v0/projects/p2/permission-requests/1/answer", {"decision": "once"}, 404
    )
    # A session the project does not have is not found; another project has no requests.
    assert (await client.get("/api/v0/projects/p1/sessions/x/permission-requests")).status == 404
    assert (await client.get("/api/v0/projects/p2/sessions/x/permission-requests")).status == 404
    assert fake.answers == []


async def test_network_and_package_requests_are_cards_answered_in_the_workspace(
    aiohttp_client: ClientFactory,
    state_dir: Path,
    static_dir: Path,
    proxy: Any,
    worker: tuple[FakeWorker, str],
) -> None:
    fake, url = proxy
    desk, worker_url = worker
    client = await client_for(aiohttp_client, state_dir, static_dir, url, worker_url)
    desk.network(10, "data.example.org")
    desk.network(11, "api.example.org", method="POST", path="/submit")
    desk.packages(12, [("conda", "conda-forge")])
    desk.packages(13, [("pypi", None)], session="b")  # another session's install
    found = await shown(client)
    assert sorted(found) == [10, 11, 12]  # requests naming no session show in every session
    assert (found[10]["kind"], found[10]["scopes"]) == ("network", ["once", "project"])
    assert found[10]["http"]["host"] == "data.example.org"
    assert found[11]["scopes"] == ["once"]  # a write from research code: once only
    assert found[12]["scopes"] == ["once", "session", "project"]
    assert found[12]["packages"]["purpose"] == "the agent's words"
    network = {"decision": "project", "kind": "network"}
    [host] = (await post(client, ANSWER.format(10), network))["granted"]
    assert (host["kind"], host["host"], host["methods"]) == ("network", "data.example.org", "read")
    await post(client, ANSWER.format(11), {"decision": "project", "kind": "network"}, 403)
    await post(client, ANSWER.format(11), {"decision": "once", "kind": "network"})
    await post(client, ANSWER.format(12), {"decision": "project", "kind": "packages"})
    await post(client, ANSWER.format(12), {"decision": "once", "kind": "mcp"}, 409)  # not MCP's
    assert desk.answers == [
        (10, {"approve": True, "scope": "host", "hours": approvals.HOST_HOURS}),
        (11, {"approve": True, "scope": "request", "hours": approvals.ONCE_HOURS}),
        (12, {"approve": True, "scope": "files", "hours": None}),
    ]
    assert fake.answers == []


async def test_the_relay_answers_network_and_package_requests_from_grants(
    aiohttp_client: ClientFactory,
    state_dir: Path,
    static_dir: Path,
    proxy: Any,
    worker: tuple[FakeWorker, str],
) -> None:
    fake, url = proxy
    desk, worker_url = worker
    client = await client_for(aiohttp_client, state_dir, static_dir, url, worker_url)
    desk.network(10, "data.example.org")
    desk.packages(11, [("conda", "conda-forge")])
    await post(client, ANSWER.format(10), {"decision": "project", "kind": "network"})
    await post(client, ANSWER.format(11), {"decision": "project", "kind": "packages"})
    desk.network(20, "data.example.org", path="/other")  # the granted host: answered
    desk.network(21, "data.example.org", method="POST")  # a write: the user's to answer
    desk.network(22, "elsewhere.example.org")
    desk.packages(23, [("conda", "conda-forge")])  # the granted source
    desk.packages(24, [("conda", "conda-forge"), ("conda", "bioconda")])  # one ungranted
    # The agent's MCP call naming the granted host is not covered by the host's grant.
    fake.hold(25, "fetch", summary={"url": "https://data.example.org/x"})
    await until(lambda: len(desk.answers) == 4)
    await asyncio.sleep(0.2)
    assert [seq for seq, _ in desk.answers] == [10, 11, 20, 23]
    assert desk.answers[2] == (
        20, {"approve": True, "scope": "host", "hours": approvals.HOST_HOURS},
    )  # fmt: skip
    assert fake.answers == []


async def test_a_worker_without_permission_routes_still_shows_tool_calls(
    aiohttp_client: ClientFactory,
    aiohttp_server: ServerFactory,
    state_dir: Path,
    static_dir: Path,
    proxy: Any,
) -> None:
    fake, url = proxy
    server = await aiohttp_server(fake_worker_app())
    client = await client_for(
        aiohttp_client, state_dir, static_dir, url, str(server.make_url("/")).rstrip("/")
    )
    fake.hold(1, "crawl")
    assert sorted(await shown(client)) == [1]


def test_the_listing_is_read_with_every_field_the_proxy_sends() -> None:
    # The proxy's listing (test_proxy.py pins the same set): a field it adds must be
    # declared here, or the strict model refuses every listing. The rest are the
    # workspace's own: ``child`` from its records of delegated runs' requests (D99), the
    # kind, the worker's request summaries and the scopes it offers (D97), the source
    # that holds the request (D98), and what a detection rule's hold saw (D103).
    assert set(approvals.PendingApproval.model_fields) - {
        "child", "kind", "http", "packages", "scopes", "source", "hold",
    } == {
        "seq", "server", "method", "tool", "policy", "session", "run", "rule", "reason",
        "recorded_at", "expires_at", "timed_out", "on_timeout", "caller_kind", "effect",
        "max_grant", "summary",
    }  # fmt: skip


async def test_a_childs_held_call_names_the_child_and_its_withdrawal_only_refuses(
    aiohttp_client: ClientFactory, state_dir: Path, static_dir: Path, proxy: Any
) -> None:
    """D99: the card says which delegated agent asked, with its parent's brief and its own
    words; a request the parent withdrew is not shown, cannot be approved, and the relay
    denies it at the proxy, once."""
    delegation = new_id("dlg")
    store = ProjectStore(state_dir)
    try:
        DelegationRecords(store.control).start(
            "p1",
            "a",
            Delegation(
                delegation_id=delegation, parent="root", run="child", kind="task",
                brief={"task": "Find the SNOVA parameter sets."},
                started_at=datetime.now(UTC),
            ),
        )  # fmt: skip
        requests = ChildRequests(store.control)
        for seq in (21, 22):
            requests.record(
                "p1", "a", run="child", delegation=delegation, request=seq,
                server="papers", tool="fetch", words=f"Fetching for step {seq}.",
            )  # fmt: skip
        requests.withdraw("p1", "a", parent="root", delegation=delegation, request=22)
    finally:
        store.close()
    fake, url = proxy
    fake.hold(20, "crawl")  # the main run's
    fake.hold(21, "fetch")
    fake.hold(22, "fetch")
    client = await client_for(aiohttp_client, state_dir, static_dir, url)
    by_seq = await shown(client)
    assert set(by_seq) == {20, 21}
    assert by_seq[20]["child"] is None
    assert by_seq[21]["child"] == {
        "delegation_id": delegation,
        "run": "child",
        "kind": "task",
        "brief": "Find the SNOVA parameter sets.",
        "words": "Fetching for step 21.",
    }
    refused = await post(client, ANSWER.format(22), {"decision": "once"}, status=409)
    assert "withdrawn" in str(refused)
    await until(lambda: bool(fake.answers))
    assert fake.answers == [(22, {"approve": False, "scope": "call", "hours": None})]
    # The child's other request is still the user's to answer.
    await post(client, ANSWER.format(21), {"decision": "once"})
    assert fake.answers[-1] == (
        21,
        {"approve": True, "scope": "call", "hours": approvals.ONCE_HOURS},
    )
