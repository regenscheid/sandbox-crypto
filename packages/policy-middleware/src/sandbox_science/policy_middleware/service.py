# SPDX-License-Identifier: MIT
"""The OpenShell ``SupervisorMiddleware`` gRPC service (openshell.middleware.v1).

The service declares two bindings, both before credential injection:

- ``HTTP_REQUEST``: every admitted request is decided by the evaluator;
- ``WEBSOCKET_MESSAGE``: every WebSocket upgrade is denied at preflight, so a
  WebSocket can never carry traffic the policy library has not seen.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator, Mapping
from concurrent import futures
from importlib.metadata import version
from typing import Final, NoReturn, cast

import grpc
from google.protobuf import json_format, struct_pb2

from ._proto import extension_pb2 as ext
from ._proto import supervisor_middleware_pb2 as pb
from .auth import Authenticator, Caller, require_sandbox
from .errors import AuthError
from .evaluator import HttpRequestFacts, PolicyMiddleware, Verdict

_log = logging.getLogger(__name__)

SERVICE_NAME: Final = "openshell.middleware.v1.SupervisorMiddleware"
IMPLEMENTATION_NAME: Final = "sandbox-science/policy-middleware"
PROTOCOL_MAJOR: Final = 1
PROTOCOL_MINOR: Final = 0
CONTRACT_CAPABILITY: Final = "openshell.supervisor-middleware.contract"
WEBSOCKET_DENIED: Final = "websocket_not_supported"

MAX_MESSAGE_BYTES: Final = 4 * 1024 * 1024 + 512 * 1024
"""A maximum-size payload plus its envelope (OpenShell asks for at least 4 MiB + 293 KiB)."""

SERVER_OPTIONS: Final = (
    ("grpc.max_receive_message_length", MAX_MESSAGE_BYTES),
    ("grpc.max_send_message_length", MAX_MESSAGE_BYTES),
    # OpenShell's client pings every 10 s, even between calls. With gRPC's
    # defaults the server would answer with GOAWAY (too_many_pings), and the
    # reconnects would eat into each evaluation's deadline and fail closed.
    ("grpc.keepalive_permit_without_calls", 1),
    ("grpc.http2.min_ping_interval_without_data_ms", 5000),
    ("grpc.http2.max_ping_strikes", 0),
)

_MAX_REASON_BYTES: Final = 4096
_MAX_FINDINGS: Final = 32


class MiddlewareService:
    """Implements the RPCs; transport and authentication details stay here."""

    def __init__(
        self,
        middleware: PolicyMiddleware,
        authenticator: Authenticator,
        *,
        max_payload_bytes: int,
        expected_audience: str = "",
    ) -> None:
        self._middleware = middleware
        self._authenticator = authenticator
        self._max_payload_bytes = max_payload_bytes
        self._expected_audience = expected_audience

    def describe(
        self, request: pb.MiddlewareDescribeRequest, context: grpc.ServicerContext
    ) -> pb.MiddlewareManifest:
        """Return the manifest after checking the gateway's protocol metadata."""
        self._caller(context)
        problem = _negotiation_problem(request)
        if problem is not None:
            _abort(context, grpc.StatusCode.FAILED_PRECONDITION, problem)
        bindings = [
            pb.MiddlewareBinding(
                operation=operation,
                phase=pb.SUPERVISOR_MIDDLEWARE_PHASE_PRE_CREDENTIALS,
                max_payload_bytes=self._max_payload_bytes,
            )
            for operation in (
                pb.SUPERVISOR_MIDDLEWARE_OPERATION_HTTP_REQUEST,
                pb.SUPERVISOR_MIDDLEWARE_OPERATION_WEBSOCKET_MESSAGE,
            )
        ]
        return pb.MiddlewareManifest(
            name=IMPLEMENTATION_NAME,
            service_version=_version(),
            bindings=bindings,
            expected_audience=self._expected_audience,
            extension=ext.PeerMetadata(
                protocol_version=ext.ProtocolVersion(major=PROTOCOL_MAJOR, minor=PROTOCOL_MINOR),
                implementation_name=IMPLEMENTATION_NAME,
                implementation_version=_version(),
                supported_capabilities=[CONTRACT_CAPABILITY],
                required_capabilities=[CONTRACT_CAPABILITY],
            ),
        )

    def validate_config(
        self, request: pb.ValidateConfigRequest, context: grpc.ServicerContext
    ) -> pb.ValidateConfigResponse:
        """Accept a sandbox policy's middleware config only if it names a stored policy."""
        self._caller(context)
        config = _struct(request.config) if request.HasField("config") else None
        problem = self._middleware.validate_config(config)
        if problem is not None:
            return pb.ValidateConfigResponse(valid=False, reason=_truncate(problem))
        return pb.ValidateConfigResponse(valid=True)

    def evaluate_http_request(
        self, request: pb.HttpRequestEvaluation, context: grpc.ServicerContext
    ) -> pb.HttpRequestResult:
        """Decide one buffered HTTP request."""
        caller = self._caller(context)
        sandbox_id = request.context.sandbox_id
        self._require_sandbox(context, caller, sandbox_id)
        if request.phase != pb.SUPERVISOR_MIDDLEWARE_PHASE_PRE_CREDENTIALS:
            _abort(context, grpc.StatusCode.INVALID_ARGUMENT, "unsupported phase")
        target = request.target
        facts = HttpRequestFacts(
            sandbox_id=sandbox_id,
            request_id=request.context.request_id,
            scheme=target.scheme,
            host=target.host,
            port=target.port,
            method=target.method,
            path=target.path,
            query=target.query,
            headers=tuple((header.name.lower(), header.value) for header in request.headers),
            body=request.body,
            config=_struct(request.config) if request.HasField("config") else None,
        )
        verdict = self._middleware.evaluate_http(facts)
        _log_verdict(facts, verdict)
        return _result(verdict)

    def evaluate_websocket_session(
        self, requests: Iterator[pb.WebSocketSessionEvent], context: grpc.ServicerContext
    ) -> Iterator[pb.WebSocketSessionEventResult]:
        """Deny every WebSocket upgrade at preflight, and any message that still arrives."""
        caller = self._caller(context)
        for event in requests:
            match event.WhichOneof("event"):
                case "preflight":
                    preflight = event.preflight
                    self._require_sandbox(context, caller, preflight.context.sandbox_id)
                    _log.info(
                        "deny websocket sandbox=%s request=%s host=%s",
                        preflight.context.sandbox_id,
                        preflight.context.request_id,
                        preflight.target.host,
                    )
                    yield pb.WebSocketSessionEventResult(
                        preflight_decision=pb.WebSocketPreflightDecision(
                            action=pb.WEB_SOCKET_PREFLIGHT_ACTION_DENY,
                            reason="WebSocket connections are not supported by the policy",
                            reason_code=WEBSOCKET_DENIED,
                        )
                    )
                case "message":
                    yield pb.WebSocketSessionEventResult(
                        message_result=pb.WebSocketMessageResult(
                            sequence=event.message.sequence,
                            decision=pb.DECISION_DENY,
                            reason_code=WEBSOCKET_DENIED,
                        )
                    )
                case _:
                    # session_start and session_end are notifications without a result.
                    pass

    def _caller(self, context: grpc.ServicerContext) -> Caller:
        authorization = [
            value
            for key, value in context.invocation_metadata()
            if key == "authorization" and isinstance(value, str)
        ]
        try:
            return self._authenticator.authenticate(authorization)
        except AuthError as exc:
            _log.warning("rejected call from %s: %s", context.peer(), exc)
            _abort(context, grpc.StatusCode.UNAUTHENTICATED, "authentication failed")

    def _require_sandbox(
        self, context: grpc.ServicerContext, caller: Caller, sandbox_id: str
    ) -> None:
        try:
            require_sandbox(caller, sandbox_id)
        except AuthError as exc:
            _log.warning("rejected call from %s: %s", context.peer(), exc)
            _abort(context, grpc.StatusCode.PERMISSION_DENIED, "caller may not act for sandbox")


