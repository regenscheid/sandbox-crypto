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
from typing import Any, ClassVar, cast

import pytest

from sandbox_science.orchestrator.consistency.controller import ReviewController, Reviews
from sandbox_science.orchestrator.consistency.findings import Findings
from sandbox_science.orchestrator.consistency.models import FinalReview, ReviewConfig
from sandbox_science.orchestrator.consistency.notices import FindingNotices
from sandbox_science.orchestrator.consistency.runner import ReviewerRunner
from sandbox_science.orchestrator.consistency.store import ReviewStore
from sandbox_science.orchestrator.hooks import Closing, Notice, RunView, TurnHook
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
    # The dismissal has the one re-check a claimed fix has (CR-43, CR-48).
    kinds = [r["kind"] for r in reviews.listing("p", "s")["reviews"]]
    assert sorted(kinds) == ["confirmation", "terminal"]
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
        assert (final["mode"], final["outcome"], final["finished"]) == ("hold", "capped", False)
        # Ended at its deadline, not dropped: no retry waits, and its window is named.
        run = reviews.run("p", "s", final["review"])
        assert (run["state"], run["cap_reached"]) == ("capped", 1)
        progress = reviews.progress("p", "s")
        assert (progress["retry_end"], progress["watermark"]) == (None, run["end_seq"])
        (left,) = reviews.listing("p", "s")["unreviewed"]
        assert (left["start"], left["end"], left["reason"]) == (
            run["start_seq"],
            run["end_seq"],
            "deadline",
        )
        assert reviewing.task is None
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
    assert final["unreviewed_reasons"] == {"final_budget": 1}
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
        "unreviewed_reasons": {},
        "not_recorded_through": None,
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


class OneMoreTurn(TurnHook):
    """Takes one more turn at the first closing it sees, as a user's message typed during
    the final review does (``UserMessages.closing``); then accepts."""

    name = "user_messages"

    def __init__(self) -> None:
        self.closings = 0

    async def closing(self, run: RunView, answer: str | None) -> Closing:
        self.closings += 1
        if self.closings > 1:
            return Closing()
        return Closing("continue", (Notice(self.name, ("msg-1",), "Also check 17.", None),))


async def test_one_final_review_a_run_and_the_hourly_cap_for_what_follows(store) -> None:
    """A final review that passes, a user's message taken in a later turn, and a second
    answer: no second final review; the later turn's work has an ordinary checkpoint,
    which the hourly calls defer (CR-48)."""
    reviews, _, _ = store
    reviewing = controller(reviews, LabReviewer(), calls_per_hour=1)
    main = ScriptedModel(
        [ModelTurn("The task is complete."), ModelTurn("17 checked as well; the task is done.")]
    )
    async with WorkerClient("http://127.0.0.1:1", "synthetic-token") as client:
        transcript = await run_task(
            main,
            Toolbox(client, context_for(reviews), reviews=reviewing),
            "Check 16.",
            run_id="author-1",
            max_turns=4,
            hooks=[Reviews(reviewing), OneMoreTurn()],
        )
    await reviewing.close()
    assert transcript.final_text == "17 checked as well; the task is done."
    listed = reviews.listing("p", "s")
    (review,) = listed["reviews"]
    assert review["classifier"]["final"]
    assert listed["waiting"]["reason"] == "calls_per_hour"
    final = cast(dict[str, Any], transcript.summaries["reviews"])["final_review"]
    assert (final["answered"], final["review"]) == (False, review["review_id"])


@pytest.mark.parametrize("mode", ["after", "hold"])
async def test_a_failed_answering_turn_keeps_the_answer_given(store, mode: FinalReview) -> None:
    """The answering turn's model call fails: the run keeps the answer the agent gave,
    the record says the run failed, and the summary that the finding was not answered."""
    from sandbox_science.orchestrator.model import ModelError

    reviews, _, _ = store
    reviewing = controller(reviews, FindingReviewer(), mode)
    given = "The cross-check is mutation-tested; the task is complete."

    class FailsSecond:
        calls = 0

        async def complete(self, messages: Sequence[Any], tools: Sequence[Any]) -> ModelTurn:
            self.calls += 1
            if self.calls == 1:
                return ModelTurn(given)
            raise ModelError("synthetic gateway failure")

    async with WorkerClient("http://127.0.0.1:1", "synthetic-token") as client:
        transcript = await run_task(
            cast(Any, FailsSecond()),
            Toolbox(client, context_for(reviews), reviews=reviewing),
            "Check the numbers.",
            run_id="author-1",
            max_turns=4,
            hooks=[Reviews(reviewing)],
        )
    await reviewing.close()
    assert (transcript.stopped, transcript.final_text, transcript.answer) == ("error", None, given)
    final = cast(dict[str, Any], transcript.summaries["reviews"])["final_review"]
    assert (final["answered"], final["answering_failed"]) == (False, True)
    assert final["open"] == {"fail": 1}


