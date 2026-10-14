# SPDX-License-Identifier: MIT
"""The proxy end to end, against a fake MCP server over Streamable HTTP and TLS."""

import asyncio
import json
import logging
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

import pytest

from sandbox_science.event_store import EventStore
from sandbox_science.mcp_proxy.cli import _answer, _pending
from sandbox_science.mcp_proxy.upstream import address_check
from sandbox_science.policy import HardFloor, LoadedPolicy, PoolCeiling
from sandbox_science.policy.approvals import Approvals, RequestKey, prepare_answer

pytestmark = pytest.mark.asyncio

PLACEHOLDER = "openshell:resolve:env:v1_PAPERS_TOKEN"
REFUSED = -31001
UPSTREAM_FAILED = -31002


class Seen(Protocol):
    method: str
    headers: dict[str, str]
    body: bytes
    message: object


class Fake(Protocol):
    """The ``fake`` fixture (conftest's FakeServer)."""

    port: int
    seen: list[Seen]
    version: str | None
    tools: list[str]
    page_size: int | None
    sessions: set[str]
    gate: asyncio.Event | None
    session_id: str | None
    initialize_extra: dict[str, object]
    last_cursor: str | None

    def answer(
        self, name: str, body: bytes | Callable[[object], bytes], **options: Any
    ) -> None: ...
    def messages(self, method: str | None = None) -> list[dict[str, Any]]: ...
    def tool_calls(self) -> list[str]: ...


class Answer(Protocol):
    status: int
    headers: Mapping[str, str]
    body: Any


class Sink(Protocol):
    events: list[tuple[str, Mapping[str, object]]]
    fail: bool
    now: Callable[[], datetime]

    def kinds(self, kind: str) -> list[Mapping[str, Any]]: ...
    def records(self, after: int) -> list[Any]: ...
    def append(self, kind: str, payload: Mapping[str, object]) -> int: ...


class Harness(Protocol):
    """The ``harness`` fixture: the proxy in front of the fake server."""

    fake: Fake
    sink: Sink
    environ: dict[str, str]
    address_check: Callable[..., Any]

    def policy(self, **overrides: Any) -> str: ...
    def bind(self, text: str, project: str = "crypto") -> LoadedPolicy: ...
    async def post(self, body: object, **options: Any) -> Answer: ...
    async def request(self, method: str, data: bytes = b"", **options: Any) -> Answer: ...
    async def initialize(
        self, caller: str = "orchestrator", version: str = "2025-11-25"
    ) -> str: ...
    async def call(
        self, session: str, method: str, params: Mapping[str, object] | None = None, **options: Any
    ) -> Answer: ...
    async def tool(
        self,
        session: str,
        name: str,
        arguments: Mapping[str, object] | None = None,
        *,
        caller: str = "orchestrator",
    ) -> Answer: ...


def initialize_message(version: str = "2025-11-25", request_id: object = 1) -> dict[str, object]:
    params = {
        "protocolVersion": version,
        "capabilities": {"sampling": {}, "elicitation": {}},
        "clientInfo": {"name": "test-client", "version": "1"},
    }
    return {"jsonrpc": "2.0", "id": request_id, "method": "initialize", "params": params}


def reason(answer: Answer) -> str:
    """The reason code of a refusal."""
    return answer.body["error"]["data"]["reason"]


def last_call(harness: Harness) -> Mapping[str, Any]:
    """The newest per-call record."""
    return harness.sink.kinds("mcp-call")[-1]


