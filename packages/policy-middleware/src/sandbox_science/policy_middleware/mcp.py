# SPDX-License-Identifier: MIT
"""MCP calls an agent sends straight to a declared server through OpenShell (D98 prototype).

The sandbox's compiled policy (``compile_agent_mcp``) admits each declared server as an
inspected ``protocol: mcp`` endpoint, whose native rules allow and deny tools by name, and
names the project and the caller in the middleware's config. For every request OpenShell
admits there, the middleware reads the JSON-RPC message as the MCP proxy reads one
(``sandbox_science.policy.mcp_wire``), and the evaluator decides it with the library's tool
rules and records it.

The answers to its ``tools/list`` requests are read whole before the sandbox gets them
(``ListingCheck``): the tools the policy denies are dropped and the rest marked with their
class and whether they escalate, as the MCP proxy does (``sandbox_science.policy.
mcp_answers``), and a drift from the server's pinned list is a finding.

The ``Sandbox-Science-Session`` and ``Sandbox-Science-Run`` headers come from the agent's
sandbox, so they are attribution, never authorization: they are recorded with the call and
its permission request, and are part of no request's key or an approval's scope.
"""

from __future__ import annotations

import json
import logging
import re
import threading
from collections import OrderedDict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final, cast

from sandbox_science.policy import LoadedPolicy, mcp_wire
from sandbox_science.policy.classification import ServerClassification, server_classification
from sandbox_science.policy.detection import DRIFT_RECORD, DriftLedger, pin_digest
from sandbox_science.policy.effects import EffectClass
from sandbox_science.policy.mcp_answers import Rewrite, rewrite_answer
from sandbox_science.policy.mcp_wire import ListedTool, McpWireError
from sandbox_science.policy.model import McpServer
from sandbox_science.policy.openshell import mcp_server_endpoint
from sandbox_science.policy.reasons import (
    ARGUMENTS_TOO_LARGE,
    CREDENTIAL_IN_BODY,
    DESTINATION_FORBIDDEN,
    REQUEST_UNCLASSIFIABLE,
)

from .errors import MiddlewareError, SettingsError
from .events import EventSink

_log = logging.getLogger(__name__)

SESSION_HEADER: Final = "sandbox-science-session"
RUN_HEADER: Final = "sandbox-science-run"
ATTRIBUTION_MISSING: Final = "attribution_missing"
"""An MCP call that does not name its session, or names it or its run malformed."""
_LABEL_RE: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
_REQUEST_KEYS: Final = frozenset({"jsonrpc", "id", "method", "params"})
_INITIALIZE_KEYS: Final = frozenset({"protocolVersion", "capabilities", "clientInfo"})
MAX_RECORDED_TEXT: Final = 256
MAX_TRACKED_LISTINGS: Final = 1024
"""``tools/list`` requests whose answers the middleware waits for, oldest forgotten first."""
MAX_LISTING_BYTES: Final = 4 * 1024 * 1024
"""The most of one ``tools/list`` answer the middleware reads; past it, the answer is
blocked."""
LISTING_BLOCKED: Final = "tool_listing_unreadable"
"""A ``tools/list`` answer the middleware could not read strictly, or whose whole body it
could not inspect: blocked, since passing it would offer the agent tools it cannot use."""
LISTINGS_OFF: Final = "mcp_listings_off"
"""A sandbox that calls MCP servers directly, served by a middleware that does not inspect
their answers (``mcp_listings``): its listings would keep denied tools, so it is refused."""
MAX_DRIFT_NAMES: Final = 100
"""Tool names a drift record lists of those added, removed and changed."""
DRIFT_FINDING: Final = "tool_list_drift"
"""The finding a listing that differs from its pin raises (D96)."""


@dataclass(frozen=True, slots=True)
class McpRef:
    """The config's ``mcp`` entry: whose policy and approvals the sandbox's calls take."""

    project: str
    caller: str


class McpRequestError(MiddlewareError):
    """A request to an MCP endpoint that is refused before the policy decides it."""

    def __init__(self, code: str, reason: str) -> None:
        super().__init__(reason)
        self.code = code


@dataclass(frozen=True, slots=True)
class McpCall:
    """One JSON-RPC message to a declared server, as decided.

    ``params`` are the bytes of the parameters MCP defines for the method (``_meta`` left
    out), whose digest a permission request's key holds, as in the MCP proxy.
    """

    server: str
    message: Mapping[str, object]
    method: str
    tool: str | None
    params: bytes
    body_bytes: int
    session: str
    run: str | None
    cursor: str | None = None

    def subject(self, ref: McpRef, sandbox_id: str) -> dict[str, object]:
        """Who called which server, method and tool: the fields every record has."""
        return {
            "project": ref.project,
            "caller": ref.caller,
            "sandbox_id": sandbox_id,
            "session": self.session,
            "run": self.run,
            "server": self.server,
            "method": _bounded(self.method),
            "tool": _bounded(self.tool),
        }


