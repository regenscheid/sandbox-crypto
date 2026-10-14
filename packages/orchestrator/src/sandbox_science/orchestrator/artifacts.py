# SPDX-License-Identifier: MIT
"""Bounded immutable artifact inventory in the canonical control transaction.

Only explicit harness publication is ingested. Source paths and worker last-writer
hints never prove a producing execution or observed input. Rich bytes are stored and
hashed, never parsed. Unresolved publication intents are retained rather than replayed.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
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


class ArtifactLimits(Contract):
    """Logical retention quotas; full retention refuses new work rather than evicting."""

    artifact_bytes: int = Field(
        default=MAX_ARTIFACT_BYTES, strict=True, ge=1, le=MAX_ARTIFACT_BYTES
    )
    project_bytes: int = Field(default=64 * 1024 * 1024, strict=True, ge=1, le=256 * 1024 * 1024)
    total_bytes: int = Field(default=256 * 1024 * 1024, strict=True, ge=1, le=1024**3)
    project_revisions: int = Field(default=4096, strict=True, ge=1, le=100_000)
    total_revisions: int = Field(default=16_384, strict=True, ge=1, le=1_000_000)
    project_operations: int = Field(default=8192, strict=True, ge=1, le=100_000)
    total_operations: int = Field(default=32_768, strict=True, ge=1, le=1_000_000)
    metadata_bytes: int = Field(
        default=64 * 1024 * 1024, strict=True, ge=1024, le=256 * 1024 * 1024
    )


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


def _clipped(value: object, limit: int) -> str:
    if not isinstance(value, bytes) or len(value) > limit:
        raise DocumentError(INTERNAL, "stored artifact metadata exceeds its limit")
    return value.decode("utf-8", "strict")


class ArtifactInventory:
    """Project-scoped internal ingestion/read boundary sharing DocumentStore ownership."""

    def __init__(self, documents: DocumentStore, *, limits: ArtifactLimits | None = None) -> None:
        self.documents = documents
        self.limits = limits or ArtifactLimits()

    def _scope(self, project: str) -> None:
        _IDENTIFIER.validate_python(project)

    def _budget(self, db: sqlite3.Connection, project: str) -> None:
        limits = self.limits
        for scope, byte_limit, revision_limit, operation_limit in (
            (None, limits.total_bytes, limits.total_revisions, limits.total_operations),
            (project, limits.project_bytes, limits.project_revisions, limits.project_operations),
        ):
            count, size, largest, negative = db.execute(
                "SELECT count(*),coalesce(sum(length(data)),0),coalesce(max(length(data)),0),"
                "coalesce(min(size),0) FROM artifact_revisions WHERE ? IS NULL OR project=?",
                (scope, scope),
            ).fetchone()
            (operations,) = db.execute(
                "SELECT count(*) FROM artifact_operations WHERE ? IS NULL OR project=?",
                (scope, scope),
            ).fetchone()
            if (
                count > revision_limit
                or size > byte_limit
                or largest > limits.artifact_bytes
                or negative < 0
                or operations > operation_limit
            ):
                raise DocumentError(
                    INVALID_REQUEST, "artifact retention quota is full or has tightened"
                )
        (metadata,) = db.execute(
            "SELECT (SELECT coalesce(sum(length(cast(record AS BLOB))),0) FROM artifact_revisions)"
            "+(SELECT count(*) FROM artifact_operations)*?"
            "+(SELECT coalesce(sum(length(cast(record AS BLOB))),0) FROM artifact_events)"
            "+(SELECT coalesce(sum(length(cast(path AS BLOB))+length(id)+length(project)),0) "
            "FROM artifact_objects)",
            (_MAX_REQUEST + 2 * _MAX_RECORD,),
        ).fetchone()
        if metadata > limits.metadata_bytes:
            raise DocumentError(INVALID_REQUEST, "artifact metadata retention quota is full")

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
            "SELECT substr(digest,1,65),substr(state,1,9),substr(cast(worker AS BLOB),1,?),"
            "substr(cast(receipt AS BLOB),1,?),substr(cast(request AS BLOB),1,?) "
            "FROM artifact_operations "
            "WHERE project=? AND actor=? AND operation=?",
            (
                _MAX_RECORD + 1,
                _MAX_RECORD + 1,
                _MAX_REQUEST + 1,
                source.context.project_id,
                source.context.session_id,
                operation,
            ),
        ).fetchone()
        if row is None:
            return None
        if row[0] != digest:
            raise DocumentError(
                CONFLICT,
                "artifact operation was already used with another request or publisher binding",
            )
        if hashlib.sha256(_clipped(row[4], _MAX_REQUEST).encode()).hexdigest() != digest:
            raise DocumentError(INTERNAL, "stored artifact intent differs from its fingerprint")
        if row[1] not in ("intent", "transfer", "done"):
            raise DocumentError(INTERNAL, "invalid stored artifact operation state")
        worker = (
            ArtifactRef.model_validate_json(_clipped(row[2], _MAX_RECORD))
            if row[2] is not None
            else None
        )
        receipt = (
            ArtifactReceipt.model_validate_json(_clipped(row[3], _MAX_RECORD))
            if row[3] is not None
            else None
        )
        if row[1] == "done" and (worker is None or receipt is None or receipt.state != "recorded"):
            raise DocumentError(INTERNAL, "incomplete stored artifact acknowledgement")
        if row[1] == "done":
            assert worker is not None  # noqa: S101 - validated above
            assert receipt is not None  # noqa: S101 - validated above
            request = json.loads(_clipped(row[4], _MAX_REQUEST))
            path = request["path"]
            logical_path = (
                path
                if path.startswith("shared/")
                else f"sessions/{source.context.session_id}/{path}"
            )
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
        logical_path = path if path.startswith("shared/") else f"sessions/{session}/{path}"
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
            row = db.execute(
                "SELECT substr(id,1,129),head FROM artifact_objects WHERE project=? AND path=?",
                (project, logical_path),
            ).fetchone()
            artifact, head = (new_id("artifact"), 0) if row is None else row
            _IDENTIFIER.validate_python(artifact)
            if type(head) is not int or not 0 <= head < MAX_COUNTER:
                raise DocumentError(INTERNAL, "invalid artifact head")
            previous = self._revision(db, project, artifact, head) if head else None
            reproduced = previous is not None and (
                previous.reference.sha256,
                previous.reference.size,
            ) == (snapshot.sha256, snapshot.size)
            now = datetime.now(UTC)
            if reproduced:
                assert previous is not None  # noqa: S101 - reproduced requires previous
                reference = previous.reference
            else:
                reference = ArtifactRevisionRef(
                    artifact_id=artifact,
                    project_id=project,
                    revision=head + 1,
                    sha256=snapshot.sha256,
                    size=snapshot.size,
                    media_type=snapshot.media_type,
                )
                revision = ArtifactRevision(
                    reference=reference,
                    path=logical_path,
                    parent=head or None,
                    created_at=now,
                    publication=source,
                    worker_snapshot=snapshot,
                    worker_last_writer_hint=snapshot.produced_by,
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
                    (artifact, head + 1, project, snapshot.size, _json(revision), data),
                )
            (sequence,) = db.execute(
                "SELECT coalesce(max(seq),0)+1 FROM artifact_events"
            ).fetchone()
            event = ArtifactEvent(
                seq=sequence,
                project_id=project,
                kind="artifact.reproduced" if reproduced else "artifact.revision_created",
                revision=reference,
                source=source,
                worker_snapshot=snapshot,
                created_at=now,
            )
            db.execute(
                "INSERT INTO artifact_events(seq,project,record) VALUES (?,?,?)",
                (event.seq, project, _json(event)),
            )
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

    def _revision(
        self, db: sqlite3.Connection, project: str, artifact: str, revision: int
    ) -> ArtifactRevision:
        row = db.execute(
            "SELECT substr(cast(record AS BLOB),1,?) FROM artifact_revisions "
            "WHERE project=? AND artifact=? AND number=?",
            (_MAX_RECORD + 1, project, artifact, revision),
        ).fetchone()
        if row is None:
            raise DocumentError(NOT_FOUND, "no such artifact revision in project")
        result = ArtifactRevision.model_validate_json(_clipped(row[0], _MAX_RECORD))
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
        if reference.size > bound:
            raise DocumentError(INVALID_REQUEST, "artifact content exceeds consumer bound")
        if project != reference.project_id:
            raise DocumentError(NOT_FOUND, "no such artifact in project")
        if self.documents.control.missing:
            raise DocumentError(NOT_FOUND, "no artifact inventory")
        with self.documents.control.read() as tx:
            db = tx.sql
            revision = self._revision(db, project, reference.artifact_id, reference.revision)
            if revision.reference != reference:
                raise DocumentError(CONFLICT, "artifact revision reference differs")
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
                "SELECT substr(id,1,129),head FROM artifact_objects "
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
                "SELECT seq,substr(cast(record AS BLOB),1,?) FROM artifact_events "
                "WHERE project=? AND seq>? ORDER BY seq LIMIT 10",
                (_MAX_RECORD + 1, project, after),
            ).fetchall()
            events = tuple(
                ArtifactEvent.model_validate_json(_clipped(record, _MAX_RECORD))
                for _, record in rows
            )
            if any(
                event.project_id != project or event.seq != row[0]
                for event, row in zip(events, rows, strict=True)
            ):
                raise DocumentError(INTERNAL, "stored artifact event identity differs")
            return ArtifactEventPage(entries=events, next_after=events[-1].seq if events else after)
