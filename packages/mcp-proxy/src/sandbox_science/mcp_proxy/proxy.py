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
7. holds escalations (recorded, then refused with ``approval_required``;
   ``audit_unavailable`` if the record cannot be stored, since there is then
   no request to approve), records alerts before they proceed, and forwards
   the rest;
8. answers with the server's response, binary content withheld, and records
   the call.

Anything unexpected ends in a refusal: nothing reaches a server that the
policy and these checks have not passed.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Final, Literal, cast, get_args

from aiohttp import hdrs, web

from sandbox_science.contracts.mcp_metadata import McpPolicyMetadata, McpServiceMetadata
from sandbox_science.policy import (
    Action,
    Decision,
    HardFloor,
    LoadedPolicy,
    PoolCeiling,
    evaluate_mcp_message,
)
from sandbox_science.policy.approvals import (
    Approvals,
    Approved,
    Denied,
    RecordLike,
    RequestKey,
    Resolution,
    TimedOut,
    Unrecorded,
    Waiting,
    prepare_answer,
)
from sandbox_science.policy.effects import EffectClass
from sandbox_science.policy.mcp_answers import callable_tools, tool_summary
from sandbox_science.policy.mcp_wire import (
    MCP_REQUEST_PARAMS,
    McpWireError,
    forwarded_params,
    listed_tools,
    params_digest,
)
from sandbox_science.policy.model import McpServer
from sandbox_science.policy.openshell import McpVersion

