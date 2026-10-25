# SPDX-License-Identifier: MIT
"""Detect-and-hold in the middleware (D103): a rule that fires holds the session's later
calls only, as a durable permission request the user clears or stops; an action-like GET
asks on its own; a drifted server's non-read calls ask until re-accepted; holds and drifts
survive a restart, counts do not."""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from functools import partial
from pathlib import Path
from typing import Any

import pytest

from sandbox_science.event_store import EventStore, StoreReader
from sandbox_science.policy import HardFloor, LoadedPolicy, PoolCeiling, reasons
from sandbox_science.policy.approvals import Approvals
from sandbox_science.policy.approvals.commands import prepare_answer
from sandbox_science.policy.store import Acceptance
from sandbox_science.policy_middleware.cli import _read_records, main
from sandbox_science.policy_middleware.evaluator import HttpRequestFacts, PolicyMiddleware
from sandbox_science.policy_middleware.events import StoreSink
from sandbox_science.policy_middleware.mcp import ToolListings
from sandbox_science.policy_middleware.store import PolicyStore

FactsFactory = Callable[..., HttpRequestFacts]
OTHER = "sbx-0002"
POLICY = """\
apiVersion: sandbox-science/v0alpha1
kind: ProjectPolicy
metadata:
  name: detect
defaults: {action: allow}
network:
  rules:
    - {name: no-bad, operation: http, match: {hosts: ["*.bad.example.org"]}, action: deny}
detection:
  same_url: 3
  denials: 3
  failures_per_host: 2
"""


class Clock:
    def __init__(self) -> None:
        self.at = datetime.now(UTC)

    def __call__(self) -> datetime:
        return self.at

    def advance(self, seconds: float) -> None:
        self.at += timedelta(seconds=seconds)


@pytest.fixture
def events(tmp_path: Path) -> Iterator[EventStore]:
    with EventStore(tmp_path / "state" / "events.db", timeout=0.5) as store:
        yield store


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def loaded(store: PolicyStore, sandbox_id: str, example: LoadedPolicy) -> LoadedPolicy:
    del example
    policy = store.add_policy(POLICY.encode())
    store.bind(sandbox_id, policy.digest)
    store.bind(OTHER, policy.digest)
    return policy


def build(store: PolicyStore, events: EventStore, clock: Clock, **options: Any) -> PolicyMiddleware:
    middleware = PolicyMiddleware(
        store,
        ceiling=PoolCeiling.unrestricted(),
        floor=HardFloor(),
        events=StoreSink(events),
        now=clock,
        requests=partial(_read_records, events.path),
        **options,
    )
    middleware.prime_approvals()
    return middleware


@pytest.fixture
def middleware(
    store: PolicyStore, loaded: LoadedPolicy, events: EventStore, clock: Clock
) -> PolicyMiddleware:
    del loaded
    return build(store, events, clock)


@pytest.fixture
def facts(make_facts: FactsFactory, loaded: LoadedPolicy) -> FactsFactory:
    config = {"policy": {"name": loaded.policy.metadata.name, "digest": loaded.digest}}

    def make(path: str = "/x", **overrides: object) -> HttpRequestFacts:
        return make_facts(**{"config": config, "path": path, **overrides})

    return make


def records(events: EventStore, kind: str) -> list[tuple[int, dict[str, Any]]]:
    with StoreReader(events.path) as reader:
        return [(record.seq, record.data()) for record in reader.records() if record.kind == kind]


def holds(events: EventStore) -> list[tuple[int, dict[str, Any]]]:
    return [(seq, data) for seq, data in records(events, "escalation") if "hold" in data]


def answer(events: EventStore, seq: int, *, approve: bool) -> None:
    approvals = Approvals()
    approvals.load(_read_records(events.path, 0))
    kind, payload = prepare_answer(
        approvals, seq, approve=approve, scope=None, hours=None, now=datetime.now(UTC)
    )
    events.append(kind, "workspace", payload)


