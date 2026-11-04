# SPDX-License-Identifier: MIT
"""A review finishes within a fixed budget and the next continues where it stopped (CR-31
and CR-34 as amended 2026-11-03; the triggers are in ``test_consistency_triggers.py``).

The SNOVA run of 2026-10-17 capped 97 of its 107 reviews, reviewed 33 windows three times
from their start, and spent $51 of a $145 run on reviews that recorded nothing. A capped
review now records how far it got and the next one goes on from there; and the reviewer is
told its budget, and before its last two calls how many are left, and gives a verdict on
what it covered with the rest marked unreviewed."""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

import pytest

from sandbox_science.orchestrator.consistency.controller import ReviewController
from sandbox_science.orchestrator.consistency.models import ReviewConfig
from sandbox_science.orchestrator.consistency.rubric import (
    BUDGET,
    BUDGET_VERSION,
    RUBRIC,
)
from sandbox_science.orchestrator.consistency.runner import ReviewerRunner
from sandbox_science.orchestrator.consistency.store import ReviewStore
from sandbox_science.orchestrator.model import ModelTurn

from .scripted import ToolCall
from .test_consistency import claim, context_for, dispatch
from .test_consistency import store as store


def notices(messages: Sequence[Any]) -> list[dict[str, Any]]:
    """The budget notices a request holds, by their payloads."""
    return [
        json.loads(m["content"][len(BUDGET) :])
        for m in messages
        if m["role"] == "user" and str(m["content"]).startswith(BUDGET)
    ]


class Reviewer:
    """Reads the window's new items in manifest order, ``per_turn`` a call. Once told its
    budget is running out it records what it checked, then gives its verdict, naming the
    new items it did not read unreviewed (unless ``silent``); with ``ignore`` it reads on
    regardless, as a reviewer that does not heed the notice."""

    def __init__(self, per_turn: int = 2, *, ignore: bool = False, silent: bool = False) -> None:
        self.per_turn, self.ignore, self.silent = per_turn, ignore, silent
        self.calls = 0
        self.requests: list[list[Any]] = []

    async def complete(self, messages: Sequence[Any], tools: Sequence[Any]) -> ModelTurn:
        self.calls += 1
        self.requests.append(list(messages))
        results = [json.loads(m["content"]) for m in messages if m["role"] == "tool"]
        listed = [
            item
            for result in results
            if isinstance(result.get("evidence"), list)
            for item in result["evidence"]
        ]
        read = {r["evidence"] for r in results if isinstance(r.get("evidence"), str)}
        new = [item["evidence"] for item in listed if item["new"]]
        unread = [e for e in new if e not in read]
        told = notices(messages)
        if not listed:
            return self.turn([("review_manifest", {})])
        if told and not self.ignore:
            if told[-1]["calls_left"] > 1 and read and not any("checked" in r for r in results):
                checked = {"statement": "The values agree.", "evidence": sorted(read)}
                return self.turn([("review_checked", checked)])
            return self.verdict([] if self.silent else unread)
        if unread:
            return self.turn([("review_read", {"evidence": e}) for e in unread[: self.per_turn]])
        if not any("checked" in r for r in results):
            return self.turn([("review_checked", {"statement": "Agree.", "evidence": new})])
        return self.verdict([])

    def verdict(self, unreviewed: list[str]) -> ModelTurn:
        return ModelTurn(
            json.dumps(
                {
                    "outcome": "passed",
                    "summary": "Checked what was covered.",
                    "cap_reached": False,
                    "unreviewed": unreviewed,
                }
            ),
            input_tokens=100,
            output_tokens=50,
        )

    def turn(self, calls: list[tuple[str, Any]]) -> ModelTurn:
        return ModelTurn(
            None,
            tuple(
                ToolCall(f"c{self.calls}-{n}", name, json.dumps(args))
                for n, (name, args) in enumerate(calls)
            ),
            input_tokens=100,
            output_tokens=50,
        )


def claims(reviews: ReviewStore, count: int, prefix: str = "claim") -> list[int]:
    return [claim(reviews, f"{prefix} {n} gave 16.", f"{prefix}-{n}") for n in range(count)]


def budget_records(reviews: ReviewStore, review: str) -> list[dict[str, Any]]:
    with reviews.documents.control.read() as tx:
        return [
            json.loads(payload)
            for (payload,) in tx.sql.execute(
                "SELECT payload FROM review_records WHERE review_id=? AND kind='budget' "
                "ORDER BY seq",
                (review,),
            )
        ]


