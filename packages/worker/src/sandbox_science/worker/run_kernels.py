# SPDX-License-Identifier: MIT
"""Bounded immutable original run/kernel bindings; no execution routing is installed."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING, cast

from pydantic import ValidationError

from sandbox_science.contracts import ErrorKind
from sandbox_science.contracts.run_kernels import (
    MAX_RUN_KERNEL_RECORD,
    RunKernelBinding,
    RunKernelEnrollment,
    RunKernelScope,
    checked_run_kernel_binding,
    checked_run_kernel_scope,
    run_kernel_binding_sha256,
)

from .errors import WorkerError

if TYPE_CHECKING:
    from .store import Store

# This synchronous component borrows the existing Store transaction and lifetime.
# pyright: reportPrivateUsage=false

MAX_PROJECT_BINDINGS = 256
MAX_BINDINGS = 1024
MAX_PROJECT_BYTES = 2 * 1024 * 1024
MAX_BYTES = 8 * 1024 * 1024
_TABLES = ("run_kernel_binding_meta", "run_kernel_bindings")
_KEYS = ("project", "session", "environment", "parent", "delegation", "child", "operation")
_CHARGE = "+".join(f"length(CAST({name} AS BLOB))" for name in _KEYS) + (
    "+length(CAST(record AS BLOB))+512"
)
_SELECT = (
    "SELECT "
    + ",".join(
        f"CASE WHEN typeof({name})='text' AND length(CAST({name} AS BLOB))<=128 "
        f"THEN CAST({name} AS BLOB) END"
        for name in _KEYS
    )
    + (
        ",CASE WHEN typeof(record)='text' THEN substr(CAST(record AS BLOB),1,4097) END,"
        "CASE WHEN typeof(bytes)='integer' AND bytes BETWEEN 512 AND 5504 THEN bytes END "
        "FROM run_kernel_bindings "
    )
)


def _unavailable() -> WorkerError:
    return WorkerError(ErrorKind.UNAVAILABLE, "original run/kernel binding is unavailable")


def _encoded(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode(
        "ascii"
    )


def _digest(binding: RunKernelBinding, operation: str) -> str:
    raw = _encoded({"binding": binding.model_dump(mode="json"), "operation_id": operation})
    return hashlib.sha256(b"run-kernel-enrollment-v1\0" + raw).hexdigest()


def _operation(value: str) -> str:
    # Reuse a strictly checked bounded identifier without accepting overridable strings.
    try:
        return checked_run_kernel_scope(
            RunKernelScope.model_construct(
                project_id="p",
                session_id="s",
                environment_id="e",
                parent_run_id="parent",
                delegation_id=value,
                child_run_id="child",
            )
        ).delegation_id
    except (ValueError, ValidationError):
        raise WorkerError(ErrorKind.INVALID_REQUEST, "invalid run/kernel operation ID") from None


def _columns(binding: RunKernelBinding, operation: str) -> tuple[str, ...]:
    scope = binding.scope
    return (
        scope.project_id,
        scope.session_id,
        scope.environment_id,
        scope.parent_run_id,
        scope.delegation_id,
        scope.child_run_id,
        operation,
    )


def _pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate record key")
        result[key] = value
    return result


class RunKernelBindings:
    """Original observations only, under a borrowed Store's synchronous ownership.

    No startup migration or auto-enrollment occurs. Explicit initialization cannot
    prove that an entirely erased namespace never existed; callers must not use
    it to repair lost history. No connection, thread or close method is owned here.
    """

    def __init__(self, store: Store) -> None:
        self.store = store

    def endpoint(self, environment_id: str) -> tuple[str, str, str] | None:
        """Bounded typed retained endpoint projection; no current metadata backfill."""
        row = self.store._db.execute(
            "SELECT CASE WHEN typeof(incarnation)='text' THEN "
            "substr(CAST(incarnation AS BLOB),1,29) END,"
            "CASE WHEN typeof(sandbox)='text' THEN substr(CAST(sandbox AS BLOB),1,129) END,"
            "CASE WHEN typeof(sandbox_id)='text' THEN substr(CAST(sandbox_id AS BLOB),1,129) END "
            "FROM executor_endpoints WHERE environment_id=?",
            (environment_id,),
        ).fetchone()
        if row is None or any(type(value) is not bytes for value in row):
            return None
        try:
            values = tuple(cast(bytes, value).decode("ascii") for value in row)
        except UnicodeError:
            return None
        return cast(tuple[str, str, str], values)

    @staticmethod
    def _schema(db: sqlite3.Connection, initialize: bool = False) -> None:
        count = db.execute(
            "SELECT count(*) FROM sqlite_master WHERE type='table' AND name IN "
            "('run_kernel_binding_meta','run_kernel_bindings')"
        ).fetchone()[0]
        if count == 0:
            if not initialize:
                raise WorkerError(ErrorKind.NOT_FOUND, "run/kernel bindings are not initialized")
            db.execute(
                "CREATE TABLE run_kernel_binding_meta(singleton INTEGER PRIMARY KEY "
                "CHECK(singleton=1),version INTEGER NOT NULL) STRICT"
            )
            db.execute("INSERT INTO run_kernel_binding_meta VALUES(1,1)")
            db.execute(
                "CREATE TABLE run_kernel_bindings(project TEXT NOT NULL,session TEXT NOT NULL,"
                "environment TEXT NOT NULL,parent TEXT NOT NULL,delegation TEXT NOT NULL,"
                "child TEXT NOT NULL,operation TEXT NOT NULL,record TEXT NOT NULL,bytes INTEGER "
                "NOT NULL,PRIMARY KEY(project,child),UNIQUE(project,delegation),"
                "UNIQUE(project,session,parent,operation)) STRICT"
            )
            for table in _TABLES:
                for verb in ("UPDATE", "DELETE"):
                    db.execute(
                        f"CREATE TRIGGER {table}_no_{verb.lower()} BEFORE {verb} ON {table} "
                        "BEGIN SELECT RAISE(ABORT,'original run binding is immutable'); END"
                    )
        elif count != 2:
            raise _unavailable()
        rows = db.execute(
            "SELECT CASE WHEN typeof(singleton)='integer' AND singleton=1 "
            "AND typeof(version)='integer' THEN version END "
            "FROM run_kernel_binding_meta LIMIT 2"
        ).fetchall()
        triggers = db.execute(
            "SELECT count(*) FROM sqlite_master WHERE type='trigger' AND name IN "
            "('run_kernel_binding_meta_no_update','run_kernel_binding_meta_no_delete',"
            "'run_kernel_bindings_no_update','run_kernel_bindings_no_delete')"
        ).fetchone()[0]
        if rows != [(1,)] or triggers != 4:
            raise _unavailable()

    @staticmethod
    def _budget(db: sqlite3.Connection, project: str) -> None:
        scalar_checks = " OR ".join(
            f"typeof({name})!='text' OR length(CAST({name} AS BLOB)) NOT BETWEEN 1 AND 128"
            for name in _KEYS
        )
        for selected, count_cap, bytes_cap in (
            (None, MAX_BINDINGS, MAX_BYTES),
            (project, MAX_PROJECT_BINDINGS, MAX_PROJECT_BYTES),
        ):
            count = db.execute(
                "SELECT count(*) FROM run_kernel_bindings WHERE ? IS NULL OR project=?",
                (selected, selected),
            ).fetchone()[0]
            if count > count_cap:
                raise _unavailable()
            count, size, invalid = db.execute(
                "SELECT count(*),coalesce(sum(CASE WHEN typeof(bytes)='integer' AND bytes "  # noqa: S608 - fixed SQL fragments
                "BETWEEN 512 AND 5504 THEN bytes ELSE 0 END),0),coalesce(sum(CASE WHEN "
                + scalar_checks
                + " OR typeof(record)!='text' OR length(CAST(record AS BLOB)) NOT BETWEEN "
                "1 AND 4096 OR typeof(bytes)!='integer' OR bytes NOT BETWEEN 512 AND 5504 "
                "OR bytes!=(" + _CHARGE + ") "
                "THEN 1 ELSE 0 END),0) FROM run_kernel_bindings WHERE ? IS NULL OR project=?",
                (selected, selected),
            ).fetchone()
            reserve = 512 if selected is None else 0
            if invalid or count > count_cap or size + reserve > bytes_cap:
                raise _unavailable()

    @staticmethod
    def _load(row: tuple[object, ...]) -> RunKernelEnrollment:
        try:
            *columns, raw, charge = row
            if (
                any(type(value) is not bytes or len(value) > 128 for value in columns)
                or type(raw) is not bytes
                or len(raw) > MAX_RUN_KERNEL_RECORD
                or type(charge) is not int
            ):
                raise ValueError
            # Reject duplicate retained keys before Pydantic can normalize them.
            json.loads(raw, object_pairs_hook=_pairs)
            value = RunKernelEnrollment.model_validate_json(raw, strict=True)
            binding = checked_run_kernel_binding(value.binding)
            expected = _columns(binding, value.operation_id)
            if (
                tuple(cast(bytes, item).decode("ascii") for item in columns) != expected
                or value.binding_sha256 != run_kernel_binding_sha256(binding)
                or value.request_sha256 != _digest(binding, value.operation_id)
                or charge != sum(len(cast(bytes, item)) for item in columns) + len(raw) + 512
            ):
                raise ValueError
            return value
        except (ValueError, TypeError, ValidationError, RecursionError):
            raise _unavailable() from None

    def enroll(
        self,
        binding: RunKernelBinding,
        operation_id: str,
        *,
        initialize: bool = False,
        validate: Callable[[], None] | None = None,
    ) -> RunKernelEnrollment:
        """Record explicit trusted attribution; never attach, launch or authorize a run.

        A managed caller supplies a synchronous fresh-proof check through validate.
        The lower-level record method itself is not endpoint authentication.
        """
        try:
            binding = checked_run_kernel_binding(binding)
        except (ValueError, ValidationError):
            raise WorkerError(
                ErrorKind.INVALID_REQUEST, "invalid original run/kernel scope"
            ) from None
        operation = _operation(operation_id)
        if type(initialize) is not bool:
            raise WorkerError(ErrorKind.INVALID_REQUEST, "invalid run/kernel initialization flag")
        scope = binding.scope
        with self.store._transaction():
            db = self.store._db
            if validate is not None:
                validate()
            self._schema(db, initialize)
            self._budget(db, scope.project_id)
            rows = db.execute(
                _SELECT + "WHERE project=? AND (child=? OR delegation=? OR "
                "(session=? AND parent=? AND operation=?)) LIMIT 2",
                (
                    scope.project_id,
                    scope.child_run_id,
                    scope.delegation_id,
                    scope.session_id,
                    scope.parent_run_id,
                    operation,
                ),
            ).fetchall()
            if rows:
                if len(rows) != 1:
                    raise WorkerError(
                        ErrorKind.CONFLICT, "run/kernel identity already has a binding"
                    )
                previous = self._load(rows[0])
                if previous.binding != binding or previous.request_sha256 != _digest(
                    binding, operation
                ):
                    raise WorkerError(
                        ErrorKind.CONFLICT, "original run/kernel binding cannot change"
                    )
                value = previous
            else:
                value = RunKernelEnrollment(
                    format_version=1,
                    operation_id=operation,
                    request_sha256=_digest(binding, operation),
                    binding_sha256=run_kernel_binding_sha256(binding),
                    binding=binding,
                    recorded_at=datetime.now(UTC).isoformat(),
                )
                raw = _encoded(value.model_dump(mode="json"))
                if len(raw) > MAX_RUN_KERNEL_RECORD:
                    raise _unavailable()
                columns = _columns(binding, operation)
                charge = sum(len(item.encode("ascii")) for item in columns) + len(raw) + 512
                db.execute(
                    "INSERT INTO run_kernel_bindings VALUES(?,?,?,?,?,?,?,?,?)",
                    (*columns, raw.decode("ascii"), charge),
                )
                self._budget(db, scope.project_id)
            if validate is not None:
                validate()
        return value

    def get(self, scope: RunKernelScope) -> RunKernelEnrollment:
        """Read one original scoped receipt; it says nothing about current eligibility."""
        try:
            scope = checked_run_kernel_scope(scope)
        except (ValueError, ValidationError):
            raise WorkerError(
                ErrorKind.INVALID_REQUEST, "invalid original run/kernel scope"
            ) from None
        self._schema(self.store._db)
        row = self.store._db.execute(
            _SELECT + "WHERE project=? AND child=?", (scope.project_id, scope.child_run_id)
        ).fetchone()
        if row is None:
            raise WorkerError(ErrorKind.NOT_FOUND, "no original binding in this run scope")
        value = self._load(row)
        if value.binding.scope != scope:
            raise WorkerError(ErrorKind.NOT_FOUND, "no original binding in this run scope")
        return value
