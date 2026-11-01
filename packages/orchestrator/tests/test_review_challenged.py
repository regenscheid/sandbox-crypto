# SPDX-License-Identifier: MIT
"""The owner's decisions of 2026-10-16 on the response loop (docs/specs/
consistency-review.md, CR-16, CR-21, CR-26 and CR-29 as amended), as the finding
lifecycle of 2026-10-25 reads them (CR-43, CR-47).

A. The author's dismissal, which the next review rejects, stands: both sides are
   recorded, the finding stays dismissed and unresolved for the gate, no further round
   follows, and it is not offered again. (The ``challenged`` status, and the notice that
   told the orchestrator of it, are gone: a rejected dismissal takes their place.)
B. A finding not delivered in its turn reaches the next run as a count with a pointer, not
   as an interruption, and the run goes on with its own task.
"""

from __future__ import annotations

from typing import Any

import pytest

from sandbox_science.orchestrator.consistency.controller import ReviewController, Reviews
from sandbox_science.orchestrator.consistency.findings import Findings
from sandbox_science.orchestrator.consistency.models import ReviewConfig
from sandbox_science.orchestrator.consistency.notices import FindingNotices
from sandbox_science.orchestrator.consistency.store import ReviewStore
from sandbox_science.orchestrator.ledger import gate
from sandbox_science.orchestrator.loop import Transcript, run_task
from sandbox_science.orchestrator.model import ScriptedModel
from sandbox_science.orchestrator.notices import REVIEW_COUNTS, REVIEW_FINDINGS
from sandbox_science.orchestrator.tools import Toolbox
from sandbox_science.orchestrator.worker_client import WorkerClient

from .test_consistency import (
    MAIN,
    claim,
    context_for,
    dispatch,
    raise_finding,
    read_confirmation,
)
from .test_document_math import ScriptedChecker, drain
from .test_document_math import stores as stores

DISMISSAL = "The output says 7031, as the document does."
REASON = "The cited output prints 7033, not 7031: the dismissal misreads it."


async def rejected(reviews: ReviewStore) -> tuple[str, str]:
    """A finding raised on author-1's work, dismissed by the author with a reason, and the
    dismissal rejected by the next review: its ID and that review's."""
    claim(reviews)
    first = dispatch(reviews)
    finding = raise_finding(reviews, first)["finding_id"]
    reviews.finish("p", "s", first, "owner", "findings", "One finding.")
    Findings(reviews).respond(
        "p", "s",
        {"finding": finding, "expected": 1, "action": "dismiss",
         "explanation": DISMISSAL, "operation_id": "dismiss"},
        MAIN,
    )  # fmt: skip
    await drain(reviews, ScriptedChecker("accepted"))
    claim(reviews, "Another message.", "m2")
    second = dispatch(reviews)
    read_confirmation(reviews, second)
    pinned = next(
        i["evidence"]
        for i in reviews.manifest("p", "s", second, limit=50)["evidence"]
        if i["kind"] == "finding" and i["locator"]["finding"] == finding
    )
    Findings(reviews).decide(
        "p", "s", second, "owner",
        {"finding": finding, "expected": 2, "decision": "reject_dismissal",
         "explanation": REASON, "anchors": [{"evidence": pinned}], "operation_id": "decide"},
    )  # fmt: skip
    reviews.finish("p", "s", second, "owner", "findings", "The dismissal was rejected.")
    return finding, second


async def one_answer(reviews: ReviewStore, run: str) -> Transcript:
    """A run of ``run`` with the review hook that answers once; reviews are off so that
    none is dispatched, and a second model call, a response round, would fail it."""
    reviews.configure("p", "s", ReviewConfig(enabled=False))
    controller = ReviewController(reviews, context_for(reviews), run_id=run, model=None,
                                  alias="reviewer")  # fmt: skip
    async with WorkerClient("http://127.0.0.1:1", "synthetic-token") as client:
        return await run_task(
            ScriptedModel.from_json([{"text": "Here is the task's answer."}]),
            Toolbox(client, context_for(reviews), reviews=controller),
            "Do the task.",
            run_id=run,
            max_turns=3,
            hooks=[Reviews(controller)],
        )


def said(transcript: Transcript, framing: str) -> list[str]:
    return [
        str(m["content"])
        for m in transcript.context.messages
        if m.get("role") == "user" and str(m.get("content", "")).startswith(framing)
    ]