def handler(service: MiddlewareService) -> grpc.GenericRpcHandler:
    """The gRPC method table for ``openshell.middleware.v1.SupervisorMiddleware``."""
    return grpc.method_handlers_generic_handler(
        SERVICE_NAME,
        {
            "Describe": grpc.unary_unary_rpc_method_handler(
                service.describe,
                request_deserializer=pb.MiddlewareDescribeRequest.FromString,
                response_serializer=pb.MiddlewareManifest.SerializeToString,
            ),
            "ValidateConfig": grpc.unary_unary_rpc_method_handler(
                service.validate_config,
                request_deserializer=pb.ValidateConfigRequest.FromString,
                response_serializer=pb.ValidateConfigResponse.SerializeToString,
            ),
            "EvaluateHttpRequest": grpc.unary_unary_rpc_method_handler(
                service.evaluate_http_request,
                request_deserializer=pb.HttpRequestEvaluation.FromString,
                response_serializer=pb.HttpRequestResult.SerializeToString,
            ),
            "EvaluateWebSocketSession": grpc.stream_stream_rpc_method_handler(
                service.evaluate_websocket_session,
                request_deserializer=pb.WebSocketSessionEvent.FromString,
                response_serializer=pb.WebSocketSessionEventResult.SerializeToString,
            ),
        },
    )


