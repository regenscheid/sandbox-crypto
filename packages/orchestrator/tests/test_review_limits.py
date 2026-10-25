# SPDX-License-Identifier: MIT
"""A review of a normal session's window finishes within its limits (lab 38), and a
review a limit ends says which, with what it used.

Lab 38's reviewer, a large model behind the deployment's ``sandbox-orchestrator`` alias,
first asked for a manifest page of 100 (refused: 20 at most), read twenty to fifty items,
sometimes more than 16 in one turn (which failed the review), and resent the whole
conversation with each call, so that six or seven calls used the 64,000-token budget.
The scripted reviewer here does the same against a window of 48 items."""

from __future__ import annotations

import json
import math
from collections.abc import Sequence
from typing import Any

import pytest

from sandbox_science.orchestrator.consistency.findings import Findings
from sandbox_science.orchestrator.consistency.models import ReviewConfig
from sandbox_science.orchestrator.consistency.runner import READ_CHARACTERS, Read, ReviewerRunner
from sandbox_science.orchestrator.consistency.store import ReviewStore
from sandbox_science.orchestrator.model import ModelTurn, ScriptedModel, ToolCall

from .test_consistency import claim, dispatch
from .test_consistency import store as store


class LabReviewer:
    """A reviewer shaped like lab 38's: a manifest page too large, more reads in one turn
    than are answered, then the rest, a check and a pass. Each call reports the tokens the
    whole conversation would cost, as a provider does."""

    def __init__(self) -> None:
        self.calls = 0

    async def complete(self, messages: Sequence[Any], tools: Sequence[Any]) -> ModelTurn:
        self.calls += 1
        tokens = math.ceil(len(json.dumps(messages)) / 3)
        results = [json.loads(m["content"]) for m in messages if m["role"] == "tool"]
        listed = [
            item["evidence"]
            for result in results
            if "evidence" in result and isinstance(result["evidence"], list)
            for item in result["evidence"]
        ]
        read = {r["evidence"] for r in results if isinstance(r.get("evidence"), str)}
        last = results[-1] if results else {}
        if self.calls == 1:
            return self.turn([("review_manifest", {"limit": 100})], tokens)
        if last.get("next_after") is not None:
            return self.turn([("review_manifest", {"after": last["next_after"]})], tokens)
        unread = [e for e in listed if e not in read]
        if unread:
            # Twenty reads in one turn: the first 16 run, the rest come back unrun.
            return self.turn([("review_read", {"evidence": e}) for e in unread[:20]], tokens)
        if not any("checked" in r for r in results):
            checked = {"statement": "Consistent.", "evidence": listed[:3]}
            return self.turn([("review_checked", checked)], tokens)
        return ModelTurn(
            json.dumps({"outcome": "passed", "summary": "Checked.", "cap_reached": False}),
            input_tokens=tokens,
            output_tokens=200,
        )

    def turn(self, calls: list[tuple[str, Any]], tokens: int) -> ModelTurn:
        return ModelTurn(
            None,
            tuple(
                ToolCall(f"c{self.calls}-{n}", name, json.dumps(args))
                for n, (name, args) in enumerate(calls)
            ),
            input_tokens=tokens,
            output_tokens=200,
        )


def window(reviews: ReviewStore, items: int = 48) -> str:
    for n in range(items):
        claim(reviews, f"Step {n}: the order of the point is {'8N' if n % 2 else 'N'}. " * 20,
              key=f"m{n}")  # fmt: skip
    return dispatch(reviews)


@pytest.mark.asyncio
async def test_a_review_of_a_normal_window_finishes_within_the_default_limits(store) -> None:
    reviews, _, _ = store
    review = window(reviews, ReviewConfig().max_feed_items)
    reviewer = LabReviewer()
    await ReviewerRunner(reviews, "p", "s", review, "owner", reviewer).run()
    run = reviews.run("p", "s", review)
    assert (run["state"], run["summary"]) == ("passed", "Checked.")
    assert run["calls"] == reviewer.calls < ReviewConfig().max_turns
    # The budget counts what the calls used, not the estimates.
    assert run["estimated_tokens"] == run["input_tokens"] + run["output_tokens"]
    assert ReviewConfig().token_budget is None  # D102: accounted, never a separate stop
    assert run["input_tokens"] + run["output_tokens"] < 400000
    assert run["coverage_complete"] == 1


