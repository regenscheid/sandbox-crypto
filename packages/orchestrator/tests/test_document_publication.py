# SPDX-License-Identifier: MIT
"""Atomic publication intents, complete exports and immutable scoped content."""

from __future__ import annotations

import json
import sqlite3
import tracemalloc
from pathlib import Path

import pytest

from sandbox_science.orchestrator.documents import (
    USER,
    Author,
    DocumentError,
    DocumentStore,
    publication,
)
from sandbox_science.orchestrator.documents.publication import Publications


def make(store: DocumentStore, name: str = "count", *, session: str | None = None) -> str:
    return store.create(
        "p",
        name=name,
        title=name,
        text="# Count\n\nCount 15.\n",
        author=USER,
        session_id=session,
        operation_id=f"create-{name}",
    ).document_id


def save(store: DocumentStore, document: str, *, operation: str = "save", base: int = 1) -> None:
    store.save_text(
        "p",
        document,
        base=base,
        text=f"# Count\n\nCount {base + 15}.\n",
        author=USER,
        reason="synthetic correction",
        operation_id=operation,
    )


def test_atomic_change_failure_and_exact_retry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    store = DocumentStore(tmp_path / "control.sqlite3")
    document = make(store)
    exporter = Publications(store)
    original = publication.record_event

    def fail(db: sqlite3.Connection, kind: str, doc: str, event: int) -> None:
        original(db, kind, doc, event)
        raise sqlite3.OperationalError("synthetic publication failure")

    with monkeypatch.context() as patcher:
        patcher.setattr(publication, "record_event", fail)
        with pytest.raises(sqlite3.OperationalError, match="synthetic publication failure"):
            save(store, document)
    assert store.document("p", document).head == 1
    assert exporter.plan("p").snapshot.catalog_seq == 1
    with sqlite3.connect(store.path) as db:
        assert db.execute("SELECT count(*) FROM requests WHERE operation_id='save'").fetchone() == (
            0,
        )
        assert db.execute("SELECT count(*) FROM revisions").fetchone() == (1,)
        assert db.execute("SELECT count(*) FROM publication_events").fetchone() == (1,)
    save(store, document)
    save(store, document)
    assert store.document("p", document).head == 2
    assert exporter.plan("p", after=1).snapshot.catalog_seq == 2
    store.close()


@pytest.mark.parametrize(
    "author",
    [
        USER,
        Author(kind="agent", session_id="s", run_id="run", role="main"),
        Author(kind="import", user="user", import_id="import"),
        Author(kind="platform", service="synthetic"),
    ],
)
def test_all_source_authors_publish_one_initial_revision(tmp_path: Path, author: Author):
    store = DocumentStore(tmp_path / "control.sqlite3")
    doc = store.create("p", name="count", title="count", text="Count 15.", author=author)
    plan = Publications(store).plan("p", after=0)
    assert plan.mode == "delta"
    assert plan.delta is not None
    assert [(c.catalog_seq, c.entry.revision) for c in plan.delta.changes] == [(1, 1)]
    assert plan.entries[0].document_id == doc.document_id
    store.close()


def test_promotion_uses_updated_metadata_without_revision_or_fake_tombstone(tmp_path: Path):
    store = DocumentStore(tmp_path / "control.sqlite3")
    document = make(store, session="s")
    exporter = Publications(store)
    original = exporter.plan("p").entries[0]
    assert original.path == "documents/sessions/s/count.md"
    store.promote(
        "p", document, base=1, name="result", author=USER, reason="promote", operation_id="promote"
    )
    promoted = exporter.plan("p", after=1)
    assert promoted.delta is not None
    assert len(promoted.delta.changes) == 1
    entry = promoted.delta.changes[0].entry
    assert entry.document_id == document
    assert entry.revision == 1
    assert not entry.deleted
    assert entry.path == "documents/result.md"
    assert entry.session_id is None
    assert entry.content_sha256 == original.content_sha256
    assert exporter.content("p", original) == exporter.content("p", entry)
    store.close()


def test_tombstone_name_reuse_and_pinned_bytes_after_delete(tmp_path: Path):
    store = DocumentStore(tmp_path / "control.sqlite3")
    document = make(store)
    exporter = Publications(store)
    pinned = exporter.plan("p").entries[0]
    store.delete("p", document, author=USER, reason="remove", operation_id="delete")
    replacement = store.create(
        "p", name="count", title="new", text="New result.", author=USER, operation_id="replacement"
    ).document_id
    plan = exporter.plan("p", after=1)
    assert plan.delta is not None
    assert [
        (c.catalog_seq, c.entry.document_id, c.entry.revision, c.entry.deleted)
        for c in plan.delta.changes
    ] == [(2, document, 2, True), (3, replacement, 1, False)]
    assert [e.document_id for e in plan.entries] == [replacement]
    assert exporter.content("p", pinned) == b"# Count\n\nCount 15.\n"
    with pytest.raises(DocumentError) as denied:
        exporter.content("other", pinned)
    assert denied.value.kind == "not_found"
    store.close()