def declared_server(loaded: LoadedPolicy, host: str, port: int, path: str) -> str | None:
    """The declared server whose endpoint a request reaches, or None for another host.

    Raises McpRequestError for another path on a server's host and port: servers often
    offer other transports (such as ``/sse``) beside the declared one.
    """
    for name, server in sorted(loaded.policy.mcp.servers.items()):
        server_port, server_path = mcp_server_endpoint(server)
        if server.host == host and server_port == port:
            if path == server_path:
                return name
            raise McpRequestError(
                DESTINATION_FORBIDDEN, f"{path!r} is not MCP server {name!r}'s endpoint"
            )
    return None


def read_call(
    server: str,
    method: str,
    query: str,
    headers: Sequence[tuple[str, str]],
    body: bytes,
    *,
    max_argument_bytes: int,
) -> McpCall:
    """Read one MCP request strictly; raise McpRequestError for anything the server could
    read differently from the policy, or that does not say whose call it is."""
    if method.upper() != "POST" or query:
        raise McpRequestError(
            REQUEST_UNCLASSIFIABLE, "MCP requests are POSTs to the endpoint, without a query"
        )
    session, run = _attribution(headers)
    try:
        value = mcp_wire.loads(body)
    except McpWireError as exc:
        raise McpRequestError(REQUEST_UNCLASSIFIABLE, str(exc)) from exc
    if not isinstance(value, dict):
        raise McpRequestError(REQUEST_UNCLASSIFIABLE, "one JSON-RPC request or notification")
    message = cast("dict[str, object]", value)
    name = message.get("method")
    raw = message.get("params")
    request_id = message.get("id", "")
    if (
        message.get("jsonrpc") != "2.0"
        or not isinstance(name, str)
        or not message.keys() <= _REQUEST_KEYS
        or (raw is not None and not isinstance(raw, dict))
        or isinstance(request_id, bool)
        or not isinstance(request_id, str | int)
    ):
        # Responses to server requests too: the agent's client declares no capabilities.
        raise McpRequestError(REQUEST_UNCLASSIFIABLE, "one JSON-RPC request or notification")
    params = cast("dict[str, object] | None", raw)
    try:
        if name == "initialize":
            forwarded = _initialize_params(params)
            encoded = mcp_wire.dumps(forwarded)
        elif "id" in message:
            forwarded = mcp_wire.forwarded_params(name, params)
            encoded = mcp_wire.dumps(forwarded) if forwarded is not None else b""
        else:
            encoded = mcp_wire.dumps(params) if params is not None else b""
    except McpWireError as exc:
        raise McpRequestError(REQUEST_UNCLASSIFIABLE, str(exc)) from exc
    if len(encoded) > max_argument_bytes:
        raise McpRequestError(
            ARGUMENTS_TOO_LARGE, f"params are {len(encoded)} bytes, over {max_argument_bytes}"
        )
    if mcp_wire.contains_placeholder(body):
        raise McpRequestError(CREDENTIAL_IN_BODY, "the body contains a credential placeholder")
    tool = (params or {}).get("name") if name == "tools/call" else None
    cursor = (params or {}).get("cursor") if name == "tools/list" else None
    return McpCall(
        server=server,
        message=message,
        method=name,
        tool=tool if isinstance(tool, str) else None,
        params=encoded,
        body_bytes=len(body),
        session=session,
        run=run,
        cursor=cursor if isinstance(cursor, str) else None,
    )


def _initialize_params(params: Mapping[str, object] | None) -> dict[str, object]:
    """``initialize``'s parameters: the agent's client declares no capabilities, so the
    server has no grounds for sampling, elicitation or roots requests, as with the proxy."""
    fields = dict(params or {})
    fields.pop("_meta", None)
    if not fields.keys() <= _INITIALIZE_KEYS or fields.get("capabilities", {}) != {}:
        raise McpWireError("initialize declares no client capabilities and nothing else")
    return fields


def _attribution(headers: Sequence[tuple[str, str]]) -> tuple[str, str | None]:
    sessions = [value for name, value in headers if name == SESSION_HEADER]
    runs = [value for name, value in headers if name == RUN_HEADER]
    if len(sessions) != 1 or not _LABEL_RE.fullmatch(sessions[0]):
        raise McpRequestError(ATTRIBUTION_MISSING, "an MCP call names its session, once")
    if len(runs) > 1 or (runs and not _LABEL_RE.fullmatch(runs[0])):
        raise McpRequestError(ATTRIBUTION_MISSING, "an MCP call names its run at most once")
    return sessions[0], runs[0] if runs else None


