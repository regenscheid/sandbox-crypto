# SPDX-License-Identifier: MIT
"""A checkpoint review is due when the work changed enough since the last author-work
review was dispatched (CR-10 as amended 2026-11-03), not on every window of a backlog.

A review takes one bounded window from the watermark. Before, the backlog it left made the
next review due at once whenever it held a trigger, so a backlog was reviewed window after
window. Now what it left is pending for the next review, which a later change, a request,
a retry of a window whose review covered nothing, or the terminal boundary brings."""

from __future__ import annotations

import pytest

from sandbox_science.orchestrator.consistency.findings import Findings
from sandbox_science.orchestrator.consistency.models import ReviewConfig

from .test_consistency import MAIN, claim, dispatch, raise_finding, reviewed_document, revise
from .test_consistency import store as store
from .test_consistency_budget import claims


def test_a_review_is_due_on_what_changed_since_the_last_one_not_on_its_backlog(store) -> None:
    """A review takes one bounded window from the watermark. What it left behind is pending
    for the next review, but makes none due by itself: the next is due when the work
    changed enough since this one was dispatched."""
    reviews, _, now = store
    reviews.defaults = ReviewConfig(max_feed_items=4)
    claims(reviews, 10)
    reviews.observe("p", "s", "d1", "document", {"run": "author-1"}, {"v": 1}, characters=2500)
    assert reviews.due("p", "s")
    first = dispatch(reviews, "checkpoint")
    reviews.finish("p", "s", first, "owner", "passed", "Checked.")
    now[0] += 200
    progress = reviews.progress("p", "s")
    assert progress["remaining"]
    assert progress["watermark"] == 4
    # Before: the backlog's document made every window of it due in turn.
    assert not reviews.due("p", "s")
    reviews.observe("p", "s", "d2", "document", {"run": "author-1"}, {"v": 2}, characters=1500)
    assert not reviews.due("p", "s")
    reviews.observe("p", "s", "d3", "document", {"run": "author-1"}, {"v": 3}, characters=500)
    assert reviews.due("p", "s")
    second = reviews.run("p", "s", dispatch(reviews, "checkpoint"))
    assert second["start_seq"] == 5  # it continues the backlog from the watermark


@pytest.mark.parametrize(
    ("observation", "due"),
    [
        ({"artifacts": 2}, False),
        ({"artifacts": 3}, True),
        ({"characters": 1999}, False),
        ({"characters": 2000}, True),
        ({"structural": 1}, True),
        ({"cells": 40}, False),  # within the 30-minute kernel interval
    ],
)
def test_each_threshold_counts_from_the_last_dispatch(
    store, observation: dict[str, int], due: bool
) -> None:
    reviews, _, now = store
    claim(reviews)
    first = dispatch(reviews, "checkpoint")
    reviews.finish("p", "s", first, "owner", "passed", "Checked.")
    now[0] += 200
    for n, (field, count) in enumerate(observation.items()):
        reviews.observe("p", "s", f"o{n}", "document", {}, {"field": field}, **{field: count})
    assert reviews.due("p", "s") is due


def test_kernel_cells_count_after_their_interval(store) -> None:
    reviews, _, now = store
    claim(reviews)
    first = dispatch(reviews, "checkpoint")
    reviews.finish("p", "s", first, "owner", "passed", "Checked.")
    for n in range(40):
        reviews.observe("p", "s", f"cell-{n}", "execution", {}, {"n": n}, cells=1)
    now[0] += 200
    assert not reviews.due("p", "s")
    now[0] += 1800
    assert reviews.due("p", "s")


def test_a_confirmation_does_not_reset_what_changed(store) -> None:
    """Only author-work reviews set where the triggers count from."""
    reviews, _, now = store
    doc, original, anchors = reviewed_document(reviews)
    finding = raise_finding(reviews, original, anchors=anchors)
    reviews.finish("p", "s", original, "owner", "findings", "One finding.")
    reviews.observe("p", "s", "d1", "document", {}, {"v": 1}, characters=2000)
    Findings(reviews).respond(
        "p",
        "s",
        {
            "finding": finding["finding_id"],
            "expected": 1,
            "action": "fix",
            "explanation": "Corrected.",
            "revisions": revise(reviews, doc),
            "operation_id": "fix",
        },
        MAIN,
    )
    confirmation = dispatch(reviews, "confirmation")
    reviews.finish("p", "s", confirmation, "owner", "inconclusive", "Undecided.")
    now[0] += 200
    assert reviews.due("p", "s")
