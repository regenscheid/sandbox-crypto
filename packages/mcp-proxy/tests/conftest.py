# SPDX-License-Identifier: MIT
"""Shared fixtures: a fake MCP server over Streamable HTTP and TLS, and the proxy in front of it.

The fake server answers as ``mcp.example.test``, which a static resolver maps
to 127.0.0.1, with a certificate from a throwaway CA. It records every
request it receives. Test modules cannot import this file
(``--import-mode=importlib``), so they describe the fixtures they use with
protocols of their own.
"""

import asyncio
import datetime
import json
import socket
import ssl
from collections.abc import AsyncIterator, Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

import pytest
import pytest_asyncio
from aiohttp import web
from aiohttp.abc import AbstractResolver, ResolveResult
from aiohttp.test_utils import TestClient, TestServer
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

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

SERVER_HOST = "mcp.example.test"
PLACEHOLDER = "openshell:resolve:env:v1_PAPERS_TOKEN"
TOKENS = {
    "orchestrator": "test-secret-orchestrator",
    "shared": "test-secret-worker-shared",
    "dedicated": "test-secret-worker-dedicated",
    "other": "test-secret-other-project",
}
CALLERS = {
    "orchestrator": Caller("orchestrator", "orchestrator", "crypto"),
    "shared": Caller("crypto-default", "worker", "crypto", container="ss-crypto-default"),
    "dedicated": Caller(
        "crypto-s42", "worker", "crypto", container="ss-crypto-s42", session="s-42"
    ),
    "other": Caller("other-orchestrator", "orchestrator", "other"),
}
TOOLS = (
    "search",
    "image",
    "stream",
    "large",
    "broken",
    "crawl",
    "admin_search",
    "admin_reset",
    "notes_search",
    "misc",
)

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


@dataclass(frozen=True)
class Tls:
    """TLS contexts for the fake server: one serves, one trusts it."""

    server: ssl.SSLContext
    client: ssl.SSLContext


@pytest.fixture(scope="session")
def tls(tmp_path_factory: pytest.TempPathFactory) -> Tls:
    """A throwaway CA and a certificate for the fake server's name, made at test time."""
    now = datetime.datetime.now(datetime.UTC)
    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "MCP proxy test CA")])
    ca_cert = (
        x509.CertificateBuilder()
        .subject_name(ca_name)
        .issuer_name(ca_name)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=False,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=True,
                crl_sign=True,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()), False)
        .sign(ca_key, hashes.SHA256())
    )
    key = ec.generate_private_key(ec.SECP256R1())
    cert = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, SERVER_HOST)]))
        .issuer_name(ca_name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(SERVER_HOST)]), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), False)
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), False
        )
        .sign(ca_key, hashes.SHA256())
    )
    directory = tmp_path_factory.mktemp("tls")
    ca_path = directory / "ca.pem"
    cert_path = directory / "server.pem"
    key_path = directory / "server-key.pem"
    ca_path.write_bytes(ca_cert.public_bytes(serialization.Encoding.PEM))
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    server = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    server.load_cert_chain(cert_path, key_path)
    client = ssl.create_default_context(cafile=str(ca_path))
    return Tls(server=server, client=client)


@dataclass(frozen=True)
class Seen:
    """A request the fake server received; header names are lowercased."""

    method: str
    headers: dict[str, str]
    body: bytes
    message: object


@dataclass
class Reply:
    """What the fake server answers to one tools/call."""

    body: bytes
    status: int = 200
    content_type: str = "application/json"
    headers: dict[str, str] = field(default_factory=dict[str, str])
    chunked: bool = False
    """Stream the body without a Content-Length."""


def json_reply(message: object) -> Reply:
    return Reply(json.dumps(message).encode())


def sse_reply(*messages: object) -> Reply:
    body = b"".join(b"data: " + json.dumps(message).encode() + b"\n\n" for message in messages)
    return Reply(body, content_type="text/event-stream")


def text_result(request_id: object, text: str) -> dict[str, object]:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "result": {"content": [{"type": "text", "text": text}], "isError": False},
    }