@pytest.mark.asyncio
async def test_lab_38s_limits_would_have_capped_it_and_say_which(store) -> None:
    """With the limits lab 38 ran with, the same review used up the token budget; the
    summary says so, with what it used."""
    reviews, _, _ = store
    reviews.defaults = ReviewConfig(
        max_turns=12, time_seconds=120, max_input_chars=150000, token_budget=64000,
        calls_per_hour=60, max_feed_items=48, max_feed_bytes=8 * 1024 * 1024,
    )  # fmt: skip
    review = window(reviews)
    await ReviewerRunner(reviews, "p", "s", review, "owner", LabReviewer()).run()
    run = reviews.run("p", "s", review)
    assert run["state"] == "capped"
    assert run["summary"].startswith("The review's token budget was reached: ")
    assert "of 64,000 tokens used after" in run["summary"]


@pytest.mark.asyncio
async def test_the_calls_of_a_turn_past_16_are_not_run_and_the_reviewer_is_told(store) -> None:
    reviews, _, _ = store
    reviews.defaults = ReviewConfig(max_feed_items=20)
    review = window(reviews, 20)
    seen: list[Any] = []

    class Greedy(LabReviewer):
        async def complete(self, messages: Sequence[Any], tools: Sequence[Any]) -> ModelTurn:
            seen.append(list(messages))
            return await super().complete(messages, tools)

    await ReviewerRunner(reviews, "p", "s", review, "owner", Greedy()).run()
    assert reviews.run("p", "s", review)["state"] == "passed"
    # The turn of twenty reads: sixteen answered, four told to come again.
    turn = next(m for m in seen if sum(1 for x in m if x["role"] == "tool") >= 20)
    answers = [json.loads(x["content"]) for x in turn if x["role"] == "tool"]
    unrun = [a for a in answers if a.get("error") == "not_run"]
    assert (len(unrun), unrun[0]["message"]) == (
        4,
        "Only the first 16 tool calls of a turn run; make this one again in your next turn.",
    )


@pytest.mark.asyncio
async def test_the_reviewer_is_told_a_reads_count_is_characters(store) -> None:
    """Lab 41: review_read's start and count said nothing of what they count. The reviewer
    passed the numbers the tool descriptions held, 16 (reads in a turn) and 50 (a manifest
    page), as each read's count, got 16 or 50 characters of every item, and ended two
    reviews inconclusive. The offered schema says the count is characters, and its
    default and maximum, which the read takes."""
    reviews, _, _ = store
    review = window(reviews, 2)
    offered: list[Any] = []

    class Recording(LabReviewer):
        async def complete(self, messages: Sequence[Any], tools: Sequence[Any]) -> ModelTurn:
            offered.extend(tools)
            return await super().complete(messages, tools)

    await ReviewerRunner(reviews, "p", "s", review, "owner", Recording()).run()
    (read,) = {spec.name: spec for spec in offered if spec.name == "review_read"}.values()
    properties = read.parameters["properties"]
    assert "characters to read, not items" in properties["count"]["description"]
    assert f"{READ_CHARACTERS} by default" in properties["count"]["description"]
    assert "0 by default" in properties["start"]["description"]
    assert f"first {READ_CHARACTERS:,} characters" in read.description
    # What the schema says is what the read does.
    item = next(i for i in reviews.manifest("p", "s", review)["evidence"])
    whole = reviews.read_evidence("p", "s", review, item["evidence"])
    assert (
        whole["text"]
        == reviews.read_evidence(
            "p", "s", review, item["evidence"], start=0, count=READ_CHARACTERS
        )["text"]
    )
    assert Read(evidence=item["evidence"]).count == READ_CHARACTERS


@pytest.mark.asyncio
async def test_a_review_the_hourly_cap_ends_says_so(store) -> None:
    reviews, _, _ = store
    reviews.defaults = ReviewConfig(calls_per_hour=2)
    review = window(reviews, 4)
    await ReviewerRunner(reviews, "p", "s", review, "owner", LabReviewer()).run()
    run = reviews.run("p", "s", review)
    assert (run["state"], run["calls"]) == ("capped", 2)
    assert run["summary"] == (
        "The project's reviewer call cap was reached: 2 of 2 calls in the last hour, after "
        "2 calls of this review. Recorded findings are retained."
    )


