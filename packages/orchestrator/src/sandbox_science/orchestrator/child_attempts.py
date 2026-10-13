# SPDX-License-Identifier: MIT
"""Internal durable child inputs/partial observations; no dispatch or grant issuance.

This synchronous component borrows one initialized control store. Callers must
drain actual admitted calls before closing that owner; it owns no thread or FD.
New-work admission is cooperative caller proof, not authentication. Late facts
belong only to an existing original reservation and confer no renewed authority.
"""

# Canonical control components share the existing transaction seam.
# pyright: reportPrivateUsage=false
from __future__ import annotations

import hashlib
import json
import math
import os
import sqlite3
import stat
from collections.abc import Callable
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal, Self, cast

from pydantic import ConfigDict, Field, ValidationError, field_validator, model_validator

from sandbox_science.contracts import Contract, Identifier, new_id

from . import delegation_records as delegation
from .delegation_records import (
    DelegationMutation,
    DelegationRecords,
    DelegationScope,
    ParentAuthority,
)
from .documents.errors import (
    CONFLICT,
    INTERNAL,
    INVALID_REQUEST,
    NOT_FOUND,
    POLICY_DENIED,
    DocumentError,
)
from .documents.store import DocumentStore
from .projects import ProjectError, ProjectStore, SessionSnapshot

MAX_RECORD = 512 * 1024
MAX_INPUT = 256 * 1024
MAX_RESPONSE = 64 * 1024
MAX_ATTEMPT = 4 * 1024 * 1024
MAX_EVENTS = 1024
MAX_COUNTER = 2**62 - 1
Counter = Annotated[int, Field(strict=True, ge=0, le=MAX_COUNTER)]
Positive = Annotated[int, Field(strict=True, ge=1, le=MAX_COUNTER)]
Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
Text = Annotated[str, Field(max_length=65_536)]
Stamp = Annotated[str, Field(max_length=40)]


class JournalModel(Contract):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class AttemptAuthority(JournalModel):
    """Explicit trusted host attribution, not a public/model-supplied capability."""

    scope: DelegationScope
    parent_run_id: Identifier
    delegation_id: Identifier
    child_run_id: Identifier
    controller_id: Identifier

    @model_validator(mode="after")
    def _runs(self) -> Self:
        if self.parent_run_id == self.child_run_id or any(
            value.casefold() == "user"
            for value in (self.parent_run_id, self.child_run_id, self.controller_id)
        ):
            raise ValueError("child/controller attribution cannot impersonate USER")
        return self


class AppliedBudget(JournalModel):
    """Explicit caller ceilings; reservations do not prove provider enforcement/spend."""

    max_calls: Annotated[int, Field(strict=True, ge=1, le=256)]
    per_call_output_tokens: Annotated[int, Field(strict=True, ge=1, le=1_048_576)]
    total_output_tokens: Annotated[int, Field(strict=True, ge=1, le=268_435_456)]
    cumulative_input_bytes: Annotated[int, Field(strict=True, ge=1, le=64 * 1024 * 1024)]
    wall_seconds: Annotated[int, Field(strict=True, ge=1, le=86_400)]

    @model_validator(mode="after")
    def _order(self) -> Self:
        if self.per_call_output_tokens > self.total_output_tokens:
            raise ValueError("per-call output exceeds the supplied total")
        return self


class RequestedProfile(JournalModel):
    """Nonsecret catalog identity only; no raw connection/settings configuration."""

    profile_id: Identifier
    profile_sha256: Digest
    alias: Annotated[str, Field(min_length=1, max_length=256)]


class ToolSchema(JournalModel):
    name: Identifier
    description: Annotated[str, Field(max_length=4096)]
    parameters_json: Annotated[str, Field(max_length=16_384)]


class RecordedCall(JournalModel):
    call_id: Annotated[str, Field(min_length=1, max_length=128)]
    name: Annotated[str, Field(min_length=1, max_length=128)]
    arguments: Text


class RecordedMessage(JournalModel):
    role: Literal["system", "user", "assistant", "tool"]
    content: Text | None
    tool_call_id: Annotated[str, Field(min_length=1, max_length=128)] | None = None
    tool_calls: tuple[RecordedCall, ...] = Field(default=(), max_length=16)

    @model_validator(mode="after")
    def _shape(self) -> Self:
        if (self.role == "tool") != (self.tool_call_id is not None):
            raise ValueError("only tool messages name a tool call")
        if self.role != "assistant" and self.tool_calls:
            raise ValueError("only assistant messages contain requested calls")
        return self


Messages = Annotated[tuple[RecordedMessage, ...], Field(min_length=1, max_length=128)]


class SessionPin(JournalModel):
    lifetime: Identifier
    root_identity: tuple[Counter, Counter]
    file_identity: tuple[Counter, Counter, Counter, Counter, Counter]


class Begin(JournalModel):
    kind: Literal["begin"] = "begin"
    prepared: DelegationMutation
    session: SessionPin
    role: Literal["delegated"] = "delegated"
    profile: RequestedProfile
    budget: AppliedBudget
    system: Annotated[str, Field(min_length=1, max_length=8192)]
    tools: tuple[ToolSchema, ...] = Field(max_length=32)
    messages: Messages


class Reserve(JournalModel):
    kind: Literal["reserve"] = "reserve"
    messages: Messages
    output_tokens: Positive


