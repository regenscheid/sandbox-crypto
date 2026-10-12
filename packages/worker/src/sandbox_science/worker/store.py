# SPDX-License-Identifier: MIT
"""Durable worker state in SQLite: executions, their events, kernels, environment revisions,
artifacts and provisioning operations."""

from __future__ import annotations

import contextlib
import logging
import sqlite3
from collections.abc import Generator, Sequence
from dataclasses import dataclass
from pathlib import Path

from pydantic import TypeAdapter, ValidationError

from sandbox_science.contracts import (
    TERMINAL_STATES,
    ArtifactRef,
    EnvironmentRevision,
    Event,
    Execution,
    ExecutionContext,
    ProvisionOperation,
    ProvisionState,
)

_log = logging.getLogger(__name__)
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
CREATE TABLE IF NOT EXISTS environment_revisions (
    environment_id TEXT PRIMARY KEY,
    revision INTEGER NOT NULL,
    fingerprint TEXT NOT NULL,
    changed_by TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS revision_entries (
    environment_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    recorded REAL NOT NULL,
    lockfile TEXT,
    lockfile_size INTEGER NOT NULL,
    record TEXT NOT NULL,
    PRIMARY KEY (environment_id, revision)
);
CREATE INDEX IF NOT EXISTS revision_entries_lockfile ON revision_entries (lockfile);
CREATE TABLE IF NOT EXISTS artifacts (
    artifact_id TEXT PRIMARY KEY,
    record TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS provisions (
    provision_id TEXT PRIMARY KEY,
    operation_key TEXT NOT NULL UNIQUE,
    request_digest TEXT NOT NULL,
    environment_id TEXT NOT NULL,
    finished INTEGER NOT NULL,
    submitted REAL NOT NULL,
    record TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS provisions_environment ON provisions (environment_id, finished);
CREATE TABLE IF NOT EXISTS executor_endpoints (
    environment_id TEXT PRIMARY KEY,
    incarnation TEXT NOT NULL,
    sandbox TEXT NOT NULL DEFAULT '',
    sandbox_id TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS executor_recycles (
    environment_id TEXT PRIMARY KEY,
    incarnation TEXT NOT NULL,
    sandbox TEXT NOT NULL DEFAULT '',
    sandbox_id TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS execution_recovery (
    execution_id TEXT PRIMARY KEY,
    request TEXT NOT NULL,
    incarnation TEXT NOT NULL DEFAULT '',
    source_seq INTEGER NOT NULL DEFAULT 0,
    cancelled INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS package_status (
    environment_id TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    provision_id TEXT NOT NULL,
    revision INTEGER
);
"""


@dataclass(frozen=True, slots=True)
class RevisionRecord:
    """An environment's revision and the fingerprint of its installed packages (W3).

    ``changed_by`` names the attempt whose result first showed the change, or says
    that it happened between executors.
    """

    revision: int
    fingerprint: str
    changed_by: str


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
        for table in ("executor_endpoints", "executor_recycles"):
            columns = {str(row[1]) for row in self._db.execute(f"PRAGMA table_info({table})")}
            for name in ("sandbox", "sandbox_id"):
                if name not in columns:
                    self._db.execute(
                        f"ALTER TABLE {table} ADD COLUMN {name} TEXT NOT NULL DEFAULT ''"
                    )

    def close(self) -> None:
        self._db.close()

    @contextlib.contextmanager
    def _transaction(self) -> Generator[None, None, None]:
        self._db.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self._db.execute("ROLLBACK")
            raise
        self._db.execute("COMMIT")

    # Executions.

    def find_operation(self, project_id: str, operation_id: str) -> tuple[str, str] | None:
        """The execution and request digest of a project's operation.

        Operations are keyed by project, so that no project meets, or takes, another's
        operation IDs. Records stored before then carry the bare operation ID.
        """
        rows = self._db.execute(
            "SELECT operation_id, execution_id, request_digest, record FROM executions"
            " WHERE operation_id IN (?, ?)",
            (_operation_key(project_id, operation_id), operation_id),
        ).fetchall()
        for key, execution_id, digest, record in rows:
            if key == operation_id:
                execution = _readable(str(execution_id), record)
                if execution is None or execution.context.project_id != project_id:
                    continue
            return str(execution_id), str(digest)
        return None

    def insert_execution(self, execution: Execution, digest: str) -> None:
        self._db.execute(
            "INSERT INTO executions VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                execution.execution_id,
                _operation_key(execution.context.project_id, execution.operation_id),
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

    def unfinished(self) -> list[tuple[str, Execution | None]]:
        """Executions not yet terminal, with ``None`` for a record that no longer validates."""
        rows = self._db.execute(
            "SELECT execution_id, record FROM executions WHERE terminal = 0"
        ).fetchall()
        return [(str(row[0]), _readable(str(row[0]), row[1])) for row in rows]

    def mark_terminal(self, execution_id: str) -> None:
        """Stop reporting an unreadable record as unfinished, so it is not retried forever."""
        self._db.execute(
            "UPDATE executions SET terminal = 1 WHERE execution_id = ?", (execution_id,)
        )

    def session_executions(self, environment_id: str, session_id: str) -> list[Execution]:
        """The session's readable executions; one unreadable record does not hide the rest."""
        rows = self._db.execute(
            "SELECT execution_id, record FROM executions"
            " WHERE environment_id = ? AND session_id = ?",
            (environment_id, session_id),
        ).fetchall()
        return [found for row in rows if (found := _readable(str(row[0]), row[1])) is not None]

    def session_jobs(
        self, context: ExecutionContext, limit: int, after: str | None = None
    ) -> list[Execution]:
        """A bounded job query; filter project before limit so other tenants reveal nothing."""
        cursor = 0
        if after is not None:
            row = self._db.execute(
                "SELECT rowid, record FROM executions WHERE execution_id = ?", (after,)
            ).fetchone()
            found = _readable(after, row[1]) if row else None
            if (
                found is None
                or found.kind != "job"
                or (
                    found.context.project_id,
                    found.context.environment_id,
                    found.context.session_id,
                )
                != (context.project_id, context.environment_id, context.session_id)
            ):
                raise LookupError("unknown session job cursor")
            cursor = int(row[0])
        rows = self._db.execute(
            "SELECT execution_id, json_remove(record, '$.result.stdout', '$.result.stderr',"
            " '$.result.outputs', '$.result.written_files', '$.result.notices', '$.result.error')"
            " FROM executions"
            " WHERE environment_id = ? AND session_id = ?"
            " AND json_extract(record, '$.context.project_id') = ?"
            " AND json_extract(record, '$.kind') = 'job' AND rowid > ? ORDER BY rowid LIMIT ?",
            (context.environment_id, context.session_id, context.project_id, cursor, limit),
        ).fetchall()
        return [found for row in rows if (found := _readable(str(row[0]), row[1])) is not None]

    def environment_executions(self, environment_id: str) -> list[Execution]:
        rows = self._db.execute(
            "SELECT execution_id, record FROM executions WHERE environment_id = ?",
            (environment_id,),
        ).fetchall()
        return [found for row in rows if (found := _readable(str(row[0]), row[1])) is not None]

    def finish(
        self, execution: Execution, events: Sequence[Event], source_seq: int | None = None
    ) -> None:
        """Store an execution's last events and its terminal record in one transaction."""
        with self._transaction():
            for event in events:
                self.add_event(event)
            self.update_execution(execution)
            if source_seq is not None:
                self.source_cursor(execution.execution_id, source_seq)

    def endpoints(self) -> tuple[str, ...]:
        return tuple(
            str(row[0]) for row in self._db.execute("SELECT environment_id FROM executor_endpoints")
        )

    def endpoint_incarnation(self, environment_id: str) -> str | None:
        row = self._db.execute(
            "SELECT incarnation FROM executor_endpoints WHERE environment_id = ?",
            (environment_id,),
        ).fetchone()
        return str(row[0]) if row else None

    def endpoint_binding(self, environment_id: str) -> tuple[str, str] | None:
        return self._executor_binding("executor_endpoints", environment_id)

    def recycle_binding(self, environment_id: str) -> tuple[str, str] | None:
        return self._executor_binding("executor_recycles", environment_id)

    def _executor_binding(self, table: str, environment_id: str) -> tuple[str, str] | None:
        query = (
            "SELECT sandbox, sandbox_id FROM executor_endpoints WHERE environment_id = ?"
            if table == "executor_endpoints"
            else "SELECT sandbox, sandbox_id FROM executor_recycles WHERE environment_id = ?"
        )
        row = self._db.execute(query, (environment_id,)).fetchone()
        return (str(row[0]), str(row[1])) if row and row[0] and row[1] else None

    def pending_recycles(self) -> tuple[str, ...]:
        return tuple(
            str(row[0]) for row in self._db.execute("SELECT environment_id FROM executor_recycles")
        )

    def recycle_needed(self, environment_id: str) -> bool:
        return (
            self._db.execute(
                "SELECT 1 FROM executor_recycles WHERE environment_id = ?", (environment_id,)
            ).fetchone()
            is not None
        )

    def require_recycle(self, environment_id: str, incarnation: str) -> None:
        binding = self.endpoint_binding(environment_id) or ("", "")
        self._db.execute(
            "INSERT OR IGNORE INTO executor_recycles VALUES (?, ?, ?, ?)",
            (environment_id, incarnation, *binding),
        )

    def finish_recycle(self, environment_id: str) -> None:
        with self._transaction():
            self._db.execute(
                "DELETE FROM executor_endpoints WHERE environment_id = ?", (environment_id,)
            )
            self._db.execute(
                "DELETE FROM executor_recycles WHERE environment_id = ?", (environment_id,)
            )

    def remember_endpoint(
        self, environment_id: str, incarnation: str, binding: tuple[str, str] | None = None
    ) -> None:
        self._db.execute(
            "INSERT INTO executor_endpoints VALUES (?, ?, ?, ?) "
            "ON CONFLICT(environment_id) DO UPDATE SET incarnation=excluded.incarnation, "
            "sandbox=excluded.sandbox, sandbox_id=excluded.sandbox_id",
            (environment_id, incarnation, *(binding or ("", ""))),
        )

    def forget_endpoint(self, environment_id: str) -> None:
        self.finish_recycle(environment_id)

    def remember_request(self, execution_id: str, request: str) -> None:
        self._db.execute(
            "INSERT INTO execution_recovery(execution_id, request) VALUES (?, ?)",
            (execution_id, request),
        )

    def recovery(self, execution_id: str) -> tuple[str, str, int, bool] | None:
        row = self._db.execute(
            "SELECT request, incarnation, source_seq, cancelled FROM execution_recovery "
            "WHERE execution_id = ?",
            (execution_id,),
        ).fetchone()
        return (str(row[0]), str(row[1]), int(row[2]), bool(row[3])) if row else None

    def dispatch_incarnation(self, execution_id: str, incarnation: str) -> None:
        self._db.execute(
            "UPDATE execution_recovery SET incarnation = ? WHERE execution_id = ?",
            (incarnation, execution_id),
        )

    def cancel_intent(self, execution_id: str) -> None:
        self._db.execute(
            "UPDATE execution_recovery SET cancelled = 1 WHERE execution_id = ?", (execution_id,)
        )

    def source_cursor(self, execution_id: str, sequence: int) -> None:
        self._db.execute(
            "UPDATE execution_recovery SET source_seq = MAX(source_seq, ?) WHERE execution_id = ?",
            (sequence, execution_id),
        )

    def commit_event(self, execution: Execution, event: Event, source_seq: int | None) -> None:
        """Worker numbering, state and source acknowledgment commit atomically."""
        with self._transaction():
            self.add_event(event)
            self.update_execution(execution)
            if source_seq is not None:
                self.source_cursor(execution.execution_id, source_seq)

    # Events.

    def add_event(self, event: Event) -> None:
        self._db.execute(
            "INSERT INTO events VALUES (?, ?, ?)",
            (event.execution_id, event.seq, _EVENT.dump_json(event).decode()),
        )

    def event_usage(self, execution_id: str) -> tuple[int, int]:
        """Recover already charged output without parsing historical stream payloads."""
        row = self._db.execute(
            "SELECT COUNT(*), COALESCE(SUM(CASE json_extract(event, '$.type') "
            "WHEN 'stream' THEN length(CAST(json_extract(event, '$.text') AS BLOB)) "
            "ELSE (SELECT SUM(length(CAST(value AS BLOB))) "
            "FROM json_each(events.event, '$.output.data')) END), 0) FROM events "
            "WHERE execution_id = ? AND json_extract(event, '$.type') IN ('stream', 'output')",
            (execution_id,),
        ).fetchone()
        return (int(row[0]), int(row[1])) if row else (0, 0)

    def last_seq(self, execution_id: str) -> int:
        """The highest stored sequence number: the record's own ``last_seq`` may lag behind it."""
        row = self._db.execute(
            "SELECT MAX(seq) FROM events WHERE execution_id = ?", (execution_id,)
        ).fetchone()
        return int(row[0]) if row and row[0] is not None else 0

    def events_after(self, execution_id: str, after: int, limit: int = -1) -> list[Event]:
        """Up to ``limit`` events after ``after`` (all of them when ``limit`` is negative)."""
        rows = self._db.execute(
            "SELECT event FROM events WHERE execution_id = ? AND seq > ? ORDER BY seq LIMIT ?",
            (execution_id, after, limit),
        ).fetchall()
        return [_EVENT.validate_json(row[0]) for row in rows]

    # Environment revisions: the current one, and a history with lockfiles.

    def revision(self, environment_id: str) -> RevisionRecord | None:
        row = self._db.execute(
            "SELECT revision, fingerprint, changed_by FROM environment_revisions"
            " WHERE environment_id = ?",
            (environment_id,),
        ).fetchone()
        return RevisionRecord(int(row[0]), str(row[1]), str(row[2])) if row else None

    def save_revision(self, environment_id: str, record: RevisionRecord) -> None:
        self._db.execute(
            "INSERT OR REPLACE INTO environment_revisions VALUES (?, ?, ?, ?)",
            (environment_id, record.revision, record.fingerprint, record.changed_by),
        )

    def add_revision(
        self, environment_id: str, record: RevisionRecord, entry: EnvironmentRevision
    ) -> None:
        """Make ``record`` the environment's revision and add it to the history, together."""
        with self._transaction():
            self.save_revision(environment_id, record)
            self.save_revision_entry(environment_id, entry)

    def save_revision_entry(self, environment_id: str, entry: EnvironmentRevision) -> None:
        lockfile = entry.lockfile
        self._db.execute(
            "INSERT OR REPLACE INTO revision_entries VALUES (?, ?, ?, ?, ?, ?)",
            (
                environment_id,
                entry.revision,
                entry.recorded_at.timestamp(),
                lockfile.sha256 if lockfile is not None else None,
                lockfile.size if lockfile is not None else 0,
                entry.model_dump_json(),
            ),
        )

    def revision_entry(self, environment_id: str, revision: int) -> EnvironmentRevision | None:
        row = self._db.execute(
            "SELECT record FROM revision_entries WHERE environment_id = ? AND revision = ?",
            (environment_id, revision),
        ).fetchone()
        return _revision(environment_id, row[0]) if row else None

    def revision_page(
        self, environment_id: str, after: int, limit: int, since: float | None = None
    ) -> list[EnvironmentRevision]:
        """Up to ``limit`` of the environment's revisions after ``after``, oldest first, and
        none recorded before ``since``; an unreadable one is skipped."""
        rows = self._db.execute(
            "SELECT record FROM revision_entries WHERE environment_id = ? AND revision > ?"
            " AND recorded >= ? ORDER BY revision LIMIT ?",
            (environment_id, after, since if since is not None else -1.0, limit),
        ).fetchall()
        return [found for row in rows if (found := _revision(environment_id, row[0])) is not None]

    def trim_revisions(self, environment_id: str, keep: int, spare: int | None = None) -> list[str]:
        """Forget all but the environment's ``keep`` newest revisions, and revision
        ``spare`` (the last known good of an environment that needs recovery); return the
        lockfiles they referenced."""
        bound = "SELECT revision FROM revision_entries WHERE environment_id = ?"
        bound += " ORDER BY revision DESC LIMIT 1 OFFSET ?"
        row = self._db.execute(bound, (environment_id, keep - 1)).fetchone()
        if row is None:
            return []
        spared = -1 if spare is None else spare
        with self._transaction():
            gone = self._db.execute(
                "SELECT lockfile FROM revision_entries WHERE environment_id = ? AND revision < ?"
                " AND revision != ? AND lockfile IS NOT NULL",
                (environment_id, int(row[0]), spared),
            ).fetchall()
            self._db.execute(
                "DELETE FROM revision_entries WHERE environment_id = ? AND revision < ?"
                " AND revision != ?",
                (environment_id, int(row[0]), spared),
            )
        return [str(found[0]) for found in gone]

    def lockfiles(self, environment_id: str) -> list[tuple[int, str, int]]:
        """The environment's revisions that have a lockfile, oldest first: revision,
        SHA-256 and size."""
        rows = self._db.execute(
            "SELECT revision, lockfile, lockfile_size FROM revision_entries"
            " WHERE environment_id = ? AND lockfile IS NOT NULL ORDER BY revision",
            (environment_id,),
        ).fetchall()
        return [(int(row[0]), str(row[1]), int(row[2])) for row in rows]

    def lockfile_used(self, sha256: str) -> bool:
        """Whether any environment's revision still names this lockfile."""
        row = self._db.execute(
            "SELECT 1 FROM revision_entries WHERE lockfile = ? LIMIT 1", (sha256,)
        ).fetchone()
        return row is not None

    def forget_revisions(self, environment_id: str) -> list[str]:
        """Forget an environment's revisions, the current one too; return the lockfiles
        they referenced."""
        with self._transaction():
            gone = self._db.execute(
                "SELECT lockfile FROM revision_entries WHERE environment_id = ?"
                " AND lockfile IS NOT NULL",
                (environment_id,),
            ).fetchall()
            self._db.execute(
                "DELETE FROM revision_entries WHERE environment_id = ?", (environment_id,)
            )
            self._db.execute(
                "DELETE FROM environment_revisions WHERE environment_id = ?", (environment_id,)
            )
            self._db.execute(
                "DELETE FROM package_status WHERE environment_id = ?", (environment_id,)
            )
        return [str(found[0]) for found in gone]

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

    # Provisioning operations (Conda provisioning), keyed by project like executions.

    def find_provision(self, project_id: str, operation_id: str) -> tuple[str, str] | None:
        """The provisioning operation and request digest of a project's operation ID."""
        row = self._db.execute(
            "SELECT provision_id, request_digest FROM provisions WHERE operation_key = ?",
            (_operation_key(project_id, operation_id),),
        ).fetchone()
        return (str(row[0]), str(row[1])) if row else None

    def insert_provision(self, operation: ProvisionOperation, digest: str) -> None:
        request = operation.request
        self._db.execute(
            "INSERT INTO provisions VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                operation.provision_id,
                _operation_key(request.context.project_id, request.operation_id),
                digest,
                request.context.environment_id,
                int(operation.state is ProvisionState.FINISHED),
                operation.submitted_at.timestamp(),
                operation.model_dump_json(),
            ),
        )

    def update_provision(self, operation: ProvisionOperation) -> None:
        self._db.execute(
            "UPDATE provisions SET record = ?, finished = ? WHERE provision_id = ?",
            (
                operation.model_dump_json(),
                int(operation.state is ProvisionState.FINISHED),
                operation.provision_id,
            ),
        )

    def provision(self, provision_id: str) -> ProvisionOperation | None:
        row = self._db.execute(
            "SELECT record FROM provisions WHERE provision_id = ?", (provision_id,)
        ).fetchone()
        return _provision(str(row[0])) if row else None

    def provision_ids(self) -> list[str]:
        """Every provisioning operation's ID, oldest first."""
        rows = self._db.execute("SELECT provision_id FROM provisions ORDER BY submitted")
        return [str(row[0]) for row in rows.fetchall()]

    def unfinished_provisions(self) -> list[ProvisionOperation]:
        """Operations not yet finished, oldest first; unreadable ones are skipped."""
        rows = self._db.execute(
            "SELECT record FROM provisions WHERE finished = 0 ORDER BY submitted"
        ).fetchall()
        return [found for row in rows if (found := _provision(str(row[0]))) is not None]

    def package_status(self, environment_id: str) -> tuple[str, str, int | None] | None:
        """An environment's package status other than ``ready``: the status, the operation
        that left it so, and the last revision known good before it."""
        row = self._db.execute(
            "SELECT status, provision_id, revision FROM package_status WHERE environment_id = ?",
            (environment_id,),
        ).fetchone()
        if row is None:
            return None
        return str(row[0]), str(row[1]), None if row[2] is None else int(row[2])

    def set_package_status(
        self, environment_id: str, status: str, provision_id: str, revision: int | None
    ) -> None:
        """Mark an environment's packages, unless a mark is there already: the earliest
        holds, with the last revision known good (CP-13). Forgetting the environment's
        revisions clears it."""
        self._db.execute(
            "INSERT OR IGNORE INTO package_status VALUES (?, ?, ?, ?)",
            (environment_id, status, provision_id, revision),
        )


def _provision(record: str) -> ProvisionOperation | None:
    try:
        return ProvisionOperation.model_validate_json(record)
    except ValidationError:
        _log.error("a stored provisioning operation does not validate")
        return None


def _readable(execution_id: str, record: object) -> Execution | None:
    try:
        return Execution.model_validate_json(str(record))
    except ValidationError:
        _log.error("the stored record of execution %s does not validate", execution_id)
        return None


def _revision(environment_id: str, record: object) -> EnvironmentRevision | None:
    try:
        return EnvironmentRevision.model_validate_json(str(record))
    except ValidationError:
        _log.error("a stored revision of environment %s does not validate", environment_id)
        return None


def _operation_key(project_id: str, operation_id: str) -> str:
    # Identifiers have no slash, so no key can be read two ways.
    return f"{project_id}/{operation_id}"