@pytest.mark.asyncio
async def test_a_review_the_call_limit_ends_says_so(store) -> None:
    reviews, _, _ = store
    reviews.defaults = ReviewConfig(max_turns=3)
    review = window(reviews, 4)
    await ReviewerRunner(reviews, "p", "s", review, "owner", LabReviewer()).run()
    run = reviews.run("p", "s", review)
    assert (run["state"], run["summary"]) == (
        "capped",
        "The review's call limit was reached: 3 calls without an outcome. Recorded findings "
        "are retained.",
    )


@pytest.mark.asyncio
async def test_native_replay_caps_before_a_confined_frame_can_overflow(store) -> None:
    """Large valid responses stop before their base64 replay exceeds the wire frame."""
    from sandbox_science.orchestrator.consistency.models import (
        REVIEWER_MAX_REPLAY_NATIVE_BYTES,
    )

    reviews, _, _ = store
    review = window(reviews, 1)
    native = json.dumps(
        {"output": [{"type": "reasoning", "encrypted_content": "x" * (5 * 1024 * 1024)}]}
    ).encode()

    class LargeNative:
        def __init__(self) -> None:
            self.calls = 0

        async def complete(self, messages: Sequence[Any], tools: Sequence[Any]) -> ModelTurn:
            self.calls += 1
            return ModelTurn(
                None,
                (ToolCall(f"manifest-{self.calls}", "review_manifest", "{}"),),
                native=native,
                native_format="responses",
                input_tokens=100,
                output_tokens=100,
            )

    model = LargeNative()
    await ReviewerRunner(reviews, "p", "s", review, "owner", model).run()
    run = reviews.run("p", "s", review)
    assert model.calls == run["calls"] == 3
    assert run["state"] == "capped"
    assert run["summary"].startswith("The review's native response replay reached ")
    assert f"{REVIEWER_MAX_REPLAY_NATIVE_BYTES:,}-byte" in run["summary"]
    assert reviews.progress("p", "s")["retry_end"] == run["end_seq"]


def test_the_run_service_sets_the_review_limits_a_project_has_not() -> None:
    from sandbox_science.orchestrator.harness import Settings

    settings = Settings(review_token_budget=800000, review_calls_per_hour=500)
    assert (settings.reviews().token_budget, settings.reviews().calls_per_hour) == (800000, 500)
    assert Settings().reviews() == ReviewConfig()


@pytest.mark.asyncio
async def test_with_no_calls_left_this_hour_the_work_waits_as_one_review(store) -> None:
    """Lab 38: once the project's calls for the hour were spent, three reviews started
    with none left, ended capped, and took their windows with them. Now no review starts:
    the session's work waits as one pending review, said once, and the first review once
    calls are left covers all of it."""
    reviews, documents, now = store
    reviews.defaults = ReviewConfig(calls_per_hour=4)
    first = window(reviews, 4)
    await ReviewerRunner(reviews, "p", "s", first, "owner", LabReviewer()).run()
    assert reviews.run("p", "s", first)["state"] == "passed"
    start = {"author_run": "author-1", "owner": "owner", "alias": "reviewer"}
    for n in range(3):
        claim(reviews, f"Later step {n}.", key=f"later{n}")
        assert reviews.dispatch("p", "s", "terminal", **start) is None
    listing = reviews.listing("p", "s")
    assert listing["waiting"] == {"reason": "calls_per_hour", "calls": 4, "limit": 4,
                                  "since": now[0]}  # fmt: skip
    assert len(deferrals(documents)) == 1
    now[0] += 3601
    assert reviews.due("p", "s")
    later = dispatch(reviews)
    run = reviews.run("p", "s", later)
    assert (run["start_seq"], run["end_seq"]) == (5, 7)
    assert reviews.listing("p", "s")["waiting"] is None


def deferrals(documents: Any) -> list[Any]:
    from sandbox_science.orchestrator.control import EventLog

    log = EventLog(documents.control, "review", "test")
    with documents.control.read() as tx:
        return log.recent(tx.sql, "p", "s", "review.deferred", 10)


