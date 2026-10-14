# SPDX-License-Identifier: MIT
# ruff: noqa: S608 - SQL fragments are local schema constants; all caller values are bound.
"""Finite SQLite-only provisioning views; never call a provisioner or guest backend."""

from __future__ import annotations

import contextlib
import hashlib
import json
import re
import sqlite3
from collections.abc import Generator
from datetime import UTC, datetime
from typing import cast

from pydantic import BaseModel, TypeAdapter

from sandbox_science.contracts import ErrorKind, ExecutionContext, Identifier
from sandbox_science.contracts.jsonio import strict_loads
from sandbox_science.contracts.provision_observations import (
    MAX_OBSERVATION_BYTES,
    AdmissionSummary,
    ProvisionDetail,
    ProvisionFilePage,
    ProvisionObservation,
    ProvisionSummary,
    RevisionProbePage,
    RevisionSummary,
    RevisionSummaryPage,
)
from sandbox_science.contracts.provisioning import (
    ProvisionOperation,
    ProvisionRequest,
    ResolvedFile,
)
from sandbox_science.contracts.revisions import EnvironmentRevision, ProbeResult

from .errors import WorkerError
from .package_gate import checked

_MAX_OPERATION = 16 * 1024 * 1024
_MAX_REVISION = 2 * 1024 * 1024
_MAX_INPUT = 32 * 1024 * 1024
_HEAD = 48 * 1024
_ID = TypeAdapter[str](Identifier)
_ARRAYS = {"files": 2000, "changes": 4000, "probes": 64, "executions": 128, "notes": 32}
_SUMMARY = (
    "provision_id",
    "seq",
    "state",
    "outcome",
    "reason",
    "stage",
    "planned_revision",
    "revision_before",
    "revision_after",
    "permission_request",
    "submitted_at",
    "started_at",
    "ended_at",
)
_REVISION = (
    "revision",
    "fingerprint",
    "managed",
    "changed_by",
    "operation",
    "recorded_at",
    "lockfile",
    "omitted_commands",
)


def _unavailable() -> WorkerError:
    return WorkerError(ErrorKind.UNAVAILABLE, "retained provisioning observation is unavailable")


def _json_object(fields: dict[str, str]) -> str:
    # Only constant, locally selected JSON paths and SQL expressions enter this builder.
    return "json_object(" + ",".join(f"'{key}',{value}" for key, value in fields.items()) + ")"


def _fields(names: tuple[str, ...]) -> dict[str, str]:
    return {name: f"json_extract(record,'$.{name}')" for name in names}


def _validate[Model: BaseModel](model: type[Model], value: object) -> Model:
    # JSON validation preserves strict dates/enums, unlike Python coercion.
    return model.model_validate_json(json.dumps(value, ensure_ascii=False), strict=True)


def _parameters(*values: tuple[object, int, int]) -> None:
    if any(type(value) is not int or not low <= value <= high for value, low, high in values):
        raise WorkerError(ErrorKind.INVALID_REQUEST, "invalid observation selection")


