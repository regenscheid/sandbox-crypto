# SPDX-License-Identifier: MIT
"""The MCP proxy's HTTP service: a Streamable HTTP endpoint per declared server, ``/mcp/<server>``.

For each request the proxy:

1. authenticates the caller's bearer token, which names its kind and project;
2. loads the policy revision the project is bound to;
3. parses the JSON-RPC message strictly: no batches, and no responses from
   callers, since no server request ever reaches them;
4. finds the caller's session, except for ``initialize``, which starts one;
5. decides the message with the policy library, with the caller's kind as its
   origin (``worker_access`` and ``worker_tools`` apply to workers);
6. applies its own checks: parameters within ``mcp.proxy.max_argument_bytes``,
   no credential placeholders, and for ``tools/call`` a tool name the server
   advertised, exactly;
7. holds escalations (recorded, then refused with ``approval_required``),
   records alerts before they proceed, and forwards the rest;
8. answers with the server's response, binary content withheld, and records
   the call.

Anything unexpected ends in a refusal: nothing reaches a server that the
policy and these checks have not passed.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Final, cast, get_args

from aiohttp import hdrs, web

from sandbox_science.policy import (
    Action,
    Decision,
    HardFloor,
    LoadedPolicy,
    PoolCeiling,
    evaluate_mcp_message,
)
from sandbox_science.policy.model import McpServer
from sandbox_science.policy.openshell import McpVersion

from . import jsonrpc
from .callers import SESSION_HEADER, Attribution, Caller, Callers, attribute
from .credentials import contains_placeholder, credential_headers
from .errors import MessageError, RefusalError, SessionGoneError, StoreError, UpstreamError
from .jsonrpc import JsonObject, Notification, Request, RequestId, Response
from .reasons import (
    APPROVAL_REQUIRED,
    ARGUMENTS_TOO_LARGE,
    AUDIT_UNAVAILABLE,
    CREDENTIAL_IN_BODY,
    LAYER_CODES,
    MESSAGES,
    ORIGIN_FORBIDDEN,
    POLICY_DENIED,
    POLICY_UNAVAILABLE,
    PROJECT_UNBOUND,
    PROTOCOL_UNSUPPORTED,
    PROXY_BUSY,
    REFUSED,
    REQUEST_TOO_LARGE,
    REQUEST_UNCLASSIFIABLE,
    SESSION_REQUIRED,
    SESSION_UNKNOWN,
    TOOL_NOT_ADVERTISED,
    UNAUTHENTICATED,
    UPSTREAM_ERROR,
    UPSTREAM_FAILED,
)
from .records import EventSink
from .results import tool_names, withhold_binary
from .sessions import Session, Sessions, SessionsFullError
from .store import PolicyStore
from .upstream import Exchange, Target, Upstream

_log = logging.getLogger(__name__)

SUPPORTED_REVISIONS: Final = ("2025-11-25", "2025-06-18", "2025-03-26")
"""MCP revisions the proxy speaks, newest first.

The policy library also knows 2026-07-28, which has no sessions and carries
its metadata in every request; the proxy does not speak it yet.
"""
CLIENT_INFO: Final[JsonObject] = {"name": "sandbox-science-mcp-proxy", "version": "0.1.0"}
SESSION_ID: Final = "Mcp-Session-Id"
PROTOCOL_VERSION: Final = "MCP-Protocol-Version"
MAX_LIST_PAGES: Final = 32
"""Pages of ``tools/list`` the proxy reads to learn a server's tools."""
MAX_RECORDED_TEXT: Final = 256

PARSE_ERROR: Final = -32700
INVALID_REQUEST: Final = -32600
INTERNAL_ERROR: Final = -32603


class _TransportError(Exception):
    """Ends a request early with a transport-level error."""

    def __init__(self, status: int, reason_code: str, detail: str, code: int = REFUSED) -> None:
        super().__init__(detail)
        self.status = status
        self.reason_code = reason_code
        self.code = code