@pytest.mark.asyncio
async def test_a_rejected_dismissal_stands_with_no_new_round(stores: Any) -> None:
    """A: both sides are recorded, the finding stays dismissed rather than reopened and
    unresolved for the gate, no re-flag is counted, the review's record counts the
    rejection, and a later review's match is kept for the record without reopening it."""
    _, reviews, _ = stores
    finding, second = await rejected(reviews)
    found = Findings(reviews).get("p", "s", finding)
    assert (found["status"], found["reflags"]) == ("dismissed", 0)
    assert (found["dismissal"]["by"], found["dismissal"]["reason"]) == ("agent", DISMISSAL)
    assert found["dismissal"]["review"]["decision"] == "rejected"
    assert found["dismissal"]["review"]["reason"] == REASON
    assert not gate.resolved(found["status"], found["dismissal"])
    assert reviews.run("p", "s", second)["dismissals_rejected"] == 1
    history = [(t["status"], t["actor_kind"], t["reason"]) for t in found["history"]]
    assert history[-2] == ("dismissed", "agent", DISMISSAL)
    assert history[-1] == ("dismissed", "controller", REASON)
    # A later review raising the same finding adds to its record, and opens no round.
    claim(reviews, "A third message.", "m3")
    third = dispatch(reviews)
    assert raise_finding(reviews, third, operation_id="again")["outcome"] == "response_pending"
    listed = Findings(reviews).listing("p", "s")["findings"]
    assert [(f["finding_id"], f["status"]) for f in listed] == [(finding, "dismissed")]
    # Nothing asks the author to respond: the run answers once and is done.
    transcript = await one_answer(reviews, "author-1")
    assert (transcript.stopped, transcript.final_text) == ("answered", "Here is the task's answer.")
    assert not said(transcript, REVIEW_FINDINGS.text)


@pytest.mark.asyncio
async def test_a_rejected_dismissal_is_not_offered_again_and_the_list_tool_shows_it(
    stores: Any,
) -> None:
    """A: a later run is given neither the rejected dismissal nor a count of it, and
    delivers nothing; the author's list tool returns it among the dismissed findings, with
    the reviewer's reason."""
    _, reviews, _ = stores
    finding, _ = await rejected(reviews)
    transcript = await one_answer(reviews, "author-2")
    assert not said(transcript, REVIEW_FINDINGS.text)
    assert not said(transcript, REVIEW_COUNTS.text)
    assert not transcript.delivered_ids("reviews")
    assert FindingNotices(reviews).counts("p", "s") == {}
    controller = ReviewController(
        reviews, context_for(reviews), run_id="author-2", model=None, alias=None
    )
    _, opened = await controller.author_call("review_findings", {}, "op")
    assert opened["result"]["findings"] == []
    _, dismissed = await controller.author_call("review_findings", {"status": "dismissed"}, "op")
    (shown,) = dismissed["result"]["findings"]
    assert shown["finding_id"] == finding
    assert shown["dismissal"]["review"]["reason"] == REASON


@pytest.mark.asyncio
async def test_undelivered_findings_arrive_as_a_count_and_the_run_does_its_task(
    stores: Any,
) -> None:
    """B: an open finding a review of an earlier run raised, which no run was given,
    reaches the next run as a count with a pointer to review_findings, which says it may
    follow it up and nothing requires it; the run answers its own task, and the finding
    stays open for the user and later runs."""
    _, reviews, _ = stores
    claim(reviews)
    review = dispatch(reviews)  # a review of author-1's work
    finding = raise_finding(reviews, review)["finding_id"]
    reviews.finish("p", "s", review, "owner", "findings", "One finding.")
    transcript = await one_answer(reviews, "author-2")
    assert (transcript.stopped, transcript.final_text) == ("answered", "Here is the task's answer.")
    assert not said(transcript, REVIEW_FINDINGS.text)
    (counts,) = said(transcript, REVIEW_COUNTS.text)
    assert '"fail:open":1' in counts.replace(" ", "")
    assert "review_findings" in REVIEW_COUNTS.text
    assert "nothing requires it" in REVIEW_COUNTS.text
    assert not transcript.delivered_ids("reviews")
    assert Findings(reviews).get("p", "s", finding)["status"] == "open"
    # The run whose work the finding is on is given it, to respond to.
    mine = FindingNotices(reviews).pending("p", "s", "author-1", set(), raised_for="author-1")
    assert [n["finding"] for n in mine] == [finding]


@pytest.mark.asyncio
async def test_the_digests_open_items_leave_out_the_dismissed_finding(stores: Any) -> None:
    """A: the state block's digest lists the session's open and stale review findings
    among its open items (CM-16's seventh section, CR-41); a dismissal that a review
    rejected stands and is not among them, while an open finding is."""
    from sandbox_science.orchestrator.digest import Digest
    from sandbox_science.orchestrator.ledger.store import Ledger

    docs, reviews, _ = stores
    finding, _ = await rejected(reviews)
    claim(reviews, "A fourth message.", "m4")
    later = dispatch(reviews)
    kept = raise_finding(reviews, later, category="method", operation_id="kept")["finding_id"]
    sections = Digest(Ledger(docs), findings=Findings(reviews)).sections("p", "s")
    (items,) = [s for s in sections if s.key == "open"]
    assert not [line.text for line in items.lines if finding in line.text]
    (line,) = [line.text for line in items.lines if kept in line.text]
    assert "fail review finding, open" in line
    assert "review_findings" in items.hint