def test_eviction_preserves_quiet_cursor_and_forces_complete_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(publication, "TOTAL_EVENTS", 2)
    monkeypatch.setattr(publication, "EVENTS_PER_PROJECT", 2)
    store = DocumentStore(tmp_path / "control.sqlite3")
    document = make(store)
    for n in range(3):
        store.create("other", name=f"doc{n}", title="other", text="Other.", author=USER)
    exporter = Publications(store)
    plan = exporter.plan("p", after=0)
    assert plan.snapshot.catalog_seq == 1
    assert plan.mode == "snapshot"
    assert plan.entries[0].document_id == document
    assert exporter.plan("p", after=1).mode == "current"
    with pytest.raises(DocumentError, match="ahead"):
        exporter.plan("p", after=2)
    with sqlite3.connect(store.path) as db:
        assert db.execute("SELECT count(*) FROM publication_events").fetchone() == (2,)
        assert db.execute("SELECT seq FROM publication_projects WHERE project='p'").fetchone() == (
            1,
        )
    store.close()


def test_snapshot_pins_old_bytes_and_delta_keeps_every_repeated_change(tmp_path: Path):
    store = DocumentStore(tmp_path / "control.sqlite3")
    document = make(store)
    exporter = Publications(store)
    before = exporter.plan("p")
    save(store, document)
    save(store, document, operation="second", base=2)
    assert before.snapshot.catalog_seq == 1
    assert before.entries[0].revision == 1
    assert exporter.content("p", before.entries[0]).endswith(b"Count 15.\n")
    after = exporter.plan("p", after=1)
    assert after.delta is not None
    assert [(c.catalog_seq, c.entry.revision) for c in after.delta.changes] == [(2, 2), (3, 3)]
    assert after.entries[0].revision == 3
    store.close()


def test_legacy_bootstrap_is_atomic_and_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    path = tmp_path / "control.sqlite3"
    store = DocumentStore(path)
    document = make(store)
    store.delete("p", document, author=USER, reason="remove")
    make(store, "other")
    store.close()
    with sqlite3.connect(path) as db:
        for name in ("events", "current", "projects", "meta"):
            db.execute(f"DROP TABLE publication_{name}")
    original = publication._append

    def fail(db: sqlite3.Connection, document: str, event: int | None) -> None:
        original(db, document, event)
        raise sqlite3.OperationalError("synthetic bootstrap interruption")

    with monkeypatch.context() as patcher:
        patcher.setattr(publication, "_append", fail)
        with pytest.raises(sqlite3.OperationalError, match="bootstrap interruption"):
            DocumentStore(path).initialize()
    with sqlite3.connect(path) as db:
        assert (
            db.execute("SELECT name FROM sqlite_master WHERE name='publication_meta'").fetchone()
            is None
        )
    store = DocumentStore(path)
    plan = Publications(store).plan("p")
    assert plan.snapshot.catalog_seq == 2
    assert len(plan.entries) == 1
    assert Publications(store).plan("p", after=0).mode == "snapshot"
    save(store, plan.entries[0].document_id)
    assert Publications(store).plan("p", after=2).mode == "delta"
    plan = Publications(store).plan("p")
    store.close()
    reopened = DocumentStore(path)
    assert Publications(reopened).plan("p").snapshot == plan.snapshot
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT count(*) FROM publication_current").fetchone() == (2,)
        assert db.execute("SELECT count(*) FROM publication_events").fetchone() == (3,)
    reopened.close()


def test_legacy_deleted_only_baseline_requires_empty_snapshot(tmp_path: Path):
    path = tmp_path / "control.sqlite3"
    store = DocumentStore(path)
    document = make(store)
    store.delete("p", document, author=USER, reason="remove")
    store.close()
    with sqlite3.connect(path) as db:
        for name in ("events", "current", "projects", "meta"):
            db.execute(f"DROP TABLE publication_{name}")
    reader = DocumentStore(path, readonly=True)
    with pytest.raises(DocumentError, match="initialize a writer"):
        Publications(reader).plan("p", after=0)
    reader.close()
    store = DocumentStore(path)
    plan = Publications(store).plan("p", after=0)
    assert plan.mode == "snapshot"
    assert plan.snapshot.catalog_seq == 1
    assert plan.entries == ()
    assert plan.delta is None
    store.close()


