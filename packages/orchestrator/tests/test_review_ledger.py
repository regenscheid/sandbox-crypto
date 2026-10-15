# SPDX-License-Identifier: MIT
"""The reviewer reads the ledger (docs/specs/consistency-review.md, CR-2, CR-9, CR-10):
each revision an agent of the session makes is an observation of its reviews, with the
entry as it reads at that revision; its text and new claims count toward the triggers;
the user's revisions and other sessions' are not the session's to review."""

from __future__ import annotations

import json
from typing import Any

import pytest

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


def test_an_entry_too_large_for_an_item_keeps_its_parts_shortened_and_incomplete(store) -> None:
    """The review of #279: the observation shortened notes and scopes instead of
    dropping the fields, links and evidence, and says it is incomplete."""
    reviews, _, _ = store
    change(reviews, *([{"type": "task", "title": "T", "statement": "s"}] * 32), op="tasks")
    change(reviews, *([{"type": "task", "title": "U", "statement": "s"}] * 32), op="more")
    links = [
        {"kind": "concerns", "entry": f"T-{n}", "note": "\U0001d53d" * 4096} for n in range(1, 65)
    ]
    change(reviews, {"type": "issue", "title": "I", "statement": "s", "links": links}, op="i")
    ingest_ledger(reviews, "p", "s", drain=True)
    with reviews.documents.control.read() as tx:
        payload, complete = tx.sql.execute(
            "SELECT payload,complete FROM review_feed WHERE kind='ledger' "
            "AND json_extract(locator,'$.handle')='I-1'"
        ).fetchone()
    entry = json.loads(payload)
    assert (complete, entry["shortened"], len(entry["links"])) == (0, True, 64)
    assert all(len(link["note"]) <= 201 for link in entry["links"])


# Findings on entries (LG-25, CR-17, CR-28 as amended).


def finding_on(reviews: ReviewStore, field: str | None) -> tuple[str, str]:
    """A fail finding on C-1's revision 1, at ``field``; the review and the finding."""
    from .test_consistency import raise_finding

    ingest_ledger(reviews, "p", "s")
    review = dispatch(reviews)
    pinned = anchor(reviews, review, "ledger")
    found = raise_finding(reviews, review, anchors=[{**pinned, "field": field}])
    reviews.finish("p", "s", review, "owner", "findings", "One finding.")
    return review, found["finding_id"]


def test_a_finding_anchors_to_an_entrys_field_and_the_entry_shows_it(store) -> None:
    reviews, _, _ = store
    change(reviews, CLAIM, op="op-1")
    _, finding = finding_on(reviews, "statement")
    view = Ledger(reviews.documents).entry("p", "C-1")
    assert [(f.finding_id, f.field, f.revision, f.status, f.severity) for f in view.findings] == [
        (finding, "statement", 1, "open", "fail")
    ]
    # A field anchor names a ledger entry revision only.
    from sandbox_science.orchestrator.documents import DocumentError

    from .test_consistency import claim, raise_finding

    claim(reviews, "Another claim.", "m2")
    review = dispatch(reviews)
    message = anchor(reviews, review, "transcript")
    with pytest.raises(DocumentError, match="field anchor needs a ledger entry"):
        raise_finding(
            reviews, review, anchors=[{**message, "field": "statement"}], operation_id="f2"
        )


def superseded(reviews: ReviewStore, finding: str) -> bool:
    from sandbox_science.orchestrator.consistency.findings import Findings

    Findings(reviews).supersede("p", "s")
    return Findings(reviews).get("p", "s", finding)["status"] == "superseded"


def test_a_field_anchor_is_replaced_only_when_that_field_changes(store) -> None:
    reviews, _, _ = store
    change(reviews, CLAIM, op="op-1")
    _, finding = finding_on(reviews, "statement")
    change(reviews, {"entry": "C-1", "expected": 1, "title": "Renamed"}, op="op-2")
    assert not superseded(reviews, finding)
    change(
        reviews, {"entry": "C-1", "expected": 2, "statement": "$a^{16} = 1$ in GF(17)."}, op="op-3"
    )
    assert superseded(reviews, finding)
    from sandbox_science.orchestrator.consistency.findings import Findings

    last = Findings(reviews).get("p", "s", finding)["history"][-1]
    entry = Ledger(reviews.documents).entry("p", "C-1").entry_id
    assert last["payload"]["ledger_replacements"] == [
        {"entry": entry, "handle": "C-1", "revision": 3, "field": "statement"}
    ]


def test_an_anchor_is_replaced_only_by_a_revision_that_changes_the_material(store) -> None:
    """Probe p2 of the review of #280: a whole-entry fail finding was superseded by a
    title-only revision. Only the statement, fields, assumptions and evidence that can
    count are what a finding, a check or a review is about (LG-17)."""
    reviews, _, _ = store
    change(reviews, CLAIM, op="op-1")
    _, finding = finding_on(reviews, None)
    assert not superseded(reviews, finding)
    change(reviews, {"entry": "C-1", "expected": 1, "status": "disputed"}, op="op-2")
    change(reviews, {"entry": "C-1", "expected": 2, "title": "Renamed"}, op="op-3")
    assert not superseded(reviews, finding)
    change(reviews, {"entry": "C-1", "expected": 3, "statement": "$a^{16} = 1$ in GF(17)."}, op="s")
    assert superseded(reviews, finding)


def test_a_field_anchor_off_the_material_is_not_replaced(store) -> None:
    reviews, _, _ = store
    change(reviews, CLAIM, op="op-1")
    _, finding = finding_on(reviews, "title")
    change(reviews, {"entry": "C-1", "expected": 1, "title": "Renamed"}, op="op-2")
    assert not superseded(reviews, finding)


def test_a_version_22_stores_findings_are_found_by_their_entries(store, tmp_path) -> None:
    """Version 23 keeps the entries each finding is anchored to; it fills them from the
    findings an older store holds."""
    from sandbox_science.orchestrator.control import migrations
    from sandbox_science.orchestrator.documents import DocumentStore, store_path

    from .control_versions import older_store

    reviews, documents, _ = store
    change(reviews, CLAIM, op="op-1")
    _, finding = finding_on(reviews, "statement")
    documents.close()
    old = tmp_path / "old"
    old.mkdir()
    older_store(store_path(tmp_path), store_path(old), migrations.BASELINE + 20)
    migrated = DocumentStore(store_path(old))
    migrated.initialize()
    try:
        view = Ledger(migrated).entry("p", "C-1")
        assert [(f.finding_id, f.field) for f in view.findings] == [(finding, "statement")]
    finally:
        migrated.close()
