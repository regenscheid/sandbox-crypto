# SPDX-License-Identifier: MIT
"""Living documents in the control store: one SQLite database in the state directory.

``<state-dir>/control.sqlite3`` holds the documents, their append-only revisions with
their block maps, the patches agents submitted (applied, rejected, refused or waiting as
suggestions), the operation IDs that make retries safe, and the events the changes
produced (LD-39). More than one process writes it: each harness run, and the workspace
server once user edits come (slice 2). So every change is one ``BEGIN IMMEDIATE``
transaction, which takes SQLite's write lock before it reads the current revision, and
commits the revision, the patch, the operation ID and the events together, or nothing.
The database uses write-ahead logging, so readers never wait for a writer, and
``synchronous=FULL``, so a change reported done survives a crash.

Revisions and events cannot be updated or deleted: triggers refuse it, as a guard
against mistakes (anyone who can write the file can drop them). The schema's version is
SQLite's ``user_version``; a database in another version is refused.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import threading
from collections import OrderedDict
from collections.abc import Callable, Generator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final, cast

from pydantic import TypeAdapter, ValidationError

from sandbox_science.contracts import new_id

from .blocks import BYTE_ORDER_MARK, Budget, check_text, normalize_newlines, parse
from .checks import math_check, warnings
from .engine import (
    Applied,
    Since,
    apply_operations,
    attach,
    block_map,
    check_names,
    conflicts,
    structural_added,
    summary,
    unnamed_changes,
    written,
)
from .errors import (
    BUSY,
    CONFLICT,
    INTERNAL,
    INVALID_REQUEST,
    NOT_FOUND,
    POLICY_DENIED,
    DocumentError,
)
from .identity import Allocator, Changes, Mapped, carry, compare, fresh
from .models import (
    MAX_REASON,
    MAX_TITLE,
    NAME,
    OPERATION_ID,
    Author,
    ChangeResult,
    DocumentInfo,
    EventRecord,
    Outcome,
    PatchInfo,
    Revision,
    RevisionInfo,
)
from .normalize import Normalized, normalize
from .operations import MAX_OPERATIONS, Operation, touches
from .parser import parser_versions

FILE_NAME: Final = "control.sqlite3"
SCHEMA_VERSION: Final = 1
DEFAULT_TIMEOUT: Final = 10.0
"""Seconds a change waits for another process's write to finish."""
CACHED: Final = 16
"""Parsed revisions kept, by document, revision and content hash."""

_SCHEMA: Final = (
    """CREATE TABLE documents (
    document_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    session_id TEXT,
    name TEXT NOT NULL,
    title TEXT NOT NULL,
    created_at TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_session TEXT,
    takes_suggestions INTEGER NOT NULL DEFAULT 0,
    head INTEGER NOT NULL,
    deleted INTEGER NOT NULL DEFAULT 0,
    next_block INTEGER NOT NULL,
    next_span INTEGER NOT NULL
) STRICT""",
    """CREATE UNIQUE INDEX documents_names
    ON documents (project_id, coalesce(session_id, ''), name) WHERE deleted = 0""",
    """CREATE TABLE revisions (
    document_id TEXT NOT NULL REFERENCES documents (document_id),
    number INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    author TEXT NOT NULL,
    accepted_by TEXT,
    session_id TEXT,
    base INTEGER,
    parent INTEGER,
    reason TEXT NOT NULL,
    operations TEXT NOT NULL,
    patch_id TEXT,
    text TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    tombstone INTEGER NOT NULL,
    normalized TEXT NOT NULL,
    warnings TEXT NOT NULL,
    block_map TEXT NOT NULL,
    parser TEXT NOT NULL,
    PRIMARY KEY (document_id, number)
) STRICT""",
    """CREATE TABLE patches (
    patch_id TEXT PRIMARY KEY,
    document_id TEXT NOT NULL REFERENCES documents (document_id),
    created_at TEXT NOT NULL,
    author TEXT NOT NULL,
    session_id TEXT,
    base INTEGER NOT NULL,
    reason TEXT NOT NULL,
    operations TEXT NOT NULL,
    state TEXT NOT NULL,
    revision INTEGER,
    details TEXT NOT NULL,
    decided_at TEXT,
    decided_by TEXT
) STRICT""",
    "CREATE INDEX patches_documents ON patches (document_id, created_at)",
    """CREATE TABLE requests (
    project_id TEXT NOT NULL,
    actor TEXT NOT NULL,
    operation_id TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    result TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (project_id, actor, operation_id)
) STRICT""",
    """CREATE TABLE events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    time TEXT NOT NULL,
    kind TEXT NOT NULL,
    producer TEXT NOT NULL,
    project_id TEXT NOT NULL,
    session_id TEXT,
    document_id TEXT NOT NULL,
    payload TEXT NOT NULL
) STRICT""",
    "CREATE INDEX events_projects ON events (project_id, seq)",
    """CREATE TRIGGER revisions_no_update BEFORE UPDATE ON revisions
BEGIN SELECT RAISE(ABORT, 'revisions are immutable'); END""",
    """CREATE TRIGGER revisions_no_delete BEFORE DELETE ON revisions
BEGIN SELECT RAISE(ABORT, 'revisions are append-only'); END""",
    """CREATE TRIGGER events_no_update BEFORE UPDATE ON events
BEGIN SELECT RAISE(ABORT, 'events are immutable'); END""",
    """CREATE TRIGGER events_no_delete BEFORE DELETE ON events
BEGIN SELECT RAISE(ABORT, 'events are append-only'); END""",
)

_DOCUMENT: Final = (
    "SELECT document_id, project_id, session_id, name, title, created_at, created_by, "
    "created_session, takes_suggestions, head, deleted, next_block, next_span FROM documents"
)
_REVISION: Final = (
    "SELECT document_id, number, created_at, author, accepted_by, session_id, base, parent, "
    "reason, operations, patch_id, sha256, tombstone, normalized, warnings, parser "
    "FROM revisions"
)
_PATCH: Final = (
    "SELECT patch_id, document_id, created_at, author, session_id, base, reason, operations, "
    "state, revision, details, decided_at, decided_by FROM patches"
)
_OPERATIONS: Final = TypeAdapter(list[Operation])
_SPACE: Final = re.compile(r"\s+")

Json = dict[str, Any]


def _now() -> datetime:
    return datetime.now(UTC)


def _dumps(value: object) -> str:
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False)


