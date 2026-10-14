# SPDX-License-Identifier: MIT
"""MCP connectors for the agent: the harness as a caller of the MCP proxy (D67, D-08).

The orchestrator reaches MCP servers only through the MCP proxy
(docs/MCP-CONNECTORS.md), which authenticates it by its bearer token, applies the
project's policy, holds escalated calls for the operator, and alone holds the
servers' credential placeholders. ``ProxyClient`` is a small Streamable HTTP client
for the proxy, which answers JSON only.

``Connectors`` are one run's connector tools (the decisions of 2026-10-14):

- listed at the run's start from each configured server, and offered under the names
  the server lists, which for a portal such as crypto-research-mcp carry its prefix.
  A name a model cannot call, or one another tool has, is left out and logged;
- each with the effect class ``effect``, since the policy marks no connector tool
  read-only, so the plan fence applies;
- what a server says about a tool is untrusted: its description is framed as the
  server's and bounded, and its input schema bounded;
- a refusal comes back as the tool's error with the proxy's reason. ``approval_required``
  says the call waits for the operator (``sandbox-science-mcp-proxy pending`` and
  ``approve``), and the agent calls again later;
- text results only: the proxy withholds binary content, and the toolbox bounds the
  text as it bounds any tool result.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final, cast

import aiohttp

from .model import ToolSpec
from .worker_client import dump

_log = logging.getLogger(__name__)

PROTOCOL: Final = "2025-11-25"
"""The MCP revision the harness speaks to the proxy."""
SESSION_HEADER: Final = "Sandbox-Science-Session"
"""Names the session whose agent made a call, which the proxy records (docs/MCP-CONNECTORS.md)."""
REFUSED: Final = -31001
"""The proxy's own JSON-RPC error code: it refused the call (its ``data.reason`` says why)."""
UPSTREAM_FAILED: Final = -31002
MAX_ANSWER_BYTES: Final = 8 * 1024 * 1024
MAX_DESCRIPTION: Final = 2000
MAX_SCHEMA_BYTES: Final = 16 * 1024
MAX_PAGES: Final = 50
_NAME: Final = re.compile(r"[A-Za-z0-9_-]{1,64}")


class ConnectorError(Exception):
    """The proxy refused a request, or could not be reached; ``kind`` is its reason."""

    def __init__(self, kind: str, message: str, data: Mapping[str, object] | None = None) -> None:
        super().__init__(f"{kind}: {message}")
        self.kind, self.message, self.data = kind, message, dict(data or {})