async def eventually(condition: Callable[[], bool], timeout: float = 5.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not condition():
        if loop.time() > deadline:
            raise AssertionError("the condition did not become true in time")
        await asyncio.sleep(0.01)


# --- Sessions and the handshake -------------------------------------------------------------


async def test_initialize_runs_the_proxys_own_handshake(harness: Harness) -> None:
    answer = await harness.post(initialize_message("2025-06-18", request_id="init-1"))
    assert answer.status == 200
    assert answer.body["id"] == "init-1"
    result = answer.body["result"]
    # The policy pins 2025-11-25, which OpenShell's fence enforces.
    assert result["protocolVersion"] == "2025-11-25"
    # The policy denies the task methods, so the proxy does not offer tasks.
    assert result["capabilities"] == {"tools": {"listChanged": True}}
    assert result["serverInfo"] == {"name": "fake", "version": "1.0"}
    session = answer.headers["Mcp-Session-Id"]
    fake = harness.fake
    [initialize] = fake.messages("initialize")
    # No client capabilities, so the server has no grounds for sampling or elicitation.
    assert initialize["params"] == {
        "protocolVersion": "2025-11-25",
        "capabilities": {},
        "clientInfo": {"name": "sandbox-science-mcp-proxy", "version": "0.1.0"},
    }
    assert "mcp-protocol-version" not in fake.seen[0].headers
    assert "mcp-session-id" not in fake.seen[0].headers
    initialized = fake.seen[1]
    assert initialized.message == {"jsonrpc": "2.0", "method": "notifications/initialized"}
    assert initialized.headers["mcp-protocol-version"] == "2025-11-25"
    assert initialized.headers["mcp-session-id"] == "upstream-session-1"
    # The caller's own notifications/initialized is not forwarded again.
    notified = await harness.post(
        {"jsonrpc": "2.0", "method": "notifications/initialized"}, session=session
    )
    assert notified.status == 202
    assert len(fake.messages("notifications/initialized")) == 1
    assert session not in fake.sessions


async def test_an_unpinned_server_uses_the_callers_revision(harness: Harness) -> None:
    harness.bind(harness.policy(revision="1"))  # not an MCP revision: nothing is pinned
    session = await harness.initialize(version="2025-06-18")
    await harness.tool(session, "search")
    fake = harness.fake
    assert fake.messages("initialize")[0]["params"]["protocolVersion"] == "2025-06-18"
    assert {seen.headers["mcp-protocol-version"] for seen in fake.seen[1:]} == {"2025-06-18"}
    # A revision the proxy does not speak gets its newest instead.
    await harness.initialize(version="2024-11-05")
    assert fake.messages("initialize")[1]["params"]["protocolVersion"] == "2025-11-25"


async def test_a_revision_the_proxy_cannot_speak_is_refused(harness: Harness) -> None:
    harness.bind(harness.policy(revision="2026-07-28"))
    answer = await harness.post(initialize_message())
    assert answer.body["error"]["code"] == REFUSED
    assert reason(answer) == "protocol_unsupported"
    assert harness.fake.seen == []


async def test_a_server_answering_another_revision_is_refused(harness: Harness) -> None:
    harness.fake.version = "2025-06-18"  # the policy pins 2025-11-25
    answer = await harness.post(initialize_message())
    assert answer.body["error"]["code"] == UPSTREAM_FAILED
    assert reason(answer) == "protocol_unsupported"
    assert "Mcp-Session-Id" not in answer.headers


async def test_every_request_after_initialize_carries_the_revision(harness: Harness) -> None:
    session = await harness.initialize()
    await harness.call(session, "tools/list")
    await harness.tool(session, "search")
    await harness.call(session, "ping")
    await harness.tool(session, "stream")  # the proxy answers two server requests
    fake = harness.fake
    await eventually(lambda: len([m for m in fake.messages() if "method" not in m]) == 2)
    assert "mcp-protocol-version" not in fake.seen[0].headers  # initialize
    later = fake.seen[1:]
    assert len(later) >= 7
    assert {seen.headers.get("mcp-protocol-version") for seen in later} == {"2025-11-25"}
    assert {seen.headers.get("mcp-session-id") for seen in later} == {"upstream-session-1"}


async def test_a_caller_keeps_to_its_sessions_revision(harness: Harness) -> None:
    session = await harness.initialize()
    other = {"MCP-Protocol-Version": "2025-06-18"}
    answer = await harness.call(session, "tools/list", headers=other)
    assert answer.status == 400
    assert reason(answer) == "protocol_unsupported"
    same = {"MCP-Protocol-Version": "2025-11-25"}
    assert "result" in (await harness.call(session, "tools/list", headers=same)).body
    assert "result" in (await harness.call(session, "tools/list")).body


async def test_requests_need_the_callers_live_session(harness: Harness) -> None:
    answer = await harness.tool("no-such-session", "search")
    assert (answer.status, reason(answer)) == (404, "session_unknown")
    tool_call = {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "search"}}
    answer = await harness.post(tool_call)
    assert (answer.status, reason(answer)) == (400, "session_required")
    session = await harness.initialize()
    answer = await harness.post(initialize_message(), session=session)
    assert answer.status == 400
    # A session id is honoured only for the caller that opened the session.
    answer = await harness.tool(session, "search", caller="shared")
    assert (answer.status, reason(answer)) == (404, "session_unknown")
    assert harness.fake.tool_calls() == []
    assert "result" in (await harness.tool(session, "search")).body


async def test_a_session_the_server_ended_ends_at_the_proxy(harness: Harness) -> None:
    session = await harness.initialize()
    harness.fake.sessions.clear()
    answer = await harness.tool(session, "search")
    assert (answer.status, reason(answer)) == (404, "session_unknown")
    answer = await harness.call(session, "ping")
    assert (answer.status, reason(answer)) == (404, "session_unknown")
    session = await harness.initialize()
    assert "result" in (await harness.tool(session, "search")).body


async def test_a_changed_server_entry_ends_the_session(harness: Harness) -> None:
    session = await harness.initialize()
    harness.bind(harness.policy(revision="2025-06-18"))
    answer = await harness.tool(session, "search")
    assert (answer.status, reason(answer)) == (404, "session_unknown")


async def test_delete_ends_the_session_without_contacting_the_server(harness: Harness) -> None:
    session = await harness.initialize()
    seen = len(harness.fake.seen)
    answer = await harness.request("DELETE", session=session)
    assert answer.status == 204
    # OpenShell's MCP inspection refuses DELETE, so the proxy does not send one.
    assert len(harness.fake.seen) == seen
    answer = await harness.tool(session, "search")
    assert (answer.status, reason(answer)) == (404, "session_unknown")
    assert (await harness.request("DELETE", session=session)).status == 404


async def test_there_is_no_stream_for_server_messages(harness: Harness) -> None:
    answer = await harness.request("GET")
    assert answer.status == 405
    assert answer.headers["Allow"] == "POST, DELETE"


# --- Callers and attribution ----------------------------------------------------------------


@pytest.mark.parametrize(
    "authorization",
    [
        None,
        "Bearer test-secret-unknown",
        "Basic dGVzdDp0ZXN0",
        "Bearer",
        "test-secret-orchestrator",
    ],
)
async def test_callers_need_a_known_token(harness: Harness, authorization: str | None) -> None:
    headers = {} if authorization is None else {"Authorization": authorization}
    answer = await harness.post(initialize_message(), caller="", headers=headers)
    assert answer.status == 401
    assert reason(answer) == "unauthenticated"
    assert answer.headers["WWW-Authenticate"] == "Bearer"
    assert harness.fake.seen == []
    assert harness.sink.events == []


async def test_requests_from_web_pages_are_refused(harness: Harness) -> None:
    answer = await harness.post(initialize_message(), headers={"Origin": "https://example.org"})
    assert (answer.status, reason(answer)) == (403, "origin_forbidden")
    assert harness.fake.seen == []


async def test_a_project_without_a_policy_is_refused(harness: Harness) -> None:
    answer = await harness.post(initialize_message(), caller="other")
    assert (answer.status, reason(answer)) == (503, "project_unbound")
    assert harness.fake.seen == []
    record = last_call(harness)
    assert record["policy"] is None
    assert record["attribution"]["project"] == "other"


async def test_workers_need_worker_access(harness: Harness) -> None:
    harness.bind(harness.policy(worker_access="false"))
    for caller in ("shared", "dedicated"):
        answer = await harness.post(initialize_message(), caller=caller)
        assert answer.body["error"]["code"] == REFUSED
        assert reason(answer) == "policy_denied"
    assert harness.fake.seen == []
    session = await harness.initialize("orchestrator")
    assert "result" in (await harness.tool(session, "search")).body


