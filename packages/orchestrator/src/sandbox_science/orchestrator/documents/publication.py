# SPDX-License-Identifier: MIT
"""Atomic publication metadata and bounded export plans; no worker or sandbox I/O.

The SQL hook shares each canonical change's transaction. Retained events are a cache
for deltas; durable project cursors and current identity projections outlive eviction.
Immutable content stays in the document revision store, never in worker-chosen paths.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, Literal

from pydantic import TypeAdapter, ValidationError

from sandbox_science.contracts import Identifier
from sandbox_science.contracts.documents import (
    MAX_CATALOG_ENTRIES,
    MAX_CATALOG_PAGE,
    MAX_DOCUMENT_BYTES,
    MAX_ENTRY_BYTES,
    CatalogChange,
    CatalogDelta,
    CatalogSnapshot,
    PublicationEntry,
    generation_id,
    manifest_sha256,
)

from .errors import INTERNAL, INVALID_REQUEST, NOT_FOUND, DocumentError

if TYPE_CHECKING:
    from .store import DocumentStore

EVENTS_PER_PROJECT: Final = 4096
TOTAL_EVENTS: Final = 65_536
MAX_SEQUENCE: Final = 2**63 - 1
_IDENTIFIER = TypeAdapter[str](Identifier)
_KINDS: Final = frozenset({"document.revision_created", "document.promoted"})
_SCHEMA: Final = (
    """CREATE TABLE IF NOT EXISTS publication_meta (
      singleton INTEGER PRIMARY KEY CHECK(singleton=1), version INTEGER NOT NULL) STRICT""",
    """CREATE TABLE IF NOT EXISTS publication_projects (
      project TEXT PRIMARY KEY, seq INTEGER NOT NULL) STRICT""",
    """CREATE TABLE IF NOT EXISTS publication_current (
      document TEXT PRIMARY KEY REFERENCES documents(document_id), project TEXT NOT NULL,
      entry TEXT NOT NULL) STRICT""",
    "CREATE INDEX IF NOT EXISTS publication_current_project ON publication_current(project)",
    """CREATE TABLE IF NOT EXISTS publication_events (
      id INTEGER PRIMARY KEY AUTOINCREMENT, project TEXT NOT NULL, seq INTEGER NOT NULL,
      source_event INTEGER UNIQUE, document TEXT NOT NULL, entry TEXT NOT NULL,
      UNIQUE(project,seq)) STRICT""",
)


def _capture(db: sqlite3.Connection, document: str) -> tuple[str, str]:
    row = db.execute(
        "SELECT d.project_id,d.session_id,d.name,d.head,d.deleted,r.sha256,"
        "length(cast(r.text AS BLOB)),r.tombstone FROM documents d JOIN revisions r "
        "ON r.document_id=d.document_id AND r.number=d.head WHERE d.document_id=?",
        (document,),
    ).fetchone()
    if row is None or row[4] != row[7]:
        raise DocumentError(INTERNAL, "publication cannot pin the canonical document head")
    project, session, name, revision, deleted, sha, size, _ = row
    # Export validation is deliberately later: a worker quota/protocol refusal must
    # not stop an ordinary canonical write or silently export a partial project.
    entry = json.dumps(
        {
            "document_id": document,
            "revision": revision,
            "scope": "session" if session is not None else "project",
            "session_id": session,
            "name": name,
            "deleted": bool(deleted),
            "content_sha256": None if deleted else sha,
            "content_bytes": 0 if deleted else size,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return str(project), entry


def _append(db: sqlite3.Connection, document: str, source_event: int | None) -> None:
    project, entry = _capture(db, document)
    db.execute("INSERT OR IGNORE INTO publication_projects VALUES (?,0)", (project,))
    (previous,) = db.execute(
        "SELECT seq FROM publication_projects WHERE project=?", (project,)
    ).fetchone()
    if previous >= MAX_SEQUENCE:
        raise DocumentError(INTERNAL, "publication sequence is exhausted")
    seq = previous + 1
    db.execute("UPDATE publication_projects SET seq=? WHERE project=?", (seq, project))
    db.execute(
        "INSERT INTO publication_current VALUES (?,?,?) ON CONFLICT(document) "
        "DO UPDATE SET project=excluded.project,entry=excluded.entry",
        (document, project, entry),
    )
    cursor = db.execute(
        "INSERT INTO publication_events(project,seq,source_event,document,entry) "
        "VALUES (?,?,?,?,?)",
        (project, seq, source_event, document, entry),
    )
    assert cursor.lastrowid is not None  # noqa: S101 - an INSERT assigned this identity
    db.execute(
        "DELETE FROM publication_events WHERE project=? AND seq<=?",
        (project, seq - EVENTS_PER_PROJECT),
    )
    db.execute("DELETE FROM publication_events WHERE id<=?", (cursor.lastrowid - TOTAL_EVENTS,))


def prepare(db: sqlite3.Connection) -> None:
    """Add and bootstrap once, inside the caller's existing BEGIN IMMEDIATE."""
    if not db.in_transaction:
        raise DocumentError(INTERNAL, "publication preparation requires the source transaction")
    for statement in _SCHEMA:
        db.execute(statement)
    version = db.execute("SELECT version FROM publication_meta WHERE singleton=1").fetchone()
    if version is not None:
        if version != (1,):
            raise DocumentError(INTERNAL, "unsupported publication metadata version")
        return
    for (document,) in db.execute(
        "SELECT document_id FROM documents ORDER BY project_id,document_id"
    ):
        _append(db, document, None)
    db.execute("INSERT INTO publication_meta VALUES (1,1)")