@pytest.mark.asyncio
async def test_a_review_a_limit_ends_keeps_its_window(store) -> None:
    """Lab 38's capped reviews took their windows as covered. A review a limit ends now
    leaves its window to the next review, as a failed one does."""
    reviews, _, now = store
    reviews.defaults = ReviewConfig(max_turns=3)
    first = window(reviews, 4)
    await ReviewerRunner(reviews, "p", "s", first, "owner", LabReviewer()).run()
    assert reviews.run("p", "s", first)["state"] == "capped"
    progress = reviews.progress("p", "s")
    assert (progress["watermark"], progress["retry_end"]) == (0, 4)
    reviews.defaults = ReviewConfig()
    now[0] += 200
    again = dispatch(reviews)
    run = reviews.run("p", "s", again)
    assert (run["start_seq"], run["end_seq"]) == (1, 4)
    await ReviewerRunner(reviews, "p", "s", again, "owner", LabReviewer()).run()
    assert reviews.run("p", "s", again)["state"] == "passed"
    assert reviews.progress("p", "s")["watermark"] == 4


@pytest.mark.asyncio
async def test_large_feed_is_reviewed_in_complete_bounded_windows(store) -> None:
    reviews, _, _ = store
    reviews.defaults = ReviewConfig(max_feed_items=4)
    for n in range(10):
        claim(reviews, f"Claim {n} is supported.", key=f"claim-{n}")

    covered: list[tuple[int, int]] = []
    while reviews.progress("p", "s")["remaining"]:
        review = dispatch(reviews)
        run = reviews.run("p", "s", review)
        assert bool(run["coverage_complete"]) == (run["end_seq"] == 10)
        assert len(reviews.manifest("p", "s", review)["evidence"]) <= 12
        covered.append((run["start_seq"], run["end_seq"]))
        await ReviewerRunner(reviews, "p", "s", review, "owner", LabReviewer()).run()
        assert reviews.run("p", "s", review)["state"] == "passed"
    assert covered == [(1, 4), (5, 8), (9, 10)]


@pytest.mark.asyncio
async def test_partial_window_cannot_claim_a_missing_later_completion(store) -> None:
    """An early bounded window cannot turn unseen later history into a finding."""
    reviews, _, _ = store
    reviews.defaults = ReviewConfig(max_feed_items=2)
    for n in range(3):
        claim(reviews, f"Chronological record {n}.", key=f"record-{n}")
    review = dispatch(reviews)
    assert not reviews.run("p", "s", review)["coverage_complete"]
    evidence = reviews.manifest("p", "s", review)["evidence"][0]["evidence"]
    runner = ReviewerRunner(reviews, "p", "s", review, "owner", None)
    result = json.loads(
        await runner.tool(
            ToolCall(
                "premature-completion",
                "review_finding",
                json.dumps(
                    {
                        "severity": "warn",
                        "category": "completion",
                        "statement": "No later completion was recorded.",
                        "anchors": [{"evidence": evidence}],
                        "assessment": "missing_evidence",
                        "operation_id": "premature-completion",
                    }
                ),
            )
        )
    )
    assert result["error"] == "incomplete_coverage"
    assert reviews.finding_states("p", "s") == {}


@pytest.mark.asyncio
async def test_partial_window_tells_reviewer_once_then_finishes_without_finding(store) -> None:
    reviews, documents, _ = store
    reviews.defaults = ReviewConfig(max_feed_items=2)
    for n in range(3):
        claim(reviews, f"Chronological record {n}.", key=f"record-{n}")
    review = dispatch(reviews)
    evidence = reviews.manifest("p", "s", review)["evidence"][0]["evidence"]
    model = ScriptedModel.from_json(
        [
            {
                "tool_calls": [
                    {
                        "name": "review_finding",
                        "arguments": {
                            "severity": "warn",
                            "category": "completion",
                            "statement": "No later completion was recorded.",
                            "anchors": [{"evidence": evidence}],
                            "operation_id": "premature-completion",
                        },
                    }
                ]
            },
            {
                "text": json.dumps(
                    {
                        "outcome": "inconclusive",
                        "summary": "Later history is outside this window.",
                        "cap_reached": False,
                    }
                )
            },
        ]
    )
    await ReviewerRunner(reviews, "p", "s", review, "owner", model).run()
    assert len(model.requests) == 2
    opening = json.loads(model.requests[0][1]["content"])
    assert opening["chronological_coverage"] == "partial"
    assert "do not create a completion finding" in opening["coverage_instruction"]
    assert reviews.run("p", "s", review)["state"] == "inconclusive"
    assert reviews.finding_states("p", "s") == {}
    with documents.control.read() as tx:
        (result,) = tx.sql.execute(
            "SELECT json_extract(payload,'$.result') FROM review_records "
            "WHERE review_id=? AND kind='tool'",
            (review,),
        ).fetchone()
    assert json.loads(result)["error"] == "incomplete_coverage"