async def test_worker_tools_narrow_what_worker_code_may_call(harness: Harness) -> None:
    harness.bind(harness.policy(worker_tools="[search]"))
    worker = await harness.initialize("shared")
    assert "result" in (await harness.tool(worker, "search", caller="shared")).body
    answer = await harness.tool(worker, "image", caller="shared")
    assert reason(answer) == "policy_denied"
    orchestrator = await harness.initialize()
    assert "result" in (await harness.tool(orchestrator, "image")).body
    assert harness.fake.tool_calls() == ["search", "image"]


@pytest.mark.parametrize(
    ("caller", "label", "expected"),
    [
        ("orchestrator", "s-1", {"level": "orchestrator", "session": "s-1", "session_claim": None}),
        ("orchestrator", None, {"level": "orchestrator", "session": None, "session_claim": None}),
        ("shared", "s-2", {"level": "container", "session": None, "session_claim": "s-2"}),
        ("dedicated", None, {"level": "sandbox", "session": "s-42", "session_claim": None}),
        ("dedicated", "s-9", {"level": "sandbox", "session": "s-42", "session_claim": "s-9"}),
    ],
)
async def test_calls_are_attributed_as_far_as_the_caller_can_be_trusted(
    harness: Harness, caller: str, label: str | None, expected: dict[str, object]
) -> None:
    session = await harness.initialize(caller)
    headers = {"Sandbox-Science-Session": label} if label is not None else None
    params = {"name": "crawl", "arguments": {}}
    await harness.call(session, "tools/call", params, caller=caller, headers=headers)
    for record in (last_call(harness), harness.sink.kinds("escalation")[-1]):
        attribution = record["attribution"]
        assert {key: attribution[key] for key in expected} == expected
        assert attribution["project"] == "crypto"
    containers = {"shared": "ss-crypto-default", "dedicated": "ss-crypto-s42"}
    assert last_call(harness)["attribution"]["container"] == containers.get(caller)


async def test_a_malformed_session_label_is_refused(harness: Harness) -> None:
    session = await harness.initialize()
    answer = await harness.call(
        session, "tools/list", headers={"Sandbox-Science-Session": "not a session id"}
    )
    assert (answer.status, reason(answer)) == (400, "request_unclassifiable")


# --- Decisions --------------------------------------------------------------------------------


async def test_an_allowed_tool_is_forwarded_with_credentials_in_headers_only(
    harness: Harness,
) -> None:
    session = await harness.initialize()
    arguments = {"query": "lattice sieving", "limit": 5}
    answer = await harness.tool(session, "search", arguments)
    assert answer.status == 200
    assert answer.body == {
        "jsonrpc": "2.0",
        "id": 7,
        "result": {"content": [{"type": "text", "text": "search: ok"}], "isError": False},
    }
    [call] = harness.fake.messages("tools/call")
    assert call["params"] == {"name": "search", "arguments": arguments}
    for seen in harness.fake.seen:
        assert seen.headers["authorization"] == f"Bearer {PLACEHOLDER}"
        assert b"openshell:resolve" not in seen.body.lower()
    record = last_call(harness)
    assert record["decision"] == {
        "action": "allow",
        "layer": "policy",
        "rule": "mcp.servers.papers.tools.allow[search]",
    }
    assert record["outcome"] == "forwarded"
    assert record["sizes"]["params"] == len(json.dumps(call["params"], separators=(",", ":")))
    assert "lattice sieving" not in repr(harness.sink.events)


async def test_metadata_is_not_forwarded(harness: Harness) -> None:
    session = await harness.initialize()
    params = {"name": "search", "arguments": {"q": 1}, "_meta": {"progressToken": "abc"}}
    await harness.call(session, "tools/call", params)
    assert harness.fake.messages("tools/call")[0]["params"] == {
        "name": "search",
        "arguments": {"q": 1},
    }


async def test_a_denied_tool_never_reaches_the_server(harness: Harness) -> None:
    session = await harness.initialize()
    answer = await harness.tool(session, "admin_reset")
    assert answer.status == 200
    assert answer.body["id"] == 7
    error = answer.body["error"]
    assert error["code"] == REFUSED
    assert error["data"] == {"reason": "policy_denied"}
    assert "admin_" not in error["message"]  # the rule that matched stays out of the answer
    assert harness.fake.tool_calls() == []
    record = last_call(harness)
    assert (record["decision"]["action"], record["outcome"]) == ("deny", "refused")
    assert record["reason_code"] == "policy_denied"


async def test_an_escalated_tool_is_held_and_recorded_as_pending(harness: Harness) -> None:
    session = await harness.initialize()
    answer = await harness.tool(session, "crawl", {"url": "https://example.org/secret-ish"})
    assert answer.body["error"]["code"] == REFUSED
    assert reason(answer) == "approval_required"
    assert harness.fake.tool_calls() == []
    [escalation] = harness.sink.kinds("escalation")
    assert escalation["server"] == "papers"
    assert (escalation["method"], escalation["tool"]) == ("tools/call", "crawl")
    assert escalation["rule"] == "mcp.servers.papers.tools.escalate[crawl]"
    assert escalation["escalation"] == {"timeout_seconds": 1800, "on_timeout": "deny"}
    assert escalation["attribution"]["level"] == "orchestrator"
    assert escalation["mcp_session"] == last_call(harness)["mcp_session"]
    assert "secret-ish" not in repr(harness.sink.events)


async def test_an_escalation_that_cannot_be_recorded_is_not_left_pending(
    harness: Harness,
) -> None:
    # There is no request to approve, so the caller must not wait for one.
    session = await harness.initialize()
    harness.sink.fail = True
    answer = await harness.tool(session, "crawl")
    assert answer.body["error"]["code"] == REFUSED
    assert reason(answer) == "audit_unavailable"
    assert harness.fake.tool_calls() == []


async def test_an_alert_proceeds_once_recorded(harness: Harness) -> None:
    harness.bind(harness.policy(default="alert"))
    session = await harness.initialize()
    answer = await harness.tool(session, "misc")  # no rule names it
    assert answer.body["result"]["content"][0]["text"] == "misc: ok"
    [alert] = harness.sink.kinds("alert")
    assert alert["tool"] == "misc"
    assert "defaults.action is alert" in alert["reason"]
    assert harness.fake.tool_calls() == ["misc"]