def test_the_rubric_states_the_budget_and_the_verdict_on_what_was_covered() -> None:
    assert "fixed budget of model calls" in RUBRIC
    assert "Before your last two calls the platform tells you how many remain" in RUBRIC
    assert "Name in unreviewed each new item you did not examine" in RUBRIC
    assert '"unreviewed":[]' in RUBRIC
    assert "Dig no further" in BUDGET


@pytest.mark.asyncio
async def test_the_reviewer_is_told_its_budget_and_before_its_last_two_calls_what_is_left(
    store,
) -> None:
    reviews, _, _ = store
    reviews.defaults = ReviewConfig(max_turns=5)
    seqs = claims(reviews, 6)
    review = dispatch(reviews)
    reviewer = Reviewer(per_turn=2)
    await ReviewerRunner(reviews, "p", "s", review, "owner", reviewer).run()
    opening = json.loads(reviewer.requests[0][1]["content"])
    assert opening["budget"] == {"calls": 5}
    # Calls 1 to 3: the manifest and two reads of two; the notices come before 4 and 5.
    assert [notices(r) for r in reviewer.requests] == [
        [],
        [],
        [],
        [{"calls_left": 2, "limit": "calls"}],
        [{"calls_left": 2, "limit": "calls"}, {"calls_left": 1, "limit": "calls"}],
    ]
    assert [(r["version"], r["calls_left"]) for r in budget_records(reviews, review)] == [
        (BUDGET_VERSION, 2),
        (BUDGET_VERSION, 1),
    ]
    run = reviews.run("p", "s", review)
    # Its verdict covers the four items it read; the last two are unreviewed, and the
    # watermark stops after the fourth, from which the next review goes on.
    assert run["state"] == "passed"
    assert run["coverage"]["through"] == seqs[3]
    assert len(run["coverage"]["unreviewed"]) == 2
    assert not run["coverage_complete"]
    progress = reviews.progress("p", "s")
    assert (progress["watermark"], progress["retry_end"], progress["remaining"]) == (
        seqs[3],
        None,
        True,
    )
    assert not progress["historical_holes"]
    later = reviews.run("p", "s", dispatch(reviews))
    assert (later["start_seq"], later["end_seq"]) == (seqs[4], seqs[5])


@pytest.mark.asyncio
async def test_after_the_notice_only_what_was_examined_counts(store) -> None:
    """A reviewer told its budget is running out that names nothing unreviewed still
    covers only what it read."""
    reviews, _, _ = store
    reviews.defaults = ReviewConfig(max_turns=4)
    seqs = claims(reviews, 6)
    review = dispatch(reviews)
    await ReviewerRunner(reviews, "p", "s", review, "owner", Reviewer(silent=True)).run()
    run = reviews.run("p", "s", review)
    assert (run["state"], run["coverage"]["through"]) == ("passed", seqs[1])
    assert run["coverage"]["named"] == []
    assert reviews.progress("p", "s")["watermark"] == seqs[1]


@pytest.mark.asyncio
async def test_a_verdict_within_budget_covers_all_but_what_it_names_unreviewed(store) -> None:
    """Without a notice, an item the reviewer looked at in the manifest and judged needs
    no read is reviewed; only what it names unreviewed is left for later."""
    reviews, _, _ = store
    seqs = claims(reviews, 4)
    review = dispatch(reviews)
    manifest = reviews.manifest("p", "s", review)["evidence"]
    new = [item["evidence"] for item in manifest if item["new"]]
    runner = ReviewerRunner(reviews, "p", "s", review, "owner", None)
    await runner.tool(
        ToolCall("c1", "review_checked", json.dumps({"statement": "Agree.", "evidence": new[:1]}))
    )
    reviews.finish("p", "s", review, "owner", "passed", "Checked.", unreviewed=[new[2]])
    run = reviews.run("p", "s", review)
    assert run["coverage"]["through"] == seqs[1]
    assert run["coverage"]["unreviewed"] == new[2:]
    assert reviews.progress("p", "s")["watermark"] == seqs[1]