def hammer(middleware: PolicyMiddleware, facts: FactsFactory, times: int = 4) -> int:
    """The same URL ``times`` times: the hold the last call raised."""
    codes = [middleware.evaluate_http(facts(request_id=f"r{i}")).reason_code for i in range(times)]
    return held_by(codes[-1])


def held_by(code: str) -> int:
    found = re.fullmatch(r"session_held_r([0-9]+)", code)
    assert found is not None, code
    return int(found[1])


def test_a_rule_holds_the_sandboxs_calls_at_its_threshold_and_not_below(
    middleware: PolicyMiddleware, facts: FactsFactory, events: EventStore
) -> None:
    for i in range(3):
        assert middleware.evaluate_http(facts(request_id=f"r{i}")).allowed
    assert holds(events) == []
    fired = middleware.evaluate_http(facts(request_id="r3"))
    seq = held_by(fired.reason_code)
    [(recorded, hold)] = holds(events)
    assert recorded == seq
    assert hold["target"]["kind"] == "hold"
    assert (hold["target"]["sandbox_id"], hold["target"]["session"]) == ("sbx-0001", "")
    assert (hold["rule"], hold["caller_kind"], hold["max_grant"]) == (
        "detect.same-url",
        "code",
        "once",
    )
    assert (hold["hold"]["count"], hold["hold"]["threshold"]) == (4, 3)
    assert hold["hold"]["requests"][-1] == {"method": "GET", "host": "example.org", "target": "/x"}
    # Every later call of the sandbox is held, whatever it is; another sandbox's is not.
    later = middleware.evaluate_http(facts("/elsewhere", request_id="r4"))
    assert later.reason_code == fired.reason_code
    assert middleware.evaluate_http(facts(sandbox_id=OTHER, request_id="o1")).allowed
    assert len(holds(events)) == 1


def test_the_query_is_never_recorded_with_a_hold(
    middleware: PolicyMiddleware, facts: FactsFactory, events: EventStore
) -> None:
    for i in range(4):
        middleware.evaluate_http(facts(query="key=test-secret-123", request_id=f"r{i}"))
    [(_, hold)] = holds(events)
    assert "test-secret" not in json.dumps(hold)
    assert hold["hold"]["requests"][-1]["target"] == "/x?(19 bytes)"


def test_clearing_resumes_the_calls_and_forgets_the_counts(
    middleware: PolicyMiddleware, facts: FactsFactory, events: EventStore, clock: Clock
) -> None:
    seq = hammer(middleware, facts)
    answer(events, seq, approve=True)
    clock.advance(2)
    for i in range(3):  # a fresh count: the same URL three times again
        assert middleware.evaluate_http(facts(request_id=f"c{i}")).allowed
    assert held_by(middleware.evaluate_http(facts(request_id="c3")).reason_code) > seq


def test_stopping_ends_the_hold_but_calls_that_go_on_hold_again(
    middleware: PolicyMiddleware, facts: FactsFactory, events: EventStore, clock: Clock
) -> None:
    seq = hammer(middleware, facts)
    answer(events, seq, approve=False)
    clock.advance(2)
    again = middleware.evaluate_http(facts(request_id="s1"))
    assert held_by(again.reason_code) > seq  # the counts stand
    assert len(holds(events)) == 2


def test_a_hold_is_answered_within_a_second(
    middleware: PolicyMiddleware, facts: FactsFactory, events: EventStore, clock: Clock
) -> None:
    answer(events, hammer(middleware, facts), approve=True)
    assert middleware.evaluate_http(facts("/y", request_id="a")).allowed  # read at once
    assert middleware.evaluate_http(facts("/z", request_id="b")).allowed


def test_a_streak_of_refusals_holds(
    middleware: PolicyMiddleware, facts: FactsFactory, events: EventStore
) -> None:
    for i in range(4):
        verdict = middleware.evaluate_http(facts(f"/{i}", host=f"h{i}.bad.example.org"))
        assert verdict.reason_code == reasons.POLICY_DENIED  # the refusal itself stands
    [(seq, hold)] = holds(events)
    assert hold["rule"] == "detect.denial-streak"
    assert held_by(middleware.evaluate_http(facts("/ok")).reason_code) == seq


