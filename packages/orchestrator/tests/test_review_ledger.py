# SPDX-License-Identifier: MIT
"""The reviewer reads the ledger (docs/specs/consistency-review.md, CR-2, CR-9, CR-10):
each revision an agent of the session makes is an observation of its reviews, with the
entry as it reads at that revision; its text and new claims count toward the triggers;
the user's revisions and other sessions' are not the session's to review."""

from __future__ import annotations

import json
from typing import Any

from sandbox_science.orchestrator.consistency.ledger import ingest_ledger
from sandbox_science.orchestrator.consistency.store import ReviewStore
from sandbox_science.orchestrator.documents import USER, Author
from sandbox_science.orchestrator.ledger import Ledger

from .test_consistency import MAIN, anchor, dispatch, feed
from .test_consistency import store as store

OTHER = Author(kind="agent", session_id="other", run_id="other-1", role="main")
CLAIM = {"type": "claim", "origin": "project", "title": "Fermat", "statement": "$a^{16}=1$."}


def change(reviews: ReviewStore, *changes: dict[str, Any], op: str, author: Author = MAIN) -> None:
    Ledger(reviews.documents).change(
        "p", {"operation_id": op, "reason": "Recorded.", "changes": list(changes)}, author
    )


def observations(reviews: ReviewStore) -> list[tuple[str, str, int, int, int]]:
    with reviews.documents.control.read() as tx:
        rows = tx.sql.execute(
            "SELECT source_key,kind,characters,structural,effects FROM review_feed "
            "WHERE kind='ledger' ORDER BY seq"
        ).fetchall()
    return [tuple(row) for row in rows]


def test_an_agents_ledger_revisions_are_observed_for_its_sessions_reviews(store) -> None:
    reviews, _, _ = store
    change(reviews, CLAIM, {"type": "task", "title": "Run", "statement": "Run it."}, op="op-1")
    change(reviews, {"entry": "C-1", "expected": 1, "title": "Fermat for 17"}, op="op-2")
    change(reviews, {"entry": "C-1", "expected": 2, "status": "disputed"}, op="op-3")
    change(reviews, {"entry": "T-1", "expected": 1, "title": "Run now"}, op="op-4", author=USER)
    change(reviews, {**CLAIM, "title": "Other"}, op="op-5", author=OTHER)
    assert ingest_ledger(reviews, "p", "s")["complete"]
    entry = Ledger(reviews.documents).entry("p", "C-1").entry_id
    task = Ledger(reviews.documents).entry("p", "T-1").entry_id
    assert observations(reviews) == [
        (f"ledger:{entry}:1", "ledger", len("Fermat") + len("$a^{16}=1$."), 1, 1),
        (f"ledger:{task}:1", "ledger", len("Run") + len("Run it."), 0, 1),
        (f"ledger:{entry}:2", "ledger", len("Fermat for 17"), 0, 1),
        (f"ledger:{entry}:3", "ledger", 0, 0, 1),
    ]
    # The other session's reviews see its own.
    assert ingest_ledger(reviews, "p", "other")["complete"]
    assert len(observations(reviews)) == 5
    # Reading again observes nothing new.
    assert ingest_ledger(reviews, "p", "s")["complete"]
    assert len(observations(reviews)) == 5


def test_the_reviewer_reads_the_entry_as_it_was_at_the_revision(store) -> None:
    reviews, _, _ = store
    change(reviews, CLAIM, op="op-1")
    ingest_ledger(reviews, "p", "s")
    review = dispatch(reviews)
    pinned = anchor(reviews, review, "ledger")
    change(reviews, {"entry": "C-1", "expected": 1, "statement": "Changed."}, op="op-2")
    read = reviews.read_evidence("p", "s", review, pinned["evidence"], owner="owner")
    entry = json.loads(read["text"])
    assert (entry["handle"], entry["revision"], entry["statement"]) == ("C-1", 1, "$a^{16}=1$.")
    assert entry["author"]["run_id"] == "author-1"
    item = reviews.evidence("p", "s", review, pinned["evidence"])
    assert item["locator"] == {
        "entry": entry["entry_id"],
        "handle": "C-1",
        "revision": 1,
        "run": "author-1",
    }
    assert reviews.run_effects("p", "s", "author-1")


def test_ledger_text_counts_toward_the_text_trigger(store) -> None:
    reviews, _, now = store
    change(reviews, {**CLAIM, "statement": "x" * 1990}, op="op-1")
    ingest_ledger(reviews, "p", "s")
    assert reviews.due("p", "s")  # a new claim is a structural block
    review = dispatch(reviews)
    reviews.finish("p", "s", review, "owner", "passed", "Checked.")
    now[0] += 1000
    change(reviews, {"type": "task", "title": "T", "statement": "y" * 1000}, op="op-2")
    ingest_ledger(reviews, "p", "s")
    assert not reviews.due("p", "s")
    change(reviews, {"entry": "T-1", "expected": 1, "statement": "z" * 1200}, op="op-3")
    ingest_ledger(reviews, "p", "s")
    assert reviews.due("p", "s")


def test_ledger_pages_and_their_cursor_commit_together(store, monkeypatch) -> None:
    reviews, _, _ = store
    for batch in range(8):
        change(reviews, *([{"type": "task", "title": "T", "statement": "s"}] * 30), op=f"b{batch}")
    first = ingest_ledger(reviews, "p", "s")
    assert (first["complete"], len(observations(reviews))) == (False, 200)
    assert ingest_ledger(reviews, "p", "s", drain=True)["complete"]
    assert len(observations(reviews)) == 240
    assert len(set(feed(reviews))) == len(feed(reviews))


def test_the_runs_review_controller_reads_the_ledger_at_its_safe_points(store) -> None:
    import asyncio

    from sandbox_science.contracts import ExecutionContext
    from sandbox_science.orchestrator.consistency.controller import ReviewController
    from sandbox_science.orchestrator.projects import ProjectStore

    reviews, documents, _ = store
    context = ExecutionContext(
        project_id="p",
        session_id="s",
        environment_id=ProjectStore(documents.path.parent).get("p").default_environment_id,
    )
    controller = ReviewController(reviews, context, run_id="author-1", model=None, alias=None)
    change(reviews, CLAIM, op="op-1")
    asyncio.run(controller.observe([]))
    assert [row[0].split(":")[0] for row in observations(reviews)] == ["ledger"]
    assert controller.ledger_complete
