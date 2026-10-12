# SPDX-License-Identifier: MIT
"""A durable control-owned catalog prerequisite; sandbox publication is disabled.

All methods are synchronous, serialized transactions. HTTP callers run them off the
event loop. Only a verified complete target replaces current catalog membership.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import sqlite3
import threading
import time
from collections.abc import Callable, Generator
from pathlib import Path
from typing import Annotated, Literal, cast

from pydantic import Field, ValidationError

from sandbox_science.contracts import Contract, ErrorKind
from sandbox_science.contracts.documents import (
    MAX_CATALOG_BYTES,
    MAX_CATALOG_ENTRIES,
    MAX_CONTENT_CHUNK,
    MAX_ENTRY_BYTES,
    CatalogContentChunk,
    CatalogDelta,
    CatalogSnapshot,
    CatalogSnapshotPage,
    CatalogStageStatus,
    CatalogStatus,
    PublicationEntry,
    manifest_sha256,
)

from .errors import WorkerError


class CatalogLimits(Contract):
    """Logical retained-data budgets, not filesystem/RSS quotas. May be lowered."""

    max_entries: Annotated[int, Field(strict=True, ge=1, le=MAX_CATALOG_ENTRIES)] = 10_000
    max_project_bytes: Annotated[int, Field(strict=True, ge=0, le=MAX_CATALOG_BYTES)] = 256 << 20
    max_total_entries: Annotated[int, Field(strict=True, ge=1, le=1_000_000)] = 100_000
    max_total_bytes: Annotated[int, Field(strict=True, ge=0, le=64 << 30)] = 4 << 30
    max_staged_entries: Annotated[int, Field(strict=True, ge=1, le=1_000_000)] = 20_000
    max_staged_bytes: Annotated[int, Field(strict=True, ge=0, le=8 << 30)] = 512 << 20
    max_projects: Annotated[int, Field(strict=True, ge=1, le=10_000)] = 1024
    max_retained_identities: Annotated[int, Field(strict=True, ge=1, le=1_000_000)] = 100_000
    """Revision high-water identities survive removal; quota refusal never forgets them."""
    staging_seconds: Annotated[float, Field(gt=0, le=86_400, allow_inf_nan=False)] = 900.0
    receipts_per_project: Annotated[int, Field(strict=True, ge=1, le=128)] = 32
    max_pending_requests: Annotated[int, Field(strict=True, ge=1, le=64)] = 8


_SCHEMA = """
CREATE TABLE IF NOT EXISTS catalog_projects (
 project TEXT PRIMARY KEY, target TEXT NOT NULL, input_digest TEXT NOT NULL,
 seq INTEGER NOT NULL, entry_count INTEGER NOT NULL, content_bytes INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS catalog_entries (
 project TEXT NOT NULL, document TEXT NOT NULL, path TEXT NOT NULL, record TEXT NOT NULL,
 sha TEXT NOT NULL, size INTEGER NOT NULL,
 PRIMARY KEY(project,document), UNIQUE(project,path)
);
CREATE TABLE IF NOT EXISTS catalog_blobs (
 project TEXT NOT NULL, sha TEXT NOT NULL, content BLOB NOT NULL,
 PRIMARY KEY(project,sha)
);
CREATE TABLE IF NOT EXISTS catalog_stages (
 project TEXT PRIMARY KEY, generation TEXT NOT NULL, mode TEXT NOT NULL,
 target TEXT NOT NULL, input_digest TEXT NOT NULL, expires REAL NOT NULL,
 entry_count INTEGER NOT NULL, content_bytes INTEGER NOT NULL, next_index INTEGER NOT NULL,
 received_bytes INTEGER NOT NULL DEFAULT 0, base_seq INTEGER
);
CREATE TABLE IF NOT EXISTS catalog_stage_entries (
 project TEXT NOT NULL REFERENCES catalog_stages(project) ON DELETE CASCADE,
 position INTEGER NOT NULL, document TEXT NOT NULL, path TEXT NOT NULL,
 record TEXT NOT NULL, sha TEXT NOT NULL, size INTEGER NOT NULL,
 PRIMARY KEY(project,position), UNIQUE(project,document), UNIQUE(project,path)
);
CREATE TABLE IF NOT EXISTS catalog_chunks (
 project TEXT NOT NULL REFERENCES catalog_stages(project) ON DELETE CASCADE,
 sha TEXT NOT NULL, offset INTEGER NOT NULL, content BLOB NOT NULL,
 PRIMARY KEY(project,sha,offset)
);
CREATE TABLE IF NOT EXISTS catalog_stage_revisions (
 project TEXT NOT NULL REFERENCES catalog_stages(project) ON DELETE CASCADE,
 document TEXT NOT NULL, record TEXT NOT NULL, PRIMARY KEY(project,document)
);
CREATE TABLE IF NOT EXISTS catalog_document_revisions (
 project TEXT NOT NULL, document TEXT NOT NULL, record TEXT NOT NULL,
 present INTEGER NOT NULL, PRIMARY KEY(project,document)
);
CREATE TABLE IF NOT EXISTS catalog_receipts (
 project TEXT NOT NULL, generation TEXT NOT NULL, input_digest TEXT NOT NULL,
 target TEXT NOT NULL, seq INTEGER NOT NULL, PRIMARY KEY(project,generation)
);
CREATE TABLE IF NOT EXISTS document_written_revisions (
 project TEXT NOT NULL, environment TEXT NOT NULL, incarnation TEXT NOT NULL,
 path TEXT NOT NULL, revision INTEGER NOT NULL, sha TEXT NOT NULL, written_at REAL NOT NULL,
 PRIMARY KEY(project,environment,incarnation,path,revision)
);
"""


def _fail(message: str, kind: ErrorKind = ErrorKind.CONFLICT) -> WorkerError:
    return WorkerError(kind, message)


def _digest(record: Contract) -> str:
    return hashlib.sha256(record.model_dump_json().encode()).hexdigest()


def _entry(record: str) -> PublicationEntry:
    if len(record.encode()) > MAX_ENTRY_BYTES:
        raise _fail("unreadable catalog metadata", ErrorKind.INTERNAL)
    try:
        return PublicationEntry.model_validate_json(record)
    except ValidationError as exc:
        raise _fail("unreadable catalog metadata", ErrorKind.INTERNAL) from exc


def _snapshot(record: str, project: str) -> CatalogSnapshot:
    if len(record.encode()) > MAX_ENTRY_BYTES:
        raise _fail("unreadable catalog target", ErrorKind.INTERNAL)
    try:
        target = CatalogSnapshot.model_validate_json(record)
    except ValidationError as exc:
        raise _fail("unreadable catalog target", ErrorKind.INTERNAL) from exc
    if target.project_id != project:
        raise _fail("catalog target belongs to another project", ErrorKind.INTERNAL)
    return target


class DocumentsCatalog:
    """One SQLite catalog, with bounded current data, staging and replay receipts.

    The written-revision table is reserved for future publication. Nothing here
    writes an environment, infers writer quiescence or advertises a mirror feature.
    """

    def __init__(
        self,
        path: Path,
        limits: CatalogLimits | None = None,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.limits = limits or CatalogLimits()
        self.clock = clock
        self._lock = threading.RLock()
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
        self._db.execute("PRAGMA foreign_keys=ON")
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=FULL")
        self._db.execute("PRAGMA journal_size_limit=16777216")
        self._db.executescript(_SCHEMA)

    def close(self) -> None:
        """Close after request handlers have drained."""
        with self._lock:
            self._db.close()

    @contextlib.contextmanager
    def _transaction(self) -> Generator[None, None, None]:
        with self._lock:
            # Expiry is independent housekeeping: a later rejected transaction must
            # not restore expired reservations and indefinitely consume capacity.
            self._db.execute("DELETE FROM catalog_stages WHERE expires<=?", (self.clock(),))
            self._db.execute("BEGIN IMMEDIATE")
            try:
                yield
                self._db.execute("COMMIT")
            except BaseException:
                self._db.execute("ROLLBACK")
                raise

    def _current(self, project: str) -> CatalogSnapshot | None:
        row = self._db.execute(
            "SELECT substr(target,1,2049) FROM catalog_projects WHERE project=?", (project,)
        ).fetchone()
        return _snapshot(row[0], project) if row else None

    def _status(self, target: CatalogSnapshot | None, project: str) -> CatalogStatus:
        staged = self._db.execute(
            "SELECT substr(generation,1,129),substr(mode,1,16),substr(target,1,2049),"
            "next_index,received_bytes,expires"
            " FROM catalog_stages WHERE project=?",
            (project,),
        ).fetchone()
        stage = None
        if staged:
            reserved = _snapshot(staged[2], project)
            stage = CatalogStageStatus(
                generation_id=staged[0],
                mode=staged[1],
                catalog_seq=reserved.catalog_seq,
                received_entries=staged[3],
                entry_count=reserved.entry_count,
                received_content_bytes=staged[4],
                reserved_content_bytes=reserved.total_content_bytes,
                expires_at=staged[5],
            )
        return CatalogStatus(
            project_id=project,
            staging=stage,
            **(target.model_dump(exclude={"protocol", "project_id"}) if target else {}),
        )

    def status(self, project: str) -> CatalogStatus:
        """Only a committed generation appears as current; staging is explicit."""
        with self._transaction():
            return self._status(self._current(project), project)

    def _replay(self, target: CatalogSnapshot, digest: str) -> CatalogStatus | None:
        row = self._db.execute(
            "SELECT substr(input_digest,1,65),substr(target,1,2049) FROM catalog_receipts"
            " WHERE project=? AND generation=?",
            (target.project_id, target.generation_id),
        ).fetchone()
        if not row:
            return None
        if row[0] != digest:
            raise _fail("generation ID was used with a different payload")
        previous = _snapshot(row[1], target.project_id)
        if previous != target:
            raise _fail("unreadable generation receipt binding", ErrorKind.INTERNAL)
        return self._status(self._current(target.project_id), target.project_id).model_copy(
            update={"replayed": True, "acknowledged": previous}
        )

    def _staging_limits(self) -> None:
        counts = self._db.execute(
            "SELECT coalesce(sum(entry_count),0),coalesce(sum(content_bytes),0),"
            "coalesce(max(entry_count),0),coalesce(max(content_bytes),0) FROM catalog_stages"
        ).fetchone()
        limits = self.limits
        if (
            counts[0] > limits.max_staged_entries
            or counts[1] > limits.max_staged_bytes
            or counts[2] > limits.max_entries
            or counts[3] > limits.max_project_bytes
        ):
            raise _fail("existing staging exceeds configured quota", ErrorKind.UNAVAILABLE)
        projects = self._db.execute(
            "SELECT count(*) FROM (SELECT project FROM catalog_projects UNION"
            " SELECT project FROM catalog_stages)"
        ).fetchone()[0]
        if projects > limits.max_projects:
            raise _fail("existing projects exceed configured quota", ErrorKind.UNAVAILABLE)

    def _reserve(
        self, target: CatalogSnapshot, mode: Literal["snapshot", "delta"], digest: str
    ) -> CatalogStatus | None:
        replay = self._replay(target, digest)
        if replay is not None:
            return replay
        current = self._current(target.project_id)
        if current is not None and target.catalog_seq <= current.catalog_seq:
            raise _fail("catalog cursor is stale; old receipts may have expired")
        staged = self._db.execute(
            "SELECT substr(generation,1,129),substr(input_digest,1,65)"
            " FROM catalog_stages WHERE project=?",
            (target.project_id,),
        ).fetchone()
        if staged:
            if staged != (target.generation_id, digest):
                raise _fail("another generation or payload is already staged")
            self._staging_limits()
            return self._status(current, target.project_id).model_copy(update={"replayed": True})
        limits = self.limits
        if (
            target.entry_count > limits.max_entries
            or target.total_content_bytes > limits.max_project_bytes
        ):
            raise _fail("project catalog quota exceeded", ErrorKind.UNAVAILABLE)
        counts = self._db.execute(
            "SELECT coalesce(sum(entry_count),0),coalesce(sum(content_bytes),0) FROM catalog_stages"
        ).fetchone()
        if (
            counts[0] + target.entry_count > limits.max_staged_entries
            or counts[1] + target.total_content_bytes > limits.max_staged_bytes
        ):
            raise _fail("worker staging quota exceeded", ErrorKind.UNAVAILABLE)
        projects = self._db.execute(
            "SELECT count(*) FROM (SELECT project FROM catalog_projects UNION"
            " SELECT project FROM catalog_stages UNION SELECT ?)",
            (target.project_id,),
        ).fetchone()[0]
        if projects > limits.max_projects:
            raise _fail("worker project catalog quota exceeded", ErrorKind.UNAVAILABLE)
        self._db.execute(
            "INSERT INTO catalog_stages VALUES (?,?,?,?,?,?,?,?,0,0,?)",
            (
                target.project_id,
                target.generation_id,
                mode,
                target.model_dump_json(),
                digest,
                self.clock() + limits.staging_seconds,
                target.entry_count,
                target.total_content_bytes,
                current.catalog_seq if current else None,
            ),
        )
        return None

    def begin(self, target: CatalogSnapshot) -> CatalogStatus:
        """Reserve a snapshot without changing the completed catalog."""
        with self._transaction():
            replay = self._reserve(target, "snapshot", _digest(target))
            return replay or self._status(self._current(target.project_id), target.project_id)

    def _stage(self, project: str, generation: str) -> tuple[CatalogSnapshot, str, int, int | None]:
        self._staging_limits()
        row = self._db.execute(
            "SELECT substr(target,1,2049),substr(mode,1,16),next_index,base_seq FROM catalog_stages"
            " WHERE project=? AND generation=?",
            (project, generation),
        ).fetchone()
        if row is None:
            raise _fail(
                "no pending generation; it may have completed or expired", ErrorKind.NOT_FOUND
            )
        target = _snapshot(row[0], project)
        if target.generation_id != generation:
            raise _fail("unreadable staged generation binding", ErrorKind.INTERNAL)
        return target, row[1], row[2], row[3]

    def _insert(self, project: str, position: int, entry: PublicationEntry) -> None:
        try:
            self._db.execute(
                "INSERT INTO catalog_stage_entries VALUES (?,?,?,?,?,?,?)",
                (
                    project,
                    position,
                    entry.document_id,
                    entry.path,
                    entry.model_dump_json(),
                    entry.content_sha256,
                    entry.content_bytes,
                ),
            )
            self._db.execute(
                "INSERT OR REPLACE INTO catalog_stage_revisions VALUES (?,?,?)",
                (project, entry.document_id, entry.model_dump_json()),
            )
        except sqlite3.IntegrityError as exc:
            raise _fail("duplicate document identity or managed path") from exc

    def page(self, page: CatalogSnapshotPage) -> CatalogStatus:
        """Accept contiguous ordered metadata; identical already-staged pages replay."""
        with self._transaction():
            target, mode, received, _ = self._stage(page.project_id, page.generation_id)
            if mode != "snapshot" or page.start_index > received:
                raise _fail("snapshot page is out of order or targets a delta")
            end = page.start_index + len(page.entries)
            if end > target.entry_count:
                raise _fail("snapshot page exceeds the declared entry count")
            previous = ""
            if page.start_index:
                row = self._db.execute(
                    "SELECT substr(record,1,2049) FROM catalog_stage_entries"
                    " WHERE project=? AND position=?",
                    (page.project_id, page.start_index - 1),
                ).fetchone()
                if row is None:
                    raise _fail("unreadable previous metadata page", ErrorKind.INTERNAL)
                previous = _entry(row[0]).document_id
            for position, entry in enumerate(page.entries, page.start_index):
                if entry.document_id <= previous:
                    raise _fail("snapshot document identities must be strictly ordered")
                previous = entry.document_id
                if position < received:
                    old = self._db.execute(
                        "SELECT substr(record,1,2049) FROM catalog_stage_entries"
                        " WHERE project=? AND position=?",
                        (page.project_id, position),
                    ).fetchone()
                    if old is None or old[0] != entry.model_dump_json():
                        raise _fail("snapshot page replay has different content")
                else:
                    self._insert(page.project_id, position, entry)
            total = self._db.execute(
                "SELECT coalesce(sum(size),0) FROM catalog_stage_entries WHERE project=?",
                (page.project_id,),
            ).fetchone()[0]
            if total > target.total_content_bytes:
                raise _fail("snapshot metadata exceeds declared content bytes")
            self._db.execute(
                "UPDATE catalog_stages SET next_index=? WHERE project=?",
                (max(received, end), page.project_id),
            )
            return self._status(self._current(page.project_id), page.project_id).model_copy(
                update={"replayed": end <= received}
            )

    def delta(self, delta: CatalogDelta) -> CatalogStatus:
        """Stage a bounded delta against the exact completed cursor, without publishing."""
        target = delta.target
        with self._transaction():
            replay = self._reserve(target, "delta", _digest(delta))
            if replay is not None:
                return replay
            current = self._current(target.project_id)
            if current is None or current.catalog_seq != delta.after_seq:
                raise _fail("delta needs the exact completed base cursor")
            rows = self._db.execute(
                "SELECT substr(record,1,2049) FROM catalog_entries"
                " WHERE project=? ORDER BY document LIMIT ?",
                (target.project_id, MAX_CATALOG_ENTRIES + 1),
            ).fetchall()
            entries = {entry.document_id: entry for entry in (_entry(row[0]) for row in rows)}
            latest: dict[str, PublicationEntry] = {}
            for change in delta.changes:
                entry = change.entry
                previous = latest.get(entry.document_id)
                if previous is None:
                    self._validate_revision(target.project_id, entry)
                    previous = self._historical(target.project_id, entry.document_id)
                self._revision(previous, entry)
                latest[entry.document_id] = entry
                if entry.deleted:
                    if entry.document_id not in entries:
                        raise _fail("delta tombstone has no current document")
                    del entries[entry.document_id]
                else:
                    entries[entry.document_id] = entry
            ordered = sorted(entries.values(), key=lambda entry: entry.document_id)
            try:
                digest = manifest_sha256(target.project_id, target.catalog_seq, ordered)
            except ValueError as exc:
                raise _fail("invalid complete delta manifest") from exc
            if (
                len(ordered) != target.entry_count
                or sum(entry.content_bytes for entry in ordered) != target.total_content_bytes
                or digest != target.manifest_sha256
            ):
                raise _fail("delta does not produce the declared complete target")
            for position, entry in enumerate(ordered):
                self._insert(target.project_id, position, entry)
            for entry in latest.values():
                self._db.execute(
                    "INSERT OR REPLACE INTO catalog_stage_revisions VALUES (?,?,?)",
                    (target.project_id, entry.document_id, entry.model_dump_json()),
                )
            self._db.execute(
                "UPDATE catalog_stages SET next_index=? WHERE project=?",
                (len(ordered), target.project_id),
            )
            return self._status(current, target.project_id)

    def content(self, chunk: CatalogContentChunk) -> CatalogStatus:
        """Receive fixed-size contiguous chunks owned by this staged project target."""
        data = base64.b64decode(chunk.content_b64, validate=True)
        with self._transaction():
            self._stage(chunk.project_id, chunk.generation_id)
            sizes = self._db.execute(
                "SELECT DISTINCT size FROM catalog_stage_entries WHERE project=? AND sha=? LIMIT 2",
                (chunk.project_id, chunk.content_sha256),
            ).fetchall()
            if len(sizes) != 1:
                raise _fail("content digest is absent or has inconsistent declared sizes")
            size = sizes[0][0]
            if (
                chunk.offset % MAX_CONTENT_CHUNK
                or chunk.offset >= size
                or len(data) != min(MAX_CONTENT_CHUNK, size - chunk.offset)
            ):
                raise _fail("content must use aligned complete chunks through its declared size")
            old = self._db.execute(
                "SELECT substr(content,1,262145) FROM catalog_chunks"
                " WHERE project=? AND sha=? AND offset=?",
                (chunk.project_id, chunk.content_sha256, chunk.offset),
            ).fetchone()
            if old:
                if old[0] != data:
                    raise _fail("content chunk replay has different bytes")
                return self._status(self._current(chunk.project_id), chunk.project_id).model_copy(
                    update={"replayed": True}
                )
            received = self._db.execute(
                "SELECT coalesce(sum(length(content)),0) FROM catalog_chunks"
                " WHERE project=? AND sha=?",
                (chunk.project_id, chunk.content_sha256),
            ).fetchone()[0]
            if received != chunk.offset:
                raise _fail("content chunks are not contiguous")
            self._db.execute(
                "INSERT INTO catalog_chunks VALUES (?,?,?,?)",
                (chunk.project_id, chunk.content_sha256, chunk.offset, data),
            )
            self._db.execute(
                "UPDATE catalog_stages SET received_bytes=received_bytes+? WHERE project=?",
                (len(data), chunk.project_id),
            )
            return self._status(self._current(chunk.project_id), chunk.project_id)

    def _content(self, project: str, sha: str, size: int) -> bytes:
        old = self._db.execute(
            "SELECT content FROM catalog_blobs WHERE project=? AND sha=? AND length(content)<=?",
            (project, sha, 1024 * 1024),
        ).fetchone()
        if old:
            data = cast(bytes, old[0])
        else:
            rows = self._db.execute(
                "SELECT offset,substr(content,1,?) FROM catalog_chunks WHERE project=? AND sha=?"
                " ORDER BY offset LIMIT 5",
                (MAX_CONTENT_CHUNK + 1, project, sha),
            ).fetchall()
            if any(
                offset != index * MAX_CONTENT_CHUNK or len(part) > MAX_CONTENT_CHUNK
                for index, (offset, part) in enumerate(rows)
            ):
                raise _fail("incomplete or unreadable content chunks")
            data = b"".join(part for _, part in rows)
        if len(data) != size or hashlib.sha256(data).hexdigest() != sha:
            raise _fail("content is incomplete or its digest mismatches")
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise _fail("catalog documents must be UTF-8 text") from exc
        if "\x00" in text:
            raise _fail("catalog documents cannot contain NUL characters")
        return data

    @staticmethod
    def _revision(previous: PublicationEntry | None, entry: PublicationEntry) -> None:
        if previous is not None and entry.revision < previous.revision:
            raise _fail("document revision cannot move backwards")
        if (
            previous is not None
            and entry.revision == previous.revision
            and (
                entry.content_sha256 != previous.content_sha256
                or entry.content_bytes != previous.content_bytes
                or entry.deleted != previous.deleted
            )
        ):
            raise _fail("an immutable revision cannot change its bytes or deletion state")

    def _historical(self, project: str, document: str) -> PublicationEntry | None:
        row = self._db.execute(
            "SELECT substr(record,1,?) FROM catalog_document_revisions"
            " WHERE project=? AND document=?",
            (MAX_ENTRY_BYTES + 1, project, document),
        ).fetchone()
        if row is None:
            return None
        entry = _entry(row[0])
        if entry.document_id != document:
            raise _fail("unreadable revision identity binding", ErrorKind.INTERNAL)
        return entry

    def _validate_revision(self, project: str, entry: PublicationEntry) -> None:
        previous = self._historical(project, entry.document_id)
        self._revision(previous, entry)
        if previous is not None and not entry.deleted and entry.revision <= previous.revision:
            present = self._db.execute(
                "SELECT present FROM catalog_document_revisions WHERE project=? AND document=?",
                (project, entry.document_id),
            ).fetchone()[0]
            if not present:
                raise _fail("a removed document needs a newer live revision")

    def commit(self, target: CatalogSnapshot) -> CatalogStatus:
        """Verify all target metadata/content, then atomically publish the catalog only."""
        with self._transaction():
            receipt = self._db.execute(
                "SELECT substr(target,1,2049) FROM catalog_receipts"
                " WHERE project=? AND generation=?",
                (target.project_id, target.generation_id),
            ).fetchone()
            if receipt:
                if receipt[0] != target.model_dump_json():
                    raise _fail("commit replay has different target metadata")
                return self._status(self._current(target.project_id), target.project_id).model_copy(
                    update={"replayed": True, "acknowledged": target}
                )
            staged, _, received, base = self._stage(target.project_id, target.generation_id)
            if staged != target or received != target.entry_count:
                raise _fail("commit needs all metadata and the exact declared target")
            if (
                target.entry_count > self.limits.max_entries
                or target.total_content_bytes > self.limits.max_project_bytes
            ):
                raise _fail("project catalog quota exceeded", ErrorKind.UNAVAILABLE)
            current = self._current(target.project_id)
            if (current.catalog_seq if current else None) != base:
                raise _fail("the catalog base changed while staging")
            rows = self._db.execute(
                "SELECT substr(record,1,2049) FROM catalog_stage_entries"
                " WHERE project=? ORDER BY position LIMIT ?",
                (target.project_id, MAX_CATALOG_ENTRIES + 1),
            ).fetchall()
            entries = [_entry(row[0]) for row in rows]
            try:
                digest = manifest_sha256(target.project_id, target.catalog_seq, entries)
            except ValueError as exc:
                raise _fail("invalid complete manifest") from exc
            if (
                len(entries) != target.entry_count
                or sum(entry.content_bytes for entry in entries) != target.total_content_bytes
                or digest != target.manifest_sha256
            ):
                raise _fail("complete manifest digest/count/bytes mismatch")
            totals = self._db.execute(
                "SELECT coalesce(sum(entry_count),0),coalesce(sum(content_bytes),0)"
                " FROM catalog_projects WHERE project<>?",
                (target.project_id,),
            ).fetchone()
            if (
                totals[0] + target.entry_count > self.limits.max_total_entries
                or totals[1] + target.total_content_bytes > self.limits.max_total_bytes
            ):
                raise _fail("worker current catalog quota exceeded", ErrorKind.UNAVAILABLE)
            seen: dict[str, int] = {}
            retained = self._db.execute(
                "SELECT count(*) FROM (SELECT project,document FROM catalog_document_revisions"
                " UNION SELECT project,document FROM catalog_stage_revisions WHERE project=?)",
                (target.project_id,),
            ).fetchone()[0]
            if retained > self.limits.max_retained_identities:
                raise _fail("retained document identity quota exceeded", ErrorKind.UNAVAILABLE)
            revisions = self._db.execute(
                "SELECT substr(record,1,?) FROM catalog_stage_revisions WHERE project=? LIMIT ?",
                (MAX_ENTRY_BYTES + 1, target.project_id, MAX_CATALOG_ENTRIES + 65),
            ).fetchall()
            if len(revisions) > MAX_CATALOG_ENTRIES + 64:
                raise _fail("unreadable staged revision history", ErrorKind.INTERNAL)
            for row in revisions:
                entry = _entry(row[0])
                self._validate_revision(target.project_id, entry)
            for entry in entries:
                sha = cast(str, entry.content_sha256)
                if sha in seen:
                    if seen[sha] != entry.content_bytes:
                        raise _fail("content digest has inconsistent declared sizes")
                    continue
                seen[sha] = entry.content_bytes
                data = self._content(target.project_id, sha, entry.content_bytes)
                self._db.execute(
                    "INSERT OR REPLACE INTO catalog_blobs VALUES (?,?,?)",
                    (target.project_id, sha, data),
                )
            self._publish(target)
            return self._status(target, target.project_id).model_copy(
                update={"acknowledged": target}
            )

    def _publish(self, target: CatalogSnapshot) -> None:
        """Only called inside commit's transaction; no externally visible partial target."""
        project = target.project_id
        digest = self._db.execute(
            "SELECT substr(input_digest,1,65) FROM catalog_stages WHERE project=?", (project,)
        ).fetchone()[0]
        self._db.execute("DELETE FROM catalog_entries WHERE project=?", (project,))
        self._db.execute(
            "INSERT INTO catalog_entries SELECT project,document,path,record,sha,size"
            " FROM catalog_stage_entries WHERE project=?",
            (project,),
        )
        self._db.execute(
            "INSERT OR REPLACE INTO catalog_projects VALUES (?,?,?,?,?,?)",
            (
                project,
                target.model_dump_json(),
                digest,
                target.catalog_seq,
                target.entry_count,
                target.total_content_bytes,
            ),
        )
        self._db.execute(
            "INSERT INTO catalog_receipts VALUES (?,?,?,?,?)",
            (project, target.generation_id, digest, target.model_dump_json(), target.catalog_seq),
        )
        self._db.execute(
            "UPDATE catalog_document_revisions SET present=0 WHERE project=?", (project,)
        )
        self._db.execute(
            "INSERT OR REPLACE INTO catalog_document_revisions"
            " SELECT s.project,s.document,s.record,exists(SELECT 1 FROM catalog_entries e"
            " WHERE e.project=s.project AND e.document=s.document)"
            " FROM catalog_stage_revisions s WHERE project=?",
            (project,),
        )
        self._db.execute(
            "DELETE FROM catalog_receipts WHERE project=? AND generation NOT IN"
            " (SELECT generation FROM catalog_receipts WHERE project=? ORDER BY seq DESC LIMIT ?)",
            (project, project, self.limits.receipts_per_project),
        )
        self._db.execute("DELETE FROM catalog_stages WHERE project=?", (project,))
        self._db.execute(
            "DELETE FROM catalog_blobs WHERE project=? AND sha NOT IN"
            " (SELECT sha FROM catalog_entries WHERE project=?)",
            (project, project),
        )
