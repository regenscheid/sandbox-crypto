# SPDX-License-Identifier: MIT
"""The harness's MCP connectors (D-08) against the real MCP proxy and its fake server.

The proxy runs in this process in front of ``sandbox_science.testing.mcp``'s fake MCP
server over TLS, with a policy bound to the project, as its own tests run it. Every
token is synthetic; no real server or credential is involved.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import pytest
import pytest_asyncio
from aiohttp import web
from aiohttp.test_utils import TestServer

from sandbox_science.mcp_proxy.callers import Caller, Callers, token_digest
from sandbox_science.mcp_proxy.proxy import McpProxy
from sandbox_science.mcp_proxy.sessions import Sessions
from sandbox_science.mcp_proxy.store import PolicyStore
from sandbox_science.mcp_proxy.upstream import GuardedResolver, Upstream, client_session
from sandbox_science.orchestrator import cli
from sandbox_science.orchestrator.connectors import Connectors, ProxyClient
from sandbox_science.policy import HardFloor, PoolCeiling
from sandbox_science.policy.approvals import Approvals, prepare_answer
from sandbox_science.testing.mcp import (
    SERVER_HOST,
    FakeServer,
    RecordingSink,
    StaticResolver,
    Tls,
    make_tls,
)

from .loop_harness import Harness, Reply, call
from .loop_harness import gateway as gateway
from .loop_harness import harness as harness
from .loop_harness import worker as worker

TOKEN = "test-secret-harness-mcp-caller"
POLICY = """\
apiVersion: sandbox-science/v0alpha1
kind: ProjectPolicy
metadata:
  name: connector-test
defaults:
  action: escalate
mcp:
  proxy:
    worker_access: false
    max_argument_bytes: 1Ki
    max_response_bytes: 64Ki
  servers:
    papers:
      url: https://{host}:{port}/mcp
      revision: "2025-11-25"
      auth: {{mode: bearer, provider: papers-token}}
      tools:
        allow: [search, image, large, "*_search"]
        escalate: [crawl]
        deny: ["admin_*"]
"""


@dataclass
class Proxied:
    """The proxy, its fake server and its event sink, and the proxy's address."""

    url: str
    fake: FakeServer
    sink: RecordingSink

    def approve(self, request: int) -> None:
        """Answer a permission request as ``sandbox-science-mcp-proxy approve`` does."""
        approvals = Approvals()
        approvals.load(self.sink.records(0))
        kind, payload = prepare_answer(
            approvals, request, approve=True, scope=None, hours=1, now=self.sink.now()
        )
        self.sink.append(kind, payload)


@pytest.fixture(scope="session")
def tls(tmp_path_factory: pytest.TempPathFactory) -> Tls:
    return make_tls(tmp_path_factory.mktemp("tls"))


@pytest_asyncio.fixture
async def proxied(tls: Tls, tmp_path: Path) -> AsyncIterator[Proxied]:
    fake = FakeServer()
    upstream_app = web.Application()
    upstream_app.router.add_route("*", "/mcp", fake.handle)
    upstream_server = TestServer(upstream_app, host="127.0.0.1")
    await upstream_server.start_server(ssl=tls.server)
    fake.port = upstream_server.port or 0
    store = PolicyStore(tmp_path / "state")
    loaded = store.add_policy(POLICY.format(host=SERVER_HOST, port=fake.port).encode())
    store.bind("crypto", loaded.digest)
    sink = RecordingSink()
    resolver = GuardedResolver(StaticResolver(), lambda host, addresses: None)
    upstream = Upstream(client_session(resolver, timeout=10, tls=tls.client))
    proxy = McpProxy(
        store=store,
        callers=Callers([(token_digest(TOKEN), Caller("orchestrator", "orchestrator", "crypto"))]),
        ceiling=PoolCeiling.unrestricted(),
        floor=HardFloor(),
        events=sink,
        upstream=upstream,
        sessions=Sessions(limit=8, per_caller=4, idle=3600),
        credentials={"papers-token": "PAPERS_TOKEN"},
        environ={"PAPERS_TOKEN": "openshell:resolve:env:v1_PAPERS_TOKEN"},
        requests=sink.records,
        now=sink.now,
    )
    proxy_server = TestServer(proxy.application(max_body_bytes=64 * 1024), host="127.0.0.1")
    await proxy_server.start_server()
    try:
        yield Proxied(str(proxy_server.make_url("")).rstrip("/"), fake, sink)
    finally:
        await proxy_server.close()
        await upstream.close()
        await upstream_server.close()


@pytest_asyncio.fixture
async def connectors(proxied: Proxied) -> AsyncIterator[Connectors]:
    client = ProxyClient(proxied.url, TOKEN)
    found = Connectors(client, ["papers"], session="s-1")
    await found.load()
    try:
        yield found
    finally:
        await client.close()


def error_of(text: str) -> dict[str, Any]:
    return json.loads(text)["error"]