class ProxyClient:
    """One caller's connection to the MCP proxy at ``url``, with its ``token``.

    It keeps one MCP session per server, started on first use, and starts it again
    once if the proxy no longer knows it (a restart). It never follows a redirect.
    """

    def __init__(self, url: str, token: str, *, timeout: float = 300.0) -> None:
        self.url = url.rstrip("/")
        self._token = token
        self._timeout = aiohttp.ClientTimeout(total=timeout)
        self._http: aiohttp.ClientSession | None = None
        self._sessions: dict[str, str] = {}
        self._ids = 0
        self._lock = asyncio.Lock()

    async def close(self) -> None:
        if self._http is not None:
            await self._http.close()
            self._http = None

    async def request(
        self, server: str, method: str, params: Mapping[str, object], *, session: str
    ) -> dict[str, Any]:
        """``method``'s result from ``server``; ``session`` names the asking session."""
        for attempt in (1, 2):
            mcp_session = await self._session(server, session)
            status, body = await self._post(server, method, params, session, mcp_session)
            if status == 404 and attempt == 1:
                self._sessions.pop(server, None)  # the proxy forgot it: start again, once
                continue
            return _result(status, body)
        raise AssertionError("unreachable")  # pragma: no cover

    async def _session(self, server: str, session: str) -> str:
        async with self._lock:
            if (found := self._sessions.get(server)) is not None:
                return found
            try:
                found = await self._start(server, session)
            except (aiohttp.ClientError, TimeoutError) as exc:
                raise ConnectorError(
                    "unavailable", f"the MCP proxy cannot be reached: {exc}"
                ) from None
            self._sessions[server] = found
            return found

    async def _start(self, server: str, session: str) -> str:
        """Start an MCP session with ``server`` through the proxy; its ID."""
        params = {
            "protocolVersion": PROTOCOL,
            "capabilities": {},
            "clientInfo": {"name": "sandbox-science-harness", "version": "0.1"},
        }
        http = self._client()
        async with http.post(
            f"{self.url}/mcp/{server}",
            json={
                "jsonrpc": "2.0",
                "id": self._next(),
                "method": "initialize",
                "params": params,
            },
            headers=self._headers(session, None),
            allow_redirects=False,
        ) as response:
            body = await _read(response)
            found = response.headers.get("Mcp-Session-Id")
        _result(response.status, body)
        if not found:
            raise ConnectorError("unavailable", f"the proxy started no session for {server}")
        async with http.post(
            f"{self.url}/mcp/{server}",
            json={"jsonrpc": "2.0", "method": "notifications/initialized"},
            headers=self._headers(session, found),
            allow_redirects=False,
        ) as response:
            await _read(response)
            if response.status != 202:
                raise ConnectorError(
                    "unavailable", f"the proxy answered initialized with {response.status}"
                )
        return found

    async def _post(
        self,
        server: str,
        method: str,
        params: Mapping[str, object],
        session: str,
        mcp_session: str,
    ) -> tuple[int, object]:
        message = {"jsonrpc": "2.0", "id": self._next(), "method": method, "params": dict(params)}
        try:
            async with self._client().post(
                f"{self.url}/mcp/{server}",
                json=message,
                headers=self._headers(session, mcp_session),
                allow_redirects=False,
            ) as response:
                return response.status, await _read(response)
        except (aiohttp.ClientError, TimeoutError) as exc:
            raise ConnectorError("unavailable", f"the MCP proxy cannot be reached: {exc}") from None

    def _client(self) -> aiohttp.ClientSession:
        if self._http is None:
            self._http = aiohttp.ClientSession(timeout=self._timeout)
        return self._http

    def _headers(self, session: str, mcp_session: str | None) -> dict[str, str]:
        headers = {
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/json",
            SESSION_HEADER: session,
        }
        if mcp_session is not None:
            headers["Mcp-Session-Id"] = mcp_session
            headers["MCP-Protocol-Version"] = PROTOCOL
        return headers

    def _next(self) -> int:
        self._ids += 1
        return self._ids


async def _read(response: aiohttp.ClientResponse) -> object:
    data = await response.content.read(MAX_ANSWER_BYTES + 1)
    if len(data) > MAX_ANSWER_BYTES:
        raise ConnectorError("too_large", f"the proxy's answer exceeds {MAX_ANSWER_BYTES} bytes")
    if not data:
        return None
    try:
        return json.loads(data)
    except (ValueError, RecursionError):
        return None


def _result(status: int, body: object) -> dict[str, Any]:
    """A JSON-RPC answer's result, or the proxy's refusal as a ``ConnectorError``."""
    message = cast(dict[str, Any], body) if isinstance(body, dict) else {}
    error = message.get("error")
    if isinstance(error, dict):
        error = cast(dict[str, Any], error)
        data = error.get("data")
        data = cast(dict[str, object], data) if isinstance(data, dict) else {}
        code = error.get("code")
        reason = data.get("reason")
        if code in (REFUSED, UPSTREAM_FAILED) and isinstance(reason, str):
            raise ConnectorError(reason, str(error.get("message", "")), data)
        raise ConnectorError("server_error", str(error.get("message", ""))[:2000])
    result = message.get("result")
    if status != 200 or not isinstance(result, dict):
        raise ConnectorError("unavailable", f"the MCP proxy answered {status} without a result")
    return cast(dict[str, Any], result)


@dataclass(frozen=True)
class ConnectorTool:
    """A tool of a server behind the proxy, as offered to the model."""

    server: str
    spec: ToolSpec