ToolBehaviour = Callable[[object, dict[str, object]], Reply]


class FakeServer:
    """An MCP server over Streamable HTTP that records every request it receives.

    It requires what OpenShell's fence and real servers require: after
    ``initialize``, a known ``Mcp-Session-Id`` (else 404) and an
    ``MCP-Protocol-Version`` header (else 400).
    """

    def __init__(self) -> None:
        self.port = 0
        self.seen: list[Seen] = []
        self.version: str | None = None
        """The revision ``initialize`` answers; None answers the one requested."""
        self.tools: list[str] = list(TOOLS)
        self.page_size: int | None = None
        self.sessions: set[str] = set()
        self.replies: dict[str, ToolBehaviour] = {
            "image": _image,
            "stream": _stream,
            "large": lambda request_id, _: json_reply(text_result(request_id, "x" * 100_000)),
        }
        self.gate: asyncio.Event | None = None
        """When set, tools/call waits for this event before answering."""
        self.session_id: str | None = None
        """The Mcp-Session-Id the next initialize answers with, instead of a fresh one."""
        self.initialize_extra: dict[str, object] = {}
        """Fields added to the initialize result."""
        self.last_cursor: str | None = None
        """A nextCursor to send on the last page of tools/list."""

    def answer(
        self,
        name: str,
        body: bytes | Callable[[object], bytes],
        *,
        status: int = 200,
        content_type: str = "application/json",
        headers: Mapping[str, str] | None = None,
        chunked: bool = False,
    ) -> None:
        """Answer calls to tool ``name`` with ``body``, or with ``body(request id)``."""

        def behaviour(request_id: object, _: dict[str, object]) -> Reply:
            data = body(request_id) if callable(body) else body
            return Reply(data, status, content_type, dict(headers or {}), chunked)

        self.replies[name] = behaviour

    def messages(self, method: str | None = None) -> list[dict[str, Any]]:
        """Parsed JSON-RPC messages received, optionally only those with ``method``."""
        found = [
            cast("dict[str, Any]", seen.message)
            for seen in self.seen
            if isinstance(seen.message, dict)
        ]
        return [m for m in found if method is None or m.get("method") == method]

    def tool_calls(self) -> list[str]:
        """Names of the tools called, in order."""
        return [m["params"]["name"] for m in self.messages("tools/call")]

    async def handle(self, request: web.Request) -> web.StreamResponse:
        body = await request.read()
        headers = {name.lower(): value for name, value in request.headers.items()}
        try:
            message: object = json.loads(body) if body else None
        except ValueError:
            message = None
        self.seen.append(Seen(request.method, headers, body, message))
        if request.method != "POST" or not isinstance(message, dict):
            return web.Response(status=400 if request.method == "POST" else 405)
        method = message.get("method")
        if method == "initialize":
            version = self.version or message["params"]["protocolVersion"]
            session_id = self.session_id or f"upstream-session-{len(self.sessions) + 1}"
            self.sessions.add(session_id)
            result = {
                "protocolVersion": version,
                "capabilities": {"tools": {"listChanged": True}, "tasks": {"list": {}}},
                "serverInfo": {"name": "fake", "version": "1.0"},
                "instructions": "Use search first.",
                **self.initialize_extra,
            }
            # A cookie, which the proxy must not send back to any server.
            return web.json_response(
                {"jsonrpc": "2.0", "id": message["id"], "result": result},
                headers={"Mcp-Session-Id": session_id, "Set-Cookie": "upstream=test; Path=/"},
            )
        if headers.get("mcp-session-id") not in self.sessions:
            return web.Response(status=404)
        if "mcp-protocol-version" not in headers:
            return web.Response(status=400, text="MCP-Protocol-Version is required")
        if "id" not in message or method is None:
            return web.Response(status=202)  # a notification, or a reply to a server request
        request_id = message["id"]
        params = message.get("params") or {}
        if method == "tools/list":
            return web.json_response(
                {"jsonrpc": "2.0", "id": request_id, "result": self._listing(params)}
            )
        if method == "tools/call":
            if self.gate is not None:
                await self.gate.wait()
            name = params["name"]
            behaviour = self.replies.get(name)
            reply = (
                behaviour(request_id, params)
                if behaviour is not None
                else json_reply(text_result(request_id, f"{name}: ok"))
            )
            sent = {"Content-Type": reply.content_type, **reply.headers}
            if not reply.chunked:
                return web.Response(status=reply.status, body=reply.body, headers=sent)
            response = web.StreamResponse(status=reply.status, headers=sent)
            response.enable_chunked_encoding()
            await response.prepare(request)
            for offset in range(0, len(reply.body), 4096):
                await response.write(reply.body[offset : offset + 4096])
            await response.write_eof()
            return response
        if method == "ping":
            return web.json_response({"jsonrpc": "2.0", "id": request_id, "result": {}})
        error = {"code": -32601, "message": f"{method} not found"}
        return web.json_response({"jsonrpc": "2.0", "id": request_id, "error": error})

    def _listing(self, params: Mapping[str, object]) -> dict[str, object]:
        start = int(str(params.get("cursor", "0")))
        size = self.page_size or len(self.tools)
        page = self.tools[start : start + size]
        listing: dict[str, object] = {
            "tools": [{"name": name, "inputSchema": {"type": "object"}} for name in page]
        }
        if start + size < len(self.tools):
            listing["nextCursor"] = str(start + size)
        elif self.last_cursor is not None:
            listing["nextCursor"] = self.last_cursor
        return listing