async def test_an_alert_that_cannot_be_recorded_is_refused(harness: Harness) -> None:
    harness.bind(harness.policy(default="alert"))
    session = await harness.initialize()
    harness.sink.fail = True
    answer = await harness.tool(session, "misc")
    assert answer.body["error"]["code"] == REFUSED
    assert reason(answer) == "audit_unavailable"
    assert harness.fake.tool_calls() == []


async def test_methods_the_policy_does_not_cover_are_refused(harness: Harness) -> None:
    session = await harness.initialize()
    for method in ("sampling/createMessage", "elicitation/create", "roots/list", "tasks/get"):
        answer = await harness.call(session, method, {})
        assert reason(answer) == "policy_denied", method
    upstream = {message.get("method") for message in harness.fake.messages()}
    assert upstream == {"initialize", "notifications/initialized"}


async def test_a_refused_notification_gets_an_http_error(harness: Harness) -> None:
    session = await harness.initialize()
    notification = {"jsonrpc": "2.0", "method": "notifications/elicitation/complete"}
    answer = await harness.post(notification, session=session)
    assert answer.status == 403
    assert answer.body["id"] is None
    assert reason(answer) == "policy_denied"
    assert len(harness.fake.seen) == 2


async def test_task_augmented_calls_are_refused(harness: Harness) -> None:
    session = await harness.initialize()
    params = {"name": "search", "arguments": {}, "task": {"ttl": 60000}}
    answer = await harness.call(session, "tools/call", params)
    assert reason(answer) == "request_unclassifiable"
    assert harness.fake.tool_calls() == []


# --- Tool names -------------------------------------------------------------------------------


async def test_a_tool_the_server_does_not_list_is_refused(harness: Harness) -> None:
    session = await harness.initialize()
    answer = await harness.tool(session, "unlisted")  # the policy allows it by name
    assert answer.body["error"]["code"] == REFUSED
    assert reason(answer) == "tool_not_advertised"
    assert harness.fake.tool_calls() == []
    assert len(harness.fake.messages("tools/list")) == 1  # the proxy's own listing


async def test_a_variant_spelling_of_a_listed_tool_is_refused(harness: Harness) -> None:
    # ADMIN_search matches the allow pattern *_search but not the deny pattern admin_*.
    # A server that folds case would run admin_search, which the policy denies.
    session = await harness.initialize()
    for name in ("ADMIN_search", "Admin_search", "notes_search​"):
        answer = await harness.tool(session, name)
        assert reason(answer) == "tool_not_advertised", name
    assert reason(await harness.tool(session, "admin_search")) == "policy_denied"
    assert "result" in (await harness.tool(session, "notes_search")).body
    assert harness.fake.tool_calls() == ["notes_search"]


async def test_tool_names_follow_the_latest_complete_listing(harness: Harness) -> None:
    fake = harness.fake
    fake.page_size = 3
    session = await harness.initialize()
    # notes_search is on the third page: the proxy reads every page.
    assert "result" in (await harness.tool(session, "notes_search")).body
    assert len(fake.messages("tools/list")) == 4
    fake.tools.remove("notes_search")
    # A listing replaces the names only once its last page has arrived.
    first = await harness.call(session, "tools/list")
    cursor = first.body["result"]["nextCursor"]
    assert "result" in (await harness.tool(session, "notes_search")).body
    while cursor is not None:
        page = await harness.call(session, "tools/list", {"cursor": cursor})
        cursor = page.body["result"].get("nextCursor")
    assert reason(await harness.tool(session, "notes_search")) == "tool_not_advertised"


async def test_a_list_change_makes_the_proxy_list_again(harness: Harness) -> None:
    fake = harness.fake
    session = await harness.initialize()
    assert reason(await harness.tool(session, "unlisted")) == "tool_not_advertised"
    fake.tools.append("unlisted")
    assert reason(await harness.tool(session, "unlisted")) == "tool_not_advertised"
    # The stream tool's answer carries notifications/tools/list_changed.
    assert "result" in (await harness.tool(session, "stream")).body
    assert "result" in (await harness.tool(session, "unlisted")).body
    assert len(fake.messages("tools/list")) == 2


# --- Sizes and credentials ------------------------------------------------------------------


async def test_parameters_over_the_limit_are_refused(harness: Harness) -> None:
    session = await harness.initialize()
    answer = await harness.tool(session, "search", {"query": "x" * 1100})  # the policy: 1Ki
    assert reason(answer) == "arguments_too_large"
    assert last_call(harness)["sizes"]["params"] > 1024
    assert harness.fake.tool_calls() == []
    assert "result" in (await harness.tool(session, "search", {"query": "x" * 900})).body


async def test_answers_over_the_limit_are_refused(harness: Harness) -> None:
    session = await harness.initialize()
    answer = await harness.tool(session, "large")  # 100 kB; the policy allows 64 KiB
    assert answer.body["error"]["code"] == UPSTREAM_FAILED
    assert reason(answer) == "result_too_large"
    assert last_call(harness)["outcome"] == "failed"


async def test_answers_without_a_length_are_counted_as_they_arrive(harness: Harness) -> None:
    text = "x" * 100_000
    harness.fake.answer("broken", lambda rid: _response(rid, result={"text": text}), chunked=True)
    session = await harness.initialize()
    assert reason(await harness.tool(session, "broken")) == "result_too_large"


async def test_streamed_answers_over_the_limit_are_refused(harness: Harness) -> None:
    def body(request_id: object) -> bytes:
        note = {"jsonrpc": "2.0", "method": "notifications/message", "params": {"data": "x" * 1000}}
        final = {"jsonrpc": "2.0", "id": request_id, "result": {"content": []}}
        events = [note] * 80 + [final]
        return b"".join(b"data: " + json.dumps(event).encode() + b"\n\n" for event in events)

    harness.fake.answer("broken", body, content_type="text/event-stream")
    session = await harness.initialize()
    answer = await harness.tool(session, "broken")
    assert reason(answer) == "result_too_large"


