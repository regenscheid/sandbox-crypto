# SPDX-License-Identifier: MIT
"""Copied numbers (docs/specs/consistency-review.md, CR-39; lab 39's B6): before the
model reviews a snapshot, the platform compares each long number its ledger entries and
document blocks state with the numbers its artifacts hold, and records a fail finding for
one that nearly matches but does not."""

from __future__ import annotations

import json
from typing import Any

import pytest

from sandbox_science.orchestrator.consistency.ledger import ingest_ledger
from sandbox_science.orchestrator.consistency.numbers import copied_numbers, near, numbers
from sandbox_science.orchestrator.consistency.runner import ReviewerRunner
from sandbox_science.orchestrator.model import ScriptedModel

from .ledger_support import AnchoredLedger as Ledger
from .test_artifact_review import dispatch, publish
from .test_artifact_review import state as state
from .test_consistency import MAIN

Q = (
    "1817096810739017226373309519720011335884103401718295150703725497951460"
    "03961539585716195755291692375963310293709091662304773755859649779"
)
"""Ed448's q, 135 digits."""
TWIST = (
    "4547246901850635874351807109181611651597300416074609244516940066400666"
    "01040713159573164826466104591839396347161046166404341655226023049"
)
"""A 135-digit number as the twist's q' stands in this test."""
COPIED = TWIST[:40] + TWIST[43:100] + "340" + TWIST[100:]
"""The twist's q', copied by hand as lab 39's agent did: three digits dropped, and three
others put in further on."""


def test_long_numbers_are_read_through_their_grouping() -> None:
    assert numbers(r"$q' = 4547\,2469\,0185 \allowbreak 06$ and 12,345,678,901, not 123") == [
        "4547246901850" + "6",
        "12345678901",
    ]
    assert near(COPIED, {Q, TWIST}) == (TWIST, 6)
    assert near(TWIST, {Q, TWIST}) is None  # copied right
    assert near(TWIST[:-1] + "8", {TWIST}) is None  # a neighbour, such as q' - 1
    assert near(str(2**446), {Q}) is None  # another number altogether


def made(state: Any) -> Any:
    """The results JSON another session's run published; R-1, a result the main agent
    records with the twist's q' copied wrong in its statement, pinning the JSON; and a
    document whose block b2 states it wrong too, and whose b3 states q right."""
    reviews, _, source = state
    context = source.context.model_copy(update={"session_id": "other"})
    other = source.model_copy(update={"context": context, "run_id": "other-run"})
    data = json.dumps({"q": Q, "q_twist": TWIST}, indent=1).encode()
    results = publish(state, "res", data, "application/json", "results/ed448.json", other)
    pin = {"role": "supports", "type": "exact_computation", "artifact": results.artifact_id,
           "revision": results.revision}  # fmt: skip
    result = {"type": "result", "origin": "project", "title": "Twist order",
              "statement": f"The twist has order $4q'$ with $q' = {COPIED}$.",
              "evidence": [pin]}  # fmt: skip
    Ledger(reviews.documents).change(
        "p", {"operation_id": "r", "reason": "r", "changes": [result]}, MAIN
    )
    grouped = r"\,".join(COPIED[i : i + 5] for i in range(0, len(COPIED), 5))
    text = f"# Ed448\n\n$q' = {grouped}$\n\nAnd $q = {Q}$.\n"
    reviews.documents.create("p", name="ed448", title="Ed448", text=text, author=MAIN)
    reviews.ingest_documents("p", "s")
    assert ingest_ledger(reviews, "p", "s")["complete"]
    return results


def test_a_copied_number_that_is_a_few_digits_off_is_a_fail_finding(state) -> None:
    """B6 (lab 39): the document and R-1 held a q' three digits short, and no review
    compared it with the computation."""
    reviews, _, _ = state
    made(state)
    review = dispatch(reviews)
    found = copied_numbers(reviews, "p", "s", review, "owner")
    assert len(found) == 2
    (on_entry,) = [f["statement"] for f in found if "R-1's statement" in f["statement"]]
    (on_block,) = [f["statement"] for f in found if "): block b" in f["statement"]]
    assert "(CR-39): R-1's statement states a 135-digit number" in on_entry
    assert "sessions/other/results/ed448.json, at `q_twist`," in on_entry
    assert f"Stated: {COPIED}. In the artifact: {TWIST}." in on_entry
    assert f"Stated: {COPIED}." in on_block
    with reviews.documents.control.read() as tx:
        rows = tx.sql.execute(
            "SELECT payload FROM review_records WHERE review_id=? AND kind='finding'", (review,)
        ).fetchall()
    assert len(rows) == 2
    # Run again, it records nothing new.
    copied_numbers(reviews, "p", "s", review, "owner")
    with reviews.documents.control.read() as tx:
        again = tx.sql.execute(
            "SELECT count(*) FROM review_records WHERE review_id=? AND kind='finding'", (review,)
        ).fetchone()[0]
    assert again == 2


@pytest.mark.asyncio
async def test_the_reviewer_is_told_of_the_platforms_findings_first(state) -> None:
    """CR-39: the check runs before the model, which is told what it recorded."""
    reviews, _, _ = state
    made(state)
    review = dispatch(reviews)
    finished = {"outcome": "findings", "summary": "The platform found copied numbers."}
    model = ScriptedModel.from_json([{"text": json.dumps(finished)}])
    await ReviewerRunner(reviews, "p", "s", review, "owner", model).run()
    opening = json.loads(model.requests[0][1]["content"])
    assert len(opening["platform_findings"]["findings"]) == 2
    assert reviews.run("p", "s", review)["state"] == "findings"