@dataclass(eq=False)
class Call:
    """What the proxy learns about one request, for its records.

    ``outcome`` is ``forwarded`` (the server answered with a result),
    ``server_error`` (it answered with a JSON-RPC error), ``refused`` (nothing
    was sent), ``failed`` (sent, but no usable answer) or ``local`` (a
    notification the proxy handled itself).
    """

    attribution: Attribution
    server: str
    started: float
    policy: LoadedPolicy | None = None
    method: str | None = None
    tool: str | None = None
    session: Session | None = None
    decision: Decision | None = None
    reason_code: str | None = None
    outcome: str = "refused"
    sizes: dict[str, int] = field(default_factory=dict[str, int])
    stream: dict[str, int] = field(default_factory=dict[str, int])

    @property
    def caller(self) -> Caller:
        """The authenticated caller."""
        return self.attribution.caller

    def subject(self) -> dict[str, object]:
        """Who called which server, method and tool, under which policy."""
        policy = self.policy
        return {
            "attribution": self.attribution.record(),
            "server": _bounded(self.server),
            "mcp_session": self.session.handle if self.session is not None else None,
            "method": _bounded(self.method),
            "tool": _bounded(self.tool),
            "policy": (
                {"name": policy.policy.metadata.name, "digest": policy.digest}
                if policy is not None
                else None
            ),
        }

    def finding(self, decision: Decision) -> dict[str, object]:
        """An escalation or alert record."""
        payload = self.subject()
        payload["rule"] = decision.rule
        payload["reason"] = decision.reason
        if decision.escalation is not None:
            payload["escalation"] = {
                "timeout_seconds": int(decision.escalation.timeout.total_seconds()),
                "on_timeout": decision.escalation.on_timeout.value,
            }
        return payload

    def record(self, now: float) -> dict[str, object]:
        """The call's own record: decision, outcome and sizes, never arguments or results."""
        payload = self.subject()
        decision = self.decision
        payload["decision"] = (
            {"action": decision.action.value, "layer": decision.layer, "rule": decision.rule}
            if decision is not None
            else None
        )
        payload["reason_code"] = self.reason_code
        payload["outcome"] = self.outcome
        payload["sizes"] = dict(self.sizes)
        if self.stream:
            payload["stream"] = dict(self.stream)
        payload["duration_ms"] = int((now - self.started) * 1000)
        return payload


def _bounded(text: str | None) -> str | None:
    # Methods and tool names come from callers, and refused ones were never length-checked.
    if text is None or len(text) <= MAX_RECORDED_TEXT:
        return text
    return text[:MAX_RECORDED_TEXT] + "…"