@pytest.mark.asyncio
async def test_partial_window_keeps_evidence_supported_completion_finding(store) -> None:
    reviews, _, _ = store
    reviews.defaults = ReviewConfig(max_feed_items=2)
    for n in range(3):
        claim(reviews, f"Chronological record {n}.", key=f"record-{n}")
    review = dispatch(reviews)
    evidence = reviews.manifest("p", "s", review)["evidence"][0]["evidence"]
    runner = ReviewerRunner(reviews, "p", "s", review, "owner", None)
    result = json.loads(
        await runner.tool(
            ToolCall(
                "supported-completion",
                "review_finding",
                json.dumps(
                    {
                        "severity": "fail",
                        "category": "completion",
                        "statement": "The pinned record contradicts the completion claim.",
                        "anchors": [{"evidence": evidence}],
                        "assessment": "supported",
                        "operation_id": "supported-completion",
                    }
                ),
            )
        )
    )
    assert result["status"] == "open"


@pytest.mark.asyncio
async def test_feed_end_is_chronologically_complete_even_with_partial_evidence(store) -> None:
    reviews, _, _ = store
    claim(reviews, "The terminal record.")
    review = reviews.dispatch(
        "p",
        "s",
        "terminal",
        author_run="author-1",
        owner="owner",
        alias="reviewer",
        coverage_complete=False,
    )
    assert review is not None
    assert not reviews.run("p", "s", review)["coverage_complete"]
    assert reviews.chronological_complete("p", "s", review)
    evidence = reviews.manifest("p", "s", review)["evidence"][0]["evidence"]
    runner = ReviewerRunner(reviews, "p", "s", review, "owner", None)
    result = json.loads(
        await runner.tool(
            ToolCall(
                "end-completion",
                "review_finding",
                json.dumps(
                    {
                        "severity": "warn",
                        "category": "completion",
                        "statement": "Completion evidence remains unavailable.",
                        "anchors": [{"evidence": evidence}],
                        "assessment": "missing_evidence",
                        "operation_id": "end-completion",
                    }
                ),
            )
        )
    )
    # A finding on missing evidence is open, and its first history record says so.
    assert result["status"] == "open"
    raised = Findings(reviews).get("p", "s", result["finding_id"])["history"][0]
    assert raised["payload"]["missing_evidence"] is True


@pytest.mark.asyncio
async def test_capped_window_shrinks_before_retry_without_skipping_feed(store) -> None:
    reviews, _, _ = store
    reviews.defaults = ReviewConfig(max_feed_items=16, max_turns=1)
    for n in range(20):
        claim(reviews, f"Claim {n} is supported.", key=f"claim-{n}")

    first = dispatch(reviews)
    assert reviews.run("p", "s", first)["end_seq"] == 16
    await ReviewerRunner(reviews, "p", "s", first, "owner", LabReviewer()).run()
    assert reviews.run("p", "s", first)["state"] == "capped"
    assert reviews.progress("p", "s")["retry_end"] == 16

    second = dispatch(reviews)
    assert not reviews.run("p", "s", second)["coverage_complete"]
    assert (
        reviews.run("p", "s", second)["start_seq"],
        reviews.run("p", "s", second)["end_seq"],
    ) == (1, 4)
    reviews.finish("p", "s", second, "owner", "inconclusive", "Bounded evidence checked.")
    assert reviews.progress("p", "s")["watermark"] == 4
    assert reviews.progress("p", "s")["remaining"]
    assert reviews.run("p", "s", dispatch(reviews))["start_seq"] == 5


