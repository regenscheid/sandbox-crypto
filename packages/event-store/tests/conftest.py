# SPDX-License-Identifier: MIT
"""Shared fixtures: a store path, and raw access to a store as a tamperer would have it."""

import sqlite3
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from pathlib import Path

import pytest

Tamper = Callable[[Path], AbstractContextManager[sqlite3.Connection]]


@pytest.fixture
def path(tmp_path: Path) -> Path:
    return tmp_path / "events.db"


@contextmanager
def _tampering(path: Path) -> Iterator[sqlite3.Connection]:
    """Write to a store directly, as anyone with the file can.

    The append-only triggers are dropped for the changes and recreated after
    them, as a careful tamperer would, so that only the chain shows the change.
    """
    db = sqlite3.connect(path, isolation_level=None)
    try:
        triggers = db.execute(
            "SELECT name, sql FROM sqlite_master WHERE type = 'trigger'"
        ).fetchall()
        for name, _ in triggers:
            db.execute(f"DROP TRIGGER {name}")
        yield db
        for _, sql in triggers:
            db.execute(sql)
    finally:
        db.close()


@pytest.fixture
def tamper() -> Tamper:
    """``with tamper(path) as db:`` changes the store behind its back."""
    return _tampering