def _time(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


@dataclass
class _Row:
    """A document's row, as a change reads it under the write lock."""

    info: DocumentInfo
    next_block: int
    next_span: int


@dataclass(frozen=True)
class _Submission:
    """A patch as submitted, which the records of its outcome keep."""

    patch_id: str
    base: int
    operations: list[Operation]
    raw: list[Json]
    """The operations as JSON, as they are stored."""
    reason: str
    author: Author
    accepted_by: Author | None = None
    """The user, when an accepted suggestion is being applied."""

    @property
    def attribution(self) -> Json:
        found: Json = {"author": self.author.model_dump(mode="json", exclude_none=True)}
        if self.accepted_by is not None:
            found["accepted_by"] = self.accepted_by.model_dump(mode="json", exclude_none=True)
        return found


def _key(row: _Row) -> tuple[object, ...]:
    """What a change computed from a document's row depends on: the same key, the same
    result."""
    info = row.info
    return (info.document_id, info.head, row.next_block, row.next_span, info.deleted)


@dataclass(frozen=True)
class _Inputs:
    """What a patch is checked against, read from one state of its document."""

    key: tuple[object, ...]
    base: Revision
    head: Revision
    since: list[tuple[int, str, str]]
    """The later revisions' numbers, authors and block maps, as stored."""


@dataclass
class _Assessed:
    """A patch checked against one state of its document, before anything is written."""

    key: tuple[object, ...]
    error: DocumentError | None = None
    deleted: bool = False
    conflicts: list[Json] = field(default_factory=list[Json])
    unnamed: list[str] = field(default_factory=list[str])
    head: Mapped | None = None
    applied: Applied | None = None
    allocate: Allocator | None = None


@dataclass(frozen=True)
class _Carried:
    """A saved text's IDs, carried from one state of its document."""

    key: tuple[object, ...]
    head: Mapped
    mapped: Mapped
    allocate: Allocator


def _attempt[T](compute: Callable[[], T]) -> T | DocumentError:
    """What ``compute`` returns, or the error it refuses with, to raise in its turn."""
    try:
        return compute()
    except DocumentError as error:
        return error


def _busy(error: sqlite3.OperationalError) -> bool:
    return any(word in str(error).lower() for word in ("locked", "busy"))


def _document(row: Sequence[Any]) -> _Row:
    info = DocumentInfo(
        document_id=row[0],
        project_id=row[1],
        scope="session" if row[2] is not None else "project",
        session_id=row[2],
        name=row[3],
        title=row[4],
        created_at=datetime.fromisoformat(row[5]),
        created_by=Author.model_validate_json(row[6]),
        created_session=row[7],
        takes_suggestions=bool(row[8]),
        head=row[9],
        deleted=bool(row[10]),
    )
    return _Row(info, row[11], row[12])


def _revision(row: Sequence[Any]) -> RevisionInfo:
    return RevisionInfo(
        document_id=row[0],
        number=row[1],
        created_at=datetime.fromisoformat(row[2]),
        author=Author.model_validate_json(row[3]),
        accepted_by=None if row[4] is None else Author.model_validate_json(row[4]),
        session_id=row[5],
        base=row[6],
        parent=row[7],
        reason=row[8],
        operations=json.loads(row[9]),
        patch_id=row[10],
        sha256=row[11],
        tombstone=bool(row[12]),
        normalized=json.loads(row[13]),
        warnings=json.loads(row[14]),
        parser=json.loads(row[15]),
    )


def _patch(row: Sequence[Any]) -> PatchInfo:
    return PatchInfo(
        patch_id=row[0],
        document_id=row[1],
        created_at=datetime.fromisoformat(row[2]),
        author=Author.model_validate_json(row[3]),
        session_id=row[4],
        base=row[5],
        reason=row[6],
        operations=json.loads(row[7]),
        state=row[8],
        revision=row[9],
        details=json.loads(row[10]),
        decided_at=None if row[11] is None else datetime.fromisoformat(row[11]),
        decided_by=None if row[12] is None else Author.model_validate_json(row[12]),
    )


def _reason(reason: str) -> str:
    text = _SPACE.sub(" ", reason).strip()
    if not text:
        raise DocumentError(INVALID_REQUEST, "give a short reason for the change")
    if len(text) > MAX_REASON:
        raise DocumentError(INVALID_REQUEST, f"the reason is longer than {MAX_REASON} characters")
    return text


def _name(name: str) -> str:
    stem = name.removesuffix(".md")
    if NAME.fullmatch(stem) is None:
        raise DocumentError(
            INVALID_REQUEST,
            f"invalid name {name[:80]!r}: use up to 64 letters, digits, '.', '_' and '-', "
            "starting with a letter or digit",
        )
    return stem


def _operations(raw: object) -> list[Operation]:
    try:
        operations = _OPERATIONS.validate_python(raw)
    except ValidationError as exc:
        first = exc.errors()[0]
        where = ".".join(str(part) for part in first.get("loc", ()))
        raise DocumentError(INVALID_REQUEST, f"operations.{where}: {first.get('msg')}") from None
    if not 1 <= len(operations) <= MAX_OPERATIONS:
        raise DocumentError(INVALID_REQUEST, f"a patch has 1 to {MAX_OPERATIONS} operations")
    return operations


class DocumentStore:
    """The control store's documents, at ``path``.

    ``producer`` names the service that records the events this object writes, such as
    ``harness``. A read-only store opens the database only to read, and finds nothing
    until a writer has made it.
    """

    def __init__(
        self,
        path: Path,
        *,
        producer: str = "harness",
        readonly: bool = False,
        timeout: float = DEFAULT_TIMEOUT,
        clock: Callable[[], datetime] = _now,
    ) -> None:
        self.path = path
        self.producer = producer
        self.readonly = readonly
        self._timeout = timeout
        self._clock = clock
        self._lock = threading.RLock()
        self._db: sqlite3.Connection | None = None
        self._cache: OrderedDict[tuple[str, int, str], Mapped] = OrderedDict()

    def initialize(self) -> None:
        """Open or initialize the control store before another control component adds tables."""
        with self._lock:
            self._connection()

    def close(self) -> None:
        with self._lock:
            if self._db is not None:
                self._db.close()
                self._db = None

    # Connections and transactions.

    def _connection(self) -> sqlite3.Connection | None:
        if self._db is not None:
            return self._db
        if self.readonly:
            if not self.path.is_file():
                return None
            db = sqlite3.connect(
                self.path.resolve().as_uri() + "?mode=ro",
                uri=True,
                timeout=self._timeout,
                isolation_level=None,
                check_same_thread=False,
            )
            (version,) = db.execute("PRAGMA user_version").fetchone()
            if version != SCHEMA_VERSION:
                db.close()
                raise DocumentError(
                    INTERNAL, f"{self.path} is in store format {version}, not {SCHEMA_VERSION}"
                )
        else:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            db = sqlite3.connect(
                self.path, timeout=self._timeout, isolation_level=None, check_same_thread=False
            )
            try:
                self._prepare(db)
            except BaseException:
                db.close()
                raise
        self._db = db
        return db

    def _prepare(self, db: sqlite3.Connection) -> None:
        (mode,) = db.execute("PRAGMA journal_mode=WAL").fetchone()
        if str(mode).lower() != "wal":
            raise DocumentError(INTERNAL, f"{self.path} cannot use write-ahead logging")
        db.execute("PRAGMA synchronous=FULL")
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("BEGIN IMMEDIATE")
        try:
            (version,) = db.execute("PRAGMA user_version").fetchone()
            if version == 0:
                (objects,) = db.execute("SELECT count(*) FROM sqlite_master").fetchone()
                if objects:
                    raise DocumentError(INTERNAL, f"{self.path} is not a control store")
                for statement in _SCHEMA:
                    db.execute(statement)
                db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            elif version != SCHEMA_VERSION:
                raise DocumentError(
                    INTERNAL,
                    f"{self.path} is in store format {version}; this version reads "
                    f"{SCHEMA_VERSION}",
                )
            from .annotations import SCHEMA as ANNOTATION_SCHEMA

            for statement in ANNOTATION_SCHEMA:
                db.execute(statement)
            db.execute("COMMIT")
        finally:
            if db.in_transaction:
                db.execute("ROLLBACK")

    @contextmanager
    def _write(self) -> Generator[sqlite3.Connection]:
        """One change: SQLite's write lock is taken before anything is read."""
        if self.readonly:
            raise DocumentError(INTERNAL, "this document store is read-only")
        with self._lock:
            try:
                db = self._connection()
                assert db is not None  # noqa: S101 - a writer always has a database
                db.execute("BEGIN IMMEDIATE")
            except sqlite3.OperationalError as error:
                if not _busy(error):
                    raise
                raise DocumentError(
                    BUSY,
                    f"another change held the document store for over {self._timeout:g} "
                    "seconds: try again (with the same operation ID, a retry is safe)",
                ) from error
            try:
                yield db
                db.execute("COMMIT")
            except BaseException:
                if db.in_transaction:
                    db.execute("ROLLBACK")
                raise

    @contextmanager
    def _read(self) -> Generator[sqlite3.Connection | None]:
        """A consistent view for reads that take several queries."""
        with self._lock:
            db = self._connection()
            if db is None:
                yield None
                return
            db.execute("BEGIN")
            try:
                yield db
            finally:
                if db.in_transaction:
                    db.execute("COMMIT")

    # Reads.

    def documents(self, project_id: str, *, deleted: bool = False) -> list[DocumentInfo]:
        """The project's documents, project and workbook ones, by name."""
        with self._read() as db:
            if db is None:
                return []
            rows = db.execute(
                _DOCUMENT + " WHERE project_id = ? ORDER BY session_id IS NOT NULL, session_id, "
                "name, created_at",
                (project_id,),
            ).fetchall()
        found = [_document(row).info for row in rows]
        return [info for info in found if deleted or not info.deleted]

    def document(
        self, project_id: str, reference: str, session_id: str | None = None
    ) -> DocumentInfo:
        """A document by ID, or by name: the session's workbook document first, then the
        project's."""
        with self._read() as db:
            return self._find(db, project_id, reference, session_id).info

    def _find(
        self,
        db: sqlite3.Connection | None,
        project_id: str,
        reference: str,
        session_id: str | None = None,
    ) -> _Row:
        if db is None:
            raise DocumentError(NOT_FOUND, f"no document {reference!r}")
        row = db.execute(
            _DOCUMENT + " WHERE project_id = ? AND document_id = ?", (project_id, reference)
        ).fetchone()
        if row is None:
            name = reference.removesuffix(".md")
            candidates = db.execute(
                _DOCUMENT + " WHERE project_id = ? AND name = ? AND deleted = 0 "
                "AND (session_id IS NULL OR session_id = ?) ORDER BY session_id IS NULL",
                (project_id, name, session_id),
            ).fetchall()
            row = candidates[0] if candidates else None
        if row is None:
            raise DocumentError(NOT_FOUND, f"project {project_id!r} has no document {reference!r}")
        return _document(row)

    def revision(self, project_id: str, reference: str, number: int | None = None) -> Revision:
        """A revision with its text and block map; the current one without ``number``."""
        with self._read() as db:
            row = self._find(db, project_id, reference)
            return self._revision(db, row.info.document_id, number or row.info.head)

    def _revision(self, db: sqlite3.Connection | None, document_id: str, number: int) -> Revision:
        assert db is not None  # noqa: S101 - a document was found in it
        found = db.execute(
            _REVISION + " WHERE document_id = ? AND number = ?", (document_id, number)
        ).fetchone()
        if found is None:
            raise DocumentError(NOT_FOUND, f"document {document_id} has no revision {number}")
        text, stored = db.execute(
            "SELECT text, block_map FROM revisions WHERE document_id = ? AND number = ?",
            (document_id, number),
        ).fetchone()
        return Revision(info=_revision(found), text=text, block_map=json.loads(stored))

    def mapped(self, project_id: str, reference: str, number: int | None = None) -> Mapped:
        """A revision's parse with its blocks' and spans' IDs."""
        revision = self.revision(project_id, reference, number)
        return self._mapped(revision)

    def _mapped(self, revision: Revision) -> Mapped:
        key = (revision.info.document_id, revision.info.number, revision.info.sha256)
        with self._lock:
            cached = self._cache.get(key)
            if cached is not None:
                self._cache.move_to_end(key)
                return cached
        mapped = attach(parse(revision.text), revision.block_map)
        with self._lock:
            self._cache[key] = mapped
            while len(self._cache) > CACHED:
                self._cache.popitem(last=False)
        return mapped

    def revisions(self, project_id: str, reference: str, after: int = 0) -> list[RevisionInfo]:
        """A document's revisions after ``after``, oldest first, without their texts."""
        with self._read() as db:
            row = self._find(db, project_id, reference)
            assert db is not None  # noqa: S101
            rows = db.execute(
                _REVISION + " WHERE document_id = ? AND number > ? ORDER BY number",
                (row.info.document_id, after),
            ).fetchall()
        return [_revision(found) for found in rows]

    def changes(
        self, project_id: str, reference: str, after: int = 0
    ) -> list[tuple[RevisionInfo, Json]]:
        """The revisions after ``after``, oldest first, with their block maps but not their
        texts."""
        with self._read() as db:
            row = self._find(db, project_id, reference)
            assert db is not None  # noqa: S101
            rows = db.execute(
                _REVISION.replace("FROM revisions", ", block_map FROM revisions")
                + " WHERE document_id = ? AND number > ? ORDER BY number",
                (row.info.document_id, after),
            ).fetchall()
        return [(_revision(found), json.loads(found[16])) for found in rows]

    def history(self, project_id: str, reference: str) -> Json:
        """Who changed what (LD-11): each revision with its author, reason and changed
        blocks; for each block, the revisions that changed it and their authors; and the
        patches that made no revision of their own, with their states and decisions."""
        with self._read() as db:
            row = self._find(db, project_id, reference)
            assert db is not None  # noqa: S101
            rows = db.execute(
                _REVISION.replace("FROM revisions", ", block_map FROM revisions")
                + " WHERE document_id = ? ORDER BY number",
                (row.info.document_id,),
            ).fetchall()
            patches = db.execute(
                _PATCH + " WHERE document_id = ? ORDER BY created_at, rowid",
                (row.info.document_id,),
            ).fetchall()
        revisions: list[Json] = []
        blocks: dict[str, list[Json]] = {}
        for found in rows:
            info = _revision(found)
            stored = cast(Json, json.loads(found[16]))
            author = info.author.model_dump(mode="json", exclude_none=True)
            statuses: dict[str, list[str]] = {
                "changed": [],
                "added": [],
                "moved": [],
                "removed": [],
            }
            for entry in cast(list[Json], stored.get("blocks", [])):
                status = str(entry.get("status"))
                if status in statuses:
                    statuses[status].append(str(entry["id"]))
                    blocks.setdefault(str(entry["id"]), []).append(
                        {"revision": info.number, "status": status, "author": author}
                    )
            for entry in cast(list[Json], stored.get("removed", [])):
                statuses["removed"].append(str(entry["id"]))
                blocks.setdefault(str(entry["id"]), []).append(
                    {"revision": info.number, "status": "removed", "author": author}
                )
            revisions.append(
                {
                    "number": info.number,
                    "created_at": info.created_at.isoformat(),
                    "author": author,
                    "accepted_by": None
                    if info.accepted_by is None
                    else info.accepted_by.model_dump(mode="json", exclude_none=True),
                    "reason": info.reason,
                    "base": info.base,
                    "parent": info.parent,
                    "patch_id": info.patch_id,
                    "tombstone": info.tombstone,
                    "normalized": bool(info.normalized.get("converted")),
                    "warnings": len(info.warnings),
                    **statuses,
                }
            )
        return {
            "document": row.info.model_dump(mode="json"),
            "revisions": revisions,
            "blocks": blocks,
            "patches": [
                _patch(found).model_dump(mode="json", exclude_none=True) for found in patches
            ],
        }

    def heads(self, document_ids: Sequence[str]) -> dict[str, int]:
        """The current revision of each document."""
        if not document_ids:
            return {}
        with self._read() as db:
            if db is None:
                return {}
            marks = ",".join("?" * len(document_ids))
            rows = db.execute(
                f"SELECT document_id, head FROM documents WHERE document_id IN ({marks})",  # noqa: S608 - placeholders only
                tuple(document_ids),
            ).fetchall()
        return dict(rows)

    def patches(
        self, project_id: str, reference: str, *, states: Sequence[str] = ()
    ) -> list[PatchInfo]:
        """A document's patches, oldest first: all of them, or those in ``states``."""
        with self._read() as db:
            row = self._find(db, project_id, reference)
            assert db is not None  # noqa: S101
            rows = db.execute(
                _PATCH + " WHERE document_id = ? ORDER BY created_at, rowid",
                (row.info.document_id,),
            ).fetchall()
        found = [_patch(patch) for patch in rows]
        return [patch for patch in found if not states or patch.state in states]

    def events(self, project_id: str, *, after: int = 0, limit: int = 1000) -> list[EventRecord]:
        """The project's events after sequence number ``after``, oldest first."""
        with self._read() as db:
            if db is None:
                return []
            rows = db.execute(
                "SELECT seq, time, kind, producer, project_id, session_id, document_id, payload "
                "FROM events WHERE project_id = ? AND seq > ? ORDER BY seq LIMIT ?",
                (project_id, after, limit),
            ).fetchall()
        return [
            EventRecord(
                seq=row[0],
                time=datetime.fromisoformat(row[1]),
                kind=row[2],
                producer=row[3],
                project_id=row[4],
                session_id=row[5],
                document_id=row[6],
                payload=json.loads(row[7]),
            )
            for row in rows
        ]

    # Changes.

    def create(
        self,
        project_id: str,
        *,
        name: str,
        title: str,
        text: str,
        author: Author,
        session_id: str | None = None,
        reason: str = "created",
        operation_id: str | None = None,
    ) -> ChangeResult:
        """A new document with its first revision; ``session_id`` makes it a workbook's."""
        if author.kind not in ("user", "agent", "import", "platform"):
            raise DocumentError(POLICY_DENIED, "captured worker edits cannot create documents")
        stem = _name(name)
        title = _SPACE.sub(" ", title).strip()
        if not 0 < len(title) <= MAX_TITLE:
            raise DocumentError(INVALID_REQUEST, f"a title has 1 to {MAX_TITLE} characters")
        text = normalize_newlines(text).removeprefix(BYTE_ORDER_MARK)
        check_text(text)
        why = _reason(reason)
        request = {"create": stem, "title": title, "session": session_id, "text": text}
        # Parsed before the write lock is taken, so other changes do not wait for it.
        made = _attempt(lambda: normalize(parse(text)))
        with self._write() as db:
            replay = self._replay(db, project_id, author, operation_id, request)
            if replay is not None:
                return replay
            taken = db.execute(
                "SELECT 1 FROM documents WHERE project_id = ? AND coalesce(session_id, '') = ? "
                "AND name = ? AND deleted = 0",
                (project_id, session_id or "", stem),
            ).fetchone()
            if taken is not None:
                where = f"session {session_id}'s workbook" if session_id else "the project"
                raise DocumentError(CONFLICT, f"{where} has a document named {stem!r} already")
            if isinstance(made, DocumentError):
                raise made
            normalized = made
            allocate = Allocator(0, 0)
            mapped = fresh(normalized.parsed, allocate)
            now = self._clock()
            document_id = new_id("doc")
            info = DocumentInfo(
                document_id=document_id,
                project_id=project_id,
                scope="session" if session_id else "project",
                session_id=session_id,
                name=stem,
                title=title,
                created_at=now,
                created_by=author,
                created_session=author.session_id,
                takes_suggestions=False,
                head=0,
                deleted=False,
            )
            db.execute(
                "INSERT INTO documents (document_id, project_id, session_id, name, title, "
                "created_at, created_by, created_session, takes_suggestions, head, deleted, "
                "next_block, next_span) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, 0, 0, 0, 0)",
                (
                    document_id,
                    project_id,
                    session_id,
                    stem,
                    title,
                    _time(now),
                    info.created_by.model_dump_json(),
                    info.created_session,
                ),
            )
            self._event(
                db,
                "document.created",
                info,
                author,
                {
                    "name": stem,
                    "title": title,
                    "scope": info.scope,
                    "author": author.model_dump(mode="json", exclude_none=True),
                },
            )
            result = self._commit(
                db,
                _Row(info, 0, 0),
                mapped,
                None,
                allocate,
                author=author,
                base=None,
                reason=why,
                operations=[{"op": "create"}],
                normalized={
                    "converted": normalized.converted,
                    "kept": list(normalized.kept),
                },
                outcome="created",
            )
            self._remember(db, project_id, author, operation_id, request, result)
            return result

    def save_text(
        self,
        project_id: str,
        reference: str,
        *,
        base: int,
        text: str,
        author: Author,
        reason: str,
        operation_id: str | None = None,
    ) -> ChangeResult:
        """A whole new text, from the current revision: the user's save or an import.

        Its delimiters are normalized throughout, and IDs carry over by the identity
        rules alone. A save from an older revision is refused; reconciling it is a later
        slice's (LD-13).
        """
        if author.kind not in ("user", "import", "platform"):
            raise DocumentError(POLICY_DENIED, "delegated authors change documents with patches")
        text = normalize_newlines(text).removeprefix(BYTE_ORDER_MARK)
        check_text(text)
        why = _reason(reason)
        request = {"save": reference, "base": base, "text": text}
        # Parsed, and its IDs carried from the current revision, before the write lock is
        # taken; under it, they are used if the document is still as it was.
        made = _attempt(lambda: normalize(parse(text)))
        carried = None
        if isinstance(made, Normalized):
            carried = self._prepare_save(project_id, reference, base, made)
        with self._write() as db:
            replay = self._replay(db, project_id, author, operation_id, request)
            if replay is not None:
                return replay
            row = self._find(db, project_id, reference)
            self._live(row)
            if base != row.info.head:
                raise DocumentError(
                    CONFLICT,
                    f"revision {row.info.head} is current, not {base}: save from it",
                    {"current": row.info.head},
                )
            if isinstance(made, DocumentError):
                raise made
            normalized = made
            if carried is not None and carried.key == _key(row):
                head, mapped, allocate = carried.head, carried.mapped, carried.allocate
            else:
                head = self._mapped(self._revision(db, row.info.document_id, base))
                allocate = Allocator(row.next_block, row.next_span)
                mapped = carry(head, normalized.parsed, allocate)
            result = self._commit(
                db,
                row,
                mapped,
                head,
                allocate,
                author=author,
                base=base,
                reason=why,
                operations=[{"op": "save"}],
                normalized={"converted": normalized.converted, "kept": list(normalized.kept)},
                outcome="applied",
            )
            self._remember(db, project_id, author, operation_id, request, result)
            return result

    def patch(
        self,
        project_id: str,
        reference: str,
        *,
        base: int,
        operations: object,
        reason: str,
        author: Author,
        operation_id: str,
    ) -> ChangeResult:
        """An agent's or the user's patch (LD-16 to LD-20).

        Applied to the current revision when nothing it names changed since its base,
        rebased when need be; kept as a rejected patch on a conflict; refused when it
        would change blocks it does not name; and kept as a suggestion when the
        document takes agent patches as suggestions.
        """
        if author.kind != "user" and not author.delegated:
            raise DocumentError(POLICY_DENIED, "patches need user or delegated authority")
        if OPERATION_ID.fullmatch(operation_id) is None:
            raise DocumentError(
                INVALID_REQUEST,
                "operation_id is up to 128 letters, digits, '.', '_', ':' and '-'",
            )
        parsed = _operations(operations)
        raw = [operation.model_dump(mode="json", by_alias=True) for operation in parsed]
        why = _reason(reason)
        request = {"patch": reference, "base": base, "operations": raw, "reason": why}
        submission = _Submission(new_id("patch"), base, parsed, raw, why, author)
        # Checked before the write lock is taken, so other changes do not wait for it.
        prepared = self._prepare_patch(project_id, reference, submission)
        with self._write() as db:
            replay = self._replay(db, project_id, author, operation_id, request)
            if replay is not None:
                return replay
            row = self._find(db, project_id, reference)
            suggest = author.delegated and row.info.takes_suggestions
            assessed = self._assessed(db, row, submission, prepared)
            result = self._act(db, row, submission, assessed, suggest=suggest)
            self._remember(db, project_id, author, operation_id, request, result)
            return result

    def edit_source(
        self,
        project_id: str,
        reference: str,
        *,
        base: int,
        text: str,
        author: Author,
        reason: str,
        operation_id: str,
    ) -> ChangeResult:
        """Reconcile a user's source draft by the blocks changed from its named base."""
        self._user(author)
        self._operation_id(operation_id)
        text = normalize_newlines(text).removeprefix(BYTE_ORDER_MARK)
        check_text(text)
        submission, prepared = self._prepare_source(
            project_id, reference, base=base, text=text, author=author, reason=reason
        )
        if not submission.operations:
            # Preserve the user's existing empty-draft operation fingerprint across
            # upgrades. Delegated callers use the prepared engine, never this path.
            return self.save_text(
                project_id,
                reference,
                base=base,
                text=text,
                author=author,
                reason=reason,
                operation_id=operation_id,
            )
        request = {"source": reference, "base": base, "text": text, "reason": submission.reason}
        with self._write() as db:
            replay = self._replay(db, project_id, author, operation_id, request)
            if replay is not None:
                return replay
            row = self._find(db, project_id, reference)
            if prepared is None or prepared.key != _key(row):
                raise DocumentError(
                    BUSY,
                    "the document changed while preparing this edit; "
                    "retry with the same operation ID",
                )
            result = self._act(db, row, submission, prepared)
            self._remember(db, project_id, author, operation_id, request, result)
            return result

    def _prepare_source(
        self,
        project_id: str,
        reference: str,
        *,
        base: int,
        text: str,
        author: Author,
        reason: str,
    ) -> tuple[_Submission, _Assessed | None]:
        """Prepare named source operations outside a write; callers fence the exact key."""
        from .source import operations as source_operations

        text = normalize_newlines(text).removeprefix(BYTE_ORDER_MARK)
        check_text(text)
        mapped = self.mapped(project_id, reference, base)
        budget = Budget()
        operations = source_operations(mapped, text, budget)
        if not operations and mapped.block_ids:
            # Whitespace-only changes still take the named, fenced patch path.
            operations = _operations(
                [
                    {
                        "op": "replace",
                        "block": mapped.block_ids[0],
                        "markdown": mapped.parsed.blocks[0].text,
                    }
                ]
            )
        why = _reason(reason)
        raw = [op.model_dump(mode="json", by_alias=True) for op in operations]
        submission = _Submission(new_id("patch"), base, operations, raw, why, author)
        budget.charge(len(text))
        made = normalize(parse(text), budget=budget)
        prepared = self._prepare_patch(project_id, reference, submission, budget)
        if prepared is not None and prepared.applied is not None and prepared.allocate is not None:
            if prepared.key[1] == base:
                # Preserve the user's exact separating whitespace when no rebase
                # is needed. IDs come from the named operations, including moves.
                final = carry(prepared.applied.mapped, made.parsed, prepared.allocate)
                assert prepared.head is not None  # noqa: S101
                prepared.unnamed = unnamed_changes(
                    prepared.head, final, [touches(op, mapped) for op in operations]
                )
                prepared.applied.mapped = final
            prepared.applied.converted.update(made.converted)
            prepared.applied.kept.extend(made.kept)
        return submission, prepared

    @staticmethod
    def _user(author: Author) -> None:
        if author.kind != "user":
            raise DocumentError(POLICY_DENIED, "only the user may make this document change")

    @staticmethod
    def _operation_id(operation_id: str) -> None:
        if OPERATION_ID.fullmatch(operation_id) is None:
            raise DocumentError(INVALID_REQUEST, "invalid operation_id")

    def promote(
        self,
        project_id: str,
        reference: str,
        *,
        base: int,
        name: str,
        author: Author,
        reason: str,
        operation_id: str,
    ) -> ChangeResult:
        """Move a workbook document to project scope, keeping its identity and history."""
        self._user(author)
        self._operation_id(operation_id)
        stem, why = _name(name), _reason(reason)
        request = {"promote": reference, "base": base, "name": stem, "reason": why}
        with self._write() as db:
            replay = self._replay(db, project_id, author, operation_id, request)
            if replay is not None:
                return replay
            row = self._find(db, project_id, reference)
            self._live(row)
            self._current(row, base)
            if row.info.scope != "session":
                raise DocumentError(INVALID_REQUEST, "this document is already project scoped")
            collision = db.execute(
                "SELECT 1 FROM documents WHERE project_id = ? AND session_id IS NULL "
                "AND name = ? AND deleted = 0",
                (project_id, stem),
            ).fetchone()
            if collision is not None:
                raise DocumentError(CONFLICT, "the project already has a document with that name")
            db.execute(
                "UPDATE documents SET session_id = NULL, name = ? WHERE document_id = ?",
                (stem, row.info.document_id),
            )
            self._event(
                db,
                "document.promoted",
                row.info,
                author,
                {
                    "from_session": row.info.session_id,
                    "name": stem,
                    "reason": why,
                    "author": author.model_dump(mode="json", exclude_none=True),
                },
            )
            result = ChangeResult(
                outcome="applied",
                document_id=row.info.document_id,
                revision=row.info.head,
                base=base,
                message="moved to project scope",
            )
            self._remember(db, project_id, author, operation_id, request, result)
            return result

    @staticmethod
    def _current(row: _Row, base: int) -> None:
        if row.info.head != base:
            raise DocumentError(
                CONFLICT,
                f"revision {row.info.head} is current, not {base}: review it first",
                {"current": row.info.head},
            )

    def revert(
        self,
        project_id: str,
        reference: str,
        *,
        base: int,
        target: int,
        author: Author,
        reason: str,
        operation_id: str,
    ) -> ChangeResult:
        """Append a user revision restoring the target's text and block/math IDs."""
        self._user(author)
        self._operation_id(operation_id)
        why = _reason(reason)
        request = {"revert": reference, "base": base, "target": target, "reason": why}
        revision = self.revision(project_id, reference, target)
        if revision.info.tombstone:
            raise DocumentError(INVALID_REQUEST, "choose a revision containing text")
        restored = self._mapped(revision)
        head = self.mapped(project_id, reference, base)
        with self._write() as db:
            replay = self._replay(db, project_id, author, operation_id, request)
            if replay is not None:
                return replay
            row = self._find(db, project_id, reference)
            self._live(row)
            self._current(row, base)
            result = self._commit(
                db,
                row,
                restored,
                head,
                Allocator(row.next_block, row.next_span),
                author=author,
                base=base,
                reason=why,
                operations=[{"op": "revert", "revision": target}],
                normalized={},
                outcome="applied",
                force=restored.block_ids != head.block_ids or restored.span_ids != head.span_ids,
            )
            self._remember(db, project_id, author, operation_id, request, result)
            return result

    def _review_patch(self, db: sqlite3.Connection, row: _Row, patch_id: str) -> PatchInfo:
        found = db.execute(
            _PATCH + " WHERE document_id = ? AND patch_id = ?", (row.info.document_id, patch_id)
        ).fetchone()
        if found is None:
            raise DocumentError(NOT_FOUND, "no such document patch")
        patch = _patch(found)
        if patch.details.get("raw_rejection"):
            raise DocumentError(
                POLICY_DENIED,
                "this retained raw edit has no safe patch; edit the document explicitly",
            )
        if patch.state not in ("conflicted", "refused", "suggested"):
            raise DocumentError(CONFLICT, "this patch is no longer awaiting application")
        return patch

    def preview_patch(self, project_id: str, reference: str, patch_id: str) -> Json:
        """Preview retained operations against the current revision, without writing."""
        with self._read() as db:
            row = self._find(db, project_id, reference)
            assert db is not None  # noqa: S101
            patch = self._review_patch(db, row, patch_id)
            submission = _Submission(
                patch_id,
                row.info.head,
                _operations(patch.operations),
                cast(list[Json], patch.operations),
                patch.reason,
                patch.author,
            )
            inputs = self._inputs(db, row, row.info.head)
        assessed = self._assess(row, inputs, submission)
        allowed = (
            assessed.error is None
            and not assessed.deleted
            and not assessed.unnamed
            and assessed.applied is not None
        )
        return {
            "patch": patch.model_dump(mode="json"),
            "base": row.info.head,
            "current": inputs.head.text,
            "proposed": assessed.applied.mapped.parsed.text
            if allowed and assessed.applied
            else None,
            "allowed": allowed,
            "message": None if assessed.error is None else assessed.error.message,
            "unnamed": assessed.unnamed,
        }

    def apply_patch(
        self,
        project_id: str,
        reference: str,
        patch_id: str,
        *,
        base: int,
        author: Author,
        reason: str,
        operation_id: str,
    ) -> ChangeResult:
        """Deliberately apply reviewed operations, guarded by the preview's exact head."""
        self._user(author)
        self._operation_id(operation_id)
        why = _reason(reason)
        request = {"apply_patch": reference, "patch": patch_id, "base": base, "reason": why}
        prepared = None
        try:
            with self._read() as reading:
                row = self._find(reading, project_id, reference)
                self._current(row, base)
                assert reading is not None  # noqa: S101
                patch = self._review_patch(reading, row, patch_id)
                candidate = _Submission(
                    patch_id,
                    base,
                    _operations(patch.operations),
                    cast(list[Json], patch.operations),
                    why,
                    patch.author,
                    author,
                )
            prepared = self._prepare_patch(project_id, reference, candidate)
        except DocumentError:
            # Replay and authoritative errors are checked in the transaction below.
            pass
        with self._write() as db:
            replay = self._replay(db, project_id, author, operation_id, request)
            if replay is not None:
                return replay
            row = self._find(db, project_id, reference)
            self._current(row, base)
            patch = self._review_patch(db, row, patch_id)
            submission = _Submission(
                patch_id,
                base,
                _operations(patch.operations),
                cast(list[Json], patch.operations),
                why,
                patch.author,
                author,
            )
            if prepared is None or prepared.key != _key(row):
                raise DocumentError(
                    BUSY,
                    "the patch could not be prepared; retry after reviewing the current revision",
                )
            assessed = self._assessed(db, row, submission, prepared)
            result = self._act(db, row, submission, assessed)
            self._remember(db, project_id, author, operation_id, request, result)
            return result

    def _prepare_save(
        self, project_id: str, reference: str, base: int, normalized: Normalized
    ) -> _Carried | None:
        """A saved text's IDs, from the document as it is now, outside the write lock."""
        if self.readonly:
            return None
        try:
            with self._read() as db:
                if db is None:
                    return None
                row = self._find(db, project_id, reference)
                if row.info.deleted or base != row.info.head:
                    return None
                revision = self._revision(db, row.info.document_id, base)
            head = self._mapped(revision)
            allocate = Allocator(row.next_block, row.next_span)
            mapped = carry(head, normalized.parsed, allocate)
        except (DocumentError, sqlite3.Error):
            return None
        return _Carried(_key(row), head, mapped, allocate)

    def _prepare_patch(
        self, project_id: str, reference: str, submission: _Submission, budget: Budget | None = None
    ) -> _Assessed | None:
        """A patch checked against the document as it is now, outside the write lock."""
        if self.readonly:
            return None
        try:
            with self._read() as db:
                if db is None:
                    return None
                row = self._find(db, project_id, reference)
                inputs = self._inputs(db, row, submission.base)
        except (DocumentError, sqlite3.Error):
            # The change finds the same error under the lock, in its turn.
            return None
        return self._assess(row, inputs, submission, budget)

    def _assessed(
        self,
        db: sqlite3.Connection,
        row: _Row,
        submission: _Submission,
        prepared: _Assessed | None,
    ) -> _Assessed:
        """The patch checked against the document as it is under the write lock: as it
        was checked before, if the document is unchanged since."""
        if prepared is not None and prepared.key == _key(row):
            return prepared
        return self._assess(row, self._inputs(db, row, submission.base), submission)

    def _inputs(self, db: sqlite3.Connection, row: _Row, base: int) -> _Inputs:
        info = row.info
        if not 1 <= base <= info.head:
            raise DocumentError(
                INVALID_REQUEST, f"document {info.name!r} has revisions 1 to {info.head}"
            )
        # The block maps and authors of the later revisions; their texts are not needed.
        since = [
            (int(number), str(author), str(stored))
            for number, author, stored in db.execute(
                "SELECT number, author, block_map FROM revisions WHERE document_id = ? "
                "AND number > ? ORDER BY number",
                (info.document_id, base),
            )
        ]
        return _Inputs(
            _key(row),
            self._revision(db, info.document_id, base),
            self._revision(db, info.document_id, info.head),
            since,
        )

    def _assess(
        self, row: _Row, inputs: _Inputs, submission: _Submission, budget: Budget | None = None
    ) -> _Assessed:
        """The patch checked against the inputs, without the database: refused, in
        conflict, or applied, with the revision it makes (LD-17, LD-18)."""
        assessed = _Assessed(inputs.key)
        operations = submission.operations
        try:
            base_mapped = self._mapped(inputs.base)
            check_names(base_mapped, operations)
            if row.info.deleted or inputs.head.info.tombstone:
                assessed.deleted = True
                return assessed
            head = self._mapped(inputs.head)
            if inputs.since:
                since = Since()
                for number, author, stored in inputs.since:
                    who = Author.model_validate_json(author).model_dump(
                        mode="json", exclude_none=True
                    )
                    since.note(number, who, json.loads(stored))
                assessed.conflicts = conflicts(base_mapped, head, operations, since)
                if assessed.conflicts:
                    return assessed
            allocate = Allocator(row.next_block, row.next_span)
            applied = apply_operations(head, operations, allocate, budget)
            named = [touches(operation, base_mapped) for operation in operations]
            assessed.unnamed = unnamed_changes(head, applied.mapped, named)
            if not assessed.unnamed:
                assessed.head, assessed.applied, assessed.allocate = head, applied, allocate
        except DocumentError as error:
            assessed.error = error
        return assessed

    def _act(
        self,
        db: sqlite3.Connection,
        row: _Row,
        submission: _Submission,
        assessed: _Assessed,
        *,
        suggest: bool = False,
    ) -> ChangeResult:
        """What a checked patch writes: the revision, or the patch kept as rejected,
        refused or suggested, with its event."""
        if assessed.error is not None:
            raise assessed.error
        info = row.info
        base = submission.base
        result = ChangeResult(
            outcome="conflict",
            document_id=info.document_id,
            revision=info.head,
            base=base,
            patch_id=submission.patch_id,
        )
        if assessed.deleted:
            message = f"document {info.name!r} was deleted in revision {info.head}"
            return self._reject(
                db,
                row,
                submission,
                "conflicted",
                {"deleted": True},
                result.model_copy(update={"message": message}),
            )
        if assessed.conflicts:
            found = assessed.conflicts
            message = (
                f"revision {info.head} changed what this patch touches since revision "
                f"{base}: nothing was written, and the patch is kept as rejected patch "
                f"{submission.patch_id}. Read revision {info.head} and patch it"
            )
            return self._reject(
                db,
                row,
                submission,
                "conflicted",
                {"conflicts": found},
                result.model_copy(update={"conflicts": found, "message": message}),
            )
        unnamed = assessed.unnamed
        if unnamed:
            message = (
                "patch_changes_unnamed_blocks: the new text would also change "
                f"{', '.join(unnamed)}, which the patch does not name: an unclosed fence, "
                "container or formula can take in the blocks after it, a closing marker can "
                "leave them outside their container, and a link reference definition changes "
                "the links that use it. Nothing was written"
            )
            return self._reject(
                db,
                row,
                submission,
                "refused",
                {"unnamed": unnamed},
                result.model_copy(
                    update={"outcome": "refused", "unnamed": unnamed, "message": message}
                ),
            )
        applied, head, allocate = assessed.applied, assessed.head, assessed.allocate
        if applied is None or head is None or allocate is None:
            raise DocumentError(INTERNAL, "the patch was not checked")
        if suggest:
            self._keep(db, info, submission, "suggested", {"current": info.head})
            self._event(
                db,
                "document.suggestion_created",
                info,
                submission.author,
                {"patch_id": submission.patch_id, "base": base, **submission.attribution},
            )
            return result.model_copy(
                update={
                    "outcome": "suggested",
                    "message": (
                        "the document takes agent patches as suggestions: patch "
                        f"{submission.patch_id} waits for the user to accept or reject it"
                    ),
                }
            )
        return self._commit(
            db,
            row,
            applied.mapped,
            head,
            allocate,
            author=submission.author,
            base=base,
            reason=submission.reason,
            operations=submission.raw,
            normalized=applied.normalized,
            outcome="accepted" if submission.accepted_by is not None else "applied",
            submission=submission,
        )

    def _keep(
        self,
        db: sqlite3.Connection,
        info: DocumentInfo,
        submission: _Submission,
        state: str,
        details: Json,
        revision: int | None = None,
    ) -> None:
        """Record a patch, or a suggestion's decision (LD-19, LD-20)."""
        now = _time(self._clock())
        if submission.accepted_by is not None:
            db.execute(
                "UPDATE patches SET state = ?, revision = ?, details = ?, decided_at = ?, "
                "decided_by = ? WHERE patch_id = ?",
                (
                    state,
                    revision,
                    _dumps(details),
                    now,
                    submission.accepted_by.model_dump_json(),
                    submission.patch_id,
                ),
            )
            return
        db.execute(
            "INSERT INTO patches (patch_id, document_id, created_at, author, session_id, base, "
            "reason, operations, state, revision, details) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                submission.patch_id,
                info.document_id,
                now,
                submission.author.model_dump_json(),
                submission.author.session_id,
                submission.base,
                submission.reason,
                _dumps(submission.raw),
                state,
                revision,
                _dumps(details),
            ),
        )

    def _reject(
        self,
        db: sqlite3.Connection,
        row: _Row,
        submission: _Submission,
        state: str,
        details: Json,
        result: ChangeResult,
    ) -> ChangeResult:
        """Keep a patch that wrote nothing, with its event (LD-19)."""
        self._keep(db, row.info, submission, state, {**details, "current": row.info.head})
        kind = "document.patch_conflicted" if state == "conflicted" else "document.patch_refused"
        payload = {
            "patch_id": submission.patch_id,
            "base": submission.base,
            "current": row.info.head,
            **submission.attribution,
            **details,
        }
        self._event(db, kind, row.info, submission.author, payload)
        return result

    def delete(
        self,
        project_id: str,
        reference: str,
        *,
        author: Author,
        reason: str,
        operation_id: str | None = None,
    ) -> ChangeResult:
        """A tombstone revision; the history and the patches remain (LD-12, RA-3).

        An agent may delete only the documents its own session created.
        """
        if author.kind not in ("user", "agent"):
            raise DocumentError(
                POLICY_DENIED, "only the user or an owning session agent may delete"
            )
        why = _reason(reason)
        request = {"delete": reference}
        with self._write() as db:
            replay = self._replay(db, project_id, author, operation_id, request)
            if replay is not None:
                return replay
            row = self._find(db, project_id, reference, author.session_id)
            self._live(row)
            if author.kind == "agent" and (
                author.session_id is None or row.info.created_session != author.session_id
            ):
                raise DocumentError(
                    POLICY_DENIED,
                    f"document {row.info.name!r} was not created by this session; only the "
                    "user can delete it",
                )
            head = self._mapped(self._revision(db, row.info.document_id, row.info.head))
            allocate = Allocator(row.next_block, row.next_span)
            result = self._commit(
                db,
                row,
                fresh(parse(""), allocate),
                head,
                allocate,
                author=author,
                base=row.info.head,
                reason=why,
                operations=[{"op": "delete_document"}],
                normalized={},
                outcome="deleted",
                tombstone=True,
            )
            self._remember(db, project_id, author, operation_id, request, result)
            return result

    def set_suggestions(
        self, project_id: str, reference: str, *, on: bool, author: Author
    ) -> DocumentInfo:
        """Whether agent patches to the document wait as suggestions (LD-20)."""
        if author.kind != "user":
            raise DocumentError(POLICY_DENIED, "only the user decides how a document takes patches")
        with self._write() as db:
            row = self._find(db, project_id, reference)
            self._live(row)
            db.execute(
                "UPDATE documents SET takes_suggestions = ? WHERE document_id = ?",
                (int(on), row.info.document_id),
            )
            self._event(
                db, "document.settings_changed", row.info, author, {"takes_suggestions": on}
            )
            return row.info.model_copy(update={"takes_suggestions": on})

    def decide(
        self, project_id: str, reference: str, patch_id: str, *, accept: bool, author: Author
    ) -> ChangeResult:
        """The user accepts a suggestion, which applies as a patch does (LD-17), or rejects it.

        An accepted suggestion's revision is attributed to its author, with the user's
        acceptance recorded.
        """
        if author.kind != "user":
            raise DocumentError(POLICY_DENIED, "only the user decides on suggestions")
        prepared = None
        if accept:
            prepared = self._prepare_decision(project_id, reference, patch_id, author)
        with self._write() as db:
            row = self._find(db, project_id, reference)
            suggestion = self._suggestion(db, row, patch_id)
            now = self._clock()
            if not accept:
                db.execute(
                    "UPDATE patches SET state = 'declined', decided_at = ?, decided_by = ? "
                    "WHERE patch_id = ?",
                    (_time(now), author.model_dump_json(), patch_id),
                )
                self._event(
                    db,
                    "document.suggestion_decided",
                    row.info,
                    author,
                    {"patch_id": patch_id, "decision": "rejected", "by": "user"},
                )
                return ChangeResult(
                    outcome="declined",
                    document_id=row.info.document_id,
                    revision=row.info.head,
                    patch_id=patch_id,
                    message=f"suggestion {patch_id} was rejected",
                )
            submission = _accepted(suggestion, author)
            assessed = self._assessed(db, row, submission, prepared)
            result = self._act(db, row, submission, assessed)
            self._event(
                db,
                "document.suggestion_decided",
                row.info,
                author,
                {
                    "patch_id": patch_id,
                    "decision": "accepted",
                    "by": "user",
                    "outcome": result.outcome,
                    "revision": result.revision,
                },
            )
            return result

    def _suggestion(self, db: sqlite3.Connection, row: _Row, patch_id: str) -> PatchInfo:
        found = db.execute(
            _PATCH + " WHERE patch_id = ? AND document_id = ?",
            (patch_id, row.info.document_id),
        ).fetchone()
        if found is None:
            raise DocumentError(NOT_FOUND, f"document {row.info.name!r} has no patch {patch_id}")
        suggestion = _patch(found)
        if suggestion.state != "suggested":
            raise DocumentError(
                CONFLICT, f"patch {patch_id} is {suggestion.state}, not a waiting suggestion"
            )
        return suggestion

    def _prepare_decision(
        self, project_id: str, reference: str, patch_id: str, author: Author
    ) -> _Assessed | None:
        """An accepted suggestion checked as a patch, outside the write lock."""
        if self.readonly:
            return None
        try:
            with self._read() as db:
                if db is None:
                    return None
                row = self._find(db, project_id, reference)
                submission = _accepted(self._suggestion(db, row, patch_id), author)
                inputs = self._inputs(db, row, submission.base)
        except (DocumentError, sqlite3.Error):
            return None
        return self._assess(row, inputs, submission)

    # Inside a change.

    def _live(self, row: _Row) -> None:
        if row.info.deleted:
            raise DocumentError(
                CONFLICT, f"document {row.info.name!r} was deleted in revision {row.info.head}"
            )

    def _commit(
        self,
        db: sqlite3.Connection,
        row: _Row,
        mapped: Mapped,
        head: Mapped | None,
        allocate: Allocator,
        *,
        author: Author,
        base: int | None,
        reason: str,
        operations: list[Json],
        normalized: Json,
        outcome: Outcome,
        submission: _Submission | None = None,
        tombstone: bool = False,
        force: bool = False,
    ) -> ChangeResult:
        """The next revision, unless the text is unchanged, with its patch and its event."""
        info = row.info
        patch_id = None if submission is None else submission.patch_id
        accepted_by = None if submission is None else submission.accepted_by
        if (
            head is not None
            and mapped.parsed.text == head.parsed.text
            and not tombstone
            and not force
        ):
            if submission is not None:
                self._keep(db, info, submission, "unchanged", {}, info.head)
            return ChangeResult(
                outcome="unchanged",
                document_id=info.document_id,
                revision=info.head,
                base=base,
                applied_to=info.head,
                patch_id=patch_id,
                message="the text is unchanged, so no revision was made",
            )
        changes: Changes = compare(head, mapped)
        number = info.head + 1
        stored = block_map(mapped, changes)
        new_blocks = [
            index
            for index, block_id in enumerate(mapped.block_ids)
            if changes.blocks[block_id] in ("added", "changed")
        ]
        found = warnings(mapped, new_blocks) + math_check(mapped, new_blocks)
        now = self._clock()
        text = mapped.parsed.text
        if normalized.get("converted"):
            normalized = {**normalized, "normalized": True}
        db.execute(
            "INSERT INTO revisions (document_id, number, created_at, author, accepted_by, "
            "session_id, base, parent, reason, operations, patch_id, text, sha256, tombstone, "
            "normalized, warnings, block_map, parser) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                info.document_id,
                number,
                _time(now),
                author.model_dump_json(),
                None if accepted_by is None else accepted_by.model_dump_json(),
                author.session_id,
                base,
                info.head or None,
                reason,
                _dumps(operations),
                patch_id,
                text,
                hashlib.sha256(text.encode()).hexdigest(),
                int(tombstone),
                _dumps(normalized),
                _dumps(found),
                _dumps(stored),
                _dumps(parser_versions()),
            ),
        )
        db.execute(
            "UPDATE documents SET head = ?, next_block = ?, next_span = ?, deleted = ? "
            "WHERE document_id = ?",
            (number, allocate.next_block, allocate.next_span, int(tombstone), info.document_id),
        )
        if submission is not None:
            state = "accepted" if accepted_by is not None else "applied"
            self._keep(db, info, submission, state, {}, number)
        changed = summary(changes, mapped)
        payload: Json = {
            "revision": number,
            "base": base,
            "parent": info.head or None,
            "author": author.model_dump(mode="json", exclude_none=True),
            "author_kind": author.kind,
            "changed": changed["changed"],
            "added": [entry["id"] for entry in changed["added"]],
            "removed": changed["removed"],
            "moved": changed["moved"],
            "structural_added": structural_added(mapped, changes),
            "agent_characters": written(head, mapped, changes) if author.delegated else 0,
            "normalized": bool(normalized.get("converted")),
            "tombstone": tombstone,
        }
        if patch_id is not None:
            payload["patch_id"] = patch_id
        if accepted_by is not None:
            payload["accepted_by"] = accepted_by.model_dump(mode="json", exclude_none=True)
        self._event(db, "document.revision_created", info, author, payload)
        self._cache_put(info.document_id, number, text, mapped)
        return ChangeResult(
            outcome=outcome,
            document_id=info.document_id,
            revision=number,
            base=base,
            applied_to=info.head or None,
            rebased=base is not None and info.head > 0 and base != info.head,
            patch_id=patch_id,
            changes=changed,
            normalized=normalized,
            warnings=found,
        )

    def _cache_put(self, document_id: str, number: int, text: str, mapped: Mapped) -> None:
        key = (document_id, number, hashlib.sha256(text.encode()).hexdigest())
        with self._lock:
            self._cache[key] = mapped
            while len(self._cache) > CACHED:
                self._cache.popitem(last=False)

    def _event(
        self,
        db: sqlite3.Connection,
        kind: str,
        info: DocumentInfo,
        author: Author,
        payload: Json,
    ) -> None:
        db.execute(
            "INSERT INTO events (time, kind, producer, project_id, session_id, document_id, "
            "payload) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                _time(self._clock()),
                kind,
                self.producer,
                info.project_id,
                author.session_id,
                info.document_id,
                _dumps(payload),
            ),
        )

    def _replay(
        self,
        db: sqlite3.Connection,
        project_id: str,
        author: Author,
        operation_id: str | None,
        request: Json,
    ) -> ChangeResult | None:
        """The first result of a request made again under its operation ID."""
        if operation_id is None:
            return None
        found = db.execute(
            "SELECT fingerprint, result FROM requests WHERE project_id = ? AND actor = ? "
            "AND operation_id = ?",
            (project_id, author.actor, operation_id),
        ).fetchone()
        if found is None:
            return None
        if found[0] != _fingerprint(request):
            raise DocumentError(
                CONFLICT,
                f"operation ID {operation_id!r} was used for another change; choose a new one",
            )
        return ChangeResult.model_validate_json(found[1]).model_copy(update={"replayed": True})

    def _remember(
        self,
        db: sqlite3.Connection,
        project_id: str,
        author: Author,
        operation_id: str | None,
        request: Json,
        result: ChangeResult,
    ) -> None:
        if operation_id is None:
            return
        db.execute(
            "INSERT INTO requests (project_id, actor, operation_id, fingerprint, result, "
            "created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (
                project_id,
                author.actor,
                operation_id,
                _fingerprint(request),
                result.model_dump_json(),
                _time(self._clock()),
            ),
        )


def _accepted(suggestion: PatchInfo, user: Author) -> _Submission:
    """A suggestion, as the patch the user accepts."""
    raw = cast(list[Json], suggestion.operations)
    return _Submission(
        suggestion.patch_id,
        suggestion.base,
        _operations(raw),
        raw,
        suggestion.reason,
        suggestion.author,
        accepted_by=user,
    )


def _fingerprint(request: Json) -> str:
    return hashlib.sha256(_dumps(request).encode()).hexdigest()


def store_path(state_dir: Path) -> Path:
    """The control store's database in a harness state directory."""
    return state_dir / FILE_NAME
