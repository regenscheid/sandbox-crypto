# SPDX-License-Identifier: MIT
"""``resolve``: the one state machine the policy middleware and the MCP proxy share."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sandbox_science.policy.approvals import (
    APPROVAL,
    DENIAL,
    ESCALATION,
    Approvals,
    Approved,
    Denied,
    RequestKey,
    TimedOut,
    Unrecorded,
    Waiting,
)

NOW = datetime(2026, 10, 14, 12, 0, tzinfo=UTC)
DIGEST = "sha256:" + "a" * 64
KEY = RequestKey.http("sb", DIGEST, "POST", "example.org", 443, "/items")
OTHER_PATH = RequestKey.http("sb", DIGEST, "POST", "example.org", 443, "/other")


@dataclass(frozen=True)
class Record:
    seq: int
    time: str
    kind: str
    payload: dict[str, object]

    def data(self) -> dict[str, object]:
        return json.loads(json.dumps(self.payload))


def request(seq: int, on_timeout: str = "deny", key: RequestKey = KEY) -> Record:
    payload: dict[str, object] = {
        "target": key.payload(),
        "escalation": {"timeout_seconds": 600, "on_timeout": on_timeout},
    }
    return Record(seq, NOW.isoformat(), ESCALATION, payload)


def answer(
    seq: int, request_seq: int, kind: str, scope: str = "request", **extra: object
) -> Record:
    payload = {"request_seq": request_seq, "scope": scope, "target": KEY.payload(), **extra}
    return Record(seq, NOW.isoformat(), kind, payload)


def test_without_a_record_the_request_is_unrecorded() -> None:
    assert Approvals().resolve(KEY, NOW) == Unrecorded()


def test_a_recorded_request_waits_then_times_out_by_its_terms() -> None:
    approvals = Approvals()
    approvals.load([request(1)])
    waiting = approvals.resolve(KEY, NOW + timedelta(seconds=599))
    assert isinstance(waiting, Waiting)
    assert waiting.request.seq == 1
    timed_out = approvals.resolve(KEY, NOW + timedelta(seconds=600))
    assert isinstance(timed_out, TimedOut)
    assert timed_out.request.on_timeout == "deny"
    alert = Approvals()
    alert.load([request(1, "alert")])
    resolved = alert.resolve(KEY, NOW + timedelta(hours=1))
    assert isinstance(resolved, TimedOut)
    assert resolved.request.on_timeout == "alert"


def test_the_newest_live_answer_decides() -> None:
    approvals = Approvals()
    approvals.load([request(1), answer(2, 1, APPROVAL), answer(3, 1, DENIAL)])
    denied = approvals.resolve(KEY, NOW)
    assert isinstance(denied, Denied)
    assert denied.answer.seq == 3
    approvals.load([answer(4, 1, APPROVAL, expires=(NOW + timedelta(hours=1)).isoformat())])
    approved = approvals.resolve(KEY, NOW)
    assert isinstance(approved, Approved)
    assert approved.answer.seq == 4
    # Once it expires, the earlier denial decides again.
    assert isinstance(approvals.resolve(KEY, NOW + timedelta(hours=2)), Denied)


def test_an_answer_covers_only_what_its_scope_covers() -> None:
    approvals = Approvals()
    approvals.load([request(1), answer(2, 1, APPROVAL)])
    assert isinstance(approvals.resolve(KEY, NOW), Approved)
    assert approvals.resolve(OTHER_PATH, NOW) == Unrecorded()
    approvals.load([answer(3, 1, APPROVAL, scope="host")])
    assert isinstance(approvals.resolve(OTHER_PATH, NOW), Approved)


def test_a_server_answer_covers_every_tool_of_the_server_and_nothing_else() -> None:
    from sandbox_science.policy.approvals.keys import RequestKey, check_scope

    def key(
        tool: str, arguments: str = "a" * 64, server: str = "papers", caller: str = "orch"
    ) -> RequestKey:
        return RequestKey.mcp("p1", caller, "sha256:p", server, "tools/call", tool, arguments)

    asked = key("search")
    assert asked.covers(key("fetch", "b" * 64), "server")
    assert not asked.covers(key("search", server="web"), "server")  # another server
    assert not asked.covers(key("search", caller="other"), "server")  # another caller
    assert not asked.covers(
        RequestKey.mcp("p2", "orch", "sha256:p", "papers", "tools/call", "x", "a" * 64), "server"
    )  # another project
    assert not asked.covers(
        RequestKey.mcp("p1", "orch", "sha256:q", "papers", "tools/call", "x", "a" * 64), "server"
    )  # another policy revision
    assert asked.covers(key("search", "b" * 64), "tool")
    assert not asked.covers(key("fetch"), "tool")
    assert check_scope("mcp", "server") is None
    assert check_scope("http", "server") is not None


MCP_KEY = RequestKey.mcp("p1", "orch", DIGEST, "papers", "tools/call", "fetch", "a" * 64)


def mcp_call(**changed: str) -> dict[str, str | int]:
    """The fields a caller that relays a refusal knows: all but the caller and revision."""
    fields: dict[str, str | int] = {
        name: value
        for name, value in MCP_KEY.fields().items()
        if name not in ("caller", "policy_digest")
    }
    return {**fields, **changed}


def mcp_answer(seq: int, request_seq: int, kind: str, scope: str = "call") -> Record:
    payload = {"request_seq": request_seq, "scope": scope, "target": MCP_KEY.payload()}
    return Record(seq, NOW.isoformat(), kind, payload)


def test_a_waiting_request_is_confirmed_only_for_its_own_call() -> None:
    approvals = Approvals()
    approvals.load([request(1, key=MCP_KEY)])
    assert approvals.confirms(1, "approval_required", mcp_call(), NOW)
    assert not approvals.confirms(1, "approval_required", mcp_call(tool="search"), NOW)
    assert not approvals.confirms(1, "approval_required", mcp_call(arguments="b" * 64), NOW)
    assert not approvals.confirms(1, "approval_required", mcp_call(project_id="p2"), NOW)
    assert not approvals.confirms(1, "approval_required", mcp_call(server="web"), NOW)
    assert not approvals.confirms(2, "approval_required", mcp_call(), NOW)  # no such request
    assert not approvals.confirms(1, "approval_required", {"host": "example.org"}, NOW)
    assert not approvals.confirms(1, "approval_denied", mcp_call(), NOW)  # nobody denied it
    assert not approvals.confirms(1, "escalation_timed_out", mcp_call(), NOW)
    assert not approvals.confirms(1, "policy_denied", mcp_call(), NOW)


def test_an_answered_request_is_confirmed_as_it_stands() -> None:
    approvals = Approvals()
    approvals.load([request(1, key=MCP_KEY), mcp_answer(2, 1, APPROVAL)])
    # Approved after the refusal: the next try proceeds, so the wait it starts is short.
    assert approvals.confirms(1, "approval_required", mcp_call(), NOW)
    approvals.load([mcp_answer(3, 1, DENIAL)])
    assert not approvals.confirms(1, "approval_required", mcp_call(), NOW)
    assert approvals.confirms(1, "approval_denied", mcp_call(), NOW)
    assert not approvals.confirms(1, "approval_denied", mcp_call(arguments="b" * 64), NOW)


def test_a_denial_by_tool_is_confirmed_for_the_tool_with_other_arguments() -> None:
    approvals = Approvals()
    approvals.load([request(1, key=MCP_KEY), mcp_answer(2, 1, DENIAL, scope="tool")])
    assert approvals.confirms(1, "approval_denied", mcp_call(arguments="b" * 64), NOW)
    assert not approvals.confirms(1, "approval_denied", mcp_call(tool="search"), NOW)


def test_a_timed_out_request_is_confirmed_as_timed_out_only() -> None:
    approvals = Approvals()
    approvals.load([request(1, key=MCP_KEY)])
    later = NOW + timedelta(minutes=11)
    assert approvals.confirms(1, "escalation_timed_out", mcp_call(), later)
    assert not approvals.confirms(1, "escalation_timed_out", mcp_call(tool="search"), later)
    assert not approvals.confirms(1, "approval_required", mcp_call(), later)