async def test_a_servers_tools_are_offered_under_their_own_names(
    connectors: Connectors, proxied: Proxied
) -> None:
    names = [spec.name for spec in connectors.specs]
    assert names == list(proxied.fake.tools)  # the proxy lists them all; calls are checked
    (search,) = [spec for spec in connectors.specs if spec.name == "search"]
    assert search.description.startswith("A tool of the MCP server papers, through the MCP proxy.")
    assert "(untrusted)" in search.description
    assert search.parameters == {"type": "object"}
    # The harness's token never reaches the server.
    assert all(seen.headers.get("authorization") != f"Bearer {TOKEN}" for seen in proxied.fake.seen)


async def test_an_allowed_call_answers_with_the_servers_text(
    connectors: Connectors, proxied: Proxied
) -> None:
    text, record = await connectors.call("search", {"query": "lattices"})
    assert text == "search: ok"
    assert record == {"connector": "papers"}
    assert proxied.fake.tool_calls() == ["search"]
    image, _ = await connectors.call("image", {})
    assert image.startswith("a figure")
    assert "iVBORw0KGgo=" not in image  # binary content is withheld by the proxy


async def test_a_denied_call_is_the_tools_error(connectors: Connectors, proxied: Proxied) -> None:
    text, record = await connectors.call("admin_reset", {})
    assert error_of(text)["kind"] == "policy_denied"
    assert record == {"connector": "papers", "error": "policy_denied"}
    assert proxied.fake.tool_calls() == []


async def test_an_escalated_call_waits_for_the_operator(
    connectors: Connectors, proxied: Proxied
) -> None:
    text, record = await connectors.call("crawl", {"url": "https://example.org/a"})
    error = error_of(text)
    assert error["kind"] == "approval_required"
    request = record["permission_request"]
    assert f"permission request {request}" in error["message"]
    assert "call it again later" in error["message"]
    assert proxied.fake.tool_calls() == []
    proxied.approve(request)  # the operator, through the proxy's command line
    again, record = await connectors.call("crawl", {"url": "https://example.org/a"})
    assert again.startswith("crawl")
    assert record == {"connector": "papers"}


async def test_a_session_the_proxy_forgot_is_started_again(proxied: Proxied) -> None:
    client = ProxyClient(proxied.url, TOKEN)
    connectors = Connectors(client, ["papers"], session="s-1")
    await connectors.load()
    others = [ProxyClient(proxied.url, TOKEN) for _ in range(4)]
    try:
        # The caller's next sessions end its least recently used one: this client's.
        for other in others:
            await other.request("papers", "tools/list", {}, session="s-2")
        text, _ = await connectors.call("search", {})
    finally:
        await client.close()
        for other in others:
            await other.close()
    assert text == "search: ok"


async def test_a_wrong_token_or_an_absent_proxy_offers_no_tools(proxied: Proxied) -> None:
    wrong = ProxyClient(proxied.url, "test-secret-not-a-caller")
    nowhere = ProxyClient("http://127.0.0.1:9", TOKEN)
    try:
        for client in (wrong, nowhere):
            connectors = Connectors(client, ["papers"], session="s-1")
            await connectors.load()
            assert connectors.specs == []
    finally:
        await wrong.close()
        await nowhere.close()


async def test_a_name_a_model_cannot_call_is_not_offered(proxied: Proxied) -> None:
    proxied.fake.tools = ["search", "has space", "x" * 65, "search"]
    client = ProxyClient(proxied.url, TOKEN)
    connectors = Connectors(client, ["papers", "papers"], session="s-1")
    try:
        await connectors.load(taken=["shell"])
    finally:
        await client.close()
    assert [spec.name for spec in connectors.specs] == ["search"]


async def test_a_run_calls_a_connector_tool_through_the_proxy(
    harness: Harness, proxied: Proxied
) -> None:
    token = harness.tmp / "mcp.token"
    token.write_text(TOKEN)
    options = ["--mcp-proxy", proxied.url, "--mcp-token-file", str(token), "--mcp-server", "papers"]
    record = await harness.run(
        "run-1",
        [Reply(calls=[call("search", query="lattices")]), Reply("Found it.")],
        options=options,
    )
    assert record.final_text == "Found it."
    (tool,) = record.tools
    assert (tool["tool"], tool["connector"]) == ("search", "papers")
    _, second = harness.gateway.main
    assert second[-1]["content"] == "search: ok"
    assert proxied.fake.tool_calls() == ["search"]
    # The proxy recorded the call as the session's.
    (recorded,) = [p for kind, p in proxied.sink.events if kind == "mcp-call" and p.get("tool")]
    assert cast(dict[str, object], recorded["attribution"])["session"] == "s"
    assert recorded["outcome"] == "forwarded"


def test_the_proxy_options_go_together() -> None:
    base = [
        "run", "--worker", "http://127.0.0.1:1", "--token-file", "t", "--project", "p",
        "--session", "s", "--environment", "e", "--script", "x.json",
    ]  # fmt: skip
    for extra in (
        ["--mcp-proxy", "http://127.0.0.1:2"],
        ["--mcp-token-file", "t"],
        ["--mcp-server", "papers"],
        ["--mcp-proxy", "http://127.0.0.1:2", "--mcp-token-file", "t"],
    ):
        with pytest.raises(SystemExit) as exited:
            cli.main([*base, *extra, "Go."])
        assert exited.value.code == 2
