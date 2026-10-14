# SPDX-License-Identifier: MIT
"""Atomic document-math scheduling; immutable source history is the durable backlog.

The save hook records a high-water revision and the current block owners with SQL
only. It never renders, parses, starts a task or refuses a save for a check quota.
"""

from __future__ import annotations

import sqlite3

from .errors import INTERNAL, DocumentError


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
