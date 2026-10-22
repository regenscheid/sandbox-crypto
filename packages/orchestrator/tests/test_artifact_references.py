# SPDX-License-Identifier: MIT
"""Reference paging is index-bounded even with a long and highly duplicated history."""

import sqlite3
from contextlib import closing, contextmanager
from types import SimpleNamespace

import pytest

from sandbox_science.orchestrator import artifact_references
from sandbox_science.orchestrator.artifact_blobs import Blob
from sandbox_science.orchestrator.artifact_references import reference_page
from sandbox_science.orchestrator.artifacts import ArtifactInventory
from sandbox_science.orchestrator.control import migrations
from sandbox_science.orchestrator.documents import DocumentStore

from .test_artifact_blobs import install


@pytest.fixture
def indexed(tmp_path, monkeypatch):
    """Use the public page API and real SQLite plans, isolating only store construction."""
    db = sqlite3.connect(tmp_path / "reference-benchmark.sqlite3")
    for table, index in (
        ("artifact_revision_blobs", "artifact_blob_hash"),
        ("user_upload_blobs", "upload_blob_hash"),
    ):
        db.execute(f"CREATE TABLE {table}(sha256 TEXT NOT NULL,size INTEGER NOT NULL)")
        db.execute(f"CREATE INDEX {index} ON {table}(sha256,size)")

    @contextmanager
    def read():
        yield SimpleNamespace(sql=db)

    def opened(path, *, readonly):
        assert readonly
        return SimpleNamespace(control=SimpleNamespace(read=read), close=lambda: None)

    monkeypatch.setattr(artifact_references, "DocumentStore", opened)
    try:
        yield db
    finally:
        db.close()


def test_page_cost_is_independent_of_unrelated_tail_and_duplicate_history(indexed, tmp_path):
    db = indexed
    hashes = [f"{i:064x}" for i in range(1, 4)]
    db.executemany("INSERT INTO artifact_revision_blobs VALUES (?,1)", ((h,) for h in hashes))
    db.execute("INSERT INTO user_upload_blobs VALUES (?,1)", (hashes[0],))
    statements = []

    def measured():
        steps = 0

        def progress():
            nonlocal steps
            steps += 1
            return 0

        db.set_trace_callback(statements.append)
        db.set_progress_handler(progress, 1)
        try:
            assert reference_page(tmp_path / "ignored", limit=2) == tuple(
                Blob(h, 1) for h in hashes[:2]
            )
        finally:
            db.set_progress_handler(None, 0)
            db.set_trace_callback(None)
        return steps

    short = measured()
    db.executemany(
        "INSERT INTO artifact_revision_blobs VALUES (?,1)",
        ((f"{i:064x}",) for i in range(4, 50_004)),
    )
    db.executemany(
        "INSERT INTO artifact_revision_blobs VALUES (?,1)", ((hashes[0],) for _ in range(50_000))
    )
    long = measured()
    print(f"reference-page VM instructions: short={short}, 100000 extra rows={long}")
    assert long < 1000
    assert long < 2 * short + 200
    for statement in set(statements):
        plan = " ".join(str(row[3]) for row in db.execute("EXPLAIN QUERY PLAN " + statement))
        assert "USING COVERING INDEX" in plan
        assert "SCAN " not in plan
        assert "TEMP B-TREE" not in plan


@pytest.mark.parametrize("table", ["artifact_revision_blobs", "user_upload_blobs"])
def test_different_sizes_sharing_hash_fail_without_scanning_duplicates(indexed, tmp_path, table):
    db = indexed
    digest = "a" * 64
    db.executemany(f"INSERT INTO {table} VALUES (?,?)", ((digest, 1), (digest, 2)))
    with pytest.raises(ValueError, match="inconsistent"):
        reference_page(tmp_path / "ignored")


def test_sizes_disagreeing_across_sources_fail(indexed, tmp_path):
    db = indexed
    digest = "a" * 64
    db.execute("INSERT INTO artifact_revision_blobs VALUES (?,1)", (digest,))
    db.execute("INSERT INTO user_upload_blobs VALUES (?,2)", (digest,))
    with pytest.raises(ValueError, match="inconsistent"):
        reference_page(tmp_path / "ignored")


def test_v77_upgrade_preserves_retained_rows_and_only_adds_covering_indexes(tmp_path, monkeypatch):
    path = tmp_path / "control.sqlite3"
    with monkeypatch.context() as old:
        old.setattr(migrations, "MIGRATIONS", migrations.MIGRATIONS[: 77 - migrations.BASELINE + 1])
        old.setattr(migrations, "VERSION", 77)
        with closing(DocumentStore(path)) as documents:
            found = install(ArtifactInventory(documents), b"unchanged").revision
            assert found is not None
            with documents.control.read() as tx:
                before = tx.sql.execute("SELECT * FROM artifact_revision_blobs").fetchall()
    with closing(DocumentStore(path)) as documents, documents.control.read() as tx:
        assert tx.sql.execute("SELECT * FROM artifact_revision_blobs").fetchall() == before
        assert tx.sql.execute("PRAGMA user_version").fetchone() == (migrations.VERSION,)
        names = {
            row[0] for row in tx.sql.execute("SELECT name FROM sqlite_master WHERE type='index'")
        }
        assert {"artifact_blob_hash", "upload_blob_hash"} <= names
    assert reference_page(path) == (Blob(found.sha256, found.size),)