def build_server(
    service: MiddlewareService,
    *,
    listen: str,
    workers: int,
    credentials: grpc.ServerCredentials | None,
) -> tuple[grpc.Server, int]:
    """Create (but do not start) a server; return it with the port it bound."""
    server = grpc.server(
        futures.ThreadPoolExecutor(max_workers=workers),
        handlers=[handler(service)],
        options=list(SERVER_OPTIONS),
    )
    if credentials is None:
        port = server.add_insecure_port(listen)
    else:
        port = server.add_secure_port(listen, credentials)
    if port == 0:
        raise OSError(f"cannot listen on {listen}")
    return server, port


def _negotiation_problem(request: pb.MiddlewareDescribeRequest) -> str | None:
    """Check the gateway's protocol metadata as OpenShell's own extensions do."""
    if not request.HasField("gateway"):
        return "the gateway sent no protocol metadata"
    gateway = request.gateway
    if not gateway.HasField("protocol_version"):
        return "the gateway sent no protocol version"
    if gateway.protocol_version.major != PROTOCOL_MAJOR:
        return (
            f"gateway protocol {gateway.protocol_version.major}.{gateway.protocol_version.minor} "
            f"is incompatible with {PROTOCOL_MAJOR}.{PROTOCOL_MINOR}"
        )
    missing = set(gateway.required_capabilities) - {CONTRACT_CAPABILITY}
    if missing:
        return f"the gateway requires unsupported capabilities: {', '.join(sorted(missing))}"
    if CONTRACT_CAPABILITY not in gateway.supported_capabilities:
        return f"the gateway does not support {CONTRACT_CAPABILITY}"
    return None


def _result(verdict: Verdict) -> pb.HttpRequestResult:
    result = pb.HttpRequestResult(
        decision=pb.DECISION_ALLOW if verdict.allowed else pb.DECISION_DENY,
        reason=_truncate(verdict.reason),
        reason_code=verdict.reason_code,
        findings=[
            pb.Finding(
                type=finding.type,
                label=finding.label,
                count=finding.count,
                confidence="high",
                severity=finding.severity,
            )
            for finding in verdict.findings[:_MAX_FINDINGS]
        ],
        header_mutations=[
            pb.HeaderMutation(
                write=pb.WriteHeader(
                    name=name, value=value, on_existing=pb.EXISTING_HEADER_ACTION_OVERWRITE
                )
            )
            for name, value in (verdict.headers if verdict.allowed else ())
        ],
    )
    decision = verdict.decision
    if decision is not None:
        result.metadata["layer"] = decision.layer
        result.metadata["action"] = decision.action.value
        if decision.rule is not None:
            result.metadata["rule"] = decision.rule
        if decision.policy_digest is not None:
            result.metadata["policy"] = decision.policy_digest
    return result


def _struct(message: struct_pb2.Struct) -> Mapping[str, object]:
    return cast("Mapping[str, object]", json_format.MessageToDict(message))


def _truncate(text: str) -> str:
    """Keep a diagnostic within OpenShell's limit without splitting a character."""
    data = text.encode()
    if len(data) <= _MAX_REASON_BYTES:
        return text
    return data[:_MAX_REASON_BYTES].decode(errors="ignore")


def _log_verdict(facts: HttpRequestFacts, verdict: Verdict) -> None:
    level = logging.DEBUG if verdict.allowed and not verdict.findings else logging.INFO
    _log.log(
        level,
        "%s sandbox=%s request=%s %s %s://%s:%d%s code=%s reason=%s",
        "allow" if verdict.allowed else "deny",
        facts.sandbox_id,
        facts.request_id,
        facts.method,
        facts.scheme,
        facts.host,
        facts.port,
        facts.path,
        verdict.reason_code or "-",
        verdict.reason,
    )


def _abort(context: grpc.ServicerContext, code: grpc.StatusCode, details: str) -> NoReturn:
    context.abort(code, details)
    raise AssertionError("ServicerContext.abort returned")  # pragma: no cover


def _version() -> str:
    return version("sandbox-science-policy-middleware")