@pytest.mark.asyncio
async def test_the_manifest_marks_the_windows_new_evidence(store) -> None:
    reviews, _, _ = store
    reviews.defaults = ReviewConfig(max_feed_items=2)
    seqs = claims(reviews, 4)
    first = dispatch(reviews)
    reviews.finish("p", "s", first, "owner", "passed", "Checked.")
    second = dispatch(reviews)
    listed = reviews.manifest("p", "s", second)["evidence"]
    held = {
        json.loads(reviews.read_evidence("p", "s", second, item["evidence"])["text"])["content"]: (
            item["new"]
        )
        for item in listed
        if item["kind"] == "transcript"
    }
    # The two items carried from before the watermark are context; the window's are new.
    assert held == {
        "claim 0 gave 16.": False,
        "claim 1 gave 16.": False,
        "claim 2 gave 16.": True,
        "claim 3 gave 16.": True,
    }
    assert reviews.run("p", "s", second)["start_seq"] == seqs[2]


@pytest.mark.asyncio
async def test_a_capped_window_is_continued_never_reviewed_again_from_its_start(store) -> None:
    """The SNOVA defect: each review of a window that capped began it again. A reviewer
    that heeds no notice, with two calls, a manifest and a read of two items, now covers
    the window in three reviews, each going on from the last, and reads no item twice as
    new."""
    reviews, _, now = store
    reviews.defaults = ReviewConfig(max_turns=2, max_feed_items=16)
    seqs = claims(reviews, 6)
    spans: list[tuple[int, int]] = []
    read_as_new: list[str] = []
    while reviews.progress("p", "s")["remaining"]:
        review = dispatch(reviews)
        reviewer = Reviewer(per_turn=2, ignore=True)
        await ReviewerRunner(reviews, "p", "s", review, "owner", reviewer).run()
        run = reviews.run("p", "s", review)
        assert run["state"] == "capped"
        spans.append((run["start_seq"], run["coverage"]["through"]))
        read_as_new.extend(run["coverage"]["covered"])
        now[0] += 200
        assert len(spans) <= 3
    assert spans == [(seqs[0], seqs[1]), (seqs[2], seqs[3]), (seqs[4], seqs[5])]
    assert reviews.progress("p", "s")["failures"] == 0
    with reviews.documents.control.read() as tx:
        payloads = [
            json.loads(p)["content"]
            for (p,) in tx.sql.execute(
                "SELECT payload FROM review_evidence WHERE evidence_id IN "
                "(SELECT value FROM json_each(?))",
                (json.dumps(read_as_new),),
            )
        ]
    assert sorted(payloads) == sorted(f"claim {n} gave 16." for n in range(6))
    # Covered without a verdict: no later pass hides it.
    assert reviews.progress("p", "s")["historical_holes"]


@pytest.mark.asyncio
async def test_a_review_that_covered_nothing_keeps_its_window_and_shrinks(store) -> None:
    """Only a review that covered nothing is tried again, as before, smaller."""
    reviews, _, _ = store
    reviews.defaults = ReviewConfig(max_turns=1, max_feed_items=16)
    claims(reviews, 20)
    first = dispatch(reviews)
    await ReviewerRunner(reviews, "p", "s", first, "owner", Reviewer()).run()
    run = reviews.run("p", "s", first)
    assert (run["state"], run["coverage"]["through"]) == ("capped", None)
    progress = reviews.progress("p", "s")
    assert (progress["watermark"], progress["retry_end"], progress["failures"]) == (0, 16, 1)
    assert reviews.due("p", "s") is False  # the minimum interval has not passed
    second = reviews.run("p", "s", dispatch(reviews))
    assert (second["start_seq"], second["end_seq"]) == (1, 4)


def test_a_verdict_that_covers_nothing_is_no_verdict(store) -> None:
    reviews, _, _ = store
    claims(reviews, 3)
    review = dispatch(reviews)
    new = [i["evidence"] for i in reviews.manifest("p", "s", review)["evidence"] if i["new"]]
    reviews.finish("p", "s", review, "owner", "passed", "Nothing read.", unreviewed=new[:1])
    assert reviews.run("p", "s", review)["state"] == "capped"
    progress = reviews.progress("p", "s")
    assert (progress["watermark"], progress["failures"]) == (0, 1)


@pytest.mark.asyncio
async def test_the_context_bound_is_announced_before_it_ends_the_review(store) -> None:
    """The notice counts the tightest limit: here the context, by the largest turn so far."""
    reviews, _, _ = store
    reviews.defaults = ReviewConfig(max_input_chars=60000, max_feed_bytes=1024 * 1024)
    for n in range(6):
        claim(reviews, f"Item {n}: " + "x" * 14000, f"big-{n}")
    review = dispatch(reviews)
    reviewer = Reviewer(per_turn=1)
    await ReviewerRunner(reviews, "p", "s", review, "owner", reviewer).run()
    told = [n for request in reviewer.requests for n in notices(request)[-1:]]
    assert told
    assert all(n["limit"] == "context" for n in told)
    run = reviews.run("p", "s", review)
    assert run["state"] == "passed"
    assert 1 <= len(run["coverage"]["covered"]) < 6
    assert run["coverage"]["through"] < run["end_seq"]


