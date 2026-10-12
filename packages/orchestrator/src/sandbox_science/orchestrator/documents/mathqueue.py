# SPDX-License-Identifier: MIT
"""Atomic document-math scheduling; immutable source history is the durable backlog.

The save hook records a high-water revision and the current block owners with SQL
only. It never renders, parses, starts a task or refuses a save for a check quota.
"""

from __future__ import annotations

import sqlite3

from .errors import INTERNAL, DocumentError

SCHEMA = (
    """CREATE TABLE IF NOT EXISTS math_sources (
      document TEXT PRIMARY KEY REFERENCES documents(document_id), project TEXT NOT NULL,
      target INTEGER NOT NULL, completed INTEGER NOT NULL DEFAULT 0,
      blocked TEXT, retry_requested INTEGER NOT NULL DEFAULT 0) STRICT""",
    """CREATE TABLE IF NOT EXISTS math_block_owners (
      document TEXT NOT NULL REFERENCES documents(document_id), block TEXT NOT NULL,
      revision INTEGER NOT NULL, author TEXT NOT NULL,
      PRIMARY KEY(document,block)) STRICT""",
    """CREATE TABLE IF NOT EXISTS math_jobs (
      check_id TEXT PRIMARY KEY, document TEXT REFERENCES documents(document_id),
      revision INTEGER NOT NULL, source_sha256 TEXT NOT NULL, state TEXT NOT NULL,
      owner TEXT, lease_until REAL, cursor INTEGER NOT NULL DEFAULT 0,
      spans INTEGER, available INTEGER NOT NULL DEFAULT 0, rejected INTEGER NOT NULL DEFAULT 0,
      unavailable INTEGER NOT NULL DEFAULT 0, configuration TEXT, runtime TEXT, reason TEXT,
      attribution_cursor INTEGER, created REAL NOT NULL, ended REAL,
      CHECK(revision>0)) STRICT""",
    "CREATE INDEX IF NOT EXISTS math_revision ON math_jobs(document,revision)",
    """CREATE UNIQUE INDEX IF NOT EXISTS math_one_running
      ON math_jobs(document) WHERE state='running'""",
    """CREATE TABLE IF NOT EXISTS math_observations (
      check_id TEXT NOT NULL REFERENCES math_jobs(check_id), ordinal INTEGER NOT NULL,
      block TEXT NOT NULL, span TEXT NOT NULL, input_sha256 TEXT NOT NULL,
      status TEXT NOT NULL, payload TEXT NOT NULL, bytes INTEGER NOT NULL,
      PRIMARY KEY(check_id,ordinal)) STRICT""",
    "CREATE INDEX IF NOT EXISTS math_observation_span ON math_observations(block,span,check_id)",
    """CREATE TABLE IF NOT EXISTS math_notices (
      finding_id TEXT NOT NULL, revision INTEGER NOT NULL, project TEXT NOT NULL,
      session TEXT NOT NULL, authored_run TEXT, claimed_run TEXT, claim_until REAL,
      delivered_run TEXT, PRIMARY KEY(finding_id,revision)) STRICT""",
    """CREATE TABLE IF NOT EXISTS math_job_owners (
      check_id TEXT NOT NULL REFERENCES math_jobs(check_id),block TEXT NOT NULL,
      revision INTEGER,author TEXT,bytes INTEGER NOT NULL,PRIMARY KEY(check_id,block)) STRICT""",
    """CREATE TABLE IF NOT EXISTS math_finding_state (
      finding_id TEXT PRIMARY KEY REFERENCES review_findings(finding_id),
      document TEXT NOT NULL,block TEXT NOT NULL,span TEXT NOT NULL,
      checked_revision INTEGER NOT NULL,input_sha256 TEXT NOT NULL,
      owner_revision INTEGER NOT NULL,
      checked_check TEXT NOT NULL REFERENCES math_jobs(check_id),excluded TEXT,
      UNIQUE(document,block,span)) STRICT""",
    """CREATE TABLE IF NOT EXISTS math_ignored (
      finding_id TEXT NOT NULL, revision INTEGER NOT NULL, run TEXT NOT NULL,
      PRIMARY KEY(finding_id,revision)) STRICT""",
    *tuple(
        f"CREATE TRIGGER IF NOT EXISTS math_observations_{verb.lower()} BEFORE {verb} "
        "ON math_observations BEGIN SELECT RAISE(ABORT,'math observations are immutable'); END"
        for verb in ("UPDATE", "DELETE")
    ),
)