class Observe(JournalModel):
    kind: Literal["observe"] = "observe"
    reservation: Positive
    outcome: Literal["returned", "unknown"]
    response: RecordedMessage | None
    resolved_model: Annotated[str, Field(min_length=1, max_length=256)] | None
    finish_reason: Annotated[str, Field(max_length=128)] | None
    input_tokens: Counter | None
    output_tokens: Counter | None
    reason: Literal["timeout", "cancelled", "interrupted", "model_error", "retention_failed"] | None

    @model_validator(mode="after")
    def _outcome(self) -> Self:
        if self.outcome == "returned":
            if (
                self.response is None
                or self.response.role != "assistant"
                or self.reason is not None
            ):
                raise ValueError("returned observation needs its exact assistant response")
        elif (
            self.response is not None
            or self.reason is None
            or any(
                value is not None
                for value in (
                    self.resolved_model,
                    self.finish_reason,
                    self.input_tokens,
                    self.output_tokens,
                )
            )
        ):
            raise ValueError("unknown retains fixed reason, not fabricated reply facts")
        return self


class EffectPin(JournalModel):
    """Explicit unresolved original effect references; this slice cannot clear them."""

    operation_id: Identifier
    execution_id: Identifier
    attempt_id: Identifier
    binding_sha256: Digest


class Checkpoint(JournalModel):
    kind: Literal["checkpoint"] = "checkpoint"
    messages: Messages
    held_effects: tuple[EffectPin, ...] = Field(max_length=16)


class Seal(JournalModel):
    kind: Literal["seal"] = "seal"
    ended: Literal["returned", "failed", "cancelled", "limit_reached", "interrupted"]


Body = Annotated[Begin | Reserve | Observe | Checkpoint | Seal, Field(discriminator="kind")]