from . import jsonrpc
from .callers import SESSION_HEADER, Attribution, Caller, Callers, attribute
from .credentials import contains_placeholder, credential_headers
from .errors import MessageError, RefusalError, SessionGoneError, StoreError, UpstreamError
from .jsonrpc import JsonObject, Notification, Request, RequestId, Response
from .metadata import Metadata
from .reasons import (
    APPROVAL_DENIED,
    APPROVAL_REQUIRED,
    ARGUMENTS_TOO_LARGE,
    AUDIT_UNAVAILABLE,
    CREDENTIAL_IN_BODY,
    ESCALATION_TIMED_OUT,
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
_RECORDING_LOCKS: Final = 64
MAX_RECORDED_TEXT: Final = 256
MAX_RESPONSE_BYTES: Final = 32 * 1024 * 1024
"""The most of one answer the proxy reads, whatever ``max_response_bytes`` the policy sets."""

PARAMS: Final = MCP_REQUEST_PARAMS
"""The parameters MCP defines for each request the proxy forwards, besides ``_meta``
(the policy library's, which the middleware applies too). Anything else is refused: a
server whose JSON decoder matches keys without regard to case would read ``NAME`` as the
tool name the policy never saw."""

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
        payload["caller_kind"] = caller_kind(self.caller.kind)
        payload["rule"] = decision.rule
        payload["reason"] = decision.reason
        payload.update(_class_fields(decision))
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
            {
                "action": decision.action.value,
                "layer": decision.layer,
                "rule": decision.rule,
                **_class_fields(decision),
            }
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
    """Caller-controlled text, shortened and made encodable for records and logs.

    Methods and tool names come from callers, and refused ones were never
    checked. A lone surrogate (from a JSON escape) has no UTF-8 encoding,
    and the event store would refuse the record.
    """
    if text is None:
        return None
    if len(text) > MAX_RECORDED_TEXT:
        text = text[:MAX_RECORDED_TEXT] + "…"
    return text.encode("utf-8", "backslashreplace").decode("utf-8")


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
        requests: Callable[[int], Iterable[RecordLike]] | None = None,
        answers: Callable[[str, dict[str, object]], Awaitable[int]] | None = None,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        """``requests`` reads the event store's records after a sequence number, from which
        permission requests and their answers are known (D76). Without it, every escalation
        is recorded and refused, and none can be approved."""
        self._metadata = Metadata()
        self._requests = requests
        self._answers = answers
        """Appends an operator's approval or denial and returns its sequence number;
        without it, an operator caller can read requests but not answer them."""
        self._now = now
        self._approvals = Approvals()
        self._recording = tuple(asyncio.Lock() for _ in range(_RECORDING_LOCKS))
        """Held while a permission request is recorded, by its fingerprint: identical
        escalations handled at once record one request."""
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
        app.router.add_get("/metadata/{server}", self._metadata_view)
        app.router.add_get("/permission-requests", self._permission_requests)
        app.router.add_post("/permission-requests/{seq}/answer", self._answer_request)
        app.router.add_route("POST", "/mcp/{server}", self._post)
        app.router.add_route("GET", "/mcp/{server}", self._get)
        app.router.add_route("DELETE", "/mcp/{server}", self._delete)
        return app

    # --- HTTP handlers --------------------------------------------------------------------

    async def _metadata_view(self, request: web.Request) -> web.Response:
        """Authenticated operator metadata; no upstream operation is performed."""
        caller = self._authenticate(request)
        if isinstance(caller, web.Response):
            return caller
        if caller.kind != "orchestrator" or request.query or request.can_read_body:
            return web.Response(status=403)
        try:
            loaded = self._policy(caller.project)
        except _TransportError:
            return web.Response(status=503)
        name = request.match_info["server"]
        server = loaded.policy.mcp.servers.get(name)
        if server is None:
            return web.Response(status=404)

        def action(method: str, tool: str | None = None) -> str:
            params: JsonObject = {"name": tool, "arguments": {}} if tool else {}
            return evaluate_mcp_message(
                name,
                jsonrpc.request(0, method, params),
                loaded,
                origin="orchestrator",
                ceiling=self._ceiling,
                floor=self._floor,
            ).action.value

        snapshot = self._metadata.tools.get((caller.project, name))
        observation = self._metadata.observations.get((caller.project, name))
        settings = loaded.policy.mcp.proxy
        answer = McpServiceMetadata(
            server=name,
            observed_at=self._now(),
            policy=McpPolicyMetadata(
                revision=loaded.digest,
                protocol=server.revision,
                authentication=server.auth.mode,
                default_action=loaded.policy.defaults.action.value,
                listing_action=action("tools/list"),
                worker_access=settings.worker_access,
                worker_tools=(settings.worker_tools,)
                if isinstance(settings.worker_tools, str)
                else settings.worker_tools,
                allow=server.tools.allow,
                escalate=server.tools.escalate,
                deny=server.tools.deny,
                argument_bytes=settings.max_argument_bytes,
                response_bytes=settings.max_response_bytes,
            ),
            checked_at=observation[0] if observation else None,
            status=observation[1] if observation else "unknown",
            listing_at=snapshot[0] if snapshot else None,
            listing_policy=snapshot[1] if snapshot else None,
            tools=tuple(
                item.model_copy(update={"action": action("tools/call", item.name)})
                for item in snapshot[2]
            )
            if snapshot
            else (),
            truncated=(snapshot[3] or (caller.project, name) in self._metadata.incomplete)
            if snapshot
            else False,
        )
        return web.Response(
            text=answer.model_dump_json(),
            content_type="application/json",
            headers={hdrs.CACHE_CONTROL: "no-store"},
        )

    async def _permission_requests(self, request: web.Request) -> web.Response:
        """The operator's project's MCP permission requests nobody answered, oldest first,
        each with the call's server, method and tool, the session the call came from, the
        rule's reason, and when it times out, and for its card and grants (D97) the
        caller's kind, the call's effect class and ceiling and its significant arguments.
        Never the call's arguments: the request holds only their digest."""
        caller = self._authenticate(request)
        if isinstance(caller, web.Response):
            return caller
        if caller.kind != "operator" or request.query or request.can_read_body:
            return web.Response(status=403)
        await self._refresh_approvals()
        now = self._now()
        waiting = [
            found
            for found in self._approvals.unanswered()
            if found.key.kind == "mcp" and found.key.fields()["project_id"] == caller.project
        ]
        recorded = await asyncio.to_thread(self._escalations, {found.seq for found in waiting})
        items: list[dict[str, object]] = []
        for found in waiting:
            fields = found.key.fields()
            detail = recorded.get(found.seq, {})
            items.append(
                {
                    "seq": found.seq,
                    "server": fields["server"],
                    "method": fields["method"],
                    "tool": fields["tool"],
                    "policy": fields["policy_digest"],
                    "session": detail.get("session"),
                    "rule": detail.get("rule"),
                    "reason": detail.get("reason"),
                    "recorded_at": found.recorded.isoformat(timespec="seconds"),
                    "expires_at": (found.recorded + found.timeout).isoformat(timespec="seconds"),
                    "timed_out": found.expired(now),
                    "on_timeout": found.on_timeout,
                    "caller_kind": detail.get("caller_kind"),
                    "effect": detail.get("effect"),
                    "max_grant": detail.get("max_grant"),
                    "summary": detail.get("summary", {}),
                }
            )
        return web.json_response({"requests": items}, headers={hdrs.CACHE_CONTROL: "no-store"})

    def _escalations(self, seqs: set[int]) -> dict[int, dict[str, object]]:
        """The session, rule and reason of the escalation records ``seqs``. Blocking."""
        if not seqs or self._requests is None:
            return {}
        found: dict[int, dict[str, object]] = {}
        for record in self._requests(min(seqs) - 1):
            if record.seq not in seqs or record.kind != "escalation":
                continue
            try:
                data = record.data()
            except ValueError:
                continue
            attribution = data.get("attribution")
            session = (
                cast(dict[str, object], attribution).get("session")
                if isinstance(attribution, dict)
                else None
            )
            kind = (
                cast(dict[str, object], attribution).get("kind")
                if isinstance(attribution, dict)
                else None
            )
            summary = data.get("summary")
            found[record.seq] = {
                "session": session if isinstance(session, str) else None,
                "rule": data.get("rule") if isinstance(data.get("rule"), str) else None,
                "reason": data.get("reason") if isinstance(data.get("reason"), str) else None,
                # From the attribution, which every record has: a record from before
                # caller kinds were recorded is told apart the same way.
                "caller_kind": caller_kind(kind) if isinstance(kind, str) else "code",
                "effect": _text(data.get("effect")),
                "max_grant": _text(data.get("max_grant")),
                "summary": {
                    str(name): str(value)
                    for name, value in cast(dict[object, object], summary).items()
                    if isinstance(name, str) and isinstance(value, str)
                }
                if isinstance(summary, dict)
                else {},
            }
        return found

    async def _answer_request(self, request: web.Request) -> web.Response:
        """An operator's approval or denial of one of its project's MCP permission
        requests: ``{"approve": bool, "scope": "call" | "tool", "hours": null | number}``.
        An answer only ever covers calls the policy escalates (``resolve`` is asked of
        nothing else), so no scope reaches a denied tool."""
        caller = self._authenticate(request)
        if isinstance(caller, web.Response):
            return caller
        if caller.kind != "operator" or request.query:
            return web.Response(status=403)
        raw = request.match_info["seq"]
        if not raw.isascii() or not raw.isdigit() or len(raw) > 18:
            return web.json_response({"error": "invalid permission request"}, status=400)
        try:
            raw_body: object = await request.json()
        except ValueError:
            return web.json_response({"error": "invalid answer"}, status=400)
        body = cast(dict[str, object], raw_body) if isinstance(raw_body, dict) else None
        approve, scope, hours = (
            (body.get("approve"), body.get("scope"), body.get("hours"))
            if body is not None
            else (None, None, None)
        )
        if (
            body is None
            or set(body) - {"approve", "scope", "hours"}
            or not isinstance(approve, bool)
            or not isinstance(scope, str)
            or not (
                hours is None or (isinstance(hours, int | float) and not isinstance(hours, bool))
            )
        ):
            return web.json_response({"error": "invalid answer"}, status=400)
        await self._refresh_approvals()
        found = self._approvals.request(int(raw))
        if (
            found is None
            or found.key.kind != "mcp"
            or found.key.fields()["project_id"] != caller.project
        ):
            return web.json_response({"error": "no such permission request"}, status=404)
        if self._answers is None:
            return web.json_response({"error": "answers cannot be recorded"}, status=503)
        try:
            kind, payload = prepare_answer(
                self._approvals,
                found.seq,
                approve=approve,
                scope=scope,
                hours=None if hours is None else float(hours),
                now=self._now(),
            )
        except ValueError as exc:
            return web.json_response({"error": str(exc)}, status=400)
        try:
            seq = await self._answers(kind, payload)
        except Exception:
            _log.exception("could not record an answer to a permission request")
            return web.json_response({"error": "the answer could not be recorded"}, status=503)
        await self._refresh_approvals()
        return web.json_response({"answer": seq, "kind": kind}, status=201)

    async def _post(self, request: web.Request) -> web.Response:
        caller = self._authenticate(request)
        if isinstance(caller, web.Response):
            return caller
        if caller.kind == "operator":
            return web.Response(status=403)  # an operator answers requests; it calls nothing
        call = Call(attribute(caller, ()), request.match_info["server"], time.monotonic())
        try:
            return await self._handle(request, call)
        except Exception:
            _log.exception("failed on a call to %r", _bounded(call.server))
            call.reason_code = None
            call.outcome = "failed"
            return _answer(500, jsonrpc.error(None, INTERNAL_ERROR, "the MCP proxy failed"))
        finally:
            await self._audit(call)

    async def _get(self, request: web.Request) -> web.Response:
        caller = self._authenticate(request)
        if isinstance(caller, web.Response):
            return caller
        if caller.kind == "operator":
            return web.Response(status=403)
        # No stream for server-initiated messages: the proxy relays none (MCP allows 405).
        return web.Response(status=405, headers={hdrs.ALLOW: "POST, DELETE"})

    async def _delete(self, request: web.Request) -> web.Response:
        caller = self._authenticate(request)
        if isinstance(caller, web.Response):
            return caller
        if caller.kind == "operator":
            return web.Response(status=403)
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
                origin=call.caller.origin,
                ceiling=self._ceiling,
                floor=self._floor,
                hints=session.tools.hints if session is not None else None,
            )
            call.decision = decision
            if decision.action is Action.DENY:
                raise RefusalError(LAYER_CODES[decision.layer], decision.reason)
            server = loaded.policy.mcp.servers[call.server]
            if session is not None and (
                session.url != server.url or _pinned_revision(server) not in (None, session.version)
            ):
                # The project's policy changed the server's entry since initialize.
                raise SessionGoneError(SESSION_UNKNOWN, "the server's entry in the policy changed")
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
            _log.info("refused a call to %r: %s", _bounded(call.server), stop)
            return self._refused(call, request_id, stop.reason_code, stop.status, stop.code)
        except RefusalError as refusal:
            _log.info("refused a call to %r: %s", _bounded(call.server), refusal)
            status = 200 if request_id is not None else 403
            return self._refused(
                call, request_id, refusal.reason_code, status, REFUSED, data=refusal.data
            )
        except SessionGoneError:
            if call.session is not None:
                self._sessions.remove(call.session)
            return self._refused(call, request_id, SESSION_UNKNOWN, 404, REFUSED)
        except UpstreamError as failure:
            _log.warning("a call to %r failed: %s", _bounded(call.server), failure)
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
        if not self._sessions.has_room(call.caller):
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
        # The policy denies the task methods, so the proxy does not offer tasks.
        offered = {
            key: entry for key, entry in cast("JsonObject", capabilities).items() if key != "tasks"
        }
        body = _server_body(jsonrpc.result(message.id, {**result, "capabilities": offered}))
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
        except BaseException:
            self._sessions.remove(session)
            raise
        return _json_response(200, body, {SESSION_ID: session.id})

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
        params = _forwarded_params(message.method, message.params)
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
            summary = _call_summary(server, call.tool, params)
            await self._escalation(call, loaded, decision, message.method, encoded, summary)
        elif decision.action is Action.ALERT:
            try:
                await self._events.record("alert", call.finding(decision))
            except Exception as exc:
                # An alert lets a call through only with a recorded finding.
                _log.exception("could not record an alert")
                raise RefusalError(AUDIT_UNAVAILABLE, "the alert could not be recorded") from exc
        elif decision.action is not Action.ALLOW:
            raise RefusalError(POLICY_DENIED, f"unexpected decision {decision.action}")
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
            self._metadata.tools.pop((call.caller.project, call.server), None)
        response = exchange.response
        if response.result is None:
            return self._relay_error(call, message.id, response)
        result = response.result
        if message.method == "tools/list":
            cursor = (params or {}).get("cursor")
            try:
                names, next_cursor = tool_names(result)
                session.tools.page(
                    cursor if isinstance(cursor, str) else None, names, next_cursor, _hints(result)
                )
                self._metadata.listing(
                    call.caller.project,
                    call.server,
                    None,
                    loaded.digest,
                    result,
                    first=cursor is None,
                )
            except (MessageError, ValueError) as exc:
                raise UpstreamError(UPSTREAM_ERROR, str(exc)) from exc
            result = self._callable(call, loaded, result)
        result, withheld = withhold_binary(message.method, result)
        if withheld:
            call.stream["withheld_binary"] = withheld
        return _server_answer(jsonrpc.result(message.id, result))

    def _callable(self, call: Call, loaded: LoadedPolicy, result: JsonObject) -> JsonObject:
        """A ``tools/list`` page without the tools the caller's policy denies, judged as
        a call by name alone, so an agent is not offered what it can never call (lab 39,
        B5). Escalated tools stay, marked: they ask the operator. The proxy's own record of
        the server's tools keeps them all, and a call to a left-out tool is still refused.
        The rule is the policy library's, which the policy middleware's path shares."""
        kept, denied = callable_tools(
            call.server,
            result,
            loaded,
            ceiling=self._ceiling,
            floor=self._floor,
            origin=call.caller.origin,
        )
        if denied:
            call.stream["denied_tools"] = denied
        return kept

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
        exchange = await self._upstream.request(
            target, body, request_id=upstream_id, limit=_answer_limit(loaded)
        )
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
        limit = _answer_limit(loaded)
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
                session.tools.page(cursor, names, next_cursor, _hints(result))
                self._metadata.listing(
                    session.caller.project,
                    session.server,
                    None,
                    loaded.digest,
                    result,
                    first=cursor is None,
                )
            except (MessageError, ValueError) as exc:
                raise UpstreamError(UPSTREAM_ERROR, str(exc)) from exc
            if next_cursor is None:
                return
            cursor = next_cursor
        raise UpstreamError(UPSTREAM_ERROR, f"tools/list has more than {MAX_LIST_PAGES} pages")

    async def _escalation(
        self,
        call: Call,
        loaded: LoadedPolicy,
        decision: Decision,
        method: str,
        params: bytes,
        summary: Mapping[str, str] | None = None,
    ) -> None:
        """Answer an escalated call from its permission request (D76); raise unless it proceeds.

        The key holds a digest of the parameters, never the parameters themselves; the
        request's record holds the call's significant arguments (``summary``, D97).
        """
        key = RequestKey.mcp(
            call.caller.project,
            call.caller.name,
            loaded.digest,
            call.server,
            method,
            call.tool or "",
            params_digest(params),
        )
        await self._refresh_approvals()
        resolution = self._approvals.resolve(key, self._now())
        # At most twice: inside its lock, an escalation records the request or returns
        # how it stands, never Unrecorded.
        while True:
            match resolution:
                case Denied(answer):
                    raise RefusalError(
                        APPROVAL_DENIED,
                        f"permission request {answer.request_seq} was denied",
                        data={"request": answer.request_seq},
                    )
                case Approved(answer):
                    payload = {
                        **call.finding(decision),
                        "approval_seq": answer.seq,
                        "request_seq": answer.request_seq,
                    }
                    try:
                        await self._events.record("approved", payload)
                    except Exception as exc:
                        _log.exception("could not record an approved call")
                        raise RefusalError(
                            AUDIT_UNAVAILABLE, "the approved call could not be recorded"
                        ) from exc
                    return
                case Unrecorded():
                    # Answered or recorded meanwhile: decided as any other call is.
                    resolution = await self._escalate(call, decision, key, summary or {})
                    continue
                case Waiting(pending):
                    raise RefusalError(
                        APPROVAL_REQUIRED,
                        f"{decision.reason} (permission request {pending.seq}, still waiting)",
                        data={"request": pending.seq},
                    )
                case TimedOut(pending) if pending.on_timeout == "alert":
                    try:
                        await self._events.record(
                            "alert", {**call.finding(decision), "timed_out_request": pending.seq}
                        )
                    except Exception as exc:
                        _log.exception("could not record an alert")
                        raise RefusalError(
                            AUDIT_UNAVAILABLE, "the alert could not be recorded"
                        ) from exc
                    return
                case TimedOut(pending):
                    raise RefusalError(
                        ESCALATION_TIMED_OUT,
                        f"permission request {pending.seq} was not answered in time",
                        data={"request": pending.seq},
                    )
                case _:
                    # A state this code does not know refuses: only the cases above proceed.
                    raise RefusalError(ESCALATION_TIMED_OUT, "the permission request is undecided")

    def prime_approvals(self) -> None:
        """Read the permission requests and answers already stored, before serving, so that
        no call folds the whole store. Blocking."""
        self._load_approvals()

    def _load_approvals(self) -> None:
        if self._requests is None:
            return
        try:
            self._approvals.load(self._requests(self._approvals.last_seq))
        except Exception:
            # Without the records no answer is known; the call waits, as before.
            _log.exception("could not read permission requests from the event store")

    async def _refresh_approvals(self) -> None:
        # The store is SQLite: read it on a thread, never on the event loop.
        if self._requests is not None:
            await asyncio.to_thread(self._load_approvals)

    async def _escalate(
        self, call: Call, decision: Decision, key: RequestKey, summary: Mapping[str, str]
    ) -> Resolution:
        """Record a permission request for ``key``, once, and refuse the call: an identical
        call handled at the same time waits on its fingerprint's lock, then finds the
        request recorded. A request answered or recorded meanwhile is returned for the
        caller to decide."""
        fingerprint = key.fingerprint()
        async with self._recording[int(fingerprint[:8], 16) % _RECORDING_LOCKS]:
            await self._refresh_approvals()
            resolution = self._approvals.resolve(key, self._now())
            if not isinstance(resolution, Unrecorded):
                return resolution
            try:
                seq = await self._events.record(
                    "escalation",
                    {**call.finding(decision), "summary": dict(summary), "target": key.payload()},
                )
            except Exception as exc:
                # No permission request exists, so the caller must not wait for an approval.
                _log.exception("could not record an escalation")
                raise RefusalError(
                    AUDIT_UNAVAILABLE, "the permission request could not be recorded"
                ) from exc
            # Known at once, so a retry before the next read waits on it too.
            await self._refresh_approvals()
        data = {"request": seq} if seq is not None else None
        raise RefusalError(APPROVAL_REQUIRED, decision.reason, data=data)

    def _target(self, server: McpServer, session: Session | None) -> Target:
        return Target(
            url=server.url,
            credentials=credential_headers(server.auth, self._credentials, self._environ),
            session_id=session.upstream_id if session is not None else None,
            version=session.version if session is not None else None,
        )

    # --- Answers and records -----------------------------------------------------------------

    def _refused(
        self,
        call: Call,
        request_id: RequestId | None,
        reason_code: str,
        status: int,
        code: int,
        *,
        data: Mapping[str, object] | None = None,
    ) -> web.Response:
        call.reason_code = reason_code
        if call.outcome != "failed":
            call.outcome = "refused"
        return _refusal(status, request_id, reason_code, code, data)

    def _relay_error(self, call: Call, request_id: RequestId, response: Response) -> web.Response:
        """Pass a server's JSON-RPC error to the caller, under the caller's id.

        The proxy's own codes say what the proxy did, such as refusing a call
        for approval, so a server may not use them.
        """
        error = response.error or {}
        if error.get("code") in (REFUSED, UPSTREAM_FAILED):
            raise UpstreamError(UPSTREAM_ERROR, "the server's error uses a code the proxy reserves")
        call.outcome = "server_error"
        return _server_answer({"jsonrpc": "2.0", "id": request_id, "error": error})

    async def _audit(self, call: Call) -> None:
        if call.method and call.outcome != "local":
            status: Literal["listed", "responded", "denied", "unavailable"] = (
                "listed"
                if call.method == "tools/list" and call.outcome == "forwarded"
                else "responded"
                if call.outcome in ("forwarded", "server_error")
                else "denied"
                if call.outcome == "refused"
                else "unavailable"
            )
            self._metadata.observe(call.caller.project, call.server, self._now(), status)
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