def test_three_failed_attempts_defer_without_skipping_evidence(store) -> None:
    reviews, _, now = store
    reviews.defaults = ReviewConfig(max_feed_items=16)
    for n in range(20):
        claim(reviews, f"Claim {n}.", key=f"claim-{n}")

    attempts = []
    for _ in range(3):
        review = dispatch(reviews)
        run = reviews.run("p", "s", review)
        attempts.append((run["start_seq"], run["end_seq"]))
        reviews.finish("p", "s", review, "owner", "failed", "Synthetic model failure.")
    assert attempts == [(1, 16), (1, 4), (1, 1)]
    assert reviews.progress("p", "s")["watermark"] == 0
    assert reviews.progress("p", "s")["retry_end"] == 1
    assert reviews.progress("p", "s")["remaining"]
    assert (
        reviews.dispatch(
            "p", "s", "terminal", author_run="author-1", owner="owner", alias="reviewer"
        )
        is None
    )
    now[0] += 300
    assert reviews.run("p", "s", dispatch(reviews))["start_seq"] == 1


def test_cooldown_starts_when_the_slow_failure_ends(store) -> None:
    reviews, _, now = store
    claim(reviews)
    for _ in range(3):
        review = dispatch(reviews)
        now[0] += 100
        reviews.finish("p", "s", review, "owner", "failed", "Model timed out.")
    assert (
        reviews.dispatch(
            "p", "s", "terminal", author_run="author-1", owner="owner", alias="reviewer"
        )
        is None
    )
    now[0] += 250
    assert not reviews.due("p", "s")
    now[0] += 50
    assert dispatch(reviews)


def test_incomplete_finding_span_remains_a_historical_gap(store) -> None:
    reviews, _, _ = store
    reviews.defaults = ReviewConfig(max_feed_items=1)
    reviews.observe(
        "p",
        "s",
        "incomplete",
        "transcript",
        {"run": "author-1"},
        {"role": "assistant", "content": "The result is 16."},
        complete=False,
    )
    claim(reviews, "Later result is 15.", "later")
    first = dispatch(reviews)
    assert not reviews.span_complete("p", "s", first)
    reviews.finish("p", "s", first, "owner", "findings", "Found a problem.")
    assert reviews.progress("p", "s")["historical_holes"]
    second = dispatch(reviews)
    reviews.finish("p", "s", second, "owner", "passed", "Checked later span.")
    assert reviews.progress("p", "s")["historical_holes"]


def test_prior_ledger_claim_is_context_and_cannot_count_as_new_coverage(store) -> None:
    reviews, _, _ = store
    reviews.defaults = ReviewConfig(max_feed_items=1)
    reviews.observe(
        "p",
        "s",
        "entry-claim",
        "ledger",
        {"entry": "entry-1", "revision": 1},
        {"claim": "The count is 16."},
    )
    reviews.observe(
        "p",
        "s",
        "observed",
        "execution",
        {"run": "author-1"},
        {"stdout": "count = 15"},
    )
    first = dispatch(reviews)
    reviews.finish("p", "s", first, "owner", "passed", "Checked entry.")
    second = dispatch(reviews)
    items = reviews.manifest("p", "s", second)["evidence"]
    assert [item["kind"] for item in items] == ["context", "execution"]
    assert (
        "The count is 16." in reviews.read_evidence("p", "s", second, items[0]["evidence"])["text"]
    )


def test_oversized_prior_item_does_not_defeat_retry_shrink(store) -> None:
    reviews, _, _ = store
    reviews.defaults = ReviewConfig(max_feed_items=1)
    claim(reviews, "A" * (200 * 1024), "large-prior")
    claim(reviews, "Current evidence.", "current")
    first = dispatch(reviews)
    reviews.finish("p", "s", first, "owner", "passed", "Checked large claim.")
    second = dispatch(reviews)
    items = reviews.manifest("p", "s", second)["evidence"]
    assert items[0]["kind"] == "context"
    assert "omitted" in reviews.read_evidence("p", "s", second, items[0]["evidence"])["text"]
    assert items[1]["kind"] == "transcript"


