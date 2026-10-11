# SPDX-License-Identifier: MIT
"""Durable worker state in SQLite: executions, their events, kernels and artifacts."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from pydantic import TypeAdapter

from sandbox_science.contracts import TERMINAL_STATES, ArtifactRef, Event, Execution

_EVENT = TypeAdapter[Event](Event)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS executions (
    execution_id TEXT PRIMARY KEY,
    operation_id TEXT NOT NULL UNIQUE,
    request_digest TEXT NOT NULL,
    environment_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    terminal INTEGER NOT NULL,
    record TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS executions_session ON executions (environment_id, session_id);
CREATE TABLE IF NOT EXISTS events (
    execution_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    event TEXT NOT NULL,
    PRIMARY KEY (execution_id, seq)
);
CREATE TABLE IF NOT EXISTS kernels (
    environment_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    language TEXT NOT NULL,
    incarnation TEXT NOT NULL,
    base INTEGER NOT NULL,
    last INTEGER NOT NULL,
    PRIMARY KEY (environment_id, session_id, language)
);
CREATE TABLE IF NOT EXISTS artifacts (
    artifact_id TEXT PRIMARY KEY,
    record TEXT NOT NULL
);
"""


@dataclass(frozen=True, slots=True)
class KernelRecord:
    """Maps an executor's kernel generations onto generations that never go back.

    A new executor incarnation starts its kernels at generation 1 again; ``base``
    is added so that the generation the worker reports keeps growing.
    """

    incarnation: str
    base: int
    last: int


class Store:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=NORMAL")
        self._db.executescript(_SCHEMA)

    def close(self) -> None:
        self._db.close()

    # Executions.

    def find_operation(self, operation_id: str) -> tuple[str, str] | None:
        row = self._db.execute(
            "SELECT execution_id, request_digest FROM executions WHERE operation_id = ?",
            (operation_id,),
        ).fetchone()
        return (str(row[0]), str(row[1])) if row else None

    def insert_execution(self, execution: Execution, digest: str) -> None:
        self._db.execute(
            "INSERT INTO executions VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                execution.execution_id,
                execution.operation_id,
                digest,
                execution.context.environment_id,
                execution.context.session_id,
                int(execution.state in TERMINAL_STATES),
                execution.model_dump_json(),
            ),
        )

    def update_execution(self, execution: Execution) -> None:
        self._db.execute(
            "UPDATE executions SET record = ?, terminal = ? WHERE execution_id = ?",
            (
                execution.model_dump_json(),
                int(execution.state in TERMINAL_STATES),
                execution.execution_id,
            ),
        )

    def execution(self, execution_id: str) -> Execution | None:
        row = self._db.execute(
            "SELECT record FROM executions WHERE execution_id = ?", (execution_id,)
        ).fetchone()
        return Execution.model_validate_json(row[0]) if row else None

    def unfinished(self) -> list[Execution]:
        rows = self._db.execute("SELECT record FROM executions WHERE terminal = 0").fetchall()
        return [Execution.model_validate_json(row[0]) for row in rows]

    def session_executions(self, environment_id: str, session_id: str) -> list[Execution]:
        rows = self._db.execute(
            "SELECT record FROM executions WHERE environment_id = ? AND session_id = ?",
            (environment_id, session_id),
        ).fetchall()
        return [Execution.model_validate_json(row[0]) for row in rows]

    def environment_executions(self, environment_id: str) -> list[Execution]:
        rows = self._db.execute(
            "SELECT record FROM executions WHERE environment_id = ?", (environment_id,)
        ).fetchall()
        return [Execution.model_validate_json(row[0]) for row in rows]

    # Events.

    def add_event(self, event: Event) -> None:
        self._db.execute(
            "INSERT INTO events VALUES (?, ?, ?)",
            (event.execution_id, event.seq, _EVENT.dump_json(event).decode()),
        )

    def events_after(self, execution_id: str, after: int) -> list[Event]:
        rows = self._db.execute(
            "SELECT event FROM events WHERE execution_id = ? AND seq > ? ORDER BY seq",
            (execution_id, after),
        ).fetchall()
        return [_EVENT.validate_json(row[0]) for row in rows]

    # Kernels.

    def kernel(self, environment_id: str, session_id: str, language: str) -> KernelRecord | None:
        row = self._db.execute(
            "SELECT incarnation, base, last FROM kernels"
            " WHERE environment_id = ? AND session_id = ? AND language = ?",
            (environment_id, session_id, language),
        ).fetchone()
        return KernelRecord(str(row[0]), int(row[1]), int(row[2])) if row else None

    def save_kernel(
        self, environment_id: str, session_id: str, language: str, record: KernelRecord
    ) -> None:
        self._db.execute(
            "INSERT OR REPLACE INTO kernels VALUES (?, ?, ?, ?, ?, ?)",
            (environment_id, session_id, language, record.incarnation, record.base, record.last),
        )

    # Artifacts.

    def insert_artifact(self, artifact: ArtifactRef) -> None:
        self._db.execute(
            "INSERT INTO artifacts VALUES (?, ?)",
            (artifact.artifact_id, artifact.model_dump_json()),
        )

    def artifact(self, artifact_id: str) -> ArtifactRef | None:
        row = self._db.execute(
            "SELECT record FROM artifacts WHERE artifact_id = ?", (artifact_id,)
        ).fetchone()
        return ArtifactRef.model_validate_json(row[0]) if row else None