def record_event(db: sqlite3.Connection, kind: str, document: str, event: int) -> None:
    """Append authoritative post-change metadata; failure rolls the source write back."""
    if kind not in _KINDS:
        return
    if not db.in_transaction:
        raise DocumentError(INTERNAL, "publication event requires the source transaction")
    _append(db, document, event)


@dataclass(frozen=True)
class PublicationPlan:
    """A complete pinned target and optional complete contiguous delta to that target."""

    snapshot: CatalogSnapshot
    entries: tuple[PublicationEntry, ...]
    mode: Literal["snapshot", "delta", "current"]
    delta: CatalogDelta | None = None


class Publications:
    """Synchronous bounded control-store reads; callers must keep SQL off async loops."""

    def __init__(self, documents: DocumentStore) -> None:
        self.documents = documents

    @staticmethod
    def _ready(db: sqlite3.Connection | None) -> sqlite3.Connection:
        if (
            db is None
            or db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='publication_meta'"
            ).fetchone()
            is None
        ):
            raise DocumentError(
                INTERNAL, "publication metadata is unavailable; initialize a writer"
            )
        if db.execute("SELECT version FROM publication_meta WHERE singleton=1").fetchone() != (1,):
            raise DocumentError(INTERNAL, "publication metadata is not completely initialized")
        return db

    @staticmethod
    def _consistent(db: sqlite3.Connection, project: str) -> None:
        # An already-running older binary can write schema-v1 documents without the
        # Python hook. Never export its newer bytes under an older publication cursor.
        malformed = db.execute(
            "SELECT 1 FROM publication_current WHERE project=? AND "
            "(length(cast(entry AS BLOB))>? OR NOT json_valid(entry)) LIMIT 1",
            (project, MAX_ENTRY_BYTES),
        ).fetchone()
        if malformed:
            raise ValueError("publication projection metadata is malformed or oversized")
        missing = db.execute(
            "SELECT 1 FROM documents d LEFT JOIN publication_current p ON p.document=d.document_id "
            "LEFT JOIN revisions r ON r.document_id=d.document_id AND r.number=d.head "
            "WHERE d.project_id=? AND (p.document IS NULL OR r.document_id IS NULL "
            "OR p.project IS NOT d.project_id OR r.tombstone IS NOT d.deleted "
            "OR json_extract(p.entry,'$.document_id') IS NOT d.document_id "
            "OR json_extract(p.entry,'$.revision') IS NOT d.head "
            "OR json_extract(p.entry,'$.name') IS NOT d.name "
            "OR json_extract(p.entry,'$.session_id') IS NOT d.session_id "
            "OR json_extract(p.entry,'$.scope') IS NOT CASE WHEN d.session_id IS NULL "
            "THEN 'project' ELSE 'session' END "
            "OR json_extract(p.entry,'$.deleted') IS NOT d.deleted "
            "OR json_extract(p.entry,'$.content_sha256') IS NOT CASE WHEN d.deleted=1 "
            "THEN NULL ELSE r.sha256 END OR json_extract(p.entry,'$.content_bytes') IS NOT "
            "CASE WHEN d.deleted=1 THEN 0 ELSE length(cast(r.text AS BLOB)) END) LIMIT 1",
            (project,),
        ).fetchone()
        extra = db.execute(
            "SELECT 1 FROM publication_current p LEFT JOIN documents d ON d.document_id=p.document "
            "WHERE p.project=? AND (d.document_id IS NULL OR d.project_id IS NOT p.project) "
            "LIMIT 1",
            (project,),
        ).fetchone()
        if missing or extra:
            raise DocumentError(INTERNAL, "publication projection differs from canonical documents")

    @staticmethod
    def _entry(raw: bytes) -> PublicationEntry:
        if len(raw) > MAX_ENTRY_BYTES:
            raise ValueError("publication entry exceeds protocol byte bound")
        return PublicationEntry.model_validate_json(raw)

    def plan(self, project: str, *, after: int | None = None) -> PublicationPlan:
        """Pin cursor plus all live metadata, or refuse; never hash a truncated prefix."""
        try:
            _IDENTIFIER.validate_python(project)
        except ValidationError as error:
            raise DocumentError(INVALID_REQUEST, "invalid publication project") from error
        if after is not None and (type(after) is not int or not 0 <= after <= MAX_SEQUENCE):
            raise DocumentError(INVALID_REQUEST, "invalid publication cursor")
        try:
            with self.documents._read() as connection:  # pyright: ignore[reportPrivateUsage]
                db = self._ready(connection)
                self._consistent(db, project)
                row = db.execute(
                    "SELECT seq FROM publication_projects WHERE project=?", (project,)
                ).fetchone()
                seq = row[0] if row else 0
                if after is not None and after > seq:
                    raise DocumentError(
                        INVALID_REQUEST, "publication cursor is ahead of this store"
                    )
                entries = tuple(
                    self._entry(raw)
                    for (raw,) in db.execute(
                        "SELECT substr(cast(entry AS BLOB),1,?) FROM publication_current "
                        "WHERE project=? "
                        "AND json_extract(entry,'$.deleted')=0 ORDER BY document LIMIT ?",
                        (MAX_ENTRY_BYTES + 1, project, MAX_CATALOG_ENTRIES + 1),
                    )
                )
                if len(entries) > MAX_CATALOG_ENTRIES:
                    raise ValueError("complete publication exceeds the catalog entry bound")
                manifest = manifest_sha256(project, seq, entries)
                snapshot = CatalogSnapshot(
                    project_id=project,
                    generation_id=generation_id(seq, manifest),
                    catalog_seq=seq,
                    entry_count=len(entries),
                    total_content_bytes=sum(entry.content_bytes for entry in entries),
                    manifest_sha256=manifest,
                )
                if after == seq:
                    return PublicationPlan(snapshot, entries, "current")
                if after is not None and seq - after <= MAX_CATALOG_PAGE:
                    # Bootstrap captures current identities, not their lifecycle.
                    # An unknown legacy tombstone cannot be replayed into an empty
                    # worker catalog; the resulting sequence gap requires a snapshot.
                    changes = tuple(
                        CatalogChange(catalog_seq=number, entry=self._entry(raw))
                        for number, raw in db.execute(
                            "SELECT seq,substr(cast(entry AS BLOB),1,?) FROM publication_events "
                            "WHERE project=? AND seq>? AND source_event IS NOT NULL "
                            "ORDER BY seq LIMIT ?",
                            (MAX_ENTRY_BYTES + 1, project, after, MAX_CATALOG_PAGE),
                        )
                    )
                    if tuple(item.catalog_seq for item in changes) == tuple(
                        range(after + 1, seq + 1)
                    ):
                        try:
                            delta = CatalogDelta(target=snapshot, after_seq=after, changes=changes)
                        except ValidationError:
                            # Complete changes may exceed the metadata byte cap. The
                            # full target still has a bounded paged snapshot protocol.
                            pass
                        else:
                            return PublicationPlan(snapshot, entries, "delta", delta)
                return PublicationPlan(snapshot, entries, "snapshot")
        except (sqlite3.DatabaseError, ValidationError, ValueError, UnicodeError) as error:
            raise DocumentError(INTERNAL, "complete publication export is unavailable") from error

    def content(self, project: str, entry: PublicationEntry) -> bytes:
        """Read only this project's exact pinned revision, including after a later delete."""
        if entry.deleted:
            raise DocumentError(INVALID_REQUEST, "a publication tombstone has no content")
        with self.documents._read() as connection:  # pyright: ignore[reportPrivateUsage]
            db = self._ready(connection)
            row = db.execute(
                "SELECT substr(cast(r.text AS BLOB),1,?),substr(r.sha256,1,65),"
                "r.tombstone,length(cast(r.text AS BLOB)) FROM revisions r JOIN documents d "
                "ON d.document_id=r.document_id WHERE d.project_id=? AND d.document_id=? "
                "AND r.number=?",
                (MAX_DOCUMENT_BYTES + 1, project, entry.document_id, entry.revision),
            ).fetchone()
            if row is None:
                raise DocumentError(NOT_FOUND, "pinned publication content was not found")
            try:
                content: bytes = row[0]
                content.decode("utf-8", errors="strict")
            except UnicodeError as error:
                raise DocumentError(INTERNAL, "pinned publication is not UTF-8") from error
            if (
                row[2]
                or row[3] > MAX_DOCUMENT_BYTES
                or len(content) > MAX_DOCUMENT_BYTES
                or len(content) != entry.content_bytes
                or row[1] != entry.content_sha256
                or hashlib.sha256(content).hexdigest() != entry.content_sha256
            ):
                raise DocumentError(INTERNAL, "pinned publication bytes differ from their identity")
            return content
