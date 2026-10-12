# SPDX-License-Identifier: MIT
"""Finding-origin migration, preserving existing IDs, history and user CAS heads."""

from __future__ import annotations

import sqlite3

from sandbox_science.orchestrator.documents.errors import INTERNAL, DocumentError

LEGACY_FIELDS = (
    "finding_id",
    "project",
    "session",
    "review_id",
    "severity",
    "category",
    "statement",
    "anchors",
    "remedy",
    "status",
    "revision",
    "reflags",
    "dedupe",
    "supersedes",
    "created",
)
FINDING_TABLE = """CREATE TABLE IF NOT EXISTS review_findings (
  finding_id TEXT PRIMARY KEY,project TEXT NOT NULL,session TEXT NOT NULL,review_id TEXT,
  severity TEXT NOT NULL,category TEXT NOT NULL,statement TEXT NOT NULL,anchors TEXT NOT NULL,
  remedy TEXT,status TEXT NOT NULL,revision INTEGER NOT NULL,reflags INTEGER NOT NULL DEFAULT 0,
  dedupe TEXT NOT NULL,supersedes TEXT,created REAL NOT NULL,
  origin TEXT NOT NULL DEFAULT 'review',math_check_id TEXT REFERENCES math_jobs(check_id),
  CHECK((origin='review' AND review_id IS NOT NULL AND math_check_id IS NULL AND category!='math')
     OR (origin='math' AND review_id IS NULL AND math_check_id IS NOT NULL
         AND category='math' AND severity='warn'))) STRICT"""
FINDING_INDEX = (
    "CREATE INDEX IF NOT EXISTS review_findings_scope ON review_findings(project,session,created)"
)


def prepare(db: sqlite3.Connection) -> None:
    """Migrate within the shared writer transaction; readers never mutate schemas."""
    if not db.in_transaction:
        raise DocumentError(INTERNAL, "finding migration requires a writer transaction")
    db.execute(
        "CREATE TABLE IF NOT EXISTS review_finding_schema "
        "(singleton INTEGER PRIMARY KEY CHECK(singleton=1),version INTEGER NOT NULL) STRICT"
    )
    version = db.execute("SELECT version FROM review_finding_schema WHERE singleton=1").fetchone()
    if version is not None and version != (2,):
        raise DocumentError(INTERNAL, "unsupported finding schema version")
    columns = {row[1] for row in db.execute("PRAGMA table_info(review_findings)")}
    if version == (2,) and not {"origin", "math_check_id"} <= columns:
        raise DocumentError(INTERNAL, "completed finding schema has missing columns")
    if "origin" not in columns:
        # Do not rename the old parent: SQLite would rewrite transition FKs to
        # that obsolete name. Preserve all IDs, rebuild, then check every FK
        # before restoring immediate enforcement in this same transaction.
        db.execute("PRAGMA defer_foreign_keys=ON")
        db.execute(FINDING_TABLE.replace("review_findings", "review_findings_with_origin", 1))
        names = "rowid," + ",".join(LEGACY_FIELDS)
        db.execute(
            "INSERT INTO review_findings_with_origin("  # noqa: S608 - fixed column names
            + names
            + ") SELECT "
            + names
            + " FROM review_findings ORDER BY rowid"
        )
        db.execute("DROP TABLE review_findings")
        db.execute("ALTER TABLE review_findings_with_origin RENAME TO review_findings")
        db.execute(FINDING_INDEX)
        if db.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise DocumentError(INTERNAL, "finding-origin migration violated a foreign key")
        db.execute("PRAGMA defer_foreign_keys=OFF")
    elif "math_check_id" not in columns:
        raise DocumentError(INTERNAL, "incomplete finding-origin migration")
    db.execute("INSERT OR IGNORE INTO review_finding_schema VALUES(1,2)")


def projection(db: sqlite3.Connection) -> str:
    """Legacy readonly stores project review origin without requiring a migration."""
    fields = ",".join(LEGACY_FIELDS)
    columns = {row[1] for row in db.execute("PRAGMA table_info(review_findings)")}
    return fields + (",origin,math_check_id" if "origin" in columns else ",'review',NULL")
