# SPDX-License-Identifier: MIT
"""Bounded immutable artifact inventory in the canonical control transaction.

Revisions come from explicit publication and from the files executions report
(research artifacts, RA-27 to RA-39): with their bytes, with only their hash and size
when the bytes are too large or the budget is full, or as tombstones. Source paths and
worker last-writer hints never prove a producing execution or observed input. Rich
bytes are stored and hashed, never parsed. Unresolved publication intents are retained
rather than replayed.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final, Literal

from pydantic import Field, TypeAdapter

from sandbox_science.contracts import ArtifactRef, Identifier, new_id
from sandbox_science.contracts.artifact_inventory import (
    MAX_ARTIFACT_BYTES,
    ArtifactEvent,
    ArtifactEventPage,
    ArtifactLink,
    ArtifactPage,
    ArtifactPublicationSource,
    ArtifactReceipt,
    ArtifactRevision,
    ArtifactRevisionRef,
    ArtifactSnapshot,
    artifact_path,
)
from sandbox_science.contracts.model import MAX_COUNTER, Contract

from .documents.errors import CONFLICT, INTERNAL, INVALID_REQUEST, NOT_FOUND, DocumentError
from .documents.store import DocumentStore

_IDENTIFIER = TypeAdapter[str](Identifier)
_MAX_RECORD: Final = 16 * 1024
_MAX_REQUEST: Final = 8 * 1024
_EMPTY: Final = hashlib.sha256(b"").hexdigest()
_GiB: Final = 1024**3


def _result(objects: str) -> str:
    """SQL for an artifact with a published or declared revision: a result (RA-37). The
    scan is bounded by the revision cap (BACKLOG: a stored flag if it is raised)."""
    return (
        "EXISTS(SELECT 1 FROM artifact_revisions k "  # noqa: S608 - a fixed alias
        f"WHERE k.artifact={objects}.id AND json_extract(k.record,'$.publication.kind') "
        "IN ('agent_publication','declared_result'))"
    )


Room = Literal["content", "metadata", "full"]
"""What the budget leaves for a new revision: its bytes, only its hash and size, or nothing."""


class ArtifactLimits(Contract):
    """Logical retention quotas; full retention refuses new work rather than evicting.
    Sized for every file executions write (research artifacts, decision 9)."""

    artifact_bytes: int = Field(
        default=MAX_ARTIFACT_BYTES, strict=True, ge=1, le=MAX_ARTIFACT_BYTES
    )
    project_bytes: int = Field(default=2 * _GiB, strict=True, ge=1, le=64 * _GiB)
    total_bytes: int = Field(default=8 * _GiB, strict=True, ge=1, le=256 * _GiB)
    project_revisions: int = Field(default=50_000, strict=True, ge=1, le=10_000_000)
    total_revisions: int = Field(default=200_000, strict=True, ge=1, le=10_000_000)
    project_operations: int = Field(default=100_000, strict=True, ge=1, le=10_000_000)
    total_operations: int = Field(default=400_000, strict=True, ge=1, le=10_000_000)
    metadata_bytes: int = Field(default=_GiB, strict=True, ge=1024, le=16 * _GiB)
    project_metadata_bytes: int = Field(
        default=256 * 1024 * 1024, strict=True, ge=1024, le=16 * _GiB
    )
    """One project's share of the metadata, so it cannot stop every project's recording."""


@dataclass(frozen=True)
class ArtifactOperation:
    state: Literal["intent", "transfer", "done"]
    worker: ArtifactRef | None
    receipt: ArtifactReceipt | None
    fresh: bool = False


def _json(value: Contract, limit: int = _MAX_RECORD) -> str:
    text = value.model_dump_json()
    if len(text.encode()) > limit:
        raise DocumentError(INVALID_REQUEST, "artifact metadata exceeds its limit")
    return text


class ArtifactInventory:
    """Project-scoped internal ingestion/read boundary sharing DocumentStore ownership."""

    def __init__(self, documents: DocumentStore, *, limits: ArtifactLimits | None = None) -> None:
        self.documents = documents
        self.limits = limits or ArtifactLimits()

    def _scope(self, project: str) -> None:
        _IDENTIFIER.validate_python(project)

    def _usage(self, db: sqlite3.Connection, scope: str | None) -> tuple[int, int, int, int]:
        """Revisions, retained bytes, operations and metadata bytes, of one project or
        (``None``) of all, as the control store keeps them (its version 20)."""
        row = db.execute(
            "SELECT revisions,bytes,operations,metadata FROM artifact_usage WHERE project=?",
            (scope or "",),
        ).fetchone()
        return (0, 0, 0, 0) if row is None else (row[0], row[1], row[2], row[3])

    def _budget(self, db: sqlite3.Connection, project: str) -> None:
        limits = self.limits
        for scope, byte_limit, revision_limit, operation_limit in (
            (None, limits.total_bytes, limits.total_revisions, limits.total_operations),
            (project, limits.project_bytes, limits.project_revisions, limits.project_operations),
        ):
            count, size, operations, _ = self._usage(db, scope)
            if count > revision_limit or size > byte_limit or operations > operation_limit:
                raise DocumentError(
                    INVALID_REQUEST, "artifact retention quota is full or has tightened"
                )
        if (
            self._usage(db, None)[3] > limits.metadata_bytes
            or self._usage(db, project)[3] > limits.project_metadata_bytes
        ):
            raise DocumentError(INVALID_REQUEST, "artifact metadata retention quota is full")

    def room(self, project: str, size: int) -> Room:
        """Whether a new revision of ``size`` bytes can keep them (``content``), only its
        hash and size (``metadata``), or cannot be recorded (``full``) (RA-31, RA-32)."""
        self._scope(project)
        limits = self.limits
        with self.documents.control.read() as tx:
            db = tx.sql
            reserve = 2 * _MAX_RECORD + _MAX_REQUEST
            spare = (
                self._usage(db, None)[3] + reserve <= limits.metadata_bytes
                and self._usage(db, project)[3] + reserve <= limits.project_metadata_bytes
            )
            room: Room = "content" if size <= limits.artifact_bytes else "metadata"
            for scope, byte_limit, revision_limit, operation_limit in (
                (None, limits.total_bytes, limits.total_revisions, limits.total_operations),
                (
                    project,
                    limits.project_bytes,
                    limits.project_revisions,
                    limits.project_operations,
                ),
            ):
                count, held, operations, _ = self._usage(db, scope)
                if count >= revision_limit or not spare:
                    return "full"
                if held + size > byte_limit or operations >= operation_limit:
                    room = "metadata"
            return room

    def publication_request(
        self, source: ArtifactPublicationSource, path: str, media_type: str | None
    ) -> tuple[str, str]:
        artifact_path(path)
        if source.context.actor.casefold() == "user":
            raise DocumentError(INVALID_REQUEST, "agent publication cannot impersonate USER")
        text = json.dumps(
            {"source": source.model_dump(mode="json"), "path": path, "media_type": media_type},
            sort_keys=True,
            separators=(",", ":"),
        )
        if len(text.encode()) > _MAX_REQUEST:
            raise DocumentError(INVALID_REQUEST, "artifact publication intent exceeds its limit")
        return text, hashlib.sha256(text.encode()).hexdigest()

    def find_operation(
        self, db: sqlite3.Connection, source: ArtifactPublicationSource, operation: str, digest: str
    ) -> ArtifactOperation | None:
        _IDENTIFIER.validate_python(operation)
        row = db.execute(
            "SELECT digest,state,worker,receipt,request FROM artifact_operations "
            "WHERE project=? AND actor=? AND operation=?",
            (source.context.project_id, source.context.session_id, operation),
        ).fetchone()
        if row is None:
            return None
        if row[0] != digest:
            raise DocumentError(
                CONFLICT,
                "artifact operation was already used with another request or publisher binding",
            )
        worker = ArtifactRef.model_validate_json(row[2]) if row[2] is not None else None
        receipt = ArtifactReceipt.model_validate_json(row[3]) if row[3] is not None else None
        if row[1] == "done" and (worker is None or receipt is None or receipt.state != "recorded"):
            raise DocumentError(INTERNAL, "incomplete stored artifact acknowledgement")
        if row[1] == "done":
            assert worker is not None  # noqa: S101 - validated above
            assert receipt is not None  # noqa: S101 - validated above
            request = json.loads(row[4])
            path = request["path"]
            logical_path = _logical(path, source.context.session_id)
            revision = (
                self._revision(
                    db,
                    source.context.project_id,
                    receipt.revision.artifact_id,
                    receipt.revision.revision,
                )
                if receipt.revision is not None
                else None
            )
            if (
                receipt.operation_id != operation
                or receipt.revision is None
                or receipt.revision.project_id != source.context.project_id
                or receipt.worker_snapshot is None
                or receipt.worker_snapshot.model_dump() != worker.model_dump()
                or revision is None
                or revision.reference != receipt.revision
                or revision.path != logical_path
                or receipt.revision.sha256 != worker.sha256
                or receipt.revision.size != worker.size
                or worker.project_id != source.context.project_id
                or worker.source_path != path
            ):
                raise DocumentError(INTERNAL, "stored artifact acknowledgement identity differs")
        elif receipt is not None and (
            receipt.state != "held"
            or receipt.operation_id != operation
            or receipt.revision is not None
        ):
            raise DocumentError(INTERNAL, "invalid stored artifact hold observation")
        if row[1] == "transfer" and worker is None:
            raise DocumentError(INTERNAL, "missing pinned worker snapshot")
        return ArtifactOperation(row[1], worker, receipt)

    def hold(
        self,
        source: ArtifactPublicationSource,
        operation: str,
        path: str,
        media_type: str | None,
        message: str,
    ) -> ArtifactReceipt:
        """Retain the latest bounded failure observation without changing retry ownership."""
        _, digest = self.publication_request(source, path, media_type)
        receipt = ArtifactReceipt(state="held", operation_id=operation, message=message[:512])
        with self.documents.control.write() as tx:
            db = tx.sql
            found = self.find_operation(db, source, operation, digest)
            if found is None:
                raise DocumentError(NOT_FOUND, "no reserved artifact publication intent")
            if found.state == "done":
                assert found.receipt is not None  # noqa: S101 - done validated above
                return found.receipt
            db.execute(
                "UPDATE artifact_operations SET receipt=? "
                "WHERE project=? AND actor=? AND operation=?",
                (_json(receipt), source.context.project_id, source.context.session_id, operation),
            )
            return receipt

    def begin(
        self, source: ArtifactPublicationSource, operation: str, path: str, media_type: str | None
    ) -> ArtifactOperation:
        """Reserve before RPC; an earlier unknown intent is deliberately never replayed."""
        request, digest = self.publication_request(source, path, media_type)
        with self.documents.control.write() as tx:
            db = tx.sql
            found = self.find_operation(db, source, operation, digest)
            if found is not None:
                return found
            self._budget(db, source.context.project_id)
            db.execute(
                "INSERT INTO artifact_operations VALUES (?,?,?,?,?,'intent',NULL,NULL)",
                (source.context.project_id, source.context.session_id, operation, digest, request),
            )
            self._budget(db, source.context.project_id)
        return ArtifactOperation("intent", None, None, True)

    def pin(
        self,
        source: ArtifactPublicationSource,
        operation: str,
        path: str,
        media_type: str | None,
        worker: ArtifactRef,
    ) -> None:
        """Durably retain the known worker outcome before transferring any bytes."""
        _, digest = self.publication_request(source, path, media_type)
        encoded = _json(worker)
        with self.documents.control.write() as tx:
            db = tx.sql
            found = self.find_operation(db, source, operation, digest)
            if found is None or found.state != "intent" or found.worker is not None:
                raise DocumentError(CONFLICT, "artifact publication no longer owns its intent")
            db.execute(
                "UPDATE artifact_operations SET state='transfer',worker=? "
                "WHERE project=? AND actor=? AND operation=?",
                (encoded, source.context.project_id, source.context.session_id, operation),
            )
            # begin() reserved both bounded worker metadata and receipt space. Retain
            # the known remote outcome even when an operator has since tightened caps.

    def finish(
        self,
        source: ArtifactPublicationSource,
        operation: str,
        path: str,
        media_type: str | None,
        worker: ArtifactRef,
        data: bytes,
    ) -> ArtifactReceipt:
        """Commit bytes, immutable revision, head, event and operation receipt atomically."""
        _, digest = self.publication_request(source, path, media_type)
        snapshot = ArtifactSnapshot.model_validate(worker.model_dump())
        project, session = source.context.project_id, source.context.session_id
        if (
            snapshot.project_id != project
            or snapshot.source_path != path
            or (media_type is not None and snapshot.media_type != media_type)
        ):
            raise DocumentError(
                INVALID_REQUEST, "worker snapshot does not match publication scope/path/media"
            )
        if (
            len(data) > self.limits.artifact_bytes
            or len(data) != snapshot.size
            or hashlib.sha256(data).hexdigest() != snapshot.sha256
        ):
            raise DocumentError(
                INVALID_REQUEST, "artifact bytes exceed quota or fail exact size/hash verification"
            )
        logical_path = _logical(path, session)
        with self.documents.control.write() as tx:
            db = tx.sql
            found = self.find_operation(db, source, operation, digest)
            if found is not None and found.state == "done" and found.receipt is not None:
                return found.receipt
            if (
                found is None
                or found.state != "transfer"
                or found.worker is None
                or found.worker.model_dump() != worker.model_dump()
            ):
                raise DocumentError(
                    CONFLICT, "artifact transfer no longer matches its pinned worker outcome"
                )
            self._budget(db, project)
            appended = self._append(
                db,
                source,
                logical_path,
                sha256=snapshot.sha256,
                size=snapshot.size,
                media_type=snapshot.media_type,
                snapshot=snapshot,
                data=data,
            )
            assert appended is not None  # noqa: S101 - only a deletion appends nothing
            reference, reproduced = appended
            receipt = ArtifactReceipt(
                state="recorded",
                operation_id=operation,
                worker_snapshot=snapshot,
                revision=reference,
                reproduced=reproduced,
            )
            db.execute(
                "UPDATE artifact_operations SET state='done',receipt=? "
                "WHERE project=? AND actor=? AND operation=?",
                (_json(receipt), project, session, operation),
            )
            self._budget(db, project)
            return receipt

    def _append(
        self,
        db: sqlite3.Connection,
        source: ArtifactPublicationSource,
        logical_path: str,
        *,
        sha256: str,
        size: int,
        media_type: str,
        snapshot: ArtifactSnapshot | None = None,
        data: bytes = b"",
        note: Literal["too_large", "quota"] | None = None,
        deleted: bool = False,
    ) -> tuple[ArtifactRevisionRef, bool] | None:
        """Append a revision and its event, or only a reproduction event when the head
        holds the same content. A deletion of a missing or deleted artifact appends
        nothing (``None``)."""
        project = source.context.project_id
        row = db.execute(
            "SELECT id,head FROM artifact_objects WHERE project=? AND path=?",
            (project, logical_path),
        ).fetchone()
        artifact, head = (new_id("artifact"), 0) if row is None else row
        previous = self._revision(db, project, artifact, head) if head else None
        if deleted and (previous is None or previous.deleted):
            return None
        # Bytes kept now are no reproduction of a revision that kept only a hash: they
        # are recorded, and its hash, which the sandbox reported, is not taken on trust.
        reproduced = (
            not deleted
            and previous is not None
            and not previous.deleted
            and (previous.content_retained or snapshot is None)
            and (previous.reference.sha256, previous.reference.size) == (sha256, size)
        )
        now = datetime.now(UTC)
        if reproduced:
            assert previous is not None  # noqa: S101 - reproduced requires previous
            reference = previous.reference
            kind = "artifact.reproduced"
        else:
            reference = ArtifactRevisionRef(
                artifact_id=artifact,
                project_id=project,
                revision=head + 1,
                sha256=sha256,
                size=size,
                media_type=media_type,
            )
            revision = ArtifactRevision(
                reference=reference,
                path=logical_path,
                parent=head or None,
                created_at=now,
                publication=source,
                worker_snapshot=snapshot,
                worker_last_writer_hint=snapshot.produced_by if snapshot else None,
                content_retained=snapshot is not None,
                content_note=note,
                deleted=deleted,
                hash_source=None if deleted else "capture" if snapshot else "sandbox",
            )
            if row is None:
                db.execute(
                    "INSERT INTO artifact_objects VALUES (?,?,?,?)",
                    (artifact, project, logical_path, head + 1),
                )
            else:
                db.execute(
                    "UPDATE artifact_objects SET head=? WHERE id=? AND project=? AND head=?",
                    (head + 1, artifact, project, head),
                )
            db.execute(
                "INSERT INTO artifact_revisions VALUES (?,?,?,?,?,?)",
                (artifact, head + 1, project, size, _json(revision), data),
            )
            kind = "artifact.deleted" if deleted else "artifact.revision_created"
        (sequence,) = db.execute("SELECT coalesce(max(seq),0)+1 FROM artifact_events").fetchone()
        event = ArtifactEvent(
            seq=sequence,
            project_id=project,
            kind=kind,
            revision=reference,
            source=source,
            worker_snapshot=snapshot,
            created_at=now,
        )
        db.execute(
            "INSERT INTO artifact_events(seq,project,record) VALUES (?,?,?)",
            (event.seq, project, _json(event)),
        )
        return reference, reproduced

    def _recording(self, source: ArtifactPublicationSource, path: str) -> str:
        artifact_path(path)
        if source.context.actor.casefold() == "user":
            raise DocumentError(INVALID_REQUEST, "an agent's record cannot impersonate USER")
        return _logical(path, source.context.session_id)

    def record_metadata(
        self,
        source: ArtifactPublicationSource,
        path: str,
        *,
        sha256: str,
        size: int,
        media_type: str,
        note: Literal["too_large", "quota"],
    ) -> tuple[ArtifactRevisionRef, bool]:
        """Record a file's hash and size without its bytes (RA-31, RA-32); the same
        hash and size as the head only adds a reproduction."""
        logical_path = self._recording(source, path)
        with self.documents.control.write() as tx:
            self._budget(tx.sql, source.context.project_id)
            appended = self._append(
                tx.sql, source, logical_path, sha256=sha256, size=size,
                media_type=media_type, note=note,
            )  # fmt: skip
            assert appended is not None  # noqa: S101 - only a deletion appends nothing
            self._budget(tx.sql, source.context.project_id)
            return appended

    def record_deletion(
        self, source: ArtifactPublicationSource, path: str
    ) -> ArtifactRevisionRef | None:
        """A tombstone for a deleted file (RA-33); ``None`` when no live artifact is there."""
        logical_path = self._recording(source, path)
        with self.documents.control.write() as tx:
            row = tx.sql.execute(
                "SELECT head FROM artifact_objects WHERE project=? AND path=?",
                (source.context.project_id, logical_path),
            ).fetchone()
            if row is None:
                return None
            self._budget(tx.sql, source.context.project_id)
            appended = self._append(
                tx.sql, source, logical_path, sha256=_EMPTY, size=0,
                media_type="application/octet-stream", deleted=True,
            )  # fmt: skip
            self._budget(tx.sql, source.context.project_id)
            return appended[0] if appended else None

    def _revision(
        self, db: sqlite3.Connection, project: str, artifact: str, revision: int
    ) -> ArtifactRevision:
        row = db.execute(
            "SELECT record FROM artifact_revisions WHERE project=? AND artifact=? AND number=?",
            (project, artifact, revision),
        ).fetchone()
        if row is None:
            raise DocumentError(NOT_FOUND, "no such artifact revision in project")
        result = ArtifactRevision.model_validate_json(row[0])
        if (
            result.reference.project_id,
            result.reference.artifact_id,
            result.reference.revision,
        ) != (project, artifact, revision):
            raise DocumentError(INTERNAL, "stored artifact revision identity differs")
        return result

    def resolve(self, project: str, link: ArtifactLink) -> ArtifactRevision:
        """Resolve latest in one snapshot, returning an exact pinned revision."""
        self._scope(project)
        if link.project_id != project:
            raise DocumentError(NOT_FOUND, "no such artifact in project")
        if self.documents.control.missing:
            raise DocumentError(NOT_FOUND, "no artifact inventory")
        with self.documents.control.read() as tx:
            db = tx.sql
            row = db.execute(
                "SELECT head FROM artifact_objects WHERE project=? AND id=?",
                (project, link.artifact_id),
            ).fetchone()
            if row is None:
                raise DocumentError(NOT_FOUND, "no such artifact in project")
            number = row[0] if link.mode == "latest" else link.revision
            if type(number) is not int or number < 1:
                raise DocumentError(INTERNAL, "invalid artifact head")
            result = self._revision(db, project, link.artifact_id, number)
            if link.mode == "pinned" and result.reference.sha256 != link.sha256:
                raise DocumentError(CONFLICT, "artifact pinned hash differs")
            return result

    def content(
        self, project: str, reference: ArtifactRevisionRef, *, max_bytes: int | None = None
    ) -> bytes:
        """Exact pinned bytes for future evidence consumers; never parse rich content."""
        self._scope(project)
        bound = self.limits.artifact_bytes if max_bytes is None else max_bytes
        if type(bound) is not int or not 0 <= bound <= self.limits.artifact_bytes:
            raise DocumentError(INVALID_REQUEST, "invalid artifact content bound")
        if project != reference.project_id:
            raise DocumentError(NOT_FOUND, "no such artifact in project")
        if self.documents.control.missing:
            raise DocumentError(NOT_FOUND, "no artifact inventory")
        with self.documents.control.read() as tx:
            db = tx.sql
            revision = self._revision(db, project, reference.artifact_id, reference.revision)
            if revision.reference != reference:
                raise DocumentError(CONFLICT, "artifact revision reference differs")
            if not revision.content_retained:
                raise DocumentError(
                    NOT_FOUND,
                    "the file was deleted"
                    if revision.deleted
                    else f"the revision's bytes were not kept ({revision.content_note})",
                )
            if reference.size > bound:
                raise DocumentError(INVALID_REQUEST, "artifact content exceeds consumer bound")
            row = db.execute(
                "SELECT CASE WHEN length(data)=0 THEN x'' ELSE substr(data,1,?) END "
                "FROM artifact_revisions "
                "WHERE project=? AND artifact=? AND number=?",
                (
                    bound + 1,
                    project,
                    reference.artifact_id,
                    reference.revision,
                ),
            ).fetchone()
            assert row is not None  # noqa: S101 - same read transaction validated revision
            data = row[0]
            if (
                not isinstance(data, bytes)
                or len(data) > bound
                or len(data) != reference.size
                or hashlib.sha256(data).hexdigest() != reference.sha256
            ):
                raise DocumentError(
                    INTERNAL, "retained artifact content fails exact integrity verification"
                )
            return data

    def list(self, project: str, *, after: str = "") -> ArtifactPage:
        """At most ten head records, in stable ID order, without content allocation."""
        self._scope(project)
        if after:
            _IDENTIFIER.validate_python(after)
        if self.documents.control.missing:
            return ArtifactPage(entries=(), next_after=None)
        with self.documents.control.read() as tx:
            db = tx.sql
            rows = db.execute(
                "SELECT id,head FROM artifact_objects "
                "WHERE project=? AND id>? ORDER BY id LIMIT 11",
                (project, after),
            ).fetchall()
            entries = tuple(
                self._revision(db, project, artifact, number) for artifact, number in rows[:10]
            )
            return ArtifactPage(
                entries=entries,
                next_after=entries[-1].reference.artifact_id
                if entries and len(rows) > 10
                else None,
            )

    def recent(
        self,
        project: str,
        *,
        before: int | None = None,
        group: Literal["results", "files"] | None = None,
        session: str | None = None,
        run: str | None = None,
        contains: str | None = None,
        at_path: str | None = None,
        media_type: str | None = None,
    ) -> tuple[tuple[ArtifactRevision, ...], int | None]:
        """At most ten artifacts at their latest revision, the most recently changed
        first, and the next page's ``before`` (``None`` at the end). ``results`` are the
        artifacts with a published or declared revision, ``files`` the others (RA-37);
        ``session`` and ``run`` keep those with a revision of theirs, ``contains`` those
        whose path has it, ``at_path`` the one at that artifact path, and ``media_type``
        those whose latest revision's media type starts with it, such as ``image/``."""
        self._scope(project)
        for value in (session, run):
            if value is not None:
                _IDENTIFIER.validate_python(value)
        for value, what in ((contains, "path filter"), (media_type, "media type filter")):
            if value is not None and (
                not 1 <= len(value) <= 256 or any(ord(c) < 32 or ord(c) == 127 for c in value)
            ):
                raise DocumentError(INVALID_REQUEST, f"invalid {what}")
        if at_path is not None:
            _checked(at_path)
        if before is not None and (type(before) is not int or before < 1):
            raise DocumentError(INVALID_REQUEST, "invalid artifact cursor")
        if self.documents.control.missing:
            return (), None
        conditions: list[str] = ["r.project=?", "o.project=?"]
        values: list[object] = [project, project]
        if before is not None:
            conditions.append("r.rowid<?")
            values.append(before)
        if group is not None:
            conditions.append(("" if group == "results" else "NOT ") + _result("o"))
        for path, value in (
            ("$.publication.context.session_id", session),
            ("$.publication.run_id", run),
        ):
            if value is not None:
                conditions.append(
                    "EXISTS(SELECT 1 FROM artifact_revisions m "  # noqa: S608 - fixed paths
                    f"WHERE m.artifact=o.id AND json_extract(m.record,'{path}')=?)"
                )
                values.append(value)
        if contains is not None:
            conditions.append("instr(o.path,?)>0")
            values.append(contains)
        if at_path is not None:
            conditions.append("o.path=?")
            values.append(at_path)
        if media_type is not None:
            conditions.append("instr(json_extract(r.record,'$.reference.media_type'),?)=1")
            values.append(media_type)
        with self.documents.control.read() as tx:
            db = tx.sql
            # A revision's rowid grows with every insert (nothing vacuums the store), so
            # the head's says when the artifact last changed. The walk goes down the
            # project's revisions, newest first, and keeps the heads: a page of all
            # files stops at its eleventh, while a rare match reads them all.
            rows = db.execute(
                "SELECT o.id,o.head,r.rowid FROM artifact_revisions r CROSS JOIN "  # noqa: S608 - fixed conditions
                "artifact_objects o ON o.id=r.artifact AND o.head=r.number WHERE "
                + " AND ".join(conditions)
                + " ORDER BY r.rowid DESC LIMIT 11",
                values,
            ).fetchall()
            entries = tuple(
                self._revision(db, project, artifact, number) for artifact, number, _ in rows[:10]
            )
            return entries, (rows[9][2] if len(rows) > 10 else None)

    def results(self, project: str, artifacts: Sequence[str]) -> set[str]:
        """Which of ``artifacts`` have a published or declared revision (RA-37)."""
        self._scope(project)
        if not artifacts or self.documents.control.missing:
            return set()
        with self.documents.control.read() as tx:
            marks = ",".join("?" * len(artifacts))
            rows = tx.sql.execute(
                f"SELECT id FROM artifact_objects WHERE project=? AND id IN ({marks}) AND "  # noqa: S608
                + _result("artifact_objects"),
                (project, *artifacts),
            ).fetchall()
        return {row[0] for row in rows}

    def events(self, project: str, *, after: int = 0) -> ArtifactEventPage:
        """Bounded immutable event pages, without guessing execution lineage."""
        self._scope(project)
        if type(after) is not int or not 0 <= after <= MAX_COUNTER:
            raise DocumentError(INVALID_REQUEST, "invalid artifact event cursor")
        if self.documents.control.missing:
            return ArtifactEventPage(entries=(), next_after=after)
        with self.documents.control.read() as tx:
            db = tx.sql
            rows = db.execute(
                "SELECT seq,record FROM artifact_events "
                "WHERE project=? AND seq>? ORDER BY seq LIMIT 10",
                (project, after),
            ).fetchall()
            events = tuple(ArtifactEvent.model_validate_json(record) for _, record in rows)
            if any(
                event.project_id != project or event.seq != row[0]
                for event, row in zip(events, rows, strict=True)
            ):
                raise DocumentError(INTERNAL, "stored artifact event identity differs")
            return ArtifactEventPage(entries=events, next_after=events[-1].seq if events else after)


def scoped_path(path: str, session: str) -> str:
    """The artifact path of a path as a session's tools name it: ``shared/…`` as it is,
    any other in the session's own area."""
    return _logical(_checked(path), session)


def _checked(path: str) -> str:
    try:
        return artifact_path(path)
    except ValueError:
        raise DocumentError(INVALID_REQUEST, "invalid artifact path") from None


def _logical(path: str, session: str) -> str:
    """The artifact path of a workspace path: ``shared/…`` as it is, any other in its session."""
    return path if path.startswith("shared/") else f"sessions/{session}/{path}"
