# SPDX-License-Identifier: MIT
"""MCP connectors for the agent: the harness as a caller of the MCP proxy (D67, D-08).

The orchestrator reaches MCP servers only through the MCP proxy
(docs/MCP-CONNECTORS.md), which authenticates it by its bearer token, applies the
project's policy, holds escalated calls for the operator, and alone holds the
servers' credential placeholders. ``ProxyClient`` is a small Streamable HTTP client
for the proxy, which answers JSON only. A token names one project at the proxy, so
connectors are offered only to runs of that project.

``Connectors`` are one run's connector tools (the decisions of 2026-10-14):

- listed at the run's start from each configured server, within a deadline, under the
  names the server lists, which for a portal such as crypto-research-mcp carry its
  prefix. A name a model cannot call, one another server's tool has, or one the harness
  gives its own tools (whether this run has them or not) is left out and logged, and
  so is a server's tool past the per-server cap;
- offered as an index (lab 39, B5): every tool's name and one-line purpose, grouped by
  the server behind it, with the operator's word on what each server is for, in the
  description of the harness's ``mcp_tools`` tool, which loads named tools, or a
  query's matches, into the run's tools for the rest of the run. Every schema at every
  call cost about 17,000 tokens for crypto-research-mcp's 72 tools;
- each with the effect class ``effect``, since the policy marks no connector tool
  read-only, so the plan fence applies;
- what a server says about a tool is untrusted. Its description loses control and
  invisible characters, is bounded and is framed as the server's. Its input schema is
  rebuilt from structural keywords only, with no text but property names and bounded
  enum values; a schema that does not fit falls back to an empty object schema;
- a refusal comes back as the tool's error with the proxy's reason. ``approval_required``
  says the call waits for the operator (``sandbox-science-mcp-proxy pending`` and
  ``approve``), and the agent calls again later, with the same arguments, which the
  harness sends in one order;
- a call sent whose answer cannot be read is ``no_usable_answer``: the server may have
  done the work, so the agent should check before calling again;
- text results only: the proxy withholds binary content, and the toolbox bounds the
  text as it bounds any tool result.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import unicodedata
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final, cast
from urllib.parse import urlsplit

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
MAX_VALUE_TEXT: Final = 1024
"""Characters of enum and const text one tool's schema keeps."""
PURPOSE_CHARS: Final = 60
"""Characters of a tool's one-line purpose in the index."""
MAX_LOAD: Final = 16
"""Tools one ``mcp_tools`` call loads by name."""
MAX_MATCHES: Final = 5
"""Tools one ``mcp_tools`` query loads."""
MAX_ABOUT: Final = 200
"""Characters of the operator's word on what a server is for."""
MAX_PAGES: Final = 50
MAX_TOOLS_PER_SERVER: Final = 128
MAX_TOOLS: Final = 256
MAX_SPEC_BYTES: Final = 512 * 1024
"""All connector tools' descriptions and schemas together, as the model gets them."""
LIST_SECONDS: Final = 15.0
"""How long listing one server's tools may take at a run's start."""
CALL_SECONDS: Final = 330.0
"""How long one call may take: longer than the proxy's default ``upstream_timeout``
(5 minutes), so the proxy's own answer arrives first."""
NAME: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")
"""A tool name a model can call, and that a run's records can hold."""
_TOKEN: Final = re.compile(r"[\x21-\x7e]{16,512}")
_PLAIN_HTTP: Final = frozenset(
    {"127.0.0.1", "localhost", "::1", "[::1]", "host.openshell.internal"}
)
"""Hosts reached over plain http: loopback, and OpenShell's route from a sandbox to
its own host (``host.openshell.internal``), whose traffic stays on that host."""


class ConnectorError(Exception):
    """The proxy refused a request, or could not be reached; ``kind`` is its reason."""

    def __init__(self, kind: str, message: str, data: Mapping[str, object] | None = None) -> None:
        super().__init__(f"{kind}: {message}")
        self.kind, self.message, self.data = kind, message, dict(data or {})


