# SPDX-License-Identifier: MIT
"""MCP connectors for the agent: the harness as a caller of the MCP proxy (D67, D-08).

The orchestrator reaches MCP servers only through the MCP proxy
(docs/MCP-CONNECTORS.md), which authenticates it by its bearer token, applies the
project's policy, holds escalated calls for the operator, and alone holds the
servers' credential placeholders. ``ProxyClient`` is a small Streamable HTTP client
for the proxy, which answers JSON only. A token names one project at the proxy, so
connectors are offered only to runs of that project.

``Connectors`` are one run's connector tools (the decisions of 2026-10-14):

- listed at the run's start from each configured server, within a deadline, a failed
  listing tried again twice, a second and then two seconds later (a timeout is not), under
  the names the server lists, which for a portal such as crypto-research-mcp carry its
  prefix. A name a model cannot call, one another server's tool has, or one the harness
  gives its own tools (whether this run has them or not) is left out and logged, and
  so is a server's tool past the per-server cap;
- offered as an index (lab 39, B5): every tool's name and one-line purpose, grouped by
  the server behind it, with the operator's word on what each server is for, in the
  description of the harness's ``mcp_tools`` tool, which loads named tools into the
  run's tools for the rest of the run. Every schema at every call cost about 17,000
  tokens for crypto-research-mcp's 72 tools. A query lists the tools that match it well,
  with their purposes, and loads none: loading a query's best matches loaded ten
  unrelated schemas for a search that had no match (lab 40, B4);
- a server whose listing still failed is unavailable, not empty: the index says so, with
  the reason, the first ``mcp_tools`` call lists it once more, and the run's record and
  its ``started`` event keep it (lab 41, B4: one failed listing left a run without
  connectors, and nothing said so);
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
  done the work, so the harness leaves its intent pending for reconciliation;
- text results only: the proxy withholds binary content, and the toolbox bounds the
  text as it bounds any tool result.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final, cast
from urllib.parse import urlsplit

import aiohttp

from .documents.agent_text import hidden_characters
from .effects import UnknownEffectError
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
MAX_MATCHES: Final = 8
"""Tools one ``mcp_tools`` query lists."""
_COMMON: Final = frozenset(
    {"a", "an", "and", "by", "for", "in", "mcp", "of", "on", "or", "the", "to", "tool", "tools"}
)
"""Words a query says without saying what it wants."""
MAX_ABOUT: Final = 200
"""Characters of the operator's word on what a server is for."""
MAX_INDEX_CHARS: Final = 16_000
"""Characters of the index's tool lines; past them, tools are found with a query."""
MAX_GROUPS: Final = 32
"""Servers behind one configured server that the index names."""
MAX_LOADED_CHARS: Final = 100_000
"""Characters of descriptions and schemas a run loads: past them, a load is refused,
and the run goes on with the tools it has rather than ending at its context limit."""
MAX_QUERY: Final = 200
"""Characters of an ``mcp_tools`` query."""
MAX_PAGES: Final = 50
MAX_TOOLS_PER_SERVER: Final = 128
MAX_TOOLS: Final = 256
MAX_SPEC_BYTES: Final = 512 * 1024
"""All connector tools' descriptions and schemas together, as the model gets them."""
LIST_SECONDS: Final = 15.0
"""How long listing one server's tools may take at a run's start."""
LIST_ATTEMPTS: Final = 3
"""Listings of a server at a run's start, while each fails at once."""
LIST_BACKOFF: Final = 1.0
"""Seconds before the second listing of a server; each later one waits twice as long."""
MAX_FAILURE: Final = 300
"""Characters of the reason a server's listing failed."""
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
        raise ConnectorError(
            "no_usable_answer", f"the MCP proxy answered {status} without a usable result"
        )
    return cast(dict[str, Any], result)


def clean(text: str) -> str:
    """``text`` a server wrote without any character a person may not see, stripped
    rather than refused: controls but line breaks and tabs, format, private-use and
    unassigned characters, line and paragraph separators, and every Default_Ignorable
    code point (``agent_text.hidden_characters``, threat model A3)."""
    hidden = {index for index, _ in hidden_characters(text)}
    return "".join(char for index, char in enumerate(text) if index not in hidden)