@pytest.mark.asyncio
async def test_terminal_catch_up_continues_a_capped_window(store) -> None:
    """At a run's close, a review a limit ended after it covered part of its window does
    not end the catch-up: the next review continues from where it stopped."""
    reviews, _, _ = store
    reviews.defaults = ReviewConfig(max_turns=2, max_feed_items=16, max_terminal_windows=6)
    claims(reviews, 6)

    class Fresh(Reviewer):
        async def complete(self, messages: Sequence[Any], tools: Sequence[Any]) -> ModelTurn:
            if len(messages) == 2:
                self.calls = 0
            return await super().complete(messages, tools)

    controller = ReviewController(
        reviews,
        context_for(reviews),
        run_id="author-1",
        model=Fresh(per_turn=2, ignore=True),
        alias="reviewer",
    )
    summary = await controller.terminal([], "The task is complete.")
    assert summary["reviews_total"] == 3
    assert not reviews.progress("p", "s")["remaining"]
    assert not summary.get("pending")
    # Covered without a verdict, so no pass is claimed for the session.
    assert summary["outcome"] == "capped"


@pytest.mark.asyncio
async def test_what_a_finding_names_is_examined(store) -> None:
    """A capped review that raised a finding on an item it never read examined it."""
    reviews, _, _ = store
    seqs = claims(reviews, 3)
    review = dispatch(reviews)
    first = next(i for i in reviews.manifest("p", "s", review)["evidence"] if i["new"])
    finding = {
        "severity": "fail",
        "category": "contradicting_value",
        "statement": "The output records 15, not 16.",
        "anchors": [{"evidence": first["evidence"]}],
        "operation_id": "finding-1",
    }
    runner = ReviewerRunner(reviews, "p", "s", review, "owner", None)
    raised = json.loads(await runner.tool(ToolCall("f", "review_finding", json.dumps(finding))))
    assert raised["status"] == "open"
    reviews.finish("p", "s", review, "owner", "capped", "A limit ended it.", cap_reached=True)
    assert reviews.run("p", "s", review)["coverage"]["through"] == seqs[0]
    assert reviews.progress("p", "s")["watermark"] == seqs[0]


@pytest.mark.parametrize("named", [False, True])
def test_a_ledger_entry_left_unreviewed_is_not_reviewed_for_the_gate(store, named: bool) -> None:
    from sandbox_science.orchestrator.ledger import gate

    reviews, _, _ = store
    claim(reviews)
    reviews.observe(
        "p", "s", "entry-1:1", "ledger", {"entry": "entry-1", "revision": 1}, {"claim": "16."}
    )
    review = dispatch(reviews)
    entry = next(i for i in reviews.manifest("p", "s", review)["evidence"] if i["kind"] == "ledger")
    unreviewed = [entry["evidence"]] if named else []
    reviews.finish("p", "s", review, "owner", "findings", "Checked.", unreviewed=unreviewed)
    with reviews.documents.control.read() as tx:
        assert gate.reviewed(tx.sql, "entry-1") == ([] if named else [1])
    assert reviews.run("p", "s", review)["coverage_complete"] == (0 if named else 1)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("config", "slowest", "limit"),
    [
        (ReviewConfig(calls_per_hour=3), 0.0, "calls_per_hour"),
        (ReviewConfig(time_seconds=250), 100.0, "time"),
    ],
)
async def test_the_notice_names_the_tightest_limit(
    store, config: ReviewConfig, slowest: float, limit: str
) -> None:
    """The project's hourly calls, counted; or the time left, by the slowest call so far."""
    reviews, _, _ = store
    reviews.defaults = config
    claims(reviews, 4)
    review = dispatch(reviews)
    reviewer = Reviewer(per_turn=1)
    runner = ReviewerRunner(reviews, "p", "s", review, "owner", reviewer)
    runner.slowest = slowest
    await runner.run()
    told = [n for request in reviewer.requests for n in notices(request)[-1:]]
    assert told[0] == {"calls_left": 2, "limit": limit}
    assert reviews.run("p", "s", review)["coverage"]["verdict"]