def test_a_notice_offered_to_a_live_run_is_not_carried_to_it_again(store) -> None:
    """A run was offered finding F; it edits what F reviewed without responding, and the
    re-check is queued: F is not offered again under its new revision."""
    from .test_consistency import raise_finding, reviewed_document, revise

    reviews, _, _ = store
    claim(reviews)
    doc, review, anchors = reviewed_document(reviews, "There are 16.\n", "doc")
    raised = raise_finding(reviews, review, anchors=anchors)
    reviews.finish("p", "s", review, "owner", "findings", "The count is wrong.")
    notices = FindingNotices(reviews)
    (offered,) = notices.pending("p", "s", "author-1", set(), raised_for="author-1")
    revise(reviews, doc, "There are 15.\n")
    assert Findings(reviews).queue_rechecks("p", "s") == 1
    seen = {offered["notice_id"]}
    assert notices.pending("p", "s", "author-1", seen, raised_for="author-1") == []
    assert Findings(reviews).get("p", "s", raised["finding_id"])["revision"] == 2


async def test_no_checkpoint_runs_in_the_answering_turn(store) -> None:
    """A safe point of the answering turn starts no review, even with one asked for; the
    turn's work is recorded unreviewed (CR-48)."""
    reviews, _, _ = store
    reviewing = controller(reviews, FindingReviewer(), min_interval_seconds=0)
    hook = Reviews(reviewing)
    from sandbox_science.orchestrator.context.agent import AgentContext
    from sandbox_science.orchestrator.loop import Transcript

    answer = {"role": "assistant", "content": "The task is complete."}
    run = Transcript(context=AgentContext(messages=[answer]))
    assert (await hook.closing(run, "The task is complete.")).action == "respond"
    reviews.request("p", "s")
    later = [answer, {"role": "assistant", "content": "Corrected the claim."}]
    await reviewing.safe_point(later)
    assert reviewing.task is None
    run.response_rounds = 1
    run.context.messages.append(later[1])
    assert (await hook.closing(run, "Corrected the claim.")).action == "accept"
    listed = reviews.listing("p", "s")
    assert len(listed["reviews"]) == 1
    (left,) = [u for u in listed["unreviewed"] if u["reason"] == "answering_turn"]
    final = cast(dict[str, Any], hook.summary())["final_review"]
    assert final["answered"]
    assert left["end"] == final["feed_end"]
    await reviewing.close()


async def test_a_later_final_review_takes_up_a_range_left_unreviewed(store) -> None:
    """A range an earlier final review's budget left out is a candidate for the session's
    next final review, after its run's own work; once reviewed it is no longer listed,
    and every position is in a covered or a recorded range (CR-48)."""
    from sandbox_science.orchestrator.consistency.models import FINAL_FEED_ITEMS

    reviews, _, _ = store
    seqs = [claim(reviews, f"Step {n}.", f"step-{n}") for n in range(FINAL_FEED_ITEMS + 5)]
    first = await controller(reviews, LabReviewer()).terminal([], "The task is complete.")
    assert first["final_review"]["unreviewed"] == [{"start": seqs[0], "end": seqs[4]}]
    reviews.observe(
        "p", "s", "later", "transcript", {"run": "author-2", "message": 1},
        {"role": "assistant", "content": "Later work."},
    )  # fmt: skip
    later = ReviewController(
        reviews, context_for(reviews), run_id="author-2", model=LabReviewer(), alias="reviewer"
    )
    summary = await later.terminal([], "The later task is complete.")
    assert summary["final_review"]["complete"]
    assert [u for u in reviews.listing("p", "s")["unreviewed"] if u.get("start")] == []


