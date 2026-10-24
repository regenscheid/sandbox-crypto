# SPDX-License-Identifier: MIT
"""The worker store's version: made, migrated once, refused unchanged when it must be."""

from __future__ import annotations

import contextlib
import hashlib
import os
import shutil
import sqlite3
import subprocess
import sys
import textwrap
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from sandbox_science.worker import schema
from sandbox_science.worker.errors import WorkerError
from sandbox_science.worker.store import Store


def pragmas(path: Path) -> tuple[int, int]:
    with contextlib.closing(sqlite3.connect(path)) as db:
        return (
            int(db.execute("PRAGMA user_version").fetchone()[0]),
            int(db.execute("PRAGMA application_id").fetchone()[0]),
        )


def files(path: Path) -> dict[str, str | None]:
    """The store's files, by suffix, with their SHA-256; None where one is missing."""
    found: dict[str, str | None] = {}
    for suffix in ("", "-wal", "-shm", "-journal"):
        file = Path(f"{path}{suffix}")
        found[suffix] = hashlib.sha256(file.read_bytes()).hexdigest() if file.exists() else None
    return found


@contextlib.contextmanager
def raw(path: Path) -> Iterator[sqlite3.Connection]:
    with contextlib.closing(sqlite3.connect(path, isolation_level=None)) as db:
        yield db


def refused_unchanged(path: Path, refusal: str) -> None:
    before = files(path)
    with pytest.raises(WorkerError, match=refusal):
        Store(path)
    assert files(path) == before


def test_a_new_store_is_made_at_the_latest_version(tmp_path: Path) -> None:
    path = tmp_path / "worker.sqlite3"
    Store(path).close()
    assert pragmas(path) == (schema.latest(), schema.APPLICATION_ID)
    with raw(path) as db:
        before = list(db.iterdump())
    Store(path).close()
    with raw(path) as db:
        assert list(db.iterdump()) == before


def test_an_unversioned_store_is_refused_unchanged(tmp_path: Path) -> None:
    """Decision 6: a store from before version 1, with its tables but no version, is not
    taken over; nor is any other SQLite file."""
    path = tmp_path / "worker.sqlite3"
    with raw(path) as db:
        db.execute("CREATE TABLE executions (execution_id TEXT PRIMARY KEY)")
    refused_unchanged(path, "not a worker store of version 1 or later")


def test_a_file_that_is_not_sqlite_is_refused_unchanged(tmp_path: Path) -> None:
    path = tmp_path / "worker.sqlite3"
    path.write_bytes(b"this is not a database\n" * 400)
    refused_unchanged(path, "is not a worker store")


@pytest.mark.parametrize(
    ("version", "application", "refusal"),
    [(99, schema.APPLICATION_ID, "newer than"), (1, 0x1234, "not a worker store")],
)
def test_a_newer_or_foreign_store_is_refused_unchanged(
    tmp_path: Path, version: int, application: int, refusal: str
) -> None:
    path = tmp_path / "worker.sqlite3"
    Store(path).close()
    with raw(path) as db:
        db.execute(f"PRAGMA user_version={version}")
        db.execute(f"PRAGMA application_id={application}")
    refused_unchanged(path, refusal)
    assert pragmas(path) == (version, application)


def test_a_newer_store_with_a_crashed_writers_log_is_refused_unchanged(tmp_path: Path) -> None:
    """The check reads through the write-ahead log without replaying or indexing it."""
    live = tmp_path / "live" / "worker.sqlite3"
    live.parent.mkdir()
    Store(live).close()
    with raw(live) as writer:
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.execute(f"PRAGMA user_version={schema.latest() + 1}")
        crashed = tmp_path / "crashed" / "worker.sqlite3"
        crashed.parent.mkdir()
        shutil.copy(live, crashed)
        shutil.copy(f"{live}-wal", f"{crashed}-wal")
    assert Path(f"{crashed}-wal").stat().st_size > 0
    refused_unchanged(crashed, "newer than")


def test_a_store_with_delegated_run_executions_is_refused_unchanged(tmp_path: Path) -> None:
    path = tmp_path / "worker.sqlite3"
    Store(path).close()
    with raw(path) as db:
        db.execute(
            "INSERT INTO executions VALUES ('exe-run', 'op-run', 'd', 'env1', 's1', 0, '{}',"
            " 'run-v1')"
        )
    refused_unchanged(path, "delegated-run executions")


def test_a_migration_runs_once_and_a_failing_one_changes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "worker.sqlite3"
    Store(path).close()
    shipped = list(schema.MIGRATIONS)
    runs: list[int] = []

    def add_notes(db: sqlite3.Connection) -> None:
        runs.append(1)
        db.execute("CREATE TABLE notes (note TEXT NOT NULL)")

    def failing(db: sqlite3.Connection) -> None:
        db.execute("CREATE TABLE half (x TEXT)")
        raise RuntimeError("a step that fails")

    monkeypatch.setattr(schema, "MIGRATIONS", [*shipped, add_notes])
    Store(path).close()
    Store(path).close()
    assert runs == [1]
    version = schema.BASELINE + len(shipped) + 1
    assert pragmas(path) == (version, schema.APPLICATION_ID)
    with raw(path) as db:
        before = list(db.iterdump())
    monkeypatch.setattr(schema, "MIGRATIONS", [*shipped, add_notes, failing])
    with pytest.raises(RuntimeError, match="a step that fails"):
        Store(path)
    with raw(path) as db:
        assert list(db.iterdump()) == before
    assert pragmas(path) == (version, schema.APPLICATION_ID)