class McpProxy:
    """Decides and forwards callers' MCP messages."""

    def __init__(
        self,
        *,
        store: PolicyStore,
        callers: Callers,
        ceiling: PoolCeiling,
        floor: HardFloor,
        events: EventSink,
        upstream: Upstream,
        sessions: Sessions,
        credentials: Mapping[str, str],
        environ: Mapping[str, str],
    ) -> None:
        self._store = store
        self._callers = callers
        self._ceiling = ceiling
        self._floor = floor
        self._events = events
        self._upstream = upstream
        self._sessions = sessions
        self._credentials = credentials
        self._environ = environ

    def application(self, *, max_body_bytes: int) -> web.Application:
        """The aiohttp application; request bodies over ``max_body_bytes`` are refused."""
        app = web.Application(client_max_size=max_body_bytes)
        app.router.add_route("POST", "/mcp/{server}", self._post)
        app.router.add_route("GET", "/mcp/{server}", self._get)
        app.router.add_route("DELETE", "/mcp/{server}", self._delete)
        return app

    # --- HTTP handlers --------------------------------------------------------------------

    async def _post(self, request: web.Request) -> web.Response:
        caller = self._authenticate(request)
        if isinstance(caller, web.Response):
            return caller
        call = Call(attribute(caller, ()), request.match_info["server"], time.monotonic())
        try:
            return await self._handle(request, call)
        except Exception:
            _log.exception("failed on a call to %s", call.server)
            call.reason_code = None
            call.outcome = "failed"
            return _answer(500, jsonrpc.error(None, INTERNAL_ERROR, "the MCP proxy failed"))
        finally:
            await self._audit(call)

    async def _get(self, request: web.Request) -> web.Response:
        caller = self._authenticate(request)
        if isinstance(caller, web.Response):
            return caller
        # No stream for server-initiated messages: the proxy relays none (MCP allows 405).
        return web.Response(status=405, headers={hdrs.ALLOW: "POST, DELETE"})

    async def _delete(self, request: web.Request) -> web.Response:
        caller = self._authenticate(request)
        if isinstance(caller, web.Response):
            return caller
        ids = request.headers.getall(SESSION_ID, [])
        server = request.match_info["server"]
        session = self._sessions.get(ids[0], caller, server) if len(ids) == 1 else None
        if session is None:
            return _refusal(404, None, SESSION_UNKNOWN)
        # The upstream session is dropped, not deleted: OpenShell's MCP inspection
        # refuses DELETE, and servers end idle sessions themselves.
        self._sessions.remove(session)
        return web.Response(status=204)

    def _authenticate(self, request: web.Request) -> Caller | web.Response:
        if hdrs.ORIGIN in request.headers:
            # Callers are services, never web pages (MCP: refuse an invalid Origin with 403).
            return _refusal(403, None, ORIGIN_FORBIDDEN)
        caller = self._callers.authenticate(request.headers.get(hdrs.AUTHORIZATION))
        if caller is None:
            _log.info("refused an unauthenticated request from %s", request.remote)
            response = _refusal(401, None, UNAUTHENTICATED)
            response.headers[hdrs.WWW_AUTHENTICATE] = "Bearer"
            return response
        return caller

    # --- One request -----------------------------------------------------------------------

    async def _handle(self, request: web.Request, call: Call) -> web.Response:
        request_id: RequestId | None = None
        try:
            message, value, loaded = await self._read(request, call)
            if isinstance(message, Request):
                request_id = message.id
            session: Session | None = None
            if message.method != "initialize":
                session = self._session(request, call)
            elif SESSION_ID in request.headers:
                raise _TransportError(
                    400, REQUEST_UNCLASSIFIABLE, "initialize starts a new session"
                )
            decision = evaluate_mcp_message(
                call.server,
                value,
                loaded,
                origin=call.caller.kind,
                ceiling=self._ceiling,
                floor=self._floor,
            )
            call.decision = decision
            if decision.action is Action.DENY:
                raise RefusalError(LAYER_CODES[decision.layer], decision.reason)
            server = loaded.policy.mcp.servers[call.server]
            if isinstance(message, Request) and session is not None:
                return await self._forward(call, loaded, server, session, message, decision)
            # Protocol methods only: the library allows them or denies them.
            if decision.action is not Action.ALLOW:
                raise RefusalError(POLICY_DENIED, f"unexpected decision {decision.action}")
            if isinstance(message, Request):
                return await self._initialize(call, loaded, server, message)
            if session is None:
                raise _TransportError(400, SESSION_REQUIRED, "a notification needs a session")
            return await self._notify(call, server, session, message)
        except _TransportError as stop:
            _log.info("refused a call to %s: %s", call.server, stop)
            return self._refused(call, request_id, stop.reason_code, stop.status, stop.code)
        except RefusalError as refusal:
            _log.info("refused a call to %s: %s", call.server, refusal)
            status = 200 if request_id is not None else 403
            return self._refused(call, request_id, refusal.reason_code, status, REFUSED)
        except SessionGoneError:
            if call.session is not None:
                self._sessions.remove(call.session)
            return self._refused(call, request_id, SESSION_UNKNOWN, 404, REFUSED)
        except UpstreamError as failure:
            _log.warning("a call to %s failed: %s", call.server, failure)
            call.outcome = "failed"
            status = 200 if request_id is not None else 502
            return self._refused(call, request_id, failure.reason_code, status, UPSTREAM_FAILED)

    async def _read(
        self, request: web.Request, call: Call
    ) -> tuple[Request | Notification, object, LoadedPolicy]:
        """Check the request's form, load the project's policy and read the message."""
        if not _accepts_json(request.headers.getall(hdrs.ACCEPT, [])):
            raise _TransportError(
                406, REQUEST_UNCLASSIFIABLE, "the caller must accept application/json"
            )
        if request.content_type != "application/json":
            raise _TransportError(415, REQUEST_UNCLASSIFIABLE, "the body must be application/json")
        try:
            body = await request.read()
        except web.HTTPRequestEntityTooLarge as exc:
            raise _TransportError(413, REQUEST_TOO_LARGE, "the body is too large") from exc
        call.sizes["request"] = len(body)
        try:
            call.attribution = attribute(call.caller, request.headers.getall(SESSION_HEADER, []))
        except ValueError as exc:
            raise _TransportError(400, REQUEST_UNCLASSIFIABLE, str(exc)) from exc
        loaded = self._policy(call.caller.project)
        call.policy = loaded
        try:
            value = jsonrpc.loads(body)
        except MessageError as exc:
            raise _TransportError(400, REQUEST_UNCLASSIFIABLE, str(exc), PARSE_ERROR) from exc
        try:
            message = jsonrpc.parse_message(value)
        except MessageError as exc:
            # Batches end here; the library would deny them too.
            raise _TransportError(400, REQUEST_UNCLASSIFIABLE, str(exc), INVALID_REQUEST) from exc
        if isinstance(message, Response):
            raise _TransportError(
                400, REQUEST_UNCLASSIFIABLE, "callers send no responses", INVALID_REQUEST
            )
        call.method = message.method
        if isinstance(message, Notification) != message.method.startswith("notifications/"):
            raise _TransportError(
                400, REQUEST_UNCLASSIFIABLE, "only notifications go without an id", INVALID_REQUEST
            )
        name = (message.params or {}).get("name")
        if message.method == "tools/call" and isinstance(name, str):
            call.tool = name
        return message, value, loaded

    def _policy(self, project: str) -> LoadedPolicy:
        try:
            digest = self._store.binding(project)
            if digest is None:
                raise _TransportError(
                    503, PROJECT_UNBOUND, f"project {project} is not bound to a policy"
                )
            return self._store.policy(digest)
        except StoreError as exc:
            raise _TransportError(503, POLICY_UNAVAILABLE, str(exc)) from exc

    def _session(self, request: web.Request, call: Call) -> Session:
        ids = request.headers.getall(SESSION_ID, [])
        if len(ids) != 1:
            raise _TransportError(400, SESSION_REQUIRED, f"{len(ids)} {SESSION_ID} headers")
        session = self._sessions.get(ids[0], call.caller, call.server)
        if session is None:
            raise _TransportError(
                404, SESSION_UNKNOWN, "no such session for this caller and server"
            )
        call.session = session
        versions = request.headers.getall(PROTOCOL_VERSION, [])
        if len(versions) > 1 or (versions and versions[0] != session.version):
            raise _TransportError(
                400,
                PROTOCOL_UNSUPPORTED,
                f"the session uses MCP {session.version}",
                INVALID_REQUEST,
            )
        return session

    # --- Methods ---------------------------------------------------------------------------

    async def _initialize(
        self, call: Call, loaded: LoadedPolicy, server: McpServer, message: Request
    ) -> web.Response:
        """Open an upstream session for the caller and complete its handshake."""
        requested = (message.params or {}).get("protocolVersion")
        if not isinstance(requested, str):
            raise RefusalError(REQUEST_UNCLASSIFIABLE, "initialize needs a protocolVersion")
        pinned = _pinned_revision(server)
        offer = pinned or (
            requested if requested in SUPPORTED_REVISIONS else SUPPORTED_REVISIONS[0]
        )
        if self._sessions.full():
            raise _TransportError(503, PROXY_BUSY, "the session limit is reached")
        # The proxy's own handshake: no client capabilities, so the server has
        # no grounds for sampling, elicitation or roots requests.
        params: JsonObject = {
            "protocolVersion": offer,
            "capabilities": {},
            "clientInfo": CLIENT_INFO,
        }
        exchange = await self._exchange(
            call, loaded, self._target(server, None), 0, "initialize", params
        )
        response = exchange.response
        if response.result is None:
            return self._relay_error(call, message.id, response)
        result = response.result
        version = result.get("protocolVersion")
        if (
            not isinstance(version, str)
            or version not in SUPPORTED_REVISIONS
            or (pinned is not None and version != pinned)
        ):
            raise UpstreamError(PROTOCOL_UNSUPPORTED, f"the server answered {str(version)[:64]!r}")
        capabilities = result.get("capabilities")
        if not isinstance(capabilities, dict) or not isinstance(result.get("serverInfo"), dict):
            raise UpstreamError(UPSTREAM_ERROR, "initialize needs capabilities and serverInfo")
        try:
            session = self._sessions.add(
                call.caller, call.server, server.url, version, exchange.session_id
            )
        except SessionsFullError as exc:
            raise _TransportError(503, PROXY_BUSY, str(exc)) from exc
        call.session = session
        try:
            await self._upstream.send(
                self._target(server, session), jsonrpc.notification("notifications/initialized")
            )
        except (RefusalError, UpstreamError):
            self._sessions.remove(session)
            raise
        # The policy denies the task methods, so the proxy does not offer tasks.
        offered = {
            key: entry for key, entry in cast("JsonObject", capabilities).items() if key != "tasks"
        }
        answer = jsonrpc.result(message.id, {**result, "capabilities": offered})
        return _server_answer(answer, {SESSION_ID: session.id})

    async def _forward(
        self,
        call: Call,
        loaded: LoadedPolicy,
        server: McpServer,
        session: Session,
        message: Request,
        decision: Decision,
    ) -> web.Response:
        """Check, hold or forward one request in an established session."""
        if session.url != server.url or _pinned_revision(server) not in (None, session.version):
            # The project's policy changed the server's entry since initialize.
            raise SessionGoneError(SESSION_UNKNOWN, "the server's entry in the policy changed")
        params = _forwarded_params(message.params)
        encoded = _dumps(params) if params is not None else b""
        call.sizes["params"] = len(encoded)
        limit = loaded.policy.mcp.proxy.max_argument_bytes
        if len(encoded) > limit:
            raise RefusalError(
                ARGUMENTS_TOO_LARGE, f"params are {len(encoded)} bytes, over {limit}"
            )
        if contains_placeholder(encoded):
            raise RefusalError(
                CREDENTIAL_IN_BODY, "the parameters contain a credential placeholder"
            )
        target = self._target(server, session)
        if message.method == "tools/call":
            # The library refuses tools/call without a string name, so call.tool is set.
            await self._check_advertised(loaded, session, target, call.tool or "")
        if decision.action is Action.ESCALATE:
            await self._record_finding("escalation", call, decision)
            raise RefusalError(APPROVAL_REQUIRED, decision.reason)
        if decision.action is Action.ALERT:
            try:
                await self._events.record("alert", call.finding(decision))
            except Exception as exc:
                # An alert lets a call through only with a recorded finding.
                _log.exception("could not record an alert")
                raise RefusalError(AUDIT_UNAVAILABLE, "the alert could not be recorded") from exc
        upstream_id = session.request_id()
        session.in_flight[message.id] = upstream_id
        try:
            exchange = await self._exchange(
                call, loaded, target, upstream_id, message.method, params
            )
        finally:
            if session.in_flight.get(message.id) == upstream_id:
                del session.in_flight[message.id]
        if exchange.tools_changed:
            session.tools.invalidate()
        response = exchange.response
        if response.result is None:
            return self._relay_error(call, message.id, response)
        result = response.result
        if message.method == "tools/list":
            cursor = (params or {}).get("cursor")
            try:
                names, next_cursor = tool_names(result)
                session.tools.page(cursor if isinstance(cursor, str) else None, names, next_cursor)
            except (MessageError, ValueError) as exc:
                raise UpstreamError(UPSTREAM_ERROR, str(exc)) from exc
        result, withheld = withhold_binary(message.method, result)
        if withheld:
            call.stream["withheld_binary"] = withheld
        return _server_answer(jsonrpc.result(message.id, result))

    async def _notify(
        self, call: Call, server: McpServer, session: Session, message: Notification
    ) -> web.Response:
        """Accept a notification. Only cancellations of calls in flight go upstream.

        The proxy completed the upstream handshake itself and passes no server
        requests on, so ``notifications/initialized`` and progress or roots
        notifications have nothing upstream to refer to.
        """
        call.outcome = "local"
        if message.method == "notifications/cancelled":
            request_id = (message.params or {}).get("requestId")
            upstream_id = (
                session.in_flight.get(request_id)
                if isinstance(request_id, str | int) and not isinstance(request_id, bool)
                else None
            )
            if upstream_id is not None:
                await self._upstream.send(
                    self._target(server, session),
                    jsonrpc.notification("notifications/cancelled", {"requestId": upstream_id}),
                )
                call.outcome = "forwarded"
        return web.Response(status=202)

    # --- Upstream ---------------------------------------------------------------------------

    async def _exchange(
        self,
        call: Call,
        loaded: LoadedPolicy,
        target: Target,
        upstream_id: int,
        method: str,
        params: JsonObject | None,
    ) -> Exchange:
        """Send a caller's request upstream and note what came back."""
        body = _dumps(jsonrpc.request(upstream_id, method, params))
        limit = loaded.policy.mcp.proxy.max_response_bytes
        exchange = await self._upstream.request(target, body, request_id=upstream_id, limit=limit)
        call.outcome = "forwarded" if exchange.response.result is not None else "server_error"
        call.sizes["response"] = exchange.received_bytes
        if exchange.notifications:
            call.stream["notifications"] = exchange.notifications
        if exchange.refused_requests:
            call.stream["refused_server_requests"] = exchange.refused_requests
        return exchange

    async def _check_advertised(
        self, loaded: LoadedPolicy, session: Session, target: Target, tool: str
    ) -> None:
        """Refuse a tool name unless the server's latest complete listing has it, exactly."""
        if session.tools.names is None:
            async with session.listing:
                if session.tools.names is None:
                    await self._list_tools(loaded, session, target)
        names = session.tools.names
        if names is None or tool not in names:
            raise RefusalError(TOOL_NOT_ADVERTISED, f"{tool!r} is not in the server's tool list")

    async def _list_tools(self, loaded: LoadedPolicy, session: Session, target: Target) -> None:
        """Read every page of the server's tool list into the session."""
        limit = loaded.policy.mcp.proxy.max_response_bytes
        cursor: str | None = None
        for _ in range(MAX_LIST_PAGES):
            upstream_id = session.request_id()
            params: JsonObject = {} if cursor is None else {"cursor": cursor}
            body = _dumps(jsonrpc.request(upstream_id, "tools/list", params))
            exchange = await self._upstream.request(
                target, body, request_id=upstream_id, limit=limit
            )
            result = exchange.response.result
            if result is None:
                raise UpstreamError(UPSTREAM_ERROR, "the server refused tools/list")
            try:
                names, next_cursor = tool_names(result)
                session.tools.page(cursor, names, next_cursor)
            except (MessageError, ValueError) as exc:
                raise UpstreamError(UPSTREAM_ERROR, str(exc)) from exc
            if next_cursor is None:
                return
            cursor = next_cursor
        raise UpstreamError(UPSTREAM_ERROR, f"tools/list has more than {MAX_LIST_PAGES} pages")

    def _target(self, server: McpServer, session: Session | None) -> Target:
        return Target(
            url=server.url,
            credentials=credential_headers(server.auth, self._credentials, self._environ),
            session_id=session.upstream_id if session is not None else None,
            version=session.version if session is not None else None,
        )

    # --- Answers and records -----------------------------------------------------------------

    def _refused(
        self, call: Call, request_id: RequestId | None, reason_code: str, status: int, code: int
    ) -> web.Response:
        call.reason_code = reason_code
        if call.outcome != "failed":
            call.outcome = "refused"
        return _refusal(status, request_id, reason_code, code)

    def _relay_error(self, call: Call, request_id: RequestId, response: Response) -> web.Response:
        """Pass a server's JSON-RPC error to the caller, under the caller's id."""
        call.outcome = "server_error"
        return _server_answer({"jsonrpc": "2.0", "id": request_id, "error": response.error})

    async def _record_finding(self, kind: str, call: Call, decision: Decision) -> None:
        try:
            await self._events.record(kind, call.finding(decision))
        except Exception:
            # The call is refused anyway; the log keeps what the store could not.
            _log.exception("could not record %s for a call to %s", kind, call.server)

    async def _audit(self, call: Call) -> None:
        try:
            await self._events.record("mcp-call", call.record(time.monotonic()))
        except Exception:
            _log.exception("could not record a call to %s", call.server)


