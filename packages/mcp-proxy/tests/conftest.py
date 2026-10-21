# SPDX-License-Identifier: MIT
"""Shared fixtures: a fake MCP server over Streamable HTTP and TLS, and the proxy in front of it.

The fake server answers as ``mcp.example.test``, which a static resolver maps
to 127.0.0.1, with a certificate from a throwaway CA. It records every
request it receives. Test modules cannot import this file
(``--import-mode=importlib``), so they describe the fixtures they use with
protocols of their own.
"""

import datetime
import json
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from pathlib import Path

import pytest
import pytest_asyncio
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from sandbox_science.mcp_proxy.callers import Caller, Callers, token_digest
from sandbox_science.mcp_proxy.proxy import McpProxy
from sandbox_science.mcp_proxy.sessions import Sessions
from sandbox_science.mcp_proxy.store import PolicyStore
from sandbox_science.mcp_proxy.upstream import (
    AddressCheck,
    GuardedResolver,
    Upstream,
    client_session,
)
from sandbox_science.policy import HardFloor, LoadedPolicy, PoolCeiling
from sandbox_science.testing.mcp import (
    SERVER_HOST,
    FakeServer,
    RecordingSink,
    StaticResolver,
    Tls,
    make_tls,
)

PLACEHOLDER = "openshell:resolve:env:v1_PAPERS_TOKEN"
TOKENS = {
    "orchestrator": "test-secret-orchestrator",
    "shared": "test-secret-worker-shared",
    "dedicated": "test-secret-worker-dedicated",
    "other": "test-secret-other-project",
    "operator": "test-secret-operator",
    "other-operator": "test-secret-other-operator",
}
CALLERS = {
    "orchestrator": Caller("orchestrator", "orchestrator", "crypto"),
    "shared": Caller("crypto-default", "worker", "crypto", container="ss-crypto-default"),
    "dedicated": Caller(
        "crypto-s42", "worker", "crypto", container="ss-crypto-s42", session="s-42"
    ),
    "other": Caller("other-orchestrator", "orchestrator", "other"),
    "operator": Caller("workspace", "operator", "crypto"),
    "other-operator": Caller("other-workspace", "operator", "other"),
}
POLICY = """\
apiVersion: sandbox-science/v0alpha1
kind: ProjectPolicy
metadata:
  name: proxy-test
defaults:
  action: {default}
mcp:
  proxy:
    worker_access: {worker_access}
    worker_tools: {worker_tools}
    max_argument_bytes: 1Ki
    max_response_bytes: 64Ki
  servers:
    papers:
      url: https://{host}:{port}/mcp
      revision: "{revision}"
      auth: {auth}
      tools:
        allow: [search, image, stream, large, broken, unlisted, "*_search"]
        escalate: [crawl]
        deny: ["admin_*"]
"""


@pytest.fixture(scope="session")
def tls(tmp_path_factory: pytest.TempPathFactory) -> Tls:
    """A throwaway CA and a certificate for the fake server's name, made at test time."""
    return make_tls(tmp_path_factory.mktemp("tls"))


@dataclass
class Answer:
    """What the proxy answered."""

    status: int
    headers: Mapping[str, str]
    body: object