def _image(request_id: object, _: dict[str, object]) -> Reply:
    content = [
        {"type": "text", "text": "a figure"},
        {"type": "image", "mimeType": "image/png", "data": "iVBORw0KGgo="},
        {"type": "resource", "resource": {"uri": "file:///a.pdf", "blob": "JVBERi0="}},
    ]
    return json_reply({"jsonrpc": "2.0", "id": request_id, "result": {"content": content}})


def _stream(request_id: object, _: dict[str, object]) -> Reply:
    progress = {"progressToken": "p", "progress": 1, "total": 2}
    sampling = {"messages": [], "maxTokens": 10}
    return sse_reply(
        {"jsonrpc": "2.0", "method": "notifications/progress", "params": progress},
        {
            "jsonrpc": "2.0",
            "id": "server-1",
            "method": "sampling/createMessage",
            "params": sampling,
        },
        {"jsonrpc": "2.0", "id": "server-2", "method": "ping"},
        {"jsonrpc": "2.0", "method": "notifications/tools/list_changed"},
        text_result(request_id, "streamed"),
    )


class StaticResolver(AbstractResolver):
    """Resolves the fake server's name to 127.0.0.1, and nothing else."""

    async def resolve(
        self, host: str, port: int = 0, family: socket.AddressFamily = socket.AF_INET
    ) -> list[ResolveResult]:
        if host != SERVER_HOST:
            raise OSError(f"test resolver: unknown host {host}")
        return [
            {
                "hostname": host,
                "host": "127.0.0.1",
                "port": port,
                "family": socket.AF_INET,
                "proto": 0,
                "flags": socket.AI_NUMERICHOST,
            }
        ]

    async def close(self) -> None:
        pass


class RecordingSink:
    """An event sink that keeps ``(kind, payload)`` records in memory, or fails on demand."""

    def __init__(self) -> None:
        self.events: list[tuple[str, Mapping[str, object]]] = []
        self.fail = False

    async def record(self, kind: str, payload: Mapping[str, object]) -> None:
        if self.fail:
            raise OSError("test: the event store is unavailable")
        self.events.append((kind, payload))

    def kinds(self, kind: str) -> list[Mapping[str, object]]:
        return [payload for name, payload in self.events if name == kind]


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
        )
        app = proxy.application(max_body_bytes=64 * 1024)

        async def close(_: web.Application) -> None:
            await upstream.close()

        app.on_cleanup.append(close)
        self.client = TestClient(TestServer(app, host="127.0.0.1"))
        await self.client.start_server()

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
