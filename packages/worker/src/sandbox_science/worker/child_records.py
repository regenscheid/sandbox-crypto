# SPDX-License-Identifier: MIT
"""The worker store's records of child environments (docs/specs/child-environments.md,
CE-11): one row a child environment, its recorded steps and its hand-back's manifest.

A row is written when the child environment is admitted, before its environment record
and its sandbox exist, so that a crash leaves nothing the worker cannot account for:
the environment's record (``environments.EnvironmentRecords``) owns the sandbox, and
only that record's sandbox is ever deleted. A delegation has one child environment: a
second admission for it finds the first row (``UNIQUE``).

Steps are recorded once, and the first record holds: what preparation found, the
hand-back's request, when its copying began, each file it copied or left out, and a
session close's early release. A hand-back a restart cut short goes on from them and
copies nothing twice. Its manifest is stored once; triggers refuse to change it.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime
from typing import Final, cast

from sandbox_science.contracts import ErrorKind
from sandbox_science.contracts.child_environments import (
    ChildFailure,
    ChildHandBack,
    ChildPhase,
)

from .errors import WorkerError

ACTIVE: Final[frozenset[ChildPhase]] = frozenset({"creating", "preparing", "ready", "collecting"})
"""Phases that hold a sandbox's CPU and memory, and count against the limits (CE-9)."""
PHASES: Final[tuple[ChildPhase, ...]] = (
    "creating",
    "preparing",
    "ready",
    "collecting",
    "released",
    "retained",
)
FAILURES: Final[tuple[ChildFailure, ...]] = (
    "environment_unavailable",
    "isolation_insufficient",
    "package_replay_failed",
    "environment_mismatch",
    "inputs_too_large",
    "staging_failed",
    "worker_restarted",
    "wall_time",
)
_PHASES: Final = "(" + ",".join(f"'{phase}'" for phase in PHASES) + ")"
_FAILURES: Final = "(" + ",".join(f"'{failure}'" for failure in FAILURES) + ")"


def create_schema(db: sqlite3.Connection) -> None:
    """Append-only worker migration: child environment rows, steps and manifests."""
    db.execute(
        f"""CREATE TABLE child_environments (
    environment_id TEXT PRIMARY KEY CHECK (length(environment_id) BETWEEN 1 AND 128),
    delegation_id TEXT NOT NULL CHECK (length(delegation_id) BETWEEN 1 AND 128),
    project_id TEXT NOT NULL CHECK (length(project_id) BETWEEN 1 AND 128),
    session_id TEXT NOT NULL CHECK (length(session_id) BETWEEN 1 AND 128),
    session_environment TEXT NOT NULL CHECK (length(session_environment) BETWEEN 1 AND 128),
    profile TEXT NOT NULL CHECK (length(profile) BETWEEN 1 AND 64),
    phase TEXT NOT NULL CHECK (phase IN {_PHASES}),
    failure TEXT CHECK (failure IS NULL OR failure IN {_FAILURES}),
    message TEXT CHECK (message IS NULL OR length(CAST(message AS BLOB)) BETWEEN 1 AND 1000),
    ready_by TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    ready_at TEXT,
    retained_until TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (project_id, delegation_id)
) STRICT"""
    )
    db.execute(
        "CREATE INDEX child_environments_session"
        " ON child_environments(project_id, session_id, phase)"
    )
    db.execute(
        """CREATE TABLE child_progress (
    environment_id TEXT NOT NULL REFERENCES child_environments(environment_id),
    step TEXT NOT NULL CHECK (length(step) BETWEEN 1 AND 32 AND step NOT GLOB '*[^a-z0-9-]*'),
    record TEXT NOT NULL CHECK (length(CAST(record AS BLOB)) BETWEEN 2 AND 4194304),
    recorded_at TEXT NOT NULL,
    PRIMARY KEY (environment_id, step)
) STRICT"""
    )
    db.execute(
        "CREATE TRIGGER child_progress_immutable BEFORE UPDATE ON child_progress"
        " BEGIN SELECT RAISE(ABORT, 'child environment steps are recorded once'); END"
    )
    db.execute(
        """CREATE TABLE child_hand_backs (
    environment_id TEXT PRIMARY KEY REFERENCES child_environments(environment_id),
    manifest TEXT NOT NULL CHECK (length(CAST(manifest AS BLOB)) BETWEEN 2 AND 4194304),
    recorded_at TEXT NOT NULL
) STRICT"""
    )
    for action in ("UPDATE", "DELETE"):
        db.execute(
            f"CREATE TRIGGER child_hand_backs_immutable_{action.lower()} BEFORE {action}"
            " ON child_hand_backs BEGIN SELECT RAISE(ABORT, 'hand-back manifests are"
            " immutable'); END"
        )