def check_proxy(url: str, token: str) -> None:
    """Refuse a proxy address or token the client should not use (ValueError).

    The address is ``https``, or ``http`` on loopback only, where the proxy's forward
    listens on one host; the token is one line of visible ASCII."""
    parts = urlsplit(url)
    host = parts.hostname
    if parts.scheme not in ("http", "https") or not host:
        raise ValueError("the MCP proxy's address is an http or https URL")
    if parts.scheme == "http" and host not in _PLAIN_HTTP:
        raise ValueError(
            "the MCP proxy is reached over https, or over http on loopback or "
            "host.openshell.internal only"
        )
    if parts.username is not None or parts.query or parts.fragment:
        raise ValueError("the MCP proxy's address has no credentials, query or fragment")
    if not _TOKEN.fullmatch(token):
        raise ValueError("the MCP proxy token is one line of 16 to 512 visible ASCII characters")


class ProxyClient:
    """One caller's connection to the MCP proxy at ``url``, with its ``token``.

    It keeps one MCP session per server, started on first use and shared by concurrent
    callers, and starts it again once if the proxy no longer knows it (a restart). It
    never follows a redirect.
    """

    def __init__(self, url: str, token: str, *, timeout: float = CALL_SECONDS) -> None:
        self.url = url.rstrip("/")
        self._token = token
        self._timeout = aiohttp.ClientTimeout(total=timeout)
        self._http: aiohttp.ClientSession | None = None
        self._sessions: dict[str, asyncio.Task[str]] = {}
        self._ids = 0

    async def close(self) -> None:
        for task in self._sessions.values():
            task.cancel()
        if self._http is not None:
            await self._http.close()
            self._http = None

    async def request(
        self, server: str, method: str, params: Mapping[str, object], *, session: str
    ) -> dict[str, Any]:
        """``method``'s result from ``server``; ``session`` names the asking session."""
        message = _body(
            {"jsonrpc": "2.0", "id": self._next(), "method": method, "params": dict(params)}
        )
        for attempt in (1, 2):
            task, mcp_session = await self._session(server, session)
            status, body = await self._post(server, message, session, mcp_session)
            if status == 404 and attempt == 1:
                self._forget(server, task)  # the proxy forgot it: start again, once
                continue
            return _result(status, body)
        raise AssertionError("unreachable")  # pragma: no cover

    def _forget(self, server: str, task: asyncio.Task[str]) -> None:
        """Forget the session ``task`` started, unless another caller already started a
        new one, which is never cancelled: others may be waiting on it."""
        if self._sessions.get(server) is task:
            del self._sessions[server]

    async def _session(self, server: str, session: str) -> tuple[asyncio.Task[str], str]:
        """The server's MCP session, and the start that made it: callers wait on one
        start, and no lock is held."""
        task = self._sessions.get(server)
        if task is None or (task.done() and (task.cancelled() or task.exception() is not None)):
            task = self._sessions[server] = asyncio.ensure_future(self._start(server, session))
        try:
            return task, await asyncio.shield(task)
        except ConnectorError:
            self._forget(server, task)
            raise

    async def _start(self, server: str, session: str) -> str:
        """Start an MCP session with ``server`` through the proxy; its ID."""
        params = {
            "protocolVersion": PROTOCOL,
            "capabilities": {},
            "clientInfo": {"name": "sandbox-science-harness", "version": "0.1"},
        }
        message = {"jsonrpc": "2.0", "id": self._next(), "method": "initialize", "params": params}
        try:
            async with self._client().post(
                f"{self.url}/mcp/{server}",
                data=_body(message),
                headers=self._headers(session, None),
                allow_redirects=False,
            ) as response:
                body = await _read(response)
                found = response.headers.get("Mcp-Session-Id")
                status = response.status
            _result(status, body)
            if not found:
                raise ConnectorError("unavailable", f"the proxy started no session for {server}")
            async with self._client().post(
                f"{self.url}/mcp/{server}",
                data=_body({"jsonrpc": "2.0", "method": "notifications/initialized"}),
                headers=self._headers(session, found),
                allow_redirects=False,
            ) as response:
                await _read(response)
                if response.status != 202:
                    raise ConnectorError(
                        "unavailable", f"the proxy answered initialized with {response.status}"
                    )
        except (aiohttp.ClientError, TimeoutError, ValueError) as exc:
            raise ConnectorError(
                "unavailable", f"the MCP proxy cannot be reached: {type(exc).__name__}"
            ) from None
        return found

    async def _post(
        self, server: str, message: bytes, session: str, mcp_session: str
    ) -> tuple[int, object]:
        try:
            response = await self._client().post(
                f"{self.url}/mcp/{server}",
                data=message,
                headers=self._headers(session, mcp_session),
                allow_redirects=False,
            )
        except (aiohttp.ClientError, TimeoutError, ValueError) as exc:
            # Nothing was answered: the call may or may not have left.
            raise ConnectorError(
                "no_usable_answer", f"the MCP proxy gave no answer ({type(exc).__name__})"
            ) from None
        async with response:
            try:
                return response.status, await _read(response)
            except (aiohttp.ClientError, TimeoutError) as exc:
                raise ConnectorError(
                    "no_usable_answer",
                    f"the MCP proxy's answer could not be read ({type(exc).__name__})",
                ) from None

    def _client(self) -> aiohttp.ClientSession:
        if self._http is None:
            self._http = aiohttp.ClientSession(timeout=self._timeout)
        return self._http

    def _headers(self, session: str, mcp_session: str | None) -> dict[str, str]:
        headers = {
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
            SESSION_HEADER: session,
        }
        if mcp_session is not None:
            headers["Mcp-Session-Id"] = mcp_session
            headers["MCP-Protocol-Version"] = PROTOCOL
        return headers

    def _next(self) -> int:
        self._ids += 1
        return self._ids