async def test_a_placeholder_in_the_parameters_is_refused(harness: Harness) -> None:
    session = await harness.initialize()
    for value in ("openshell:resolve:env:v1_GITHUB_TOKEN", "OpenShell:Resolve:env:x"):
        answer = await harness.tool(session, "search", {"query": value})
        assert reason(answer) == "credential_in_body"
    # Refused before it is held for approval: a pending request for it would be useless.
    answer = await harness.tool(session, "crawl", {"url": "openshell:resolve:env:v1_X"})
    assert reason(answer) == "credential_in_body"
    assert harness.sink.kinds("escalation") == []
    assert harness.fake.tool_calls() == []


@pytest.mark.parametrize("value", [None, "test-secret-raw-token", "openshell:resolve:env:"])
async def test_a_server_is_not_called_without_a_placeholder(
    harness: Harness, value: str | None
) -> None:
    if value is None:
        del harness.environ["PAPERS_TOKEN"]
    else:
        harness.environ["PAPERS_TOKEN"] = value
    answer = await harness.post(initialize_message())
    assert answer.body["error"]["code"] == REFUSED
    assert reason(answer) == "credential_unavailable"
    assert harness.fake.seen == []


async def test_header_credentials_carry_placeholders(harness: Harness) -> None:
    auth = (
        '{mode: headers, headers: {X-Api-Key: "${provider:papers-key.API_KEY}", '
        "X-Account: research}}"
    )
    harness.bind(harness.policy(auth=auth))
    session = await harness.initialize()
    await harness.tool(session, "search")
    for seen in harness.fake.seen:
        assert seen.headers["x-api-key"] == "openshell:resolve:env:v1_PAPERS_API_KEY"
        assert seen.headers["x-account"] == "research"
        assert "authorization" not in seen.headers


async def test_the_servers_addresses_are_checked_before_connecting(harness: Harness) -> None:
    harness.address_check = address_check(HardFloor(), PoolCeiling.unrestricted())
    answer = await harness.post(initialize_message())  # the fake server is on 127.0.0.1
    assert answer.body["error"]["code"] == REFUSED
    assert reason(answer) == "destination_forbidden"
    assert harness.fake.seen == []


# --- Malformed traffic ----------------------------------------------------------------------


async def test_batches_are_refused(harness: Harness) -> None:
    session = await harness.initialize()
    batch = [
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "search"}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "image"}},
    ]
    answer = await harness.post(batch, session=session)
    assert answer.status == 400
    assert answer.body["error"]["code"] == -32600
    assert reason(answer) == "request_unclassifiable"
    assert harness.fake.tool_calls() == []


@pytest.mark.parametrize(
    ("raw", "code"),
    [
        (b"{not json", -32700),
        (b'{"jsonrpc":"2.0","id":1,"id":2,"method":"ping"}', -32700),
        (b'{"jsonrpc":"2.0","id":1,"method":"ping","extra":1}', -32600),
        (b'{"jsonrpc":"2.0","id":true,"method":"ping"}', -32600),
        (b'{"jsonrpc":"2.0","id":1,"result":{}}', -32600),
        (b'{"jsonrpc":"2.0","id":1,"method":"notifications/cancelled"}', -32600),
        (b'{"jsonrpc":"2.0","method":"ping"}', -32600),
        (b'{"jsonrpc":"2.0","id":1,"method":"ping","params":[1]}', -32600),
        (b'{"jsonrpc":"2.0","id":1,"method":"ping","params":{"x":NaN}}', -32700),
    ],
)
async def test_malformed_requests_are_refused(harness: Harness, raw: bytes, code: int) -> None:
    session = await harness.initialize()
    answer = await harness.post(None, raw=raw, session=session)
    assert answer.status == 400
    assert answer.body["error"]["code"] == code
    assert len(harness.fake.seen) == 2  # initialize and notifications/initialized


async def test_requests_must_be_json(harness: Harness) -> None:
    answer = await harness.post(initialize_message(), headers={"Content-Type": "text/plain"})
    assert answer.status == 415
    answer = await harness.post(initialize_message(), headers={"Accept": "text/html"})
    assert answer.status == 406
    answer = await harness.post(None, raw=b"[" * 70_000)  # over the 64 KiB body limit
    assert (answer.status, reason(answer)) == (413, "request_too_large")
    assert harness.fake.seen == []


def _response(request_id: object, **fields: object) -> bytes:
    return json.dumps({"jsonrpc": "2.0", "id": request_id, **fields}).encode()


FENCE_REFUSAL = json.dumps(
    {
        "error": "policy_denied",
        "policy": "mcp-lab-echo",
        "rule": "POST /mcp",
        "detail": "MCP tool call has no matching allow rule; ask the policy owner to review "
        "rules and tool selectors",
        "layer": "l7",
        "protocol": "rest",
        "method": "POST",
        "path": "/mcp",
        "next_steps": [],
    }
).encode()

MALFORMED: dict[str, tuple[Callable[[object], bytes], dict[str, Any], str]] = {
    "not json": (lambda _: b"{oops", {}, "upstream_error"),
    "another id": (lambda _: _response(999_999, result={}), {}, "upstream_error"),
    "no result": (lambda rid: _response(rid), {}, "upstream_error"),
    "a list result": (lambda rid: _response(rid, result=[1]), {}, "upstream_error"),
    "duplicate keys": (
        lambda rid: b'{"jsonrpc":"2.0","id":%s,"result":{},"result":{"a":1}}' % str(rid).encode(),
        {},
        "upstream_error",
    ),
    "a request": (
        lambda rid: _response(rid, method="tools/call"),
        {},
        "upstream_error",
    ),
    "plain text": (
        lambda rid: _response(rid, result={}),
        {"content_type": "text/plain"},
        "upstream_error",
    ),
    "compressed": (
        lambda rid: _response(rid, result={}),
        {"headers": {"Content-Encoding": "gzip"}},
        "upstream_error",
    ),
    "a stream without the response": (
        lambda _: b'data: {"jsonrpc":"2.0","method":"notifications/progress"}\n\n',
        {"content_type": "text/event-stream"},
        "upstream_error",
    ),
    "a malformed event": (
        lambda _: b"data: {oops\n\n",
        {"content_type": "text/event-stream"},
        "upstream_error",
    ),
    "a server error": (lambda _: b"", {"status": 500}, "upstream_error"),
    "unauthorized": (lambda _: b"", {"status": 401}, "upstream_unauthorized"),
    # OpenShell's refusal at the proxy sandbox's fence, as lab 13 saw it.
    "the sandbox's fence": (lambda _: FENCE_REFUSAL, {"status": 403}, "fence_denied"),
    "a server's own 403": (lambda _: b'{"error": "forbidden"}', {"status": 403}, "upstream_error"),
    "a 403 in text": (
        lambda _: FENCE_REFUSAL,
        {"status": 403, "content_type": "text/plain"},
        "upstream_error",
    ),
    "a redirect": (
        lambda _: b"",
        {"status": 307, "headers": {"Location": "/mcp"}},
        "upstream_error",
    ),
}


