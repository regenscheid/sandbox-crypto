# SPDX-License-Identifier: MIT
"""Exact durable acknowledgements and offline, conservative capture replica release."""

from __future__ import annotations

import hashlib
import os
import re
import sqlite3
import stat
from dataclasses import dataclass

from sandbox_science.contracts import SYSTEM_CLOCK, Clock, ErrorKind
from sandbox_science.contracts.artifact_inventory import ArtifactSnapshot
from sandbox_science.contracts.artifact_retention import (
    MAX_RETENTION_ACK_BYTES,
    ArtifactRetentionAcknowledgement,
)
from sandbox_science.contracts.canonical import canonical_json
from sandbox_science.contracts.jsonio import strict_loads

from .artifacts import Blobs
from .errors import WorkerError

MAX_RELEASE_PAGE = 100
MAX_HASH_REFERENCES = 1000
_INSTALLER = "application/vnd.sandbox-science.installer-log+json"
# The bounded expression is indexed by the migration, including NULL for records
# whose hash cannot be classified. An unknown hash pins every candidate conservatively.
ARTIFACT_HASH = f"""CASE WHEN typeof(record)='text'
 AND length(CAST(record AS BLOB)) BETWEEN 1 AND {MAX_RETENTION_ACK_BYTES}
 THEN CASE WHEN json_valid(record) THEN CASE
 WHEN json_type(record,'$.sha256')='text'
 AND length(CAST(json_extract(record,'$.sha256') AS BLOB))=64
 AND instr(json_extract(record,'$.sha256'),char(0))=0
 AND json_extract(record,'$.sha256') NOT GLOB '*[^0-9a-f]*'
 THEN json_extract(record,'$.sha256') END END END"""
_ACK_COLUMNS = f"""typeof(record),length(CAST(record AS BLOB)),
 substr(CAST(record AS BLOB),1,{MAX_RETENTION_ACK_BYTES + 1}),
 CASE WHEN typeof(acknowledgement_id)='text'
 AND length(CAST(acknowledgement_id AS BLOB)) BETWEEN 1 AND 128
 THEN acknowledgement_id END,
 CASE WHEN typeof(artifact_id)='text' AND length(CAST(artifact_id AS BLOB)) BETWEEN 1 AND 128
 THEN artifact_id END,
 CASE WHEN typeof(snapshot_digest)='text' AND length(CAST(snapshot_digest AS BLOB))=64
 THEN snapshot_digest END"""


def create_schema(db: sqlite3.Connection) -> None:
    """Append-only worker migration; existing metadata is never rewritten."""
    db.execute(f"CREATE INDEX artifact_retention_hash ON artifacts(({ARTIFACT_HASH}))")
    db.execute(
        f"""CREATE TABLE artifact_retention_acknowledgements (
        acknowledgement_id TEXT PRIMARY KEY,
        artifact_id TEXT NOT NULL,
        snapshot_digest TEXT NOT NULL,
        record TEXT NOT NULL CHECK (length(CAST(record AS BLOB)) BETWEEN 1
            AND {MAX_RETENTION_ACK_BYTES}),
        recorded_at TEXT NOT NULL
        ) STRICT"""
    )
    db.execute(
        "CREATE INDEX artifact_retention_snapshot ON artifact_retention_acknowledgements"
        "(artifact_id,snapshot_digest)"
    )
    db.execute(
        """CREATE TABLE artifact_release_intents (
        release_id INTEGER PRIMARY KEY,
        sha256 TEXT NOT NULL CHECK (length(sha256)=64 AND sha256 NOT GLOB '*[^0-9a-f]*'),
        size INTEGER NOT NULL CHECK (size BETWEEN 0 AND 4611686018427387904),
        intended_at TEXT NOT NULL,
        settled_at TEXT
        ) STRICT"""
    )
    db.execute(
        "CREATE UNIQUE INDEX artifact_release_pending ON artifact_release_intents(sha256)"
        " WHERE settled_at IS NULL"
    )


def _snapshot_digest(snapshot: ArtifactSnapshot) -> str:
    return hashlib.sha256(canonical_json(snapshot.model_dump(mode="json"))).hexdigest()


def _snapshot(record: str | bytes) -> ArtifactSnapshot:
    strict_loads(record)
    return ArtifactSnapshot.model_validate_json(record, strict=True)