def prepare(db: sqlite3.Connection) -> None:
    """Initialize queue metadata inside the canonical store's schema transaction.

    Existing block ownership is deliberately unknown. A checker must prove it from
    immutable block maps; attributing every old block to the current author is wrong.
    """
    if not db.in_transaction:
        raise DocumentError(INTERNAL, "math preparation requires the source transaction")
    columns = list(db.execute("PRAGMA table_info(math_jobs)"))
    if any(row[1] == "document" and row[3] == 1 for row in columns):
        # Preserve the parent name, rowids and all observation/finding FKs. A
        # finding explanation has no document parent; never borrow an anchor.
        db.execute("PRAGMA defer_foreign_keys=ON")
        views = list(
            db.execute(
                "SELECT name,sql FROM sqlite_master WHERE type='view' AND name IN "
                "('math_current_checks','math_job_scope')"
            )
        )
        for name, _ in views:
            db.execute("DROP VIEW " + name)
        db.execute(SCHEMA[2].replace("math_jobs", "math_jobs_source_neutral", 1))
        names = "rowid," + ",".join(row[1] for row in columns)
        db.execute(
            "INSERT INTO math_jobs_source_neutral("  # noqa: S608 - schema column names
            + names
            + ") SELECT "
            + names
            + " FROM math_jobs ORDER BY rowid"
        )
        db.execute("DROP TABLE math_jobs")
        db.execute("ALTER TABLE math_jobs_source_neutral RENAME TO math_jobs")
        for _, sql in views:
            db.execute(sql)
        if db.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise DocumentError(INTERNAL, "source-neutral math migration violated a foreign key")
        db.execute("PRAGMA defer_foreign_keys=OFF")
    for statement in SCHEMA:
        db.execute(statement)
    # One cursor per document references all retained revisions without copying an
    # unbounded list of queued jobs or vetoing an ordinary edit when a quota fills.
    db.execute(
        "INSERT OR IGNORE INTO math_sources(document,project,target) "
        "SELECT document_id,project_id,head FROM documents"
    )
    db.execute(
        "UPDATE math_sources SET target=max(target,(SELECT head FROM documents d "
        "WHERE d.document_id=math_sources.document))"
    )


def record_event(db: sqlite3.Connection, kind: str, document: str, event: int) -> None:
    """Schedule every saved revision and keep exact current content ownership."""
    if kind != "document.revision_created":
        return
    if not db.in_transaction:
        raise DocumentError(INTERNAL, "math scheduling requires the source transaction")
    row = db.execute(
        "SELECT d.project_id,d.head,r.author FROM documents d JOIN revisions r "
        "ON r.document_id=d.document_id AND r.number=d.head WHERE d.document_id=?",
        (document,),
    ).fetchone()
    if row is None:
        raise DocumentError(INTERNAL, "math scheduling cannot pin the saved revision")
    project, revision, author = row
    # Verify the exact event, rather than trusting a caller-supplied revision/author.
    if (
        db.execute(
            "SELECT 1 FROM events WHERE seq=? AND document_id=? AND kind=? "
            "AND json_extract(payload,'$.revision')=?",
            (event, document, kind, revision),
        ).fetchone()
        is None
    ):
        raise DocumentError(INTERNAL, "math scheduling event does not name the saved revision")
    db.execute(
        "INSERT INTO math_sources(document,project,target) VALUES(?,?,?) "
        "ON CONFLICT(document) DO UPDATE SET target=excluded.target",
        (document, project, revision),
    )
    # USER/import/platform are real ownership markers too: never skip them while
    # searching backwards for an agent. Moving a block does not change its owner.
    db.execute(
        "INSERT INTO math_block_owners(document,block,revision,author) "
        "SELECT ?,json_extract(b.value,'$.id'),?,? FROM revisions r, "
        "json_each(r.block_map,'$.blocks') b WHERE r.document_id=? AND r.number=? "
        "AND json_extract(b.value,'$.status') IN ('added','changed') "
        "ON CONFLICT(document,block) DO UPDATE SET "
        "revision=excluded.revision,author=excluded.author",
        (document, revision, author, document, revision),
    )
    db.execute(
        "DELETE FROM math_block_owners WHERE document=? AND block NOT IN "
        "(SELECT json_extract(b.value,'$.id') FROM revisions r,json_each(r.block_map,'$.blocks') b "
        "WHERE r.document_id=? AND r.number=?)",
        (document, document, revision),
    )