def _forwarded_params(method: str, params: JsonObject | None) -> JsonObject | None:
    """The parameters as forwarded: those MCP defines for the method, and nothing else.

    ``_meta`` is dropped: it carries progress tokens and other caller metadata
    the proxy does not relay (``mcp_wire.forwarded_params``).
    """
    if method not in PARAMS:
        raise RefusalError(REQUEST_UNCLASSIFIABLE, f"the proxy does not forward {method!r}")
    try:
        return forwarded_params(method, params)
    except McpWireError as exc:
        raise RefusalError(REQUEST_UNCLASSIFIABLE, str(exc)) from exc


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


def _text(value: object) -> str | None:
    return value if isinstance(value, str) else None


def caller_kind(kind: str) -> str:
    """Who a call comes from, as grants tell them apart (D97): ``agent`` for the run
    service's calls, ``code`` for research code's (a worker caller). Class grants cover
    only an agent's calls."""
    return "agent" if kind == "orchestrator" else "code"


def _call_summary(server: McpServer, tool: str | None, params: object) -> dict[str, str]:
    """A tool call's significant arguments, from its server's classification (D97)."""
    return tool_summary(server, tool, params)


def _class_fields(decision: Decision) -> dict[str, object]:
    """A tool call's effect class and grant ceiling, for records (D96, D97)."""
    if decision.effect is None:
        return {}
    return {"effect": decision.effect, "max_grant": decision.max_grant}