def test_failed_responses_count_when_statuses_are_read(
    store: PolicyStore, loaded: LoadedPolicy, events: EventStore, clock: Clock, facts: FactsFactory
) -> None:
    del loaded
    middleware = build(store, events, clock, response_statuses=True)
    for i in range(3):
        assert middleware.evaluate_http(facts(f"/{i}", request_id=f"r{i}")).allowed
        middleware.response_status(f"r{i}", "sbx-0001", 404 if i else 200)
    middleware.response_status("r1", "sbx-0001", 500)  # answered once only
    assert holds(events) == []
    assert middleware.evaluate_http(facts("/3", request_id="r3")).allowed
    middleware.response_status("r3", OTHER, 500)  # not that sandbox's request
    assert holds(events) == []
    assert middleware.evaluate_http(facts("/4", request_id="r4")).allowed
    middleware.response_status("r4", "sbx-0001", 503)
    [(seq, hold)] = holds(events)
    assert hold["rule"] == "detect.host-failures"
    assert held_by(middleware.evaluate_http(facts("/5")).reason_code) == seq


def test_an_action_like_get_holds_only_that_call(
    middleware: PolicyMiddleware, facts: FactsFactory, events: EventStore, clock: Clock
) -> None:
    link = facts("/newsletter/unsubscribe", query="token=f3a9c1e07b2d4c5a8e9f0a1b2c3d4e5f")
    verdict = middleware.evaluate_http(link)
    assert verdict.reason_code == reasons.APPROVAL_REQUIRED
    [(seq, request)] = records(events, "escalation")
    assert (request["rule"], request["max_grant"], request["target"]["kind"]) == (
        "detect.action-url",
        "once",
        "http",
    )
    assert request["escalation"]["on_timeout"] == "deny"
    assert middleware.evaluate_http(facts("/papers")).allowed  # the sandbox is not held
    answer(events, seq, approve=True)
    assert middleware.evaluate_http(link).allowed


def test_detection_can_be_turned_off_per_project(
    store: PolicyStore, events: EventStore, clock: Clock, make_facts: FactsFactory
) -> None:
    text = POLICY.replace("detection:\n", "detection:\n  enabled: false\n  action_urls: true\n")
    policy = store.add_policy(text.encode())
    store.bind("sbx-0001", policy.digest)
    middleware = build(store, events, clock)
    config = {"policy": {"name": "detect", "digest": policy.digest}}
    for i in range(10):
        assert middleware.evaluate_http(make_facts(config=config, request_id=f"r{i}")).allowed
    link = make_facts(config=config, path="/confirm", query="t=9a8b7c6d5e4f3a2b1c0d9e8f7a6b5c4d")
    assert middleware.evaluate_http(link).allowed
    assert records(events, "escalation") == []


def test_near_misses_and_holds_are_logged_as_findings(
    middleware: PolicyMiddleware, facts: FactsFactory, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="sandbox_science.policy_middleware")
    for i in range(4):
        middleware.evaluate_http(facts(request_id=f"r{i}"))
    found = [
        (r.__dict__["outcome"], r.__dict__["rule"], r.__dict__["count"])
        for r in caplog.records
        if r.__dict__.get("event") == "detect_finding"
    ]
    assert ("near_miss", "same-url", 2) in found
    assert ("hold", "same-url", 4) in found


def test_a_hold_survives_a_restart_and_the_counts_do_not(
    store: PolicyStore,
    loaded: LoadedPolicy,
    events: EventStore,
    clock: Clock,
    facts: FactsFactory,
) -> None:
    del loaded
    first = build(store, events, clock)
    for i in range(4):
        first.evaluate_http(facts(request_id=f"r{i}"))
    for i in range(3):
        assert first.evaluate_http(facts(sandbox_id=OTHER, request_id=f"o{i}")).allowed
    restarted = build(store, events, clock)
    assert restarted.evaluate_http(facts("/y")).reason_code.startswith("session_held_r")
    # The other sandbox's three calls were forgotten: a fourth does not hold it.
    assert restarted.evaluate_http(facts(sandbox_id=OTHER, request_id="o3")).allowed


