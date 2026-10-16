# SPDX-License-Identifier: MIT
"""The owner's decisions of 2026-10-16 on the response loop (docs/specs/
consistency-review.md, CR-16, CR-21, CR-26 and CR-29 as amended).

A. A rebuttal that cites recorded evidence, which the reviewer rejects, leaves its finding
   challenged: both sides are recorded, the agent's rebuttal stands, no further round
   follows, and the orchestrator is told, as context.
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
from sandbox_science.orchestrator.loop import Transcript, run_task
from sandbox_science.orchestrator.model import ScriptedModel
from sandbox_science.orchestrator.notices import (
    REVIEW_CHALLENGED,
    REVIEW_COUNTS,
    REVIEW_FINDINGS,
)
from sandbox_science.orchestrator.tools import Toolbox
from sandbox_science.orchestrator.worker_client import WorkerClient

from .test_consistency import (
    MAIN,
    anchor,
    claim,
    context_for,
    dispatch,
    raise_finding,
    read_confirmation,
)
from .test_document_math import ScriptedChecker, drain
from .test_document_math import stores as stores

REASON = "The cited output prints 7033, not 7031: the rebuttal misreads it."


async def challenged(reviews: ReviewStore) -> str:
    """A finding raised on author-1's work, rebutted with recorded evidence, and the
    rebuttal rejected by the next review: its ID."""
    claim(reviews)
    first = dispatch(reviews)
    finding = raise_finding(reviews, first)["finding_id"]
    reviews.finish("p", "s", first, "owner", "findings", "One finding.")
    Findings(reviews).respond(
        "p", "s",
        {"finding": finding, "expected": 1, "action": "rebut",
         "explanation": "The output says 7031, as the document does.",
         "evidence": [anchor(reviews, first)], "operation_id": "rebut"},
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
    Findings(reviews).confirm(
        "p", "s", second, "owner",
        {"finding": finding, "expected": 2, "decision": "reject_rebuttal",
         "explanation": REASON, "anchors": [{"evidence": pinned}], "operation_id": "decide"},
    )  # fmt: skip
    reviews.finish("p", "s", second, "owner", "findings", "The rebuttal was rejected.")
    return finding


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
        for m in transcript.messages
        if m.get("role") == "user" and str(m.get("content", "")).startswith(framing)
    ]


@pytest.mark.asyncio
async def test_a_rejected_rebuttal_with_evidence_is_challenged_with_no_new_round(
    stores: Any,
) -> None:
    """A: both sides are recorded, the finding is challenged rather than reopened, no
    re-flag is counted, and a later review's match is kept for the record without
    reopening it."""
    _, reviews, _ = stores
    finding = await challenged(reviews)
    found = Findings(reviews).get("p", "s", finding)
    assert (found["status"], found["reflags"]) == ("challenged", 0)
    history = [(t["status"], t["reason"]) for t in found["history"]]
    assert history[-2][0] == "open"  # the rebuttal, with its explanation
    assert history[-1] == ("challenged", REASON)
    # A later review raising the same finding adds to its record, and opens no round.
    claim(reviews, "A third message.", "m3")
    third = dispatch(reviews)
    raise_finding(reviews, third, operation_id="again")
    listed = Findings(reviews).listing("p", "s")["findings"]
    assert [(f["finding_id"], f["status"]) for f in listed] == [(finding, "challenged")]
    # Nothing asks the author to respond: the run answers once and is done.
    transcript = await one_answer(reviews, "author-1")
    assert (transcript.stopped, transcript.final_text) == ("answered", "Here is the task's answer.")
    assert not said(transcript, REVIEW_FINDINGS.text)


@pytest.mark.asyncio
async def test_the_orchestrator_sees_the_challenge(stores: Any) -> None:
    """A: a later run is told, as context, which finding was challenged and the reviewer's
    reason, and its counts name the challenged finding; the notice is among what the run
    delivered, to be acknowledged once its record is saved, like any other."""
    _, reviews, _ = stores
    finding = await challenged(reviews)
    transcript = await one_answer(reviews, "author-2")
    (notice,) = said(transcript, REVIEW_CHALLENGED.text)
    assert finding in notice
    assert REASON in notice
    (counts,) = said(transcript, REVIEW_COUNTS.text)
    assert '"fail:challenged":1' in counts.replace(" ", "")
    assert set(transcript.delivered_ids("reviews")) == {f"{finding}-r3"}


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
async def test_the_digests_open_items_name_the_challenged_finding(stores: Any) -> None:
    """A: the state block's digest lists the session's unresolved review findings among
    its open items (CM-16's seventh section), a challenged one saying the rebuttal
    stands."""
    from sandbox_science.orchestrator.digest import Digest
    from sandbox_science.orchestrator.ledger.store import Ledger

    docs, reviews, _ = stores
    finding = await challenged(reviews)
    sections = Digest(Ledger(docs), findings=Findings(reviews)).sections("p", "s")
    (items,) = [s for s in sections if s.key == "open"]
    (line,) = [line.text for line in items.lines if finding in line.text]
    assert "challenged (the reviewer rejected your rebuttal; it stands" in line
    assert "review_findings" in items.hint