def _bounded(text: str | None) -> str | None:
    """Agent-supplied text, shortened and made encodable for records."""
    if text is None:
        return None
    if len(text) > MAX_RECORDED_TEXT:
        text = text[:MAX_RECORDED_TEXT] + "…"
    return text.encode("utf-8", "backslashreplace").decode("utf-8")


@dataclass(frozen=True, slots=True)
class Listing:
    """A ``tools/list`` request whose answer the middleware reads, by OpenShell's request id.

    ``pin`` is the server's pinned tool list, each tool's digest by name (None: the name
    alone is pinned), or None for a server without one, whose listing gives hints only.
    """

    sandbox_id: str
    server: str
    subject: Mapping[str, object]
    first_page: bool
    pin: Mapping[str, str | None] | None = None
    rpc_id: str | int = ""
    """The JSON-RPC id of the ``tools/list`` request, whose response is rewritten."""
    rewrite: Rewrite | None = None
    """How a page is changed before the sandbox sees it: its denied tools dropped and the
    rest marked (``mcp_answers.callable_tools``)."""


@dataclass
class _Pages:
    tools: list[ListedTool] = field(default_factory=list[ListedTool])


@dataclass(frozen=True, slots=True)
class Drift:
    """How a server's complete tool listing differs from its pinned list."""

    server: str
    added: tuple[str, ...]
    removed: tuple[str, ...]
    changed: tuple[str, ...] = ()
    """Pinned tools whose name, description or input schema changed."""
    digests: Mapping[str, str] = field(default_factory=dict[str, str])
    """The listing's digest of each added and changed tool (``mcp_wire.tool_digest``)."""
    hints: Mapping[str, str] = field(default_factory=dict[str, str])
    """The class each added tool's hints give it (D96)."""


class ToolListings:
    """Reads the tool listings sandboxes receive: their tools' hints, which later calls are
    decided with, and drift from the pinned lists (D96's drift test).

    A server's pin is the operator's, from ``mcp_tool_pins`` (names only), else its
    classification's (``sandbox_science.policy.classification``), which pins each tool's
    digest. A listing can span pages, each its own request: pages are joined per sandbox
    and server from a first page to one without a cursor. A drift is a finding; it blocks
    nothing.
    """

    def __init__(self, pins: Mapping[str, Sequence[str]] | None = None) -> None:
        self._pins = {server: frozenset(names) for server, names in (pins or {}).items()}
        self._lock = threading.Lock()
        self._waiting: OrderedDict[str, Listing] = OrderedDict()
        self._pages: dict[tuple[str, str], _Pages] = {}
        self._hints: OrderedDict[tuple[str, str], dict[str, EffectClass]] = OrderedDict()

    def pin(
        self, name: str, server: McpServer, project: ServerClassification | None = None
    ) -> Mapping[str, str | None] | None:
        """The pinned tool list of server ``name``: the operator's, else that of its
        classification in force, the project's accepted revision (``project``) or the
        catalog's."""
        if name in self._pins:
            return dict.fromkeys(self._pins[name])
        found = server_classification(server, project)
        return found.pins if found is not None else None

    def pinned(self, server: str) -> bool:
        """Whether ``server`` has a pinned tool list from the operator."""
        return server in self._pins

    def hints(self, sandbox_id: str, server: str) -> dict[str, EffectClass]:
        """The classes the tools of the sandbox's latest complete listing declare."""
        with self._lock:
            return dict(self._hints.get((sandbox_id, server), {}))

    def expect(self, request_id: str, listing: Listing) -> None:
        """Note an allowed ``tools/list`` request, so that its answer is read."""
        if not request_id:
            return
        with self._lock:
            self._waiting[request_id] = listing
            while len(self._waiting) > MAX_TRACKED_LISTINGS:
                self._waiting.popitem(last=False)

    def answer(self, request_id: str) -> Listing | None:
        """The listing a response answers, once; None for any other response."""
        with self._lock:
            return self._waiting.pop(request_id, None)

    def page(self, listing: Listing, result: Mapping[str, object]) -> Drift | None:
        """Take one page of a listing; the drift once the last page has arrived."""
        _, cursor = mcp_wire.tool_names(result)
        tools = mcp_wire.listed_tools(result)
        key = (listing.sandbox_id, listing.server)
        with self._lock:
            pages = self._pages.setdefault(key, _Pages())
            if listing.first_page:
                pages.tools = []
            pages.tools += tools
            if cursor is not None:
                return None
            seen = {tool.name: tool for tool in self._pages.pop(key).tools}
            self._hints[key] = {name: tool.hint for name, tool in seen.items()}
            self._hints.move_to_end(key)
            while len(self._hints) > MAX_TRACKED_LISTINGS:
                self._hints.popitem(last=False)
        pinned = listing.pin
        if pinned is None:
            return None
        changed = tuple(
            sorted(
                name
                for name, digest in pinned.items()
                if digest is not None and name in seen and seen[name].digest != digest
            )
        )
        added = tuple(sorted(set(seen) - set(pinned)))
        removed = tuple(sorted(set(pinned) - set(seen)))
        if not (added or removed or changed):
            return None
        digests = {name: seen[name].digest for name in (*added, *changed)}
        hints = {name: seen[name].hint.value for name in added}
        return Drift(listing.server, added, removed, changed, digests, hints)


