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

from sandbox_science.orchestrator.consistency.models import ReviewConfig
from sandbox_science.orchestrator.consistency.runner import ReviewerRunner
from sandbox_science.orchestrator.consistency.store import ReviewStore
from sandbox_science.orchestrator.model import ModelTurn, ToolCall

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
    review = window(reviews)
    reviewer = LabReviewer()
    await ReviewerRunner(reviews, "p", "s", review, "owner", reviewer).run()
    run = reviews.run("p", "s", review)
    assert (run["state"], run["summary"]) == ("passed", "Checked.")
    assert run["calls"] == reviewer.calls < ReviewConfig().max_turns
    # The budget counts what the calls used, not the estimates.
    assert run["estimated_tokens"] == run["input_tokens"] + run["output_tokens"]
    assert run["input_tokens"] + run["output_tokens"] < ReviewConfig().token_budget
    assert run["coverage_complete"] == 1


@pytest.mark.asyncio
async def test_lab_38s_limits_would_have_capped_it_and_say_which(store) -> None:
    """With the limits lab 38 ran with, the same review used up the token budget; the
    summary says so, with what it used."""
    reviews, _, _ = store
    reviews.defaults = ReviewConfig(
        max_turns=12, time_seconds=120, max_input_chars=150000, token_budget=64000,
        calls_per_hour=60,
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
