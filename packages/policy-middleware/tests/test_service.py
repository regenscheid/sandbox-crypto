# SPDX-License-Identifier: MIT
"""The gRPC service, exercised over a real channel as OpenShell would call it."""

import time
from collections.abc import Iterator
from typing import Any

import grpc
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ed25519
from google.protobuf import struct_pb2
from google.protobuf.message import Message

from sandbox_science.policy import LoadedPolicy
from sandbox_science.policy_middleware._proto import extension_pb2 as ext
from sandbox_science.policy_middleware._proto import supervisor_middleware_pb2 as pb
from sandbox_science.policy_middleware.auth import TokenAuthenticator, default_audience
from sandbox_science.policy_middleware.evaluator import PolicyMiddleware
from sandbox_science.policy_middleware.service import (
    CONTRACT_CAPABILITY,
    MAX_MESSAGE_BYTES,
    SERVER_OPTIONS,
    SERVICE_NAME,
    MiddlewareService,
    build_server,
)

AUDIENCE = default_audience("sandbox-science-policy")
KEY = ed25519.Ed25519PrivateKey.generate()
PAYLOAD_LIMIT = 4 * 1024 * 1024


def token(caller_kind: str = "supervisor", sandbox_id: str | None = "sbx-0001") -> str:
    claims: dict[str, object] = {
        "iss": "openshell-gateway:test",
        "aud": AUDIENCE,
        "exp": int(time.time()) + 300,
        "caller_kind": caller_kind,
    }
    if sandbox_id is not None:
        claims["sandbox_id"] = sandbox_id
    return jwt.encode(claims, KEY, algorithm="EdDSA", headers={"typ": "openshell-ext+jwt"})


def auth(value: str | None = None) -> tuple[tuple[str, str], ...]:
    return (("authorization", f"Bearer {value or token()}"),)


GATEWAY = auth(token("gateway", None))


@pytest.fixture
def channel(middleware: PolicyMiddleware) -> Iterator[grpc.Channel]:
    service = MiddlewareService(
        middleware,
        TokenAuthenticator(public_key=KEY.public_key(), gateway_id="test", audience=AUDIENCE),
        max_payload_bytes=PAYLOAD_LIMIT,
        expected_audience=AUDIENCE,
    )
    server, port = build_server(service, listen="127.0.0.1:0", workers=4, credentials=None)
    server.start()
    options = [("grpc.max_send_message_length", 8 * 1024 * 1024)]
    with grpc.insecure_channel(f"127.0.0.1:{port}", options=options) as channel:
        yield channel
    server.stop(grace=None)


def call(
    channel: grpc.Channel,
    method: str,
    request: Message,
    response_type: Any,
    metadata: tuple[tuple[str, str], ...],
) -> Any:
    stub = channel.unary_unary(
        f"/{SERVICE_NAME}/{method}",
        request_serializer=type(request).SerializeToString,
        response_deserializer=response_type.FromString,
    )
    return stub(request, metadata=metadata, timeout=10)


def gateway_metadata(**overrides: object) -> ext.PeerMetadata:
    fields: dict[str, object] = {
        "protocol_version": ext.ProtocolVersion(major=1, minor=0),
        "implementation_name": "openshell/gateway",
        "implementation_version": "0.1.3",
        "supported_capabilities": [CONTRACT_CAPABILITY],
        "required_capabilities": [CONTRACT_CAPABILITY],
    }
    fields.update(overrides)
    return ext.PeerMetadata(**fields)  # pyright: ignore[reportArgumentType]


def config(policy: LoadedPolicy) -> struct_pb2.Struct:
    struct = struct_pb2.Struct()
    struct.update({"policy": {"name": policy.policy.metadata.name, "digest": policy.digest}})
    return struct