# The agent's MCP calls straight to a server (D98), by session.
SERVER = "crypto-research-mcp"
MCP_POLICY_LINES = (
    "  effects:\n    write: {action: allow}\n    communicate: {action: allow}\n  servers:\n"
)


@pytest.fixture
def mcp(store: PolicyStore, events: EventStore, clock: Clock, example: LoadedPolicy) -> Any:
    del example
    source = (Path(__file__).resolve().parents[3] / "examples" / "policy.yaml").read_text()
    source = source.replace("  servers:\n", MCP_POLICY_LINES, 1)
    source += "detection:\n  same_url: 3\n"
    policy = store.add_policy(source.encode())
    store.bind("sbx-agent", policy.digest)
    middleware = build(store, events, clock, listings=ToolListings())
    config = {
        "policy": {"name": policy.policy.metadata.name, "digest": policy.digest},
        "mcp": {"project": "crypto", "caller": "agent-runtime"},
    }

    def call(
        tool: str,
        session: str = "s-1",
        request_id: str = "m",
        on: PolicyMiddleware | None = None,
        **params: Any,
    ) -> Any:
        body: dict[str, Any] = {"jsonrpc": "2.0", "id": 7, "method": "tools/call"}
        body["params"] = {"name": tool, "arguments": params or {"query": "SNOVA"}}
        if tool == "tools/list":
            body = {"jsonrpc": "2.0", "id": 7, "method": "tools/list", "params": {}}
        return (on or middleware).evaluate_http(
            HttpRequestFacts(
                sandbox_id="sbx-agent",
                request_id=request_id,
                scheme="https",
                host="mcp.example.invalid",
                port=443,
                method="POST",
                path="/crypto-research",
                query="",
                headers=(
                    ("content-type", "application/json"),
                    ("sandbox-science-session", session),
                ),
                body=json.dumps(body).encode(),
                config=config,
            )
        )

    return middleware, call, store


READ_TOOL = "attack-catalog-mcp_search_attacks"
WRITE_TOOL = "firecrawl-mcp_firecrawl_monitor_create"


def test_mcp_calls_are_held_by_session(mcp: Any, events: EventStore) -> None:
    _, call, _ = mcp
    for i in range(3):
        assert call(READ_TOOL, request_id=f"m{i}").allowed
    fired = call(READ_TOOL, request_id="m3")
    seq = held_by(fired.reason_code)
    [(_, hold)] = holds(events)
    assert (hold["target"]["project_id"], hold["target"]["session"]) == ("crypto", "s-1")
    assert hold["caller_kind"] == "agent"
    assert held_by(call(READ_TOOL, request_id="m4", query="other").reason_code) == seq
    assert call(READ_TOOL, session="s-2", request_id="n1").allowed  # another session
    [*_, last] = records(events, "mcp-call")
    assert last[1]["session"] == "s-2"


def test_a_drifted_server_asks_for_its_non_read_calls_until_re_accepted(
    mcp: Any, events: EventStore, clock: Clock, tmp_path: Path
) -> None:
    middleware, call, store = mcp
    write = call(WRITE_TOOL, request_id="w0", url="https://example.org", interval=1)
    assert write.allowed, write.reason  # the policy allows writes
    assert call("tools/list", request_id="list-1").allowed
    check = middleware.check_listing("list-1", "sbx-agent", stream=False)
    assert check is not None
    tool = {"name": "brand_new", "description": "x", "inputSchema": {"type": "object"}}
    check.feed(json.dumps({"jsonrpc": "2.0", "id": 7, "result": {"tools": [tool]}}).encode())
    assert check.finish().drift is not None
    [(_, drift)] = records(events, "mcp-tools-drift")
    assert drift["server"] == SERVER
    assert isinstance(drift["pin"], str)
    held = call(WRITE_TOOL, request_id="w1", url="https://example.org", interval=2)
    assert held.reason_code.startswith("approval_required_r")
    [*_, (_, request)] = records(events, "escalation")
    assert (request["rule"], request["max_grant"]) == ("detect.tool-list-drift", "once")
    assert call(READ_TOOL, request_id="r1").allowed  # reads stay free
    # A restart keeps the drift; the operator's acceptance ends it.
    restarted = build(store, events, clock, listings=ToolListings())
    kept = call(WRITE_TOOL, request_id="w9", on=restarted, url="https://example.org", interval=9)
    assert kept.reason_code.startswith("approval_required_r")
    settings = tmp_path / "middleware.toml"
    settings.write_text(
        f'state_dir = "{tmp_path / "state"}"\nceiling = "c.yaml"\nallow_unauthenticated = true\n'
    )
    assert main(["accept-tools", "--settings", str(settings), "--server", SERVER]) == 0
    clock.advance(2)
    for running in (middleware, restarted):
        again = call(WRITE_TOOL, request_id="w2", on=running, url="https://e.org", interval=3)
        assert again.allowed, again.reason


