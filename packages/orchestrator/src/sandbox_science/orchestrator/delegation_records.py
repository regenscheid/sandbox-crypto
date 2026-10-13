# SPDX-License-Identifier: MIT
"""Internal delegation intent and handback records, without execution authority.

This synchronous store borrows DocumentStore's connection and lifetime. Its caller
must finish/drain admitted calls before closing that owner. DTO construction is
not authentication; no launcher, tool, model, worker or notification is installed.
"""

# Logical control-store modules share the canonical transaction seam.
# pyright: reportPrivateUsage=false

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import UTC, datetime
from typing import Annotated, Literal, Self, cast

from pydantic import ConfigDict, Field, ValidationError, field_validator, model_validator

from sandbox_science.contracts import Contract, Identifier, new_id

from .documents.errors import (
    CONFLICT,
    INTERNAL,
    INVALID_REQUEST,
    NOT_FOUND,
    DocumentError,
)
from .documents.store import DocumentStore

MAX_BRIEF = 65_536
MAX_RAW = 65_536
MAX_RESULT = 8192
MAX_METADATA = 98_304
MAX_PAGE = 131_072
MAX_COUNTER = 2**62 - 1
Counter = Annotated[int, Field(strict=True, ge=0, le=MAX_COUNTER)]
Positive = Annotated[int, Field(strict=True, ge=1, le=MAX_COUNTER)]
Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
Text = Annotated[str, Field(min_length=1, max_length=4096)]
Ending = Literal["returned", "failed", "cancelled", "limit_reached", "interrupted"]
ResultError = Literal[
    "missing_payload",
    "invalid_utf8",
    "invalid_json",
    "duplicate_keys",
    "invalid_result",
    "result_too_large",
]


class RecordModel(Contract):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    @field_validator("*")
    @classmethod
    def _text(cls, value: object) -> object:
        if type(value) is str and (not value.strip() or "\0" in value):
            raise ValueError("record text must be nonempty and cannot contain NUL")
        return value


class DelegationScope(RecordModel):
    project_id: Identifier
    session_id: Identifier
    environment_id: Identifier


class _Authority(RecordModel):
    scope: DelegationScope
    parent_run_id: Identifier

    @field_validator("parent_run_id")
    @classmethod
    def _agent(cls, value: str) -> str:
        if value.casefold() == "user":
            raise ValueError("an agent cannot impersonate USER")
        return value


class ParentAuthority(_Authority):
    """Separate trusted caller attribution; never model request data or a grant."""

    kind: Literal["parent"] = "parent"


class HandbackAuthority(_Authority):
    """Trusted controller's exact original parent/child identity, not child JSON."""

    kind: Literal["controller"] = "controller"
    delegation_id: Identifier
    child_run_id: Identifier

    @model_validator(mode="after")
    def _distinct(self) -> Self:
        if self.child_run_id.casefold() == "user" or self.child_run_id == self.parent_run_id:
            raise ValueError("a child requires its own non-USER run identity")
        return self


class ReferenceClaim(RecordModel):
    """A bounded same-project pin claim, with no lookup or resource authority."""

    project_id: Identifier
    kind: Literal["artifact", "document", "ledger", "execution"]
    reference_id: Identifier
    revision: Positive | None = None
    sha256: Digest | None = None
    attempt_id: Identifier | None = None

    @model_validator(mode="after")
    def _pin(self) -> Self:
        if self.kind == "execution":
            if self.attempt_id is None or self.revision is not None or self.sha256 is not None:
                raise ValueError("an execution claim needs exactly its attempt pin")
        elif self.revision is None or self.sha256 is None or self.attempt_id is not None:
            raise ValueError("a retained resource claim needs its exact revision and digest")
        return self


class StepPin(RecordModel):
    step_id: Identifier
    sha256: Digest


class TrackPin(RecordModel):
    """Historical brief data; it does not issue a current delegated-progress grant."""

    plan_id: Identifier
    version: Positive
    track_id: Identifier
    steps: tuple[StepPin, ...] = Field(min_length=1, max_length=128)

    @model_validator(mode="after")
    def _unique(self) -> Self:
        if len({step.step_id for step in self.steps}) != len(self.steps):
            raise ValueError("step pins must be unique")
        return self


class RequestedLimits(RecordModel):
    """Recorded requests, not applied runtime budgets or provider spend limits."""

    turns: Annotated[int, Field(strict=True, ge=1, le=1024)]
    wall_seconds: Annotated[int, Field(strict=True, ge=1, le=86_400)]
    output_tokens: Annotated[int, Field(strict=True, ge=1, le=1_048_576)]