@dataclass(frozen=True)
class ConnectorTool:
    """A tool of a server behind the proxy, as offered to the model."""

    server: str
    spec: ToolSpec
    purpose: str = ""
    """The first line of what the server says it does, bounded: the index shows it."""

    @property
    def group(self) -> str:
        """The index's key for the server the tool is under: behind a portal, the
        configured server and the server the tool's name starts with, a prefix with a
        hyphen (``crypto-research-mcp/e-print-mcp``); otherwise the configured server.
        A server chooses its tools' names, so the key always says which configured
        server it was."""
        head, separator, _ = self.spec.name.partition("_")
        return f"{self.server}/{head}" if separator and "-" in head else self.server

    @property
    def cost(self) -> int:
        """Characters of its description and schema, as the listing's bound counts them."""
        return len(self.spec.description) + len(json.dumps(self.spec.parameters))

    @property
    def sha256(self) -> str:
        """The SHA-256 of the spec the model is given, which the run's record keeps."""
        spec = {
            "name": self.spec.name,
            "description": self.spec.description,
            "parameters": self.spec.parameters,
        }
        return hashlib.sha256(json.dumps(spec, sort_keys=True).encode()).hexdigest()


def heading(key: str) -> str:
    """How the index names a server: ``e-print-mcp (via crypto-research-mcp)``."""
    server, _, behind = key.partition("/")
    return f"{behind} (via {server})" if behind else server


