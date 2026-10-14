# SPDX-License-Identifier: MIT
"""The worker store's schema: one ordered list of migrations and one version.

The version is SQLite's ``user_version``, and a worker store carries
``APPLICATION_ID`` as its ``application_id``. Version 1 is the baseline,
``baseline.sql``, and each step in ``MIGRATIONS`` takes the store one version
further. ``open_store`` refuses a file this worker cannot use before anything could
change it, through a read-only connection: a store in a newer version, an unversioned
one from before the baseline (decision 6), one holding the executions of a removed
feature, and a file that is not a worker store. It then opens the writer and runs the
steps the store lacks, in one ``BEGIN IMMEDIATE`` transaction that reads the version
again, so a step runs once however many workers open the store together, and a step
that fails leaves the store as it was.

To discard a refused state directory, release or recycle its sandboxes first: the
worker keeps their daemons' endpoints there (docs/WORKER.md).

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
from pathlib import Path
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

_STATE: Final = (
    "SELECT (SELECT user_version FROM pragma_user_version),"
    " (SELECT application_id FROM pragma_application_id),"
    " (SELECT count(*) FROM sqlite_master)"
)
"""The version, the application ID and whether the file holds anything, in one statement
and so one snapshot: read apart, another worker's first commit could land in between."""


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
    """Make the baseline's tables and indexes, and the output observations' identity
    (output_observations.py)."""
    script = resources.files(__package__).joinpath("baseline.sql").read_text(encoding="utf-8")
    for statement in _statements(script):
        db.execute(statement)
    db.execute(
        "INSERT INTO output_observation_meta VALUES(1,?,?)",
        (secrets.token_hex(32).encode(), datetime.now(UTC).isoformat().encode()),
    )


def _refuse(path: Path, reason: str) -> WorkerError:
    return WorkerError(
        ErrorKind.UNAVAILABLE,
        f"{path} {reason}; to discard the state directory, release or recycle its "
        "sandboxes first, or use the worker that made it",
    )


def _check(path: Path, state: tuple[int, int, int]) -> int:
    """The version of a store this worker can bring to ``latest()``; a refusal otherwise."""
    version, application, objects = state
    if version == 0 and application == 0:
        if objects:  # an unversioned worker store from before version 1, or another file
            raise _refuse(path, "is not a worker store of version 1 or later")
        return 0
    if application != APPLICATION_ID or version < BASELINE:
        raise _refuse(path, "is not a worker store")
    if version > latest():
        raise _refuse(path, f"is in version {version}, newer than this worker's {latest()}")
    return version


def inspect(path: Path) -> None:
    """Refuse a store this worker cannot use, through a read-only connection that changes
    no file. A missing file is a new store."""
    if not path.exists():
        return
    # A read-only connection to a store in write-ahead mode makes the log and its index
    # (-wal, -shm) where there are none; a refused store has them removed again, so it
    # stays as it was.
    made = [file for file in (Path(f"{path}-wal"), Path(f"{path}-shm")) if not file.exists()]
    try:
        _inspect(path)
    except BaseException:
        for file in made:
            file.unlink(missing_ok=True)
        raise


def _inspect(path: Path) -> None:
    try:
        db = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, isolation_level=None)
    except sqlite3.DatabaseError:
        raise _refuse(path, "is not a worker store") from None
    try:
        try:
            state = db.execute(_STATE).fetchone()
        except sqlite3.DatabaseError:
            raise _refuse(path, "is not a worker store") from None
        if _check(path, state) == 0:
            return
        # Delegated runs were removed; their old tables stay untouched.
        if db.execute(
            "SELECT 1 FROM executions WHERE kernel_scope IS NOT 'legacy' LIMIT 1"
        ).fetchone():
            raise _refuse(path, "holds delegated-run executions from a removed feature")
    finally:
        db.close()


def prepare(path: Path, db: sqlite3.Connection) -> None:
    """Bring the writer's store to ``latest()``: make it if it is new, and run the steps
    it lacks. Decides again inside the write transaction, from one snapshot."""
    db.execute("BEGIN IMMEDIATE")
    try:
        version = _check(path, db.execute(_STATE).fetchone())
        if version < latest():
            if version == 0:
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