@pytest.mark.parametrize("case", sorted(MALFORMED))
async def test_malformed_answers_fail_closed(harness: Harness, case: str) -> None:
    body, options, expected = MALFORMED[case]
    harness.fake.answer("broken", body, **options)
    session = await harness.initialize()
    answer = await harness.tool(session, "broken")
    assert answer.status == 200
    assert answer.body["id"] == 7
    assert answer.body["error"]["code"] == UPSTREAM_FAILED
    assert reason(answer) == expected
    assert harness.fake.tool_calls() == ["broken"]  # once: a redirect is not followed
    assert last_call(harness)["outcome"] == "failed"


async def test_a_servers_error_is_passed_on(harness: Harness) -> None:
    error = {"code": -32602, "message": "bad arguments", "data": {"field": "query"}}
    harness.fake.answer("broken", lambda rid: _response(rid, error=error))
    session = await harness.initialize()
    answer = await harness.tool(session, "broken")
    assert answer.body == {"jsonrpc": "2.0", "id": 7, "error": error}
    assert last_call(harness)["outcome"] == "server_error"


async def test_server_requests_in_a_stream_are_answered_by_the_proxy(harness: Harness) -> None:
    session = await harness.initialize()
    answer = await harness.tool(session, "stream")
    assert answer.body["result"]["content"][0]["text"] == "streamed"
    fake = harness.fake
    await eventually(lambda: len([m for m in fake.messages() if "method" not in m]) == 2)
    replies = {m["id"]: m for m in fake.messages() if "method" not in m}
    assert replies["server-2"] == {"jsonrpc": "2.0", "id": "server-2", "result": {}}
    assert replies["server-1"]["error"]["code"] == -32601
    assert last_call(harness)["stream"] == {"notifications": 2, "refused_server_requests": 1}


async def test_a_stream_may_end_its_last_line_with_a_lone_cr(harness: Harness) -> None:
    harness.fake.answer(
        "broken",
        lambda rid: b"data: " + _response(rid, result={"content": []}) + b"\r\r",
        content_type="text/event-stream",
    )
    session = await harness.initialize()
    assert (await harness.tool(session, "broken")).body["result"] == {"content": []}


async def test_a_flood_of_server_requests_is_cut_off(harness: Harness) -> None:
    def body(request_id: object) -> bytes:
        pings = [{"jsonrpc": "2.0", "id": f"s{n}", "method": "ping"} for n in range(20)]
        final = {"jsonrpc": "2.0", "id": request_id, "result": {"content": []}}
        return b"".join(b"data: " + json.dumps(m).encode() + b"\n\n" for m in [*pings, final])

    harness.fake.answer("broken", body, content_type="text/event-stream")
    session = await harness.initialize()
    assert reason(await harness.tool(session, "broken")) == "upstream_error"


async def test_cookies_from_servers_are_not_sent_back(harness: Harness) -> None:
    session = await harness.initialize()  # the fake server sets a cookie here
    await harness.tool(session, "search")
    await harness.initialize()
    assert all("cookie" not in seen.headers for seen in harness.fake.seen)


async def test_binary_content_is_withheld(harness: Harness) -> None:
    session = await harness.initialize()
    answer = await harness.tool(session, "image")
    content = answer.body["result"]["content"]
    assert content[0] == {"type": "text", "text": "a figure"}
    assert content[1]["type"] == "text"
    assert content[1]["text"].startswith("[image/png withheld by the MCP proxy")
    assert content[2]["type"] == "text"
    assert "iVBORw0KGgo=" not in json.dumps(answer.body)
    assert "JVBERi0=" not in json.dumps(answer.body)
    assert last_call(harness)["stream"] == {"withheld_binary": 2}


async def test_a_cancellation_reaches_the_server_under_its_own_id(harness: Harness) -> None:
    fake = harness.fake
    session = await harness.initialize()
    await harness.call(session, "tools/list")
    fake.gate = asyncio.Event()
    params = {"name": "search", "arguments": {}}
    pending = asyncio.create_task(harness.call(session, "tools/call", params, request_id="call-1"))
    await eventually(lambda: fake.tool_calls() == ["search"])
    upstream_id = fake.messages("tools/call")[0]["id"]
    cancelled = {
        "jsonrpc": "2.0",
        "method": "notifications/cancelled",
        "params": {"requestId": "call-1", "reason": "the agent gave up"},
    }
    assert (await harness.post(cancelled, session=session)).status == 202
    [forwarded] = fake.messages("notifications/cancelled")
    assert forwarded["params"] == {"requestId": upstream_id}  # the reason text stays here
    fake.gate.set()
    await pending
    # A cancellation for no call in flight goes nowhere.
    assert (await harness.post(cancelled, session=session)).status == 202
    assert len(fake.messages("notifications/cancelled")) == 1


# --- What reaches the server ------------------------------------------------------------------


