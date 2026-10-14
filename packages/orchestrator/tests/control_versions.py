# SPDX-License-Identifier: MIT
"""Older control stores for migration tests, made from a current one.

The code writes only the current schema, so a test cannot make a store in an older
version by writing to it. ``older_store`` makes one: an empty store in that version,
made by the migration steps up to it, filled with a current store's rows in the columns
both have, and with the rows of the tables later steps dropped derived from the
columns that replaced them. A migration test makes its data with the current code,
copies it back, migrates the copy, and reads it as it read the original.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from sandbox_science.orchestrator.control import ControlStore, migrations

MACRO_REVISIONS = migrations.BASELINE + 8
"""The version that replaced the macro pin tables with ``macro_revision`` columns."""

EMPTY_SHA = "44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a"
"""The SHA-256 of ``{}``, the empty macro setting."""

_PINS = (
    # The pin tables version 10 dropped, from the columns that replaced them.
    "INSERT INTO document_macro_pins SELECT document_id,number,macro_revision "
    "FROM src.revisions WHERE macro_revision IS NOT NULL",
    "INSERT INTO math_macro_pins SELECT j.check_id,s.project,j.macro_revision,"
    "coalesce((SELECT r.sha256 FROM src.project_macro_revisions r WHERE r.project=s.project "
    f"AND r.revision=j.macro_revision),'{EMPTY_SHA}'),'[]' FROM src.math_jobs j "
    "JOIN src.math_job_scope s USING(check_id) WHERE j.macro_revision IS NOT NULL",
    "INSERT INTO math_macro_contexts SELECT check_id,'{}','[]',macro_held,"
    "lower(hex(zeroblob(32))) FROM src.math_jobs WHERE macro_held IS NOT NULL",
    "INSERT INTO response_macro_revisions SELECT source,revision,macro_revision "
    "FROM src.review_response_bodies WHERE macro_revision IS NOT NULL",
    "INSERT OR IGNORE INTO response_macro_anchors SELECT source,'[]' "
    "FROM src.review_response_bodies",
    "INSERT INTO review_macro_pins SELECT p.review_id,p.finding_id,p.macro_revision,"
    "coalesce((SELECT r.sha256 FROM src.project_macro_revisions r JOIN src.review_findings f "
    "ON f.project=r.project WHERE f.finding_id=p.finding_id AND r.revision=p.macro_revision),"
    f"'{EMPTY_SHA}') FROM src.review_response_pins p WHERE p.macro_revision IS NOT NULL",
)


@contextmanager
def at_version(version: int) -> Iterator[None]:
    """The migration list and version as they were at ``version``."""
    saved = migrations.MIGRATIONS, migrations.VERSION
    setattr(migrations, "MIGRATIONS", saved[0][: version - migrations.BASELINE + 1])  # noqa: B010
    setattr(migrations, "VERSION", version)  # noqa: B010
    try:
        yield
    finally:
        setattr(migrations, "MIGRATIONS", saved[0])  # noqa: B010
        setattr(migrations, "VERSION", saved[1])  # noqa: B010


def older_store(source: Path, target: Path, version: int) -> None:
    """Make ``target`` a store in ``version`` holding the rows of the store ``source``."""
    with at_version(version):
        store = ControlStore(target)
        store.initialize()
        store.close()
    db = sqlite3.connect(target, isolation_level=None)
    try:
        db.execute("PRAGMA foreign_keys=OFF")
        db.execute("ATTACH DATABASE ? AS src", (str(source),))
        db.execute("BEGIN")
        tables = [
            name
            for (name,) in db.execute(
                "SELECT name FROM main.sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%' ORDER BY rowid"
            )
        ]
        for table in tables:
            _copy(db, table)
        if version < MACRO_REVISIONS:
            for statement in _PINS:
                db.execute(statement)
        db.execute("COMMIT")
    finally:
        db.close()


def _copy(db: sqlite3.Connection, table: str) -> None:
    mine = db.execute(f'PRAGMA main.table_info("{table}")').fetchall()
    theirs = {row[1] for row in db.execute(f'PRAGMA src.table_info("{table}")')}
    if not theirs:
        return
    columns = [row[1] for row in mine if row[1] in theirs]
    keys = [row for row in mine if row[5]]
    # A table whose key is its rowid copies it with the key; any other keeps its rowids.
    alias = len(keys) == 1 and keys[0][2].upper() == "INTEGER"
    names = ",".join(f'"{column}"' for column in columns)
    rowid = "" if alias else "rowid,"
    db.execute(
        f'INSERT INTO main."{table}" ({rowid}{names}) '
        f'SELECT {rowid}{names} FROM src."{table}" ORDER BY rowid'
    )