def _body(message: Mapping[str, object]) -> bytes:
    """A message with its keys sorted, so the same arguments are the same request: a call
    approved for the operator matches it when it comes again. A message JSON cannot hold
    is never sent (``not_sent``)."""
    try:
        return json.dumps(message, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()
    except (TypeError, ValueError, RecursionError) as exc:
        raise ConnectorError(
            "not_sent", f"the arguments cannot be sent as JSON ({type(exc).__name__})"
        ) from None


async def _read(response: aiohttp.ClientResponse) -> object:
    """The whole answer, up to ``MAX_ANSWER_BYTES``, as JSON; ``None`` if it is not."""
    data = bytearray()
    while chunk := await response.content.read(64 * 1024):
        data += chunk
        if len(data) > MAX_ANSWER_BYTES:
            raise ConnectorError(
                "no_usable_answer", f"the proxy's answer exceeds {MAX_ANSWER_BYTES} bytes"
            )
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
            raise ConnectorError(reason, clean(str(error.get("message", "")))[:500], data)
        said = clean(str(error.get("message", "")))[:2000]
        raise ConnectorError(
            "server_error", f"the MCP server answered an error (untrusted): {said}"
        )
    result = message.get("result")
    if status != 200 or not isinstance(result, dict):
        raise ConnectorError("unavailable", f"the MCP proxy answered {status} without a result")
    return cast(dict[str, Any], result)


def clean(text: str) -> str:
    """``text`` without control, format (invisible) and private-use characters, but for
    line breaks and tabs."""
    return "".join(
        char
        for char in text
        if char in "\n\t" or unicodedata.category(char) not in ("Cc", "Cf", "Co", "Cs")
    )


@dataclass(frozen=True)
class ConnectorTool:
    """A tool of a server behind the proxy, as offered to the model."""

    server: str
    spec: ToolSpec
    purpose: str = ""
    """The first line of what the server says it does, bounded: the index shows it."""


def group(server: str, name: str) -> str:
    """The server a tool is indexed under: behind a portal, the server its name starts
    with (``e-print-mcp`` of ``e-print-mcp_search_eprint``); else the configured one."""
    head, separator, _ = name.partition("_")
    return head if separator and "-" in head else server


def purpose(description: object) -> str:
    """A server's description of a tool cut to its first sentence or line, cleaned and
    bounded, for the index."""
    if not isinstance(description, str):
        return ""
    text = " ".join(clean(description).split())
    for end in (". ", "; ", " - "):
        if end in text:
            text = text[: text.index(end) + (1 if end == ". " else 0)]
            break
    return text if len(text) <= PURPOSE_CHARS else text[: PURPOSE_CHARS - 1].rstrip() + "…"


class Connectors:
    """One run's connector tools, from ``servers`` behind the proxy (see the module)."""

    def __init__(
        self,
        client: ProxyClient,
        servers: Sequence[str],
        *,
        session: str,
        list_seconds: float = LIST_SECONDS,
        about: Mapping[str, str] | None = None,
        shown: Sequence[str] | None = None,
    ) -> None:
        self.client, self.servers, self.session = client, tuple(servers), session
        self.list_seconds = list_seconds
        self.tools: dict[str, ConnectorTool] = {}
        self.about = {
            name: " ".join(clean(text).split())[:MAX_ABOUT] for name, text in (about or {}).items()
        }
        """The operator's word on what each server is for, by its index name."""
        self.shown = None if shown is None else frozenset(shown)
        """The servers whose tools the index lists one by one; ``None`` lists every
        server's. The others' tools are found with a query."""
        self.loaded: list[str] = []
        """The tools loaded into the run, in order."""

    @property
    def specs(self) -> list[ToolSpec]:
        return [tool.spec for tool in self.tools.values()]

    async def load(self, taken: Iterable[str] = ()) -> None:
        """List each server's tools. A server that cannot be listed in time offers none,
        and the run goes on without it; ``taken`` names tools the harness keeps."""
        names = set(taken)
        size = 0
        for server in self.servers:
            try:
                async with asyncio.timeout(self.list_seconds):
                    listed = await self._list(server)
            except (ConnectorError, TimeoutError) as error:
                _log.warning("the tools of MCP server %s are not offered: %s", server, error)
                continue
            offered = 0
            for tool in listed:
                name = tool.get("name")
                if not isinstance(name, str) or not NAME.fullmatch(name) or name in names:
                    _log.warning("MCP server %s's tool %r is not offered: its name", server, name)
                    continue
                spec = _spec(server, name, tool)
                cost = len(spec.description) + len(json.dumps(spec.parameters))
                if (
                    offered >= MAX_TOOLS_PER_SERVER
                    or len(self.tools) >= MAX_TOOLS
                    or size + cost > MAX_SPEC_BYTES
                ):
                    _log.warning(
                        "MCP server %s's tool %s is not offered: too many tools", server, name
                    )
                    continue
                names.add(name)
                offered += 1
                size += cost
                self.tools[name] = ConnectorTool(server, spec, purpose(tool.get("description")))

    def index(self) -> str:
        """The connector tools as the model first sees them: by server, each server with
        the operator's word on what it is for, and each tool's name and purpose; a
        server not ``shown`` with its count alone."""
        by: dict[str, list[str]] = {}
        for name, tool in self.tools.items():
            by.setdefault(group(tool.server, name), []).append(name)
        lines: list[str] = []
        for server in sorted(by):
            names = sorted(by[server])
            about = self.about.get(server)
            heading = f"{server}: {about}" if about else f"{server}:"
            if self.shown is not None and server not in self.shown:
                lines.append(f"{heading} ({len(names)} tools, not listed: find them with a query)")
                continue
            lines.append(heading)
            for name in names:
                said = self.tools[name].purpose
                lines.append(f"  {name}{f' - {said}' if said else ''}")
        return "\n".join(lines)

    def search(self, query: str) -> list[str]:
        """The tools that best match ``query``'s words, in their names, purposes or
        servers, at most ``MAX_MATCHES``."""
        words = [word for word in re.split(r"[^a-z0-9]+", query.lower()) if word]
        scored: list[tuple[int, str]] = []
        for name, tool in self.tools.items():
            server = group(tool.server, name)
            haystack = f"{name} {tool.purpose} {server} {self.about.get(server, '')}".lower()
            score = sum(3 if word in name.lower() else 1 for word in words if word in haystack)
            if score:
                scored.append((-score, name))
        return [name for _, name in sorted(scored)[:MAX_MATCHES]]

    def take(self, names: Iterable[str]) -> tuple[list[str], list[str], list[str]]:
        """Load ``names`` into the run: those loaded now, those loaded before, and those
        no listed tool has."""
        added: list[str] = []
        before: list[str] = []
        unknown: list[str] = []
        for name in names:
            if name not in self.tools:
                unknown.append(name)
            elif name in self.loaded:
                before.append(name)
            else:
                self.loaded.append(name)
                added.append(name)
        return added, before, unknown

    def summary(self) -> dict[str, Any]:
        """What the run's record keeps: how many tools were listed, and which were loaded."""
        return {"listed": len(self.tools), "loaded": list(self.loaded)}

    async def _list(self, server: str) -> list[dict[str, Any]]:
        tools: list[dict[str, Any]] = []
        cursor: str | None = None
        seen: set[str] = set()
        for _ in range(MAX_PAGES):
            params: dict[str, object] = {} if cursor is None else {"cursor": cursor}
            result = await self.client.request(server, "tools/list", params, session=self.session)
            page = result.get("tools")
            if not isinstance(page, list):
                raise ConnectorError("unavailable", f"{server}'s tools/list has no tools")
            for tool in cast(list[object], page):
                if isinstance(tool, dict):
                    tools.append(cast(dict[str, Any], tool))
            following = result.get("nextCursor")
            if not isinstance(following, str) or not following:
                return tools
            if following in seen:
                raise ConnectorError("unavailable", f"{server}'s tools/list goes round in a circle")
            seen.add(following)
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
            if error.kind == "approval_required" and "request" in error.data:
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
            f"permission request {request}; call it again later with the same arguments."
        )
    if error.kind == "no_usable_answer":
        return (
            f"{error.message}. The call was sent, and the server may have done the work: "
            "check its effect before calling again, and do not repeat a call that spends "
            "or changes something."
        )
    if error.kind == "not_sent":
        return f"{error.message}. Nothing reached the server; call it with other arguments."
    return error.message