def _recorded_ack(row: tuple[object, ...]) -> ArtifactRetentionAcknowledgement:
    kind, length, encoded, acknowledgement_id, artifact_id, snapshot_digest = row
    if (
        kind != "text"
        or type(length) is not int
        or not 0 < length <= MAX_RETENTION_ACK_BYTES
        or type(encoded) is not bytes
        or len(encoded) != length
    ):
        raise ValueError("invalid acknowledgement bounds")
    strict_loads(encoded)
    acknowledgement = ArtifactRetentionAcknowledgement.model_validate_json(encoded, strict=True)
    if (
        acknowledgement.acknowledgement_id != acknowledgement_id
        or acknowledgement.snapshot.artifact_id != artifact_id
        or _snapshot_digest(acknowledgement.snapshot) != snapshot_digest
    ):
        raise ValueError("invalid acknowledgement identity")
    return acknowledgement


@dataclass(frozen=True)
class ReleaseResult:
    """One referenced CAS hash considered; metadata remains present in every state."""

    sha256: str
    size: int | None
    status: str
    reason: str = ""


@dataclass(frozen=True)
class ReleasePage:
    """A bounded lexicographic hash page; after a complete sweep restart without a cursor."""

    entries: tuple[ReleaseResult, ...]
    next_after: str | None