def _pinned_revision(server: McpServer) -> str | None:
    """The revision a server's policy entry pins, which OpenShell's fence enforces, or None.

    Raises RefusalError for a revision the library knows but the proxy cannot speak.
    """
    if server.revision in SUPPORTED_REVISIONS:
        return server.revision
    if server.revision in get_args(McpVersion):
        raise RefusalError(
            PROTOCOL_UNSUPPORTED, f"the proxy does not speak MCP {server.revision} yet"
        )
    return None


def _forwarded_params(params: JsonObject | None) -> JsonObject | None:
    """The parameters as forwarded: ``_meta`` is dropped, and task requests are refused.

    ``_meta`` carries progress tokens and other caller metadata the proxy does
    not relay. A task-augmented request would start work whose result only the
    task methods return, and the policy denies those.
    """
    if params is None:
        return None
    if "task" in params:
        raise RefusalError(REQUEST_UNCLASSIFIABLE, "task-augmented requests are not supported")
    return {key: value for key, value in params.items() if key != "_meta"}


def _dumps(value: object) -> bytes:
    try:
        return jsonrpc.dumps(value)
    except MessageError as exc:
        raise RefusalError(REQUEST_UNCLASSIFIABLE, str(exc)) from exc


def _accepts_json(values: Sequence[str]) -> bool:
    if not values:
        return True
    for value in values:
        for item in value.split(","):
            media = item.split(";", 1)[0].strip().lower()
            if media in ("application/json", "application/*", "*/*"):
                return True
    return False


def _answer(
    status: int, payload: JsonObject, headers: Mapping[str, str] | None = None
) -> web.Response:
    """A JSON answer. Raises MessageError if the payload cannot be serialized."""
    return web.Response(
        status=status,
        body=jsonrpc.dumps(payload),
        content_type="application/json",
        headers=headers,
    )


def _server_answer(payload: JsonObject, headers: Mapping[str, str] | None = None) -> web.Response:
    """A JSON answer built from a server's response, which may hold what JSON cannot carry."""
    try:
        return _answer(200, payload, headers)
    except MessageError as exc:
        raise UpstreamError(UPSTREAM_ERROR, f"the answer cannot be passed on: {exc}") from exc


def _refusal(
    status: int, request_id: RequestId | None, reason_code: str, code: int = REFUSED
) -> web.Response:
    message = f"{reason_code}: {MESSAGES[reason_code]}"
    return _answer(status, jsonrpc.error(request_id, code, message, {"reason": reason_code}))