def test_reviews_before_coverage_records_say_so(store) -> None:
    """A session reviewed before reviews recorded their coverage shows that coverage as
    not recorded, not as unreviewed work (CR-49)."""
    reviews, _, _ = store
    seqs = [claim(reviews, f"Claim {n}.", f"claim-{n}") for n in range(3)]
    review = dispatch(reviews)
    with reviews.documents.control.write() as tx:
        tx.sql.execute(
            "UPDATE review_runs SET state='passed',ended=1000,lease_until=NULL WHERE review_id=?",
            (review,),
        )
    later = claim(reviews, "A later claim.", "later")
    coverage = reviews.coverage("p", "s")
    assert coverage["not_recorded_through"] == seqs[-1]
    assert coverage["unreviewed"] == [{"start": later, "end": later}]


async def test_hold_keeps_the_answer_from_the_chat_until_its_closing_ends() -> None:
    """While a hook holds a closing answer, the run's events say it is being reviewed and
    do not show it; it is written when the closing ends (CR-48)."""
    from sandbox_science.contracts import ExecutionContext
    from sandbox_science.orchestrator.chat import HELD, EventLog

    written: list[tuple[str, dict[str, Any]]] = []

    class Chat:
        context = ExecutionContext(project_id="p", session_id="s", environment_id="env")
        run_id, run, parent, delegation = "r", "run-r", None, None
        message_ids: tuple[str, ...] = ()
        unavailable: ClassVar[dict[str, str]] = {}
        connectors = None
        task = None
        prefix = 2

        async def write(self, kind: str, data: dict[str, Any]) -> None:
            written.append((kind, data))

    def texts() -> list[str]:
        return [str(d.get("text")) for kind, d in written if kind == "assistant"]

    class Holds(TurnHook):
        name = "reviews"

        def __init__(self) -> None:
            self.seen_during: list[str] = []

        @property
        def holds_answers(self) -> bool:
            return True

        async def closing(self, run: Any, answer: str | None) -> Closing:
            self.seen_during = texts()
            assert ("notice", {"source": "reviews", "ids": [], "text": HELD}) in written
            return Closing()

    from sandbox_science.orchestrator.tools import Toolbox as Tools

    holds = Holds()
    transcript = await run_task(
        ScriptedModel([ModelTurn("the held answer")]),
        Tools(cast(Any, object()), Chat.context),
        "go",
        run_id="r",
        hooks=[holds, EventLog(cast(Any, Chat()))],
    )
    assert transcript.final_text == "the held answer"
    assert "the held answer" not in holds.seen_during
    assert texts() == ["the held answer"]


def _post(reviews: ReviewStore, message: str, text: str) -> None:
    from sandbox_science.orchestrator.runstate import RunState

    RunState(reviews.documents.control).post("p", "s", message, text, author="user")


def _request(reviews: ReviewStore, review: str) -> dict[str, Any]:
    items = reviews.manifest("p", "s", review, limit=50)["evidence"]
    (found,) = [i for i in items if i["kind"] == "request"]
    assert not found["new"]
    return reviews.evidence("p", "s", review, found["evidence"])["payload"]


async def test_every_review_is_given_the_users_request_and_its_later_change(store) -> None:
    """The session's first message and a later one that narrows it are context in a
    checkpoint's snapshot and in the final review's (CR-48); they are not new items."""
    from sandbox_science.contracts import new_id

    reviews, _, _ = store
    first, later = new_id("msg"), new_id("msg")
    _post(reviews, first, "Compute the order for n = 16 and n = 32.")
    claim(reviews, "Started on n = 16.", "c1")
    _post(reviews, later, "Only n = 16 is needed now.")
    checkpoint = dispatch(reviews, "checkpoint")
    texts = [m["text"] for m in _request(reviews, checkpoint)["messages"]]
    assert texts == ["Compute the order for n = 16 and n = 32.", "Only n = 16 is needed now."]
    reviews.finish("p", "s", checkpoint, "owner", "passed", "Checked.")
    claim(reviews, "The order for n = 16 is 8.", "c2")
    summary = await controller(reviews, LabReviewer()).terminal([], "The task is complete.")
    asked = _request(reviews, summary["final_review"]["review"])
    assert [m["message"] for m in asked["messages"]] == [first, later]
    assert summary["final_review"]["complete"]


