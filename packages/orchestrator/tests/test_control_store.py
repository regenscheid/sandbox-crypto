# SPDX-License-Identifier: MIT
"""The control store's transactions and schema versions, through its public API."""

from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

import pytest

from sandbox_science.orchestrator.control import ControlError, ControlStore, migrations
from sandbox_science.orchestrator.documents import DocumentError


def _store(path: Path, *, readonly: bool = False) -> ControlStore:
    return ControlStore(path, readonly=readonly)


def _runs(tx_sql: sqlite3.Connection) -> list[tuple[str]]:
    return tx_sql.execute("SELECT project FROM job_runs ORDER BY project").fetchall()


def test_a_change_commits_and_a_failed_one_rolls_back(tmp_path: Path) -> None:
    store = _store(tmp_path / "control.sqlite3")
    assert not store.in_transaction
    with store.write() as tx:
        assert store.in_transaction
        tx.sql.execute("INSERT INTO job_runs(project) VALUES ('kept')")
    assert not store.in_transaction

    def failing() -> None:
        with store.write() as tx:
            tx.sql.execute("INSERT INTO job_runs(project) VALUES ('dropped')")
            raise RuntimeError("the change fails")

    with pytest.raises(RuntimeError):
        failing()
    with store.read() as tx:
        assert _runs(tx.sql) == [("kept",)]
    store.close()


def test_a_nested_transaction_is_refused_clearly(tmp_path: Path) -> None:
    store = _store(tmp_path / "control.sqlite3")

    def nested() -> None:
        with store.write() as tx:
            tx.sql.execute("INSERT INTO job_runs(project) VALUES ('outer')")
            with store.write():
                pass

    with pytest.raises(ControlError, match="already open") as raised:
        nested()
    assert raised.value.kind == "internal"
    with pytest.raises(ControlError, match="already open"), store.write(), store.read():
        pass
    with store.read() as tx:
        assert _runs(tx.sql) == []
    store.close()


def test_a_read_only_store_is_missing_until_a_writer_makes_it(tmp_path: Path) -> None:
    path = tmp_path / "control.sqlite3"
    reader = _store(path, readonly=True)
    assert reader.missing
    with pytest.raises(ControlError) as raised, reader.read():
        pass
    assert raised.value.kind == "not_found"
    assert not path.exists()
    with pytest.raises(ControlError, match="read-only"), reader.write():
        pass
    writer = _store(path)
    writer.initialize()
    assert not reader.missing
    with reader.read() as tx:
        assert _runs(tx.sql) == []
        assert tx.sql.execute("PRAGMA user_version").fetchone() == (migrations.VERSION,)
    reader.close()
    writer.close()


def test_the_document_error_is_the_control_error() -> None:
    assert DocumentError is ControlError


def _database(path: Path, version: int) -> str:
    """A database in ``version`` with one row, and the digest of its bytes."""
    db = sqlite3.connect(path)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("CREATE TABLE documents (document_id TEXT PRIMARY KEY)")
    db.execute("INSERT INTO documents VALUES ('doc-1')")
    db.execute(f"PRAGMA user_version = {version}")
    db.commit()
    db.close()
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_a_development_store_from_before_the_baseline_is_refused_unchanged(
    tmp_path: Path,
) -> None:
    path = tmp_path / "control.sqlite3"
    digest = _database(path, migrations.BASELINE - 1)
    for readonly in (False, True):
        store = _store(path, readonly=readonly)
        with pytest.raises(ControlError, match="document export") as raised, store.read():
            pass
        assert raised.value.kind == "internal"
        assert "move the file aside" in raised.value.message
        store.close()
    assert hashlib.sha256(path.read_bytes()).hexdigest() == digest


def test_a_newer_store_is_refused_unchanged(tmp_path: Path) -> None:
    path = tmp_path / "control.sqlite3"
    digest = _database(path, migrations.VERSION + 1)
    for readonly in (False, True):
        store = _store(path, readonly=readonly)
        with pytest.raises(ControlError, match="newer"), store.read():
            pass
        store.close()
    assert hashlib.sha256(path.read_bytes()).hexdigest() == digest


def test_a_writer_runs_the_pending_steps_all_or_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "control.sqlite3"
    _store(path).initialize()
    baseline = migrations.MIGRATIONS

    def add_notes(db: sqlite3.Connection) -> None:
        db.execute("CREATE TABLE notes (text TEXT NOT NULL) STRICT")

    def fail(db: sqlite3.Connection) -> None:
        db.execute("SELECT no_such_function()")

    monkeypatch.setattr(migrations, "MIGRATIONS", (*baseline, add_notes, fail))
    monkeypatch.setattr(migrations, "VERSION", migrations.BASELINE + 2)
    with pytest.raises(sqlite3.OperationalError, match="no_such_function"):
        _store(path).initialize()
    reader = _store(path, readonly=True)
    with pytest.raises(ControlError, match="open it with the harness"), reader.read():
        pass
    db = sqlite3.connect(path)
    assert db.execute("PRAGMA user_version").fetchone() == (migrations.BASELINE,)
    assert db.execute("SELECT 1 FROM sqlite_master WHERE name='notes'").fetchone() is None
    db.close()

    monkeypatch.setattr(migrations, "MIGRATIONS", (*baseline, add_notes))
    monkeypatch.setattr(migrations, "VERSION", migrations.BASELINE + 1)
    writer = _store(path)
    with writer.write() as tx:
        tx.sql.execute("INSERT INTO notes VALUES ('migrated')")
    writer.close()
    with reader.read() as tx:
        assert tx.sql.execute("PRAGMA user_version").fetchone() == (migrations.BASELINE + 1,)
        assert tx.sql.execute("SELECT text FROM notes").fetchall() == [("migrated",)]
    reader.close()


def test_the_baseline_is_never_edited() -> None:
    # A database made from the baseline keeps it: change the schema by appending a
    # step to migrations.MIGRATIONS instead.
    text = Path(migrations.__file__).with_name("baseline.sql").read_bytes()
    assert (
        hashlib.sha256(text).hexdigest()
        == "24ee67e37dc5db1fc6d6a852beaebcc02c9f38812a84fe58251a157eeee0e310"
    )