def _spec(server: str, name: str, tool: Mapping[str, Any]) -> ToolSpec:
    description = tool.get("description")
    said = clean(description)[:MAX_DESCRIPTION] if isinstance(description, str) else ""
    parameters: dict[str, Any] = {"type": "object"}
    schema = tool.get("inputSchema")
    if isinstance(schema, dict) and len(json.dumps(schema)) <= MAX_SCHEMA_BYTES:
        rebuilt = _schema(cast(dict[str, Any], schema), 0)
        if rebuilt is not None and rebuilt.get("type") == "object":
            # Enum and const text is the server's too: past a small budget, a tool's
            # values are dropped, which only loosens what the model is told.
            parameters = rebuilt if _value_text(rebuilt) <= MAX_VALUE_TEXT else _bare(rebuilt)
    return ToolSpec(
        name,
        f"A tool of the MCP server {server}, through the MCP proxy. "
        f"The server describes it (untrusted): {said}",
        parameters,
    )


_TYPES: Final = frozenset({"object", "array", "string", "number", "integer", "boolean", "null"})
_PROPERTY: Final = re.compile(r"[A-Za-z0-9_.$-]{1,64}")
_COUNTS: Final = (
    "minLength",
    "maxLength",
    "minItems",
    "maxItems",
    "minProperties",
    "maxProperties",
)
_BOUNDS: Final = ("minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "multipleOf")
_MAX_DEPTH: Final = 6


def _schema(schema: Mapping[str, Any], depth: int) -> dict[str, Any] | None:
    """A JSON Schema (2020-12) rebuilt from structural keywords only, or ``None`` when a
    keyword it keeps is malformed. Text the model would read as instructions (titles,
    descriptions, examples, defaults, patterns, references) is left out."""
    if depth > _MAX_DEPTH:
        return None
    built: dict[str, Any] = {}
    kind = schema.get("type")
    if kind is not None:
        kinds: list[object] = cast(list[object], kind) if isinstance(kind, list) else [kind]
        names = [k for k in kinds if isinstance(k, str) and k in _TYPES]
        if not kinds or len(names) != len(kinds) or len(set(names)) != len(names):
            return None
        built["type"] = kind
    if "properties" in schema:
        properties = schema["properties"]
        if not isinstance(properties, dict) or len(cast(dict[str, Any], properties)) > 128:
            return None
        rebuilt: dict[str, Any] = {}
        for key, value in cast(dict[str, Any], properties).items():
            if not _PROPERTY.fullmatch(key) or not isinstance(value, dict):
                return None
            inner = _schema(cast(dict[str, Any], value), depth + 1)
            if inner is None:
                return None
            rebuilt[key] = inner
        built["properties"] = rebuilt
    if "required" in schema:
        required = schema["required"]
        known = built.get("properties", {})
        if (
            not isinstance(required, list)
            or not all(isinstance(r, str) and r in known for r in cast(list[object], required))
            or len(set(cast(list[str], required))) != len(cast(list[str], required))
        ):
            return None
        built["required"] = list(cast(list[str], required))
    if "additionalProperties" in schema:
        extra = schema["additionalProperties"]
        if not isinstance(extra, bool):
            return None  # a schema there could carry text; a boolean is enough
        built["additionalProperties"] = extra
    if "items" in schema:
        items = schema["items"]
        if not isinstance(items, dict):
            return None
        inner = _schema(cast(dict[str, Any], items), depth + 1)
        if inner is None:
            return None
        built["items"] = inner
    for key in ("anyOf", "oneOf", "allOf"):
        if key in schema:
            options = schema[key]
            if not isinstance(options, list) or not 1 <= len(cast(list[object], options)) <= 16:
                return None
            rebuilt_options: list[dict[str, Any]] = []
            for option in cast(list[object], options):
                inner = (
                    _schema(cast(dict[str, Any], option), depth + 1)
                    if isinstance(option, dict)
                    else None
                )
                if inner is None:
                    return None
                rebuilt_options.append(inner)
            built[key] = rebuilt_options
    if "enum" in schema:
        values = schema["enum"]
        if not isinstance(values, list) or not 1 <= len(cast(list[object], values)) <= 64:
            return None
        kept = [_scalar(value) for value in cast(list[object], values)]
        if any(value is _BAD for value in kept):
            return None
        built["enum"] = kept
    if "const" in schema:
        value = _scalar(schema["const"])
        if value is _BAD:
            return None
        built["const"] = value
    for key in _COUNTS:
        if key in schema:
            value = schema[key]
            if type(value) is not int or value < 0:
                return None
            built[key] = value
    for key in _BOUNDS:
        if key in schema:
            value = schema[key]
            if type(value) not in (int, float) or value != value or abs(value) == float("inf"):
                return None
            built[key] = value
    return built


_BAD: Final = object()


def _value_text(schema: object) -> int:
    """Characters of text in a rebuilt schema's enum and const values."""
    if isinstance(schema, list):
        return sum(_value_text(item) for item in cast(list[object], schema))
    if not isinstance(schema, dict):
        return 0
    total = 0
    for key, value in cast(dict[str, object], schema).items():
        if key == "enum":
            total += sum(len(v) for v in cast(list[object], value) if isinstance(v, str))
        elif key == "const":
            total += len(value) if isinstance(value, str) else 0
        elif key == "properties":
            total += sum(_value_text(v) for v in cast(dict[str, object], value).values())
        else:
            total += _value_text(value)
    return total


def _bare(schema: object) -> Any:
    """A rebuilt schema without its enum and const values."""
    if isinstance(schema, list):
        return [_bare(item) for item in cast(list[object], schema)]
    if not isinstance(schema, dict):
        return schema
    return {
        key: (
            {name: _bare(v) for name, v in cast(dict[str, object], value).items()}
            if key == "properties"
            else _bare(value)
        )
        for key, value in cast(dict[str, object], schema).items()
        if key not in ("enum", "const")
    }


def _scalar(value: object) -> object:
    """An enum or const value: a scalar, its text bounded and cleaned; ``_BAD`` if not."""
    if value is None or isinstance(value, bool | int):
        return value
    if isinstance(value, float):
        return value if value == value and abs(value) != float("inf") else _BAD
    if isinstance(value, str):
        return clean(value)[:200]
    return _BAD


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