@pytest.mark.parametrize("near_limit_outcome", [False, True])
def test_oversized_delegated_observation_is_an_incomplete_marker(
    store, monkeypatch, near_limit_outcome: bool
) -> None:
    from sandbox_science.orchestrator.consistency import delegated
    from sandbox_science.orchestrator.consistency.delegated import Delegated
    from sandbox_science.orchestrator.consistency.store import MAX_ITEM_BYTES

    reviews, _, _ = store
    outcome = {"detail": "y" * (MAX_ITEM_BYTES - 20)} if near_limit_outcome else {"status": "done"}
    monkeypatch.setattr(
        delegated, "ended_delegations", lambda *_: [("dlg", "saved-run", "child-run")]
    )
    monkeypatch.setattr(
        delegated,
        "delegated_work",
        lambda *_: [
            Delegated("delegated:dlg", "delegation", {"delegation": "dlg"}, outcome),
            Delegated(
                "delegated:dlg:execution:large",
                "execution",
                {"delegation": "dlg"},
                {"stdout": "x" * (MAX_ITEM_BYTES + 1)},
            ),
        ],
    )
    assert delegated.observe_delegated(reviews, "p", "s", "author-1") == 1
    review = dispatch(reviews)
    items = reviews.manifest("p", "s", review)["evidence"]
    assert [item["complete"] for item in items] == [False, False]
    assert (
        "review_observations_omitted"
        in reviews.read_evidence("p", "s", review, items[0]["evidence"])["text"]
    )
    assert (
        "delegated observation exceeds review bound"
        in reviews.read_evidence("p", "s", review, items[1]["evidence"])["text"]
    )
    reviews.finish("p", "s", review, "owner", "passed", "Claimed all checked.")
    assert reviews.run("p", "s", review)["state"] == "inconclusive"
    assert reviews.progress("p", "s")["historical_holes"]


@pytest.mark.asyncio
@pytest.mark.parametrize("max_terminal_windows", [5, 24])
async def test_terminal_hold_catches_up_more_than_three_small_windows(
    store, max_terminal_windows: int
) -> None:
    from sandbox_science.orchestrator.consistency.controller import ReviewController

    from .test_consistency import context_for

    reviews, _, _ = store
    reviews.defaults = ReviewConfig(max_feed_items=4, max_terminal_windows=max_terminal_windows)
    for n in range(20):
        claim(reviews, f"Claim {n} is supported.", key=f"claim-{n}")

    class FreshReviewer(LabReviewer):
        async def complete(self, messages: Sequence[Any], tools: Sequence[Any]) -> ModelTurn:
            if len(messages) == 2:
                self.calls = 0
            return await super().complete(messages, tools)

    controller = ReviewController(
        reviews, context_for(reviews), run_id="author-1", model=FreshReviewer(), alias="reviewer"
    )
    summary = await controller.terminal([], "The task is complete.")
    assert summary["outcome"] == "passed"
    assert not summary.get("pending")
    assert not reviews.progress("p", "s")["remaining"]
    assert len(reviews.listing("p", "s")["reviews"]) >= 5


@pytest.mark.asyncio
async def test_terminal_pass_keeps_an_earlier_inconclusive_gap_visible(store) -> None:
    from sandbox_science.orchestrator.consistency.controller import ReviewController

    from .test_consistency import context_for

    reviews, _, _ = store
    reviews.defaults = ReviewConfig(max_feed_items=4)
    for n in range(5):
        claim(reviews, f"Claim {n} is supported.", key=f"claim-{n}")
    first = dispatch(reviews)
    reviews.finish("p", "s", first, "owner", "inconclusive", "Evidence was not decisive.")
    controller = ReviewController(
        reviews, context_for(reviews), run_id="author-1", model=LabReviewer(), alias="reviewer"
    )
    summary = await controller.terminal([], "The task is complete.")
    assert summary["outcome"] == "inconclusive"
    assert summary["coverage_gap"]
    assert reviews.progress("p", "s")["historical_holes"]


