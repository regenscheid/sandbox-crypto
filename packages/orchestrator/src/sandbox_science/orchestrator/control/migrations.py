# SPDX-License-Identifier: MIT
"""The control store's schema: one ordered list of migrations and one version.

The version is SQLite's ``user_version``. Version 2 is the baseline, ``baseline.sql``,
and each later step in ``MIGRATIONS`` takes the database one version further. A writer
runs the steps its database lacks when it first opens the store, all in one
``BEGIN IMMEDIATE`` transaction, so a step that fails leaves the database as it was. A
reader only reads the current version. Any other database is refused and left
unchanged: one in a newer version, a file that is not a control store, and a
development store from before the baseline (version 1), whose documents are exported
and imported again into a fresh store (docs/LIVING-DOCUMENTS.md).

To change the schema, append a step to ``MIGRATIONS``. Never edit a step that has
shipped, or ``baseline.sql``: databases made with them keep them.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from importlib import resources
from pathlib import Path
from typing import Final

from .errors import INTERNAL, ControlError

type Migration = Callable[[sqlite3.Connection], None]

BASELINE: Final = 2
"""The version ``baseline.sql`` creates. Version 1 was the development stores before it."""


def _baseline(db: sqlite3.Connection) -> None:
    script = resources.files(__package__).joinpath("baseline.sql").read_text(encoding="utf-8")
    statement = ""
    for line in script.splitlines(keepends=True):
        statement += line
        if sqlite3.complete_statement(statement):
            db.execute(statement)
            statement = ""
    if statement.strip():
        raise ControlError(INTERNAL, "the control store's baseline schema is incomplete")


MIGRATIONS: Final[tuple[Migration, ...]] = (_baseline,)
"""``MIGRATIONS[0]`` creates the baseline in an empty database, and ``MIGRATIONS[i]``
takes a database in version ``BASELINE + i - 1`` to ``BASELINE + i``."""

VERSION: Final = BASELINE + len(MIGRATIONS) - 1
"""The version this code reads and writes."""


def check(path: Path, version: int, *, readonly: bool) -> None:
    """Refuse a database in ``version`` that this code cannot use as it is (a reader)
    or bring to ``VERSION`` (a writer). An empty database is in version 0."""
    if version == VERSION or (not readonly and (version == 0 or BASELINE <= version < VERSION)):
        return
    if 0 < version < BASELINE:
        raise ControlError(
            INTERNAL,
            f"{path} is a development control store from before schema version {BASELINE}, "
            "which this version neither reads nor converts. Export its documents with the "
            "harness that made it (document export), move the file aside, and import them "
            "into the new store this version creates (see docs/LIVING-DOCUMENTS.md)",
        )
    if version > VERSION:
        raise ControlError(
            INTERNAL,
            f"{path} is in schema version {version}, newer than this version's {VERSION}: "
            "upgrade the harness and the workspace",
        )
    raise ControlError(
        INTERNAL,
        f"{path} is in schema version {version}: open it with the harness or the "
        f"workspace first, which bring it to version {VERSION}",
    )


def migrate(db: sqlite3.Connection, path: Path) -> None:
    """Bring the database to ``VERSION``, inside the writer's open transaction."""
    if not db.in_transaction:
        raise ControlError(INTERNAL, "migrating the control store needs a write transaction")
    (version,) = db.execute("PRAGMA user_version").fetchone()
    check(path, version, readonly=False)
    if version == 0:
        (objects,) = db.execute("SELECT count(*) FROM sqlite_master").fetchone()
        if objects:
            raise ControlError(INTERNAL, f"{path} is not a control store")
        pending = MIGRATIONS
    else:
        pending = MIGRATIONS[version - BASELINE + 1 :]
    for step in pending:
        step(db)
    if pending:
        db.execute(f"PRAGMA user_version = {VERSION}")