@pytest.mark.parametrize(
    "params",
    [
        # A server whose JSON decoder matches keys without regard to case (Go's encoding/json,
        # some .NET settings) would run the tool named by the variant, which the policy never saw.
        {"name": "search", "NAME": "admin_reset", "arguments": {}},
        {"name": "search", "Name": "crawl", "arguments": {}},
        {"name": "search", "name\u0000": "admin_reset", "arguments": {}},
        {"name": "search", "Arguments": {"q": 1}},
        {"name": "search", "arguments": {}, "_META": {"progressToken": 1}},
        {"name": "search", "arguments": {}, "Task": {"ttl": 1}},
        {"name": "search", "arguments": {}, "extra": 1},
        {"name": "search", "arguments": ["not", "an", "object"]},
    ],
)
async def test_parameters_mcp_does_not_define_are_refused(
    harness: Harness, params: dict[str, object]
) -> None:
    session = await harness.initialize()
    answer = await harness.call(session, "tools/call", params)
    assert answer.body["error"]["code"] == REFUSED
    assert reason(answer) == "request_unclassifiable"
    assert harness.fake.tool_calls() == []


async def test_other_methods_keep_to_their_parameters(harness: Harness) -> None:
    session = await harness.initialize()
    for method, params in (
        ("tools/list", {"cursor": "0", "Cursor": "x"}),
        ("resources/read", {"uri": "file:///a", "URI": "file:///b"}),
        ("prompts/get", {"name": "p", "arguments": "text"}),
        ("logging/setLevel", {"level": "info", "Level": "debug"}),
        ("ping", {"anything": 1}),
    ):
        answer = await harness.call(session, method, params)
        assert reason(answer) == "request_unclassifiable", method
    assert len(harness.fake.seen) == 2
    assert "result" in (await harness.call(session, "ping", {"_meta": {"x": 1}})).body


async def test_callers_headers_never_reach_the_server(harness: Harness) -> None:
    headers = {
        "Cookie": "session=caller",
        "X-Forwarded-For": "203.0.113.9",
        "X-Api-Key": "caller-chosen",
        "Sandbox-Science-Session": "s-1",
        "MCP-Protocol-Version": "2025-11-25",
    }
    session = await harness.initialize()
    await harness.call(session, "tools/list", headers=headers)
    await harness.tool(session, "search")
    for seen in harness.fake.seen:
        assert not {"cookie", "x-forwarded-for", "x-api-key", "sandbox-science-session"} & set(
            seen.headers
        )
        assert seen.headers["authorization"] == f"Bearer {PLACEHOLDER}"


async def test_a_moved_server_ends_its_sessions(harness: Harness) -> None:
    session = await harness.initialize()
    harness.bind(harness.policy(port=harness.fake.port + 1))
    answer = await harness.tool(session, "search")
    assert (answer.status, reason(answer)) == (404, "session_unknown")
    seen = len(harness.fake.seen)
    cancelled = {
        "jsonrpc": "2.0",
        "method": "notifications/cancelled",
        "params": {"requestId": 7},
    }
    assert (await harness.post(cancelled, session=session)).status == 404
    assert len(harness.fake.seen) == seen


async def test_sessions_after_a_pin_to_a_revision_the_proxy_cannot_speak(
    harness: Harness,
) -> None:
    session = await harness.initialize()
    harness.bind(harness.policy(revision="2026-07-28"))
    answer = await harness.tool(session, "search")
    assert reason(answer) == "protocol_unsupported"
    assert harness.fake.tool_calls() == []


@pytest.mark.parametrize(
    "session_id",
    ["openshell:resolve:env:v1_OTHER_TOKEN", "x" * 2049, "has space"],
)
async def test_a_servers_session_id_is_checked(harness: Harness, session_id: str) -> None:
    harness.fake.session_id = session_id
    answer = await harness.post(initialize_message())
    assert answer.body["error"]["code"] == UPSTREAM_FAILED
    assert reason(answer) == "upstream_error"
    assert "Mcp-Session-Id" not in answer.headers
    assert len(harness.fake.seen) == 1  # no notifications/initialized with that id


async def test_a_long_session_id_from_a_server_is_accepted(harness: Harness) -> None:
    harness.fake.session_id = "jwt." + "x" * 1500
    session = await harness.initialize()
    assert "result" in (await harness.tool(session, "search")).body


async def test_a_server_may_not_use_the_proxys_error_codes(harness: Harness) -> None:
    for code in (REFUSED, UPSTREAM_FAILED):
        error = {
            "code": code,
            "message": "approval_required",
            "data": {"reason": "approval_required"},
        }
        harness.fake.answer("broken", lambda rid, error=error: _response(rid, error=error))
        session = await harness.initialize()
        answer = await harness.tool(session, "broken")
        assert answer.body["error"]["code"] == UPSTREAM_FAILED
        assert reason(answer) == "upstream_error"


async def test_a_failed_initialize_leaves_no_session_behind(harness: Harness) -> None:
    harness.fake.initialize_extra = {"instructions": "\ud800"}  # no UTF-8 encoding
    for _ in range(10):  # more than the harness's 8 sessions
        answer = await harness.post(initialize_message())
        assert reason(answer) == "upstream_error"
    harness.fake.initialize_extra = {}
    assert await harness.initialize()


async def test_a_caller_cannot_take_every_session(harness: Harness) -> None:
    worker = [await harness.initialize("shared") for _ in range(6)]  # 4 per caller
    for session in worker[:2]:
        answer = await harness.tool(session, "search", caller="shared")
        assert reason(answer) == "session_unknown"
    assert "result" in (await harness.tool(worker[-1], "search", caller="shared")).body
    orchestrator = await harness.initialize()
    assert "result" in (await harness.tool(orchestrator, "search")).body


async def test_sessions_are_bound_to_their_server_and_caller_over_http(
    harness: Harness,
) -> None:
    session = await harness.initialize()
    answer = await harness.request("DELETE", session=session, caller="shared")
    assert answer.status == 404
    ping = {"jsonrpc": "2.0", "id": 3, "method": "ping"}
    answer = await harness.post(ping, session=session, server="other")
    assert (answer.status, reason(answer)) == (404, "session_unknown")
    assert "result" in (await harness.call(session, "ping")).body


async def test_an_empty_cursor_ends_a_listing(harness: Harness) -> None:
    harness.fake.last_cursor = ""
    session = await harness.initialize()
    assert "result" in (await harness.tool(session, "search")).body
    assert len(harness.fake.messages("tools/list")) == 1


