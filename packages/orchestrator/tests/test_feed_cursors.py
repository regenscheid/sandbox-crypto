# SPDX-License-Identifier: MIT
"""Feed cursors (B-04): the reviewer reads the document and artifact events through a
pinned head in bounded pages, and moves its cursor in the change that writes what it
read, by compare-and-set; version 14 moves both cursors into one table."""

from __future__ import annotations

import sqlite3

import pytest

from sandbox_science.orchestrator.artifacts import ArtifactInventory
from sandbox_science.orchestrator.consistency import artifacts as consumer_module
from sandbox_science.orchestrator.consistency.artifacts import ingest_artifacts
from sandbox_science.orchestrator.consistency.store import ReviewStore
from sandbox_science.orchestrator.control import ControlError
from sandbox_science.orchestrator.control.feeds import FeedCursor
from sandbox_science.orchestrator.documents import Author, DocumentStore, store_path

from .control_versions import FEED_CURSORS, older_store
from .test_artifact_review import dispatch, items, publish
from .test_artifact_review import state as state

AGENT = Author(kind="agent", session_id="s", run_id="author", role="main", model="scripted")


def write(reviews: ReviewStore, name: str) -> None:
    reviews.documents.create(
        "p", name=name, title=name, text=f"{name}.\n", author=AGENT, session_id="s"
    )


def test_a_document_page_that_fails_leaves_its_cursor_and_is_read_again(state, monkeypatch):
    reviews, _, _ = state
    write(reviews, "first")
    original = reviews.record_observation

    def fail(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError("crash before the cursor moves")

    monkeypatch.setattr(reviews, "record_observation", fail)
    with pytest.raises(RuntimeError):
        reviews.ingest_documents("p", "s")
    monkeypatch.setattr(reviews, "record_observation", original)
    target = reviews.ingest_documents("p", "s")["target"]
    assert reviews.ingest_documents("p", "s") == {
        "cursor": target,
        "target": target,
        "complete": True,
    }


def test_a_document_page_another_ingestor_wrote_first_does_not_end_the_read(state, monkeypatch):
    """A second ingestor writes the page this one read before it commits: this one's
    compare-and-set fails, it reads on from the winner's cursor, and it reports the
    target reached rather than its stale cursor."""
    reviews, _, _ = state
    write(reviews, "first")
    other = ReviewStore(reviews.documents)
    read = getattr(reviews, "_document_page")  # noqa: B009 - the race needs the page read
    won: list[dict] = []

    def racing(project: str, session: str, target: int):
        page = read(project, session, target)
        if not won:
            won.append(other.ingest_documents(project, session))
        return page

    monkeypatch.setattr(reviews, "_document_page", racing)
    found = reviews.ingest_documents("p", "s")
    assert won[0]["complete"]
    assert found == {"cursor": won[0]["target"], "target": won[0]["target"], "complete": True}
    review = dispatch(reviews)
    documents = [i for i in items(reviews, review) if i["kind"] == "document"]
    assert len(documents) == 1  # written once, by the winner


def test_an_artifact_page_another_ingestor_wrote_first_does_not_end_the_read(state, monkeypatch):
    reviews, _, _ = state
    publish(state)
    read = getattr(consumer_module, "_read_page")  # noqa: B009 - the race needs the page read
    won: list[dict] = []

    def racing(store, project: str, session: str, target: int):
        page = read(store, project, session, target)
        if not won:
            won.append({})  # the winner reads through this function too
            won[0] = ingest_artifacts(store, project, session)
        return page

    monkeypatch.setattr(consumer_module, "_read_page", racing)
    found = ingest_artifacts(reviews, "p", "s")
    assert won[0]["complete"]
    assert found == won[0]


def test_a_cursor_moves_only_from_where_it_was_read(state):
    reviews, _, _ = state
    feed = FeedCursor("document")
    with reviews.documents.control.write() as tx:
        assert feed.position(tx.sql, "p", "s") == 0
        assert feed.advance(tx.sql, "p", "s", 0, 5)
        assert not feed.advance(tx.sql, "p", "s", 0, 7)  # another reader moved it
        assert feed.advance(tx.sql, "p", "s", 5, 5)
    with reviews.documents.control.read() as tx:
        assert feed.position(tx.sql, "p", "s") == 5
        assert feed.position(tx.sql, "p", "other") == 0


def _read(reviews: ReviewStore) -> tuple[dict, dict]:
    return reviews.ingest_documents("p", "s", drain=True), ingest_artifacts(
        reviews, "p", "s", drain=True
    )


def test_cursors_from_version_13_continue_where_they_were(state, tmp_path):
    reviews, _, _ = state
    write(reviews, "first")
    publish(state)
    before = _read(reviews)
    assert before[0]["cursor"] > 0
    assert before[1]["cursor"] > 0
    old = store_path(tmp_path / "old")
    older_store(reviews.documents.path, old, FEED_CURSORS - 1)
    copied = DocumentStore(old)
    try:
        copied.initialize()  # the writer brings the store to the current version
        moved = ReviewStore(copied, artifacts=ArtifactInventory(copied))
        assert _read(moved) == before
        write(moved, "second")
        documents, _ = _read(moved)
        assert documents["cursor"] == documents["target"] > before[0]["cursor"]
    finally:
        copied.close()


def test_a_cursor_version_14_refuses_leaves_the_store_at_version_13(state, tmp_path):
    reviews, _, _ = state
    write(reviews, "first")
    old = store_path(tmp_path / "old")
    older_store(reviews.documents.path, old, FEED_CURSORS - 1)
    with sqlite3.connect(old) as db:
        db.execute("INSERT INTO review_artifact_cursors VALUES('p','s',-1)")
    copied = DocumentStore(old)
    try:
        with pytest.raises(ControlError, match=r"stays at version 13.*review_artifact_cursors"):
            copied.initialize()
    finally:
        copied.close()