class Connectors:
    """One run's connector tools, from ``servers`` behind the proxy (see the module)."""

    def __init__(self, client: ProxyClient, servers: Sequence[str], *, session: str) -> None:
        self.client, self.servers, self.session = client, tuple(servers), session
        self.tools: dict[str, ConnectorTool] = {}

    @property
    def specs(self) -> list[ToolSpec]:
        return [tool.spec for tool in self.tools.values()]

    async def load(self, taken: Sequence[str] = ()) -> None:
        """List each server's tools. A server that cannot be listed offers none, and the
        run goes on without it; ``taken`` names tools the run already has."""
        names = set(taken)
        for server in self.servers:
            try:
                listed = await self._list(server)
            except ConnectorError as error:
                _log.warning("the tools of MCP server %s are not offered: %s", server, error)
                continue
            for tool in listed:
                name = tool.get("name")
                if not isinstance(name, str) or not _NAME.fullmatch(name) or name in names:
                    _log.warning("MCP server %s's tool %r is not offered: its name", server, name)
                    continue
                names.add(name)
                self.tools[name] = ConnectorTool(server, _spec(server, name, tool))

    async def _list(self, server: str) -> list[dict[str, Any]]:
        tools: list[dict[str, Any]] = []
        cursor: str | None = None
        for _ in range(MAX_PAGES):
            params: dict[str, object] = {} if cursor is None else {"cursor": cursor}
            result = await self.client.request(server, "tools/list", params, session=self.session)
            page = result.get("tools")
            if not isinstance(page, list):
                raise ConnectorError("unavailable", f"{server}'s tools/list has no tools")
            tools.extend(
                cast(dict[str, Any], t) for t in cast(list[object], page) if isinstance(t, dict)
            )
            following = result.get("nextCursor")
            if not isinstance(following, str) or not following:
                return tools
            cursor = following
        raise ConnectorError(
            "unavailable", f"{server}'s tools/list has more than {MAX_PAGES} pages"
        )

    async def call(self, name: str, arguments: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        """Call a connector tool: its text for the model, and the record to keep."""
        tool = self.tools[name]
        record: dict[str, Any] = {"connector": tool.server}
        try:
            result = await self.client.request(
                tool.server,
                "tools/call",
                {"name": name, "arguments": arguments},
                session=self.session,
            )
        except ConnectorError as error:
            record["error"] = error.kind
            if "request" in error.data:
                record["permission_request"] = error.data["request"]
            return dump({"error": {"kind": error.kind, "message": _explain(error)}}), record
        text = _text(result)
        if result.get("isError") is True:
            record["error"] = "tool_error"
            return dump({"error": {"kind": "tool_error", "message": text}}), record
        return text, record


def _explain(error: ConnectorError) -> str:
    if error.kind == "approval_required":
        request = error.data.get("request")
        return (
            f"{error.message}. The MCP proxy holds this call until the operator approves "
            f"permission request {request}; call it again later."
        )
    return error.message


def _spec(server: str, name: str, tool: Mapping[str, Any]) -> ToolSpec:
    description = tool.get("description")
    said = description[:MAX_DESCRIPTION] if isinstance(description, str) else ""
    schema = tool.get("inputSchema")
    parameters: dict[str, Any] = {"type": "object"}
    if (
        isinstance(schema, dict)
        and cast(dict[str, Any], schema).get("type") == "object"
        and len(json.dumps(schema)) <= MAX_SCHEMA_BYTES
    ):
        parameters = cast(dict[str, Any], schema)
    return ToolSpec(
        name,
        f"A tool of the MCP server {server}, through the MCP proxy. "
        f"The server describes it (untrusted): {said}",
        parameters,
    )


def _text(result: Mapping[str, Any]) -> str:
    """A tool result's text: its text blocks and text resources; other blocks by kind."""
    parts: list[str] = []
    content = result.get("content")
    for block in cast(list[object], content) if isinstance(content, list) else []:
        if not isinstance(block, dict):
            continue
        block = cast(dict[str, Any], block)
        kind = block.get("type")
        if kind == "text" and isinstance(block.get("text"), str):
            parts.append(block["text"])
        elif kind == "resource" and isinstance(resource := block.get("resource"), dict):
            text = cast(dict[str, Any], resource).get("text")
            parts.append(text if isinstance(text, str) else f"[a {kind} block, not shown]")
        else:
            parts.append(f"[a {kind} block, not shown]")
    if not parts and "structuredContent" in result:
        return dump(result["structuredContent"])
    return "\n".join(parts)