class Harness:
    """The proxy in front of the fake server, with a client for its callers."""

    def __init__(self, fake: FakeServer, sink: RecordingSink, root: Path) -> None:
        self.fake = fake
        self.sink = sink
        self.store = PolicyStore(root / "state")
        self.environ: dict[str, str] = {
            "PAPERS_TOKEN": PLACEHOLDER,
            "PAPERS_API_KEY": "openshell:resolve:env:v1_PAPERS_API_KEY",
        }
        self.tokens = TOKENS
        self.callers = CALLERS
        self.address_check: AddressCheck = lambda host, addresses: None
        self.client: TestClient[web.Request, web.Application] | None = None

    def policy(
        self,
        *,
        default: str = "escalate",
        worker_access: str = "true",
        worker_tools: str = "inherit",
        revision: str = "2025-11-25",
        auth: str = "{mode: bearer, provider: papers-token}",
        port: int | None = None,
    ) -> str:
        return POLICY.format(
            default=default,
            worker_access=worker_access,
            worker_tools=worker_tools,
            revision=revision,
            auth=auth,
            host=SERVER_HOST,
            port=self.fake.port if port is None else port,
        )

    def bind(self, text: str, project: str = "crypto") -> LoadedPolicy:
        loaded = self.store.add_policy(text.encode())
        self.store.bind(project, loaded.digest)
        return loaded

    async def start(self, tls: Tls) -> None:
        resolver = GuardedResolver(StaticResolver(), lambda h, a: self.address_check(h, a))
        upstream = Upstream(client_session(resolver, timeout=10, tls=tls.client))
        proxy = McpProxy(
            store=self.store,
            callers=Callers((token_digest(TOKENS[key]), CALLERS[key]) for key in TOKENS),
            ceiling=PoolCeiling.unrestricted(),
            floor=HardFloor(),
            events=self.sink,
            upstream=upstream,
            sessions=Sessions(limit=8, per_caller=4, idle=3600),
            credentials={"papers-token": "PAPERS_TOKEN", "papers-key.API_KEY": "PAPERS_API_KEY"},
            environ=self.environ,
            # test_records.py swaps in a sink on a real store, which has no reader.
            requests=self.sink.records if isinstance(self.sink, RecordingSink) else None,
            answers=self._answer if isinstance(self.sink, RecordingSink) else None,
            now=lambda: (
                self.sink.now()
                if isinstance(self.sink, RecordingSink)
                else datetime.datetime.now(datetime.UTC)
            ),
        )
        app = proxy.application(max_body_bytes=64 * 1024)

        async def close(_: web.Application) -> None:
            await upstream.close()

        app.on_cleanup.append(close)
        self.client = TestClient(TestServer(app, host="127.0.0.1"))
        await self.client.start_server()

    async def _answer(self, kind: str, payload: dict[str, object]) -> int:
        assert isinstance(self.sink, RecordingSink)
        return self.sink.append(kind, payload)

    async def operator(
        self, method: str, path: str, body: object = None, *, caller: str = "operator"
    ) -> Answer:
        """A request to the proxy's operator routes, as a caller."""
        assert self.client is not None
        headers = {"Authorization": f"Bearer {TOKENS[caller]}"} if caller else {}
        async with self.client.request(
            method, path, json=body if body is not None else None, headers=headers
        ) as response:
            text = await response.read()
            parsed: object = json.loads(text) if text else None
            return Answer(response.status, dict(response.headers), parsed)

    async def post(
        self,
        body: object,
        *,
        caller: str = "orchestrator",
        session: str | None = None,
        headers: Mapping[str, str] | None = None,
        server: str = "papers",
        raw: bytes | None = None,
    ) -> Answer:
        """POST one JSON-RPC message (or raw bytes) as a caller."""
        data = raw if raw is not None else json.dumps(body).encode()
        return await self.request(
            "POST", data, caller=caller, session=session, headers=headers, server=server
        )

    async def request(
        self,
        method: str,
        data: bytes = b"",
        *,
        caller: str = "orchestrator",
        session: str | None = None,
        headers: Mapping[str, str] | None = None,
        server: str = "papers",
    ) -> Answer:
        """Send any request to a server's endpoint as a caller (no caller: no token)."""
        assert self.client is not None
        sent = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if caller:
            sent["Authorization"] = f"Bearer {TOKENS[caller]}"
        if session is not None:
            sent["Mcp-Session-Id"] = session
        sent.update(headers or {})
        async with self.client.request(
            method, f"/mcp/{server}", data=data, headers=sent
        ) as response:
            text = await response.read()
            try:
                parsed: object = json.loads(text) if text else None
            except ValueError:
                parsed = text.decode(errors="replace")
            return Answer(response.status, dict(response.headers), parsed)

    async def initialize(self, caller: str = "orchestrator", version: str = "2025-11-25") -> str:
        """Start a session as a caller; return its id."""
        params = {
            "protocolVersion": version,
            "capabilities": {"sampling": {}, "roots": {"listChanged": True}},
            "clientInfo": {"name": "test-client", "version": "1"},
        }
        answer = await self.post(
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": params}, caller=caller
        )
        assert answer.status == 200, answer.body
        session = answer.headers["Mcp-Session-Id"]
        notified = await self.post(
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            caller=caller,
            session=session,
        )
        assert notified.status == 202
        return session

    async def call(
        self,
        session: str,
        method: str,
        params: Mapping[str, object] | None = None,
        *,
        caller: str = "orchestrator",
        request_id: object = 7,
        headers: Mapping[str, str] | None = None,
    ) -> Answer:
        message: dict[str, object] = {"jsonrpc": "2.0", "id": request_id, "method": method}
        if params is not None:
            message["params"] = dict(params)
        return await self.post(message, caller=caller, session=session, headers=headers)

    async def tool(
        self,
        session: str,
        name: str,
        arguments: Mapping[str, object] | None = None,
        *,
        caller: str = "orchestrator",
    ) -> Answer:
        params = {"name": name, "arguments": dict(arguments or {"query": "lattices"})}
        return await self.call(session, "tools/call", params, caller=caller)

    async def close(self) -> None:
        if self.client is not None:
            await self.client.close()


@pytest.fixture
def sink() -> RecordingSink:
    return RecordingSink()


@pytest_asyncio.fixture
async def fake(tls: Tls) -> AsyncIterator[FakeServer]:
    """The fake server, started on 127.0.0.1 with TLS."""
    server = FakeServer()
    app = web.Application()
    app.router.add_route("*", "/mcp", server.handle)
    test_server = TestServer(app, host="127.0.0.1")
    await test_server.start_server(ssl=tls.server)
    server.port = test_server.port or 0
    yield server
    await test_server.close()


@pytest_asyncio.fixture
async def harness(
    fake: FakeServer, sink: RecordingSink, tls: Tls, tmp_path: Path
) -> AsyncIterator[Harness]:
    """The proxy, started, with the default test policy bound to project ``crypto``."""
    harness = Harness(fake, sink, tmp_path)
    harness.bind(harness.policy())
    await harness.start(tls)
    yield harness
    await harness.close()