def _hints(result: JsonObject) -> dict[str, EffectClass]:
    """The classes a listing page's tools declare by their annotations (D96)."""
    return {tool.name: tool.hint for tool in listed_tools(result)}


def _server_answer(payload: JsonObject, headers: Mapping[str, str] | None = None) -> web.Response:
    """A JSON answer built from a server's response."""
    return _json_response(200, _server_body(payload), headers)


def _server_body(payload: JsonObject) -> bytes:
    """Serialize an answer built from a server's response, which may hold what JSON cannot carry."""
    try:
        return jsonrpc.dumps(payload)
    except MessageError as exc:
        raise UpstreamError(UPSTREAM_ERROR, f"the answer cannot be passed on: {exc}") from exc


def _json_response(
    status: int, body: bytes, headers: Mapping[str, str] | None = None
) -> web.Response:
    return web.Response(status=status, body=body, content_type="application/json", headers=headers)


def _answer_limit(loaded: LoadedPolicy) -> int:
    return min(loaded.policy.mcp.proxy.max_response_bytes, MAX_RESPONSE_BYTES)


def _refusal(
    status: int,
    request_id: RequestId | None,
    reason_code: str,
    code: int = REFUSED,
    data: Mapping[str, object] | None = None,
) -> web.Response:
    message = f"{reason_code}: {MESSAGES[reason_code]}"
    return _answer(
        status, jsonrpc.error(request_id, code, message, {**(data or {}), "reason": reason_code})
    )
