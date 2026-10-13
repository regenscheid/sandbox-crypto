# SPDX-License-Identifier: MIT
"""Selected child attempts and lineage, distinct from all legacy kernel records."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Literal, Self, cast

from pydantic import ConfigDict, Field, ValidationError, model_validator

from sandbox_science.contracts import TERMINAL_STATES, Contract, ErrorKind, Execution, KernelInfo
from sandbox_science.contracts.kernel_lineage import KernelBirthPin
from sandbox_science.contracts.run_cells import (
    MAX_RUN_REQUEST_BYTES,
    RunCellRequest,
    run_cell_bytes,
    run_kernel_id,
)
from sandbox_science.contracts.run_kernels import (
    RunKernelBinding,
    checked_run_kernel_binding,
    run_kernel_binding_sha256,
)

from .errors import WorkerError
from .run_kernels import RunKernelBindings

if TYPE_CHECKING:
    from .store import Store

# All calls are synchronous under the existing owner; no thread/close ownership.
# pyright: reportPrivateUsage=false
MAX_RUN_ATTEMPTS = 1024
MAX_RUN_CONTROLS = 1024
MAX_RUN_BYTES = 128 * 1024 * 1024
_TABLES = (
    "run_dispatch_meta",
    "run_cell_selections",
    "run_kernel_generations",
    "run_kernel_births",
    "run_kernel_controls",
    "run_kernel_closures",
)
_STRING = re.compile(rb'"[^"\\]*(?:\\.[^"\\]*)*"')


class RunSelection(Contract):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    execution_id: str = Field(pattern=r"^exe-[0-9a-f]{24}$")
    attempt_id: str = Field(pattern=r"^att-[0-9a-f]{24}$")
    request_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    request_bytes: int = Field(strict=True, ge=1, le=MAX_RUN_REQUEST_BYTES)
    binding: RunKernelBinding
    language: Literal["python", "sage"]


class RunControl(Contract):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    binding: RunKernelBinding
    operation_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
    action: Literal["restart", "close"]
    language: Literal["python", "sage"] | None
    recorded_at: str = Field(max_length=40)

    @model_validator(mode="after")
    def _action(self) -> Self:
        if (self.action == "restart" and self.language is None) or (
            self.action == "close" and self.language is not None
        ):
            raise ValueError("child control language differs from its action")
        return self


class RunControlReceipt(Contract):
    request: RunControl
    state: Literal["pending", "done"]
    kernel: KernelInfo | None = None


def unavailable() -> WorkerError:
    return WorkerError(ErrorKind.UNAVAILABLE, "original child kernel selection is unavailable")


def _pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate retained key")
        result[key] = value
    return result


def strict_json(raw: bytes, cap: int) -> object:
    if type(raw) is not bytes or len(raw) > cap:
        raise ValueError("retained child data exceeds its cap")
    if raw.count(b"{") + raw.count(b"[") > 4096:
        outside = _STRING.sub(b"", raw)
        if outside.count(b"{") + outside.count(b"[") > 4096:
            raise ValueError("retained child data structure exceeds its cap")
    return json.loads(
        raw.decode("utf-8"),
        object_pairs_hook=_pairs,
        parse_constant=lambda _: (_ for _ in ()).throw(ValueError()),
    )


def checked_control(value: RunControl) -> RunControl:
    if type(value) is not RunControl:
        raise unavailable()
    data = object.__getattribute__(value, "__dict__")
    if type(data) is not dict or object.__getattribute__(value, "__pydantic_extra__") is not None:
        raise unavailable()
    data = cast(dict[str, object], data)
    if (
        len(data) != len(RunControl.model_fields)
        or any(type(key) is not str for key in data)
        or set(data) != set(RunControl.model_fields)
    ):
        raise unavailable()
    for name, cap in (("operation_id", 128), ("action", 7), ("language", 6), ("recorded_at", 40)):
        item = data[name]
        if name == "language" and item is None:
            continue
        if type(item) is not str or len(item) > cap or not item.isascii():
            raise unavailable()
    try:
        binding = checked_run_kernel_binding(cast(RunKernelBinding, data["binding"]))
        checked = RunControl.model_validate({**data, "binding": binding}, strict=True)
        stamp = datetime.fromisoformat(checked.recorded_at)
        if stamp.utcoffset() != UTC.utcoffset(stamp):
            raise ValueError
        return checked
    except (ValueError, TypeError, ValidationError):
        raise unavailable() from None


def checked_info(value: KernelInfo) -> KernelInfo:
    if type(value) is not KernelInfo:
        raise unavailable()
    data = object.__getattribute__(value, "__dict__")
    if type(data) is not dict or object.__getattribute__(value, "__pydantic_extra__") is not None:
        raise unavailable()
    data = cast(dict[str, object], data)
    if (
        len(data) != len(KernelInfo.model_fields)
        or any(type(key) is not str for key in data)
        or set(data) != set(KernelInfo.model_fields)
    ):
        raise unavailable()
    for name, cap in (("kernel_id", 128), ("language", 6), ("revision_status", 8)):
        item = data[name]
        if type(item) is not str or len(item) > cap or not item.isascii():
            raise unavailable()
    for name, low, high in (
        ("generation", 1, 2**31 - 1),
        ("start_revision", 0, 2**62),
        ("current_revision", 0, 2**62),
    ):
        item = data[name]
        if name != "generation" and item is None:
            continue
        if type(item) is not int or not low <= item <= high:
            raise unavailable()
    try:
        return KernelInfo.model_validate(data, strict=True)
    except (ValueError, TypeError, ValidationError):
        raise unavailable() from None


class RunKernelState:
    def __init__(self, store: Store) -> None:
        self.store = store

    def initialize(self) -> None:
        """Explicit trusted first setup; never automatic missing-history recovery."""
        with self.store._transaction():
            db = self.store._db
            present = db.execute(
                "SELECT count(*) FROM sqlite_master WHERE type='table' AND name IN "
                "('run_dispatch_meta','run_cell_selections','run_kernel_generations',"
                "'run_kernel_births','run_kernel_controls','run_kernel_closures')"
            ).fetchone()[0]
            if present == 0:
                # A canonical attempt marker proves this is not first setup.
                if db.execute(
                    "SELECT 1 FROM executions WHERE kernel_scope!='legacy' LIMIT 1"
                ).fetchone():
                    raise unavailable()
                db.execute("CREATE TABLE run_dispatch_meta(version INTEGER NOT NULL) STRICT")
                db.execute("INSERT INTO run_dispatch_meta VALUES(1)")
                db.execute(
                    "CREATE TABLE run_cell_selections(execution TEXT PRIMARY KEY,project TEXT "
                    "NOT NULL,record TEXT NOT NULL) STRICT"
                )
                db.execute(
                    "CREATE TABLE run_kernel_generations(binding TEXT NOT NULL,project TEXT "
                    "NOT NULL,language TEXT NOT NULL,last INTEGER NOT NULL,"
                    "PRIMARY KEY(binding,language)) STRICT"
                )
                db.execute(
                    "CREATE TABLE run_kernel_births(binding TEXT NOT NULL,project"
                    " TEXT NOT NULL,language TEXT NOT NULL,record TEXT NOT NULL,P"
                    "RIMARY KEY(binding,language)) STRICT"
                )
                db.execute(
                    "CREATE TABLE run_kernel_controls(binding TEXT NOT NULL,proje"
                    "ct TEXT NOT NULL,operation TEXT NOT NULL,action TEXT NOT NUL"
                    "L,request_sha TEXT NOT NULL,record TEXT NOT NULL,state TEXT "
                    "NOT NULL,result TEXT,PRIMARY KEY(binding,operation)) STRICT"
                )
                db.execute(
                    "CREATE TABLE run_kernel_closures(binding TEXT PRIMARY KEY,pr"
                    "oject TEXT NOT NULL,"
                    "recorded TEXT NOT NULL) STRICT"
                )
                for table in ("run_dispatch_meta", "run_cell_selections", "run_kernel_closures"):
                    for verb in ("UPDATE", "DELETE"):
                        db.execute(
                            f"CREATE TRIGGER {table}_no_{verb.lower()} BEFORE {verb} "
                            f"ON {table} BEGIN SELECT RAISE(ABORT,'child identity is immutable'); "
                            "END"
                        )
                db.execute(
                    "CREATE TRIGGER run_controls_identity BEFORE UPDATE OF bindin"
                    "g,project,operation,action,request_sha,record ON run_kernel_"
                    "controls BEGIN SELECT RAISE(ABORT,'child control identity is"
                    " immutable'); END"
                )
                db.execute(
                    "CREATE TRIGGER run_controls_no_delete BEFORE DELETE ON run_kernel_controls "
                    "BEGIN SELECT RAISE(ABORT,'child control is immutable'); END"
                )
                db.execute(
                    "CREATE TRIGGER run_controls_once BEFORE UPDATE OF state,resu"
                    "lt ON run_kernel_controls "
                    "WHEN OLD.state!='pending' OR NEW.state!='done' "
                    "BEGIN SELECT RAISE(ABORT,'child control outcome is immutable'); END"
                )
            self.require()
            self.budget()

    def require(self) -> None:
        db = self.store._db
        found = db.execute(
            "SELECT count(*) FROM sqlite_master WHERE type='table' AND name IN "
            "('run_dispatch_meta','run_cell_selections','run_kernel_generations',"
            "'run_kernel_births','run_kernel_controls','run_kernel_closures')"
        ).fetchone()[0]
        if found != len(_TABLES):
            raise unavailable()
        rows = db.execute(
            "SELECT CASE WHEN typeof(version)='integer' THEN version END "
            "FROM run_dispatch_meta LIMIT 2"
        ).fetchall()
        triggers = db.execute(
            "SELECT count(*) FROM sqlite_master WHERE type='trigger' AND name IN "
            "('run_dispatch_meta_no_update','run_dispatch_meta_no_delete',"
            "'run_cell_selections_no_update','run_cell_selections_no_delete',"
            "'run_kernel_closures_no_update','run_kernel_closures_no_delete',"
            "            'run_controls_identity','run_controls_no_delete','run_controls_once')"
        ).fetchone()[0]
        if rows != [(1,)] or triggers != 9:
            raise unavailable()

    def budget(self) -> None:
        """Count every retained row and original envelope, with preaggregate caps."""
        self.require()
        db = self.store._db
        total = 512
        for table, cap, column in (
            ("run_cell_selections", MAX_RUN_ATTEMPTS, "record"),
            ("run_kernel_controls", MAX_RUN_CONTROLS, "record"),
            ("run_kernel_generations", 2048, None),
            ("run_kernel_births", 2048, "record"),
            ("run_kernel_closures", 1024, "recorded"),
        ):
            count = db.execute(f"SELECT count(*) FROM {table}").fetchone()[0]  # noqa: S608 - fixed internal names
            if count > cap:
                raise unavailable()
            scalar_columns = {
                "run_cell_selections": (("execution", 28), ("project", 128)),
                "run_kernel_controls": (
                    ("binding", 64),
                    ("project", 128),
                    ("operation", 128),
                    ("action", 7),
                    ("request_sha", 64),
                    ("state", 7),
                ),
                "run_kernel_generations": (("binding", 64), ("project", 128), ("language", 6)),
                "run_kernel_births": (("binding", 64), ("project", 128), ("language", 6)),
                "run_kernel_closures": (("binding", 64), ("project", 128)),
            }[table]
            conditions = [
                f"typeof({name})!='text' OR length(CAST({name} AS BLOB)) NOT BETWEEN 1 AND {bound}"
                for name, bound in scalar_columns
            ]
            if table in ("run_kernel_generations", "run_kernel_births"):
                conditions.append("language NOT IN ('python','sage')")
            if table == "run_kernel_generations":
                conditions.append("typeof(last)!='integer' OR last NOT BETWEEN 1 AND 2147483647")
            if table == "run_kernel_controls":
                conditions.extend(
                    (
                        "action NOT IN ('restart','close')",
                        "state NOT IN ('pending','done')",
                        "length(binding)!=64 OR binding GLOB '*[^0-9a-f]*'",
                        "length(request_sha)!=64 OR request_sha GLOB '*[^0-9a-f]*'",
                    )
                )
            if db.execute(
                f"SELECT 1 FROM {table} WHERE " + " OR ".join(conditions) + " LIMIT 1"  # noqa: S608 - fixed internal names
            ).fetchone():
                raise unavailable()
            total += count * 1024  # validated finite keys/scalars + row/index reservation
            if column is not None:
                cap = 40 if table == "run_kernel_closures" else 4096
                size, bad = db.execute(
                    f"SELECT coalesce(sum(CASE WHEN typeof({column})='text' AND "  # noqa: S608 - fixed internal names
                    f"length(CAST({column} AS BLOB))<={cap} THEN length(CAST({column} AS BLOB)) "
                    f"ELSE 0 END),0),coalesce(sum(typeof({column})!='text' OR "
                    f"length(CAST({column} AS BLOB))>{cap}),0) FROM {table}"
                ).fetchone()
                if bad:
                    raise unavailable()
                total += size
        if db.execute(
            "SELECT 1 FROM run_cell_selections s LEFT JOIN executions e O"
            "N e.execution_id=s.execution "
            "WHERE e.execution_id IS NULL OR e.kernel_scope!='run-v1' LIMIT 1"
        ).fetchone():
            raise unavailable()
        if (
            db.execute("SELECT count(*) FROM executions WHERE kernel_scope='run-v1'").fetchone()[0]
            > MAX_RUN_ATTEMPTS
        ):
            raise unavailable()
        # The original request lives exactly once in the existing recovery row.
        size, bad = db.execute(
            "SELECT coalesce(sum(CASE WHEN typeof(r.request)='text' AND l"
            "ength(CAST(r.request AS BLOB))<=2097152 THEN length(CAST(r.r"
            "equest AS BLOB)) ELSE 0 END),0),coalesce(sum(s.execution IS "
            "NULL OR r.execution_id IS NULL OR typeof(r.request)!='text' "
            "OR length(CAST(r.request AS BLOB))>2097152),0) FROM executio"
            "ns e LEFT JOIN run_cell_selections s ON s.execution=e.execut"
            "ion_id LEFT JOIN execution_recovery r ON r.execution_id=e.ex"
            "ecution_id WHERE e.kernel_scope='run-v1'"
        ).fetchone()
        if bad or total + size > MAX_RUN_BYTES:
            raise unavailable()
        total += size
        size, bad = db.execute(
            "SELECT coalesce(sum(CASE WHEN result IS NOT NULL AND typeof(result)='text' AND "
            "length(CAST(result AS BLOB))<=4096 THEN length(CAST(result AS BLOB)) ELSE 0 END),0),"
            "coalesce(sum(result IS NOT NULL AND (typeof(result)!='text' OR "
            "length(CAST(result AS BLOB))>4096)),0) FROM run_kernel_controls"
        ).fetchone()
        if bad or total + size > MAX_RUN_BYTES:
            raise unavailable()

    def accept(self, execution: Execution, request: RunCellRequest, raw: bytes) -> None:
        """Called inside the same transaction as canonical attempt/request insertion."""
        header = RunSelection(
            execution_id=execution.execution_id,
            attempt_id=execution.attempt_id,
            request_sha256=hashlib.sha256(raw).hexdigest(),
            request_bytes=len(raw),
            binding=request.binding,
            language=request.language,
        )
        self.store._db.execute(
            "INSERT INTO run_cell_selections VALUES(?,?,?)",
            (
                execution.execution_id,
                execution.context.project_id,
                header.model_dump_json(),
            ),
        )
        self.budget()

    def request(self, execution: Execution) -> RunCellRequest:
        self.require()
        if self.store.kernel_scope(execution.execution_id) != "run-v1":
            raise unavailable()
        row = self.store._db.execute(
            "SELECT CASE WHEN typeof(s.record)='text' THEN substr(CAST(s."
            "record AS BLOB),1,4097) END,"
            "CASE WHEN typeof(r.request)='text' THEN substr(CAST(r.request AS BLOB),1,2097153) END,"
            "CASE WHEN typeof(e.request_digest)='text' THEN substr(CAST(e"
            ".request_digest AS BLOB),1,65) END,"
            "CASE WHEN typeof(s.project)='text' THEN substr(CAST(s.project AS BLOB),1,129) END,"
            "CASE WHEN typeof(e.environment_id)='text' THEN substr(CAST(e"
            ".environment_id AS BLOB),1,129) END,"
            "CASE WHEN typeof(e.session_id)='text' THEN substr(CAST(e.ses"
            "sion_id AS BLOB),1,129) END,"
            "CASE WHEN typeof(e.operation_id)='text' THEN substr(CAST(e.o"
            "peration_id AS BLOB),1,258) END,"
            "CASE WHEN typeof(e.terminal)='integer' THEN e.terminal END "
            "FROM run_cell_selections s JOIN execution_recovery r ON r.execution_id=s.execution "
            "JOIN executions e ON e.execution_id=s.execution WHERE s.execution=?",
            (execution.execution_id,),
        ).fetchone()
        if row is None:
            raise unavailable()
        try:
            (
                header_raw,
                request_raw,
                digest_raw,
                project,
                environment,
                session,
                operation,
                terminal,
            ) = row
            if type(digest_raw) is not bytes or re.fullmatch(rb"[0-9a-f]{64}", digest_raw) is None:
                raise ValueError
            digest = digest_raw.decode("ascii")
            strict_json(header_raw, 4096)
            strict_json(request_raw, MAX_RUN_REQUEST_BYTES)
            header = RunSelection.model_validate_json(header_raw, strict=True)
            request = RunCellRequest.model_validate_json(request_raw, strict=True)
            canonical = run_cell_bytes(request)
            enrolled = RunKernelBindings(self.store).get(request.binding.scope)
            if (
                project != execution.context.project_id.encode("ascii")
                or environment != execution.context.environment_id.encode("ascii")
                or session != execution.context.session_id.encode("ascii")
                or operation
                != f"{execution.context.project_id}/{execution.operation_id}".encode("ascii")
                or type(terminal) is not int
                or terminal != int(execution.state in TERMINAL_STATES)
                or canonical != request_raw
                or hashlib.sha256(canonical).hexdigest() != digest
                or header.request_sha256 != digest
                or header.request_bytes != len(canonical)
                or header.execution_id != execution.execution_id
                or header.attempt_id != execution.attempt_id
                or request.context != execution.context
                or request.operation_id != execution.operation_id
                or header.binding != request.binding
                or header.language != request.language
                or enrolled.binding != request.binding
            ):
                raise ValueError
            return request
        except (ValueError, TypeError, ValidationError, RecursionError, WorkerError):
            raise unavailable() from None

    def closed(self, binding: RunKernelBinding) -> bool:
        self.require()
        key = run_kernel_binding_sha256(binding)
        row = self.store._db.execute(
            (
                "SELECT CASE WHEN typeof(recorded)='text' THEN substr(CAST(re"
                "corded AS BLOB),1,41) END,CASE WHEN typeof(project)='text' T"
                "HEN CASE WHEN length(CAST(project AS BLOB))<=128 THEN CAST(p"
                "roject AS BLOB) END END FROM run_kernel_closures WHERE bindi"
                "ng=?"
            ),
            (key,),
        ).fetchone()
        if row is None:
            return False
        try:
            stamp = datetime.fromisoformat(row[0].decode("ascii"))
            if (
                row[1] != binding.scope.project_id.encode("ascii")
                or len(row[0]) > 40
                or stamp.utcoffset() != UTC.utcoffset(stamp)
            ):
                raise ValueError
        except (ValueError, TypeError, AttributeError, UnicodeError):
            raise unavailable() from None
        return True

    def generation(self, binding: RunKernelBinding, language: str) -> int | None:
        self.require()
        row = self.store._db.execute(
            (
                "SELECT CASE WHEN typeof(last)='integer' THEN last END,CASE W"
                "HEN typeof(project)='text' THEN CASE WHEN length(CAST(projec"
                "t AS BLOB))<=128 THEN CAST(project AS BLOB) END END FROM run"
                "_kernel_generations WHERE binding=? AND language=?"
            ),
            (run_kernel_binding_sha256(binding), language),
        ).fetchone()
        if row is None:
            return None
        if (
            row[1] != binding.scope.project_id.encode("ascii")
            or type(row[0]) is not int
            or not 1 <= row[0] < 2**31
        ):
            raise unavailable()
        return row[0]

    def birth(self, binding: RunKernelBinding, language: str) -> KernelBirthPin | None:
        self.require()
        row = self.store._db.execute(
            (
                "SELECT CASE WHEN typeof(record)='text' THEN substr(CAST(reco"
                "rd AS BLOB),1,2049) END,CASE WHEN typeof(project)='text' THE"
                "N CASE WHEN length(CAST(project AS BLOB))<=128 THEN CAST(pro"
                "ject AS BLOB) END END FROM run_kernel_births WHERE binding=?"
                " AND language=?"
            ),
            (run_kernel_binding_sha256(binding), language),
        ).fetchone()
        if row is None:
            return None
        try:
            if row[1] != binding.scope.project_id.encode("ascii"):
                raise ValueError
            strict_json(row[0], 2048)
            value = KernelBirthPin.model_validate_json(row[0], strict=True)
            scope = binding.scope
            if (
                value.project_id,
                value.session_id,
                value.environment_id,
                value.incarnation,
                value.sandbox,
                value.sandbox_id,
                value.language,
            ) != (
                scope.project_id,
                scope.session_id,
                scope.environment_id,
                binding.incarnation,
                binding.sandbox,
                binding.sandbox_id,
                language,
            ):
                raise ValueError
            return value
        except (ValueError, TypeError, ValidationError, RecursionError):
            raise unavailable() from None

    def observe(
        self, binding: RunKernelBinding, birth: KernelBirthPin, info: KernelInfo, *, remember: bool
    ) -> KernelInfo:
        """Raw and normalized generations coincide within this fixed original incarnation."""
        old = self.birth(binding, info.language)
        last = self.generation(binding, info.language)
        if (
            (old is None) != (last is None)
            or (old is not None and old.generation != last)
            or info.generation != birth.generation
            or info.kernel_id != birth.kernel_id
            or info.language != birth.language
            or info.kernel_id != run_kernel_id(binding, info.language)
        ):
            raise unavailable()
        revision = birth.start_revision
        if old is not None:
            if old.model_copy(update={"start_revision": None}) == birth.model_copy(
                update={"start_revision": None}
            ):
                revision = old.start_revision
            elif old.generation >= birth.generation:
                raise unavailable()
        if remember:
            self.budget()
            key = run_kernel_binding_sha256(binding)
            self.store._db.execute(
                "INSERT OR REPLACE INTO run_kernel_generations VALUES(?,?,?,?)",
                (key, binding.scope.project_id, info.language, max(info.generation, last or 0)),
            )
            if old is None or old.generation < birth.generation:
                self.store._db.execute(
                    "INSERT OR REPLACE INTO run_kernel_births VALUES(?,?,?,?)",
                    (key, binding.scope.project_id, info.language, birth.model_dump_json()),
                )
            self.budget()
        current = info.current_revision
        if revision is not None and current is not None and revision > current:
            revision = None
        status = (
            "unknown"
            if revision is None or current is None
            else "current"
            if revision == current
            else "predates"
        )
        return info.model_copy(update={"start_revision": revision, "revision_status": status})

    def lookup_control(
        self,
        binding: RunKernelBinding,
        operation: str,
        action: Literal["restart", "close"],
        language: Literal["python", "sage"] | None,
    ) -> RunControlReceipt | None:
        """Exact historical audit only; no current channel or repeated effect is needed."""
        key = run_kernel_binding_sha256(binding)
        if type(operation) is not str or not 1 <= len(operation) <= 128:
            raise WorkerError(ErrorKind.INVALID_REQUEST, "invalid internal control operation")
        self.require()
        if (
            self.store._db.execute(
                "SELECT 1 FROM run_kernel_controls WHERE binding=? AND operation=?",
                (key, operation),
            ).fetchone()
            is None
        ):
            return None
        return self.begin_control(binding, operation, action, language)[0]

    def begin_control(
        self,
        binding: RunKernelBinding,
        operation: str,
        action: Literal["restart", "close"],
        language: Literal["python", "sage"] | None,
    ) -> tuple[RunControlReceipt, bool]:
        """A durable intent is never automatically repeated after an unknown effect."""
        # Constructor + bounded original binding checks happen before aggregate JSON.
        run_kernel_binding_sha256(binding)
        if type(operation) is not str or len(operation) > 128:
            raise WorkerError(ErrorKind.INVALID_REQUEST, "invalid internal control operation")
        request = RunControl(
            binding=binding,
            operation_id=operation,
            action=action,
            language=language,
            recorded_at=datetime.now(UTC).isoformat(),
        )
        key = run_kernel_binding_sha256(binding)
        with self.store._transaction():
            self.budget()
            row = self.store._db.execute(
                (
                    "SELECT CASE WHEN typeof(record)='text' THEN substr(CAST(reco"
                    "rd AS BLOB),1,4097) END,substr(state,1,8),CASE WHEN result I"
                    "S NULL THEN NULL WHEN typeof(result)='text' THEN substr(CAST"
                    "(result AS BLOB),1,4097) END,substr(CAST(project AS BLOB),1,"
                    "129),substr(CAST(action AS BLOB),1,8),substr(CAST(request_sh"
                    "a AS BLOB),1,65) FROM run_kernel_controls WHERE binding=? AN"
                    "D operation=?"
                ),
                (key, operation),
            ).fetchone()
            if row is not None:
                try:
                    strict_json(row[0], 4096)
                    saved = RunControl.model_validate_json(row[0], strict=True)
                    if row[0] != saved.model_dump_json().encode("utf-8"):
                        raise ValueError
                    if saved.model_copy(update={"recorded_at": request.recorded_at}) != request:
                        raise WorkerError(
                            ErrorKind.CONFLICT, "internal control operation already differs"
                        )
                    stamp = datetime.fromisoformat(saved.recorded_at)
                    if stamp.utcoffset() != UTC.utcoffset(stamp) or row[3:] != (
                        binding.scope.project_id.encode("ascii"),
                        action.encode("ascii"),
                        hashlib.sha256(row[0]).hexdigest().encode("ascii"),
                    ):
                        raise ValueError
                    if row[1] not in ("pending", "done") or (
                        row[1] == "pending" and row[2] is not None
                    ):
                        raise ValueError
                    kernel = None
                    if row[2] is not None:
                        strict_json(row[2], 4096)
                        kernel = KernelInfo.model_validate_json(row[2], strict=True)
                    if (action == "close" and kernel is not None) or (
                        row[1] == "done"
                        and action == "restart"
                        and (
                            kernel is None
                            or kernel.language != language
                            or kernel.kernel_id != run_kernel_id(binding, language)
                            or kernel.generation >= 2**31
                        )
                    ):
                        raise ValueError
                    return RunControlReceipt(request=saved, state=row[1], kernel=kernel), False
                except (ValueError, TypeError, ValidationError, RecursionError):
                    raise unavailable() from None
            if action == "restart" and self.closed(binding):
                raise unavailable()
            raw = request.model_dump_json()
            self.store._db.execute(
                "INSERT INTO run_kernel_controls VALUES(?,?,?,?,?,?,?,NULL)",
                (
                    key,
                    binding.scope.project_id,
                    operation,
                    action,
                    hashlib.sha256(raw.encode()).hexdigest(),
                    raw,
                    "pending",
                ),
            )
            if action == "close":
                self.store._db.execute(
                    "INSERT OR IGNORE INTO run_kernel_closures VALUES(?,?,?)",
                    (key, binding.scope.project_id, request.recorded_at),
                )
            self.budget()
        return RunControlReceipt(request=request, state="pending"), True

    def finish_control(self, request: RunControl, kernel: KernelInfo | None) -> RunControlReceipt:
        """Called within the transaction that retains any restart birth/generation."""
        request = checked_control(request)
        kernel = checked_info(kernel) if kernel is not None else None
        if (request.action == "close" and kernel is not None) or (
            request.action == "restart"
            and (
                kernel is None
                or kernel.language != request.language
                or kernel.kernel_id != run_kernel_id(request.binding, request.language)
                or kernel.generation >= 2**31
            )
        ):
            raise unavailable()
        key = run_kernel_binding_sha256(request.binding)
        raw = request.model_dump_json()
        changed = self.store._db.execute(
            "UPDATE run_kernel_controls SET state='done',result=? WHERE binding=? AND operation=? "
            "AND state='pending' AND request_sha=? AND record=?",
            (
                kernel.model_dump_json() if kernel else None,
                key,
                request.operation_id,
                hashlib.sha256(raw.encode()).hexdigest(),
                raw,
            ),
        )
        if changed.rowcount != 1:
            raise unavailable()
        self.budget()
        return RunControlReceipt(request=request, state="done", kernel=kernel)