@pytest.mark.slow
def test_a_migration_runs_once_when_two_workers_open_the_store_together(
    tmp_path: Path,
) -> None:
    """The writer decides from the version it reads inside its write transaction."""
    path = tmp_path / "worker.sqlite3"
    Store(path).close()
    code = textwrap.dedent(
        f"""
        import os, sqlite3, time
        from pathlib import Path
        from sandbox_science.worker import schema
        from sandbox_science.worker.store import Store

        def step(db: sqlite3.Connection) -> None:
            db.execute("CREATE TABLE IF NOT EXISTS runs (pid INTEGER)")
            db.execute("INSERT INTO runs VALUES (?)", (os.getpid(),))
            time.sleep(1.0)  # hold the write lock while the other worker looks

        schema.MIGRATIONS.append(step)
        Store(Path({str(path)!r})).close()
        """
    )
    first = subprocess.Popen([sys.executable, "-c", code], env=dict(os.environ))
    time.sleep(0.4)
    second = subprocess.Popen([sys.executable, "-c", code], env=dict(os.environ))
    assert [first.wait(30), second.wait(30)] == [0, 0]
    with raw(path) as db:
        assert db.execute("SELECT count(*) FROM runs").fetchone()[0] == 1
    assert pragmas(path)[0] == schema.latest() + 1


def test_a_row_the_checks_refuse_leaves_the_store_at_its_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Version 2 checks the admission tables' values; a store holding a row they refuse
    stays at version 1, and the message names the table."""
    path = tmp_path / "worker.sqlite3"
    shipped = list(schema.MIGRATIONS)
    monkeypatch.setattr(schema, "MIGRATIONS", [])
    Store(path).close()
    with raw(path) as db:
        db.execute(
            "INSERT INTO package_execution_gates VALUES ('env1', 1, 'sideways', '', NULL, '')"
        )
        before = list(db.iterdump())
    monkeypatch.setattr(schema, "MIGRATIONS", shipped)
    with pytest.raises(
        WorkerError, match=r"stays at version 1.*package_execution_gates"
    ) as refused:
        Store(path)
    # It names the row and says what to do with it; the rebuild's own table name stays out.
    assert "environment_id='env1'" in refused.value.message
    assert "repair or delete that row" in refused.value.message
    assert "__new" not in refused.value.message
    with raw(path) as db:
        assert list(db.iterdump()) == before
    assert pragmas(path) == (schema.BASELINE, schema.APPLICATION_ID)


def test_a_rebuild_keeps_the_tables_triggers_and_the_views_and_triggers_that_name_it(
    tmp_path: Path,
) -> None:
    path = tmp_path / "worker.sqlite3"
    Store(path).close()
    with raw(path) as db:
        db.execute("CREATE TABLE audit (note TEXT)")
        db.execute(
            "CREATE TRIGGER status_audit AFTER INSERT ON package_status"
            " BEGIN INSERT INTO audit VALUES (NEW.environment_id); END"
        )
        db.execute(
            "CREATE TRIGGER audit_status AFTER INSERT ON audit"
            " BEGIN SELECT count(*) FROM package_status; END"
        )
        db.execute("CREATE VIEW marked AS SELECT environment_id FROM package_status")
        db.execute("BEGIN")
        schema.rebuild(
            db,
            "package_status",
            "CREATE TABLE package_status (environment_id TEXT PRIMARY KEY, status TEXT NOT NULL,"
            " provision_id TEXT NOT NULL, revision INTEGER) STRICT",
        )
        db.execute("COMMIT")
        names = {
            str(row[0])
            for row in db.execute("SELECT name FROM sqlite_master WHERE type IN ('trigger','view')")
        }
        # The store's own triggers (immutable job manifests and progress) are other tables'.
        own = {
            "job_manifests_immutable_update",
            "job_manifests_immutable_delete",
            "job_progress_immutable",
        }
        assert names - own == {"status_audit", "audit_status", "marked"}
        db.execute("INSERT INTO package_status VALUES ('env1','needs_recovery','prv-first',1)")
        assert db.execute("SELECT note FROM audit").fetchall() == [("env1",)]
        assert db.execute("SELECT environment_id FROM marked").fetchall() == [("env1",)]


def test_an_identifier_with_a_nul_is_refused_when_written(tmp_path: Path) -> None:
    path = tmp_path / "worker.sqlite3"
    Store(path).close()
    with raw(path) as db, pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
        db.execute(
            "INSERT INTO package_status VALUES ('env1', 'needs_recovery', ?, 1)",
            ("prv-first\x00tail",),
        )