async def test_a_server_may_batch_messages_in_one_event(harness: Harness) -> None:
    def body(request_id: object) -> bytes:
        batch = [
            {"jsonrpc": "2.0", "method": "notifications/progress", "params": {"progress": 1}},
            {"jsonrpc": "2.0", "id": request_id, "result": {"content": []}},
        ]
        return b"data: " + json.dumps(batch).encode() + b"\n\n"

    harness.fake.answer("broken", body, content_type="text/event-stream")
    session = await harness.initialize()
    assert (await harness.tool(session, "broken")).body["result"] == {"content": []}
    assert last_call(harness)["stream"] == {"notifications": 1}


async def test_numbers_too_large_to_represent_are_refused(harness: Harness) -> None:
    harness.fake.answer(
        "broken", lambda rid: b'{"jsonrpc":"2.0","id":%s,"result":{"x":1e400}}' % str(rid).encode()
    )
    session = await harness.initialize()
    assert reason(await harness.tool(session, "broken")) == "upstream_error"


async def test_caller_text_cannot_forge_log_lines(
    harness: Harness, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.INFO, logger="sandbox_science.mcp_proxy"):
        answer = await harness.post(initialize_message(), server="x%0AFORGED%20line")
    assert answer.status == 200
    assert reason(answer) == "request_unclassifiable"
    assert caplog.records
    assert all("\nFORGED" not in record.getMessage() for record in caplog.records)


# Permission requests (D76).


def request_of(answer: Answer) -> int:
    request = answer.body["error"]["data"].get("request")
    assert isinstance(request, int), answer.body
    return request


def answer_request(harness: Harness, seq: int, *, approve: bool, scope: str | None = None) -> None:
    """Approve or deny a request as the command line does, into the harness's store."""
    approvals = Approvals()
    approvals.load(harness.sink.records(0))
    kind, payload = prepare_answer(
        approvals, seq, approve=approve, scope=scope, hours=1, now=harness.sink.now()
    )
    harness.sink.append(kind, payload)


async def test_a_retried_escalation_waits_on_the_same_request(harness: Harness) -> None:
    session = await harness.initialize()
    first = await harness.tool(session, "crawl", {"url": "https://example.org/a"})
    assert reason(first) == "approval_required"
    seq = request_of(first)
    again = await harness.tool(session, "crawl", {"url": "https://example.org/a"})
    assert (reason(again), request_of(again)) == ("approval_required", seq)
    assert len(harness.sink.kinds("escalation")) == 1
    [escalation] = harness.sink.kinds("escalation")
    target = escalation["target"]
    assert (target["kind"], target["server"], target["tool"]) == ("mcp", "papers", "crawl")
    assert "example.org" not in json.dumps(target)  # the arguments only as a digest


async def test_an_approved_call_goes_through_and_only_that_call(harness: Harness) -> None:
    session = await harness.initialize()
    seq = request_of(await harness.tool(session, "crawl", {"url": "https://example.org/a"}))
    answer_request(harness, seq, approve=True)
    approved = await harness.tool(session, "crawl", {"url": "https://example.org/a"})
    assert approved.body["result"]["content"][0]["text"].startswith("crawl")
    assert harness.fake.tool_calls() == ["crawl"]
    assert harness.sink.kinds("approved")[0]["request_seq"] == seq
    other = await harness.tool(session, "crawl", {"url": "https://example.org/b"})
    assert reason(other) == "approval_required"
    assert request_of(other) != seq


async def test_a_tool_wide_approval_covers_other_arguments(harness: Harness) -> None:
    session = await harness.initialize()
    seq = request_of(await harness.tool(session, "crawl", {"url": "https://example.org/a"}))
    answer_request(harness, seq, approve=True, scope="tool")
    other = await harness.tool(session, "crawl", {"url": "https://example.org/b"})
    assert "result" in other.body, other.body


async def test_a_denied_call_is_refused_with_its_request(harness: Harness) -> None:
    session = await harness.initialize()
    seq = request_of(await harness.tool(session, "crawl"))
    answer_request(harness, seq, approve=False)
    denied = await harness.tool(session, "crawl")
    assert (reason(denied), request_of(denied)) == ("approval_denied", seq)
    assert harness.fake.tool_calls() == []


async def test_an_unanswered_call_times_out(harness: Harness) -> None:
    session = await harness.initialize()
    seq = request_of(await harness.tool(session, "crawl"))
    later = harness.sink.now() + timedelta(minutes=31)
    harness.sink.now = lambda: later
    late = await harness.tool(session, "crawl")
    assert (reason(late), request_of(late)) == ("escalation_timed_out", seq)


async def test_requests_belong_to_their_caller(harness: Harness) -> None:
    session = await harness.initialize()
    seq = request_of(await harness.tool(session, "crawl"))
    answer_request(harness, seq, approve=True, scope="tool")
    worker = await harness.initialize(caller="shared")
    theirs = await harness.tool(worker, "crawl", caller="shared")
    assert reason(theirs) == "approval_required"
    assert request_of(theirs) != seq


async def test_an_mcp_request_takes_mcp_scopes(harness: Harness) -> None:
    session = await harness.initialize()
    seq = request_of(await harness.tool(session, "crawl"))
    approvals = Approvals()
    approvals.load(harness.sink.records(0))
    with pytest.raises(ValueError, match="call or tool"):
        prepare_answer(approvals, seq, approve=True, scope="host", hours=1, now=harness.sink.now())


async def test_the_command_line_answers_requests_in_the_store(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "events.db"
    key = RequestKey.mcp(
        "crypto", "orchestrator", "sha256:" + "0" * 64, "papers", "tools/call", "crawl", "d" * 64
    )
    with EventStore(path) as events:
        events.append(
            "escalation",
            "mcp-proxy",
            {
                "target": key.payload(),
                "escalation": {"timeout_seconds": 1800, "on_timeout": "deny"},
            },
        )
    assert _pending(path) == 0
    assert "papers tools/call crawl" in capsys.readouterr().out
    assert _answer(path, approve=True, seq=1, scope="tool", hours=2) == 0
    assert _answer(path, approve=True, seq=9, scope=None, hours=2) == 1
    assert _pending(path) == 0
    assert "no permission requests are waiting" in capsys.readouterr().out
