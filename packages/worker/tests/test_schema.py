# SPDX-License-Identifier: MIT
"""The worker store's version: made, adopted, migrated once, refused when it must be."""

from __future__ import annotations

import contextlib
import sqlite3
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


def dump(path: Path) -> list[str]:
    """The store's schema and rows, without its version stamps."""
    with contextlib.closing(sqlite3.connect(path)) as db:
        return list(db.iterdump())


@contextlib.contextmanager
def raw(path: Path) -> Iterator[sqlite3.Connection]:
    with contextlib.closing(sqlite3.connect(path, isolation_level=None)) as db:
        yield db


def test_a_new_store_is_made_at_the_latest_version(tmp_path: Path) -> None:
    path = tmp_path / "worker.sqlite3"
    Store(path).close()
    assert pragmas(path) == (schema.latest(), schema.APPLICATION_ID)
    before = dump(path)
    Store(path).close()
    assert dump(path) == before


def test_an_unversioned_store_of_the_day_is_adopted_unchanged(tmp_path: Path) -> None:
    """A store the worker made before versioning, with its rows, becomes the baseline."""
    path = tmp_path / "worker.sqlite3"
    store = Store(path)
    store.remember_endpoint("env1", "inc-" + "1" * 24, ("sandbox", "sandbox-id"))
    store.bind_package_gate("env1", "original")
    store.close()
    with raw(path) as db:
        db.execute("PRAGMA user_version=0")
        db.execute("PRAGMA application_id=0")
        # Tables made on demand by later code may be missing; adoption makes them.
        db.execute("DROP TABLE kernel_package_epochs")
    before = dump(path)
    store = Store(path)
    try:
        assert store.endpoint_incarnation("env1") == "inc-" + "1" * 24
    finally:
        store.close()
    assert pragmas(path) == (schema.latest(), schema.APPLICATION_ID)
    after = dump(path)
    assert set(before) <= set(after)
    assert any("kernel_package_epochs" in line for line in after)


def test_a_store_from_before_the_baseline_is_refused_unchanged(tmp_path: Path) -> None:
    path = tmp_path / "worker.sqlite3"
    with raw(path) as db:
        db.execute(
            "CREATE TABLE executor_endpoints (environment_id TEXT PRIMARY KEY,"
            " incarnation TEXT NOT NULL)"
        )
    before = dump(path)
    with pytest.raises(WorkerError, match="predates the versioned schema"):
        Store(path)
    assert dump(path) == before
    assert pragmas(path) == (0, 0)


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
    before = dump(path)
    with pytest.raises(WorkerError, match=refusal):
        Store(path)
    assert dump(path) == before
    assert pragmas(path) == (version, application)


def test_a_migration_runs_once_and_a_failing_one_changes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "worker.sqlite3"
    Store(path).close()
    runs: list[int] = []

    def add_notes(db: sqlite3.Connection) -> None:
        runs.append(1)
        db.execute("CREATE TABLE notes (note TEXT NOT NULL)")

    def failing(db: sqlite3.Connection) -> None:
        db.execute("CREATE TABLE half (x TEXT)")
        raise RuntimeError("a step that fails")

    monkeypatch.setattr(schema, "MIGRATIONS", [add_notes])
    Store(path).close()
    Store(path).close()
    assert runs == [1]
    assert pragmas(path) == (schema.BASELINE + 1, schema.APPLICATION_ID)
    before = dump(path)
    monkeypatch.setattr(schema, "MIGRATIONS", [add_notes, failing])
    with pytest.raises(RuntimeError, match="a step that fails"):
        Store(path)
    assert dump(path) == before
    assert pragmas(path) == (schema.BASELINE + 1, schema.APPLICATION_ID)