def test_the_command_line_lists_and_clears_a_hold(
    middleware: PolicyMiddleware,
    facts: FactsFactory,
    events: EventStore,
    clock: Clock,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    seq = hammer(middleware, facts)
    settings = tmp_path / "middleware.toml"
    settings.write_text(
        f'state_dir = "{tmp_path / "state"}"\nceiling = "c.yaml"\nallow_unauthenticated = true\n'
    )
    assert main(["pending", "--settings", str(settings)]) == 0
    assert "hold of research code's outward calls (same-url)" in capsys.readouterr().out
    assert main(["approve", "--settings", str(settings), "--request", str(seq)]) == 0
    clock.advance(2)
    assert middleware.evaluate_http(facts("/y")).allowed


def test_the_service_reads_response_statuses_only_when_asked(
    store: PolicyStore, loaded: LoadedPolicy, events: EventStore, clock: Clock, facts: FactsFactory
) -> None:
    import grpc

    from sandbox_science.policy_middleware._proto import extension_pb2 as ext
    from sandbox_science.policy_middleware._proto import supervisor_middleware_pb2 as pb
    from sandbox_science.policy_middleware.auth import UnverifiedCallers
    from sandbox_science.policy_middleware.service import (
        RESPONSE_SERVICE_NAME,
        SERVICE_NAME,
        MiddlewareService,
        build_server,
    )

    del loaded
    gateway = ext.PeerMetadata(
        protocol_version=ext.ProtocolVersion(major=1),
        supported_capabilities=["openshell.supervisor-middleware.contract"],
    )
    for statuses in (False, True):
        middleware = build(store, events, clock, response_statuses=statuses)
        service = MiddlewareService(middleware, UnverifiedCallers(), max_payload_bytes=1024)
        server, port = build_server(service, listen="127.0.0.1:0", workers=2, credentials=None)
        server.start()
        try:
            with grpc.insecure_channel(f"127.0.0.1:{port}") as channel:
                describe = channel.unary_unary(
                    f"/{SERVICE_NAME}/Describe",
                    request_serializer=pb.MiddlewareDescribeRequest.SerializeToString,
                    response_deserializer=pb.MiddlewareManifest.FromString,
                )
                manifest = describe(pb.MiddlewareDescribeRequest(gateway=gateway), timeout=10)
                operations = [binding.operation for binding in manifest.bindings]
                assert (pb.SUPERVISOR_MIDDLEWARE_OPERATION_HTTP_RESPONSE in operations) is statuses
                respond = channel.stream_stream(
                    f"/{RESPONSE_SERVICE_NAME}/Evaluate",
                    request_serializer=pb.HttpResponseEvent.SerializeToString,
                    response_deserializer=pb.HttpResponseEventResult.FromString,
                )
                for i in range(3):
                    request_id = f"{statuses}-{i}"
                    assert middleware.evaluate_http(facts(f"/{i}", request_id=request_id)).allowed
                    context = pb.RequestContext(request_id=request_id, sandbox_id="sbx-0001")
                    event = pb.HttpResponseEvent(
                        preflight=pb.HttpResponsePreflight(context=context, status_code=500)
                    )
                    (result,) = list(respond(iter([event]), timeout=10))
                    assert result.preflight_result.WhichOneof("action") == "skip"
        finally:
            server.stop(grace=None)
    [(_, hold)] = holds(events)  # only the run that reads statuses counted them
    assert hold["rule"] == "detect.host-failures"


def _drift(middleware: PolicyMiddleware, call: Any, request_id: str) -> None:
    """A listing that adds a tool and changes a pinned one."""
    assert call("tools/list", request_id=request_id).allowed
    check = middleware.check_listing(request_id, "sbx-agent", stream=False)
    assert check is not None
    added = {"name": "brand_new", "description": "x", "inputSchema": {"type": "object"}}
    changed = {"name": READ_TOOL, "description": "now different", "inputSchema": {}}
    result = {"tools": [added, changed]}
    check.feed(json.dumps({"jsonrpc": "2.0", "id": 7, "result": result}).encode())
    assert check.finish().drift is not None


def test_a_drift_record_gives_the_digests_of_added_and_changed_tools(
    mcp: Any, events: EventStore
) -> None:
    middleware, call, _ = mcp
    _drift(middleware, call, "list-d")
    [(_, drift)] = records(events, "mcp-tools-drift")
    assert drift["added"] == ["brand_new"]
    assert READ_TOOL in drift["changed"]
    assert set(drift["digests"]) == {"brand_new", READ_TOOL}
    assert all(re.fullmatch(r"[0-9a-f]{64}", digest) for digest in drift["digests"].values())
    assert drift["hints"] == {"brand_new": "unclassified"}


def test_an_acceptance_naming_the_project_ends_its_hold(
    mcp: Any, events: EventStore, clock: Clock
) -> None:
    middleware, call, _ = mcp
    _drift(middleware, call, "list-a")

    def write(request_id: str) -> Any:
        # Each call its own arguments: the policy holds the same call made thrice.
        return call(WRITE_TOOL, request_id=request_id, url="https://example.org", n=request_id)

    assert write("w1").reason_code.startswith("approval_required_r")
    events.append(
        "mcp-tools-accepted",
        "workspace",
        {"project": "another", "server": SERVER, "reason": "x", "actor": "user"},
    )
    clock.advance(2)
    assert write("w2").reason_code.startswith("approval_required_r")  # another project's
    events.append(
        "mcp-tools-accepted",
        "workspace",
        {"project": "crypto", "server": SERVER, "reason": "reviewed", "actor": "user"},
    )
    clock.advance(2)
    assert write("w3").allowed
    # A new drift holds again, until a classification accepted for the project ends it.
    _drift(middleware, call, "list-b")
    assert write("w4").reason_code.startswith("approval_required_r")
    events.append(
        "mcp-classification-accepted",
        "workspace",
        {"project": "crypto", "server": SERVER, "revision": "a" * 64, "actor": "user"},
    )
    clock.advance(2)
    assert write("w5").allowed


def test_a_projects_accepted_revision_decides_its_calls(mcp: Any, events: EventStore) -> None:
    _, call, store = mcp
    assert call(READ_TOOL, request_id="r0").allowed
    data = (
        "apiVersion: sandbox-science/v0alpha1\nkind: ToolClassification\n"
        f"server: {SERVER}\ntools:\n  {READ_TOOL}:\n    class: browse\n"
        '    reason: "Reviewed: it drives a browser."\n'
    ).encode()
    store.accept_classification(
        "crypto", SERVER, data, Acceptance("user", datetime.now(UTC), "reviewed")
    )
    refused = call(READ_TOOL, request_id="r1")
    assert not refused.allowed
    assert refused.decision is not None
    assert refused.decision.effect == "browse"
    # A binding that cannot be read refuses the call rather than falling back.
    current = store.classification_binding("crypto", SERVER)
    assert current is not None
    path = Path(events.path).parent / "classifications" / "crypto" / SERVER / "current.json"
    path.write_text("not json")
    broken = call(READ_TOOL, request_id="r2")
    assert broken.reason_code == reasons.POLICY_UNAVAILABLE
