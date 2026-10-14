# SPDX-License-Identifier: MIT
"""The control store's transactions, through its public ``read()`` and ``write()``."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from sandbox_science.orchestrator.control import ControlError, ControlStore
from sandbox_science.orchestrator.documents import DocumentError


def _prepare(db: sqlite3.Connection) -> None:
    if db.execute("PRAGMA user_version").fetchone()[0] == 0:
        db.execute("CREATE TABLE notes (text TEXT NOT NULL)")
        db.execute("PRAGMA user_version = 1")


def _store(path: Path, *, readonly: bool = False) -> ControlStore:
    return ControlStore(path, version=1, prepare=_prepare, readonly=readonly)


def test_a_change_commits_and_a_failed_one_rolls_back(tmp_path: Path) -> None:
    store = _store(tmp_path / "control.sqlite3")
    assert not store.in_transaction
    with store.write() as tx:
        assert store.in_transaction
        tx.sql.execute("INSERT INTO notes VALUES ('kept')")
    assert not store.in_transaction

    def failing() -> None:
        with store.write() as tx:
            tx.sql.execute("INSERT INTO notes VALUES ('dropped')")
            raise RuntimeError("the change fails")

    with pytest.raises(RuntimeError):
        failing()
    with store.read() as tx:
        assert tx.sql.execute("SELECT text FROM notes").fetchall() == [("kept",)]
    store.close()


def test_a_nested_transaction_is_refused_clearly(tmp_path: Path) -> None:
    store = _store(tmp_path / "control.sqlite3")

    def nested() -> None:
        with store.write() as tx:
            tx.sql.execute("INSERT INTO notes VALUES ('outer')")
            with store.write():
                pass

    with pytest.raises(ControlError, match="already open") as raised:
        nested()
    assert raised.value.kind == "internal"
    with pytest.raises(ControlError, match="already open"), store.write(), store.read():
        pass
    with store.read() as tx:
        assert tx.sql.execute("SELECT count(*) FROM notes").fetchone() == (0,)
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
        assert tx.sql.execute("SELECT count(*) FROM notes").fetchone() == (0,)
    reader.close()
    writer.close()


def test_the_document_error_is_the_control_error() -> None:
    assert DocumentError is ControlError