def evaluation(policy: LoadedPolicy, **target: Any) -> pb.HttpRequestEvaluation:
    fields: dict[str, Any] = {
        "scheme": "https",
        "host": "example.org",
        "port": 443,
        "method": "GET",
        "path": "/",
    }
    body = target.pop("body", b"")
    headers = target.pop("headers", ())
    sandbox = target.pop("sandbox_id", "sbx-0001")
    phase = target.pop("phase", pb.SUPERVISOR_MIDDLEWARE_PHASE_PRE_CREDENTIALS)
    fields.update(target)
    return pb.HttpRequestEvaluation(
        phase=phase,
        context=pb.RequestContext(request_id="req-1", sandbox_id=sandbox),
        config=config(policy),
        target=pb.HttpRequestTarget(**fields),
        headers=[pb.HttpHeader(name=name, value=value) for name, value in headers],
        body=body,
        middleware_name="sandbox-science-policy",
    )


def status_of(error: pytest.ExceptionInfo[grpc.RpcError]) -> grpc.StatusCode:
    return error.value.code()  # pyright: ignore[reportAttributeAccessIssue]


# --- Describe --------------------------------------------------------------------------------


def test_the_server_accepts_openshells_transport() -> None:
    options = dict(SERVER_OPTIONS)
    # OpenShell sends up to 4,493,376-byte messages and pings every 10 s while idle.
    assert options["grpc.max_receive_message_length"] == MAX_MESSAGE_BYTES >= 4_493_376
    assert options["grpc.keepalive_permit_without_calls"] == 1
    assert options["grpc.http2.min_ping_interval_without_data_ms"] <= 10_000
    assert options["grpc.http2.max_ping_strikes"] == 0


def test_describe_returns_the_manifest(channel: grpc.Channel) -> None:
    request = pb.MiddlewareDescribeRequest(gateway=gateway_metadata())
    manifest = call(channel, "Describe", request, pb.MiddlewareManifest, GATEWAY)
    assert manifest.name == "sandbox-science/policy-middleware"
    assert manifest.expected_audience == AUDIENCE
    assert [(b.operation, b.phase, b.max_payload_bytes) for b in manifest.bindings] == [
        (
            pb.SUPERVISOR_MIDDLEWARE_OPERATION_HTTP_REQUEST,
            pb.SUPERVISOR_MIDDLEWARE_PHASE_PRE_CREDENTIALS,
            PAYLOAD_LIMIT,
        ),
        (
            pb.SUPERVISOR_MIDDLEWARE_OPERATION_WEBSOCKET_MESSAGE,
            pb.SUPERVISOR_MIDDLEWARE_PHASE_PRE_CREDENTIALS,
            PAYLOAD_LIMIT,
        ),
    ]
    extension = manifest.extension
    assert (extension.protocol_version.major, extension.protocol_version.minor) == (1, 0)
    assert list(extension.supported_capabilities) == [CONTRACT_CAPABILITY]
    assert list(extension.required_capabilities) == [CONTRACT_CAPABILITY]
    assert extension.implementation_version


@pytest.mark.parametrize(
    "request_message",
    [
        pytest.param(pb.MiddlewareDescribeRequest(), id="no-metadata"),
        pytest.param(
            pb.MiddlewareDescribeRequest(gateway=gateway_metadata(protocol_version=None)),
            id="no-version",
        ),
        pytest.param(
            pb.MiddlewareDescribeRequest(
                gateway=gateway_metadata(protocol_version=ext.ProtocolVersion(major=2))
            ),
            id="other-major",
        ),
        pytest.param(
            pb.MiddlewareDescribeRequest(
                gateway=gateway_metadata(
                    required_capabilities=[CONTRACT_CAPABILITY, "openshell.future.thing"]
                )
            ),
            id="unmet-requirement",
        ),
        pytest.param(
            pb.MiddlewareDescribeRequest(gateway=gateway_metadata(supported_capabilities=[])),
            id="no-contract",
        ),
    ],
)
def test_describe_rejects_incompatible_gateways(
    channel: grpc.Channel, request_message: pb.MiddlewareDescribeRequest
) -> None:
    with pytest.raises(grpc.RpcError) as error:
        call(channel, "Describe", request_message, pb.MiddlewareManifest, GATEWAY)
    assert status_of(error) == grpc.StatusCode.FAILED_PRECONDITION


