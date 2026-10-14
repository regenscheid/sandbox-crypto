# SPDX-License-Identifier: MIT
"""Text revisions: versioned Markdown with its block map, owners and identity counters.

A comment body and a finding explanation are each a text whose revisions are kept
here, in one table, ``text_revisions``, under the kind of text (``subject_type``) and
its ID (refactoring plan, B-04). A revision is written once. It keeps its body and
SHA-256, the block map and block owners its parse gave, the counters the next block
and span IDs come from, the first revision whose identities it carries
(``identity_floor``), why its identity is held if it is, the project macro revision it
was saved with, and what it belongs to: an explanation's finding and decision revision
(``parent``, ``parent_revision``) or the comment entry that recorded the revision
(``origin``). The kind of text checks its own parent; this table checks values. The
identity floor and held reason are read from their columns; the block map the writer
stores also carries them, as it always did, and nothing reads that copy.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import Any, Final

Json = dict[str, Any]

_HEADER: Final = (
    "revision,body,sha256,author,next_block,next_span,identity_floor,held,macro_revision,"
    "origin,parent,parent_revision"
)


@dataclass(frozen=True)
class NewRevision:
    """A revision to append: its text, its parse and who wrote it."""

    revision: int
    body: str
    sha256: str
    block_map: str
    author: str
    owners: str
    next_block: int
    next_span: int
    identity_floor: int
    held: str | None = None
    macro_revision: int | None = None
    origin: str | None = None
    parent: str | None = None
    parent_revision: int | None = None


class TextRevisions:
    """The revisions of one kind of text."""

    SUBJECTS: Final = ("comment", "response")

    def __init__(self, subject_type: str) -> None:
        if subject_type not in self.SUBJECTS:
            raise ValueError(f"unknown text subject {subject_type!r}")
        self.subject_type = subject_type

    def latest(self, db: sqlite3.Connection, subject: str) -> tuple[int, str] | None:
        """The latest revision of ``subject`` and its SHA-256, or None before the first."""
        row = db.execute(
            "SELECT revision,sha256 FROM text_revisions WHERE subject_type=? AND subject_id=? "
            "ORDER BY revision DESC LIMIT 1",
            (self.subject_type, subject),
        ).fetchone()
        return None if row is None else (row[0], row[1])

    def read(
        self,
        db: sqlite3.Connection,
        subject: str,
        revision: int | None = None,
        *,
        full: bool = True,
    ) -> Json | None:
        """Revision ``revision`` of ``subject`` (the latest when None), as it was written,
        or None. ``full`` also parses its block map and owners."""
        columns = _HEADER + (",block_map,owners" if full else "")
        row = db.execute(
            f"SELECT {columns} FROM text_revisions WHERE subject_type=? "  # noqa: S608
            "AND subject_id=? AND (? IS NULL OR revision=?) ORDER BY revision DESC LIMIT 1",
            (self.subject_type, subject, revision, revision),
        ).fetchone()
        if row is None:
            return None
        found: Json = dict(zip(_HEADER.split(","), row[:12], strict=True))
        found["author"] = json.loads(found["author"])
        if full:
            found["block_map"], found["owners"] = json.loads(row[12]), json.loads(row[13])
        return found

    def append(self, db: sqlite3.Connection, subject: str, new: NewRevision) -> None:
        """Write ``new`` in the open change ``db``. The caller has checked that it follows
        the latest revision."""
        db.execute(
            "INSERT INTO text_revisions(subject_type,subject_id,revision,parent,"
            "parent_revision,origin,body,sha256,block_map,author,owners,next_block,next_span,"
            "identity_floor,held,macro_revision) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                self.subject_type,
                subject,
                new.revision,
                new.parent,
                new.parent_revision,
                new.origin,
                new.body,
                new.sha256,
                new.block_map,
                new.author,
                new.owners,
                new.next_block,
                new.next_span,
                new.identity_floor,
                new.held,
                new.macro_revision,
            ),
        )
