# SPDX-License-Identifier: MIT
"""The worker store's schema: one ordered list of migrations and one version.

The version is SQLite's ``user_version``, and a worker store carries
``APPLICATION_ID`` as its ``application_id``. Version 1 is the baseline,
``baseline.sql``, and each step in ``MIGRATIONS`` takes the store one version
further. ``prepare`` runs the steps a store lacks when the worker opens it, in one
``BEGIN IMMEDIATE`` transaction, so a step that fails leaves the store as it was.

A store from before versioning, which the worker of the day made and opened, is
taken as the baseline: the tables it lacks are made, and nothing else changes. One
older still, without the columns that the worker of the day added on opening, is
refused, and so is a store in a newer version or a file that is not a worker store.
To discard a refused state directory, release or recycle its sandboxes first: the
worker keeps its daemons' endpoints there (docs/WORKER.md).

To change the schema, append a step to ``MIGRATIONS``. Never edit a step that has
shipped, or ``baseline.sql``: stores made with them keep them. A step gets the
connection inside the transaction, and must not commit or roll back.
"""

from __future__ import annotations

import secrets
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime
from importlib import resources
from typing import Final

from sandbox_science.contracts import ErrorKind

from .errors import WorkerError

BASELINE: Final = 1
"""The version ``baseline.sql`` creates."""

APPLICATION_ID: Final = 0x53537773
"""The ``application_id`` of a worker store ("SSws")."""

type Migration = Callable[[sqlite3.Connection], None]

MIGRATIONS: list[Migration] = []
"""Steps after the baseline: the one at index ``i`` takes a store to ``BASELINE + i + 1``."""

_ADDED_ON_OPEN: Final = {
    "executions": ("kernel_scope",),
    "executor_endpoints": ("sandbox", "sandbox_id"),
    "executor_recycles": ("sandbox", "sandbox_id"),
}
"""Columns the worker added to a store when it opened one, before versioning. A store
without them predates the baseline."""


def latest() -> int:
    """The version this worker writes."""
    return BASELINE + len(MIGRATIONS)


def _statements(script: str) -> list[str]:
    found: list[str] = []
    statement = ""
    for line in script.splitlines(keepends=True):
        statement += line
        if sqlite3.complete_statement(statement):
            found.append(statement)
            statement = ""
    if statement.strip():
        raise WorkerError(ErrorKind.INTERNAL, "the worker store's baseline schema is incomplete")
    return found


def _baseline(db: sqlite3.Connection) -> None:
    """Make every table and index of the baseline that is missing, and the output
    observations' identity (output_observations.py)."""
    script = resources.files(__package__).joinpath("baseline.sql").read_text(encoding="utf-8")
    for statement in _statements(script):
        db.execute(statement)
    db.execute(
        "INSERT OR IGNORE INTO output_observation_meta VALUES(1,?,?)",
        (secrets.token_hex(32).encode(), datetime.now(UTC).isoformat().encode()),
    )


def _refuse(reason: str) -> WorkerError:
    return WorkerError(
        ErrorKind.UNAVAILABLE,
        f"the worker store {reason}; release or recycle its sandboxes and discard the "
        "state directory, or use the worker that made it",
    )


def _check_unversioned(db: sqlite3.Connection) -> None:
    for table, columns in _ADDED_ON_OPEN.items():
        present = {str(row[1]) for row in db.execute(f"PRAGMA table_info({table})")}
        if present and not set(columns) <= present:
            raise _refuse("predates the versioned schema")


def prepare(db: sqlite3.Connection) -> None:
    """Bring the store to the latest version, or refuse it and leave it unchanged."""
    version = int(db.execute("PRAGMA user_version").fetchone()[0])
    application = int(db.execute("PRAGMA application_id").fetchone()[0])
    if application not in (0, APPLICATION_ID):
        raise _refuse("is not a worker store")
    if version > 0 and application != APPLICATION_ID:
        raise _refuse("is not a worker store")
    if version > latest():
        raise _refuse(f"is in version {version}, newer than this worker's {latest()}")
    if version == latest():
        return
    db.execute("BEGIN IMMEDIATE")
    try:
        if version == 0:
            _check_unversioned(db)
            _baseline(db)
            db.execute(f"PRAGMA application_id={APPLICATION_ID}")
            version = BASELINE
        for step in MIGRATIONS[version - BASELINE :]:
            step(db)
        db.execute(f"PRAGMA user_version={latest()}")
    except BaseException:
        db.execute("ROLLBACK")
        raise
    db.execute("COMMIT")
