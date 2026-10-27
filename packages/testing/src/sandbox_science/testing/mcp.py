# SPDX-License-Identifier: MIT
"""A fake MCP server over Streamable HTTP and TLS, for tests of MCP clients.

The fake server answers as ``mcp.example.test``, with a certificate from a throwaway CA
that ``make_tls`` makes, or over plain HTTP behind a test's stand-in gateway. It records
every request it receives. It requires what OpenShell's fence and real servers
require: after ``initialize``, a known ``Mcp-Session-Id`` (else 404) and an
``MCP-Protocol-Version`` header (else 400).
"""

from __future__ import annotations

import asyncio
import datetime
import json
import ssl
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

from aiohttp import web
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

SERVER_HOST = "mcp.example.test"
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


@dataclass(frozen=True)
class Tls:
    """TLS contexts for the fake server: one serves, one trusts it."""

    server: ssl.SSLContext
    client: ssl.SSLContext


def make_tls(directory: Path) -> Tls:
    """A throwaway CA and a certificate for the fake server's name, written to
    ``directory``, made at test time."""
    now = datetime.datetime.now(datetime.UTC)
    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "MCP test CA")])
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
        self.listing: Callable[[Mapping[str, object]], dict[str, object]] | None = None
        """Answers tools/list instead of the pages of ``tools``, when set."""
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
        found: list[dict[str, Any]] = []
        for seen in self.seen:
            message: object = seen.message
            if isinstance(message, dict):
                found.append(cast("dict[str, Any]", message))
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
        message = cast("dict[str, Any]", message)
        method = message.get("method")
        if method == "initialize":
            version = self.version or str(message["params"]["protocolVersion"])
            session_id = self.session_id or f"upstream-session-{len(self.sessions) + 1}"
            self.sessions.add(session_id)
            result: dict[str, object] = {
                "protocolVersion": version,
                "capabilities": {"tools": {"listChanged": True}, "tasks": {"list": {}}},
                "serverInfo": {"name": "fake", "version": "1.0"},
                "instructions": "Use search first.",
                **self.initialize_extra,
            }
            # A cookie, which a client must not send back to any server.
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
        request_id: object = message["id"]
        params = cast("dict[str, Any]", message.get("params") or {})
        if method == "tools/list":
            return web.json_response(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "result": (self.listing or self._listing)(params),
                }
            )
        if method == "tools/call":
            if self.gate is not None:
                await self.gate.wait()
            name = str(params["name"])
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
    sampling: dict[str, object] = {"messages": [], "maxTokens": 10}
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


@dataclass(frozen=True)
class MemoryRecord:
    """A record as the shared approvals code reads it (``sandbox_science.policy.approvals``)."""

    seq: int
    time: str
    kind: str
    payload: Mapping[str, object]

    def data(self) -> dict[str, object]:
        return dict(self.payload)


class RecordingSink:
    """An event sink that keeps numbered records in memory, or fails on demand.

    ``now`` gives each record its time, so a test can move time on.
    """

    def __init__(self) -> None:
        self.events: list[tuple[str, Mapping[str, object]]] = []
        self.stored: list[MemoryRecord] = []
        self.fail = False
        self.now: Callable[[], datetime.datetime] = lambda: datetime.datetime.now(datetime.UTC)

    async def record(self, kind: str, payload: Mapping[str, object]) -> int:
        if self.fail:
            raise OSError("test: the event store is unavailable")
        self.events.append((kind, payload))
        return self.append(kind, payload)

    def append(self, kind: str, payload: Mapping[str, object]) -> int:
        """Store a record directly, as the command line does for approvals."""
        seq = len(self.stored) + 1
        time = self.now().isoformat(timespec="microseconds")
        self.stored.append(MemoryRecord(seq, time, kind, dict(payload)))
        return seq

    def records(self, after: int) -> list[MemoryRecord]:
        return [record for record in self.stored if record.seq > after]

    def kinds(self, kind: str) -> list[Mapping[str, object]]:
        return [payload for name, payload in self.events if name == kind]