@dataclass(frozen=True, slots=True)
class ChildRecord:
    """A child environment's row."""

    environment_id: str
    delegation_id: str
    project_id: str
    session_id: str
    session_environment: str
    profile: str
    phase: ChildPhase
    failure: ChildFailure | None
    message: str | None
    ready_by: datetime
    expires_at: datetime
    ready_at: datetime | None
    retained_until: datetime | None
    created_at: datetime
    updated_at: datetime


_COLUMNS: Final = (
    "environment_id,delegation_id,project_id,session_id,session_environment,profile,phase,"
    "failure,message,ready_by,expires_at,ready_at,retained_until,created_at,updated_at"
)
_SELECT: Final = f"SELECT {_COLUMNS} FROM child_environments"  # noqa: S608 - fixed columns
_UNSET: Final = object()


class ChildRecords:
    """Child environment rows, steps and manifests in the worker's store, on its writer
    connection. Each method is one statement or reads only, on the event loop's thread."""

    def __init__(self, db: sqlite3.Connection) -> None:
        self.db = db

    def insert(self, record: ChildRecord) -> None:
        try:
            self.db.execute(
                f"INSERT INTO child_environments ({_COLUMNS}) VALUES"  # noqa: S608 - fixed columns
                " (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                _row(record),
            )
        except sqlite3.IntegrityError as exc:
            raise WorkerError(
                ErrorKind.CONFLICT, "the delegation has a child environment already"
            ) from exc

    def update(
        self,
        environment_id: str,
        *,
        now: datetime,
        phase: ChildPhase | None = None,
        failure: ChildFailure | object | None = _UNSET,
        message: str | object | None = _UNSET,
        ready_at: datetime | None = None,
        retained_until: datetime | None = None,
    ) -> ChildRecord:
        """Change a row; a retention deadline belongs to ``retained`` only."""
        found = self.get(environment_id)
        if found is None:
            raise WorkerError(ErrorKind.NOT_FOUND, f"no child environment {environment_id!r}")
        until = found.retained_until if phase is None else None
        if phase == "retained":
            until = retained_until
        self.db.execute(
            "UPDATE child_environments SET phase=?, failure=?, message=?, ready_at=?,"
            " retained_until=?, updated_at=? WHERE environment_id=?",
            (
                phase or found.phase,
                found.failure if failure is _UNSET else failure,
                (found.message if message is _UNSET else cast(str | None, message) or None),
                _time(ready_at or found.ready_at),
                _time(until),
                now.isoformat(),
                environment_id,
            ),
        )
        record = self.get(environment_id)
        if record is None:
            raise WorkerError(ErrorKind.INTERNAL, f"the row of {environment_id} went")
        return record

    def get(self, environment_id: str) -> ChildRecord | None:
        row = self.db.execute(_SELECT + " WHERE environment_id=?", (environment_id,)).fetchone()
        return None if row is None else _record(row)

    def for_delegation(self, project_id: str, delegation_id: str) -> ChildRecord | None:
        row = self.db.execute(
            _SELECT + " WHERE project_id=? AND delegation_id=?", (project_id, delegation_id)
        ).fetchone()
        return None if row is None else _record(row)

    def listed(
        self,
        *,
        phases: frozenset[ChildPhase] | None = None,
        project_id: str | None = None,
        session_id: str | None = None,
    ) -> list[ChildRecord]:
        """Rows in ``phases`` (every row without), of a project and session, oldest first."""
        rows = self.db.execute(
            _SELECT + " WHERE (? IS NULL OR project_id=?) AND (? IS NULL OR session_id=?)"
            " ORDER BY created_at, environment_id LIMIT 1000",
            (project_id, project_id, session_id, session_id),
        ).fetchall()
        records = [_record(row) for row in rows]
        return [record for record in records if phases is None or record.phase in phases]

    def record_step(self, environment_id: str, step: str, record: str, now: datetime) -> str:
        """Record one step once; return its record, the first one when it was recorded."""
        self.db.execute(
            "INSERT INTO child_progress (environment_id,step,record,recorded_at) VALUES"
            " (?,?,?,?) ON CONFLICT (environment_id, step) DO NOTHING",
            (environment_id, step, record, now.isoformat()),
        )
        found = self.steps(environment_id).get(step)
        if found is None:
            raise WorkerError(ErrorKind.INTERNAL, f"step {step} of {environment_id} was lost")
        return found

    def steps(self, environment_id: str) -> dict[str, str]:
        rows = self.db.execute(
            "SELECT step, record FROM child_progress WHERE environment_id=?", (environment_id,)
        ).fetchall()
        return {str(step): str(record) for step, record in rows}

    def save_hand_back(self, environment_id: str, manifest: ChildHandBack, now: datetime) -> None:
        """Store the hand-back's manifest once; a different second one is refused."""
        text = manifest.model_dump_json()
        existing = self.hand_back(environment_id)
        if existing is not None:
            if existing != manifest:
                raise WorkerError(ErrorKind.CONFLICT, "the hand-back is recorded already")
            return
        self.db.execute(
            "INSERT INTO child_hand_backs (environment_id,manifest,recorded_at) VALUES (?,?,?)",
            (environment_id, text, now.isoformat()),
        )

    def hand_back(self, environment_id: str) -> ChildHandBack | None:
        row = self.db.execute(
            "SELECT manifest FROM child_hand_backs WHERE environment_id=?", (environment_id,)
        ).fetchone()
        return None if row is None else ChildHandBack.model_validate_json(str(row[0]))


