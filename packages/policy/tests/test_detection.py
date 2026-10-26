# SPDX-License-Identifier: MIT
"""Detect-and-hold rules (D95, D103): each fires past its threshold and not at it, reports
a near miss half way, and keeps bounded counts; holds and drifts fold from records."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest

from sandbox_science.policy import PolicyLoadError, parse_policy
from sandbox_science.policy.approvals import Approvals
from sandbox_science.policy.approvals.commands import prepare_answer
from sandbox_science.policy.detection import (
    DAILY_CEILING,
    DENIAL_STREAK,
    HOST_FAILURES,
    HOST_RATE,
    HOST_SPREAD,
    SAME_URL,
    SEQUENTIAL_IDS,
    Call,
    Detector,
    DriftLedger,
    Finding,
    Holds,
    Subject,
    UnreadListings,
    action_like,
    pin_digest,
)
from sandbox_science.policy.model import Detection

T0 = datetime(2026, 10, 24, 12, 0, tzinfo=UTC)
CODE = Subject("sbx-1")
AGENT = Subject("sbx-agent", "proj", "main")


def holds(found: list[Finding], rule: str) -> bool:
    return any(finding.rule == rule and finding.hold for finding in found)


def calls(
    detector: Detector,
    settings: Detection,
    count: int,
    make: Callable[[int], Call],
    *,
    every: float = 0.0,
    subject: Subject = CODE,
) -> list[list[Finding]]:
    """``count`` calls, the ``i``-th made by ``make(i)``, ``every`` seconds apart."""
    return [
        detector.observe(settings, subject, "p", make(i), T0 + timedelta(seconds=i * every))
        for i in range(count)
    ]


def test_the_defaults_are_generous() -> None:
    defaults = Detection()
    assert defaults.enabled
    assert defaults.action_urls
    assert defaults.host_rate_per_second == 5
    assert defaults.host_rate_window == timedelta(minutes=1)
    assert (defaults.distinct_hosts, defaults.distinct_hosts_window) == (60, timedelta(minutes=10))
    assert (defaults.same_url, defaults.sequential_ids, defaults.failures_per_host) == (100, 50, 30)
    assert (defaults.denials, defaults.denials_window) == (20, timedelta(minutes=5))
    assert defaults.daily_calls == 20_000


def test_a_policy_sets_the_thresholds_per_project() -> None:
    text = (
        "apiVersion: sandbox-science/v0alpha1\nkind: ProjectPolicy\nmetadata: {name: p}\n"
        "detection: {host_rate_per_second: 2, host_rate_window: 30s, daily_calls: 500}\n"
    )
    detection = parse_policy(text).policy.detection
    assert detection.host_rate_per_second == 2
    assert detection.host_rate_window == timedelta(seconds=30)
    assert detection.daily_calls == 500
    with pytest.raises(PolicyLoadError, match="detection"):
        parse_policy(text.replace("daily_calls: 500", "daily_calls: 0"))


def test_a_sustained_burst_to_one_host_holds_past_its_threshold() -> None:
    settings = Detection(host_rate_per_second=2, host_rate_window=timedelta(seconds=10))
    detector = Detector()
    found = calls(detector, settings, 21, lambda i: Call("a.org", "GET", f"/{i}"), every=0.4)
    assert not any(holds(each, HOST_RATE) for each in found[:20])
    assert holds(found[20], HOST_RATE)
    assert [f.count for f in found[10] if f.rule == HOST_RATE] == [11]  # the near miss


def test_calls_spread_over_the_window_do_not_hold() -> None:
    settings = Detection(host_rate_per_second=2, host_rate_window=timedelta(seconds=10))
    detector = Detector()
    found = calls(detector, settings, 100, lambda i: Call("a.org", "GET", f"/{i}"), every=0.6)
    assert not any(holds(each, HOST_RATE) for each in found)


def test_many_hosts_within_minutes_hold() -> None:
    settings = Detection(distinct_hosts=10)
    detector = Detector()
    found = calls(detector, settings, 11, lambda i: Call(f"h{i}.org", "GET", "/"), every=1)
    assert not any(holds(each, HOST_SPREAD) for each in found[:10])
    assert holds(found[10], HOST_SPREAD)
    later = calls(
        Detector(), settings, 30, lambda i: Call(f"h{i}.org", "GET", "/"), every=70
    )  # one host a minute stays below
    assert not any(holds(each, HOST_SPREAD) for each in later)


def test_the_same_url_again_and_again_holds() -> None:
    settings = Detection(same_url=5)
    detector = Detector()
    found = calls(detector, settings, 6, lambda i: Call("a.org", "GET", "/status?job=1"), every=30)
    assert not any(holds(each, SAME_URL) for each in found[:5])
    assert holds(found[5], SAME_URL)
    other = detector.observe(settings, CODE, "p", Call("a.org", "GET", "/status?job=2"), T0)
    assert not holds(other, SAME_URL)


def test_sequential_ids_hold_and_a_jump_starts_a_new_run() -> None:
    settings = Detection(sequential_ids=5)
    detector = Detector()
    path = "/api/users/{}/profile"
    found = calls(
        detector,
        settings,
        6,
        lambda i: Call("a.org", "GET", path.format(100 + i), path=path.format(100 + i)),
        every=1,
    )
    assert not any(holds(each, SEQUENTIAL_IDS) for each in found[:5])
    assert holds(found[5], SEQUENTIAL_IDS)
    jumps = calls(
        Detector(), settings, 20, lambda i: Call("a.org", "GET", f"/p/{i * 7}", path=f"/p/{i * 7}")
    )
    assert not any(holds(each, SEQUENTIAL_IDS) for each in jumps)
    down = calls(
        Detector(), settings, 6, lambda i: Call("a.org", "GET", f"/p/{50 - i}", path=f"/p/{50 - i}")
    )
    assert holds(down[5], SEQUENTIAL_IDS)


def test_repeated_failures_from_one_host_hold() -> None:
    settings = Detection(failures_per_host=3)
    detector = Detector()
    found = [detector.outcome(settings, CODE, "a.org", T0, denied=False) for _ in range(4)]
    assert not any(holds(each, HOST_FAILURES) for each in found[:3])
    assert holds(found[3], HOST_FAILURES)
    assert not any(holds(each, DENIAL_STREAK) for each in found)  # errors are not refusals


def test_a_streak_of_refusals_holds() -> None:
    settings = Detection(denials=4)
    detector = Detector()
    found = [
        detector.outcome(settings, CODE, f"h{i}.org", T0 + timedelta(seconds=i), denied=True)
        for i in range(5)
    ]
    assert not any(holds(each, DENIAL_STREAK) for each in found[:4])
    assert holds(found[4], DENIAL_STREAK)
    slow = Detector()
    spaced = [
        slow.outcome(settings, CODE, "h.org", T0 + timedelta(minutes=2 * i), denied=True)
        for i in range(10)
    ]
    assert not any(holds(each, DENIAL_STREAK) for each in spaced)


def test_the_daily_ceiling_holds_per_project_and_resets_each_day() -> None:
    settings = Detection(daily_calls=3, host_rate_per_second=100, distinct_hosts=100)
    detector = Detector()
    one, other = Subject("sbx-1"), Subject("sbx-2")
    found = [
        detector.observe(settings, subject, "p", Call(f"h{i}.org", "GET", "/"), T0)
        for i, subject in enumerate((one, other, one, other))
    ]
    assert not any(holds(each, DAILY_CEILING) for each in found[:3])
    assert holds(found[3], DAILY_CEILING)  # the project's count, whichever sandbox
    elsewhere = detector.observe(settings, one, "q", Call("x.org", "GET", "/"), T0)
    assert not holds(elsewhere, DAILY_CEILING)
    tomorrow = detector.observe(settings, one, "p", Call("x.org", "GET", "/"), T0 + timedelta(1))
    assert not holds(tomorrow, DAILY_CEILING)


def test_clearing_a_subject_forgets_its_counts_only() -> None:
    settings = Detection(same_url=2)
    detector = Detector()
    for subject in (CODE, AGENT):
        calls(detector, settings, 3, lambda i: Call("a.org", "GET", "/x"), subject=subject)
    detector.clear(CODE)
    assert not holds(
        detector.observe(settings, CODE, "p", Call("a.org", "GET", "/x"), T0), SAME_URL
    )
    assert holds(detector.observe(settings, AGENT, "p", Call("a.org", "GET", "/x"), T0), SAME_URL)


def test_disabled_detection_counts_nothing() -> None:
    settings = Detection(enabled=False, same_url=1)
    detector = Detector()
    found = calls(detector, settings, 5, lambda i: Call("a.org", "GET", "/x"))
    assert found == [[]] * 5
    assert detector.recent(CODE) == []


def test_the_evidence_keeps_the_latest_calls_without_their_query() -> None:
    detector = Detector()
    for i in range(8):
        call = Call("a.org", "GET", f"/x?token={i}", path="/x", label="/x?(7 bytes)")
        detector.observe(Detection(), CODE, "p", call, T0)
    recent = detector.recent(CODE)
    assert len(recent) == 5
    assert recent[-1] == {"method": "GET", "host": "a.org", "target": "/x?(7 bytes)"}


def test_counts_are_bounded_in_subjects() -> None:
    detector = Detector()
    for i in range(10_050):
        detector.observe(Detection(), Subject(f"sbx-{i}"), "p", Call("a.org", "GET", "/"), T0)
    assert detector.recent(Subject("sbx-0")) == []  # the oldest was forgotten
    assert detector.recent(Subject("sbx-10049")) != []


@pytest.mark.parametrize(
    ("path", "query"),
    [
        ("/newsletter/unsubscribe", "token=f3a9c1e07b2d4c5a8e9f0a1b2c3d4e5f"),
        ("/account/confirm/eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0In0", ""),
        ("/api/v1/items/9a8b7c6d5e4f3a2b1c0d9e8f7a6b5c4d/delete", ""),
        ("/approveRequest", "id=AbCdEf0123456789XyZw"),
        ("/verify-email", "code=Zx81Kq09Lm72Np63Rt54"),
    ],
)
def test_action_like_urls(path: str, query: str) -> None:
    assert action_like(path, query)


@pytest.mark.parametrize(
    ("path", "query"),
    [
        ("/abs/2310.12345", ""),
        ("/unsubscribe", ""),  # a word without a token
        ("/files/9a8b7c6d5e4f3a2b1c0d9e8f7a6b5c4d", ""),  # a token without a word
        ("/search", "q=delete+old+papers"),
        ("/repos/org/research/commits", "sha=main"),
    ],
)
def test_ordinary_urls_are_no_actions(path: str, query: str) -> None:
    assert not action_like(path, query)


@dataclass
class Rec:
    seq: int
    kind: str
    payload: dict[str, object]
    time: str = "2026-10-24T12:00:00+00:00"

    def data(self) -> dict[str, object]:
        return json.loads(json.dumps(self.payload))


def hold_record(seq: int, subject: Subject, rule: str = HOST_RATE) -> Rec:
    key = subject.hold_key(rule, T0)
    return Rec(
        seq,
        "escalation",
        {"target": key.payload(), "escalation": {"timeout_seconds": 60, "on_timeout": "deny"}},
    )


def answer(records: list[Rec], seq: int, approve: bool, at: int) -> Rec:
    approvals = Approvals()
    approvals.load(records)
    kind, payload = prepare_answer(approvals, seq, approve=approve, scope=None, hours=None, now=T0)
    return Rec(at, kind, payload)


def test_a_hold_is_open_until_cleared_or_stopped() -> None:
    first, second = hold_record(1, CODE), hold_record(2, AGENT, SAME_URL)
    ledger = Holds()
    assert ledger.load([first, second]) == []
    assert (ledger.active(CODE), ledger.active(AGENT)) == (1, 2)
    assert ledger.active(Subject("sbx-2")) is None
    answered = ledger.load(
        [answer([first, second], 1, True, 3), answer([first, second], 2, False, 4)]
    )
    assert [(a.subject, a.rule, a.cleared) for a in answered] == [
        (CODE, HOST_RATE, True),
        (AGENT, SAME_URL, False),
    ]
    assert ledger.active(CODE) is None
    assert ledger.active(AGENT) is None


def test_an_answer_to_another_key_does_not_clear_a_hold() -> None:
    first = hold_record(1, CODE)
    ledger = Holds()
    ledger.load([first])
    forged = answer([first], 1, True, 2)
    forged.payload["target"] = AGENT.hold_key(HOST_RATE, T0).payload()
    ledger.load([forged, Rec(3, "approval", {"request_seq": "1"}), Rec(4, "escalation", {})])
    assert ledger.active(CODE) == 1


def test_a_drift_lasts_until_accepted_or_pinned_anew() -> None:
    pin = pin_digest({"search": "a" * 64})
    ledger = DriftLedger()
    ledger.load([Rec(1, "mcp-tools-drift", {"server": "papers", "pin": pin})])
    assert ledger.drifted("papers", pin) == 1
    assert ledger.drifted("other", pin) is None
    assert ledger.drifted("papers", pin_digest({"search": "b" * 64})) is None  # a new pin
    ledger.load([Rec(2, "mcp-tools-accepted", {"server": "papers"})])
    assert ledger.drifted("papers", pin) is None
    ledger.note("papers", pin, 3)
    assert ledger.drifted("papers", pin) == 3


def test_an_unread_listing_waits_for_its_projects_report_and_any_report_answers_it() -> None:
    at = datetime(2026, 10, 24, 12, 0, tzinfo=UTC)
    later = at + timedelta(seconds=31)
    unread = UnreadListings()
    one: dict[str, object] = {"project": "p1", "server": "s"}
    unread.load([Rec(1, "mcp-tools-listing-unread", one)])
    assert unread.pending("p1", "s", at + timedelta(seconds=29)) is None  # within the grace
    assert unread.pending("p1", "s", later) == 1
    # A report for another project does not answer it.
    unread.load([Rec(2, "mcp-tools-listing-reported", {"project": "p2", "server": "s"})])
    assert unread.pending("p1", "s", later) == 1
    # The accepted residual: any report of the project's server answers every unread
    # listing outstanding, since the runtime's word is trusted for what it listed.
    unread.load([Rec(3, "mcp-tools-listing-unread", one)])
    unread.load([Rec(4, "mcp-tools-listing-reported", one)])
    assert unread.pending("p1", "s", later) is None