def test_a_newer_minor_version_is_compatible(channel: grpc.Channel) -> None:
    request = pb.MiddlewareDescribeRequest(
        gateway=gateway_metadata(protocol_version=ext.ProtocolVersion(major=1, minor=4))
    )
    assert call(channel, "Describe", request, pb.MiddlewareManifest, GATEWAY).bindings


@pytest.mark.parametrize(
    "metadata",
    [
        pytest.param((), id="no-token"),
        pytest.param((("authorization", "Bearer forged"),), id="bad-token"),
    ],
)
def test_calls_without_a_valid_token_are_unauthenticated(
    channel: grpc.Channel, example: LoadedPolicy, metadata: tuple[tuple[str, str], ...]
) -> None:
    for method, request, response in (
        (
            "Describe",
            pb.MiddlewareDescribeRequest(gateway=gateway_metadata()),
            pb.MiddlewareManifest,
        ),
        (
            "ValidateConfig",
            pb.ValidateConfigRequest(config=config(example)),
            pb.ValidateConfigResponse,
        ),
        ("EvaluateHttpRequest", evaluation(example), pb.HttpRequestResult),
    ):
        with pytest.raises(grpc.RpcError) as error:
            call(channel, method, request, response, metadata)
        assert status_of(error) == grpc.StatusCode.UNAUTHENTICATED


# --- ValidateConfig --------------------------------------------------------------------------


def test_validate_config(channel: grpc.Channel, example: LoadedPolicy) -> None:
    valid = call(
        channel,
        "ValidateConfig",
        pb.ValidateConfigRequest(config=config(example), middleware_name="sandbox-science-policy"),
        pb.ValidateConfigResponse,
        GATEWAY,
    )
    assert valid.valid
    assert valid.reason == ""
    unknown = struct_pb2.Struct()
    unknown.update({"policy": {"name": "x", "digest": "sha256:" + "0" * 64}})
    invalid = call(
        channel,
        "ValidateConfig",
        pb.ValidateConfigRequest(config=unknown),
        pb.ValidateConfigResponse,
        GATEWAY,
    )
    assert not invalid.valid
    assert "no policy" in invalid.reason
    missing = call(
        channel, "ValidateConfig", pb.ValidateConfigRequest(), pb.ValidateConfigResponse, GATEWAY
    )
    assert not missing.valid


# --- EvaluateHttpRequest ---------------------------------------------------------------------


def test_an_allowed_request(channel: grpc.Channel, example: LoadedPolicy) -> None:
    result = call(channel, "EvaluateHttpRequest", evaluation(example), pb.HttpRequestResult, auth())
    assert result.decision == pb.DECISION_ALLOW
    assert result.reason_code == ""
    assert not result.has_body
    assert not result.header_mutations
    assert result.metadata["rule"] == "read-only-web"
    assert result.metadata["policy"] == example.digest


def test_an_escalated_request(channel: grpc.Channel, example: LoadedPolicy) -> None:
    result = call(
        channel,
        "EvaluateHttpRequest",
        evaluation(example, method="POST", body=b"{}"),
        pb.HttpRequestResult,
        auth(),
    )
    assert result.decision == pb.DECISION_DENY
    assert result.reason_code == "approval_required"
    assert result.metadata["action"] == "escalate"


def test_a_maximum_size_body_fits_the_transport(
    channel: grpc.Channel, example: LoadedPolicy
) -> None:
    result = call(
        channel,
        "EvaluateHttpRequest",
        evaluation(example, method="POST", body=b"x" * PAYLOAD_LIMIT),
        pb.HttpRequestResult,
        auth(),
    )
    assert result.reason_code == "approval_required"


def test_header_names_are_compared_case_insensitively(
    channel: grpc.Channel, example: LoadedPolicy
) -> None:
    result = call(
        channel,
        "EvaluateHttpRequest",
        evaluation(example, headers=(("Upgrade", "websocket"),)),
        pb.HttpRequestResult,
        auth(),
    )
    assert result.reason_code == "request_unclassifiable"


