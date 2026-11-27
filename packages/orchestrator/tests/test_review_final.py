# SPDX-License-Identifier: MIT
"""A run's end is one final review, its findings reach the agent once and the user
always, and reviews during the run keep up or say so (CR-48, CR-49).

On the cluster on 2026-11-26 a four-hour session's end walked its review backlog window
by window: 57 reviews over 56 minutes after the final answer, until the project's hourly
reviewer calls ran out, leaving the late work unreviewed and the one ``fail`` finding
undelivered, while nothing the user saw said so."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Sequence
from typing import Any, cast

import pytest

from sandbox_science.orchestrator.consistency.controller import ReviewController, Reviews
from sandbox_science.orchestrator.consistency.findings import Findings
from sandbox_science.orchestrator.consistency.models import FinalReview, ReviewConfig
from sandbox_science.orchestrator.consistency.notices import FindingNotices
from sandbox_science.orchestrator.consistency.runner import ReviewerRunner
from sandbox_science.orchestrator.consistency.store import ReviewStore
from sandbox_science.orchestrator.loop import run_task
from sandbox_science.orchestrator.model import ModelTurn
from sandbox_science.orchestrator.model_profiles import requested
from sandbox_science.orchestrator.tools import Toolbox
from sandbox_science.orchestrator.worker_client import WorkerClient

from .scripted import ScriptedModel, ToolCall
from .test_consistency import MAIN, claim, context_for, dispatch
from .test_consistency import store as store
from .test_review_limits import LabReviewer
from .test_review_terminal_settlement import DelayedReviewer


class FindingReviewer:
    """Reads the manifest, raises one ``fail`` finding on the first new item, which a
    final review lists newest first, records a check, and gives its verdict."""

    def __init__(self) -> None:
        self.calls = 0
        self.first: list[str] = []
        """The first new item of each review it was given, its content."""

    async def complete(self, messages: Sequence[Any], tools: Sequence[Any]) -> ModelTurn:
        self.calls += 1
        results = [json.loads(m["content"]) for m in messages if m["role"] == "tool"]
        if not results:
            return self.turn([("review_manifest", {})])
        listed = next(r["evidence"] for r in results if isinstance(r.get("evidence"), list))
        new = [item["evidence"] for item in listed if item["new"]]
        if len(results) == 1:
            finding = {
                "severity": "fail",
                "category": "contradicting_value",
                "statement": "The answer says mutation-tested; the script only compares ranks.",
                "anchors": [{"evidence": new[0]}],
                "operation_id": f"final-finding-{self.calls}",
            }
            checked = {"statement": "The answer was read.", "evidence": new}
            return self.turn(
                [
                    ("review_read", {"evidence": new[0]}),
                    ("review_finding", finding),
                    ("review_checked", checked),
                ]
            )
        read = next(r for r in results if r.get("evidence") == new[0])
        self.first.append(str(read.get("text", "")))
        return ModelTurn(
            json.dumps({"outcome": "findings", "summary": "One finding.", "cap_reached": False}),
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


def controller(
    reviews: ReviewStore, model: Any, mode: FinalReview = "after", **options: Any
) -> ReviewController:
    reviews.defaults = ReviewConfig(final_review=mode, **options)
    return ReviewController(
        reviews, context_for(reviews), run_id="author-1", model=model, alias="reviewer"
    )


@pytest.mark.parametrize("mode", ["after", "hold"])
async def test_a_final_fail_finding_takes_exactly_one_answering_turn(
    store, mode: FinalReview
) -> None:
    """The final review's ``fail`` finding is delivered once, as one more turn; the
    summary shows the finding as that turn left it; no review follows the turn. With
    ``after`` the user's answer is the one the agent gave, the turn's reply following the
    review; with ``hold`` it is the reply after the turn."""
    reviews, _, _ = store
    reviewer = FindingReviewer()
    reviewing = controller(reviews, reviewer, mode)
    hook = Reviews(reviewing)
    main = ScriptedModel(
        [
            ModelTurn("The cross-check is mutation-tested; the task is complete."),
            ModelTurn("The cross-check compares ranks only; I corrected the claim."),
        ]
    )
    async with WorkerClient("http://127.0.0.1:1", "synthetic-token") as client:
        transcript = await run_task(
            main,
            Toolbox(client, context_for(reviews), reviews=reviewing),
            "Check the numbers.",
            run_id="author-1",
            max_turns=4,
            hooks=[hook],
        )
    await reviewing.close()
    assert transcript.response_rounds == 1
    assert len(transcript.delivered_ids("reviews")) == 1
    given = "The cross-check is mutation-tested; the task is complete."
    reply = "The cross-check compares ranks only; I corrected the claim."
    assert transcript.final_text == reply
    assert transcript.answer == (given if mode == "after" else reply)
    (review,) = reviews.listing("p", "s")["reviews"]
    assert (review["classifier"]["final"], review["state"]) == (True, "findings")
    # The final answer is the newest work: the review was given it first.
    assert "mutation-tested" in reviewer.first[0]
    summary = cast(dict[str, Any], transcript.summaries["reviews"])
    final = summary["final_review"]
    assert (final["mode"], final["answered"], final["finished"]) == (mode, True, True)
    assert final["open"] == {"fail": 1}
    assert final["fixes_claimed"] == 0
    # The answering turn's work is recorded, for a later review, not reviewed here.
    assert final["feed_end"] > final["reviewed_through"]
    assert final["unreviewed"][-1]["end"] == final["feed_end"]


async def test_the_summary_shows_the_finding_as_the_answering_turn_left_it(store) -> None:
    reviews, _, _ = store
    reviewing = controller(reviews, FindingReviewer())
    hook = Reviews(reviewing)
    from sandbox_science.orchestrator.context.agent import AgentContext
    from sandbox_science.orchestrator.loop import Transcript

    answer = {"role": "assistant", "content": "The task is complete."}
    run = Transcript(context=AgentContext(messages=[answer]))
    first = await hook.closing(run, "The task is complete.")
    assert (first.action, first.hold_answer) == ("respond", False)
    (notice,) = [n for n in first.notices if n.ids]
    (finding,) = cast(list[dict[str, Any]], notice.payload)
    assert cast(dict[str, Any], hook.summary())["final_review"]["open"] == {"fail": 1}
    Findings(reviews).respond(
        "p",
        "s",
        {
            "finding": finding["finding"],
            "expected": finding["revision"],
            "action": "dismiss",
            "explanation": "The answer was corrected in the next message.",
            "operation_id": "dismiss-final",
        },
        MAIN,
    )
    run.response_rounds = 1
    second = await hook.closing(run, "Corrected.")
    assert second.action == "accept"
    final = cast(dict[str, Any], hook.summary())["final_review"]
    assert (final["open"], final["fixes_claimed"], final["answered"]) == ({}, 0, True)
    assert len(reviews.listing("p", "s")["reviews"]) == 1
    await reviewing.close()


async def test_hold_releases_the_answer_when_its_time_passes(store) -> None:
    """``hold`` keeps the answer for the final review at most its time limit; then the
    answer goes out as given, and the summary says the review did not finish."""
    reviews, _, _ = store
    claim(reviews)
    model = DelayedReviewer()
    reviews.defaults = ReviewConfig(final_review="hold")
    reviewing = ReviewController(
        reviews,
        context_for(reviews),
        run_id="author-1",
        model=model,
        alias="reviewer",
        hold_seconds=0.05,
    )
    hook = Reviews(reviewing)
    from sandbox_science.orchestrator.context.agent import AgentContext
    from sandbox_science.orchestrator.loop import Transcript

    try:
        closing = await asyncio.wait_for(
            hook.closing(Transcript(context=AgentContext(messages=[])), "The task is complete."),
            timeout=5,
        )
        assert closing.action == "accept"
        summary = cast(dict[str, Any], hook.summary())
        assert summary["pending"]
        final = summary["final_review"]
        assert (final["mode"], final["outcome"], final["finished"]) == ("hold", "pending", False)
    finally:
        model.release.set()
        await reviewing.close()


async def test_the_hourly_cap_does_not_stop_the_final_review(store) -> None:
    reviews, _, _ = store
    reviews.defaults = ReviewConfig(calls_per_hour=1)
    claim(reviews)
    checkpoint = dispatch(reviews, "checkpoint")
    await ReviewerRunner(reviews, "p", "s", checkpoint, "owner", LabReviewer()).run()
    assert reviews.run("p", "s", checkpoint)["state"] == "capped"
    claim(reviews, "The final count is 16.", "final")
    reviewing = controller(reviews, LabReviewer(), calls_per_hour=1)
    summary = await reviewing.terminal([], "The task is complete.")
    final = reviews.run("p", "s", summary["final_review"]["review"])
    assert final["state"] == "passed"
    assert final["calls"] > 1
    assert summary["final_review"]["complete"]


def test_a_checkpoint_skips_a_backlog_to_the_newest_work_and_says_so(store) -> None:
    """A checkpoint that starts with more than three windows of backlog is given the
    newest window; the range it skipped is recorded unreviewed, and the session's
    coverage names it (CR-49)."""
    reviews, _, _ = store
    reviews.defaults = ReviewConfig(max_feed_items=2)
    seqs = [claim(reviews, f"Step {n}.", f"step-{n}") for n in range(10)]
    review = dispatch(reviews, "checkpoint")
    run = reviews.run("p", "s", review)
    assert (run["start_seq"], run["end_seq"]) == (seqs[8], seqs[9])
    assert reviews.progress("p", "s")["watermark"] == seqs[7]
    (skipped,) = reviews.listing("p", "s")["unreviewed"]
    assert (skipped["start"], skipped["end"], skipped["reason"]) == (seqs[0], seqs[7], "backlog")
    reviews.finish("p", "s", review, "owner", "passed", "Checked the newest steps.")
    coverage = reviews.coverage("p", "s")
    assert not coverage["complete"]
    assert coverage["unreviewed"] == [{"start": seqs[0], "end": seqs[7]}]
    assert (coverage["reviewed_through"], coverage["feed_end"]) == (seqs[9], seqs[9])
    # Three windows of backlog are kept up with, not skipped.
    reviews.defaults = ReviewConfig(max_feed_items=4)
    for n in range(12):
        claim(reviews, f"Later {n}.", f"later-{n}")
    later = dispatch(reviews, "checkpoint")
    assert reviews.run("p", "s", later)["start_seq"] == seqs[9] + 1


async def test_a_final_review_picks_up_a_skipped_range_its_budget_allows(store) -> None:
    reviews, _, _ = store
    reviews.defaults = ReviewConfig(max_feed_items=2)
    for n in range(10):
        claim(reviews, f"Step {n}.", f"step-{n}")
    review = dispatch(reviews, "checkpoint")
    reviews.finish("p", "s", review, "owner", "passed", "Checked the newest steps.")
    assert not reviews.coverage("p", "s")["complete"]
    summary = await controller(reviews, LabReviewer(), max_feed_items=2).terminal(
        [], "The task is complete."
    )
    final = summary["final_review"]
    assert (final["complete"], final["unreviewed"]) == (True, [])
    run = reviews.run("p", "s", final["review"])
    assert run["coverage"]["from"] == 1


async def test_a_backlog_larger_than_the_final_budget_leaves_a_named_range(store) -> None:
    from sandbox_science.orchestrator.consistency.models import FINAL_FEED_ITEMS

    reviews, _, _ = store
    seqs = [claim(reviews, f"Step {n}.", f"step-{n}") for n in range(FINAL_FEED_ITEMS + 5)]
    summary = await controller(reviews, LabReviewer()).terminal([], "The task is complete.")
    final = summary["final_review"]
    assert (final["complete"], summary["coverage_gap"]) == (False, True)
    assert final["unreviewed"] == [{"start": seqs[0], "end": seqs[4]}]
    (left,) = [u for u in reviews.listing("p", "s")["unreviewed"] if u.get("reason")]
    assert (left["start"], left["end"], left["reason"]) == (seqs[0], seqs[4], "final_budget")
    run = reviews.run("p", "s", final["review"])
    assert run["start_seq"] == seqs[5]


async def test_a_final_review_is_given_the_files_and_entries_its_answer_names(store) -> None:
    from .test_review_ledger import CLAIM, change

    reviews, _, _ = store
    change(reviews, CLAIM, op="op-1")
    claim(reviews)
    summary = await controller(reviews, LabReviewer()).terminal(
        [], "As C-1 records, the order is 16, and T-9 names nothing; the task is complete."
    )
    items = reviews.manifest("p", "s", summary["final_review"]["review"], limit=50)["evidence"]
    cited = [i for i in items if i["kind"] == "entry"]
    assert [i["locator"]["handle"] for i in cited] == ["C-1"]
    assert cited[0]["locator"]["cited_by"] == {"answer": "author-1"}
    assert not cited[0]["new"]


async def test_the_reviewers_profile_and_provenance_are_recorded_once(store) -> None:
    """A review records its prompt's provenance with its first call, which each later
    call reads, and its model's profile once, in its own records (CR-49)."""
    reviews, _, _ = store
    for n in range(4):
        claim(reviews, f"Claim {n}.", f"claim-{n}")
    review = dispatch(reviews)

    class Profiled(LabReviewer):
        requested_profile = requested("reviewer")

    await ReviewerRunner(reviews, "p", "s", review, "owner", Profiled()).run()
    with reviews.documents.control.read() as tx:
        calls = tx.sql.execute(
            "SELECT seq,prompt_provenance IS NOT NULL FROM review_calls WHERE review_id=? "
            "ORDER BY seq",
            (review,),
        ).fetchall()
    assert len(calls) > 2
    assert [pinned for _, pinned in calls] == [1] + [0] * (len(calls) - 1)
    first = reviews.call_provenance("p", "s", review, calls[0][0])
    last = reviews.call_provenance("p", "s", review, calls[-1][0])
    assert first.status == last.status == "complete"
    assert first.provenance == last.provenance
    profiles = [
        r["payload"] for r in reviews.records("p", "s", review)["records"] if r["kind"] == "profile"
    ]
    assert profiles == [{"profile": requested("reviewer").model_dump(mode="json")}]


