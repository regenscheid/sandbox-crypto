# SPDX-License-Identifier: MIT
"""Evidence admission policy can be exercised without a repository or transaction."""

from __future__ import annotations

from dataclasses import FrozenInstanceError, replace

import pytest

from sandbox_science.orchestrator.consistency.snapshot import (
    ITEM_OVERHEAD,
    EvidenceGrant,
    EvidenceItem,
    FindingGroup,
    SnapshotInputs,
    SnapshotLimits,
    SnapshotPlanner,
    SnapshotWindow,
)
from sandbox_science.orchestrator.consistency.values import digest, dump, stored_json


def item(name: str, *, complete: int = 1) -> EvidenceItem:
    payload = dump({"name": name})
    return EvidenceItem(name, dump({"name": name}), payload, digest(payload), complete)


def group(name: str, *, grants: tuple[EvidenceGrant, ...] = ()) -> FindingGroup:
    return FindingGroup(name, 1, (item(name),), grants, None)


def inputs(**changes: object) -> SnapshotInputs:
    return replace(
        SnapshotInputs(
            kind="terminal",
            window=SnapshotWindow(10, None, 20, 0),
            targets=(),
            coverage_complete=True,
            finding_count=0,
            max_feed_bytes=8000,
            max_feed_items=32,
        ),
        **changes,
    )


def test_reserved_findings_keep_their_order_when_a_later_pass_admits_more() -> None:
    planner = SnapshotPlanner(
        inputs(finding_count=2, shown=(item("plan"),), limits=SnapshotLimits(items=8))
    )
    first = replace(group("first"), items=(item("first-revision"), item("first")))
    second = replace(group("second"), items=(item("second-revision"), item("second")))
    planner.reserve(0, first)
    planner.reserve(1, second)
    assert planner.admitted(0)
    assert not planner.admitted(1)
    assert planner.feed_ceiling == 5
    planner.select_feed(20, (item("feed"),))
    # The second materialization, rather than the rejected first one, is admitted.
    fresh = replace(second, items=(item("new-response-revision"), item("second")))
    planner.admit(1, fresh)
    plan = planner.finish()
    assert [e.kind for e in plan.items] == [
        "feed",
        "plan",
        "first-revision",
        "first",
        "new-response-revision",
        "second",
    ]
    assert plan.complete


def test_repeated_grants_use_one_slot_and_preserve_first_admitted_evidence() -> None:
    original = {"locator": {"run": "earlier"}, "payload": {"value": 7}, "complete": True}
    grant = EvidenceGrant.from_record("ev-original", original)
    planner = SnapshotPlanner(inputs(finding_count=2, limits=SnapshotLimits(items=6)))
    planner.reserve(0, group("first", grants=(grant,)))
    planner.reserve(1, group("second", grants=(grant,)))
    assert planner.admitted(1)  # Three reserved slots: two findings and one grant.
    assert planner.granted == frozenset({"ev-original"})
    original["payload"]["value"] = 99
    planner.select_feed(20, ())
    plan = planner.finish()
    assert len(plan.grants) == 1
    assert stored_json(plan.grants[0].record)["payload"] == {"value": 7}
    assert plan.complete


@pytest.mark.parametrize(
    ("failures", "expected"), [(0, (8000, 32)), (1, (2000, 8)), (2, (500, 2)), (9, (500, 2))]
)
def test_retry_feed_allowance_shrinks_at_most_twice(
    failures: int, expected: tuple[int, int]
) -> None:
    planner = SnapshotPlanner(inputs(window=SnapshotWindow(10, 20, 20, failures)))
    assert planner.feed_room == expected


def test_plan_item_counts_against_the_reserved_finding_share() -> None:
    planner = SnapshotPlanner(
        inputs(finding_count=1, shown=(item("plan"),), limits=SnapshotLimits(items=2))
    )
    planner.reserve(0, group("finding"))
    assert not planner.admitted(0)
    planner.select_feed(20, ())
    planner.admit(0, group("finding"))
    assert [i.kind for i in planner.finish().items] == ["plan", "finding"]


def test_oversized_group_is_omitted_in_both_passes_with_incomplete_coverage() -> None:
    planner = SnapshotPlanner(inputs(finding_count=1, limits=SnapshotLimits(item_bytes=3)))
    candidate = group("too-large")
    planner.reserve(0, candidate)
    planner.select_feed(20, (item("feed"),))
    planner.admit(0, candidate)
    plan = planner.finish()
    assert [i.kind for i in plan.items] == ["feed"]
    assert not plan.span_complete
    assert not plan.complete


def test_confirmation_pins_exact_bodies_and_does_not_consume_the_author_feed() -> None:
    pin = {
        "revision": 3,
        "sha256": "a" * 64,
        "macro_context": {"revision": 8},
        "response_revision": 1,
    }
    candidate = replace(group("finding"), body_pin=dump(pin))
    planner = SnapshotPlanner(
        inputs(kind="confirmation", finding_count=1, targets=(("finding", 1),))
    )
    planner.reserve(0, candidate)
    planner.select_feed(10, ())
    plan = planner.finish(item("fix-actions"))
    assert plan.selected_end == 10
    assert plan.targets == (("finding", 1),)
    assert plan.complete
    assert plan.confirmation_key == digest(
        dump([[["finding", 1]], [["finding", 3, "a" * 64, digest(dump({"revision": 8}))]]])
    )
    assert [i.kind for i in plan.items] == ["finding", "fix-actions"]
    assert stored_json(plan.response_pins[0].record) == pin
    with pytest.raises(FrozenInstanceError):
        plan.complete = False  # type: ignore[misc]


@pytest.mark.parametrize("missing", ["target", "finding", "feed", "grant", "coverage"])
def test_incomplete_evidence_never_claims_full_coverage(missing: str) -> None:
    grant = EvidenceGrant.from_record(
        "ev-old", {"locator": {}, "payload": {}, "complete": missing != "grant"}
    )
    planner = SnapshotPlanner(
        inputs(
            kind="confirmation" if missing == "target" else "terminal",
            targets=(("absent", 1),) if missing == "target" else (),
            finding_count=2 if missing == "finding" else 1,
            coverage_complete=missing != "coverage",
        )
    )
    planner.reserve(0, group("finding", grants=(grant,)))
    planner.select_feed(20, (item("feed", complete=int(missing != "feed")),))
    assert not planner.finish().span_complete


def test_a_bounded_prefix_is_span_complete_but_not_chronologically_complete() -> None:
    planner = SnapshotPlanner(inputs())
    planner.select_feed(15, (item("prefix"),))
    plan = planner.finish()
    assert plan.span_complete
    assert not plan.complete


def test_grant_metadata_counts_utf8_bytes_and_cannot_follow_mutable_input() -> None:
    record = {"locator": {"path": "données"}, "payload": {"text": "数学"}, "complete": False}
    grant = EvidenceGrant.from_record("ev-unicode", record)
    expected = (
        len(dump(record["locator"]).encode())
        + len(dump(record["payload"]).encode())
        + ITEM_OVERHEAD
    )
    record["locator"]["path"] = "changed"
    record["payload"]["text"] = "changed"
    record["complete"] = True
    assert grant.size == expected
    assert not grant.complete
    assert stored_json(grant.record) == {
        "locator": {"path": "données"},
        "payload": {"text": "数学"},
        "complete": False,
    }
    with pytest.raises(FrozenInstanceError):
        grant.size = 0  # type: ignore[misc]