class ProfileIntent(RecordModel):
    """Explicit desired profile pins; no tool selection, loader or grant follows."""

    profile_id: Identifier
    profile_sha256: Digest
    model_alias: Annotated[str, Field(min_length=1, max_length=256)] | None = None


class DelegationBrief(RecordModel):
    task: Text
    context_summary: Text
    track: TrackPin | None = None
    inputs: tuple[ReferenceClaim, ...] = Field(max_length=32)
    profile: ProfileIntent
    limits: RequestedLimits
    output_schema: Literal["sandbox-delegation-result-1"] = "sandbox-delegation-result-1"


class StructuredHandback(RecordModel):
    """The child's assertion; separate from the trusted host ending."""

    schema_version: Literal[1]
    status: Literal["complete", "partial", "blocked"]
    summary: Text
    results: tuple[ReferenceClaim, ...] = Field(max_length=32)
    deviations: tuple[Text, ...] = Field(max_length=16)
    open_questions: tuple[Text, ...] = Field(max_length=16)

    @field_validator("schema_version", mode="before")
    @classmethod
    def _version(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("schema version must be an exact integer")
        return value


class HostObservation(RecordModel):
    ended: Ending
    resolved_model: Annotated[str, Field(min_length=1, max_length=256)] | None = None


class DelegationMutation(RecordModel):
    """Immutable mutation, scoped event and historical receipt in one stored row."""

    format_version: Literal[1] = 1
    scope: DelegationScope
    parent_run_id: Identifier
    delegation_id: Identifier
    child_run_id: Identifier
    sequence: Positive
    revision: Literal[1, 2]
    action: Literal["prepare", "finish"]
    operation_id: Identifier
    request_sha256: Digest
    recorded_at: Annotated[str, Field(max_length=40)]
    brief: DelegationBrief | None
    observation: HostObservation | None
    raw_size: Counter | None
    raw_sha256: Digest | None
    result: StructuredHandback | None
    result_error: ResultError | None

    @field_validator("format_version", "revision", mode="before")
    @classmethod
    def _versions(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("record versions must be exact integers")
        return value

    @field_validator("recorded_at")
    @classmethod
    def _utc(cls, value: str) -> str:
        stamp = datetime.fromisoformat(value)
        offset = stamp.utcoffset()
        if offset is None or offset.total_seconds() != 0:
            raise ValueError("record time needs a UTC offset")
        return value

    @model_validator(mode="after")
    def _shape(self) -> Self:
        if self.parent_run_id.casefold() == "user" or (
            self.child_run_id.casefold() == "user" or self.child_run_id == self.parent_run_id
        ):
            raise ValueError("record run identity is invalid")
        if self.action == "prepare":
            if (
                self.revision != 1
                or self.brief is None
                or any(
                    value is not None
                    for value in (
                        self.observation,
                        self.raw_size,
                        self.raw_sha256,
                        self.result,
                        self.result_error,
                    )
                )
            ):
                raise ValueError("prepared intent cannot claim a handback or dispatch")
        elif (
            self.revision != 2
            or self.brief is not None
            or self.observation is None
            or (self.raw_size is None) != (self.raw_sha256 is None)
            or (self.result is None) == (self.result_error is None)
        ):
            raise ValueError("terminal record requires an observation and exact result state")
        return self


class DelegationSnapshot(RecordModel):
    prepared: DelegationMutation
    outcome: DelegationMutation | None
    raw_payload: bytes | None


class DelegationEvent(RecordModel):
    sequence: Positive
    delegation_id: Identifier
    child_run_id: Identifier
    revision: Literal[1, 2]
    action: Literal["prepare", "finish"]
    operation_id: Identifier
    request_sha256: Digest

    @field_validator("revision", mode="before")
    @classmethod
    def _revision(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("event revision must be an exact integer")
        return value


class DelegationPage(RecordModel):
    items: tuple[DelegationEvent, ...]
    after: Counter
    target: Counter
    next_after: Counter | None


class DelegationLimits(RecordModel):
    """Retained storage bounds; these are not concurrent-run policy defaults."""

    project_delegations: Annotated[int, Field(strict=True, ge=1, le=4096)] = 256
    total_delegations: Annotated[int, Field(strict=True, ge=1, le=16_384)] = 1024
    project_bytes: Annotated[int, Field(strict=True, ge=1024, le=256 * 1024 * 1024)] = (
        64 * 1024 * 1024
    )
    total_bytes: Annotated[int, Field(strict=True, ge=1024, le=1024 * 1024 * 1024)] = (
        256 * 1024 * 1024
    )

    @model_validator(mode="after")
    def _order(self) -> Self:
        if (
            self.project_delegations > self.total_delegations
            or self.project_bytes > self.total_bytes
        ):
            raise ValueError("project limits cannot exceed global limits")
        return self


_MODELS = {
    DelegationScope,
    ParentAuthority,
    HandbackAuthority,
    ReferenceClaim,
    StepPin,
    TrackPin,
    RequestedLimits,
    ProfileIntent,
    DelegationBrief,
    StructuredHandback,
    HostObservation,
    DelegationMutation,
    DelegationEvent,
    DelegationPage,
    DelegationLimits,
}


def _plain(
    value: object,
    *,
    depth: int = 0,
    nodes: list[int] | None = None,
    used: list[int] | None = None,
    maximum: int = MAX_METADATA,
) -> object:
    """Bound host copies before overridable iteration/encoding or model dumping."""
    if nodes is None:
        nodes = [0]
    if used is None:
        used = [0]
    nodes[0] += 1
    if depth > 16 or nodes[0] > 4096:
        raise ValueError("metadata structure limit")

    def account(size: int) -> None:
        assert used is not None  # noqa: S101 - initialized shared recursion budget
        used[0] += size
        if used[0] > maximum:
            raise ValueError("aggregate metadata limit")

    def child(item: object) -> object:
        return _plain(item, depth=depth + 1, nodes=nodes, used=used, maximum=maximum)

    kind = type(value)
    if value is None or kind is bool:
        account(4 if value is None or value is True else 5)
        return value
    if kind is int:
        number = cast(int, value)
        if not -MAX_COUNTER <= number <= MAX_COUNTER:
            raise ValueError("integer limit")
        account(len(str(number)))
        return value
    if kind is str:
        text = cast(str, value)
        if len(text) > maximum - used[0]:
            raise ValueError("text limit")
        size = len(text.encode("utf-8")) + 2
        for char in text:
            if char in '\\"\b\f\n\r\t':
                size += 1
            elif ord(char) < 32:
                size += 5
        account(size)
        return value
    if any(kind is known for known in _MODELS):
        fields = object.__getattribute__(value, "__dict__")
        extras = object.__getattribute__(value, "__pydantic_extra__")
        model = cast(type[RecordModel], kind)
        if type(fields) is not dict or extras is not None:
            raise ValueError("undeclared model fields")
        declared = cast(dict[str, object], fields)
        if (
            len(declared) != len(model.model_fields)
            or any(type(key) is not str for key in declared)
            or set(declared) != set(model.model_fields)
        ):
            raise ValueError("undeclared model fields")
        return child(declared)
    if kind is dict:
        mapping = cast(dict[object, object], value)
        if len(mapping) > 256 or any(type(key) is not str for key in mapping):
            raise ValueError("object limit")
        account(2 + max(0, len(mapping) - 1) + len(mapping))
        return {cast(str, child(key)): child(item) for key, item in mapping.items()}
    if kind is tuple or kind is list:
        items = cast(tuple[object, ...] | list[object], value)
        if len(items) > 256:
            raise ValueError("array limit")
        account(2 + max(0, len(items) - 1))
        return [child(item) for item in items]
    raise ValueError("unsupported host value")


def _json(value: object, maximum: int = MAX_METADATA) -> bytes:
    try:
        data = json.dumps(
            _plain(value, maximum=maximum),
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        if len(data) > maximum:
            raise ValueError("encoded metadata limit")
        return data
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise DocumentError(INVALID_REQUEST, "invalid or overlarge delegation input") from None


def _checked[M: RecordModel](model: type[M], value: M, maximum: int = MAX_METADATA) -> M:
    if type(value) is not model:
        raise DocumentError(INVALID_REQUEST, "invalid delegation input type")
    try:
        return model.model_validate_json(_json(value, maximum), strict=True)
    except ValidationError:
        raise DocumentError(INVALID_REQUEST, "invalid delegation input") from None


class _DuplicateError(ValueError):
    pass


def _pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateError
        result[key] = value
    return result


def _parsed(raw: bytes) -> object:
    return json.loads(
        raw.decode("utf-8"),
        object_pairs_hook=_pairs,
        parse_constant=lambda _: (_ for _ in ()).throw(ValueError()),
    )


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _identifier(value: str) -> str:
    if type(value) is not str or len(value) > 128:
        raise DocumentError(INVALID_REQUEST, "invalid delegation identifier")
    try:
        # A strict existing scope field supplies the common Identifier validation.
        DelegationScope(project_id=value, session_id="s", environment_id="e")
    except ValidationError:
        raise DocumentError(INVALID_REQUEST, "invalid delegation identifier") from None
    return value


def _counter(value: int) -> int:
    if type(value) is not int or not 0 <= value <= MAX_COUNTER:
        raise DocumentError(INVALID_REQUEST, "invalid delegation cursor")
    return value


def _same_project(scope: DelegationScope, refs: tuple[ReferenceClaim, ...]) -> None:
    if any(ref.project_id != scope.project_id for ref in refs):
        raise DocumentError(INVALID_REQUEST, "reference claim belongs to another project")


def _result(
    scope: DelegationScope, raw: bytes | None
) -> tuple[StructuredHandback | None, ResultError | None]:
    if raw is None:
        return None, "missing_payload"
    try:
        _plain(_parsed(raw))
    except UnicodeError:
        return None, "invalid_utf8"
    except _DuplicateError:
        return None, "duplicate_keys"
    except (ValueError, TypeError, RecursionError):
        return None, "invalid_json"
    try:
        result = StructuredHandback.model_validate_json(raw, strict=True)
        _same_project(scope, result.results)
    except (ValidationError, DocumentError, ValueError):
        return None, "invalid_result"
    try:
        _json(result, MAX_RESULT)
    except DocumentError:
        return None, "result_too_large"
    return result, None


_TABLES = ("delegation_record_meta", "delegation_record_events")
_SCHEMA = (
    "CREATE TABLE delegation_record_meta(singleton INTEGER PRIMARY KEY CHECK(singleton=1),"
    "version INTEGER NOT NULL) STRICT",
    "CREATE TABLE delegation_record_events(project TEXT NOT NULL,session TEXT NOT NULL,"
    "environment TEXT NOT NULL,parent TEXT NOT NULL,sequence INTEGER NOT NULL,"
    "action TEXT NOT NULL,delegation TEXT NOT NULL,child TEXT NOT NULL,operation TEXT NOT NULL,"
    "request_sha TEXT NOT NULL,metadata TEXT NOT NULL,raw BLOB,bytes INTEGER NOT NULL,"
    "PRIMARY KEY(project,session,parent,sequence),"
    "UNIQUE(project,session,parent,action,operation),"
    "UNIQUE(project,session,delegation,action),UNIQUE(project,session,child,action)) STRICT",
)
_CHARGE = (
    "length(CAST(project AS BLOB))+length(CAST(session AS BLOB))+"
    "length(CAST(environment AS BLOB))+length(CAST(parent AS BLOB))+"
    "length(CAST(action AS BLOB))+length(CAST(delegation AS BLOB))+"
    "length(CAST(child AS BLOB))+length(CAST(operation AS BLOB))+"
    "length(CAST(request_sha AS BLOB))+length(CAST(metadata AS BLOB))+"
    "coalesce(length(raw),0)+512"
)
_SELECT = (
    "SELECT CASE WHEN typeof(sequence)='integer' THEN sequence END,"
    "CASE WHEN typeof(metadata)='text' THEN substr(CAST(metadata AS BLOB),1,98305) END,"
    "CASE WHEN raw IS NULL THEN NULL WHEN typeof(raw)='blob' AND length(raw)=0 THEN X'' "
    "WHEN typeof(raw)='blob' THEN substr(raw,1,65537) "
    "ELSE 0 END,"
    "CASE WHEN typeof(environment)='text' THEN substr(environment,1,129) END,"
    "CASE WHEN typeof(action)='text' THEN substr(action,1,8) END,"
    "CASE WHEN typeof(delegation)='text' THEN substr(delegation,1,129) END,"
    "CASE WHEN typeof(child)='text' THEN substr(child,1,129) END,"
    "CASE WHEN typeof(operation)='text' THEN substr(operation,1,129) END,"
    "CASE WHEN typeof(request_sha)='text' THEN substr(request_sha,1,65) END,"
    "CASE WHEN typeof(bytes)='integer' AND bytes BETWEEN 512 AND 165888 THEN bytes END "
    "FROM delegation_record_events "
)


class DelegationRecords:
    """Immutable records only. No store-owned thread/connection or effect authority."""

    def __init__(
        self,
        documents: DocumentStore,
        *,
        initialize: bool = False,
        limits: DelegationLimits | None = None,
    ) -> None:
        if type(initialize) is not bool:
            raise DocumentError(INVALID_REQUEST, "invalid initialization flag")
        self.documents = documents
        self.limits = _checked(DelegationLimits, DelegationLimits() if limits is None else limits)
        if initialize:
            with documents._write() as db:
                present = self._presence(db)
                if present == 0:
                    for statement in _SCHEMA:
                        db.execute(statement)
                    db.execute("INSERT INTO delegation_record_meta VALUES(1,1)")
                    for table in _TABLES:
                        for verb in ("UPDATE", "DELETE"):
                            db.execute(
                                f"CREATE TRIGGER {table}_no_{verb.lower()} BEFORE {verb} ON "
                                f"{table} BEGIN SELECT RAISE(ABORT,'delegation history is "
                                "immutable'); END"
                            )
                self._schema(db)
                self._budget(db, None)
        else:
            with documents._read() as db:
                self._schema(db)

    @staticmethod
    def _presence(db: sqlite3.Connection) -> int:
        return db.execute(
            "SELECT count(*) FROM sqlite_master WHERE type='table' AND "
            "name IN ('delegation_record_meta','delegation_record_events')"
        ).fetchone()[0]

    @classmethod
    def _schema(cls, db: sqlite3.Connection | None) -> None:
        present = cls._presence(db) if db is not None else 0
        if present == 0:
            raise DocumentError(NOT_FOUND, "delegation history is not initialized")
        if present != 2 or db is None:
            raise DocumentError(INTERNAL, "delegation schema is incomplete")
        row = db.execute(
            "SELECT CASE WHEN typeof(version)='integer' THEN version END FROM "
            "delegation_record_meta WHERE singleton=1"
        ).fetchall()
        triggers = db.execute(
            "SELECT count(*) FROM sqlite_master WHERE type='trigger' AND name IN "
            "('delegation_record_meta_no_update','delegation_record_meta_no_delete',"
            "'delegation_record_events_no_update','delegation_record_events_no_delete')"
        ).fetchone()[0]
        if row != [(1,)] or triggers != 4:
            raise DocumentError(INTERNAL, "delegation schema is unsupported or incomplete")

    def _budget(self, db: sqlite3.Connection, project: str | None) -> None:
        budgets: list[tuple[str | None, int, int]] = [
            (None, self.limits.total_delegations, self.limits.total_bytes)
        ]
        if project is not None:
            budgets.append((project, self.limits.project_delegations, self.limits.project_bytes))
        for selected, count_limit, byte_limit in budgets:
            count, preparations, size, bad = db.execute(
                "SELECT count(*),coalesce(sum(action='prepare'),0),coalesce(sum(bytes),0),"  # noqa: S608 - fixed SQL fragments only
                "coalesce(sum(CASE WHEN typeof(bytes)!='integer' OR bytes<512 OR bytes!=("
                + _CHARGE
                + ") OR typeof(metadata)!='text' OR length(CAST(metadata AS BLOB))>"
                "98304 OR (raw IS NOT NULL AND (typeof(raw)!='blob' OR length(raw)>65536)) "
                "OR action NOT IN ('prepare','finish') OR typeof(sequence)!='integer' OR "
                "sequence<1 OR sequence>? OR (action='finish' AND NOT EXISTS(SELECT 1 "
                "FROM delegation_record_events AS p WHERE p.project=e.project AND "
                "p.session=e.session AND p.parent=e.parent AND p.environment=e.environment "
                "AND p.delegation=e.delegation AND p.child=e.child AND p.action='prepare' "
                "AND p.sequence<e.sequence)) THEN 1 ELSE 0 END),0) FROM "
                "delegation_record_events AS e WHERE ? IS NULL OR project=?",
                (MAX_COUNTER, selected, selected),
            ).fetchone()
            if bad or count > preparations * 2:
                raise DocumentError(INTERNAL, "retained delegation accounting is invalid")
            # One fixed metadata row is charged globally; physical SQLite pages are not measured.
            reserve = 512 if selected is None else 0
            if preparations > count_limit or count > 2 * count_limit or size + reserve > byte_limit:
                raise DocumentError(INVALID_REQUEST, "delegation retention quota is full")

    @staticmethod
    def _request(mutation: DelegationMutation) -> dict[str, object]:
        if mutation.action == "prepare":
            authority: RecordModel = ParentAuthority(
                scope=mutation.scope, parent_run_id=mutation.parent_run_id
            )
            data: dict[str, object] = {"brief": mutation.brief}
        else:
            authority = HandbackAuthority(
                scope=mutation.scope,
                parent_run_id=mutation.parent_run_id,
                delegation_id=mutation.delegation_id,
                child_run_id=mutation.child_run_id,
            )
            data = {
                "expected_revision": 1,
                "observation": mutation.observation,
                "raw_size": mutation.raw_size,
                "raw_sha256": mutation.raw_sha256,
            }
        return {
            "action": mutation.action,
            "authority": authority,
            "operation_id": mutation.operation_id,
            **data,
        }

    @staticmethod
    def _parent(db: sqlite3.Connection, authority: ParentAuthority) -> int:
        rows = db.execute(
            "SELECT count(*),min(sequence),max(sequence),coalesce(sum(CASE WHEN "
            "typeof(sequence)!='integer' OR sequence<1 OR sequence>? OR "
            "typeof(environment)!='text' OR length(CAST(environment AS BLOB))>128 OR "
            "environment!=? THEN 1 ELSE 0 END),0) FROM "
            "delegation_record_events WHERE project=? AND session=? AND parent=? ",
            (
                MAX_COUNTER,
                authority.scope.environment_id,
                authority.scope.project_id,
                authority.scope.session_id,
                authority.parent_run_id,
            ),
        ).fetchone()
        count, first, current, bad = rows
        if count == 0:
            return 0
        if bad:
            raise DocumentError(CONFLICT, "original parent scope is unavailable or changed")
        if (
            type(current) is not int
            or not 1 <= current <= MAX_COUNTER
            or first != 1
            or count != current
        ):
            raise DocumentError(INTERNAL, "retained delegation cursor is invalid")
        return current

    def _load(
        self, row: tuple[object, ...], authority: ParentAuthority
    ) -> tuple[DelegationMutation, bytes | None]:
        try:
            (
                seq,
                metadata,
                raw,
                environment,
                action,
                delegation,
                child,
                operation,
                digest,
                charge,
            ) = row
            if type(metadata) is not bytes or len(metadata) > MAX_METADATA:
                raise ValueError
            if raw is not None and (type(raw) is not bytes or len(raw) > MAX_RAW):
                raise ValueError
            _parsed(metadata)
            value = DelegationMutation.model_validate_json(metadata, strict=True)
            columns = (
                value.scope.project_id,
                value.scope.session_id,
                value.scope.environment_id,
                value.parent_run_id,
                value.action,
                value.delegation_id,
                value.child_run_id,
                value.operation_id,
                value.request_sha256,
            )
            if (
                value.scope != authority.scope
                or value.parent_run_id != authority.parent_run_id
                or (
                    value.sequence,
                    value.scope.environment_id,
                    value.action,
                    value.delegation_id,
                    value.child_run_id,
                    value.operation_id,
                    value.request_sha256,
                )
                != (seq, environment, action, delegation, child, operation, digest)
                or _sha(_json(self._request(value))) != value.request_sha256
                or charge
                != sum(len(item.encode("utf-8")) for item in columns)
                + len(metadata)
                + len(raw or b"")
                + 512
            ):
                raise ValueError
            if value.action == "prepare":
                if raw is not None or value.brief is None:
                    raise ValueError
                _json(value.brief, MAX_BRIEF)
                _same_project(value.scope, value.brief.inputs)
            else:
                if (value.raw_size, value.raw_sha256) != (
                    (len(raw), _sha(raw)) if raw is not None else (None, None)
                ):
                    raise ValueError
                if (value.result, value.result_error) != _result(value.scope, raw):
                    raise ValueError
            return value, raw
        except (ValidationError, ValueError, TypeError, DocumentError, RecursionError):
            raise DocumentError(INTERNAL, "retained delegation record is invalid") from None

    def _snapshot(
        self, db: sqlite3.Connection, authority: ParentAuthority, delegation: str
    ) -> DelegationSnapshot:
        rows = db.execute(
            _SELECT + "WHERE project=? AND session=? AND parent=? AND delegation=? "
            "ORDER BY sequence LIMIT 3",
            (
                authority.scope.project_id,
                authority.scope.session_id,
                authority.parent_run_id,
                delegation,
            ),
        ).fetchall()
        if not rows:
            raise DocumentError(NOT_FOUND, "delegation is absent from this parent scope")
        loaded = [self._load(row, authority) for row in rows]
        prepared, no_raw = loaded[0]
        if prepared.action != "prepare" or no_raw is not None or len(loaded) > 2:
            raise DocumentError(INTERNAL, "retained delegation history is incomplete")
        outcome, raw = loaded[1] if len(loaded) == 2 else (None, None)
        if outcome is not None and (
            outcome.action != "finish"
            or outcome.child_run_id != prepared.child_run_id
            or outcome.sequence <= prepared.sequence
        ):
            raise DocumentError(INTERNAL, "retained handback does not match its original brief")
        return DelegationSnapshot(prepared=prepared, outcome=outcome, raw_payload=raw)

    def _append(self, db: sqlite3.Connection, value: DelegationMutation, raw: bytes | None) -> None:
        metadata = _json(value)
        columns = (
            value.scope.project_id,
            value.scope.session_id,
            value.scope.environment_id,
            value.parent_run_id,
            value.action,
            value.delegation_id,
            value.child_run_id,
            value.operation_id,
            value.request_sha256,
        )
        charge = (
            sum(len(item.encode("utf-8")) for item in columns)
            + len(metadata)
            + len(raw or b"")
            + 512
        )
        db.execute(
            "INSERT INTO delegation_record_events VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                *columns[:4],
                value.sequence,
                *columns[4:],
                metadata.decode("utf-8"),
                raw,
                charge,
            ),
        )

    def prepare(
        self, authority: ParentAuthority, operation_id: str, brief: DelegationBrief
    ) -> DelegationMutation:
        """Atomically retain an intent, not a running child or a dispatch receipt."""
        authority = _checked(ParentAuthority, authority)
        operation_id = _identifier(operation_id)
        brief = _checked(DelegationBrief, brief, MAX_BRIEF)
        _same_project(authority.scope, brief.inputs)
        request = {
            "action": "prepare",
            "authority": authority,
            "operation_id": operation_id,
            "brief": brief,
        }
        digest = _sha(_json(request))
        with self.documents._write() as db:
            self._schema(db)
            current = self._parent(db, authority)
            row = db.execute(
                _SELECT + "WHERE project=? AND session=? AND parent=? AND action='prepare' "
                "AND operation=?",
                (
                    authority.scope.project_id,
                    authority.scope.session_id,
                    authority.parent_run_id,
                    operation_id,
                ),
            ).fetchone()
            if row:
                replay, _ = self._load(row, authority)
                if replay.request_sha256 != digest:
                    raise DocumentError(CONFLICT, "operation ID already has a different request")
                self._snapshot(db, authority, replay.delegation_id)
                return replay
            self._budget(db, authority.scope.project_id)
            if current == MAX_COUNTER:
                raise DocumentError(INVALID_REQUEST, "delegation cursor is exhausted")
            value = DelegationMutation(
                scope=authority.scope,
                parent_run_id=authority.parent_run_id,
                delegation_id=new_id("dlg"),
                child_run_id=new_id("run"),
                sequence=current + 1,
                revision=1,
                action="prepare",
                operation_id=operation_id,
                request_sha256=digest,
                recorded_at=datetime.now(UTC).isoformat(),
                brief=brief,
                observation=None,
                raw_size=None,
                raw_sha256=None,
                result=None,
                result_error=None,
            )
            self._append(db, value, None)
            self._budget(db, authority.scope.project_id)
            return value

    def finish(
        self,
        authority: HandbackAuthority,
        operation_id: str,
        expected_revision: int,
        observation: HostObservation,
        raw_payload: bytes | None,
    ) -> DelegationMutation:
        """Retain one trusted host ending and bounded raw/derived child data atomically."""
        authority = _checked(HandbackAuthority, authority)
        operation_id = _identifier(operation_id)
        if type(expected_revision) is not int or expected_revision != 1:
            raise DocumentError(CONFLICT, "handback requires the exact prepared revision")
        observation = _checked(HostObservation, observation)
        if raw_payload is not None and (
            type(raw_payload) is not bytes or len(raw_payload) > MAX_RAW
        ):
            raise DocumentError(INVALID_REQUEST, "raw handback exceeds its type or byte limit")
        result, error = _result(authority.scope, raw_payload)
        raw_size = len(raw_payload) if raw_payload is not None else None
        raw_sha = _sha(raw_payload) if raw_payload is not None else None
        request = {
            "action": "finish",
            "authority": authority,
            "operation_id": operation_id,
            "expected_revision": 1,
            "observation": observation,
            "raw_size": raw_size,
            "raw_sha256": raw_sha,
        }
        digest = _sha(_json(request))
        parent = ParentAuthority(scope=authority.scope, parent_run_id=authority.parent_run_id)
        with self.documents._write() as db:
            self._schema(db)
            current = self._parent(db, parent)
            snapshot = self._snapshot(db, parent, authority.delegation_id)
            if snapshot.prepared.child_run_id != authority.child_run_id:
                raise DocumentError(CONFLICT, "handback child does not match the original brief")
            if snapshot.outcome is not None:
                outcome = snapshot.outcome
                if outcome.operation_id != operation_id or outcome.request_sha256 != digest:
                    raise DocumentError(CONFLICT, "terminal handback is immutable")
                return outcome
            existing = db.execute(
                "SELECT 1 FROM delegation_record_events WHERE project=? AND session=? AND "
                "parent=? AND action='finish' AND operation=?",
                (
                    authority.scope.project_id,
                    authority.scope.session_id,
                    authority.parent_run_id,
                    operation_id,
                ),
            ).fetchone()
            if existing:
                raise DocumentError(CONFLICT, "operation ID already names another handback")
            self._budget(db, authority.scope.project_id)
            if current == MAX_COUNTER:
                raise DocumentError(INVALID_REQUEST, "delegation cursor is exhausted")
            value = DelegationMutation(
                scope=authority.scope,
                parent_run_id=authority.parent_run_id,
                delegation_id=authority.delegation_id,
                child_run_id=authority.child_run_id,
                sequence=current + 1,
                revision=2,
                action="finish",
                operation_id=operation_id,
                request_sha256=digest,
                recorded_at=datetime.now(UTC).isoformat(),
                brief=None,
                observation=observation,
                raw_size=raw_size,
                raw_sha256=raw_sha,
                result=result,
                result_error=error,
            )
            self._append(db, value, raw_payload)
            self._budget(db, authority.scope.project_id)
            return value

    def get(self, authority: ParentAuthority, delegation_id: str) -> DelegationSnapshot:
        """Read exact historical intent/outcome/raw bytes within the original parent scope."""
        authority = _checked(ParentAuthority, authority)
        delegation_id = _identifier(delegation_id)
        with self.documents._read() as db:
            self._schema(db)
            assert db is not None  # noqa: S101 - schema validates its existence
            self._parent(db, authority)
            return self._snapshot(db, authority, delegation_id)

    def list(
        self,
        authority: ParentAuthority,
        *,
        after: int = 0,
        target: int | None = None,
        limit: int = 8,
    ) -> DelegationPage:
        """Page a pinned parent-local event prefix; no cross-parent/global cursor leaks."""
        authority = _checked(ParentAuthority, authority)
        after = _counter(after)
        if target is not None:
            target = _counter(target)
        if type(limit) is not int or not 1 <= limit <= 8:
            raise DocumentError(INVALID_REQUEST, "invalid delegation page limit")
        with self.documents._read() as db:
            self._schema(db)
            assert db is not None  # noqa: S101 - schema validates its existence
            current = self._parent(db, authority)
            target = current if target is None else target
            if not after <= target <= current:
                raise DocumentError(CONFLICT, "delegation page target is unavailable")
            rows = db.execute(
                _SELECT + "WHERE project=? AND session=? AND parent=? AND sequence>? AND "
                "sequence<=? ORDER BY sequence LIMIT ?",
                (
                    authority.scope.project_id,
                    authority.scope.session_id,
                    authority.parent_run_id,
                    after,
                    target,
                    limit,
                ),
            ).fetchall()
            events: list[DelegationEvent] = []
            for row in rows:
                mutation, _ = self._load(row, authority)
                if mutation.sequence != after + len(events) + 1:
                    raise DocumentError(INTERNAL, "retained delegation event sequence has a gap")
                self._snapshot(db, authority, mutation.delegation_id)
                events.append(
                    DelegationEvent(
                        **{name: getattr(mutation, name) for name in DelegationEvent.model_fields}
                    )
                )
            consumed = after + len(events)
            if consumed < target and len(events) < limit:
                raise DocumentError(INTERNAL, "retained delegation event sequence has a gap")
            page = DelegationPage(
                items=tuple(events),
                after=after,
                target=target,
                next_after=consumed if consumed < target else None,
            )
            _json(page, MAX_PAGE)
            return page