def _time(value: datetime | None) -> str | None:
    return None if value is None else value.isoformat()


def _row(record: ChildRecord) -> tuple[object, ...]:
    return (
        record.environment_id,
        record.delegation_id,
        record.project_id,
        record.session_id,
        record.session_environment,
        record.profile,
        record.phase,
        record.failure,
        record.message,
        record.ready_by.isoformat(),
        record.expires_at.isoformat(),
        _time(record.ready_at),
        _time(record.retained_until),
        record.created_at.isoformat(),
        record.updated_at.isoformat(),
    )


def _when(value: object) -> datetime | None:
    return None if value is None else datetime.fromisoformat(str(value))


def _phase(value: object) -> ChildPhase:
    for phase in PHASES:
        if value == phase:
            return phase
    raise WorkerError(ErrorKind.INTERNAL, "a child environment row has an unknown phase")


def _failure(value: object) -> ChildFailure | None:
    for failure in FAILURES:
        if value == failure:
            return failure
    if value is not None:
        raise WorkerError(ErrorKind.INTERNAL, "a child environment row has an unknown failure")
    return None


def _record(row: tuple[object, ...]) -> ChildRecord:
    phase, failure = _phase(row[6]), _failure(row[7])
    return ChildRecord(
        environment_id=str(row[0]),
        delegation_id=str(row[1]),
        project_id=str(row[2]),
        session_id=str(row[3]),
        session_environment=str(row[4]),
        profile=str(row[5]),
        phase=phase,
        failure=failure,
        message=None if row[8] is None else str(row[8]),
        ready_by=datetime.fromisoformat(str(row[9])),
        expires_at=datetime.fromisoformat(str(row[10])),
        ready_at=_when(row[11]),
        retained_until=_when(row[12]),
        created_at=datetime.fromisoformat(str(row[13])),
        updated_at=datetime.fromisoformat(str(row[14])),
    )