def load_tool_pins(path: Path) -> ToolListings:
    """Read pinned tool lists, ``{"<server>": ["<tool>", ...]}``; SettingsError if invalid."""
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SettingsError(f"cannot read the pinned tool lists {path}: {exc}") from exc
    pins: dict[str, list[str]] = {}
    servers = cast("dict[str, object]", value) if isinstance(value, dict) else None
    for server, names in (servers or {}).items():
        if not isinstance(names, list) or not all(
            isinstance(name, str) for name in cast("list[object]", names)
        ):
            raise SettingsError(f"{path}: each server's pinned tools are a list of names")
        pins[server] = cast("list[str]", names)
    if servers is None:
        raise SettingsError(f"{path}: pinned tool lists are an object of servers")
    return ToolListings(pins)


@dataclass(frozen=True, slots=True)
class ListingOutcome:
    """What becomes of one ``tools/list`` answer: the bytes the sandbox gets, or None when
    the answer cannot be read and is blocked, and any drift from the pinned list."""

    body: bytes | None
    drift: Drift | None = None


class ListingCheck:
    """Reads one whole ``tools/list`` answer, JSON or an event stream, before the sandbox
    gets it: a drift from the pinned list is recorded, and the answer is rewritten without
    the tools the policy denies (D98, as the MCP proxy does). An answer that cannot be read
    strictly is blocked, never passed unread: the agent would see tools it cannot use."""

    def __init__(
        self,
        listings: ToolListings,
        listing: Listing,
        *,
        stream: bool,
        events: EventSink,
        request_id: str,
        drifts: DriftLedger | None = None,
    ) -> None:
        """``drifts``, when given, learns of a drift at once: until its server's
        classification is accepted again, its non-read calls ask (D103)."""
        self._listings, self._listing, self._events = listings, listing, events
        self._drifts = drifts
        self._request_id = request_id
        self._stream = stream
        self._body = bytearray()
        self._over = False

    def feed(self, data: bytes) -> None:
        """Take the next piece of the answer."""
        if len(self._body) + len(data) > MAX_LISTING_BYTES:
            self._over = True
            return
        self._body += data

    def finish(self) -> ListingOutcome:
        """End the answer: the bytes to pass on, and its drift, recorded."""
        if self._over:
            _log.warning("tools/list answer to request %s is too large", self._request_id)
            return ListingOutcome(None)
        body = bytes(self._body)
        results: list[Mapping[str, object]] = []

        def read(result: dict[str, object]) -> dict[str, object]:
            results.append(result)
            rewrite = self._listing.rewrite
            return rewrite(result) if rewrite is not None else result

        try:
            rewritten = rewrite_answer(
                body, stream=self._stream, request_id=self._listing.rpc_id, rewrite=read
            )
            drift = self._listings.page(self._listing, results[0]) if results else None
        except McpWireError:
            _log.warning("unreadable tools/list answer to request %s", self._request_id)
            return ListingOutcome(None)
        if drift is not None:
            self._record(drift)
        return ListingOutcome(rewritten if self._listing.rewrite is not None else body, drift)

    def _record(self, drift: Drift) -> None:
        pinned = self._listing.pin
        pin = pin_digest(pinned) if pinned is not None else None
        added, changed = drift.added[:MAX_DRIFT_NAMES], drift.changed[:MAX_DRIFT_NAMES]
        try:
            seq = self._events.record(
                DRIFT_RECORD,
                {
                    **self._listing.subject,
                    "request_id": self._request_id,
                    "finding": DRIFT_FINDING,
                    "pin": pin,
                    "added": list(added),
                    "removed": list(drift.removed[:MAX_DRIFT_NAMES]),
                    "changed": list(changed),
                    "digests": {name: drift.digests[name] for name in (*added, *changed)},
                    "hints": {name: drift.hints[name] for name in added},
                    "counts": {
                        "added": len(drift.added),
                        "removed": len(drift.removed),
                        "changed": len(drift.changed),
                    },
                },
            )
        except Exception:
            _log.exception("could not record the tool list drift of %s", drift.server)
            seq = None
        if self._drifts is not None:
            # Escalates even when unrecorded: the drift was seen, whatever the store says.
            self._drifts.note(drift.server, pin, seq)