def test_a_review_stored_before_the_final_review_still_loads(store) -> None:
    """A review stored before CR-48 and CR-49, its settings without ``final_review`` and
    its coverage without ``from`` or ``partial``, loads, and counts as covering its
    window from its start; its calls' provenance reads as it did."""
    reviews, _, _ = store
    seqs = [claim(reviews, f"Claim {n}.", f"claim-{n}") for n in range(3)]
    review = dispatch(reviews)
    with reviews.documents.control.write() as tx:
        (config,) = tx.sql.execute(
            "SELECT config FROM review_runs WHERE review_id=?", (review,)
        ).fetchone()
        old = {k: v for k, v in json.loads(config).items() if k != "final_review"}
        # Ended as a review ended then: its coverage record names no first position.
        tx.sql.execute(
            "UPDATE review_runs SET config=?,state='passed',ended=1000,lease_until=NULL "
            "WHERE review_id=?",
            (json.dumps(old), review),
        )
        coverage = {"through": seqs[-1], "end": seqs[-1], "verdict": True, "covered": []}
        tx.sql.execute(
            "INSERT INTO review_records(review_id,kind,payload,created) "
            "VALUES(?,'coverage',?,1000)",
            (review, json.dumps(coverage)),
        )
        tx.sql.execute(
            "UPDATE review_windows SET watermark=? WHERE project='p' AND session='s'",
            (seqs[-1],),
        )
        tx.sql.execute(
            "INSERT INTO review_calls(project,review_id,created,estimated_tokens) "
            "VALUES('p',?,1000,10)",
            (review,),
        )
        (call,) = tx.sql.execute("SELECT max(seq) FROM review_calls").fetchone()
    found = reviews.run("p", "s", review)
    assert found["state"] == "passed"
    assert "final_review" not in found["config"]
    assert ReviewConfig.model_validate(found["config"]).final_review == "after"
    assert reviews.coverage("p", "s") == {
        "reviewed_through": seqs[-1],
        "feed_end": seqs[-1],
        "complete": True,
        "unreviewed": [],
        "unreviewed_ranges": 0,
    }
    assert reviews.listing("p", "s")["reviews"][0]["review_id"] == review
    assert reviews.call_provenance("p", "s", review, call).status == "legacy"


def test_a_platform_recheck_does_not_take_an_undelivered_finding_from_the_agent(store) -> None:
    """Queueing a finding's re-check, or recording one that could not decide, is the
    platform's bookkeeping: a notice no run was given still reaches the agent (the
    cluster run's ``fail`` finding was never delivered after its re-check)."""
    from .test_consistency import raise_finding, reviewed_document, revise

    reviews, _, _ = store
    claim(reviews)
    doc, review, anchors = reviewed_document(reviews, "There are 16.\n", "doc")
    raised = raise_finding(reviews, review, anchors=anchors)
    reviews.finish("p", "s", review, "owner", "findings", "The count is wrong.")
    revise(reviews, doc, "There are 15.\n")
    assert Findings(reviews).queue_rechecks("p", "s") == 1
    notices = FindingNotices(reviews).pending("p", "s", "author-1", set(), raised_for="author-1")
    assert [(n["finding"], n["revision"]) for n in notices] == [(raised["finding_id"], 2)]
