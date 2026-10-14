# SPDX-License-Identifier: MIT
"""Bounded worker-local terminal reports on the Store's existing connection owner.

No transport, filesystem, content capture or attribution authority lives here.
Detail has independent quotas; fixed attempt markers follow execution history.
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import sqlite3
from datetime import UTC, datetime
from typing import cast

from pydantic import ValidationError

from sandbox_science.contracts import ErrorKind, Execution
from sandbox_science.contracts.model import MAX_COUNTER
from sandbox_science.contracts.output_observations import (
    MAX_MARKER_BYTES,
    MAX_PAGE_BYTES,
    MAX_REPORT_BYTES,
    Gap,
    OutputBinding,
    OutputMarker,
    OutputPage,
    OutputRecord,
    OutputReport,
    OutputScope,
)

from .errors import WorkerError

MAX_DETAILS = 4096
MAX_DETAIL_BYTES = 32 * 1024 * 1024
MAX_PROJECT_BYTES = 8 * 1024 * 1024
MAX_EVICTIONS = 64

_SCHEMA = """
CREATE TABLE IF NOT EXISTS output_observation_meta (
 singleton INTEGER PRIMARY KEY CHECK(singleton=1), generation BLOB NOT NULL, baseline BLOB NOT NULL
);
CREATE TABLE IF NOT EXISTS output_observation_scopes (
 project TEXT NOT NULL, session TEXT NOT NULL, environment TEXT NOT NULL,
 head INTEGER NOT NULL, evicted_through INTEGER NOT NULL, unavailable INTEGER NOT NULL,
 PRIMARY KEY(project,session,environment)
);
CREATE TABLE IF NOT EXISTS output_observation_attempts (
 execution TEXT PRIMARY KEY, project TEXT NOT NULL, session TEXT NOT NULL,
 environment TEXT NOT NULL,
 binding BLOB NOT NULL, seq INTEGER, marker BLOB NOT NULL DEFAULT X'',
 UNIQUE(project,session,environment,seq)
);
CREATE TABLE IF NOT EXISTS output_observation_details (
 execution TEXT PRIMARY KEY, payload BLOB NOT NULL, size INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS output_observation_scope
 ON output_observation_attempts(project,session,environment,seq);
"""


def normalize(raw: object) -> tuple[OutputReport | None, Gap | None]:
    """Bound structure before schema/JSON work; malformed observations cannot fail execution."""
    if raw is None:
        return None, "missing"
    pending: list[tuple[object, int]] = [(raw, 0)]
    nodes = size = 0
    while pending:
        value, depth = pending.pop()
        nodes += 1
        if nodes > 2048 or depth > 6:
            return None, "oversized"
        if isinstance(value, str):
            if len(value) > 8192:
                return None, "oversized"
            try:
                size += len(value.encode())
            except UnicodeError:
                return None, "invalid"
        elif isinstance(value, dict):
            if len(cast(dict[object, object], value)) > 64:
                return None, "oversized"
            for key, item in cast(dict[object, object], value).items():
                if not isinstance(key, str):
                    return None, "invalid"
                pending.extend(((key, depth + 1), (item, depth + 1)))
        elif isinstance(value, (list, tuple)):
            if len(cast(list[object] | tuple[object, ...], value)) > 64:
                return None, "oversized"
            pending.extend(
                (item, depth + 1) for item in cast(list[object] | tuple[object, ...], value)
            )
        elif value is not None and type(value) not in (bool, int):
            return None, "invalid"
        else:
            size += 24
        if size > MAX_REPORT_BYTES:
            return None, "oversized"
    try:
        return OutputReport.model_validate(raw), None
    except (ValidationError, ValueError, RecursionError):
        return None, "invalid"


def _bounded_text(value: str, limit: int) -> bool:
    try:
        return (
            len(value) <= limit
            and len(value.encode()) <= limit
            and not any(ord(char) < 32 or 127 <= ord(char) < 160 for char in value)
        )
    except UnicodeError:
        return False


def accepted(execution: Execution, digest: str | None) -> OutputBinding:
    """Copy only accepted request identity; no model JSON can supply extra provenance."""
    context = execution.context
    policy = context.policy_revision
    policy_valid = policy is None or _bounded_text(policy, 256)
    return OutputBinding(
        project_id=context.project_id,
        session_id=context.session_id,
        environment_id=context.environment_id,
        execution_id=execution.execution_id,
        attempt_id=execution.attempt_id,
        operation_id=execution.operation_id,
        request_sha256=digest if digest and re.fullmatch(r"[0-9a-f]{64}", digest) else None,
        kind=execution.kind,
        actor=context.actor,
        policy_revision=policy if policy_valid else None,
        binding_unavailable=not policy_valid,
    )


class ObservationJournal:
    """Borrow the Store connection; all mutations run inside its owning transaction."""

    def __init__(self, db: sqlite3.Connection) -> None:
        self.db = db
        db.executescript(_SCHEMA)
        db.execute(
            "INSERT OR IGNORE INTO output_observation_meta VALUES(1,?,?)",
            (secrets.token_hex(32).encode(), datetime.now(UTC).isoformat().encode()),
        )

    def accept(self, execution: Execution, digest: str) -> None:
        binding = accepted(execution, digest)
        self.db.execute(
            "INSERT INTO output_observation_attempts"
            "(execution,project,session,environment,binding) VALUES(?,?,?,?,?)",
            (
                execution.execution_id,
                binding.project_id,
                binding.session_id,
                binding.environment_id,
                binding.model_dump_json().encode(),
            ),
        )

    def binding(self, execution: Execution) -> OutputBinding:
        row = self.db.execute(
            "SELECT CASE WHEN typeof(binding)='blob' THEN substr(binding,1,?) END,"
            " CASE WHEN typeof(binding)='blob' THEN length(binding) END"
            " FROM output_observation_attempts WHERE execution=?",
            (MAX_MARKER_BYTES + 1, execution.execution_id),
        ).fetchone()
        try:
            if row is None or row[1] is None or row[1] > MAX_MARKER_BYTES:
                raise ValueError("missing original binding")
            result = OutputBinding.model_validate_json(row[0])
            canonical = self.db.execute(
                "SELECT CASE WHEN typeof(request_digest)='text' AND length(request_digest)=64"
                " AND instr(request_digest,char(0))=0 THEN substr(request_digest,1,64) END"
                " FROM executions WHERE execution_id=?",
                (execution.execution_id,),
            ).fetchone()
            original = accepted(execution, canonical[0] if canonical else None)
            if result.model_dump(
                exclude={"dispatched", "incarnation", "sandbox", "sandbox_id"}
            ) != (
                original.model_dump(exclude={"dispatched", "incarnation", "sandbox", "sandbox_id"})
            ):
                raise ValueError("binding does not match accepted execution")
            return result
        except (ValueError, ValidationError, TypeError):
            return accepted(execution, None).model_copy(update={"binding_unavailable": True})

    def dispatch(
        self, execution: Execution, incarnation: str, identity: tuple[str, str] | None
    ) -> str | None:
        self.db.execute("SAVEPOINT output_dispatch")
        try:
            return self._dispatch(execution, incarnation, identity)
        except (ValueError, TypeError, OverflowError, sqlite3.IntegrityError):
            self.db.execute("ROLLBACK TO output_dispatch")
            return None  # canonical recovery identity is independent of this projection
        finally:
            self.db.execute("RELEASE output_dispatch")

    def _dispatch(
        self, execution: Execution, incarnation: str, identity: tuple[str, str] | None
    ) -> str | None:
        binding = self.binding(execution)
        if binding.dispatched or binding.binding_unavailable:
            return (
                binding.incarnation
            )  # never backfill from today's endpoint or overwrite an uncertain dispatch
        valid_incarnation = bool(re.fullmatch(r"inc-[0-9a-f]{24}", incarnation))
        valid_identity = identity is not None and all(
            value and _bounded_text(value, 2048) for value in identity
        )
        updated = binding.model_copy(
            update={
                "dispatched": True,
                "incarnation": incarnation if valid_incarnation else None,
                "sandbox": identity[0] if valid_identity and identity else None,
                "sandbox_id": identity[1] if valid_identity and identity else None,
                "binding_unavailable": not valid_incarnation,
            }
        )
        self.db.execute(
            "UPDATE output_observation_attempts SET binding=? WHERE execution=? AND seq IS NULL",
            (updated.model_dump_json().encode(), execution.execution_id),
        )
        return updated.incarnation

    def finish(self, execution: Execution, report: OutputReport | None, gap: Gap | None) -> None:
        """Derived corruption cannot roll back a healthy terminal record/source ACK."""
        self.db.execute("SAVEPOINT output_observation")
        try:
            self._finish(execution, report, gap)
        except (ValueError, TypeError, OverflowError, sqlite3.IntegrityError):
            self.db.execute("ROLLBACK TO output_observation")
            self._quarantine(execution)
        finally:
            self.db.execute("RELEASE output_observation")

    def _quarantine(self, execution: Execution) -> None:
        binding = self.binding(execution)
        scope = (binding.project_id, binding.session_id, binding.environment_id)
        self.db.execute(
            "INSERT INTO output_observation_scopes VALUES(?,?,?,0,0,1)"
            " ON CONFLICT(project,session,environment) DO UPDATE SET unavailable=1",
            scope,
        )
        marker = OutputMarker(
            binding=binding,
            seq=None,
            state=execution.state,
            received_at=datetime.now(UTC),
            gap="invalid",
            mutation_coverage="unavailable",
            potentially_partial=execution.state.value != "succeeded",
        )
        self.db.execute(
            "INSERT INTO output_observation_attempts"
            "(execution,project,session,environment,binding,marker) VALUES(?,?,?,?,?,?)"
            " ON CONFLICT(execution) DO UPDATE SET seq=NULL,marker=excluded.marker",
            (
                execution.execution_id,
                *scope,
                binding.model_dump_json().encode(),
                marker.model_dump_json().encode(),
            ),
        )

    def _finish(self, execution: Execution, report: OutputReport | None, gap: Gap | None) -> None:
        binding = self.binding(execution)
        scope = (binding.project_id, binding.session_id, binding.environment_id)
        stored = self.db.execute(
            "SELECT CASE WHEN typeof(binding)='blob' THEN substr(binding,1,?) END"
            " FROM output_observation_attempts WHERE execution=?",
            (MAX_MARKER_BYTES + 1, execution.execution_id),
        ).fetchone()
        if stored is None:
            # An old unfinished attempt has no original dispatch facts. Never backfill them.
            self.accept(execution, "")
            self.db.execute(
                "UPDATE output_observation_attempts SET binding=? WHERE execution=?",
                (binding.model_dump_json().encode(), execution.execution_id),
            )
        elif binding != OutputBinding.model_validate_json(stored[0]):
            raise ValueError("invalid original binding")
        self.db.execute(
            "INSERT OR IGNORE INTO output_observation_scopes VALUES(?,?,?,0,0,0)", scope
        )
        counters = self.db.execute(
            "SELECT CASE WHEN typeof(head)='integer' THEN head END,"
            "CASE WHEN typeof(evicted_through)='integer' THEN evicted_through END,"
            "CASE WHEN typeof(unavailable)='integer' THEN unavailable END"
            " FROM output_observation_scopes WHERE project=? AND session=? AND environment=?",
            scope,
        ).fetchone()
        head, floor, unavailable = counters
        if (
            type(head) is not int
            or type(floor) is not int
            or not 0 <= floor <= head < MAX_COUNTER
            or unavailable != 0
        ):
            raise ValueError("invalid scoped sequence state")
        seq = head + 1
        self.db.execute(
            "UPDATE output_observation_scopes SET head=?"
            " WHERE project=? AND session=? AND environment=?",
            (seq, *scope),
        )
        payload = report.model_dump_json().encode() if report is not None else None
        digest = hashlib.sha256(b"output-report-v1\0" + payload).hexdigest() if payload else None
        if binding.binding_unavailable:
            gap = "binding_unavailable"
        if payload is not None:
            if self._room(binding.project_id, len(payload)):
                self.db.execute(
                    "INSERT INTO output_observation_details VALUES(?,?,?)",
                    (execution.execution_id, payload, len(payload)),
                )
            else:
                gap = "quota"
        marker = OutputMarker(
            binding=binding,
            seq=seq,
            state=execution.state,
            received_at=datetime.now(UTC),
            report_sha256=digest,
            gap=gap,
            mutation_coverage="incomplete" if report is not None else "unavailable",
            potentially_partial=execution.state.value != "succeeded",
        )
        self.db.execute(
            "UPDATE output_observation_attempts SET seq=?,marker=? WHERE execution=?",
            (seq, marker.model_dump_json().encode(), execution.execution_id),
        )

    def _room(self, project: str, size: int) -> bool:
        # Only tiny scalar projections enter the bounded aggregate. length(BLOB)
        # reads its size without copying content; TEXT/corrupt counters fail closed.
        count, total, own, invalid = self.db.execute(
            "WITH bounded AS MATERIALIZED(SELECT CASE WHEN typeof(d.size)='integer'"
            " AND d.size BETWEEN 0 AND ? THEN d.size ELSE 0 END AS size,a.project=? AS own,"
            "CASE WHEN typeof(d.size)='integer' AND d.size BETWEEN 0 AND ?"
            " AND typeof(d.payload)='blob' AND a.execution IS NOT NULL"
            " THEN length(d.payload)=d.size ELSE 0 END AS valid"
            " FROM output_observation_details d LEFT JOIN output_observation_attempts a"
            " ON d.execution=a.execution LIMIT ?)"
            " SELECT count(*),coalesce(sum(CASE WHEN valid THEN size ELSE 0 END),0),"
            " coalesce(sum(CASE WHEN valid AND own THEN size ELSE 0 END),0),"
            " coalesce(sum(NOT valid),0) FROM bounded",
            (MAX_REPORT_BYTES, project, MAX_REPORT_BYTES, MAX_DETAILS + 1),
        ).fetchone()
        if invalid or count > MAX_DETAILS:
            return False
        for removed in range(MAX_EVICTIONS + 1):
            if (
                count < MAX_DETAILS
                and total + size <= MAX_DETAIL_BYTES
                and own + size <= MAX_PROJECT_BYTES
            ):
                return True
            if removed == MAX_EVICTIONS or size > min(MAX_DETAIL_BYTES, MAX_PROJECT_BYTES):
                return False
            victim = self.db.execute(
                "SELECT d.rowid,d.size,a.project=? FROM output_observation_details d"
                " JOIN output_observation_attempts a ON a.execution=d.execution"
                " WHERE (?=0 OR a.project=?) ORDER BY d.rowid LIMIT 1",
                (project, int(own + size > MAX_PROJECT_BYTES), project),
            ).fetchone()
            if victim is None:
                return False
            self.db.execute(
                "UPDATE output_observation_scopes SET evicted_through=max(evicted_through,"
                "(SELECT a.seq FROM output_observation_attempts a"
                " JOIN output_observation_details d ON a.execution=d.execution WHERE d.rowid=?))"
                " WHERE (project,session,environment) IN"
                " (SELECT a.project,a.session,a.environment FROM output_observation_attempts a"
                " JOIN output_observation_details d ON a.execution=d.execution WHERE d.rowid=?)",
                (victim[0], victim[0]),
            )
            self.db.execute("DELETE FROM output_observation_details WHERE rowid=?", (victim[0],))
            count -= 1
            total -= victim[1]
            own -= victim[1] if victim[2] else 0
        return False

    def page(
        self,
        scope: OutputScope,
        *,
        after: int = 0,
        target: int | None = None,
        stream_id: str | None = None,
    ) -> OutputPage:
        """Read a bounded scope-local prefix, preserving gaps and explicit feature baseline."""
        if (
            type(after) is not int
            or not 0 <= after <= MAX_COUNTER
            or (target is not None and (type(target) is not int or not 0 <= target <= MAX_COUNTER))
        ):
            raise WorkerError(ErrorKind.INVALID_REQUEST, "invalid observation cursor")
        meta = self.db.execute(
            "SELECT CASE WHEN typeof(generation)='blob' THEN substr(generation,1,65) END,"
            " CASE WHEN typeof(baseline)='blob' THEN substr(baseline,1,65) END"
            " FROM output_observation_meta WHERE singleton=1"
        ).fetchone()
        try:
            generation = bytes(meta[0]).decode()
            if not re.fullmatch(r"[0-9a-f]{64}", generation):
                raise ValueError("invalid generation")
            baseline = datetime.fromisoformat(bytes(meta[1]).decode())
            if baseline.tzinfo is None:
                raise ValueError("invalid baseline")
        except (ValueError, TypeError, IndexError) as exc:
            raise WorkerError(
                ErrorKind.UNAVAILABLE, "invalid observation history metadata"
            ) from exc
        key = (scope.project_id, scope.session_id, scope.environment_id)
        identity = hashlib.sha256(
            b"output-stream-v1\0" + generation.encode() + json.dumps(key).encode()
        ).hexdigest()
        if stream_id is not None and stream_id != identity:
            raise WorkerError(ErrorKind.CONFLICT, "observation stream was reset or changed scope")
        row = self.db.execute(
            "SELECT CASE WHEN typeof(head)='integer' THEN head END,"
            " CASE WHEN typeof(evicted_through)='integer' THEN evicted_through END,"
            " CASE WHEN typeof(unavailable)='integer' THEN unavailable END"
            " FROM output_observation_scopes WHERE project=? AND session=? AND environment=?",
            key,
        ).fetchone()
        head, floor, unavailable = row if row else (0, 0, 0)
        if (
            type(head) is not int
            or type(floor) is not int
            or not 0 <= floor <= head <= MAX_COUNTER
            or unavailable != 0
        ):
            raise WorkerError(ErrorKind.UNAVAILABLE, "invalid observation history counters")
        end = head if target is None else target
        if not after <= end <= head:
            raise WorkerError(
                ErrorKind.INVALID_REQUEST, "observation cursor exceeds scoped history"
            )
        records: list[OutputRecord] = []
        size, last = 2048, after
        rows = self.db.execute(
            "SELECT substr(a.execution,1,129),CASE WHEN typeof(a.seq)='integer' THEN a.seq END,"
            "CASE WHEN typeof(a.marker)='blob' THEN substr(a.marker,1,?) END,"
            "CASE WHEN typeof(a.marker)='blob' THEN length(a.marker) END,"
            "CASE WHEN typeof(d.payload)='blob' THEN substr(d.payload,1,?) END,"
            "CASE WHEN typeof(d.payload)='blob' THEN length(d.payload) END,"
            "CASE WHEN typeof(a.binding)='blob' THEN substr(a.binding,1,?) END,"
            "d.execution IS NOT NULL"
            " FROM output_observation_attempts a LEFT JOIN output_observation_details d"
            " ON a.execution=d.execution WHERE a.project=? AND a.session=? AND a.environment=?"
            " AND a.seq>? AND a.seq<=? ORDER BY a.seq LIMIT 32",
            (MAX_MARKER_BYTES + 1, MAX_REPORT_BYTES + 1, MAX_MARKER_BYTES + 1, *key, after, end),
        )
        for execution_id, seq, marker_raw, marker_size, body, body_size, pin, has_body in rows:
            try:
                if (
                    type(seq) is not int
                    or marker_size is None
                    or marker_size > MAX_MARKER_BYTES
                    or pin is None
                    or len(pin) > MAX_MARKER_BYTES
                ):
                    raise ValueError("oversized marker")
                marker = OutputMarker.model_validate_json(marker_raw)
                if (
                    seq != last + 1
                    or marker.seq != seq
                    or marker.binding.execution_id != execution_id
                    or marker.binding != OutputBinding.model_validate_json(pin)
                    or (
                        marker.binding.project_id,
                        marker.binding.session_id,
                        marker.binding.environment_id,
                    )
                    != key
                ):
                    raise ValueError("incoherent observation binding or sequence")
                report = None
                if has_body:
                    if body is None or body_size is None or body_size > MAX_REPORT_BYTES:
                        raise ValueError("oversized detail")
                    report = OutputReport.model_validate_json(body)
                    digest = hashlib.sha256(
                        b"output-report-v1\0" + report.model_dump_json().encode()
                    ).hexdigest()
                    if digest != marker.report_sha256:
                        raise ValueError("detail digest mismatch")
                elif marker.report_sha256 is not None and marker.gap is None:
                    marker = marker.model_copy(update={"gap": "evicted"})
                record = OutputRecord(marker=marker, report=report)
                encoded = len(record.model_dump_json().encode())
            except (ValueError, TypeError, ValidationError) as exc:
                raise WorkerError(
                    ErrorKind.UNAVAILABLE, "invalid retained output observation"
                ) from exc
            if size + encoded > MAX_PAGE_BYTES:
                break
            records.append(record)
            size += encoded
            last = seq
        if last < end and not records:
            raise WorkerError(ErrorKind.UNAVAILABLE, "observation history prefix is missing")
        return OutputPage(
            **scope.model_dump(),
            stream_id=identity,
            baseline=baseline,
            target=end,
            next_after=last,
            evicted_through=floor,
            records=tuple(records),
        )