@pytest.mark.parametrize(
    "metadata",
    [
        pytest.param(auth(token(sandbox_id="sbx-0002")), id="other-sandbox"),
        pytest.param(GATEWAY, id="gateway"),
    ],
)
def test_only_the_requests_own_supervisor_may_evaluate(
    channel: grpc.Channel, example: LoadedPolicy, metadata: tuple[tuple[str, str], ...]
) -> None:
    with pytest.raises(grpc.RpcError) as error:
        call(channel, "EvaluateHttpRequest", evaluation(example), pb.HttpRequestResult, metadata)
    assert status_of(error) == grpc.StatusCode.PERMISSION_DENIED


def test_other_phases_are_rejected(channel: grpc.Channel, example: LoadedPolicy) -> None:
    request = evaluation(example, phase=pb.SUPERVISOR_MIDDLEWARE_PHASE_PRE_RETURN)
    with pytest.raises(grpc.RpcError) as error:
        call(channel, "EvaluateHttpRequest", request, pb.HttpRequestResult, auth())
    assert status_of(error) == grpc.StatusCode.INVALID_ARGUMENT


# --- EvaluateWebSocketSession ----------------------------------------------------------------


def websocket(
    channel: grpc.Channel,
    events: list[pb.WebSocketSessionEvent],
    metadata: tuple[tuple[str, str], ...],
) -> list[pb.WebSocketSessionEventResult]:
    stub = channel.stream_stream(
        f"/{SERVICE_NAME}/EvaluateWebSocketSession",
        request_serializer=pb.WebSocketSessionEvent.SerializeToString,
        response_deserializer=pb.WebSocketSessionEventResult.FromString,
    )
    return list(stub(iter(events), metadata=metadata, timeout=10))


def preflight(sandbox_id: str = "sbx-0001") -> pb.WebSocketSessionEvent:
    return pb.WebSocketSessionEvent(
        preflight=pb.WebSocketPreflight(
            session_id="ws-1",
            phase=pb.SUPERVISOR_MIDDLEWARE_PHASE_PRE_CREDENTIALS,
            context=pb.RequestContext(request_id="req-ws", sandbox_id=sandbox_id),
            target=pb.HttpRequestTarget(
                scheme="wss", host="example.org", port=443, method="GET", path="/socket"
            ),
        )
    )


def test_websocket_upgrades_are_denied_at_preflight(channel: grpc.Channel) -> None:
    results = websocket(channel, [preflight()], auth())
    assert len(results) == 1
    decision = results[0].preflight_decision
    assert decision.action == pb.WEB_SOCKET_PREFLIGHT_ACTION_DENY
    assert decision.reason_code == "websocket_not_supported"


def test_websocket_messages_are_denied_and_notifications_unanswered(
    channel: grpc.Channel,
) -> None:
    events = [
        preflight(),
        pb.WebSocketSessionEvent(session_start=pb.WebSocketSessionStart()),
        pb.WebSocketSessionEvent(message=pb.WebSocketMessage(sequence=3, text="hello")),
        pb.WebSocketSessionEvent(
            session_end=pb.MiddlewareSessionEnd(reason=pb.MIDDLEWARE_SESSION_END_REASON_NORMAL)
        ),
    ]
    results = websocket(channel, events, auth())
    assert [result.WhichOneof("result") for result in results] == [
        "preflight_decision",
        "message_result",
    ]
    assert results[1].message_result.sequence == 3
    assert results[1].message_result.decision == pb.DECISION_DENY


def test_websocket_sessions_check_the_sandbox(channel: grpc.Channel) -> None:
    with pytest.raises(grpc.RpcError) as error:
        websocket(channel, [preflight("sbx-0002")], auth())
    assert status_of(error) == grpc.StatusCode.PERMISSION_DENIED
    with pytest.raises(grpc.RpcError) as error:
        websocket(channel, [preflight()], ())
    assert status_of(error) == grpc.StatusCode.UNAUTHENTICATED
