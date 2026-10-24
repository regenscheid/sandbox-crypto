# SPDX-License-Identifier: MIT
"""The worker store's records of job sandboxes (D93): one row a job, and its manifest.

A job's row is written when the job is accepted, before its sandbox is created, so a
retry with the same operation finds the job and never makes a second sandbox (JS-7),
and a sandbox left by a crash is known to be the job's. The sandbox's ownership is the
environment's record (``environments.EnvironmentRecords``): the row names the job
environment, and only that record's sandbox is ever deleted (JS-26).

A job's result manifest (JS-16) is stored once. Triggers refuse to change or delete it,
so a later bug or a second writer cannot rewrite what a job returned.
"""

from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from typing import Final

from sandbox_science.contracts import ErrorKind, JobResultManifest
from sandbox_science.contracts.canonical import canonical_json
from sandbox_science.contracts.worker import CollectionStatus, JobSandboxPhase

from .errors import WorkerError

ACTIVE: Final[frozenset[JobSandboxPhase]] = frozenset(
    {"creating", "preparing", "running", "collecting"}
)
"""Phases that hold a sandbox's CPU and memory, and count against the limits (JS-9)."""
_PHASES: Final = "('creating','preparing','running','collecting','released','retained')"


def create_schema(db: sqlite3.Connection) -> None:
    """Append-only worker migration: job sandbox rows and their immutable manifests."""
    db.execute(
        f"""CREATE TABLE job_sandboxes (
    execution_id TEXT PRIMARY KEY CHECK (length(execution_id) BETWEEN 1 AND 128),
    project_id TEXT NOT NULL CHECK (length(project_id) BETWEEN 1 AND 128),
    session_environment TEXT NOT NULL CHECK (length(session_environment) BETWEEN 1 AND 128),
    environment_id TEXT NOT NULL UNIQUE CHECK (length(environment_id) BETWEEN 1 AND 128),
    profile TEXT NOT NULL CHECK (length(profile) BETWEEN 1 AND 64),
    phase TEXT NOT NULL CHECK (phase IN {_PHASES}),
    collection TEXT NOT NULL DEFAULT 'pending'
        CHECK (collection IN ('pending','complete','incomplete')),
    retained_until TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
) STRICT"""
    )
    db.execute("CREATE INDEX job_sandboxes_project ON job_sandboxes(project_id, phase)")
    db.execute(
        """CREATE TABLE job_manifests (
    execution_id TEXT PRIMARY KEY REFERENCES job_sandboxes(execution_id),
    sha256 TEXT NOT NULL CHECK (length(sha256)=64 AND sha256 NOT GLOB '*[^0-9a-f]*'),
    manifest TEXT NOT NULL CHECK (length(CAST(manifest AS BLOB)) BETWEEN 2 AND 4194304),
    recorded_at TEXT NOT NULL
) STRICT"""
    )
    for action in ("UPDATE", "DELETE"):
        db.execute(
            f"CREATE TRIGGER job_manifests_immutable_{action.lower()} BEFORE {action}"
            " ON job_manifests BEGIN SELECT RAISE(ABORT, 'job manifests are immutable'); END"
        )


@dataclass(frozen=True, slots=True)
class JobSandboxRecord:
    """A job sandbox's row."""

    execution_id: str
    project_id: str
    session_environment: str
    environment_id: str
    profile: str
    phase: JobSandboxPhase
    collection: CollectionStatus
    retained_until: datetime | None
    created_at: datetime
    updated_at: datetime


def manifest_digest(manifest: JobResultManifest) -> str:
    """The SHA-256 of the manifest's canonical JSON, which ``job_status`` returns."""
    return hashlib.sha256(canonical_json(manifest.model_dump(mode="json"))).hexdigest()


_SELECT: Final = (
    "SELECT execution_id,project_id,session_environment,environment_id,profile,phase,"
    "collection,retained_until,created_at,updated_at FROM job_sandboxes"
)
"""Every column of a row, in ``JobSandboxRecord``'s order."""


