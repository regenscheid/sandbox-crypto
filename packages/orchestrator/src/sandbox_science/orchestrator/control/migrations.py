# SPDX-License-Identifier: MIT
"""The control store's schema: one ordered list of migrations and one version.

The version is SQLite's ``user_version``, and a control store carries
``APPLICATION_ID`` as its ``application_id``. Version 2 is the baseline,
``baseline.sql``, and each later step in ``MIGRATIONS`` takes the database one version
further. A writer runs the steps its database lacks when it first opens the store, all
in one ``BEGIN IMMEDIATE`` transaction, so a step that fails leaves the database as it
was. A reader only reads the current version. Any other file is refused, and checked
through a read-only connection first, so it is left unchanged: a store in a newer
version, a file that is not a control store, and a development store from before the
baseline (version 1), whose documents are exported and imported again into a fresh
store (docs/LIVING-DOCUMENTS.md).

To change the schema, append a step to ``MIGRATIONS``. Never edit a step that has
shipped, or ``baseline.sql``: databases made with them keep them. A step gets a
``Schema``, which runs one statement at a time and refuses transaction control, since
the step runs inside the writer's transaction. A step that rebuilds a table (create the
new one, copy the rows, drop the old one, rename) first runs
``PRAGMA defer_foreign_keys=ON``, because ``foreign_keys`` cannot be switched off inside
a transaction, and runs ``PRAGMA foreign_key_check`` before it ends.
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Callable, Sequence
from importlib import resources
from pathlib import Path
from typing import Final

from .errors import BUSY, INTERNAL, NOT_FOUND, UNAVAILABLE, ControlError

BASELINE: Final = 2
"""The version ``baseline.sql`` creates. Version 1 was the development stores before it."""

APPLICATION_ID: Final = 0x53536373
"""The ``application_id`` of a control store ("SScs"), which ``baseline.sql`` sets."""

_TRANSACTION: Final = re.compile(
    r"(?:\s|--[^\n]*\n)*(BEGIN|COMMIT|END|ROLLBACK|SAVEPOINT|RELEASE|VACUUM)\b", re.IGNORECASE
)


class Schema:
    """What a migration step may do: run statements in the writer's transaction."""

    def __init__(self, db: sqlite3.Connection) -> None:
        self._db = db

    def execute(self, sql: str, parameters: Sequence[object] = ()) -> sqlite3.Cursor:
        if _TRANSACTION.match(sql):
            raise ControlError(INTERNAL, "a migration step runs inside the writer's transaction")
        return self._db.execute(sql, parameters)


type Migration = Callable[[Schema], None]


def _baseline(schema: Schema) -> None:
    script = resources.files(__package__).joinpath("baseline.sql").read_text(encoding="utf-8")
    statement = ""
    for line in script.splitlines(keepends=True):
        statement += line
        if sqlite3.complete_statement(statement):
            schema.execute(statement)
            statement = ""
    if statement.strip():
        raise ControlError(INTERNAL, "the control store's baseline schema is incomplete")


MIGRATIONS: Final[tuple[Migration, ...]] = (_baseline,)
"""``MIGRATIONS[0]`` creates the baseline in an empty database, and ``MIGRATIONS[i]``
takes a database in version ``BASELINE + i - 1`` to ``BASELINE + i``."""

VERSION: Final = BASELINE + len(MIGRATIONS) - 1
"""The version this code reads and writes."""


def _busy(error: sqlite3.OperationalError) -> bool:
    return any(word in str(error).lower() for word in ("locked", "busy"))


def check(path: Path, db: sqlite3.Connection, *, readonly: bool) -> int:
    """The version of the database ``db`` opens, if this code can read it (a reader) or
    bring it to ``VERSION`` (a writer); a refusal otherwise. Only reads."""
    not_ours = ControlError(UNAVAILABLE, f"{path.name} is not a control store")
    try:
        (version,) = db.execute("PRAGMA user_version").fetchone()
        (application,) = db.execute("PRAGMA application_id").fetchone()
        (objects,) = db.execute("SELECT count(*) FROM sqlite_master").fetchone()
    except sqlite3.OperationalError as error:
        if _busy(error):
            raise ControlError(BUSY, "another process held the control store: try again") from None
        raise not_ours from None
    except sqlite3.DatabaseError:
        raise not_ours from None
    if version == 0:
        if objects:
            raise not_ours
        if readonly:
            raise ControlError(NOT_FOUND, "nothing has been stored yet")
        return version
    if application != APPLICATION_ID:
        if 0 < version < BASELINE and application == 0:
            raise ControlError(
                UNAVAILABLE,
                f"{path.name} is a development control store from before "
                f"schema version {BASELINE}, which this version neither reads nor converts. "
                "Export its documents with the harness that made it (document export), move "
                "the file aside, and import them into the new store this version creates "
                "(see docs/LIVING-DOCUMENTS.md)",
            )
        raise not_ours
    if version > VERSION:
        raise ControlError(
            UNAVAILABLE,
            f"the control store is in schema version {version}, newer than this version's "
            f"{VERSION}: upgrade the harness and the workspace",
        )
    if version < BASELINE:
        raise not_ours
    if readonly and version < VERSION:
        raise ControlError(
            UNAVAILABLE,
            f"the control store is in schema version {version}: open it with the harness or "
            f"the workspace first, which bring it to version {VERSION}",
        )
    return version


def migrate(db: sqlite3.Connection, path: Path) -> None:
    """Bring the database to ``VERSION``, inside the writer's open transaction."""
    if not db.in_transaction:
        raise ControlError(INTERNAL, "migrating the control store needs a write transaction")
    version = check(path, db, readonly=False)
    pending = MIGRATIONS if version == 0 else MIGRATIONS[version - BASELINE + 1 :]
    schema = Schema(db)
    for step in pending:
        step(schema)
        if not db.in_transaction:
            raise ControlError(INTERNAL, "a migration step ended the writer's transaction")
    if pending:
        db.execute(f"PRAGMA user_version = {VERSION}")