class ArtifactRetention:
    """Uses the worker's FULL-synchronous store and exclusive state lease.

    Online callers only record acknowledgements. Physical release is offline: callers
    must hold StateLock, as the production CLI does, with no readers or writers alive.
    """

    def __init__(self, db: sqlite3.Connection, *, clock: Clock = SYSTEM_CLOCK) -> None:
        self.db = db
        self.clock = clock

    def original(self, artifact_id: str) -> ArtifactSnapshot:
        """Read only bounded, valid immutable metadata for acknowledgement scope checks."""
        row = self.db.execute(
            "SELECT typeof(record),length(CAST(record AS BLOB)),"
            "substr(CAST(record AS BLOB),1,?) FROM artifacts WHERE artifact_id=?",
            (MAX_RETENTION_ACK_BYTES + 1, artifact_id),
        ).fetchone()
        if row is None:
            raise WorkerError(ErrorKind.NOT_FOUND, "no artifact")
        try:
            if row[0] != "text" or not 0 < row[1] <= MAX_RETENTION_ACK_BYTES:
                raise ValueError("invalid metadata size")
            snapshot = _snapshot(row[2])
            if snapshot.artifact_id != artifact_id:
                raise ValueError("invalid metadata identity")
        except (ValueError, TypeError, RecursionError):
            raise WorkerError(ErrorKind.CONFLICT, "invalid original artifact metadata") from None
        return snapshot

    def acknowledge(
        self, acknowledgement: ArtifactRetentionAcknowledgement
    ) -> ArtifactRetentionAcknowledgement:
        """Record an exact snapshot acknowledgement, or accept an identical retry."""
        encoded = acknowledgement.model_dump_json()
        if len(encoded.encode()) > MAX_RETENTION_ACK_BYTES:
            raise WorkerError(ErrorKind.INVALID_REQUEST, "retention acknowledgement exceeds limit")
        snapshot = acknowledgement.snapshot
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            if self.original(snapshot.artifact_id) != snapshot:
                raise WorkerError(ErrorKind.CONFLICT, "retention snapshot differs from original")
            existing = self.db.execute(
                f"SELECT {_ACK_COLUMNS} FROM artifact_retention_acknowledgements"  # noqa: S608 - fixed bounded columns
                " WHERE acknowledgement_id=?",
                (acknowledgement.acknowledgement_id,),
            ).fetchone()
            if existing is not None:
                try:
                    recorded = _recorded_ack(existing)
                except (ValueError, TypeError, RecursionError):
                    raise WorkerError(
                        ErrorKind.CONFLICT, "invalid recorded acknowledgement"
                    ) from None
                if recorded != acknowledgement:
                    raise WorkerError(ErrorKind.CONFLICT, "acknowledgement identity already used")
                return acknowledgement
            self.db.execute(
                "INSERT INTO artifact_retention_acknowledgements VALUES(?,?,?,?,?)",
                (
                    acknowledgement.acknowledgement_id,
                    snapshot.artifact_id,
                    _snapshot_digest(snapshot),
                    encoded,
                    self.clock.now().isoformat(),
                ),
            )
        return acknowledgement

    def _eligible(self, sha256: str) -> tuple[int | None, str]:
        rows = self.db.execute(
            "SELECT typeof(artifact_id),length(CAST(artifact_id AS BLOB)),"  # noqa: S608 - fixed indexed expression
            "substr(CAST(artifact_id AS BLOB),1,129),substr(CAST(record AS BLOB),1,?)"
            f" FROM artifacts WHERE ({ARTIFACT_HASH})=? LIMIT ?",
            (MAX_RETENTION_ACK_BYTES + 1, sha256, MAX_HASH_REFERENCES + 1),
        ).fetchall()
        if not rows or len(rows) > MAX_HASH_REFERENCES:
            return None, "reference count exceeds limit or references missing"
        size: int | None = None
        for kind, length, key, record in rows:
            try:
                if kind != "text" or not 0 < length <= 128:
                    return None, "invalid reference identity"
                artifact_id = key.decode("utf-8")
                snapshot = _snapshot(record)
                if snapshot.artifact_id != artifact_id or snapshot.sha256 != sha256:
                    return None, "invalid reference identity"
                if snapshot.media_type == _INSTALLER or artifact_id.startswith("art-inst-"):
                    return None, "installer reference retains bytes"
                if size is not None and snapshot.size != size:
                    return None, "reference sizes disagree"
                size = snapshot.size
                ack = self.db.execute(
                    f"SELECT {_ACK_COLUMNS} FROM artifact_retention_acknowledgements"  # noqa: S608 - fixed bounded columns
                    " WHERE artifact_id=? AND snapshot_digest=? LIMIT 1",
                    (artifact_id, _snapshot_digest(snapshot)),
                ).fetchone()
                if ack is None:
                    return size, "reference has no exact acknowledgement"
                if _recorded_ack(ack).snapshot != snapshot:
                    return None, "invalid acknowledgement snapshot"
            except (ValueError, TypeError, RecursionError):
                return None, "invalid reference or acknowledgement"
        return size, ""

    def release_page(
        self, blobs: Blobs, *, apply: bool = False, limit: int = 10, after: str | None = None
    ) -> ReleasePage:
        """Recheck every reference before releasing at most ``limit`` referenced hashes.

        An unsettled intent is reused after a crash. A settled intent never authorizes
        deleting later reinstalled bytes: every invocation recomputes eligibility.
        """
        if type(apply) is not bool or type(limit) is not int or not 1 <= limit <= MAX_RELEASE_PAGE:
            raise ValueError("invalid release page bounds")
        if after is not None and re.fullmatch(r"[0-9a-f]{64}", after) is None:
            raise ValueError("invalid release cursor")
        unknown = self.db.execute(
            f"SELECT 1 FROM artifacts WHERE ({ARTIFACT_HASH}) IS NULL LIMIT 1"  # noqa: S608 - fixed indexed expression
        ).fetchone()
        hashes = self.db.execute(
            f"SELECT DISTINCT ({ARTIFACT_HASH}) AS hash FROM artifacts"  # noqa: S608 - fixed indexed expression
            f" WHERE ({ARTIFACT_HASH})>? ORDER BY hash LIMIT ?",
            (after or "", limit + 1),
        ).fetchall()
        results: list[ReleaseResult] = []
        for (sha256,) in hashes[:limit]:
            size, reason = (
                self._eligible(sha256) if unknown is None else (None, "unknown reference hash")
            )
            if reason or size is None:
                results.append(ReleaseResult(sha256, size, "kept", reason))
                continue
            results.append(self._release(blobs, sha256, size, apply=apply))
        return ReleasePage(tuple(results), hashes[limit - 1][0] if len(hashes) > limit else None)

    def _release(self, blobs: Blobs, sha256: str, size: int, *, apply: bool) -> ReleaseResult:
        path = blobs.path(sha256)
        pending = self.db.execute(
            "SELECT release_id,size FROM artifact_release_intents"
            " WHERE sha256=? AND settled_at IS NULL",
            (sha256,),
        ).fetchone()
        if pending is not None and pending[1] != size:
            return ReleaseResult(sha256, size, "kept", "pending intent size disagrees")
        try:
            info = path.lstat()
        except FileNotFoundError:
            info = None
        if info is not None and (not stat.S_ISREG(info.st_mode) or info.st_size != size):
            return ReleaseResult(sha256, size, "kept", "blob is not a regular file of exact size")
        if info is None and pending is None:
            return ReleaseResult(sha256, size, "missing")
        if not apply:
            return ReleaseResult(sha256, size, "would_release" if info else "would_reconcile")
        if pending is None:
            cursor = self.db.execute(
                "INSERT INTO artifact_release_intents(sha256,size,intended_at) VALUES(?,?,?)",
                (sha256, size, self.clock.now().isoformat()),
            )
            pending = (cursor.lastrowid, size)
        if info is not None:
            path.unlink()
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        self.db.execute(
            "UPDATE artifact_release_intents SET settled_at=? WHERE release_id=?",
            (self.clock.now().isoformat(), pending[0]),
        )
        return ReleaseResult(sha256, size, "released" if info else "reconciled")