class JobSandboxRecords:
    """Job sandbox rows and manifests in the worker's store, on its writer connection."""

    def __init__(self, db: sqlite3.Connection) -> None:
        self.db = db

    def insert(self, record: JobSandboxRecord) -> None:
        try:
            self.db.execute(
                "INSERT INTO job_sandboxes VALUES (?,?,?,?,?,?,?,?,?,?)",
                _row(record),
            )
        except sqlite3.IntegrityError as exc:
            raise WorkerError(ErrorKind.CONFLICT, "the job sandbox is recorded already") from exc

    def update(
        self,
        execution_id: str,
        *,
        now: datetime,
        phase: JobSandboxPhase | None = None,
        collection: CollectionStatus | None = None,
        retained_until: datetime | None = None,
    ) -> JobSandboxRecord:
        """Change a row's phase, collection or retention deadline; return the row."""
        found = self.get(execution_id)
        if found is None:
            raise WorkerError(ErrorKind.NOT_FOUND, f"no job sandbox for {execution_id}")
        # A deadline belongs to retention only: another phase clears it.
        until = found.retained_until if phase is None else None
        if phase == "retained":
            until = retained_until
        self.db.execute(
            "UPDATE job_sandboxes SET phase=?, collection=?, retained_until=?, updated_at=?"
            " WHERE execution_id=?",
            (
                phase or found.phase,
                collection or found.collection,
                _time(until),
                now.isoformat(),
                execution_id,
            ),
        )
        record = self.get(execution_id)
        if record is None:
            raise WorkerError(ErrorKind.INTERNAL, f"the job sandbox of {execution_id} went")
        return record

    def get(self, execution_id: str) -> JobSandboxRecord | None:
        row = self.db.execute(_SELECT + " WHERE execution_id=?", (execution_id,)).fetchone()
        return None if row is None else _record(row)

    def by_environment(self, environment_id: str) -> JobSandboxRecord | None:
        row = self.db.execute(_SELECT + " WHERE environment_id=?", (environment_id,)).fetchone()
        return None if row is None else _record(row)

    def listed(self, *, phases: frozenset[JobSandboxPhase] | None = None) -> list[JobSandboxRecord]:
        """Rows in ``phases`` (every row without), oldest first."""
        rows = self.db.execute(_SELECT + " ORDER BY created_at, execution_id").fetchall()
        records = [_record(row) for row in rows]
        return [record for record in records if phases is None or record.phase in phases]

    def save_manifest(self, manifest: JobResultManifest, now: datetime) -> str:
        """Store the job's manifest once; return its digest. A second, different one is
        refused, and the same one again returns the stored digest."""
        digest = manifest_digest(manifest)
        text = manifest.model_dump_json()
        existing = self.db.execute(
            "SELECT sha256 FROM job_manifests WHERE execution_id=?", (manifest.job_id,)
        ).fetchone()
        if existing is not None:
            if existing[0] != digest:
                raise WorkerError(ErrorKind.CONFLICT, "the job's manifest is recorded already")
            return digest
        self.db.execute(
            "INSERT INTO job_manifests (execution_id,sha256,manifest,recorded_at) VALUES (?,?,?,?)",
            (manifest.job_id, digest, text, now.isoformat()),
        )
        return digest

    def manifest(self, execution_id: str) -> tuple[JobResultManifest, str] | None:
        row = self.db.execute(
            "SELECT manifest, sha256 FROM job_manifests WHERE execution_id=?", (execution_id,)
        ).fetchone()
        if row is None:
            return None
        manifest = JobResultManifest.model_validate_json(str(row[0]))
        if manifest_digest(manifest) != row[1]:
            raise WorkerError(ErrorKind.INTERNAL, f"the manifest of {execution_id} is corrupt")
        return manifest, str(row[1])


def _time(value: datetime | None) -> str | None:
    return None if value is None else value.isoformat()


def _row(record: JobSandboxRecord) -> tuple[object, ...]:
    return (
        record.execution_id,
        record.project_id,
        record.session_environment,
        record.environment_id,
        record.profile,
        record.phase,
        record.collection,
        _time(record.retained_until),
        record.created_at.isoformat(),
        record.updated_at.isoformat(),
    )


def _record(row: tuple[object, ...]) -> JobSandboxRecord:
    (
        execution_id,
        project_id,
        session_environment,
        environment_id,
        profile,
        phase,
        collection,
        retained_until,
        created_at,
        updated_at,
    ) = row
    return JobSandboxRecord(
        execution_id=str(execution_id),
        project_id=str(project_id),
        session_environment=str(session_environment),
        environment_id=str(environment_id),
        profile=str(profile),
        phase=_phase(phase),
        collection=_collection(collection),
        retained_until=None
        if retained_until is None
        else datetime.fromisoformat(str(retained_until)),
        created_at=datetime.fromisoformat(str(created_at)),
        updated_at=datetime.fromisoformat(str(updated_at)),
    )


def _phase(value: object) -> JobSandboxPhase:
    for phase in ("creating", "preparing", "running", "collecting", "released", "retained"):
        if value == phase:
            return phase
    raise WorkerError(ErrorKind.INTERNAL, "a job sandbox row has an unknown phase")


def _collection(value: object) -> CollectionStatus:
    for status in ("pending", "complete", "incomplete"):
        if value == status:
            return status
    raise WorkerError(ErrorKind.INTERNAL, "a job sandbox row has an unknown collection status")
