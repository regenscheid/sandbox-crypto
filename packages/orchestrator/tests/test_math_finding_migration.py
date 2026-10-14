# SPDX-License-Identifier: MIT
"""Populated finding-origin migration preserves immutable history and USER CAS."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from sandbox_science.orchestrator.consistency import finding_schema
from sandbox_science.orchestrator.consistency import store as storage
from sandbox_science.orchestrator.consistency.findings import Findings
from sandbox_science.orchestrator.consistency.store import ReviewStore
from sandbox_science.orchestrator.documents import USER, DocumentStore, store_path
from sandbox_science.orchestrator.projects import ProjectStore

LEGACY = """CREATE TABLE review_findings (
 finding_id TEXT PRIMARY KEY,project TEXT NOT NULL,session TEXT NOT NULL,review_id TEXT NOT NULL,
 severity TEXT NOT NULL,category TEXT NOT NULL,statement TEXT NOT NULL,anchors TEXT NOT NULL,
 remedy TEXT,status TEXT NOT NULL,revision INTEGER NOT NULL,reflags INTEGER NOT NULL DEFAULT 0,
 dedupe TEXT NOT NULL,supersedes TEXT,created REAL NOT NULL) STRICT"""
HEADER = (
    "finding-aaaaaaaaaaaaaaaaaaaaaaaa",
    "p",
    "s",
    "review-bbbbbbbbbbbbbbbbbbbbbbbb",
    "warn",
    "contradicting_value",
    "Old assessment.",
    "[]",
    None,
    "claimed fixed",
    7,
    2,
    "dedupe",
    None,
    100.0,
)


def legacy(tmp_path: Path) -> DocumentStore:
    projects = ProjectStore(tmp_path)
    projects.create("p", "minimal", "sha256:" + "a" * 64)
    projects.open_session("p", "s", dedicated=False)
    docs = DocumentStore(store_path(tmp_path))
    with docs.control.write() as tx:
        db = tx.sql
        for sql in storage.SCHEMA:
            db.execute(LEGACY if sql == finding_schema.FINDING_TABLE else sql)
        db.execute(
            "INSERT INTO review_findings(rowid,"
            + ",".join(finding_schema.LEGACY_FIELDS)
            + ") VALUES(42,"
            + ",".join("?" for _ in HEADER)
            + ")",
            HEADER,
        )
        db.execute(
            "INSERT INTO review_transitions"
            "(finding_id,revision,status,actor,reason,payload,created)"
            " "
            "VALUES('finding-aaaaaaaaaaaaaaaaaaaaaaaa',7,'claimed fixed','user:user',"
            "'Original response.','{}',100)"
        )
        db.execute(
            "INSERT INTO review_notices(finding_id,revision,project,session) "
            "VALUES('finding-aaaaaaaaaaaaaaaaaaaaaaaa',7,'p','s')"
        )
    return docs


def test_populated_legacy_readonly_and_writer_migration_preserves_identity(tmp_path: Path) -> None:
    docs = legacy(tmp_path)
    reader = DocumentStore(docs.path, readonly=True)
    try:
        result = Findings(ReviewStore(reader)).get("p", "s", "finding-aaaaaaaaaaaaaaaaaaaaaaaa")
        assert result["origin"] == "review"
        assert result["source"] == {"kind": "review", "review": "review-bbbbbbbbbbbbbbbbbbbbbbbb"}
        assert result["revision"] == 7
        assert len(result["history"]) == 1
    finally:
        reader.close()
    reviews = ReviewStore(docs)
    with docs.control.read() as tx:
        db = tx.sql
        assert db.execute(
            "SELECT rowid," + ",".join(finding_schema.LEGACY_FIELDS) + " FROM review_findings"
        ).fetchone() == (42, *HEADER)
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
        assert db.execute("PRAGMA defer_foreign_keys").fetchone() == (0,)
        assert db.execute("SELECT count(*) FROM review_transitions").fetchone()[0] == 1
    response = {
        "finding": "finding-aaaaaaaaaaaaaaaaaaaaaaaa",
        "expected": 7,
        "action": "confirm",
        "explanation": "Explicit current USER decision.",
        "operation_id": "confirm",
    }
    assert Findings(reviews).respond("p", "s", response, USER)["revision"] == 8
    from sandbox_science.orchestrator.documents import DocumentError

    with pytest.raises(DocumentError, match="changed"):
        Findings(reviews).respond("p", "s", {**response, "operation_id": "stale"}, USER)
    with docs.control.write() as tx, pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
        tx.sql.execute(
            "DELETE FROM review_findings WHERE finding_id='finding-aaaaaaaaaaaaaaaaaaaaaaaa'"
        )
    ReviewStore(docs)
    docs.close()


def test_migration_failure_rolls_old_parent_history_and_schema_back(
    tmp_path: Path, monkeypatch
) -> None:
    docs = legacy(tmp_path)
    monkeypatch.setattr(finding_schema, "FINDING_INDEX", "THIS IS NOT SQL")
    with pytest.raises(sqlite3.OperationalError):
        ReviewStore(docs)
    with docs.control.read() as tx:
        db = tx.sql
        assert len(db.execute("PRAGMA table_info(review_findings)").fetchall()) == 15
        assert db.execute("SELECT * FROM review_findings").fetchone() == HEADER
        assert db.execute("SELECT revision FROM review_transitions").fetchall() == [(7,)]
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
        assert (
            db.execute(
                "SELECT name FROM sqlite_master WHERE name='review_findings_with_origin'"
            ).fetchone()
            is None
        )
    docs.close()