def _tools(count: int) -> str:
    return f"{count} tool{'' if count == 1 else 's'}"


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
        self.client, self.servers, self.session = client, tuple(dict.fromkeys(servers)), session
        self.list_seconds = list_seconds
        self.tools: dict[str, ConnectorTool] = {}
        self.about = {
            name: " ".join(clean(text).split())[:MAX_ABOUT] for name, text in (about or {}).items()
        }
        """The operator's word on what each server is for, by its index key: a configured
        server's name, or ``SERVER/BEHIND`` for one behind it (``ConnectorTool.group``)."""
        self.shown = None if shown is None else frozenset(shown)
        """The servers, by index key, whose tools the index lists one by one; ``None``
        lists every server's. The others' tools are found with a query."""
        self.counts: dict[str, int] = {}
        """How many tools each configured server offers."""
        self.refused: set[str] = set()
        """Names more than one configured server listed: none of them is offered."""
        self.loaded: list[str] = []
        """The tools loaded into the run, in order."""
        self.loaded_chars = 0
        self.failed: dict[str, str] = {}
        """The configured servers whose listing failed, with why: unavailable, not empty."""
        self.relisted: list[str] = []
        """The servers a later listing reached, after the run's start."""
        self.taken: set[str] = set()
        self.retried = False

    @property
    def specs(self) -> list[ToolSpec]:
        return [tool.spec for tool in self.tools.values()]

    async def load(self, taken: Iterable[str] = ()) -> None:
        """List each server's tools. A server that cannot be listed in time offers none,
        and the run goes on without it; ``taken`` names tools the harness keeps."""
        self.taken = set(taken)
        listings: dict[str, list[tuple[str, dict[str, Any]]]] = {}
        for server in self.servers:
            listed = await self._listed(server, LIST_ATTEMPTS)
            if listed is not None:
                listings[server] = self._kept(server, listed, self.taken)
        self._register(listings)

    async def relist(self) -> list[str]:
        """List once more, once in the run, each server whose listing failed: the servers
        that answered now, whose tools join the index."""
        if self.retried or not self.failed:
            return []
        self.retried = True
        listings: dict[str, list[tuple[str, dict[str, Any]]]] = {}
        for server in list(self.failed):
            listed = await self._listed(server, 1)
            if listed is not None:
                listings[server] = self._kept(server, listed, self.taken | set(self.tools))
        self._register(listings)
        self.relisted.extend(listings)
        return list(listings)

    async def _listed(self, server: str, attempts: int) -> list[dict[str, Any]] | None:
        """``server``'s tools, listed up to ``attempts`` times while a listing fails at
        once, waiting longer each time; ``None``, with the reason kept, when none worked."""
        delay = LIST_BACKOFF
        for attempt in range(1, attempts + 1):
            try:
                async with asyncio.timeout(self.list_seconds):
                    listed = await self._list(server)
            except ConnectorError as error:
                failure = f"{error.kind}: {error.message}"
                if attempt < attempts:
                    _log.info("listing MCP server %s failed (%s): trying again", server, failure)
                    await asyncio.sleep(delay)
                    delay *= 2
                    continue
            except TimeoutError:
                failure = f"no listing in {self.list_seconds:g} s"
            else:
                self.failed.pop(server, None)
                return listed
            self.failed[server] = " ".join(clean(failure).split())[:MAX_FAILURE]
            _log.warning("the tools of MCP server %s are not offered: %s", server, failure)
            return None
        return None

    def _kept(
        self, server: str, listed: list[dict[str, Any]], names: set[str]
    ) -> list[tuple[str, dict[str, Any]]]:
        kept: list[tuple[str, dict[str, Any]]] = []
        seen: set[str] = set()
        for tool in listed:
            name = tool.get("name")
            if not isinstance(name, str) or not NAME.fullmatch(name) or name in names | seen:
                _log.warning("MCP server %s's tool %r is not offered: its name", server, name)
                continue
            seen.add(name)
            kept.append((name, tool))
        return kept

    def _register(self, listings: dict[str, list[tuple[str, dict[str, Any]]]]) -> None:
        """Offer the listed servers' tools, within the caps."""
        # A name two configured servers list is neither's: one could stand in for the
        # other's tool, whichever was listed first.
        owners = Counter(name for kept in listings.values() for name, _ in kept)
        size = sum(tool.cost for tool in self.tools.values())
        for server, kept in listings.items():
            offered = 0
            for name, tool in kept:
                if owners[name] > 1:
                    if name not in self.refused:
                        _log.warning(
                            "MCP servers %s each list the tool %s: none of them is offered",
                            ", ".join(s for s in listings if name in dict(listings[s])),
                            name,
                        )
                    self.refused.add(name)
                    continue
                spec = _spec(server, name, tool)
                offered_tool = ConnectorTool(server, spec, purpose(tool.get("description")))
                if (
                    offered >= MAX_TOOLS_PER_SERVER
                    or len(self.tools) >= MAX_TOOLS
                    or size + offered_tool.cost > MAX_SPEC_BYTES
                ):
                    _log.warning(
                        "MCP server %s's tool %s is not offered: too many tools", server, name
                    )
                    continue
                offered += 1
                size += offered_tool.cost
                self.tools[name] = offered_tool
            self.counts[server] = offered
        for server in listings:
            if not self.counts.setdefault(server, 0):
                _log.warning(
                    "MCP server %s offers no tools: none listed, or the policy denies all "
                    "(check tool_prefix)",
                    server,
                )
        keys = {tool.group for tool in self.tools.values()}
        for key in sorted({*self.about, *(self.shown or ())} - keys):
            if key.partition("/")[0] in self.failed:
                continue  # said already: its listing failed
            _log.warning(
                "the tool index has no server %s, which --mcp-about or --mcp-index names: "
                "name a configured server, or SERVER/BEHIND for one behind a portal",
                key,
            )

    def index(self) -> str:
        """The connector tools as the model first sees them: by server, each named with
        the configured server it is behind and with the operator's word on what it is
        for, marked as the operator's, and each tool's name and purpose; a server not
        ``shown`` with its count alone. At most ``MAX_GROUPS`` servers behind one
        configured server, and ``MAX_INDEX_CHARS`` of tool lines: the rest are found with
        a query."""
        by: dict[str, list[str]] = {}
        for name, tool in self.tools.items():
            by.setdefault(tool.group, []).append(name)
        lines: list[str] = []
        groups: Counter[str] = Counter()
        unlisted_groups: Counter[str] = Counter()
        unlisted = used = 0
        for server, failure in sorted(self.failed.items()):
            lines.append(
                f"{server} - unavailable: its listing failed ({failure}). Call mcp_tools to "
                "list it once more; until then its tools cannot be called"
            )
        for key in sorted(by):
            server = key.partition("/")[0]
            groups[server] += 1
            if groups[server] > MAX_GROUPS:
                unlisted_groups[server] += 1
                continue
            names = sorted(by[key])
            about = self.about.get(key)
            line = heading(key) + (f" - operator: {about}" if about else "")
            if self.shown is not None and key not in self.shown:
                lines.append(f"{line} ({_tools(len(names))}, not listed: find them with a query)")
                continue
            lines.append(line)
            for name in names:
                said = self.tools[name].purpose
                entry = f"  {name}{f' - {said}' if said else ''}"
                if used + len(entry) > MAX_INDEX_CHARS:
                    unlisted += 1
                    continue
                used += len(entry) + 1
                lines.append(entry)
        for server, count in sorted(unlisted_groups.items()):
            lines.append(f"{count} more servers via {server}: find their tools with a query")
        if unlisted:
            lines.append(f"{_tools(unlisted)} more, not listed for room: find them with a query")
        return "\n".join(lines)

    def search(self, query: str) -> list[str]:
        """The tools that match ``query`` well, best first, at most ``MAX_MATCHES``: those
        whose name, purpose or server holds each of a short query's words, or two thirds
        of a longer one's, one of them in the name or purpose. A query that shares a word
        with a tool by chance, such as one for a harness tool, finds nothing (lab 40,
        B4)."""
        words = list(
            dict.fromkeys(
                word
                for word in re.split(r"[^a-z0-9]+", query.lower())
                if len(word) > 1 and word not in _COMMON
            )
        )
        needed = len(words) if len(words) <= 2 else -(-2 * len(words) // 3)
        scored: list[tuple[int, str]] = []
        for name, tool in self.tools.items():
            own = f"{name} {tool.purpose}".lower()
            server = f"{tool.group} {self.about.get(tool.group, '')}".lower()
            found = [word for word in words if word in own or word in server]
            if not words or len(found) < needed or not any(word in own for word in found):
                continue
            score = sum(3 if word in name.lower() else 2 if word in own else 1 for word in found)
            scored.append((-score, name))
        return [name for _, name in sorted(scored)[:MAX_MATCHES]]

    def take(self, names: Iterable[str]) -> tuple[list[str], list[str], list[str], list[str]]:
        """Load ``names`` into the run: those loaded now, those loaded before, those no
        listed tool has, and those past the run's ``MAX_LOADED_CHARS`` of schemas."""
        added: list[str] = []
        before: list[str] = []
        unknown: list[str] = []
        over: list[str] = []
        for name in names:
            tool = self.tools.get(name)
            if tool is None:
                unknown.append(name)
            elif name in self.loaded:
                before.append(name)
            elif self.loaded_chars + tool.cost > MAX_LOADED_CHARS:
                over.append(name)
            else:
                self.loaded.append(name)
                self.loaded_chars += tool.cost
                added.append(name)
        return added, before, unknown, over

    def summary(self) -> dict[str, Any]:
        """What the run's record keeps: the tools each configured server offered, the
        names refused for being listed twice, and each tool loaded, in order, with the
        SHA-256 of the spec the model was given."""
        found: dict[str, Any] = {
            "listed": len(self.tools),
            "servers": dict(self.counts),
            "loaded": [{"name": name, "sha256": self.tools[name].sha256} for name in self.loaded],
        }
        if self.refused:
            found["refused"] = sorted(self.refused)
        if self.failed:
            found["unavailable"] = dict(self.failed)
        if self.relisted:
            found["relisted"] = list(self.relisted)
        return found

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
            if error.kind == "no_usable_answer":
                raise UnknownEffectError(error.kind, error.message) from error
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
