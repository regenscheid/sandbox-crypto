# SPDX-License-Identifier: MIT
"""MCP calls straight to a declared server through OpenShell (the D98 prototype).

OpenShell's side is faked as the proto defines it: request facts as ``EvaluateHttpRequest``
carries them, and ``HttpResponsePreReturn`` streams over a real gRPC channel.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterator, Mapping
from functools import partial
from pathlib import Path
from typing import Any, cast

import grpc
import pytest

from sandbox_science.event_store import EventStore, StoreReader
from sandbox_science.policy import HardFloor, LoadedPolicy, PoolCeiling
from sandbox_science.policy.approvals import RequestKey
from sandbox_science.policy.mcp_wire import params_digest
from sandbox_science.policy.openshell import AgentMcpTarget, compile_agent_mcp
from sandbox_science.policy_middleware._proto import supervisor_middleware_pb2 as pb
from sandbox_science.policy_middleware.auth import UnverifiedCallers
from sandbox_science.policy_middleware.cli import _read_records, main
from sandbox_science.policy_middleware.errors import SettingsError
from sandbox_science.policy_middleware.evaluator import (
    HttpRequestFacts,
    PolicyConfigError,
    PolicyMiddleware,
    Verdict,
    parse_config,
)
from sandbox_science.policy_middleware.events import StoreSink
from sandbox_science.policy_middleware.mcp import McpRef, ToolListings, load_tool_pins
from sandbox_science.policy_middleware.service import (
    RESPONSE_SERVICE_NAME,
    SERVICE_NAME,
    MiddlewareService,
    build_server,
)
from sandbox_science.policy_middleware.store import PolicyStore

SANDBOX = "sbx-0001"
HOST = "mcp.example.invalid"
PATH = "/crypto-research"
SERVER = "crypto-research-mcp"
READ_TOOL = "attack-catalog-mcp_search_attacks"
ESCALATED_TOOL = "firecrawl-mcp_firecrawl_monitor_create"
DENIED_TOOL = "portal_toggle_servers"
ATTRIBUTION = (("sandbox-science-session", "s-42"), ("sandbox-science-run", "main-1"))


@pytest.fixture
def events(tmp_path: Path) -> Iterator[EventStore]:
    with EventStore(tmp_path / "state" / "events.db", timeout=0.5) as store:
        yield store


@pytest.fixture
def pins() -> ToolListings:
    return ToolListings({SERVER: ["a", "b"]})


@pytest.fixture
def middleware(
    store: PolicyStore, example: LoadedPolicy, events: EventStore, pins: ToolListings
) -> PolicyMiddleware:
    del example
    return PolicyMiddleware(
        store,
        ceiling=PoolCeiling.unrestricted(),
        floor=HardFloor(),
        events=StoreSink(events),
        requests=partial(_read_records, events.path),
        listings=pins,
    )


@pytest.fixture
def settings(tmp_path: Path) -> Path:
    path = tmp_path / "middleware.toml"
    path.write_text('state_dir = "state"\nceiling = "c.yaml"\nallow_unauthenticated = true\n')
    return path


def mcp_config(policy: LoadedPolicy) -> dict[str, object]:
    """The middleware config ``compile_agent_mcp`` writes."""
    return {
        "policy": {"name": policy.policy.metadata.name, "digest": policy.digest},
        "mcp": {"project": "crypto", "caller": "agent-runtime"},
    }


def message(method: str, params: Mapping[str, object] | None = None, **fields: object) -> bytes:
    body: dict[str, object] = {"jsonrpc": "2.0", "id": 7, "method": method, **fields}
    if params is not None:
        body["params"] = params
    return json.dumps(body, sort_keys=True).encode()


def tool_call(tool: str, arguments: dict[str, object] | None = None) -> bytes:
    return message("tools/call", {"name": tool, "arguments": arguments or {"query": "SNOVA"}})


@pytest.fixture
def facts(example: LoadedPolicy) -> Callable[..., HttpRequestFacts]:
    def make(body: bytes = b"", **overrides: Any) -> HttpRequestFacts:
        fields: dict[str, Any] = {
            "sandbox_id": SANDBOX,
            "request_id": "req-1",
            "scheme": "https",
            "host": HOST,
            "port": 443,
            "method": "POST",
            "path": PATH,
            "query": "",
            "headers": (("content-type", "application/json"), *ATTRIBUTION),
            "body": body,
            "config": mcp_config(example),
        }
        fields.update(overrides)
        return HttpRequestFacts(**fields)

    return make


def records(events: EventStore, kind: str) -> list[dict[str, Any]]:
    with StoreReader(events.path) as reader:
        return [record.data() for record in reader.records() if record.kind == kind]


def request_of(verdict: Verdict) -> int:
    match = re.fullmatch(r"[a-z_]+_r([0-9]+)", verdict.reason_code)
    assert match is not None, verdict.reason_code
    return int(match.group(1))


def test_the_config_names_project_and_caller(example: LoadedPolicy) -> None:
    ref = parse_config(mcp_config(example))
    assert ref.mcp == McpRef(project="crypto", caller="agent-runtime")
    with_proxy = {**mcp_config(example), "mcp_proxy": {"host": "h", "port": 1}}
    for config in (
        with_proxy,
        {**mcp_config(example), "mcp": {"project": "crypto"}},
        {**mcp_config(example), "mcp": {"project": "Crypto!", "caller": "x"}},
    ):
        with pytest.raises(PolicyConfigError, match="config must be"):
            parse_config(config)


def test_an_allowed_read_tool_is_admitted_and_recorded(
    middleware: PolicyMiddleware, facts: Callable[..., HttpRequestFacts], events: EventStore
) -> None:
    verdict = middleware.evaluate_http(facts(tool_call(READ_TOOL)))
    assert verdict.allowed, verdict.reason
    assert verdict.decision is not None
    assert (verdict.decision.rule, verdict.decision.effect) == ("mcp.effects.read", "read")
    (call,) = records(events, "mcp-call")
    assert call["project"] == "crypto"
    assert (call["session"], call["run"], call["sandbox_id"]) == ("s-42", "main-1", SANDBOX)
    assert (call["server"], call["method"], call["tool"]) == (SERVER, "tools/call", READ_TOOL)
    assert call["outcome"] == "admitted"
    assert "SNOVA" not in json.dumps(call)  # never the arguments


def test_protocol_methods_are_admitted(
    middleware: PolicyMiddleware, facts: Callable[..., HttpRequestFacts]
) -> None:
    initialize = {"protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "h"}}
    assert middleware.evaluate_http(facts(message("initialize", initialize))).allowed
    assert middleware.evaluate_http(facts(message("tools/list", {}))).allowed
    initialized = json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"})
    assert middleware.evaluate_http(facts(initialized.encode())).allowed
    # The agent's client declares no capabilities, so the server makes no requests of it.
    sampling = {**initialize, "capabilities": {"sampling": {}}}
    verdict = middleware.evaluate_http(facts(message("initialize", sampling)))
    assert verdict.reason_code == "request_unclassifiable"


def test_a_denied_tool_is_refused(
    middleware: PolicyMiddleware, facts: Callable[..., HttpRequestFacts], events: EventStore
) -> None:
    verdict = middleware.evaluate_http(facts(tool_call(DENIED_TOOL)))
    assert (verdict.allowed, verdict.reason_code) == (False, "policy_denied")
    (call,) = records(events, "mcp-call")
    assert (call["outcome"], call["reason_code"], call["tool"]) == (
        "refused",
        "policy_denied",
        DENIED_TOOL,
    )
    assert records(events, "escalation") == []


def test_an_escalated_tool_waits_for_approval_then_proceeds(
    middleware: PolicyMiddleware,
    facts: Callable[..., HttpRequestFacts],
    events: EventStore,
    example: LoadedPolicy,
    settings: Path,
) -> None:
    body = tool_call(ESCALATED_TOOL, {"url": "https://example.org", "interval": "1d"})
    first = middleware.evaluate_http(facts(body))
    assert not first.allowed
    assert first.reason_code.startswith("approval_required_r")
    seq = request_of(first)
    (escalation,) = records(events, "escalation")
    assert escalation["target"] == {
        "kind": "mcp",
        **RequestKey.mcp(
            "crypto",
            "agent-runtime",
            example.digest,
            SERVER,
            "tools/call",
            ESCALATED_TOOL,
            params_digest(
                json.dumps(
                    {
                        "arguments": {"interval": "1d", "url": "https://example.org"},
                        "name": ESCALATED_TOOL,
                    },
                    separators=(",", ":"),
                ).encode()
            ),
        ).fields(),
    }
    assert (escalation["session"], escalation["run"], escalation["project"]) == (
        "s-42",
        "main-1",
        "crypto",
    )
    # A retry waits on the same request, without a new one; it is still refused.
    again = middleware.evaluate_http(facts(body, request_id="req-2"))
    assert request_of(again) == seq
    assert again.reason_code.startswith("approval_required")
    assert len(records(events, "escalation")) == 1
    # The same tool with other arguments is another request.
    other = request_of(
        middleware.evaluate_http(facts(tool_call(ESCALATED_TOOL, {"url": "https://x.org"})))
    )
    assert other != seq

    assert main(["approve", "--settings", str(settings), "--request", str(seq)]) == 0
    approved = middleware.evaluate_http(facts(body, request_id="req-3"))
    assert approved.allowed, approved.reason
    (record,) = records(events, "approved")
    assert (record["request_seq"], record["session"], record["run"]) == (seq, "s-42", "main-1")
    calls = records(events, "mcp-call")
    assert [call.get("permission_request") for call in calls] == [seq, seq, other, None]


def test_a_denied_request_names_itself(
    middleware: PolicyMiddleware,
    facts: Callable[..., HttpRequestFacts],
    settings: Path,
) -> None:
    body = tool_call(ESCALATED_TOOL)
    seq = request_of(middleware.evaluate_http(facts(body)))
    assert main(["deny", "--settings", str(settings), "--request", str(seq)]) == 0
    verdict = middleware.evaluate_http(facts(body))
    assert verdict.reason_code == f"approval_denied_r{seq}"


def test_the_session_and_run_are_attribution_not_part_of_the_request(
    middleware: PolicyMiddleware, facts: Callable[..., HttpRequestFacts], settings: Path
) -> None:
    body = tool_call(ESCALATED_TOOL)
    seq = request_of(middleware.evaluate_http(facts(body)))
    assert main(["approve", "--settings", str(settings), "--request", str(seq)]) == 0
    other_session = (("sandbox-science-session", "s-43"),)
    assert middleware.evaluate_http(facts(body, headers=other_session)).allowed


@pytest.mark.parametrize(
    "headers",
    [
        pytest.param((), id="no-session"),
        pytest.param((("sandbox-science-session", "s 42"),), id="malformed-session"),
        pytest.param((*ATTRIBUTION, ("sandbox-science-session", "s-9")), id="two-sessions"),
        pytest.param(
            (("sandbox-science-session", "s-1"), ("sandbox-science-run", "../x")), id="bad-run"
        ),
    ],
)
def test_a_call_must_say_whose_it_is(
    middleware: PolicyMiddleware,
    facts: Callable[..., HttpRequestFacts],
    headers: tuple[tuple[str, str], ...],
) -> None:
    verdict = middleware.evaluate_http(facts(tool_call(READ_TOOL), headers=headers))
    assert verdict.reason_code == "attribution_missing"


def test_arguments_over_the_bound_are_refused(
    middleware: PolicyMiddleware, facts: Callable[..., HttpRequestFacts], events: EventStore
) -> None:
    body = tool_call(READ_TOOL, {"query": "x" * (64 * 1024)})
    verdict = middleware.evaluate_http(facts(body))
    assert verdict.reason_code == "arguments_too_large"
    assert records(events, "mcp-call")[0]["reason_code"] == "arguments_too_large"


@pytest.mark.parametrize(
    ("body", "code"),
    [
        pytest.param(
            b'{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"portal_toggle_servers",'
            b'"name":"' + READ_TOOL.encode() + b'"}}',
            "request_unclassifiable",
            id="duplicate-key",
        ),
        pytest.param(
            message("tools/call", {"name": READ_TOOL, "NAME": DENIED_TOOL}),
            "request_unclassifiable",
            id="undefined-param",
        ),
        pytest.param(b"[" + tool_call(READ_TOOL) + b"]", "request_unclassifiable", id="batch"),
        pytest.param(
            b'{"jsonrpc":"2.0","id":1,"result":{}}', "request_unclassifiable", id="response"
        ),
        pytest.param(
            message("tools/call", {"name": READ_TOOL, "task": {}}),
            "request_unclassifiable",
            id="task",
        ),
        pytest.param(
            tool_call(READ_TOOL, {"q": "openshell:resolve:env:CF_ACCESS_CLIENT_SECRET"}),
            "credential_in_body",
            id="placeholder",
        ),
        pytest.param(message("tasks/list", {}), "request_unclassifiable", id="task-method"),
    ],
)
def test_messages_the_server_could_read_otherwise_are_refused(
    middleware: PolicyMiddleware, facts: Callable[..., HttpRequestFacts], body: bytes, code: str
) -> None:
    assert middleware.evaluate_http(facts(body)).reason_code == code


@pytest.mark.parametrize(
    ("overrides", "code"),
    [
        pytest.param({"host": "other.example.org"}, "destination_forbidden", id="undeclared"),
        pytest.param({"path": "/sse"}, "destination_forbidden", id="other-path"),
        pytest.param({"port": 8443}, "destination_forbidden", id="other-port"),
        pytest.param({"scheme": "http"}, "destination_forbidden", id="plain-http"),
        pytest.param({"method": "GET", "body": b""}, "request_unclassifiable", id="get"),
        pytest.param({"query": "a=1"}, "request_unclassifiable", id="query"),
    ],
)
def test_only_declared_endpoints_are_reached(
    middleware: PolicyMiddleware,
    facts: Callable[..., HttpRequestFacts],
    overrides: dict[str, Any],
    code: str,
) -> None:
    body = overrides.pop("body", tool_call(READ_TOOL))
    assert middleware.evaluate_http(facts(body, **overrides)).reason_code == code


def test_an_unbound_sandbox_is_refused(
    middleware: PolicyMiddleware, facts: Callable[..., HttpRequestFacts]
) -> None:
    verdict = middleware.evaluate_http(facts(tool_call(READ_TOOL), sandbox_id="sbx-other"))
    assert verdict.reason_code == "sandbox_unbound"


def test_pins_load_from_a_file(tmp_path: Path) -> None:
    path = tmp_path / "pins.json"
    path.write_text(json.dumps({SERVER: ["a"]}))
    assert load_tool_pins(path).pinned(SERVER)
    path.write_text(json.dumps({SERVER: "a"}))
    with pytest.raises(SettingsError):
        load_tool_pins(path)


# --- Responses, over gRPC ---------------------------------------------------------------


@pytest.fixture
def channel(middleware: PolicyMiddleware) -> Iterator[grpc.Channel]:
    service = MiddlewareService(middleware, UnverifiedCallers(), max_payload_bytes=4 * 1024 * 1024)
    server, port = build_server(service, listen="127.0.0.1:0", workers=4, credentials=None)
    server.start()
    with grpc.insecure_channel(f"127.0.0.1:{port}") as channel:
        yield channel
    server.stop(grace=None)


def preflight(request_id: str, content_type: str, status: int = 200) -> pb.HttpResponseEvent:
    return pb.HttpResponseEvent(
        preflight=pb.HttpResponsePreflight(
            context=pb.RequestContext(request_id=request_id, sandbox_id=SANDBOX),
            target=pb.HttpRequestTarget(
                scheme="https", host=HOST, port=443, method="POST", path=PATH
            ),
            status_code=status,
            headers=[pb.HttpHeader(name="content-type", value=content_type)],
            max_payload_bytes=4 * 1024 * 1024,
            permitted_body_modes=[
                pb.HTTP_RESPONSE_BODY_MODE_HEADERS_ONLY,
                pb.HTTP_RESPONSE_BODY_MODE_WHOLE_BODY_BYTES,
                pb.HTTP_RESPONSE_BODY_MODE_STREAM_BYTES,
            ],
        )
    )


def body_units(*pieces: bytes) -> list[pb.HttpResponseEvent]:
    return [
        pb.HttpResponseEvent(
            body=pb.HttpResponseBodyUnit(
                sequence=index, data=piece, end_of_stream=index == len(pieces)
            )
        )
        for index, piece in enumerate(pieces, start=1)
    ]


def respond(channel: grpc.Channel, events: list[pb.HttpResponseEvent]) -> list[Any]:
    stub = channel.stream_stream(
        f"/{RESPONSE_SERVICE_NAME}/Evaluate",
        request_serializer=pb.HttpResponseEvent.SerializeToString,
        response_deserializer=pb.HttpResponseEventResult.FromString,
    )
    return list(stub(iter(events), timeout=10))


def listing(names: list[str], cursor: str | None = None) -> dict[str, object]:
    result: dict[str, object] = {"tools": [{"name": name} for name in names]}
    if cursor is not None:
        result["nextCursor"] = cursor
    return {"jsonrpc": "2.0", "id": 7, "result": result}


def sse(*messages: dict[str, object]) -> bytes:
    return b"".join(b"event: message\ndata: " + json.dumps(m).encode() + b"\n\n" for m in messages)


def test_describe_declares_the_response_binding(channel: grpc.Channel) -> None:
    from sandbox_science.policy_middleware._proto import extension_pb2 as ext

    stub = channel.unary_unary(
        f"/{SERVICE_NAME}/Describe",
        request_serializer=pb.MiddlewareDescribeRequest.SerializeToString,
        response_deserializer=pb.MiddlewareManifest.FromString,
    )
    gateway = ext.PeerMetadata(
        protocol_version=ext.ProtocolVersion(major=1),
        supported_capabilities=["openshell.supervisor-middleware.contract"],
    )
    manifest = stub(pb.MiddlewareDescribeRequest(gateway=gateway), timeout=10)
    assert (
        pb.SUPERVISOR_MIDDLEWARE_OPERATION_HTTP_RESPONSE,
        pb.SUPERVISOR_MIDDLEWARE_PHASE_PRE_RETURN,
    ) in [(binding.operation, binding.phase) for binding in manifest.bindings]


def test_other_responses_and_their_event_streams_are_skipped(
    channel: grpc.Channel, middleware: PolicyMiddleware, facts: Callable[..., HttpRequestFacts]
) -> None:
    assert middleware.evaluate_http(facts(tool_call(READ_TOOL), request_id="call-1")).allowed
    stream = sse({"jsonrpc": "2.0", "method": "notifications/progress"}, listing(["x"]))
    (result,) = respond(channel, [preflight("call-1", "text/event-stream"), *body_units(stream)])
    assert result.preflight_result.WhichOneof("action") == "skip"
    (unknown,) = respond(channel, [preflight("never-seen", "application/json")])
    assert unknown.preflight_result.WhichOneof("action") == "skip"


def body_of(result: Any) -> bytes:
    """The bytes a body result passes on: its replacement."""
    assert result.body_result.WhichOneof("action") == "transform", result
    return result.body_result.transform.data


def sse_messages(body: bytes) -> list[dict[str, Any]]:
    return [
        json.loads(line[len(b"data: ") :])
        for line in body.splitlines()
        if line.startswith(b"data: ")
    ]


def test_a_listing_over_an_event_stream_is_read_whole_rewritten_and_drift_is_found(
    channel: grpc.Channel,
    middleware: PolicyMiddleware,
    facts: Callable[..., HttpRequestFacts],
    events: EventStore,
) -> None:
    assert middleware.evaluate_http(facts(message("tools/list", {}), request_id="list-1")).allowed
    notice = {"jsonrpc": "2.0", "method": "notifications/message", "params": {"level": "info"}}
    stream = sse(notice, listing(["a", "c", DENIED_TOOL]))
    results = respond(
        channel,
        [
            preflight("list-1", "text/event-stream; charset=utf-8"),
            *body_units(stream),
            pb.HttpResponseEvent(trailers=pb.HttpResponseTrailers()),
        ],
    )
    preflight_result, unit, trailers = results
    assert (
        preflight_result.preflight_result.inspect.body_mode
        == pb.HTTP_RESPONSE_BODY_MODE_WHOLE_BODY_BYTES
    )
    passed, answer = sse_messages(body_of(unit))
    assert passed == notice  # other messages of the stream pass as they were
    assert [tool["name"] for tool in answer["result"]["tools"]] == ["a", "c"]
    assert [finding.type for finding in unit.body_result.findings] == ["tool_list_drift"]
    assert trailers.WhichOneof("result") == "trailers_result"
    (drift,) = records(events, "mcp-tools-drift")
    # Drift is judged on the server's own listing, denied tools included.
    assert (drift["added"], drift["removed"], drift["session"]) == (
        ["c", DENIED_TOOL],
        ["b"],
        "s-42",
    )


def test_a_paged_json_listing_that_matches_its_pin_is_no_drift(
    channel: grpc.Channel,
    middleware: PolicyMiddleware,
    facts: Callable[..., HttpRequestFacts],
    events: EventStore,
) -> None:
    for request_id, params, answer in (
        ("page-1", {}, listing(["a"], cursor="next")),
        ("page-2", {"cursor": "next"}, listing(["b"])),
    ):
        verdict = middleware.evaluate_http(
            facts(message("tools/list", params), request_id=request_id)
        )
        assert verdict.allowed
        first, unit = respond(
            channel,
            [preflight(request_id, "application/json"), *body_units(json.dumps(answer).encode())],
        )
        assert (
            first.preflight_result.inspect.body_mode == pb.HTTP_RESPONSE_BODY_MODE_WHOLE_BODY_BYTES
        )
        rewritten = json.loads(body_of(unit))
        sent = cast("dict[str, Any]", answer["result"])
        assert rewritten["result"]["tools"] == [
            {**tool, "_meta": {"sandbox-science/effect": "unclassified",
                               "sandbox-science/escalates": True}}
            for tool in sent["tools"]
        ]  # fmt: skip
        assert rewritten["result"].get("nextCursor") == sent.get("nextCursor")
        assert not unit.body_result.findings
    assert records(events, "mcp-tools-drift") == []


def test_listings_lose_denied_tools_and_mark_escalating_ones_over_the_servers_word(
    channel: grpc.Channel, middleware: PolicyMiddleware, facts: Callable[..., HttpRequestFacts]
) -> None:
    spoofed = {"sandbox-science/effect": "read", "sandbox-science/escalates": True, "x": 1}
    tools = [
        {"name": READ_TOOL, "inputSchema": {"type": "object"}, "_meta": spoofed},
        {"name": ESCALATED_TOOL, "inputSchema": {"type": "object"}},
        {"name": DENIED_TOOL, "inputSchema": {"type": "object"}, "_meta": spoofed},
    ]
    answer = {"jsonrpc": "2.0", "id": 7, "result": {"tools": tools}}
    assert middleware.evaluate_http(facts(message("tools/list", {}), request_id="list-2")).allowed
    _, unit = respond(
        channel, [preflight("list-2", "application/json"), *body_units(json.dumps(answer).encode())]
    )
    kept = {tool["name"]: tool["_meta"] for tool in json.loads(body_of(unit))["result"]["tools"]}
    assert set(kept) == {READ_TOOL, ESCALATED_TOOL}
    # The server's own marks are replaced: a read does not escalate, whatever it said.
    assert kept[READ_TOOL] == {"sandbox-science/effect": "read", "x": 1}
    assert kept[ESCALATED_TOOL]["sandbox-science/escalates"] is True
    assert kept[ESCALATED_TOOL]["sandbox-science/effect"] != "read"


def test_the_same_page_reads_alike_through_the_proxys_rule(
    middleware: PolicyMiddleware, example: LoadedPolicy
) -> None:
    from sandbox_science.policy.mcp_answers import callable_tools

    result: dict[str, object] = {
        "tools": [{"name": name} for name in (READ_TOOL, ESCALATED_TOOL, DENIED_TOOL)]
    }
    kept, denied = callable_tools(SERVER, result, example, ceiling=PoolCeiling.unrestricted())
    assert denied == 1
    assert [
        cast("dict[str, Any]", tool)["name"] for tool in cast("list[object]", kept["tools"])
    ] == [
        READ_TOOL,
        ESCALATED_TOOL,
    ]


@pytest.mark.parametrize(
    ("content_type", "body", "modes"),
    [
        pytest.param("application/json", b"{not json", None, id="unreadable"),
        pytest.param(
            "application/json",
            b'{"jsonrpc":"2.0","id":7,"result":{"tools":[{"name":"a"}],"tools":[]}}',
            None,
            id="duplicate-key",
        ),
        pytest.param(
            "application/json",
            b'{"jsonrpc":"2.0","id":7,"result":{"tools":{}}}',
            None,
            id="malformed",
        ),
        pytest.param("text/plain", b"", None, id="other-media"),
        pytest.param(
            "application/json",
            b"",
            [pb.HTTP_RESPONSE_BODY_MODE_HEADERS_ONLY, pb.HTTP_RESPONSE_BODY_MODE_STREAM_BYTES],
            id="not-whole",
        ),
    ],
)
def test_a_listing_that_cannot_be_read_whole_is_blocked(
    channel: grpc.Channel,
    middleware: PolicyMiddleware,
    facts: Callable[..., HttpRequestFacts],
    content_type: str,
    body: bytes,
    modes: list[Any] | None,
) -> None:
    assert middleware.evaluate_http(facts(message("tools/list", {}), request_id="list-3")).allowed
    head = preflight("list-3", content_type)
    if modes is not None:
        del head.preflight.permitted_body_modes[:]
        head.preflight.permitted_body_modes.extend(modes)
    results = respond(channel, [head, *body_units(body)])
    first = results[0].preflight_result
    if first.WhichOneof("action") == "block_delivery":
        assert first.reason_code == "tool_listing_unreadable"
        return
    assert first.WhichOneof("action") == "inspect"
    unit = results[1].body_result
    assert (unit.WhichOneof("action"), unit.reason_code) == (
        "block_delivery",
        "tool_listing_unreadable",
    )


def test_an_event_stream_listing_not_offered_whole_passes_unread_and_recorded(
    channel: grpc.Channel,
    middleware: PolicyMiddleware,
    facts: Callable[..., HttpRequestFacts],
    events: EventStore,
) -> None:
    # OpenShell 0.1.5 never offers an event stream whole: the runtime's client reads it.
    assert middleware.evaluate_http(facts(message("tools/list", {}), request_id="list-5")).allowed
    head = preflight("list-5", "text/event-stream")
    del head.preflight.permitted_body_modes[:]
    head.preflight.permitted_body_modes.extend(
        [pb.HTTP_RESPONSE_BODY_MODE_HEADERS_ONLY, pb.HTTP_RESPONSE_BODY_MODE_STREAM_BYTES]
    )
    (result,) = respond(channel, [head])
    assert result.preflight_result.WhichOneof("action") == "skip"
    (unread,) = records(events, "mcp-tools-listing-unread")
    assert (unread["server"], unread["request_id"], unread["method"]) == (
        SERVER,
        "list-5",
        "tools/list",
    )
    assert "pin" in unread
    # Nothing was read, so nothing drifted; and calls stay gated as before.
    assert records(events, "mcp-tools-drift") == []
    assert not middleware.evaluate_http(facts(tool_call(ESCALATED_TOOL))).allowed


def test_an_error_answer_to_a_listing_passes(
    channel: grpc.Channel, middleware: PolicyMiddleware, facts: Callable[..., HttpRequestFacts]
) -> None:
    assert middleware.evaluate_http(facts(message("tools/list", {}), request_id="list-4")).allowed
    (result,) = respond(channel, [preflight("list-4", "application/json", status=500)])
    assert result.preflight_result.WhichOneof("action") == "skip"


def test_without_listing_inspection_a_direct_sandbox_is_refused(
    store: PolicyStore,
    example: LoadedPolicy,
    events: EventStore,
    facts: Callable[..., HttpRequestFacts],
) -> None:
    bare = PolicyMiddleware(
        store, ceiling=PoolCeiling.unrestricted(), floor=HardFloor(), events=StoreSink(events)
    )
    problem = bare.validate_config(mcp_config(example))
    assert problem is not None
    assert "mcp_listings" in problem
    verdict = bare.evaluate_http(facts(tool_call(READ_TOOL)))
    assert verdict.reason_code == "mcp_listings_off"


def test_an_escalation_records_the_calls_summary_for_its_card(
    middleware: PolicyMiddleware, facts: Callable[..., HttpRequestFacts], events: EventStore
) -> None:
    arguments: dict[str, object] = {
        "page": "https://example.org/watch",
        "email": "a@example.org",
        "name": "w",
    }
    assert not middleware.evaluate_http(facts(tool_call(ESCALATED_TOOL, arguments))).allowed
    (escalation,) = records(events, "escalation")
    assert escalation["caller_kind"] == "agent"
    assert escalation["summary"] == {
        "recipient": "a@example.org",
        "url": "https://example.org/watch",
        "host": "example.org",
        "title": "w",
    }
    assert escalation["effect"] == "communicate"


def test_the_compiled_config_is_the_one_the_middleware_reads(example: LoadedPolicy) -> None:
    target = AgentMcpTarget(
        middleware="sandbox-science-policy", project="crypto", binaries=("/usr/bin/python3",)
    )
    compiled = compile_agent_mcp(example, target, ceiling=PoolCeiling.unrestricted())
    assert compiled.policy.network_middlewares is not None
    entry = compiled.policy.network_middlewares["sandbox-science-policy"]
    assert parse_config(entry.config) == parse_config(mcp_config(example))
