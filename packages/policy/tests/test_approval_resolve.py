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
