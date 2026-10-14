# SPDX-License-Identifier: MIT
"""The event log (B-04): the documents' and the reviewer's events, committed with their
changes; version 13 moves both into one table, keeping the documents' numbers, which the
reviewer's document cursor holds."""

from __future__ import annotations

import gzip
import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from sandbox_science.orchestrator.consistency.store import ReviewStore
from sandbox_science.orchestrator.control import ControlError, migrations
from sandbox_science.orchestrator.documents import USER, DocumentStore, store_path

from .control_versions import EVENT_LOG, older_store
from .test_consistency import claim, dispatch, raise_finding
from .test_consistency import store as store
from .test_document_math import MAIN

FIXTURE = Path(__file__).parent / "fixtures" / "control_v9"


def history(reviews: ReviewStore, documents: DocumentStore, now: list[float]) -> None:
    """Document and comment events, and a review whose window fails three times."""
    documents.create("p", name="notes", title="N", text="A report.\n", author=MAIN, session_id="s")
    documents.create("p", name="user", title="U", text="Mine.\n", author=USER)
    claim(reviews)
    review = dispatch(reviews)
    raise_finding(reviews, review)
    reviews.finish("p", "s", review, "owner", "failed", "Reviewer unavailable.")
    retry = dispatch(reviews)
    now[0] += 121
    assert reviews.recover("p", "s") == 1
    assert reviews.run("p", "s", retry)["state"] == "dropped"
    third = dispatch(reviews)
    reviews.finish("p", "s", third, "owner", "failed", "Third failure.")


def answers(reviews: ReviewStore, documents: DocumentStore) -> dict[str, Any]:
    events = [event.model_dump(mode="json") for event in documents.events("p")]
    return {
        "events": events,
        "paged": [e.seq for e in documents.events("p", after=events[0]["seq"], limit=1)],
        "unreviewed": reviews.listing("p", "s")["unreviewed"],
        "ingested": reviews.ingest_documents("p", "s", drain=True),
    }


def test_the_documents_events_are_theirs_alone(store):
    reviews, documents, now = store
    history(reviews, documents, now)
    found = answers(reviews, documents)
    assert [e["kind"] for e in found["events"]] == [
        "document.created",
        "document.revision_created",
    ] * 2
    assert found["paged"] == [found["events"][1]["seq"]]
    assert [u["failures"] for u in found["unreviewed"]] == [3]
    assert found["ingested"]["complete"]


def test_events_from_version_12_answer_as_before_and_continue(store, tmp_path):
    reviews, documents, now = store
    history(reviews, documents, now)
    before = answers(reviews, documents)
    old = store_path(tmp_path / "old")
    older_store(documents.path, old, EVENT_LOG - 1)
    copied = DocumentStore(old)
    try:
        copied.initialize()  # the writer brings the store to the current version
        copied_reviews = ReviewStore(copied, clock=lambda: now[0])
        assert answers(copied_reviews, copied) == before
        last = before["events"][-1]["seq"]
        copied.create("p", name="later", title="L", text="Later.\n", author=MAIN, session_id="s")
        *_, later = copied.events("p", after=last)
        ingested = copied_reviews.ingest_documents("p", "s", drain=True)
        assert ingested == {"cursor": later.seq, "target": later.seq, "complete": True}
        assert later.seq > max(e["seq"] for e in before["events"])
    finally:
        copied.close()


def test_an_event_version_13_refuses_leaves_the_store_at_version_12(store, tmp_path):
    reviews, documents, now = store
    history(reviews, documents, now)
    old = store_path(tmp_path / "old")
    older_store(documents.path, old, EVENT_LOG - 1)
    with sqlite3.connect(old) as db:
        db.execute(
            "INSERT INTO review_events(project,session,kind,payload,created) "
            "VALUES('p','s','review.ended','{}',1000.0)"
        )
    copied = DocumentStore(old)
    try:
        with pytest.raises(ControlError, match=r"stays at version 12.*review_events"):
            copied.initialize()
    finally:
        copied.close()
    with sqlite3.connect(old) as db:
        assert db.execute("PRAGMA user_version").fetchone() == (EVENT_LOG - 1,)


def test_a_version_9_store_from_the_code_before_keeps_its_events(tmp_path):
    """The events of the version-9 store main wrote (fixtures/control_v9) keep their
    numbers, kinds, times and payloads; the reviewer's are each about their review or
    finding, in their order."""
    path = store_path(tmp_path)
    with sqlite3.connect(path) as db:
        db.executescript(gzip.decompress((FIXTURE / "control-v9.sql.gz").read_bytes()).decode())
        db.execute(f"PRAGMA application_id = {migrations.APPLICATION_ID}")
        db.execute(f"PRAGMA user_version = {migrations.BASELINE + 7}")
        documents_before = [
            (seq, time, kind, producer, project, session, document, json.loads(payload))
            for seq, time, kind, producer, project, session, document, payload in db.execute(
                "SELECT seq,time,kind,producer,project_id,session_id,document_id,payload "
                "FROM events ORDER BY seq"
            )
        ]
        reviews_before = [
            (project, session, kind, json.loads(payload))
            for project, session, kind, payload in db.execute(
                "SELECT project,session,kind,payload FROM review_events ORDER BY seq"
            )
        ]
    assert documents_before
    assert reviews_before
    docs = DocumentStore(path)
    try:
        docs.initialize()
        documents_after = [
            (
                e.seq,
                e.time.isoformat(),
                e.kind,
                e.producer,
                e.project_id,
                e.session_id,
                e.document_id,
                e.payload,
            )
            for e in docs.events("p", limit=100_000)
        ]
    finally:
        docs.close()
    assert documents_after == documents_before
    with sqlite3.connect(path) as db:
        reviews_after = [
            (project, session, kind, json.loads(payload), subject_type, subject)
            for project, session, kind, payload, subject_type, subject in db.execute(
                "SELECT project,session,kind,payload,subject_type,subject_id FROM control_events "
                "WHERE subject_type<>'document' ORDER BY seq"
            )
        ]
    assert [row[:4] for row in reviews_after] == reviews_before
    for *_, kind, payload, subject_type, subject in reviews_after:
        named = "finding" if kind.startswith("finding.") else "review"
        assert (subject_type, subject) == (named, payload[named])
