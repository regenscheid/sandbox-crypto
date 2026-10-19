# SPDX-License-Identifier: MIT
"""Original uploads survive schema upgrades and retain storage authority and bounds."""

import sqlite3
from pathlib import Path

import pytest

from sandbox_science.orchestrator.control import migrations
from sandbox_science.orchestrator.documents import DocumentError, DocumentStore
from sandbox_science.orchestrator.projects import ProjectStore
from sandbox_science.orchestrator.uploads import UserUploads


def prepared(path: Path) -> DocumentStore:
    projects = ProjectStore(path)
    projects.create("p1", "minimal", "sha256:" + "a" * 64)
    projects.open_session("p1", "s1", dedicated=False)
    projects.create("p2", "minimal", "sha256:" + "a" * 64)
    projects.open_session("p2", "s1", dedicated=False)
    projects.close()
    return DocumentStore(path / "control.sqlite3")


def test_schema_66_upgrade_preserves_originals_ids_constraints_and_triggers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with monkeypatch.context() as old:
        old.setattr(
            migrations,
            "MIGRATIONS",
            migrations.MIGRATIONS[: migrations.MIGRATIONS.index(migrations._larger_user_uploads)],
        )
        old.setattr(migrations, "VERSION", 66)
        documents = prepared(tmp_path)
        uploads = UserUploads(documents)
        original = uploads.create("p1", "s1", "old", "old.pdf", "application/pdf", b"original")
        documents.close()
    documents = DocumentStore(tmp_path / "control.sqlite3")
    try:
        uploads = UserUploads(documents)
        assert uploads.get("p1", "s1", "old") == original
        assert uploads.content("p1", "s1", "old") == b"original"
        assert (
            uploads.create("p1", "s1", "old", "old.pdf", "application/pdf", b"original") == original
        )
        larger = uploads.create(
            "p1", "s1", "large", "large.pdf", "application/pdf", b"x" * (9 * 1024 * 1024)
        )
        assert larger.number == original.number + 1
        for action in ("UPDATE user_uploads SET content=content", "DELETE FROM user_uploads"):
            with (
                pytest.raises(sqlite3.IntegrityError, match="immutable"),
                documents.control.write() as tx,
            ):
                tx.sql.execute(action)
        with pytest.raises(sqlite3.IntegrityError, match="CHECK"), documents.control.write() as tx:
            tx.sql.execute(
                "INSERT INTO user_uploads VALUES "
                "(3,'p1','s1','over','d','{}',104857601,zeroblob(104857601))"
            )
        assert uploads.content("p1", "s1", "old") == b"original"
    finally:
        documents.close()


def test_project_and_global_quotas_remain_atomic_and_allow_exact_replays(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import sandbox_science.orchestrator.uploads as limits

    monkeypatch.setattr(limits, "MAX_PROJECT_BYTES", 10)
    monkeypatch.setattr(limits, "MAX_TOTAL_BYTES", 15)
    documents = prepared(tmp_path)
    try:
        uploads = UserUploads(documents)
        first = uploads.create("p1", "s1", "first", "a.pdf", "application/pdf", b"a" * 10)
        with pytest.raises(DocumentError, match="quota"):
            uploads.create("p1", "s1", "second", "b.pdf", "application/pdf", b"b")
        uploads.create("p2", "s1", "first", "a.pdf", "application/pdf", b"c" * 5)
        with pytest.raises(DocumentError, match="quota"):
            uploads.create("p2", "s1", "second", "b.pdf", "application/pdf", b"b")
        assert uploads.create("p1", "s1", "first", "a.pdf", "application/pdf", b"a" * 10) == first
        assert len(uploads.list("p1", "s1").uploads) == 1
    finally:
        documents.close()