class AttemptEvent(JournalModel):
    version: Literal[1]
    authority: AttemptAuthority
    attempt_id: Identifier
    sequence: Positive
    expected_cursor: Counter
    previous_sha256: Digest | None
    operation_id: Identifier
    request_sha256: Digest
    recorded_at: Stamp
    body: Body

    @field_validator("version", mode="before")
    @classmethod
    def _version(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("journal version must be an exact integer")
        return value

    @field_validator("recorded_at")
    @classmethod
    def _time(cls, value: str) -> str:
        _stamp(value)
        return value


@dataclass(frozen=True)
class AttemptSnapshot:
    begin: AttemptEvent
    cursor: int
    messages: tuple[RecordedMessage, ...]
    pending: AttemptEvent | None
    held_effects: tuple[EffectPin, ...]
    sealed: AttemptEvent | None
    calls: int
    reserved_output_tokens: int
    reserved_input_bytes: int


_MODELS = (
    *delegation._MODELS,
    AttemptAuthority,
    AppliedBudget,
    RequestedProfile,
    ToolSchema,
    RecordedCall,
    RecordedMessage,
    SessionPin,
    Begin,
    Reserve,
    Observe,
    EffectPin,
    Checkpoint,
    Seal,
    AttemptEvent,
)


def _stamp(value: str) -> datetime:
    stamp = datetime.fromisoformat(value)
    if stamp.utcoffset() != timedelta(0):
        raise ValueError("journal time must be UTC")
    return stamp


def _plain(value: object, maximum: int) -> object:
    remaining, nodes = maximum, 16_384

    def walk(item: object, depth: int = 0) -> object:
        nonlocal remaining, nodes
        nodes -= 1
        if nodes < 0 or depth > 20:
            raise ValueError("journal structure exceeds budget")
        kind = type(item)
        if any(kind is model for model in _MODELS):
            raw_fields: object = object.__getattribute__(item, "__dict__")
            model = cast(type[Contract], kind)
            if type(raw_fields) is not dict:
                raise ValueError("undeclared journal model fields")
            fields = cast(dict[str, object], raw_fields)
            if (
                type(fields) is not dict
                or object.__getattribute__(item, "__pydantic_extra__") is not None
                or len(fields) != len(model.model_fields)
                or any(type(key) is not str for key in fields)
                or set(fields) != set(model.model_fields)
            ):
                raise ValueError("undeclared journal model fields")
            return walk(fields, depth + 1)
        if kind is dict:
            fields = cast(dict[object, object], item)
            if len(fields) > 512 or any(type(key) is not str for key in fields):
                raise ValueError("invalid journal object")
            result = {
                cast(str, walk(key, depth + 1)): walk(child, depth + 1)
                for key, child in fields.items()
            }
            remaining -= len(fields) * 2 + 2
        elif kind is tuple or kind is list:
            items = cast(tuple[object, ...] | list[object], item)
            if len(items) > 512:
                raise ValueError("journal collection exceeds budget")
            result = [walk(child, depth + 1) for child in items]
            remaining -= len(items) + 2
        elif kind is str:
            text = cast(str, item)
            if len(text) > maximum:
                raise ValueError("journal text exceeds budget")
            remaining -= (
                len(text.encode("utf-8"))
                + 2
                + sum(5 if ord(char) < 32 else 1 if char in '\\"' else 0 for char in text)
            )
            result = item
        elif item is None or kind is bool:
            remaining -= 6
            result = item
        elif kind is int:
            if not -MAX_COUNTER <= cast(int, item) <= MAX_COUNTER:
                raise ValueError("journal integer exceeds budget")
            remaining -= 22
            result = item
        elif kind is float and math.isfinite(cast(float, item)):
            remaining -= 32
            result = item
        else:
            raise ValueError("invalid journal value type")
        if remaining < 0:
            raise ValueError("journal encoding exceeds budget")
        return result

    return walk(value)


def _json(value: object, maximum: int = MAX_RECORD) -> bytes:
    try:
        raw = json.dumps(
            _plain(value, maximum),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
        if len(raw) > maximum:
            raise ValueError("encoded journal exceeds budget")
        return raw
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise DocumentError(INVALID_REQUEST, "invalid or overlarge child attempt input") from None


def _pairs(items: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in items:
        if key in result:
            raise ValueError("duplicate journal JSON key")
        result[key] = value
    return result


def _parse(raw: bytes) -> object:
    return json.loads(
        raw.decode("utf-8"),
        object_pairs_hook=_pairs,
        parse_constant=lambda _: (_ for _ in ()).throw(ValueError()),
    )


def _checked[M: Contract](model: type[M], value: M, maximum: int = MAX_RECORD) -> M:
    if type(value) is not model:
        raise DocumentError(INVALID_REQUEST, "invalid child attempt model type")
    try:
        return model.model_validate_json(_json(value, maximum), strict=True)
    except ValidationError:
        raise DocumentError(INVALID_REQUEST, "invalid child attempt input") from None


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _request(
    authority: AttemptAuthority, attempt: str | None, cursor: int, operation: str, body: Body
) -> str:
    return _sha(
        b"child-attempt-request-v1\0"
        + _json(
            {
                "authority": authority,
                "attempt": attempt,
                "cursor": cursor,
                "operation": operation,
                "body": body,
            }
        )
    )


_KEYS = (
    "project",
    "session",
    "child",
    "attempt",
    "controller",
    "action",
    "operation",
    "request_sha",
)
_CHARGE = (
    "+".join(f"length(CAST({key} AS BLOB))" for key in _KEYS) + "+length(CAST(record AS BLOB))+512"
)
_TABLES = ("child_attempt_meta", "child_attempt_events", "child_attempt_heads")
_HEAD_KEYS = ("project", "session", "child", "attempt", "controller", "start_sha", "tail_sha")
_HEAD_CHARGE = "+".join(f"length(CAST({key} AS BLOB))" for key in _HEAD_KEYS) + "+512"
_SELECT = (
    "SELECT "  # noqa: S608 - only module-owned column names
    + ",".join(
        f"CASE WHEN typeof({key})='text' AND length(CAST({key} AS BLOB))<=128 "
        f"THEN CAST({key} AS BLOB) END"
        for key in _KEYS
    )
    + (
        ",CASE WHEN typeof(sequence)='integer' THEN sequence END,"
        "CASE WHEN typeof(record)='text' THEN substr(CAST(record AS BLOB),1,524289) END,"
        "CASE WHEN typeof(bytes)='integer' THEN bytes END FROM child_attempt_events "
    )
)


class ChildAttempts:
    """Records only; existing caller owns admission, actual thread and store lifetime."""

    def __init__(
        self,
        documents: DocumentStore,
        records: DelegationRecords,
        projects: ProjectStore,
        *,
        initialize: bool = False,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        if documents is not records.documents or type(initialize) is not bool:
            raise DocumentError(INVALID_REQUEST, "child attempts require one canonical owner")
        self.documents, self.records, self.projects, self.clock = (
            documents,
            records,
            projects,
            clock,
        )
        if initialize:
            with documents._write() as db:
                count = self._presence(db)
                if count == 0:
                    db.execute(
                        "CREATE TABLE child_attempt_meta(singleton INTEGER PRIMARY KEY "
                        "CHECK(singleton=1),version INTEGER NOT NULL,"
                        "event_count INTEGER NOT NULL CHECK(event_count>=0),"
                        "child_count INTEGER NOT NULL CHECK(child_count>=0)) STRICT"
                    )
                    db.execute("INSERT INTO child_attempt_meta VALUES(1,1,0,0)")
                    db.execute(
                        "CREATE TABLE child_attempt_events(project TEXT NOT NULL,"
                        "session TEXT NOT NULL,child TEXT NOT NULL,attempt TEXT NOT NULL,"
                        "controller TEXT NOT NULL,action TEXT NOT NULL,"
                        "operation TEXT NOT NULL,request_sha TEXT NOT NULL,"
                        "sequence INTEGER NOT NULL,"
                        "record TEXT NOT NULL,bytes INTEGER NOT NULL,"
                        "PRIMARY KEY(project,session,attempt,sequence),"
                        "UNIQUE(project,session,controller,operation)) STRICT"
                    )
                    db.execute(
                        "CREATE UNIQUE INDEX child_attempt_once ON "
                        "child_attempt_events(project,session,child) WHERE action='begin'"
                    )
                    db.execute(
                        "CREATE TABLE child_attempt_heads(project TEXT NOT NULL,"
                        "session TEXT NOT NULL,"
                        "child TEXT NOT NULL,attempt TEXT NOT NULL,controller TEXT NOT NULL,"
                        "start_sha TEXT NOT NULL,tail_sha TEXT NOT NULL,cursor INTEGER NOT NULL,"
                        "bytes INTEGER NOT NULL,PRIMARY KEY(project,session,child),"
                        "UNIQUE(project,session,attempt)) STRICT"
                    )
                    for table in _TABLES:
                        for verb in ("UPDATE", "DELETE"):
                            if verb == "UPDATE" and table != "child_attempt_events":
                                continue
                            db.execute(
                                f"CREATE TRIGGER {table}_no_{verb.lower()} "
                                f"BEFORE {verb} ON {table} "
                                "BEGIN SELECT RAISE(ABORT,'child attempts are immutable'); END"
                            )
                    db.execute(
                        "CREATE TRIGGER child_attempt_meta_step "
                        "BEFORE UPDATE ON child_attempt_meta "
                        "WHEN NEW.singleton!=OLD.singleton OR NEW.version!=OLD.version "
                        "OR NEW.event_count!=OLD.event_count+1 "
                        "OR NEW.child_count NOT IN (OLD.child_count,OLD.child_count+1) "
                        "BEGIN SELECT RAISE(ABORT,'invalid journal counter transition'); END"
                    )
                    fixed = " OR ".join(
                        f"NEW.{key}!=OLD.{key}" for key in _HEAD_KEYS if key != "tail_sha"
                    )
                    db.execute(
                        "CREATE TRIGGER child_attempt_heads_step "
                        "BEFORE UPDATE ON child_attempt_heads "
                        f"WHEN {fixed} OR NEW.cursor!=OLD.cursor+1 OR NEW.bytes!=OLD.bytes "
                        "BEGIN SELECT RAISE(ABORT,'invalid original child head transition'); END"
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
            "name IN ('child_attempt_meta','child_attempt_events','child_attempt_heads')"
        ).fetchone()[0]

    @classmethod
    def _schema(cls, db: sqlite3.Connection | None) -> None:
        if db is None or cls._presence(db) == 0:
            raise DocumentError(NOT_FOUND, "child attempt history is not initialized")
        if cls._presence(db) != 3:
            raise DocumentError(INTERNAL, "child attempt schema is corrupt or unsupported")
        rows = db.execute(
            "SELECT CASE WHEN typeof(version)='integer' THEN version END,"
            "CASE WHEN typeof(event_count)='integer' AND event_count BETWEEN 0 AND 32768 "
            "THEN event_count END,"
            "CASE WHEN typeof(child_count)='integer' AND child_count BETWEEN 0 AND 32768 "
            "THEN child_count END "
            "FROM child_attempt_meta LIMIT 2"
        ).fetchall()
        if (
            len(rows) != 1
            or rows[0][0] != 1
            or any(type(value) is not int for value in rows[0])
            or rows[0][1] != db.execute("SELECT count(*) FROM child_attempt_events").fetchone()[0]
            or rows[0][2] != db.execute("SELECT count(*) FROM child_attempt_heads").fetchone()[0]
        ):
            raise DocumentError(INTERNAL, "committed child journal rows are missing or corrupt")

    @staticmethod
    def _budget(db: sqlite3.Connection, project: str | None) -> None:
        invalid = " OR ".join(
            f"typeof({key})!='text' OR length(CAST({key} AS BLOB)) NOT BETWEEN 1 AND 128"
            for key in _KEYS
        )
        found = db.execute(
            f"SELECT count(*),coalesce(sum({_CHARGE}),0),sum(CASE WHEN ({invalid}) "  # noqa: S608 - static expressions
            f"OR typeof(record)!='text' OR length(CAST(record AS BLOB))>{MAX_RECORD} "
            f"OR typeof(bytes)!='integer' OR bytes!=({_CHARGE}) "
            f"OR typeof(sequence)!='integer' OR sequence NOT BETWEEN 1 AND {MAX_EVENTS} "
            "THEN 1 ELSE 0 END) FROM child_attempt_events"
        ).fetchone()
        bad_heads = " OR ".join(
            f"typeof({key})!='text' OR length(CAST({key} AS BLOB)) NOT BETWEEN 1 AND 128"
            for key in _HEAD_KEYS
        )
        heads = db.execute(
            f"SELECT count(*),coalesce(sum({_HEAD_CHARGE}),0),sum(CASE WHEN ({bad_heads}) "  # noqa: S608 - module-owned expressions
            f"OR typeof(cursor)!='integer' OR cursor NOT BETWEEN 1 AND {MAX_EVENTS} "
            f"OR typeof(bytes)!='integer' OR bytes!=({_HEAD_CHARGE}) THEN 1 ELSE 0 END) "
            "FROM child_attempt_heads"
        ).fetchone()
        if (
            found[2]
            or heads[2]
            or found[0] + heads[0] + 1 > 32_768
            or found[1] + heads[1] + 512 > 64 * 1024 * 1024
        ):
            raise DocumentError(INVALID_REQUEST, "child attempt global quota/state refuses")
        if project is not None:
            count, size = db.execute(
                f"SELECT count(*),coalesce(sum({_CHARGE}),0) "  # noqa: S608 - static expression
                "FROM child_attempt_events WHERE project=?",
                (project,),
            ).fetchone()
            child_count, child_size = db.execute(
                f"SELECT count(*),coalesce(sum({_HEAD_CHARGE}),0) "  # noqa: S608 - static expression
                "FROM child_attempt_heads WHERE project=?",
                (project,),
            ).fetchone()
            if count + child_count > 8192 or size + child_size > 16 * 1024 * 1024:
                raise DocumentError(INVALID_REQUEST, "child attempt project quota refuses")

    @staticmethod
    def _authority(value: AttemptAuthority) -> AttemptAuthority:
        return _checked(AttemptAuthority, value)

    def _canonical(
        self,
        db: sqlite3.Connection,
        authority: AttemptAuthority,
        prepared: DelegationMutation,
        *,
        live: bool,
    ) -> None:
        parent = ParentAuthority(scope=authority.scope, parent_run_id=authority.parent_run_id)
        self.records._schema(db)
        self.records._parent(db, parent)
        saved = self.records._snapshot(db, parent, authority.delegation_id)
        if saved.prepared != prepared or (live and saved.outcome is not None):
            raise DocumentError(CONFLICT, "original prepared child changed or ended")
        if (
            prepared.scope,
            prepared.parent_run_id,
            prepared.delegation_id,
            prepared.child_run_id,
        ) != (
            authority.scope,
            authority.parent_run_id,
            authority.delegation_id,
            authority.child_run_id,
        ):
            raise DocumentError(POLICY_DENIED, "original child scope disagrees")

    @staticmethod
    def _admit(admitted: Callable[[], bool]) -> None:
        try:
            allowed = admitted()
        except Exception:
            raise DocumentError(
                POLICY_DENIED, "child controller admission is unavailable"
            ) from None
        if allowed is not True:
            raise DocumentError(POLICY_DENIED, "child controller admission refused")

    def _session(self, authority: AttemptAuthority, original: SessionSnapshot) -> SessionPin:
        if type(original) is not SessionSnapshot:
            raise DocumentError(POLICY_DENIED, "original session proof is unavailable")
        current: SessionSnapshot | None = None
        try:
            current = self.projects.session_snapshot(
                authority.scope.project_id, authority.scope.session_id
            )
            named_root = os.stat(self.projects.root, follow_symlinks=False)
            root = os.fstat(current._root)
            file = os.fstat(current._descriptor)
            named_file = os.stat(
                authority.scope.project_id + ".json", dir_fd=current._root, follow_symlinks=False
            )
            identity = (file.st_dev, file.st_ino, file.st_size, file.st_mtime_ns, file.st_ctime_ns)
            if (
                not original.matches(current)
                or current.context.environment_id != authority.scope.environment_id
                or current.session.kernel_notice_lifetime is None
                or not stat.S_ISDIR(named_root.st_mode)
                or (named_root.st_dev, named_root.st_ino) != (root.st_dev, root.st_ino)
                or not stat.S_ISREG(named_file.st_mode)
                or identity
                != (
                    named_file.st_dev,
                    named_file.st_ino,
                    named_file.st_size,
                    named_file.st_mtime_ns,
                    named_file.st_ctime_ns,
                )
            ):
                raise ValueError("session continuity changed")
            return SessionPin(
                lifetime=current.session.kernel_notice_lifetime,
                root_identity=(root.st_dev, root.st_ino),
                file_identity=identity,
            )
        except (ValueError, OSError, ProjectError):
            raise DocumentError(
                POLICY_DENIED, "original open child session is unavailable"
            ) from None
        finally:
            if current is not None:
                current.close()

    def _now(self) -> datetime:
        value = self.clock()
        if type(value) is not datetime or value.utcoffset() != timedelta(0):
            raise DocumentError(INVALID_REQUEST, "child attempt clock is unavailable")
        return value

    def _rows(
        self, db: sqlite3.Connection, authority: AttemptAuthority, attempt: str
    ) -> list[AttemptEvent]:
        result: list[AttemptEvent] = []
        select = ",".join(
            f"CASE WHEN typeof({key})='text' AND length(CAST({key} AS BLOB))<=128 "
            f"THEN CAST({key} AS BLOB) END"
            for key in _HEAD_KEYS
        )
        head = db.execute(
            "SELECT " + select + ",CASE WHEN typeof(cursor)='integer' THEN cursor END,"  # noqa: S608 - static columns
            "CASE WHEN typeof(bytes)='integer' THEN bytes END FROM child_attempt_heads "
            "WHERE project=? AND session=? AND attempt=?",
            (authority.scope.project_id, authority.scope.session_id, attempt),
        ).fetchone()
        if (
            head is None
            or any(type(value) is not bytes for value in head[:7])
            or head[:5]
            != (
                authority.scope.project_id.encode(),
                authority.scope.session_id.encode(),
                authority.child_run_id.encode(),
                attempt.encode(),
                authority.controller_id.encode(),
            )
            or type(head[7]) is not int
            or not 1 <= head[7] <= MAX_EVENTS
            or type(head[8]) is not int
            or head[8] != sum(len(value) for value in head[:7]) + 512
        ):
            raise DocumentError(INTERNAL, "original child committed head is absent or corrupt")
        total, previous = head[8], None
        for row in db.execute(
            _SELECT + "WHERE project=? AND session=? AND attempt=? ORDER BY sequence LIMIT ?",
            (authority.scope.project_id, authority.scope.session_id, attempt, MAX_EVENTS + 1),
        ):
            *keys, sequence, raw, charge = row
            if (
                any(type(key) is not bytes for key in keys)
                or type(raw) is not bytes
                or len(raw) > MAX_RECORD
                or type(sequence) is not int
                or sequence != len(result) + 1
                or type(charge) is not int
                or charge != len(raw) + sum(len(key) for key in keys) + 512
            ):
                raise DocumentError(INTERNAL, "retained child attempt row is corrupt")
            total += charge
            if total > MAX_ATTEMPT or len(result) >= MAX_EVENTS:
                raise DocumentError(INTERNAL, "retained child attempt exceeds read bounds")
            try:
                _plain(_parse(raw), MAX_RECORD)
                event = AttemptEvent.model_validate_json(raw, strict=True)
                expected = (
                    event.authority.scope.project_id,
                    event.authority.scope.session_id,
                    event.authority.child_run_id,
                    event.attempt_id,
                    event.authority.controller_id,
                    event.body.kind,
                    event.operation_id,
                    event.request_sha256,
                )
                if (
                    _json(event) != raw
                    or event.authority != authority
                    or event.attempt_id != attempt
                    or event.sequence != sequence
                    or event.expected_cursor != sequence - 1
                    or event.previous_sha256 != previous
                    or tuple(key.decode("utf-8") for key in keys) != expected
                    or event.request_sha256
                    != _request(
                        authority,
                        None if event.body.kind == "begin" else attempt,
                        event.expected_cursor,
                        event.operation_id,
                        event.body,
                    )
                ):
                    raise ValueError("canonical row mismatch")
            except (ValueError, TypeError, UnicodeError, RecursionError, DocumentError):
                raise DocumentError(
                    INTERNAL, "retained child attempt identity is corrupt"
                ) from None
            result.append(event)
            previous = _sha(raw)
        if not result:
            raise DocumentError(NOT_FOUND, "original child attempt is absent")
        if (
            len(result) != head[7]
            or _sha(_json(result[0])).encode() != head[5]
            or previous is None
            or previous.encode() != head[6]
        ):
            raise DocumentError(INTERNAL, "original child committed tail is missing or corrupt")
        starts = db.execute(
            "SELECT CASE WHEN typeof(attempt)='text' AND length(CAST(attempt AS BLOB))<=128 "
            "THEN CAST(attempt AS BLOB) END FROM child_attempt_events "
            "WHERE project=? AND session=? AND child=? AND action='begin' LIMIT 2",
            (authority.scope.project_id, authority.scope.session_id, authority.child_run_id),
        ).fetchall()
        if starts != [(attempt.encode("utf-8"),)]:
            raise DocumentError(INTERNAL, "original child attempt uniqueness is corrupt")
        return result

    @staticmethod
    def _prefix(
        messages: tuple[RecordedMessage, ...], previous: tuple[RecordedMessage, ...], system: str
    ) -> None:
        if (
            messages[: len(previous)] != previous
            or messages[0].role != "system"
            or messages[0].content != system
            or any(message.role == "system" for message in messages[1:])
        ):
            raise DocumentError(CONFLICT, "child conversation prefix changed")
        _json(messages, MAX_INPUT)

    @classmethod
    def _state(cls, events: list[AttemptEvent]) -> AttemptSnapshot:
        first = events[0]
        if type(first.body) is not Begin:
            raise DocumentError(INTERNAL, "child attempt lacks its original input")
        start = first.body
        brief = start.prepared.brief
        if (
            brief is None
            or (start.profile.profile_id, start.profile.profile_sha256)
            != (brief.profile.profile_id, brief.profile.profile_sha256)
            or (
                brief.profile.model_alias is not None
                and start.profile.alias != brief.profile.model_alias
            )
        ):
            raise DocumentError(CONFLICT, "original child profile intent changed")
        cls._prefix(start.messages, (), start.system)
        names = [tool.name for tool in start.tools]
        if len(names) != len(set(names)):
            raise DocumentError(INTERNAL, "child offered tools are not unique")
        for text in (tool.parameters_json for tool in start.tools):
            try:
                parsed = _parse(text.encode("utf-8"))
                if type(parsed) is not dict:
                    raise ValueError("settings/schema must be objects")
                _plain(cast(object, parsed), MAX_INPUT)
            except (ValueError, TypeError, UnicodeError, RecursionError):
                raise DocumentError(INVALID_REQUEST, "invalid child settings/tool schema") from None
        messages = start.messages
        pending: AttemptEvent | None = None
        held: tuple[EffectPin, ...] = ()
        sealed: AttemptEvent | None = None
        calls = output = inputs = 0
        for event in events[1:]:
            body = event.body
            if type(body) is Reserve:
                if pending is not None or sealed is not None or held:
                    raise DocumentError(CONFLICT, "original child call/effect is held or sealed")
                cls._prefix(body.messages, messages, start.system)
                calls += 1
                output += body.output_tokens
                inputs += len(_json(body.messages, MAX_INPUT)) + len(_json(start.tools, MAX_INPUT))
                if (
                    calls > start.budget.max_calls
                    or body.output_tokens > start.budget.per_call_output_tokens
                    or output > start.budget.total_output_tokens
                    or inputs > start.budget.cumulative_input_bytes
                    or not _stamp(first.recorded_at)
                    <= _stamp(event.recorded_at)
                    <= _stamp(first.recorded_at) + timedelta(seconds=start.budget.wall_seconds)
                ):
                    raise DocumentError(CONFLICT, "explicit child reservation budget exhausted")
                pending = event
                messages = body.messages
            elif type(body) is Observe:
                if pending is None or body.reservation != pending.sequence:
                    raise DocumentError(
                        CONFLICT, "observation lacks the exact original reservation"
                    )
                _json(body, MAX_RESPONSE)
                if body.outcome == "returned":
                    if body.response is None:
                        raise DocumentError(INTERNAL, "response is missing")
                    messages = (*messages, body.response)
                    if len(messages) > 128:
                        raise DocumentError(
                            INVALID_REQUEST, "complete child conversation exceeds message limit"
                        )
                    _json(messages, MAX_INPUT)
                    pending = None
            elif type(body) is Checkpoint:
                cls._prefix(body.messages, messages, start.system)
                if len({item.operation_id for item in body.held_effects}) != len(
                    body.held_effects
                ) or any(item not in body.held_effects for item in held):
                    raise DocumentError(CONFLICT, "unresolved original effects cannot be removed")
                messages, held = body.messages, body.held_effects
            elif type(body) is Seal:
                if sealed is not None:
                    raise DocumentError(CONFLICT, "child local ending is immutable")
                sealed = event
            else:
                raise DocumentError(INTERNAL, "child attempt has another begin")
        return AttemptSnapshot(
            first, events[-1].sequence, messages, pending, held, sealed, calls, output, inputs
        )

    def get(self, authority: AttemptAuthority, attempt: str) -> AttemptSnapshot:
        authority = self._authority(authority)
        attempt = delegation._identifier(attempt)
        with self.documents._read() as db:
            self._schema(db)
            if db is None:
                raise DocumentError(NOT_FOUND, "child attempt is absent")
            state = self._state(self._rows(db, authority, attempt))
            self._canonical(db, authority, cast(Begin, state.begin.body).prepared, live=False)
            return state

    def _replay(
        self, db: sqlite3.Connection, authority: AttemptAuthority, operation: str, digest: str
    ) -> AttemptEvent | None:
        row = db.execute(
            "SELECT CASE WHEN typeof(attempt)='text' AND length(CAST(attempt AS BLOB))<=128 "
            "THEN CAST(attempt AS BLOB) END FROM child_attempt_events "
            "WHERE project=? AND session=? "
            "AND controller=? AND operation=?",
            (
                authority.scope.project_id,
                authority.scope.session_id,
                authority.controller_id,
                operation,
            ),
        ).fetchone()
        if row is None:
            return None
        try:
            if type(row[0]) is not bytes:
                raise ValueError("invalid replay key")
            attempt = delegation._identifier(row[0].decode("utf-8"))
        except (ValueError, DocumentError):
            raise DocumentError(INTERNAL, "replay attempt is corrupt") from None
        events = self._rows(db, authority, attempt)
        self._state(events)
        self._canonical(db, authority, cast(Begin, events[0].body).prepared, live=False)
        matches = [event for event in events if event.operation_id == operation]
        if len(matches) != 1:
            raise DocumentError(INTERNAL, "replay operation identity is corrupt")
        if matches[0].request_sha256 != digest:
            raise DocumentError(CONFLICT, "operation ID names another child attempt mutation")
        return matches[0]

    def _append(
        self,
        authority: AttemptAuthority,
        operation: str,
        attempt: str | None,
        cursor: int,
        body: Body,
        *,
        original: SessionSnapshot | None = None,
        admitted: Callable[[], bool] | None = None,
    ) -> AttemptEvent:
        authority = self._authority(authority)
        operation = delegation._identifier(operation)
        cursor = delegation._counter(cursor)
        body = _checked(type(body), body)
        digest = _request(authority, attempt, cursor, operation, body)
        live = type(body) is Begin or type(body) is Reserve
        with self.documents._read() as db:
            self._schema(db)
            if db is not None:
                replay = self._replay(db, authority, operation, digest)
                if replay is not None:
                    return replay
        with (
            self.projects._locked(authority.scope.project_id) if live else nullcontext(),
            self.documents._write() as db,
        ):
            self._schema(db)
            replay = self._replay(db, authority, operation, digest)
            if replay is not None:
                return replay
            if type(body) is Begin:
                if cursor or attempt is not None or body.prepared.action != "prepare":
                    raise DocumentError(
                        INVALID_REQUEST, "begin requires an original prepared child"
                    )
                if db.execute(
                    "SELECT 1 FROM child_attempt_events WHERE project=? AND session=? AND child=?",
                    (
                        authority.scope.project_id,
                        authority.scope.session_id,
                        authority.child_run_id,
                    ),
                ).fetchone():
                    raise DocumentError(
                        CONFLICT, "original child already has an attempt; no implicit retry"
                    )
                self._canonical(db, authority, body.prepared, live=True)
                attempt = new_id("cat")
                events = []
            else:
                if attempt is None:
                    raise DocumentError(INVALID_REQUEST, "original attempt is required")
                attempt = delegation._identifier(attempt)
                events = self._rows(db, authority, attempt)
                state = self._state(events)
                if cursor != state.cursor:
                    raise DocumentError(CONFLICT, "child attempt cursor changed")
                self._canonical(db, authority, cast(Begin, state.begin.body).prepared, live=live)
            expected: SessionPin | None = None
            if live:
                if original is None or admitted is None:
                    raise DocumentError(
                        POLICY_DENIED, "new child work needs original controller/session proof"
                    )
                self._admit(admitted)
                pin = self._session(authority, original)
                expected = (
                    body.session if type(body) is Begin else cast(Begin, events[0].body).session
                )
                if pin != expected:
                    raise DocumentError(POLICY_DENIED, "original child session continuity changed")
            event = AttemptEvent(
                version=1,
                authority=authority,
                attempt_id=attempt,
                sequence=len(events) + 1,
                expected_cursor=cursor,
                previous_sha256=_sha(_json(events[-1])) if events else None,
                operation_id=operation,
                request_sha256=digest,
                recorded_at=self._now().isoformat(),
                body=body,
            )
            if event.sequence > MAX_EVENTS:
                raise DocumentError(INVALID_REQUEST, "child attempt event limit reached")
            self._state([*events, event])
            raw = _json(event)
            keys = (
                authority.scope.project_id,
                authority.scope.session_id,
                authority.child_run_id,
                attempt,
                authority.controller_id,
                body.kind,
                operation,
                digest,
            )
            charge = len(raw) + sum(len(key.encode("utf-8")) for key in keys) + 512
            db.execute(
                "INSERT INTO child_attempt_events VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (*keys, event.sequence, raw.decode("utf-8"), charge),
            )
            tail = _sha(raw)
            if type(body) is Begin:
                header = (*keys[:5], tail, tail)
                # keys[:5] is project/session/child/attempt/controller.
                db.execute(
                    "INSERT INTO child_attempt_heads VALUES(?,?,?,?,?,?,?,?,?)",
                    (*header, event.sequence, sum(len(key.encode()) for key in header) + 512),
                )
            else:
                db.execute(
                    "UPDATE child_attempt_heads SET cursor=?,tail_sha=? "
                    "WHERE project=? AND session=? AND child=? AND attempt=? AND controller=?",
                    (event.sequence, tail, *keys[:5]),
                )
            db.execute(
                "UPDATE child_attempt_meta SET event_count=event_count+1,child_count=child_count+?",
                (int(type(body) is Begin),),
            )
            self._budget(db, authority.scope.project_id)
            self._schema(db)
            self._rows(db, authority, attempt)  # aggregate attempt cap before commit
            if live:
                self._canonical(
                    db,
                    authority,
                    body.prepared if type(body) is Begin else cast(Begin, events[0].body).prepared,
                    live=True,
                )
                self._admit(cast(Callable[[], bool], admitted))
                if (
                    expected is None
                    or self._session(authority, cast(SessionSnapshot, original)) != expected
                ):
                    raise DocumentError(
                        POLICY_DENIED, "original child session changed before commit"
                    )
            return event

    def begin_attempt(
        self,
        authority: AttemptAuthority,
        operation: str,
        *,
        prepared: DelegationMutation,
        original: SessionSnapshot,
        session: SessionPin,
        profile: RequestedProfile,
        budget: AppliedBudget,
        system: str,
        tools: tuple[ToolSchema, ...],
        messages: tuple[RecordedMessage, ...],
        admitted: Callable[[], bool],
    ) -> AttemptEvent:
        authority = self._authority(authority)
        prepared = delegation._checked(DelegationMutation, prepared)
        try:
            body = Begin.model_validate_json(
                _json(
                    {
                        "kind": "begin",
                        "prepared": prepared,
                        "session": session,
                        "role": "delegated",
                        "profile": profile,
                        "budget": budget,
                        "system": system,
                        "tools": tools,
                        "messages": messages,
                    }
                ),
                strict=True,
            )
        except ValidationError:
            raise DocumentError(INVALID_REQUEST, "invalid child attempt begin input") from None
        return self._append(
            authority, operation, None, 0, body, original=original, admitted=admitted
        )

    def pin_session(self, authority: AttemptAuthority, original: SessionSnapshot) -> SessionPin:
        """Bounded local continuity observation, never an admission or grant."""
        return self._session(self._authority(authority), original)

    def reserve_turn(
        self,
        authority: AttemptAuthority,
        operation: str,
        attempt: str,
        cursor: int,
        request: Reserve,
        *,
        original: SessionSnapshot,
        admitted: Callable[[], bool],
    ) -> AttemptEvent:
        return self._append(
            authority,
            operation,
            attempt,
            cursor,
            _checked(Reserve, request, MAX_INPUT),
            original=original,
            admitted=admitted,
        )

    def observe_turn(
        self,
        authority: AttemptAuthority,
        operation: str,
        attempt: str,
        cursor: int,
        observation: Observe,
    ) -> AttemptEvent:
        return self._append(
            authority, operation, attempt, cursor, _checked(Observe, observation, MAX_RESPONSE)
        )

    def checkpoint(
        self,
        authority: AttemptAuthority,
        operation: str,
        attempt: str,
        cursor: int,
        checkpoint: Checkpoint,
    ) -> AttemptEvent:
        return self._append(
            authority, operation, attempt, cursor, _checked(Checkpoint, checkpoint, MAX_INPUT)
        )

    def seal_local_attempt(
        self, authority: AttemptAuthority, operation: str, attempt: str, cursor: int, ending: Seal
    ) -> AttemptEvent:
        return self._append(authority, operation, attempt, cursor, _checked(Seal, ending))
