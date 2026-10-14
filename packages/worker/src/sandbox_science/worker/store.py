# SPDX-License-Identifier: MIT
"""Durable worker state in SQLite: executions, their events, kernels, environment revisions,
artifacts and provisioning operations."""

from __future__ import annotations

import contextlib
import json
import logging
import re
import sqlite3
import time
from collections.abc import Callable, Generator, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import cast

from pydantic import TypeAdapter, ValidationError

from sandbox_science.contracts import (
    TERMINAL_STATES,
    ArtifactRef,
    EnvironmentRevision,
    ErrorKind,
    Event,
    Execution,
    ExecutionContext,
    ProvisionOperation,
    ProvisionState,
)
from sandbox_science.contracts.jsonio import strict_loads
from sandbox_science.contracts.output_observations import Gap, OutputPage, OutputReport, OutputScope

from .errors import WorkerError
from .kernel_revisions import KernelStarts
from .output_observations import ObservationJournal
from .package_gate import PackageGate, checked
from .provision_observations import ProvisionObservations
from .schema import inspect, prepare

_log = logging.getLogger(__name__)
_EVENT = TypeAdapter[Event](Event)


@dataclass(frozen=True, slots=True)
class CaptureLease:
    """Durable capture admission; a held lease never expires on a clock or reconnect."""

    publication_id: str
    environment_id: str
    incarnation: str = ""
    capture_id: str = ""
    phase: str = "active"
    guest_clean: int = 0
    sandbox: str = ""
    sandbox_id: str = ""


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
        self._path = path
        # Refuse a file this worker cannot use before anything could change it: opening
        # it to write could switch its journal or replay a crashed WAL.
        inspect(path)
        self._db = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
        try:
            self._db.execute("PRAGMA synchronous=FULL")
            prepare(path, self._db)
            self._db.execute("PRAGMA journal_mode=WAL")
        except BaseException:
            self._db.close()
            raise
        self.provision_observations = ProvisionObservations(self._db)
        self.observations = ObservationJournal(self._db)
        self.kernel_starts = KernelStarts(self._db)
        # Each start closes the gate of every environment with an unfinished operation
        # until the provisioner classifies it, and holds one marked for recovery:
        # never infer readiness from the absence of a gate row. A store from before
        # versioning may hold operations whose writer published no gate at all.
        with self._transaction():
            self._db.execute(
                "INSERT OR IGNORE INTO package_execution_gates"
                " SELECT environment_id,1,'needs_recovery',provision_id,revision,''"
                " FROM package_status"
            )
            self._db.execute(
                "INSERT OR IGNORE INTO package_execution_gates"
                " SELECT environment_id,1,'hold',provision_id,NULL,''"
                " FROM provisions WHERE finished=0 ORDER BY submitted"
            )
            for row in self._db.execute(
                "SELECT substr(CAST(environment_id AS BLOB),1,130),"
                " substr(CAST(provision_id AS BLOB),1,130)"
                " FROM provisions WHERE finished=0 ORDER BY submitted"
            ):
                environment, operation = (bytes(value).decode("ascii", "replace") for value in row)
                checked(1, "hold", environment, None)
                checked(1, "hold", operation, None)
                gate = self.package_gate(environment)
                if gate.mode == "ready":
                    self._gate_write(
                        environment, PackageGate(gate.generation + 1, "hold", operation)
                    )
            for row in self._db.execute(
                "SELECT substr(CAST(environment_id AS BLOB),1,130) FROM package_status"
            ):
                environment = bytes(row[0]).decode("ascii", "replace")
                checked(1, "needs_recovery", environment, None)
                marked = self.package_status(environment)
                if marked is None:
                    raise RuntimeError("retained package recovery identity changed")
                _, operation, revision = marked
                gate = self.package_gate(environment)
                if gate.mode != "needs_recovery" or (gate.operation, gate.revision) != (
                    operation,
                    revision,
                ):
                    self._gate_write(
                        environment,
                        PackageGate(
                            gate.generation + 1,
                            "needs_recovery",
                            operation,
                            revision,
                        ),
                    )

    def close(self) -> None:
        self._db.close()

    def capture_leases(self) -> tuple[CaptureLease, ...]:
        """Read at most four bounded lease records; corrupt state holds all admission."""
        rows = self._db.execute(
            "SELECT substr(publication_id,1,29), substr(environment_id,1,129),"
            " substr(incarnation,1,29), substr(capture_id,1,29), substr(phase,1,7),"
            " CASE WHEN typeof(guest_clean)='integer' THEN guest_clean ELSE NULL END,"
            " substr(sandbox,1,129), substr(sandbox_id,1,129)"
            " FROM artifact_capture_leases LIMIT 5"
        ).fetchall()
        if len(rows) > 4:
            raise ValueError("capture admission state exceeds its bound; cleanup is unknown")
        leases: list[CaptureLease] = []
        for row in rows:
            publication, environment, incarnation, capture_id, phase, clean, sandbox, sandbox_id = (
                row
            )
            if (
                not isinstance(publication, str)
                or re.fullmatch(r"pub-[0-9a-f]{24}", publication) is None
                or not isinstance(environment, str)
                or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", environment) is None
                or not isinstance(incarnation, str)
                or (incarnation != "" and re.fullmatch(r"inc-[0-9a-f]{24}", incarnation) is None)
                or not isinstance(capture_id, str)
                or (capture_id != "" and re.fullmatch(r"cap-[0-9a-f]{24}", capture_id) is None)
                or bool(incarnation) != bool(capture_id)
                or phase not in ("active", "held")
                or type(clean) is not int
                or clean not in (0, 1)
                or not isinstance(sandbox, str)
                or not isinstance(sandbox_id, str)
                or bool(sandbox) != bool(sandbox_id)
                or (
                    sandbox != ""
                    and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", sandbox) is None
                )
                or (
                    sandbox_id != ""
                    and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", sandbox_id) is None
                )
            ):
                raise ValueError("capture admission state is corrupt; cleanup is unknown")
            leases.append(
                CaptureLease(
                    publication,
                    environment,
                    incarnation,
                    capture_id,
                    phase,
                    clean,
                    sandbox,
                    sandbox_id,
                )
            )
        return tuple(leases)

    def reserve_capture(self, publication_id: str, environment_id: str) -> CaptureLease:
        """Atomically count durable admission across connections, before any dispatch await."""
        if (
            re.fullmatch(r"pub-[0-9a-f]{24}", publication_id) is None
            or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", environment_id) is None
        ):
            raise ValueError("invalid capture admission identity")
        with self._transaction():
            leases = self.capture_leases()
            if (
                len(leases) >= 4
                or sum(item.environment_id == environment_id for item in leases) >= 2
            ):
                raise ValueError("capture admission limit reached; prior cleanup may be unknown")
            lease = CaptureLease(publication_id, environment_id)
            self._db.execute(
                "INSERT INTO artifact_capture_leases VALUES (?,?,?,?,?,?,?,?)",
                (publication_id, environment_id, "", "", "active", 0, "", ""),
            )
            return lease

    def dispatch_capture(
        self,
        lease: CaptureLease,
        incarnation: str,
        capture_id: str,
        binding: tuple[str, str] | None = None,
    ) -> CaptureLease:
        """Pin the exact helper/incarnation before sending, fenced by the prior lease."""
        if binding is not None and any(
            re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", value) is None for value in binding
        ):
            raise ValueError("capture VM binding is corrupt; cleanup is unknown")
        if (
            re.fullmatch(r"inc-[0-9a-f]{24}", incarnation) is None
            or re.fullmatch(r"cap-[0-9a-f]{24}", capture_id) is None
            or (lease.incarnation and lease.incarnation != incarnation)
            or (lease.sandbox and (lease.sandbox, lease.sandbox_id) != binding)
        ):
            raise ValueError("capture endpoint identity changed; cleanup is unknown")
        result = self._db.execute(
            "UPDATE artifact_capture_leases SET incarnation=?,capture_id=?,guest_clean=0,"
            " sandbox=?,sandbox_id=?"
            " WHERE publication_id=? AND environment_id=? AND incarnation=? AND capture_id=?"
            " AND phase='active' AND guest_clean=0 AND sandbox=? AND sandbox_id=?",
            (
                incarnation,
                capture_id,
                *(binding or ("", "")),
                lease.publication_id,
                lease.environment_id,
                lease.incarnation,
                lease.capture_id,
                lease.sandbox,
                lease.sandbox_id,
            ),
        )
        if result.rowcount != 1:
            raise ValueError("capture admission was displaced; cleanup is unknown")
        return CaptureLease(
            lease.publication_id,
            lease.environment_id,
            incarnation,
            capture_id,
            sandbox=(binding or ("", ""))[0],
            sandbox_id=(binding or ("", ""))[1],
        )

    def capture_binding(self, environment_id: str, incarnation: str) -> tuple[str, str] | None:
        """A clipped original endpoint binding for one exact daemon incarnation."""
        row = self._db.execute(
            "SELECT substr(sandbox,1,129), substr(sandbox_id,1,129) FROM executor_endpoints"
            " WHERE environment_id=? AND incarnation=?",
            (environment_id, incarnation),
        ).fetchone()
        if row is None:
            raise ValueError("capture endpoint incarnation changed before dispatch")
        if row == ("", ""):
            return None
        if any(
            not isinstance(value, str)
            or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", value) is None
            for value in row
        ):
            raise ValueError("capture VM binding is corrupt; cleanup is unknown")
        return row[0], row[1]

    def finish_capture(self, lease: CaptureLease, *, certain: bool) -> bool:
        """Called only after actual host work drains; retain unknown guest ownership."""
        with self._transaction():
            found = next(
                (
                    item
                    for item in self.capture_leases()
                    if item.publication_id == lease.publication_id
                ),
                None,
            )
            if found is None or (
                found.environment_id,
                found.incarnation,
                found.capture_id,
                found.phase,
                found.sandbox,
                found.sandbox_id,
            ) != (
                lease.environment_id,
                lease.incarnation,
                lease.capture_id,
                "active",
                lease.sandbox,
                lease.sandbox_id,
            ):
                raise ValueError("capture completion was displaced; cleanup is unknown")
            if certain or found.guest_clean == 1:
                self._db.execute(
                    "DELETE FROM artifact_capture_leases WHERE publication_id=?",
                    (lease.publication_id,),
                )
                return True
            self._db.execute(
                "UPDATE artifact_capture_leases SET phase='held' WHERE publication_id=?",
                (lease.publication_id,),
            )
            return False

    def acknowledge_capture(self, lease: CaptureLease) -> None:
        """Release one held exact tuple after a same-incarnation positive cleanup ACK."""
        result = self._db.execute(
            "DELETE FROM artifact_capture_leases WHERE publication_id=? AND environment_id=?"
            " AND incarnation=? AND capture_id=? AND phase='held' AND sandbox=? AND sandbox_id=?",
            (
                lease.publication_id,
                lease.environment_id,
                lease.incarnation,
                lease.capture_id,
                lease.sandbox,
                lease.sandbox_id,
            ),
        )
        if result.rowcount != 1:
            raise ValueError("capture cleanup acknowledgement was displaced")

    def fenced_captures(
        self, environment_id: str, incarnation: str, sandbox: str, sandbox_id: str
    ) -> None:
        """Verified original-VM termination; active host work still owns its rows."""
        if not sandbox or not sandbox_id:
            raise ValueError("capture cleanup needs the original immutable VM binding")
        with self._transaction():
            self.capture_leases()  # Corrupt state cannot inherit an unrelated lifecycle proof.
            self._db.execute(
                "UPDATE artifact_capture_leases SET guest_clean=1"
                " WHERE environment_id=? AND incarnation=? AND sandbox=? AND sandbox_id=?",
                (environment_id, incarnation, sandbox, sandbox_id),
            )
            self._db.execute(
                "DELETE FROM artifact_capture_leases WHERE environment_id=? AND incarnation=?"
                " AND phase='held' AND sandbox=? AND sandbox_id=?",
                (environment_id, incarnation, sandbox, sandbox_id),
            )

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
        with self._transaction():
            self._db.execute(
                "INSERT INTO executions(execution_id,operation_id,request_digest,environment_id,"
                "session_id,terminal,record) VALUES (?, ?, ?, ?, ?, ?, ?)",
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
            self.observations.accept(execution, digest)

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

    def producer_hint(
        self, project_id: str, environment_id: str, session_id: str, path: str, deadline: float
    ) -> str | None:
        """A bounded, unproven hint from the last 256 terminal rows, never lineage.

        LIMIT precedes the path predicate. Corrupt/large records are unknown, and
        only small scalar projections cross the SQLite/Python boundary.
        """
        shared = path.startswith("shared/")
        try:
            with contextlib.closing(
                sqlite3.connect(self._path.resolve().as_uri() + "?mode=ro", uri=True, timeout=0.1)
            ) as database:
                database.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
                return self._producer_hint(
                    database, project_id, environment_id, session_id, path, shared
                )
        except sqlite3.DatabaseError:
            _log.warning("artifact producer hint unavailable; attribution remains unknown")
            return None

    @staticmethod
    def _producer_hint(
        database: sqlite3.Connection,
        project_id: str,
        environment_id: str,
        session_id: str,
        path: str,
        shared: bool,
    ) -> str | None:
        row = database.execute(
            "WITH recent AS MATERIALIZED ("
            " SELECT rowid AS execution_row FROM executions"
            " WHERE environment_id = ? AND terminal = 1"
            " AND (? OR session_id = ?) ORDER BY rowid DESC LIMIT 256"
            "), bounded AS NOT MATERIALIZED ("
            " SELECT CASE WHEN length(CAST(record AS BLOB)) <= 65536"
            " THEN CASE WHEN json_valid(record) THEN record ELSE '{}' END"
            " ELSE '{}' END AS record FROM executions"
            " JOIN recent ON executions.rowid = recent.execution_row"
            ") SELECT substr(json_extract(record, '$.attempt_id'), 1, 29) AS attempt,"
            " substr(json_extract(record, '$.ended_at'), 1, 65)"
            " FROM bounded WHERE json_extract(record, '$.context.project_id') = ?"
            " AND json_extract(record, '$.context.environment_id') = ?"
            " AND (? OR json_extract(record, '$.context.session_id') = ?)"
            " AND json_type(record, '$.ended_at') = 'text'"
            " AND length(json_extract(record, '$.ended_at')) <= 64"
            " AND json_type(record, '$.attempt_id') = 'text'"
            " AND length(json_extract(record, '$.attempt_id')) = 28"
            " AND json_type(record, '$.result.written_files') = 'array'"
            " AND json_array_length(record, '$.result.written_files') <= 1000"
            " AND EXISTS(SELECT 1 FROM json_each(record, '$.result.written_files')"
            " WHERE type = 'text' AND value = ?)"
            " ORDER BY json_extract(record, '$.ended_at') DESC LIMIT 1",
            (
                environment_id,
                shared,
                session_id,
                project_id,
                environment_id,
                shared,
                session_id,
                path,
            ),
        ).fetchone()
        attempt = row[0] if row else None
        try:
            ended = datetime.fromisoformat(row[1]) if row else None
        except ValueError:
            return None
        return (
            attempt
            if isinstance(attempt, str)
            and re.fullmatch(r"att-[0-9a-f]{24}", attempt)
            and ended is not None
            and ended.tzinfo is not None
            else None
        )

    def finish(
        self,
        execution: Execution,
        events: Sequence[Event],
        source_seq: int | None = None,
        *,
        observation: OutputReport | None = None,
        observation_gap: Gap | None = "missing",
    ) -> None:
        """Store an execution's last events and its terminal record in one transaction."""
        with self._transaction():
            row = self._db.execute(
                "SELECT terminal FROM executions WHERE execution_id=?", (execution.execution_id,)
            ).fetchone()
            if row is not None and row[0] == 1:
                return  # terminal replay cannot overwrite its original observation or result
            for event in events:
                self.add_event(event)
            self.update_execution(execution)
            self.observations.finish(execution, observation, observation_gap)
            if source_seq is not None:
                self.source_cursor(execution.execution_id, source_seq)

    def observation_dispatch(
        self,
        execution: Execution,
        incarnation: str,
        identity: tuple[str, str] | None,
        *,
        daemon: bool,
    ) -> None:
        """Pin original dispatch facts together before the first request write."""
        with self._transaction():
            if daemon:
                # Recoverability is canonical execution state, independent of a
                # missing/corrupt derived observation binding. Never rebind it.
                self._db.execute(
                    "UPDATE execution_recovery SET incarnation=?"
                    " WHERE execution_id=? AND incarnation=''",
                    (incarnation, execution.execution_id),
                )
            self.observations.dispatch(execution, incarnation, identity)

    def observation_page(
        self,
        scope: OutputScope,
        *,
        after: int = 0,
        target: int | None = None,
        stream_id: str | None = None,
    ) -> OutputPage:
        """Internal scoped read only; this grants no remote/client or capture authority."""
        return self.observations.page(scope, after=after, target=target, stream_id=stream_id)

    def endpoints(self) -> tuple[str, ...]:
        return tuple(
            str(row[0]) for row in self._db.execute("SELECT environment_id FROM executor_endpoints")
        )

    def endpoint_incarnation(self, environment_id: str) -> str | None:
        row = self._db.execute(
            "SELECT substr(CAST(incarnation AS BLOB),1,29)"
            " FROM executor_endpoints WHERE environment_id = ?",
            (environment_id,),
        ).fetchone()
        if row is None or not isinstance(row[0], bytes):
            return None
        try:
            incarnation = row[0].decode("ascii")
        except UnicodeDecodeError:
            return None
        return incarnation if re.fullmatch(r"inc-[0-9a-f]{24}", incarnation) else None

    def endpoint_binding(self, environment_id: str) -> tuple[str, str] | None:
        return self._executor_binding("executor_endpoints", environment_id)

    def lineage_endpoint(self, environment_id: str) -> tuple[str, str, str] | None:
        """One bounded incarnation/VM projection; never combine separate generations."""
        row = self._db.execute(
            "SELECT substr(CAST(incarnation AS BLOB),1,29),"
            " substr(CAST(sandbox AS BLOB),1,129), substr(CAST(sandbox_id AS BLOB),1,129)"
            " FROM executor_endpoints WHERE environment_id=?",
            (environment_id,),
        ).fetchone()
        if row is None or any(not isinstance(value, bytes) for value in row):
            return None
        try:
            incarnation, sandbox, sandbox_id = (value.decode("ascii") for value in row)
        except UnicodeDecodeError:
            return None
        if re.fullmatch(r"inc-[0-9a-f]{24}", incarnation) is None or any(
            re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", value) is None
            for value in (sandbox, sandbox_id)
        ):
            return None
        return incarnation, sandbox, sandbox_id

    def recycle_binding(self, environment_id: str) -> tuple[str, str] | None:
        return self._executor_binding("executor_recycles", environment_id)

    def _executor_binding(self, table: str, environment_id: str) -> tuple[str, str] | None:
        query = (
            "SELECT coalesce(substr(CAST(sandbox AS BLOB),1,2049),X''),"
            "coalesce(substr(CAST(sandbox_id AS BLOB),1,2049),X'')"
            " FROM executor_endpoints WHERE environment_id = ?"
            if table == "executor_endpoints"
            else "SELECT coalesce(substr(CAST(sandbox AS BLOB),1,2049),X''),"
            "coalesce(substr(CAST(sandbox_id AS BLOB),1,2049),X'')"
            " FROM executor_recycles WHERE environment_id = ?"
        )
        row = self._db.execute(query, (environment_id,)).fetchone()
        if row is None or not row[0] or not row[1]:
            return None
        if any(len(value) > 2048 for value in row):
            raise WorkerError(ErrorKind.UNAVAILABLE, "invalid retained executor binding")
        try:
            return bytes(row[0]).decode(), bytes(row[1]).decode()
        except UnicodeDecodeError as exc:
            raise WorkerError(ErrorKind.UNAVAILABLE, "invalid retained executor binding") from exc

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

    def commit_kernel_start(
        self, start: Callable[[], Execution], event: Event, source_seq: int | None
    ) -> Execution:
        """Birth/generation, cell state and source acknowledgement share one commit."""
        with self._transaction():
            execution = start()
            self.add_event(event)
            self.update_execution(execution)
            if source_seq is not None:
                self.source_cursor(execution.execution_id, source_seq)
        return execution

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
        return [str(found[0]) for found in gone]

    def forget_package_gate(self, environment_id: str, *, binding: str | None) -> None:
        """Clear only after confirmed sandbox deletion and durable operation drain."""
        with self._transaction():
            if not self.provisioning_finished(environment_id):
                raise WorkerError(ErrorKind.UNAVAILABLE, "old provisioning is not durably finished")
            self._guard_package_binding(environment_id, binding)
            self._db.execute("DELETE FROM package_status WHERE environment_id=?", (environment_id,))
            self._db.execute(
                "DELETE FROM package_execution_gates WHERE environment_id=?", (environment_id,)
            )

    def provisioning_finished(self, environment_id: str) -> bool:
        """Do not parse or skip corrupt old records when proving durable drain."""
        return (
            self._db.execute(
                "SELECT 1 FROM provisions WHERE environment_id=? AND ("
                " typeof(finished)!='integer' OR finished!=1 OR"
                " CASE WHEN json_valid(record) THEN"
                " json_extract(record,'$.state') IS NOT 'finished' OR"
                " json_extract(record,'$.provision_id') IS NOT provision_id OR"
                " json_extract(record,'$.request.context.environment_id') IS NOT environment_id"
                " ELSE 1 END) LIMIT 1",
                (environment_id,),
            ).fetchone()
            is None
        )

    # Kernels.

    def kernel(self, environment_id: str, session_id: str, language: str) -> KernelRecord | None:
        row = self._db.execute(
            "SELECT substr(incarnation,1,29), CASE WHEN typeof(base)='integer' THEN base END,"
            " CASE WHEN typeof(last)='integer' THEN last END FROM kernels"
            " WHERE environment_id = ? AND session_id = ? AND language = ?",
            (environment_id, session_id, language),
        ).fetchone()
        if row is None:
            return None
        incarnation, base, last = row
        if (
            not isinstance(incarnation, str)
            or re.fullmatch(r"inc-[0-9a-f]{24}", incarnation) is None
            or type(base) is not int
            or type(last) is not int
            or not 0 <= base < last <= 2**62
        ):
            raise ValueError("invalid retained kernel generation")
        return KernelRecord(incarnation, base, last)

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

    def installer_artifact(
        self, expected: ArtifactRef, *, insert: bool = False
    ) -> ArtifactRef | None:
        """A bounded exact host-generated installer reference; never replace/reseed one."""
        if type(expected) is not ArtifactRef or type(insert) is not bool:
            raise ValueError("invalid installer artifact type")
        data = cast(dict[object, object], object.__getattribute__(expected, "__dict__"))
        if (
            type(data) is not dict
            or len(data) != len(ArtifactRef.model_fields)
            or any(type(key) is not str for key in data)
            or set(data) != set(ArtifactRef.model_fields)
            or object.__getattribute__(expected, "__pydantic_extra__") is not None
            or type(expected.artifact_id) is not str
            or re.fullmatch(r"art-inst-[0-9a-f]{32}", expected.artifact_id) is None
            or type(expected.project_id) is not str
            or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", expected.project_id) is None
            or type(expected.sha256) is not str
            or re.fullmatch(r"[0-9a-f]{64}", expected.sha256) is None
            or type(expected.size) is not int
            or not 0 < expected.size <= 64 * 1024
            or type(expected.media_type) is not str
            or expected.media_type != "application/vnd.sandbox-science.installer-log+json"
            or type(expected.source_path) is not str
            or re.fullmatch(
                r"provisioning/[A-Za-z0-9][A-Za-z0-9._-]{0,127}/"
                r"(?:version|resolve|install)-(?:conda|pip)\.json",
                expected.source_path,
            )
            is None
            or expected.produced_by is not None
            or type(expected.created_at) is not datetime
        ):
            raise ValueError("invalid bounded installer artifact fields")

        def read() -> ArtifactRef | None:
            row = self._db.execute(
                "SELECT typeof(record),length(CAST(record AS BLOB)),"
                "substr(CAST(record AS BLOB),1,4097) FROM artifacts WHERE artifact_id=?",
                (expected.artifact_id,),
            ).fetchone()
            if row is None:
                return None
            if (
                row[0] != "text"
                or type(row[1]) is not int
                or not 0 < row[1] <= 4096
                or type(row[2]) is not bytes
                or len(row[2]) != row[1]
            ):
                raise ValueError("retained installer artifact exceeds bounds")
            try:
                strict_loads(row[2])
                value = ArtifactRef.model_validate_json(row[2], strict=True)
            except (ValueError, TypeError, RecursionError):
                raise ValueError("retained installer artifact invalid") from None
            if value != expected:
                raise ValueError("retained installer artifact identity changed")
            return value

        if not insert:
            return read()
        with self._transaction():
            existing = read()
            if existing is None:
                self.insert_artifact(expected)
            return expected

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
        environment = operation.request.context.environment_id
        with self._transaction():
            gate = self.package_gate(environment)
            if operation.state is ProvisionState.INSTALLING and gate.mode == "ready":
                self._gate_write(
                    environment,
                    PackageGate(
                        gate.generation + 1,
                        "hold",
                        operation.provision_id,
                        operation.revision_before,
                    ),
                )
            elif operation.state is ProvisionState.FINISHED and (
                gate.mode == "hold" and gate.operation == operation.provision_id
            ):
                self._gate_write(environment, PackageGate(gate.generation + 1))
            self._db.execute(
                "UPDATE provisions SET record = ?, finished = ? WHERE provision_id = ?",
                (
                    operation.model_dump_json(),
                    int(operation.state is ProvisionState.FINISHED),
                    operation.provision_id,
                ),
            )

    def package_gate(self, environment_id: str) -> PackageGate:
        row = self._db.execute(
            "SELECT generation,substr(CAST(mode AS BLOB),1,32),"
            "coalesce(substr(CAST(operation AS BLOB),1,130),X''),revision"
            " FROM package_execution_gates WHERE environment_id=?",
            (environment_id,),
        ).fetchone()
        try:
            gate = (
                PackageGate()
                if row is None
                else checked(row[0], bytes(row[1]).decode(), bytes(row[2]).decode(), row[3])
            )
        except (UnicodeError, TypeError):
            return checked(None, None, None, None)
        marked = self.package_status(environment_id)
        if marked is not None and (gate.mode, gate.operation, gate.revision) != marked:
            if not self._db.in_transaction:
                with self._transaction():
                    return self.package_gate(environment_id)
            gate = PackageGate(gate.generation + 1, "needs_recovery", marked[1], marked[2])
            self._gate_write(environment_id, gate)
        return gate

    def _guard_package_binding(self, environment_id: str, binding: str | None) -> None:
        """Validate bounded original identity inside the caller's SQL transaction."""
        gate = self.package_gate(environment_id)
        encoded: bytes | None = None
        if binding is not None:
            try:
                if type(binding) is not str or len(binding) > 4096:
                    raise ValueError
                encoded = binding.encode("utf-8")
                if not encoded or len(encoded) > 4096:
                    raise ValueError
            except (UnicodeError, ValueError):
                raise WorkerError(
                    ErrorKind.UNAVAILABLE, "invalid package sandbox binding"
                ) from None
        row = self._db.execute(
            "SELECT typeof(binding),coalesce(substr(CAST(binding AS BLOB),1,4097),X'')"
            " FROM package_execution_gates WHERE environment_id=?",
            (environment_id,),
        ).fetchone()
        retained = b"" if row is None else row[1]
        if row is not None and (row[0] != "text" or len(retained) > 4096):
            raise WorkerError(ErrorKind.UNAVAILABLE, "invalid retained package sandbox binding")
        if retained:
            if encoded is None or retained != encoded:
                raise WorkerError(
                    ErrorKind.UNAVAILABLE, "package admission sandbox binding changed"
                )
        elif gate.mode != "ready":
            previous = self.endpoint_binding(environment_id)
            if binding is None or previous is None or repr(previous) != binding:
                raise WorkerError(
                    ErrorKind.UNAVAILABLE,
                    "legacy package hold has no proven original sandbox binding",
                )

    def bind_package_gate(self, environment_id: str, binding: str) -> None:
        """Never transfer a retained hold to another immutable sandbox binding."""
        with self._transaction():
            self._guard_package_binding(environment_id, binding)
            self._db.execute(
                "INSERT OR IGNORE INTO package_execution_gates VALUES (?,0,'ready','',NULL,?)",
                (environment_id, binding),
            )
            self._db.execute(
                "UPDATE package_execution_gates SET binding=? WHERE environment_id=?",
                (binding, environment_id),
            )

    def guard_package_deletion(self, environment_id: str, binding: str | None) -> None:
        """Prove and retain the original binding before any guest lifecycle mutation."""
        with self._transaction():
            self._guard_package_binding(environment_id, binding)
            if binding is not None:
                # Preserve an exact legacy endpoint proof across executor shutdown,
                # which normally forgets that endpoint before final gate cleanup.
                self._db.execute(
                    "INSERT OR IGNORE INTO package_execution_gates VALUES (?,0,'ready','',NULL,?)",
                    (environment_id, binding),
                )
                self._db.execute(
                    "UPDATE package_execution_gates SET binding=? WHERE environment_id=?",
                    (binding, environment_id),
                )

    def package_gate_has_binding(self, environment_id: str) -> bool:
        """Existence projection: ready state does not transfer an original binding."""
        return (
            self._db.execute(
                "SELECT 1 FROM package_execution_gates WHERE environment_id=?"
                " AND (typeof(binding)!='text' OR binding!='')",
                (environment_id,),
            ).fetchone()
            is not None
        )

    def _gate_write(self, environment_id: str, gate: PackageGate) -> None:
        checked(gate.generation, gate.mode, gate.operation, gate.revision)
        self._db.execute(
            "INSERT INTO package_execution_gates VALUES (?,?,?,?,?,'')"
            " ON CONFLICT(environment_id) DO UPDATE SET generation=excluded.generation,"
            " mode=excluded.mode,operation=excluded.operation,revision=excluded.revision",
            (environment_id, gate.generation, gate.mode, gate.operation, gate.revision),
        )

    def provision(self, provision_id: str) -> ProvisionOperation | None:
        row = self._db.execute(
            "SELECT record FROM provisions WHERE provision_id = ?", (provision_id,)
        ).fetchone()
        return _provision(str(row[0])) if row else None

    def provision_authority(
        self, provision_id: str
    ) -> tuple[ExecutionContext, ProvisionState] | None:
        """Small positive authority projection, independent of large resolve/probe records."""
        try:
            row = self._db.execute(
                "SELECT substr(CAST(CASE WHEN json_valid(record) THEN json_object("
                " 'id',json_extract(record,'$.provision_id'),"
                " 'context',json_extract(record,'$.request.context'),"
                " 'state',json_extract(record,'$.state')) ELSE '' END AS BLOB),1,4097),"
                " substr(CAST(environment_id AS BLOB),1,130)"
                " FROM provisions WHERE provision_id=?",
                (provision_id,),
            ).fetchone()
            if row is None:
                return None
            if row[0] is None or len(row[0]) > 4096:
                raise ValueError("oversized authority")
            raw: object = json.loads(row[0])
            if not isinstance(raw, dict):
                raise ValueError("invalid authority identity")
            value = cast(dict[str, object], raw)
            if value.get("id") != provision_id:
                raise ValueError("invalid authority identity")
            context = ExecutionContext.model_validate(value.get("context"))
            if row[1] != context.environment_id.encode():
                raise ValueError("invalid authority scope")
            return context, ProvisionState(value.get("state"))
        except (ValueError, TypeError, sqlite3.Error, RecursionError) as exc:
            raise WorkerError(
                ErrorKind.UNAVAILABLE, "invalid retained provisioning authority"
            ) from exc

    def provision_step(self, provision_id: str, operation_id: str) -> str | None:
        row = self._db.execute(
            "SELECT substr(CAST(request_digest AS BLOB),1,65) FROM provision_steps"
            " WHERE provision_id=? AND operation_id=?",
            (provision_id, operation_id),
        ).fetchone()
        return bytes(row[0]).decode("ascii", "replace") if row else None

    def authorize_provision_step(self, provision_id: str, operation_id: str, digest: str) -> None:
        """Only the internal provisioner registers an exact saved step; no RPC exposes this."""
        with self._transaction():
            self._db.execute(
                "INSERT OR IGNORE INTO provision_steps VALUES (?,?,?)",
                (provision_id, operation_id, digest),
            )
            if self.provision_step(provision_id, operation_id) != digest:
                raise RuntimeError("provisioning step identity changed")

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
            "SELECT substr(CAST(status AS BLOB),1,32),"
            "coalesce(substr(CAST(provision_id AS BLOB),1,130),X''),revision"
            " FROM package_status WHERE environment_id = ?",
            (environment_id,),
        ).fetchone()
        if row is None:
            return None
        mode = bytes(row[0]).decode("ascii", "replace") if row[0] is not None else ""
        operation = bytes(row[1]).decode("ascii", "replace")
        checked(0, mode, operation, row[2])
        if mode != "needs_recovery":
            raise WorkerError(ErrorKind.UNAVAILABLE, "invalid retained package recovery status")
        return mode, operation, row[2]

    def set_package_status(
        self, environment_id: str, status: str, provision_id: str, revision: int | None
    ) -> None:
        """Mark an environment's packages, unless a mark is there already: the earliest
        holds, with the last revision known good (CP-13). Forgetting the environment's
        revisions clears it."""
        with self._transaction():
            self._db.execute(
                "INSERT OR IGNORE INTO package_status VALUES (?, ?, ?, ?)",
                (environment_id, status, provision_id, revision),
            )
            marked = self.package_status(environment_id)
            if marked is None:
                raise RuntimeError("package recovery mark was not retained")
            gate = self.package_gate(environment_id)
            if gate.mode != "needs_recovery":
                self._gate_write(
                    environment_id,
                    PackageGate(
                        gate.generation + 1,
                        "needs_recovery",
                        marked[1],
                        marked[2],
                    ),
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