@pytest.mark.asyncio
async def test_smaller_window_still_records_a_consequential_finding(store) -> None:
    reviews, _, _ = store
    reviews.defaults = ReviewConfig(max_feed_items=4)
    for n in range(8):
        claim(
            reviews,
            "The measured count is 16." if n == 5 else f"The measured count is {n}.",
            key=f"claim-{n}",
        )
        if n == 5:
            reviews.observe(
                "p",
                "s",
                "measured-count",
                "execution",
                {"run": "author-1"},
                {"status": "succeeded", "stdout": "measured count = 15"},
            )
    first = dispatch(reviews)
    reviews.finish("p", "s", first, "owner", "inconclusive", "Evidence checked.")
    second = dispatch(reviews)
    assert reviews.run("p", "s", second)["start_seq"] == 5
    assert not reviews.run("p", "s", second)["coverage_complete"]
    item = reviews.manifest("p", "s", second)["evidence"][1]
    measured = reviews.manifest("p", "s", second)["evidence"][2]
    model = ScriptedModel.from_json(
        [
            {"tool_calls": [{"name": "review_manifest", "arguments": {}}]},
            {
                "tool_calls": [
                    {"name": "review_read", "arguments": {"evidence": item["evidence"]}},
                    {"name": "review_read", "arguments": {"evidence": measured["evidence"]}},
                    {
                        "name": "review_finding",
                        "arguments": {
                            "severity": "fail",
                            "category": "contradicting_value",
                            "statement": "Reported count conflicts with pinned observation.",
                            "anchors": [
                                {"evidence": item["evidence"]},
                                {"evidence": measured["evidence"]},
                            ],
                            "operation_id": "count-contradiction",
                        },
                    },
                ]
            },
            {
                "text": json.dumps(
                    {"outcome": "findings", "summary": "Count differs.", "cap_reached": False}
                )
            },
        ]
    )
    await ReviewerRunner(reviews, "p", "s", second, "owner", model).run()
    assert reviews.run("p", "s", second)["state"] == "findings"
    assert reviews.finding_states("p", "s")["open"] == 1
    assert reviews.progress("p", "s")["watermark"] == reviews.run("p", "s", second)["end_seq"]
    assert (
        reviews.run("p", "s", dispatch(reviews))["start_seq"]
        > reviews.run("p", "s", second)["end_seq"]
    )


@pytest.mark.asyncio
async def test_next_window_can_find_a_contradiction_at_its_boundary(store) -> None:
    reviews, _, _ = store
    reviews.defaults = ReviewConfig(max_feed_items=16)
    for n in range(15):
        claim(reviews, f"Unrelated claim {n}.", key=f"claim-{n}")
    claim(reviews, "The measured count is 16.", key="count-claim")
    reviews.observe(
        "p",
        "s",
        "measured-count",
        "execution",
        {"run": "author-1"},
        {"status": "succeeded", "stdout": "measured count = 15"},
    )
    first = dispatch(reviews)
    assert reviews.run("p", "s", first)["end_seq"] == 16
    reviews.finish("p", "s", first, "owner", "inconclusive", "First span checked.")

    second = dispatch(reviews)
    assert reviews.run("p", "s", second)["start_seq"] == 17
    entries = reviews.manifest("p", "s", second, limit=50)["evidence"]
    claim_item = next(
        item
        for item in entries
        if "measured count is 16"
        in json.dumps(reviews.evidence("p", "s", second, item["evidence"])["payload"])
    )
    measurement = next(
        item
        for item in entries
        if "measured count = 15"
        in json.dumps(reviews.evidence("p", "s", second, item["evidence"])["payload"])
    )
    model = ScriptedModel.from_json(
        [
            {"tool_calls": [{"name": "review_manifest", "arguments": {}}]},
            {
                "tool_calls": [
                    {"name": "review_read", "arguments": {"evidence": claim_item["evidence"]}},
                    {"name": "review_read", "arguments": {"evidence": measurement["evidence"]}},
                    {
                        "name": "review_finding",
                        "arguments": {
                            "severity": "fail",
                            "category": "contradicting_value",
                            "statement": "Reported count conflicts with the next observation.",
                            "anchors": [
                                {"evidence": claim_item["evidence"]},
                                {"evidence": measurement["evidence"]},
                            ],
                            "operation_id": "boundary-contradiction",
                        },
                    },
                ]
            },
            {
                "text": json.dumps(
                    {
                        "outcome": "findings",
                        "summary": "Count differs.",
                        "cap_reached": False,
                    }
                )
            },
        ]
    )
    await ReviewerRunner(reviews, "p", "s", second, "owner", model).run()
    assert reviews.run("p", "s", second)["state"] == "findings"
    assert reviews.finding_states("p", "s")["open"] == 1