class ProvisionObservations:
    """Borrow a Store connection for synchronous bounded read snapshots; own no resources."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.db = connection
        self._bytes = 0

    @contextlib.contextmanager
    def _read(self, project: str, environment: str) -> Generator[dict[str, object]]:
        try:
            _ID.validate_python(project, strict=True)
            _ID.validate_python(environment, strict=True)
            if self.db.in_transaction:
                raise _unavailable()
            self._bytes = 0
            self.db.execute("BEGIN")
            try:
                yield {
                    "project_id": project,
                    "environment_id": environment,
                    "observed_at": datetime.now(UTC).isoformat(),
                }
            finally:
                self.db.rollback()
        except WorkerError:
            raise
        except (ValueError, TypeError, UnicodeError, RecursionError, sqlite3.Error):
            raise _unavailable() from None

    def _meta(self, table: str, where: str, args: tuple[object, ...], maximum: int) -> int:
        row = self.db.execute(
            f"SELECT rowid,typeof(record),length(CAST(record AS BLOB)) FROM {table} WHERE {where}",
            args,
        ).fetchone()
        if row is None:
            raise WorkerError(ErrorKind.NOT_FOUND, "no selected provisioning record")
        row_id, kind, size = row
        if kind != "text" or type(size) is not int or not 0 < size <= maximum:
            raise _unavailable()
        self._bytes += size
        if self._bytes > _MAX_INPUT:
            raise _unavailable()
        # The raw type/byte guard precedes *any* JSON function or blob materialization.
        valid = self.db.execute(
            f"SELECT CASE WHEN json_valid(record) THEN json_type(record) END FROM {table} "
            f"WHERE rowid=?",
            (row_id,),
        ).fetchone()
        if valid is None or valid[0] != "object":
            raise _unavailable()
        return cast(int, row_id)

    def _object(
        self, table: str, row_id: int, expression: str, cap: int = _HEAD
    ) -> dict[str, object]:
        raw = self.db.execute(
            f"SELECT substr(CAST({expression} AS BLOB),1,?) FROM {table} WHERE rowid=?",
            (cap + 1, row_id),
        ).fetchone()
        if raw is None or type(raw[0]) is not bytes or len(raw[0]) > cap:
            raise _unavailable()
        parsed: object = strict_loads(raw[0])
        if type(parsed) is not dict:
            raise _unavailable()
        return cast(dict[str, object], parsed)

    def _shape(
        self, table: str, row_id: int, keys: tuple[str, ...], arrays: dict[str, int]
    ) -> dict[str, int]:
        quoted = ",".join("'" + key + "'" for key in keys)
        bad = self.db.execute(
            f"SELECT count(*) FROM json_each((SELECT record FROM {table} WHERE rowid=?)) "
            f"WHERE key NOT IN ({quoted})",
            (row_id,),
        ).fetchone()
        duplicate = self.db.execute(
            f"SELECT 1 FROM json_each((SELECT record FROM {table} WHERE rowid=?)) GROUP BY "
            f"key HAVING count(*)>1 LIMIT 1",
            (row_id,),
        ).fetchone()
        if bad is None or bad[0] or duplicate:
            raise _unavailable()
        counts: dict[str, int] = {}
        for name, maximum in arrays.items():
            row = self.db.execute(
                f"SELECT json_type(record,'$.{name}'),json_array_length(record,'$.{name}') "
                f"FROM {table} WHERE rowid=?",
                (row_id,),
            ).fetchone()
            if row is None or row[0] not in (None, "array"):
                raise _unavailable()
            count = 0 if row[0] is None else row[1]
            if type(count) is not int or not 0 <= count <= maximum:
                raise _unavailable()
            counts[name + "_total"] = count
        return counts

    def _types(self, table: str, row_id: int, fields: dict[str, tuple[str | None, ...]]) -> None:
        columns = ",".join(f"json_type(record,'$.{name}')" for name in fields)
        values = self.db.execute(
            f"SELECT {columns} FROM {table} WHERE rowid=?", (row_id,)
        ).fetchone()
        if values is None or any(
            value not in allowed for value, allowed in zip(values, fields.values(), strict=True)
        ):
            raise _unavailable()

    def _operation(
        self,
        project: str,
        environment: str,
        provision_id: str,
        since: float,
        seq: int | None = None,
    ) -> tuple[int, ProvisionSummary]:
        _ID.validate_python(provision_id, strict=True)
        row_id = self._meta(
            "provisions",
            "provision_id=? AND environment_id=? AND (typeof(submitted) NOT "
            "IN('real','integer') OR submitted>=?)",
            (provision_id, environment, since),
            _MAX_OPERATION,
        )
        counts = self._shape("provisions", row_id, tuple(ProvisionOperation.model_fields), _ARRAYS)
        self._types(
            "provisions",
            row_id,
            {
                "seq": ("integer",),
                "planned_revision": (None, "null", "integer"),
                "revision_before": (None, "null", "integer"),
                "revision_after": (None, "null", "integer"),
                "permission_request": (None, "null", "integer"),
                "submitted_at": ("text",),
                "started_at": (None, "null", "text"),
                "ended_at": (None, "null", "text"),
            },
        )
        for path, keys in (
            ("$.request", tuple(ProvisionRequest.model_fields)),
            ("$.request.context", tuple(ExecutionContext.model_fields)),
        ):
            quoted = ",".join("'" + key + "'" for key in keys)
            row = self.db.execute(
                "SELECT json_type(record,?) FROM provisions WHERE rowid=?", (path, row_id)
            ).fetchone()
            if row is None or row[0] != "object":
                raise _unavailable()
            unknown = self.db.execute(
                f"SELECT 1 FROM json_each((SELECT record FROM provisions WHERE rowid=?),?) "
                f"WHERE key NOT IN ({quoted}) LIMIT 1",
                (row_id, path),
            ).fetchone()
            duplicate = self.db.execute(
                "SELECT 1 FROM json_each((SELECT record FROM provisions WHERE rowid=?),?) "
                "GROUP BY key HAVING count(*)>1 LIMIT 1",
                (row_id, path),
            ).fetchone()
            if unknown or duplicate:
                raise _unavailable()
        fields = _fields(_SUMMARY)
        fields.update(
            {
                name: f"json_extract(record,'$.request.context.{name}')"
                for name in ("project_id", "environment_id", "session_id")
            }
        )
        fields["operation_id"] = "json_extract(record,'$.request.operation_id')"
        value = self._object("provisions", row_id, _json_object(fields))
        if (
            value.pop("project_id") != project
            or value.pop("environment_id") != environment
            or value.get("provision_id") != provision_id
        ):
            raise _unavailable()
        summary = _validate(ProvisionSummary, {**value, **counts})
        metadata = self.db.execute(
            "SELECT substr(CAST(operation_key AS BLOB),1,1025),typeof(finished),CASE WHEN "
            "typeof(finished)='integer' THEN finished END,typeof(submitted),CASE WHEN "
            "typeof(submitted) IN('real','integer') THEN submitted END FROM provisions WHERE "
            "rowid=?",
            (row_id,),
        ).fetchone()
        expected_key = project + "/" + summary.operation_id
        if (
            metadata is None
            or metadata[0] != expected_key.encode()
            or metadata[1] != "integer"
            or type(metadata[2]) is not int
            or metadata[2] != int(summary.state == "finished")
            or metadata[3] not in ("real", "integer")
            or abs(metadata[4] - summary.submitted_at.timestamp()) > 1e-6
        ):
            raise _unavailable()
        if seq is not None and summary.seq != seq:
            raise WorkerError(ErrorKind.CONFLICT, "selected provisioning sequence changed")
        return row_id, summary

    def summary(self, project: str, environment: str, *, since: float) -> ProvisionObservation:
        """Complete bounded queue summaries and literal package admission state."""
        with self._read(project, environment) as scope:
            total = self.db.execute(
                "SELECT count(*) FROM provisions WHERE environment_id=? AND "
                "(typeof(finished)!='integer' OR finished IS NULL OR finished!=1) AND "
                "(typeof(submitted) NOT IN('real','integer') OR submitted>=?)",
                (environment, since),
            ).fetchone()[0]
            if type(total) is not int or not 0 <= total <= 32:
                raise _unavailable()
            rows = self.db.execute(
                "SELECT substr(CAST(provision_id AS BLOB),1,129) FROM provisions WHERE "
                "environment_id=? AND (typeof(finished)!='integer' OR finished IS NULL OR "
                "finished!=1) AND (typeof(submitted) NOT IN('real','integer') OR "
                "submitted>=?) ORDER BY rowid LIMIT 33",
                (environment, since),
            ).fetchall()
            if any(type(row[0]) is not bytes or not 0 < len(row[0]) <= 128 for row in rows):
                raise _unavailable()
            operations = [
                self._operation(project, environment, row[0].decode("ascii"), since)[1]
                for row in rows
            ]
            # SQLite substr of an empty BLOB yields NULL on supported versions.
            # Preserve the producer's ready TEXT '' without admitting real NULL/BLOBs.
            gate_row = self.db.execute(
                "SELECT CASE WHEN typeof(generation)='integer' THEN generation "
                "END,substr(CAST(mode AS BLOB),1,17),CASE WHEN typeof(operation)='text' "
                "THEN coalesce(substr(CAST(operation AS BLOB),1,129),X'') END,"
                "CASE WHEN typeof(revision)='integer' THEN revision "
                "END,typeof(revision) FROM package_execution_gates WHERE environment_id=?",
                (environment,),
            ).fetchone()
            if gate_row:
                if (
                    gate_row[4] not in ("null", "integer")
                    or type(gate_row[1]) is not bytes
                    or type(gate_row[2]) is not bytes
                ):
                    raise _unavailable()
                gate = checked(
                    gate_row[0],
                    gate_row[1].decode("ascii"),
                    gate_row[2].decode("ascii"),
                    gate_row[3],
                )
            else:
                marked = self.db.execute(
                    "SELECT 1 FROM package_status WHERE environment_id=?", (environment,)
                ).fetchone()
                if marked or any(operation.state == "installing" for operation in operations):
                    raise _unavailable()
                gate = checked(0, "ready", "", None)
            if gate.operation and not any(o.provision_id == gate.operation for o in operations):
                self._operation(project, environment, gate.operation, since)
            revision_row = self.db.execute(
                "SELECT CASE WHEN typeof(r.revision)='integer' THEN r.revision END,"
                "substr(CAST(r.fingerprint AS BLOB),1,33) FROM "
                "environment_revisions r JOIN revision_entries e ON "
                "e.environment_id=r.environment_id AND e.revision=r.revision WHERE "
                "r.environment_id=? AND (typeof(e.recorded) NOT IN('real','integer') OR "
                "e.recorded>=?)",
                (environment, since),
            ).fetchone()
            if revision_row:
                current = self._revision(environment, revision_row[0], since)[1]
                if revision_row[1] != current.fingerprint.encode("ascii"):
                    raise _unavailable()
            return _validate(
                ProvisionObservation,
                {
                    **scope,
                    "gate": AdmissionSummary(
                        generation=gate.generation,
                        mode=gate.mode,
                        operation=gate.operation or None,
                        revision=gate.revision,
                        recorded=gate_row is not None,
                    ).model_dump(),
                    "current_revision": revision_row[0] if revision_row else None,
                    "queue_total": total,
                    "operations": [o.model_dump(mode="json") for o in operations],
                },
            )

    def detail(
        self, project: str, environment: str, provision_id: str, *, since: float, seq: int | None
    ) -> ProvisionDetail:
        if seq is not None:
            _parameters((seq, 0, 2**62))
        with self._read(project, environment) as scope:
            row_id, summary = self._operation(project, environment, provision_id, since, seq)
            self._types("provisions", row_id, {"error": (None, "null", "object")})
            # Do not materialize an error body. Its selected kind must have one unambiguous
            # original text key; json_extract alone would silently choose the first one.
            error = self.db.execute(
                "SELECT json_type(record,'$.error'),(SELECT count(*) FROM json_each("
                "record,'$.error') WHERE key='kind'),(SELECT count(*) FROM json_each("
                "record,'$.error') WHERE key='kind' AND type='text') FROM provisions "
                "WHERE rowid=?",
                (row_id,),
            ).fetchone()
            if error is None or (error[0] == "object" and error[1:] != (1, 1)):
                raise _unavailable()
            fields = _fields(("request", "policy", "catalog", "next_step"))
            fields.update(
                error_present=(
                    "json_type(record,'$.error') IS NOT NULL "
                    "AND json_type(record,'$.error')!='null'"
                ),
                error_kind="json_extract(record,'$.error.kind')",
            )
            value = self._object("provisions", row_id, _json_object(fields))
            value["error_present"] = bool(value["error_present"])
            return _validate(
                ProvisionDetail, {**scope, "summary": summary.model_dump(mode="json"), **value}
            )

    def _items[Model: BaseModel](
        self, table: str, row_id: int, section: str, offset: int, limit: int, model: type[Model]
    ) -> list[Model]:
        rows = self.db.execute(
            f"SELECT key,type,substr(CAST(value AS BLOB),1,16385) FROM json_each((SELECT record "
            f"FROM {table} WHERE rowid=?),'$.{section}') WHERE key>=? ORDER BY key LIMIT ?",
            (row_id, offset, limit),
        ).fetchall()
        result: list[Model] = []
        budget = 4096
        for index, (key, kind, raw) in enumerate(rows, offset):
            if (
                type(key) is not int
                or key != index
                or kind != "object"
                or type(raw) is not bytes
                or len(raw) > 16384
            ):
                raise _unavailable()
            # The original element type and clipped bytes are checked before parsing.
            # Reject duplicates recursively, then retain strict JSON date/enum validation.
            strict_loads(raw)
            item = model.model_validate_json(raw, strict=True)
            encoded = item.model_dump_json().encode("utf-8")
            if budget + len(encoded) + 1 > MAX_OBSERVATION_BYTES:
                break
            result.append(item)
            budget += len(encoded) + 1
        return result

    def files(
        self,
        project: str,
        environment: str,
        provision_id: str,
        *,
        since: float,
        seq: int,
        offset: int,
        limit: int,
    ) -> ProvisionFilePage:
        _parameters((seq, 0, 2**62), (offset, 0, 2000), (limit, 1, 8))
        with self._read(project, environment) as scope:
            row_id, summary = self._operation(project, environment, provision_id, since, seq)
            if offset > summary.files_total:
                raise WorkerError(ErrorKind.INVALID_REQUEST, "file offset exceeds selected total")
            files = self._items("provisions", row_id, "files", offset, limit, ResolvedFile)
            end = offset + len(files)
            return _validate(
                ProvisionFilePage,
                {
                    **scope,
                    "provision_id": provision_id,
                    "seq": seq,
                    "total": summary.files_total,
                    "offset": offset,
                    "limit": limit,
                    "next_offset": end if end < summary.files_total else None,
                    "files": [f.model_dump(mode="json") for f in files],
                },
            )

    def _pin(self, row_id: int) -> str:
        digest = hashlib.sha256()
        with self.db.blobopen("revision_entries", "record", row_id, readonly=True) as blob:
            while chunk := blob.read(65536):
                digest.update(chunk)
        return digest.hexdigest()

    def _revision(
        self, environment: str, revision: int, since: float
    ) -> tuple[int, RevisionSummary]:
        row_id = self._meta(
            "revision_entries",
            "environment_id=? AND revision=? AND (typeof(recorded) NOT IN('real','integer') "
            "OR recorded>=?)",
            (environment, revision, since),
            _MAX_REVISION,
        )
        counts = self._shape(
            "revision_entries",
            row_id,
            tuple(EnvironmentRevision.model_fields),
            {"commands": 8, "probes": 64},
        )
        self._types(
            "revision_entries",
            row_id,
            {"revision": ("integer",), "omitted_commands": ("integer",), "recorded_at": ("text",)},
        )
        fields = _fields(_REVISION)
        fields["managed"] = "json(COALESCE(json_type(record,'$.managed'),'null'))"
        value = self._object("revision_entries", row_id, _json_object(fields))
        summary = _validate(
            RevisionSummary, {**value, **counts, "record_sha256": self._pin(row_id)}
        )
        if summary.revision != revision:
            raise _unavailable()
        row = self.db.execute(
            "SELECT typeof(recorded),CASE WHEN typeof(recorded) IN('real','integer') THEN "
            "recorded END,substr(CAST(lockfile AS BLOB),1,65),CASE WHEN "
            "typeof(lockfile_size)='integer' THEN lockfile_size END FROM revision_entries "
            "WHERE rowid=?",
            (row_id,),
        ).fetchone()
        if (
            row is None
            or row[0] not in ("real", "integer")
            or abs(row[1] - summary.recorded_at.timestamp()) > 1e-6
            or (row[2], row[3])
            != (
                (summary.lockfile.sha256.encode(), summary.lockfile.size)
                if summary.lockfile
                else (None, 0)
            )
        ):
            raise _unavailable()
        return row_id, summary

    def revisions(
        self,
        project: str,
        environment: str,
        *,
        since: float,
        after: int,
        limit: int,
        target: int | None,
    ) -> RevisionSummaryPage:
        _parameters((after, -1, 2**62), (limit, 1, 8))
        if target is not None:
            _parameters((target, 0, 2**62))
        with self._read(project, environment) as scope:
            row = self.db.execute(
                "SELECT count(*),min(CASE WHEN typeof(revision)='integer' THEN revision "
                "END),max(CASE WHEN typeof(revision)='integer' THEN revision "
                "END),sum(typeof(revision)!='integer') FROM revision_entries WHERE "
                "environment_id=? AND (typeof(recorded) NOT IN('real','integer') OR "
                "recorded>=?)",
                (environment, since),
            ).fetchone()
            total, first, latest, invalid = row
            if invalid:
                raise _unavailable()
            if (
                type(total) is not int
                or not 0 <= total <= 512
                or any(
                    v is not None and (type(v) is not int or not 0 <= v <= 2**62)
                    for v in (first, latest)
                )
            ):
                raise _unavailable()
            if target is not None and (
                latest is None or first is None or not first <= target <= latest
            ):
                raise WorkerError(
                    ErrorKind.CONFLICT, "selected revision window is no longer retained"
                )
            selected = latest if target is None else target
            rows = self.db.execute(
                "SELECT revision FROM revision_entries WHERE environment_id=? AND "
                "(typeof(recorded) NOT IN('real','integer') OR recorded>=?) AND revision>? "
                "AND revision<=? ORDER BY revision LIMIT ?",
                (environment, since, after, selected if selected is not None else -1, limit + 1),
            ).fetchall()
            entries = [self._revision(environment, r[0], since)[1] for r in rows[:limit]]
            return _validate(
                RevisionSummaryPage,
                {
                    **scope,
                    "retained_total": total,
                    "first_retained": first,
                    "target": selected,
                    "after": after,
                    "limit": limit,
                    "next_after": entries[-1].revision if len(rows) > limit else None,
                    "revisions": [e.model_dump(mode="json") for e in entries],
                },
            )

    def probes(
        self,
        project: str,
        environment: str,
        revision: int,
        *,
        since: float,
        pin: str,
        offset: int,
        limit: int,
    ) -> RevisionProbePage:
        _parameters((revision, 0, 2**62), (offset, 0, 64), (limit, 1, 8))
        if type(pin) is not str or re.fullmatch(r"[0-9a-f]{64}", pin) is None:
            raise WorkerError(ErrorKind.INVALID_REQUEST, "invalid revision pin")
        with self._read(project, environment) as scope:
            row_id, summary = self._revision(environment, revision, since)
            if summary.record_sha256 != pin:
                raise WorkerError(ErrorKind.CONFLICT, "selected revision record changed")
            if offset > summary.probes_total:
                raise WorkerError(ErrorKind.INVALID_REQUEST, "probe offset exceeds selected total")
            probes = self._items("revision_entries", row_id, "probes", offset, limit, ProbeResult)
            end = offset + len(probes)
            return _validate(
                RevisionProbePage,
                {
                    **scope,
                    "revision": revision,
                    "record_sha256": pin,
                    "total": summary.probes_total,
                    "offset": offset,
                    "limit": limit,
                    "next_offset": end if end < summary.probes_total else None,
                    "probes": [p.model_dump(mode="json") for p in probes],
                },
            )

    def selected_revision(
        self,
        project: str,
        environment: str,
        *,
        since: float,
        revision: int,
        pin: str,
    ) -> RevisionSummary:
        """Pin one bounded canonical header in one read snapshot, without reading its blob."""
        return self._selected_revisions(project, environment, since, ((revision, pin),))[0]

    def selected_pair(
        self,
        project: str,
        environment: str,
        *,
        since: float,
        before: tuple[int, str],
        after: tuple[int, str],
    ) -> tuple[RevisionSummary, RevisionSummary]:
        """Pin exactly two caller-selected retained headers in one bounded read snapshot."""
        selected = self._selected_revisions(project, environment, since, (before, after))
        return selected[0], selected[1]

    def _selected_revisions(
        self,
        project: str,
        environment: str,
        since: float,
        selections: tuple[tuple[int, str], ...],
    ) -> tuple[RevisionSummary, ...]:
        for revision, pin in selections:
            _parameters((revision, 0, 2**62))
            if type(pin) is not str or re.fullmatch(r"[0-9a-f]{64}", pin) is None:
                raise WorkerError(ErrorKind.INVALID_REQUEST, "invalid selected revision pin")
        with self._read(project, environment):
            selected = tuple(
                self._revision(environment, revision, since)[1] for revision, _ in selections
            )
            if any(
                row.record_sha256 != pin for row, (_, pin) in zip(selected, selections, strict=True)
            ):
                raise WorkerError(ErrorKind.CONFLICT, "selected revision record changed")
            return selected