def test_delta_page_gap_uses_whole_snapshot(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(publication, "MAX_CATALOG_PAGE", 2)
    store = DocumentStore(tmp_path / "control.sqlite3")
    document = make(store)
    for revision in range(1, 4):
        save(store, document, operation=f"save-{revision}", base=revision)
    plan = Publications(store).plan("p", after=0)
    assert plan.mode == "snapshot"
    assert plan.snapshot.catalog_seq == 4
    assert plan.entries[0].revision == 4
    recent = Publications(store).plan("p", after=2)
    assert recent.delta is not None
    assert [c.catalog_seq for c in recent.delta.changes] == [3, 4]
    store.close()


@pytest.mark.parametrize("change", ["revision", "path", "extra", "deleted"])
def test_mixed_old_writer_discrepancy_never_exports(tmp_path: Path, change: str):
    store = DocumentStore(tmp_path / "control.sqlite3")
    document = make(store, session="s")
    with sqlite3.connect(store.path) as db:
        if change == "revision":
            db.execute("UPDATE documents SET head=2 WHERE document_id=?", (document,))
        elif change == "path":
            db.execute(
                "UPDATE documents SET session_id=NULL,name='moved' WHERE document_id=?", (document,)
            )
        elif change == "deleted":
            db.execute("UPDATE documents SET deleted=1 WHERE document_id=?", (document,))
        else:
            db.execute("DELETE FROM publication_current WHERE document=?", (document,))
    with pytest.raises(DocumentError, match="differs"):
        Publications(store).plan("p", after=0)
    store.close()


def test_overflow_and_corrupt_content_refuse_without_partial_export(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    store = DocumentStore(tmp_path / "control.sqlite3")
    document = make(store)
    make(store, "second")
    exporter = Publications(store)
    pinned = next(e for e in exporter.plan("p").entries if e.document_id == document)
    monkeypatch.setattr(publication, "MAX_CATALOG_ENTRIES", 1)
    with pytest.raises(DocumentError, match="unavailable"):
        exporter.plan("p")
    # Worker export caps do not veto otherwise valid canonical writes.
    make(store, "third")
    with sqlite3.connect(store.path) as db:
        db.execute("DROP TRIGGER revisions_no_update")
        db.execute("UPDATE revisions SET text='Changed bytes' WHERE document_id=?", (document,))
    with pytest.raises(DocumentError, match="differ"):
        exporter.content("p", pinned)
    store.close()


@pytest.mark.parametrize("table", ["publication_current", "publication_events", "revisions"])
def test_corrupted_large_rows_are_bounded_before_fetch(tmp_path: Path, table: str):
    store = DocumentStore(tmp_path / "control.sqlite3")
    make(store)
    exporter = Publications(store)
    pinned = exporter.plan("p").entries[0]
    with sqlite3.connect(store.path) as db:
        if table == "revisions":
            db.execute("DROP TRIGGER revisions_no_update")
            db.execute("UPDATE revisions SET text=?", ("x" * (20 * 1024 * 1024),))
        elif table == "publication_current":
            db.execute("UPDATE publication_current SET entry=?", ("x" * (20 * 1024 * 1024),))
        else:
            db.execute("UPDATE publication_events SET entry=?", ("x" * (20 * 1024 * 1024),))
    tracemalloc.start()
    try:
        if table == "revisions":
            with pytest.raises(DocumentError):
                exporter.content("p", pinned)
        else:
            with pytest.raises(DocumentError):
                exporter.plan("p", after=0)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    # A 20MiB corrupt row cannot become a whole Python string/bytes object.
    # SQLite page/cache memory is separate from this logical export bound.
    assert peak < 4 * 1024 * 1024
    store.close()


def test_readonly_missing_schema_and_invalid_exact_cursors(tmp_path: Path):
    missing = DocumentStore(tmp_path / "missing.sqlite3", readonly=True)
    with pytest.raises(DocumentError, match="unavailable"):
        Publications(missing).plan("p")
    assert not missing.path.exists()
    store = DocumentStore(tmp_path / "control.sqlite3")
    make(store)
    for value in (True, 1.0, -1):
        with pytest.raises(DocumentError, match="invalid publication cursor"):
            Publications(store).plan("p", after=value)  # type: ignore[arg-type]
    with sqlite3.connect(store.path) as db:
        raw = db.execute("SELECT entry FROM publication_current").fetchone()[0]
        assert json.loads(raw)["revision"] == 1
    store.close()