def test_a_long_request_keeps_the_newest_whole_and_the_first(store) -> None:
    from sandbox_science.contracts import new_id
    from sandbox_science.orchestrator.consistency.models import REQUEST_BYTES

    reviews, _, _ = store
    ids = [new_id("msg") for _ in range(6)]
    for n, message in enumerate(ids):
        _post(reviews, message, f"Message {n}: " + "x" * (REQUEST_BYTES // 4))
    claim(reviews)
    asked = _request(reviews, dispatch(reviews, "checkpoint"))
    listed = [m["message"] for m in asked["messages"]]
    assert (listed[0], listed[-1]) == (ids[0], ids[-1])
    assert asked["omitted"] == len(ids) - len(listed) > 0


class FixingReviewer(LabReviewer):
    """A final review raises a fail finding on the document; the re-check after the
    answering turn decides it resolved, anchored to the document's new revision."""

    def __init__(self) -> None:
        super().__init__()
        self.reviews: list[str] = []

    async def complete(self, messages: Sequence[Any], tools: Sequence[Any]) -> ModelTurn:
        opening = json.loads(messages[1]["content"])
        if opening["review"] not in self.reviews:
            self.reviews.append(opening["review"])
            self.calls = 0
        self.calls += 1
        results = [json.loads(m["content"]) for m in messages if m["role"] == "tool"]
        if not results:
            return self.turn([("review_manifest", {})], 100)
        listed = results[0]["evidence"]
        documents = [i for i in listed if i["kind"] == "document"]
        if "final" in opening and len(results) == 1:
            finding = {
                "severity": "fail",
                "category": "contradicting_value",
                "statement": "The document gives 16; the output says 15.",
                "anchors": [{"evidence": documents[-1]["evidence"]}],
                "operation_id": "final-doc-finding",
            }
            return self.turn([("review_finding", finding)], 100)
        if "final" not in opening and len(results) == 1:
            reads = [("review_read", {"evidence": i["evidence"]}) for i in listed[:16]]
            return self.turn(reads, 100)
        if "final" not in opening and len(results) == 1 + min(16, len(listed)):
            (item,) = [i for i in listed if i["kind"] == "finding"]
            newest = max(documents, key=lambda i: i["locator"]["revision"])
            decision = {
                "finding": item["locator"]["finding"],
                "expected": item["locator"]["revision"],
                "decision": "resolved",
                "explanation": "The new revision gives 15.",
                "anchors": [{"evidence": newest["evidence"]}],
                "operation_id": "recheck-resolved",
            }
            checked = {"statement": "The count is 15.", "evidence": [newest["evidence"]]}
            return self.turn([("review_decide", decision), ("review_checked", checked)], 100)
        outcome = "findings" if "final" in opening else "passed"
        return ModelTurn(
            json.dumps({"outcome": outcome, "summary": "Done.", "cap_reached": False}),
            input_tokens=100,
            output_tokens=50,
        )


@pytest.mark.parametrize("mode", ["after", "hold"])
async def test_one_recheck_after_the_answering_turn(store, mode: FinalReview) -> None:
    """The answering turn claims the final review's finding fixed: one re-check of it
    runs, outside the hourly calls, and the summary says resolved; no second answering
    turn follows. In hold the reply stays held until the re-check ends (CR-48)."""
    from sandbox_science.orchestrator.context.agent import AgentContext
    from sandbox_science.orchestrator.loop import Transcript

    from .test_consistency import revise

    reviews, documents, _ = store
    doc = documents.create(
        "p", name="count", title="C", text="There are 16.\n", author=MAIN, session_id="s"
    ).document_id
    reviews.ingest_documents("p", "s")
    reviewing = controller(reviews, FixingReviewer(), mode, calls_per_hour=1)
    hook = Reviews(reviewing)
    async with reviewing.active():
        answer = {"role": "assistant", "content": "The count is 16; the task is complete."}
        run = Transcript(context=AgentContext(messages=[answer]))
        first = await hook.closing(run, "The count is 16; the task is complete.")
        assert first.action == "respond"
        assert hook.holds_answers == (mode == "hold")
        (notice,) = [n for n in first.notices if n.ids]
        (finding,) = cast(list[dict[str, Any]], notice.payload)
        Findings(reviews).respond(
            "p",
            "s",
            {
                "finding": finding["finding"],
                "expected": finding["revision"],
                "action": "fix",
                "explanation": "Corrected the count to 15.",
                "revisions": revise(reviews, doc, "There are 15.\n"),
                "operation_id": "fix-count",
            },
            MAIN,
        )
        run.response_rounds = 1
        second = await hook.closing(run, "Corrected the count to 15.")
        assert second.action == "accept"
    final = cast(dict[str, Any], hook.summary())["final_review"]
    assert (final["answered"], final["rechecked"]) == (True, True)
    assert final["recheck"]["outcome"] == "passed"
    assert [(f["finding"], f["recheck"]) for f in final["findings"]] == [
        (finding["finding"], "resolved")
    ]
    kinds = [r["kind"] for r in reviews.listing("p", "s")["reviews"]]
    assert sorted(kinds) == ["confirmation", "terminal"]
    await reviewing.close()


async def test_a_final_review_may_anchor_a_finding_to_a_file_the_answer_cites(
    store, tmp_path
) -> None:
    """The answer relies on what a script it names says of itself; the final review
    anchors its finding to that file's cited item (CR-48)."""
    from .test_review_actions import published

    reviews, documents, _ = store
    published(documents, tmp_path, "crosscheck.py", b'"""Recomputes every number."""\n')
    claim(reviews)

    class CitedReviewer(LabReviewer):
        raised: Any = None

        async def complete(self, messages: Sequence[Any], tools: Sequence[Any]) -> ModelTurn:
            self.calls += 1
            results = [json.loads(m["content"]) for m in messages if m["role"] == "tool"]
            if not results:
                return self.turn([("review_manifest", {})], 100)
            if len(results) == 1:
                (cited,) = [i for i in results[0]["evidence"] if "cited_by" in i["locator"]]
                finding = {
                    "severity": "fail",
                    "category": "evidence_strength",
                    "statement": "The script compares ranks; it recomputes nothing.",
                    "anchors": [{"evidence": cited["evidence"]}],
                    "operation_id": "cited-finding",
                }
                return self.turn([("review_finding", finding)], 100)
            self.raised = results[-1]
            return ModelTurn(
                json.dumps({"outcome": "findings", "summary": "Done.", "cap_reached": False}),
                input_tokens=100,
                output_tokens=50,
            )

    reviewer = CitedReviewer()
    summary = await controller(reviews, reviewer).terminal(
        [], "crosscheck.py recomputes every number independently; the task is complete."
    )
    assert reviewer.raised["status"] == "open"
    assert summary["final_review"]["open"] == {"fail": 1}


class AcceptanceReviewer(LabReviewer):
    """Reads the final review's acceptances item; a task accepted with files unread whose
    result the answer calls verified draws a warn of category evidence_strength."""

    unread: Any = None

    async def complete(self, messages: Sequence[Any], tools: Sequence[Any]) -> ModelTurn:
        self.calls += 1
        results = [json.loads(m["content"]) for m in messages if m["role"] == "tool"]
        if not results:
            return self.turn([("review_manifest", {})], 100)
        listed = results[0]["evidence"]
        if len(results) == 1:
            (item,) = [i for i in listed if i["kind"] == "acceptances"]
            return self.turn([("review_read", {"evidence": item["evidence"]})], 100)
        if len(results) == 2:
            accepted = json.loads(results[1]["text"])["acceptances"]
            self.unread = [a["unread"] for a in accepted]
            answer = next(i for i in listed if i["new"])
            if any(self.unread):
                finding = {
                    "severity": "warn",
                    "category": "evidence_strength",
                    "statement": "The answer calls the table verified; its task was accepted "
                    "with files unread.",
                    "anchors": [{"evidence": answer["evidence"]}],
                    "operation_id": "accepted-unread",
                }
                return self.turn([("review_finding", finding)], 100)
            checked = {"statement": "The task's files were read.", "evidence": [answer["evidence"]]}
            return self.turn([("review_checked", checked)], 100)
        outcome = "findings" if any(self.unread or []) else "passed"
        return ModelTurn(
            json.dumps({"outcome": outcome, "summary": "Done.", "cap_reached": False}),
            input_tokens=100,
            output_tokens=50,
        )


@pytest.mark.parametrize("read_whole", [False, True])
async def test_a_final_review_sees_a_task_accepted_unread(store, read_whole: bool) -> None:
    """A task accepted with its files unread, whose result the answer presents as verified,
    draws the finding; one read whole before acceptance does not (CR-48, PD-46)."""
    from datetime import UTC, datetime

    from sandbox_science.orchestrator.context.history import Archive
    from sandbox_science.orchestrator.delegation.records import DelegationRecords, Ending
    from sandbox_science.testing import FakeClock

    from . import test_acceptance_reads as reads

    reviews, _, _ = store
    clock = FakeClock(datetime(2026, 11, 26, 12, tzinfo=UTC))
    records = DelegationRecords(reviews.documents.control, clock=clock)
    archive = Archive(reviews.documents.control, clock=clock)
    records.start("p", "s", reads.task())
    records.end("p", "s", reads.DELEGATION, Ending("returned", "answered", "{}",
                result=reads.RESULT, files=reads.FILES))  # fmt: skip
    clock.advance(60)
    if read_whole:
        for sha in (reads.A, reads.B, reads.C):
            reads.read(archive, reads.PARENT, sha)
    records.accept("p", "s", reads.DELEGATION, by=reads.PARENT, reason="parent")
    claim(reviews, "The table is verified.", "answer")
    reviewer = AcceptanceReviewer()
    summary = await controller(reviews, reviewer).terminal([], "The verified table is done.")
    if read_whole:
        assert reviewer.unread == [None]
        assert summary["final_review"]["open"] == {}
    else:
        assert reviewer.unread[0].startswith("accepted unread: ")
        assert summary["final_review"]["open"] == {"warn": 1}


def test_an_accept_action_says_whether_its_files_were_read() -> None:
    from sandbox_science.orchestrator.consistency.actions import _made

    unread = {"accept": {"delegation_id": "dlg-1", "unread": "accepted unread: a.py"}}
    whole = {"accept": {"delegation_id": "dlg-2"}}
    assert _made(unread, {}) == ["accepted delegation dlg-1, accepted unread: a.py"]
    assert _made(whole, {}) == ["accepted delegation dlg-2, its files read whole"]


def _delegated(reviews: ReviewStore) -> None:
    """The run's ``delegate`` call, an action: all the work a run that only delegates does
    itself."""
    reviews.observe(
        "p",
        "s",
        "turn-1:action:2",
        "action",
        {"run": "author-1", "invocation": "turn-1", "message": 2},
        {"tool": "delegate", "call": "call-1", "outcome": "ok", "made": ["delegation dlg-1"]},
    )


async def test_a_run_that_only_delegated_has_a_final_review(store) -> None:
    """The cluster's live test of 2026-11-27: two main runs that delegated tasks and
    accepted them, and ran nothing themselves, ended with no final review, since only a
    run's own executions and files counted as work to review."""
    reviews, _, _ = store
    _delegated(reviews)
    controller = ReviewController(
        reviews, context_for(reviews), run_id="author-1", model=LabReviewer(), alias="reviewer"
    )
    summary = await controller.terminal([], "Delegated as dlg-1; out/result.txt holds line 1.")
    assert summary["classifier"]["effects"]
    assert summary["final_review"]["review"] is not None
    assert "no_final_review" not in summary
    (review,) = reviews.listing("p", "s")["reviews"]
    assert (review["kind"], review["classifier"]["final"]) == ("terminal", True)


async def test_a_run_end_without_a_final_review_says_why(store) -> None:
    """A run that changed nothing and whose answer claims no work has no final review,
    and its end state says so, as it does when the project's reviews are disabled."""
    from sandbox_science.orchestrator.consistency.controller import (
        NOTHING_TO_REVIEW,
        REVIEWS_DISABLED,
    )

    reviews, _, _ = store
    controller = ReviewController(
        reviews, context_for(reviews), run_id="author-1", model=LabReviewer(), alias="reviewer"
    )
    summary = await controller.terminal([], "Which file should I read?")
    assert (summary["no_final_review"], reviews.listing("p", "s")["reviews"]) == (
        NOTHING_TO_REVIEW,
        [],
    )
    reviews.configure("p", "s", ReviewConfig(enabled=False))
    summary = await controller.terminal([], "Which file should I read?")
    assert summary["no_final_review"] == REVIEWS_DISABLED


async def test_a_checkpoint_running_at_the_end_gives_its_window_to_the_final_review(
    store,
) -> None:
    """A checkpoint still running when the run's answer arrives is ended at once, its
    window recorded unreviewed, and the final review covers that window with the newest
    work, rather than waiting a minute for the checkpoint and then discarding it."""
    from sandbox_science.orchestrator.consistency.controller import SUPERSEDED
    from sandbox_science.testing import eventually

    reviews, _, _ = store
    reviews.defaults = ReviewConfig(min_interval_seconds=0)
    seq = claim(reviews)
    reviews.request("p", "s")
    model = DelayedReviewer()
    controller = ReviewController(
        reviews, context_for(reviews), run_id="author-1", model=model, alias="reviewer"
    )
    await controller.safe_point([])
    await model.started.wait()
    (checkpoint,) = reviews.listing("p", "s")["reviews"]
    _delegated(reviews)
    ending = asyncio.create_task(controller.terminal([], "Delegated as dlg-1."))
    try:

        def final() -> list[Any]:
            return [r for r in reviews.listing("p", "s")["reviews"] if r["kind"] == "terminal"]

        # The final review starts while the checkpoint's model call is still unanswered.
        (review,) = await eventually(final, timeout=10)
        ended = reviews.run("p", "s", checkpoint["review_id"])
        assert (ended["state"], ended["summary"]) == ("capped", SUPERSEDED)
        model.release.set()
        summary = await ending
    finally:
        model.release.set()
        await controller.close()
    assert summary["final_review"]["review"] == review["review_id"]
    assert summary["final_review"]["outcome"] == "passed"
    # The checkpoint's window is the final review's to cover: nothing is left unreviewed.
    assert summary["final_review"]["complete"]
    assert reviews.coverage("p", "s")["unreviewed"] == []
    assert reviews.run("p", "s", review["review_id"])["start_seq"] <= seq


def test_an_operator_sets_a_projects_final_review(
    tmp_path, capsys: pytest.CaptureFixture[str]
) -> None:
    """``project review`` shows the project's final review and sets it, the project's own
    over the deployment's; the settings it does not set follow the deployment's."""
    from sandbox_science.orchestrator import cli
    from sandbox_science.orchestrator.documents import DocumentStore, store_path
    from sandbox_science.orchestrator.projects import ProjectStore

    projects = ProjectStore(tmp_path)
    projects.create("p", "minimal", "sha256:" + "a" * 64)
    projects.close()
    argv = ["project", "review", "--state-dir", str(tmp_path), "--project", "p"]
    assert cli.main(argv) == 0
    assert capsys.readouterr().out == (
        "project p: final review the deployment's ([model].final_review; after unless it is "
        "set): after, by default; other review settings: the deployment's\n"
    )
    assert cli.main([*argv, "--final-review", "hold"]) == 0
    assert capsys.readouterr().out == (
        "project p: final review hold, the project's own: the answer is held until the "
        "final review ends; other review settings: the deployment's\n"
    )
    documents = DocumentStore(store_path(tmp_path))
    try:
        reviews = ReviewStore(documents)
        # The deployment's other settings still apply: here, its hourly calls.
        reviews.defaults = ReviewConfig(calls_per_hour=500, final_review="after")
        config, _ = reviews.config("p")
        assert (config.final_review, config.calls_per_hour) == ("hold", 500)
    finally:
        documents.close()
    assert cli.main([*argv, "--final-review", "after"]) == 0
    assert "final review after, the project's own" in capsys.readouterr().out
    assert cli.main([*argv, "--project", "missing"]) == 1


def test_a_stored_review_configuration_still_sets_every_setting(store) -> None:
    """A row ``configure`` wrote, as before projects set some settings of their own, sets
    them all: the deployment's do not show through."""
    reviews, _, _ = store
    reviews.configure("p", "s", ReviewConfig(calls_per_hour=10))
    reviews.defaults = ReviewConfig(calls_per_hour=500, final_review="hold")
    config, _ = reviews.config("p")
    assert (config.calls_per_hour, config.final_review) == (10, "after")


class DismissalReviewer(FixingReviewer):
    """A final review raises a fail finding on the document; the re-check after the
    answering turn decides the author's dismissal of it: accepted or rejected."""

    def __init__(self, decision: str) -> None:
        super().__init__()
        self.decision = decision

    async def complete(self, messages: Sequence[Any], tools: Sequence[Any]) -> ModelTurn:
        opening = json.loads(messages[1]["content"])
        if "final" in opening:
            return await super().complete(messages, tools)
        if opening["review"] not in self.reviews:
            self.reviews.append(opening["review"])
            self.calls = 0
        self.calls += 1
        results = [json.loads(m["content"]) for m in messages if m["role"] == "tool"]
        if not results:
            return self.turn([("review_manifest", {})], 100)
        if len(results) == 1:
            listed = results[0]["evidence"]
            (item,) = [i for i in listed if i["kind"] == "finding"]
            decision = {
                "finding": item["locator"]["finding"],
                "expected": item["locator"]["revision"],
                "decision": self.decision,
                "explanation": "The reason holds." if "accept" in self.decision else "It does not.",
                "anchors": [{"evidence": item["evidence"]}],
                "operation_id": f"recheck-{self.decision}",
            }
            return self.turn([("review_decide", decision)], 100)
        return ModelTurn(
            json.dumps({"outcome": "passed", "summary": "Decided.", "cap_reached": False}),
            input_tokens=100,
            output_tokens=50,
        )


@pytest.mark.parametrize("decision", ["accept_dismissal", "reject_dismissal"])
async def test_a_dismissal_in_the_answering_turn_is_rechecked(store, decision: str) -> None:
    """The answering turn dismisses the final review's fail finding: the one re-check
    after it accepts or rejects the reason (CR-43), and the summary says which."""
    from sandbox_science.orchestrator.context.agent import AgentContext
    from sandbox_science.orchestrator.loop import Transcript

    reviews, documents, _ = store
    documents.create(
        "p", name="count", title="C", text="There are 16.\n", author=MAIN, session_id="s"
    )
    reviews.ingest_documents("p", "s")
    reviewing = controller(reviews, DismissalReviewer(decision), "hold")
    hook = Reviews(reviewing)
    async with reviewing.active():
        answer = {"role": "assistant", "content": "The count is 16; the task is complete."}
        run = Transcript(context=AgentContext(messages=[answer]))
        first = await hook.closing(run, "The count is 16; the task is complete.")
        assert first.action == "respond"
        (notice,) = [n for n in first.notices if n.ids]
        (finding,) = cast(list[dict[str, Any]], notice.payload)
        Findings(reviews).respond(
            "p",
            "s",
            {
                "finding": finding["finding"],
                "expected": finding["revision"],
                "action": "dismiss",
                "explanation": "Sixteen is the count the source gives.",
                "operation_id": "dismiss-count",
            },
            MAIN,
        )
        run.response_rounds = 1
        second = await hook.closing(run, "Sixteen stands; I set the finding aside.")
        assert second.action == "accept"
    final = cast(dict[str, Any], hook.summary())["final_review"]
    assert (final["answered"], final["rechecked"]) == (True, True)
    assert [(f["finding"], f["recheck"]) for f in final["findings"]] == [
        (finding["finding"], "dismissal_accepted" if "accept" in decision else "dismissal_rejected")
    ]
    kinds = [r["kind"] for r in reviews.listing("p", "s")["reviews"]]
    assert sorted(kinds) == ["confirmation", "terminal"]
    await reviewing.close()


async def test_hold_ends_at_its_limit_through_the_answering_turn(store) -> None:
    """``hold``'s limit counts from the final review's start, its answering turn
    included: past it, the hook no longer holds answers (CR-48)."""
    from sandbox_science.orchestrator.context.agent import AgentContext
    from sandbox_science.orchestrator.loop import Transcript

    reviews, documents, _ = store
    documents.create(
        "p", name="count", title="C", text="There are 16.\n", author=MAIN, session_id="s"
    )
    reviews.ingest_documents("p", "s")
    reviews.defaults = ReviewConfig(final_review="hold")
    reviewing = ReviewController(
        reviews,
        context_for(reviews),
        run_id="author-1",
        model=FixingReviewer(),
        alias="reviewer",
        hold_seconds=30,
    )
    hook = Reviews(reviewing)
    async with reviewing.active():
        answer = {"role": "assistant", "content": "The count is 16; the task is complete."}
        run = Transcript(context=AgentContext(messages=[answer]))
        first = await hook.closing(run, "The count is 16; the task is complete.")
        assert (first.action, first.hold_answer) == ("respond", True)
        assert hook.holds_answers
        reviewing.hold_until = asyncio.get_running_loop().time()  # the limit passes
        assert not hook.holds_answers
    await reviewing.close()
