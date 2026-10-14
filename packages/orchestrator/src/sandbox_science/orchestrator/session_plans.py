# SPDX-License-Identifier: MIT
"""Internal immutable session plans and attributed progress, used by the plan tools
(`plan_tools`, for MAIN runs the harness builds) and by the workspace's plan views and
USER plan editing.

Authority is a trusted caller capability, never a field decoded from model requests.
This module borrows DocumentStore's connection/transaction and never closes it.
Its synchronous calls must finish before that owner closes the store.
"""

from __future__ import annotations

import copy
import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated, Literal, Self, TypeVar

from pydantic import (
    AwareDatetime,
    BeforeValidator,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from sandbox_science.contracts import Contract, Identifier, new_id

from .documents.errors import (
    CONFLICT,
    INTERNAL,
    INVALID_REQUEST,
    NOT_FOUND,
    POLICY_DENIED,
    DocumentError,
)
from .documents.store import DocumentStore

MAX_COUNTER = 2**62
MAX_BODY = 65_536
MAX_RECORD = 81_920
MAX_PAGE = 262_144
Counter = Annotated[int, Field(strict=True, ge=0, le=MAX_COUNTER)]
Positive = Annotated[int, Field(strict=True, ge=1, le=MAX_COUNTER)]
Text = Annotated[str, Field(min_length=1, max_length=4096)]
Title = Annotated[str, Field(min_length=1, max_length=128)]
Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


def _status(value: object) -> object:
    """A plan stored with the removed ``awaiting_approval`` status reads as a draft,
    which fences effects (decision 10)."""
    return "draft" if value == "awaiting_approval" else value


PlanStatus = Annotated[
    Literal["draft", "active", "completed", "abandoned"], BeforeValidator(_status)
]
StepStatus = Literal["pending", "in_progress", "completed", "blocked", "skipped"]
Action = Literal["publish", "revise", "progress", "status"]


class PlanModel(Contract):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    @field_validator("*")
    @classmethod
    def _text_encoding(cls, value: object) -> object:
        if isinstance(value, str):
            value.encode("utf-8", "strict")
            if "\0" in value or not value.strip():
                raise ValueError("plan text must be nonempty and cannot contain NUL")
        return value


class PlanScope(PlanModel):
    project_id: Identifier
    session_id: Identifier


class PlanRef(PlanModel):
    plan_id: Identifier
    version: Positive


class PlanAuthority(PlanModel):
    """Trusted control attribution; construction is not authentication or a public API."""

    scope: PlanScope
    role: Literal["user", "main", "delegated"]
    user_id: Identifier | None = None
    run_id: Identifier | None = None
    alias: Annotated[str, Field(min_length=1, max_length=256)] | None = None
    resolved_model: Annotated[str, Field(min_length=1, max_length=256)] | None = None
    tool_call: Identifier | None = None
    grant: PlanRef | None = None
    track_id: Identifier | None = None

    @model_validator(mode="after")
    def _positive(self) -> Self:
        if self.role == "user":
            if self.user_id is None or any(
                value is not None
                for value in (
                    self.run_id,
                    self.alias,
                    self.resolved_model,
                    self.tool_call,
                    self.grant,
                    self.track_id,
                )
            ):
                raise ValueError("USER attribution requires only its trusted user identity")
        elif self.user_id is not None or self.run_id is None or self.run_id.casefold() == "user":
            raise ValueError("agent attribution needs a trusted run and cannot impersonate USER")
        if self.role == "delegated":
            if self.grant is None or self.track_id is None:
                raise ValueError("delegated progress requires an exact plan/track grant")
        elif self.grant is not None or self.track_id is not None:
            raise ValueError("only delegated authority carries a track grant")
        return self

    @property
    def namespace(self) -> tuple[str, str]:
        """A tuple prevents delimiter collisions between role and principal IDs."""
        return self.role, self.user_id or self.run_id or ""


class PlanEvidence(PlanModel):
    """A same-project claimed reference, never lookup authority or verified evidence."""

    project_id: Identifier
    kind: Literal["artifact", "execution", "ledger"]
    reference_id: Identifier
    revision: Positive | None = None
    sha256: Digest | None = None
    attempt_id: Identifier | None = None

    @model_validator(mode="after")
    def _pin(self) -> Self:
        if self.kind == "artifact":
            if self.revision is None or self.sha256 is None or self.attempt_id is not None:
                raise ValueError("artifact claim needs an immutable revision/digest")
        elif self.kind == "execution":
            if self.attempt_id is None or self.revision is not None or self.sha256 is not None:
                raise ValueError("execution claim needs an exact attempt")
        elif self.revision is None or self.sha256 is not None or self.attempt_id is not None:
            raise ValueError("ledger claim needs a revision")
        return self


class PlanOutput(PlanModel):
    title: Title
    description: Text
    evidence_standard: Text


class PlanTrack(PlanModel):
    track_id: Identifier
    title: Title


class PlanStep(PlanModel):
    step_id: Identifier
    title: Title
    description: Text
    evidence_standard: Text
    track_id: Identifier | None = None


class PlanPhase(PlanModel):
    phase_id: Identifier
    title: Title
    steps: tuple[PlanStep, ...] = Field(min_length=1, max_length=128)


class PlanBody(PlanModel):
    goal: Text
    feasibility: Text
    outputs: tuple[PlanOutput, ...] = Field(min_length=1, max_length=32)
    tracks: tuple[PlanTrack, ...] = Field(default=(), max_length=16)
    phases: tuple[PlanPhase, ...] = Field(min_length=1, max_length=16)

    @model_validator(mode="after")
    def _unique(self) -> Self:
        tracks = {track.track_id for track in self.tracks}
        phases = {phase.phase_id for phase in self.phases}
        steps = [step for phase in self.phases for step in phase.steps]
        if len(tracks) != len(self.tracks) or len(phases) != len(self.phases):
            raise ValueError("plan phase/track IDs must be unique")
        if len(steps) > 128 or len({step.step_id for step in steps}) != len(steps):
            raise ValueError("plan step IDs must be unique and bounded")
        if len({step.title.casefold() for step in steps}) != len(steps):
            raise ValueError("plan step titles must be unique")
        if any(step.track_id is not None and step.track_id not in tracks for step in steps):
            raise ValueError("plan step names an absent track")
        return self


class GoalOutcome(PlanModel):
    """An attributed goal assertion, distinct from terminal workflow accounting."""

    status: Literal["achieved", "partially_achieved", "not_achieved"]
    summary: Text
    evidence: tuple[PlanEvidence, ...] = Field(max_length=8)
    remaining_work: tuple[Text, ...] = Field(max_length=16)

    @model_validator(mode="after")
    def _remaining(self) -> Self:
        if self.status == "achieved" and self.remaining_work:
            raise ValueError("an achieved outcome cannot have remaining work")
        if self.status != "achieved" and not self.remaining_work:
            raise ValueError("an unachieved outcome must state remaining work")
        return self


class PlanVersion(PlanModel):
    scope: PlanScope
    ref: PlanRef
    base: Positive | None
    body: PlanBody
    author: PlanAuthority
    reason: Text
    recorded_at: AwareDatetime


class PlanProgress(PlanModel):
    scope: PlanScope
    ref: PlanRef
    sequence: Positive
    step_id: Identifier
    step_sha256: Digest
    status: StepStatus
    note: Text
    evidence: tuple[PlanEvidence, ...] = Field(default=(), max_length=8)
    author: PlanAuthority
    recorded_at: AwareDatetime


class PlanStatusChange(PlanModel):
    scope: PlanScope
    ref: PlanRef
    revision: Positive
    progress_cursor: Counter
    status: PlanStatus
    outcome: GoalOutcome | None = None
    reason: Text
    author: PlanAuthority
    recorded_at: AwareDatetime

    @model_validator(mode="after")
    def _outcome(self) -> Self:
        if (self.status == "completed") != (self.outcome is not None):
            raise ValueError("only completed workflow status carries an explicit goal outcome")
        return self


class PlanHead(PlanModel):
    scope: PlanScope
    ref: PlanRef
    status_revision: Positive
    progress_cursor: Counter
    status: PlanStatus
    outcome: GoalOutcome | None = None

    @model_validator(mode="after")
    def _outcome(self) -> Self:
        if (self.status == "completed") != (self.outcome is not None):
            raise ValueError("completed workflow needs its explicit goal outcome")
        return self


class CurrentPlan(PlanModel):
    scope: PlanScope
    ref: PlanRef
    event_cursor: Positive


class PlanEvent(PlanModel):
    scope: PlanScope
    sequence: Positive
    operation_id: Identifier
    request_sha256: Digest
    action: Action
    ref: PlanRef
    number: Counter
    status_revision: Positive
    progress_cursor: Counter
    author: PlanAuthority
    recorded_at: AwareDatetime


class PlanReceipt(PlanModel):
    """The acknowledged historical mutation; it never claims to be the current head."""

    scope: PlanScope
    operation_id: Identifier
    action: Action
    ref: PlanRef
    event_sequence: Positive
    status_revision: Positive
    progress_cursor: Counter


class PlanRequest(PlanModel):
    authority: PlanAuthority
    operation_id: Identifier
    digest: Digest
    receipt: PlanReceipt


class StepProjection(PlanModel):
    step_id: Identifier
    step_sha256: Digest
    latest: PlanProgress | None
    matches_current_step: bool


class PlanSnapshot(PlanModel):
    event_cursor: Positive
    head: PlanHead
    version: PlanVersion
    steps: tuple[StepProjection, ...] = Field(max_length=128)


class CompactProgress(PlanModel):
    """Exact assertion identity with long note/evidence content explicitly omitted."""

    ref: PlanRef
    sequence: Positive
    step_sha256: Digest
    status: StepStatus
    author: PlanAuthority
    recorded_at: AwareDatetime
    evidence_count: Annotated[int, Field(strict=True, ge=0, le=8)]


class CompactStep(PlanModel):
    step_id: Identifier
    step_sha256: Digest
    latest: CompactProgress | None
    matches_current_step: bool


class CompactPlan(PlanModel):
    event_cursor: Positive
    head: PlanHead
    version: PlanVersion
    steps: tuple[CompactStep, ...] = Field(max_length=128)


class PlanSummary(PlanModel):
    """Small safe-point identity; full bodies and progress require explicit reads."""

    head: PlanRef
    status: PlanStatus
    status_revision: Positive
    progress_cursor: Counter
    event_cursor: Positive
    goal_outcome: Literal["achieved", "partially_achieved", "not_achieved"] | None
    last_event: PlanEvent


class PlanLimits(PlanModel):
    """Logical retention budgets, including zero-content and auxiliary records."""

    project_versions: Annotated[int, Field(strict=True, ge=1, le=4096)] = 1024
    total_versions: Annotated[int, Field(strict=True, ge=1, le=16384)] = 4096
    project_progress: Annotated[int, Field(strict=True, ge=1, le=32768)] = 8192
    total_progress: Annotated[int, Field(strict=True, ge=1, le=131072)] = 32768
    project_records: Annotated[int, Field(strict=True, ge=1, le=131072)] = 32768
    total_records: Annotated[int, Field(strict=True, ge=1, le=524288)] = 131072
    metadata_bytes: Annotated[int, Field(strict=True, ge=1024, le=128 * 1024 * 1024)] = (
        32 * 1024 * 1024
    )


M = TypeVar("M", bound=PlanModel)
H = TypeVar("H", PlanVersion, PlanProgress, PlanStatusChange, PlanEvent)


@dataclass(frozen=True)
class PlanPage[H: (PlanVersion, PlanProgress, PlanStatusChange, PlanEvent)]:
    items: tuple[H, ...]
    after: int
    target: int
    more: bool


def _checked[M: PlanModel](model: type[M], value: M) -> M:
    try:
        return model.model_validate(value.model_dump(mode="python"), strict=True)
    except (ValidationError, ValueError, TypeError) as error:
        raise DocumentError(INVALID_REQUEST, "invalid plan input or authority") from error


def _json(value: object, limit: int = MAX_RECORD) -> str:
    if isinstance(value, PlanModel):
        value = value.model_dump(mode="json")
    try:
        text = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        if len(text) > limit:
            raise ValueError("metadata limit")
        return text
    except (ValueError, UnicodeError, RecursionError) as error:
        raise DocumentError(
            INVALID_REQUEST, "plan metadata exceeds its encoding/byte limit"
        ) from error


def _decode[M: PlanModel](model: type[M], raw: object) -> M:
    if not isinstance(raw, bytes) or len(raw) > MAX_RECORD:
        raise DocumentError(INTERNAL, "retained plan metadata is unavailable")
    try:
        return model.model_validate_json(raw, strict=True)
    except (ValidationError, ValueError, TypeError, RecursionError) as error:
        raise DocumentError(INTERNAL, "retained plan metadata is invalid") from error


def _digest(value: object) -> str:
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _steps(body: PlanBody) -> dict[str, tuple[PlanStep, str]]:
    return {
        step.step_id: (
            step,
            _digest({"phase_id": phase.phase_id, "step": step.model_dump(mode="json")}),
        )
        for phase in body.phases
        for step in phase.steps
    }


class SessionPlans:
    """Bounded internal plans sharing, but never owning, DocumentStore lifetime.

    A change needs its session recorded and open, checked in the change's own
    transaction, so a session's close cannot land between the check and the change.
    """

    def __init__(self, documents: DocumentStore, *, limits: PlanLimits | None = None) -> None:
        self.documents = documents
        self.limits = _checked(PlanLimits, limits or PlanLimits())
        self.opening: str | None = None
        if not documents.readonly:
            with documents.control.write() as tx:
                self._budget(tx.sql, None)

    def pinned(self, opening: str) -> SessionPlans:
        """These plans for one opening of the session, named by ``opening``, the random
        marker each open gives the session (``kernel_notice_lifetime``): a change is
        refused once the session has been closed, even if it was opened again."""
        pinned = copy.copy(self)
        pinned.opening = opening
        return pinned

    def has_records(self, scope: PlanScope) -> bool:
        """Whether the store keeps any plan record of the session, even without a current
        pointer, which tells a damaged plan from an absent one."""
        scope = _checked(PlanScope, scope)
        if self.documents.control.missing:
            return False
        with self.documents.control.read() as tx:
            return (
                tx.sql.execute(
                    "SELECT 1 FROM session_plan_records WHERE project=? AND session=? "
                    "UNION ALL SELECT 1 FROM session_plan_projections WHERE "
                    "project=? AND session=? LIMIT 1",
                    (scope.project_id, scope.session_id) * 2,
                ).fetchone()
                is not None
            )

    def _session_open(self, db: sqlite3.Connection, scope: PlanScope) -> None:
        row = db.execute(
            "SELECT kernel_notice_lifetime, closed_at FROM sessions "
            "WHERE project_id=? AND session_id=?",
            (scope.project_id, scope.session_id),
        ).fetchone()
        if (
            row is None
            or row[1] is not None
            or (self.opening is not None and row[0] != self.opening)
        ):
            raise DocumentError(
                CONFLICT,
                "the session is closed, or was closed and opened again",
                {"session": "closed"},
            )

    def _budget(self, db: sqlite3.Connection, scope: PlanScope | None) -> None:
        limits = self.limits
        budgets: list[tuple[str | None, int, int, int]] = [
            (None, limits.total_versions, limits.total_progress, limits.total_records),
        ]
        if scope is not None:
            budgets.append(
                (
                    scope.project_id,
                    limits.project_versions,
                    limits.project_progress,
                    limits.project_records,
                )
            )
        for project, versions, progress, records in budgets:
            count, size, bad, version_count, progress_count = db.execute(
                "SELECT count(*),coalesce(sum(bytes),0),coalesce(sum(CASE WHEN "
                "typeof(bytes)!='integer' OR bytes<512 OR bytes>? OR "
                "bytes!=length(CAST(record AS BLOB))+512 THEN 1 ELSE 0 "
                "END),0),coalesce(sum(kind='version'),0),coalesce(sum(kind='progress'),0)"
                " FROM session_plan_records WHERE ? IS NULL OR project=?",
                (MAX_RECORD + 512, project, project),
            ).fetchone()
            projection_count, projection_size, projection_bad = db.execute(
                "SELECT count(*),coalesce(sum(bytes),0),coalesce(sum(CASE WHEN "
                "typeof(bytes)!='integer' OR bytes<512 OR bytes>? OR "
                "bytes!=length(CAST(record AS BLOB))+512 THEN 1 ELSE 0 END),0) FROM "
                "session_plan_projections WHERE ? IS NULL OR project=?",
                (MAX_RECORD + 512, project, project),
            ).fetchone()
            if bad or projection_bad:
                raise DocumentError(INTERNAL, "retained plan accounting is invalid")
            if (
                count + projection_count > records
                or version_count > versions
                or progress_count > progress
                or size + projection_size > limits.metadata_bytes
            ):
                raise DocumentError(
                    INVALID_REQUEST, "session-plan retention quota is full or has tightened"
                )

    @staticmethod
    def _scope(model: M, scope: PlanScope) -> M:
        if getattr(model, "scope", None) != scope:
            raise DocumentError(INTERNAL, "retained plan scope is invalid")
        return model

    def _projection(
        self, db: sqlite3.Connection, scope: PlanScope, kind: str, identifier: str, model: type[M]
    ) -> M | None:
        row = db.execute(
            "SELECT substr(CAST(record AS BLOB),1,?) FROM session_plan_projections WHERE "
            "project=? AND session=? AND kind=? AND id=?",
            (MAX_RECORD + 1, scope.project_id, scope.session_id, kind, identifier),
        ).fetchone()
        return self._scope(_decode(model, row[0]), scope) if row else None

    def _record(
        self,
        db: sqlite3.Connection,
        scope: PlanScope,
        kind: str,
        identifier: str,
        number: int,
        model: type[M],
    ) -> M:
        row = db.execute(
            "SELECT substr(CAST(record AS BLOB),1,?) FROM session_plan_records WHERE "
            "project=? AND session=? AND kind=? AND id=? AND number=?",
            (MAX_RECORD + 1, scope.project_id, scope.session_id, kind, identifier, number),
        ).fetchone()
        if not row:
            raise DocumentError(NOT_FOUND, "plan record not found in this session")
        value = _decode(model, row[0])
        if isinstance(value, PlanRequest):
            if value.authority.scope != scope or value.receipt.scope != scope:
                raise DocumentError(INTERNAL, "retained request scope is invalid")
            return value
        return self._scope(value, scope)

    @staticmethod
    def _append(
        db: sqlite3.Connection,
        scope: PlanScope,
        kind: str,
        identifier: str,
        number: int,
        value: PlanModel,
        key: str = "",
    ) -> None:
        record = _json(value)
        db.execute(
            "INSERT INTO session_plan_records VALUES(?,?,?,?,?,?,?,?)",
            (
                scope.project_id,
                scope.session_id,
                kind,
                identifier,
                number,
                key,
                record,
                len(record) + 512,
            ),
        )

    @staticmethod
    def _put(
        db: sqlite3.Connection, scope: PlanScope, kind: str, identifier: str, value: PlanModel
    ) -> None:
        record = _json(value)
        db.execute(
            "INSERT INTO session_plan_projections VALUES(?,?,?,?,?,?) ON "
            "CONFLICT(project,session,kind,id) DO UPDATE SET "
            "record=excluded.record,bytes=excluded.bytes",
            (scope.project_id, scope.session_id, kind, identifier, record, len(record) + 512),
        )

    def _current(self, db: sqlite3.Connection, scope: PlanScope) -> CurrentPlan | None:
        pointer = self._projection(db, scope, "current", "", CurrentPlan)
        if pointer is not None:
            event = self._record(db, scope, "event", "", pointer.event_cursor, PlanEvent)
            if (
                event.ref != pointer.ref
                or event.sequence != pointer.event_cursor
                or event.author.scope != scope
            ):
                raise DocumentError(INTERNAL, "retained current plan cursor is incoherent")
        return pointer

    def _head(
        self, db: sqlite3.Connection, scope: PlanScope, ref: PlanRef, *, current: bool = True
    ) -> PlanHead:
        pointer: CurrentPlan | None = None
        if current:
            pointer = self._current(db, scope)
            if pointer is None:
                raise DocumentError(NOT_FOUND, "this session has no current plan")
            if pointer.ref != ref:
                raise DocumentError(
                    CONFLICT,
                    "plan version changed; reread the current plan",
                    details={"current": pointer.ref.model_dump(mode="json")},
                )
        head = self._projection(db, scope, "head", ref.plan_id, PlanHead)
        if head is None:
            raise DocumentError(NOT_FOUND, "plan not found in this session")
        if head.ref != ref:
            raise DocumentError(
                CONFLICT,
                "plan version changed; reread its current version",
                details={"current": head.ref.model_dump(mode="json")},
            )
        status = self._record(
            db, scope, "status", ref.plan_id, head.status_revision, PlanStatusChange
        )
        if (
            status.status != head.status
            or status.outcome != head.outcome
            or status.ref.plan_id != ref.plan_id
            or status.ref.version > ref.version
            or status.progress_cursor > head.progress_cursor
            or status.author.scope != scope
            or status.revision != head.status_revision
        ):
            raise DocumentError(INTERNAL, "retained plan status is incoherent")
        for kind, expected in (
            ("version", ref.version),
            ("status", head.status_revision),
            ("progress", head.progress_cursor),
        ):
            row = db.execute(
                "SELECT CASE WHEN typeof(number)='integer' THEN number END "
                "FROM session_plan_records WHERE project=? AND session=? AND kind=? "
                "AND id=? ORDER BY number DESC LIMIT 1",
                (scope.project_id, scope.session_id, kind, ref.plan_id),
            ).fetchone()
            if (row[0] if row else 0) != expected:
                raise DocumentError(INTERNAL, "retained plan head counters are incoherent")
        if pointer is not None:
            event = self._record(db, scope, "event", "", pointer.event_cursor, PlanEvent)
            if (event.status_revision, event.progress_cursor) != (
                head.status_revision,
                head.progress_cursor,
            ):
                raise DocumentError(INTERNAL, "retained current plan event is incoherent")
        return head

    def _version(self, db: sqlite3.Connection, scope: PlanScope, ref: PlanRef) -> PlanVersion:
        version = self._record(db, scope, "version", ref.plan_id, ref.version, PlanVersion)
        if (
            version.ref != ref
            or version.author.scope != scope
            or version.base != (None if ref.version == 1 else ref.version - 1)
        ):
            raise DocumentError(INTERNAL, "retained plan version is incoherent")
        return version

    def _event(self, db: sqlite3.Connection, scope: PlanScope, sequence: int) -> PlanEvent:
        event = self._record(db, scope, "event", "", sequence, PlanEvent)
        if event.sequence != sequence or event.author.scope != scope:
            raise DocumentError(INTERNAL, "retained plan event identity is incoherent")
        if event.action in {"publish", "revise"}:
            record = self._version(db, scope, event.ref)
            actual = record.ref.version
        elif event.action == "progress":
            record = self._record(
                db, scope, "progress", event.ref.plan_id, event.number, PlanProgress
            )
            actual = record.sequence
            if event.number != event.progress_cursor:
                raise DocumentError(INTERNAL, "retained progress event cursor is incoherent")
        else:
            record = self._record(
                db, scope, "status", event.ref.plan_id, event.number, PlanStatusChange
            )
            actual = record.revision
            if event.number != event.status_revision:
                raise DocumentError(INTERNAL, "retained status event cursor is incoherent")
        if record.ref != event.ref or record.author != event.author or actual != event.number:
            raise DocumentError(INTERNAL, "retained plan event does not bind its record")
        return event

    def _latest(
        self, db: sqlite3.Connection, scope: PlanScope, ref: PlanRef, step: str
    ) -> PlanProgress | None:
        row = db.execute(
            "SELECT CASE WHEN typeof(number)='integer' THEN number END,"
            "substr(CAST(record AS BLOB),1,?) FROM session_plan_records WHERE "
            "project=? AND session=? AND kind='progress' AND id=? AND key=? ORDER BY "
            "number DESC LIMIT 1",
            (MAX_RECORD + 1, scope.project_id, scope.session_id, ref.plan_id, step),
        ).fetchone()
        if row is None:
            return None
        entry = self._scope(_decode(PlanProgress, row[1]), scope)
        if (
            entry.step_id != step
            or entry.ref.plan_id != ref.plan_id
            or entry.ref.version > ref.version
            or entry.author.scope != scope
            or entry.sequence != row[0]
        ):
            raise DocumentError(INTERNAL, "retained step progress is incoherent")
        historical = _steps(self._version(db, scope, entry.ref).body).get(step)
        if historical is None or historical[1] != entry.step_sha256:
            raise DocumentError(INTERNAL, "retained step assertion does not bind its version")
        return entry

    def summary(self, authority: PlanAuthority) -> PlanSummary | None:
        """Read the small current identity/cursors without materializing plan progress."""
        authority = _checked(PlanAuthority, authority)
        if self.documents.control.missing:
            return None
        with self.documents.control.read() as tx:
            db = tx.sql
            pointer = self._current(db, authority.scope)
            if pointer is None:
                return None
            head = self._head(db, authority.scope, pointer.ref)
            event = self._event(db, authority.scope, pointer.event_cursor)
            result = PlanSummary(
                head=head.ref,
                status=head.status,
                status_revision=head.status_revision,
                progress_cursor=head.progress_cursor,
                event_cursor=pointer.event_cursor,
                goal_outcome=head.outcome.status if head.outcome else None,
                last_event=event,
            )
            _json(result, 8192)
            return result

    @staticmethod
    def _main(authority: PlanAuthority) -> None:
        if authority.role == "delegated":
            raise DocumentError(POLICY_DENIED, "delegated runs may only update their granted track")

    @staticmethod
    def _evidence(scope: PlanScope, evidence: tuple[PlanEvidence, ...]) -> None:
        if any(item.project_id != scope.project_id for item in evidence):
            raise DocumentError(POLICY_DENIED, "plan evidence claims must belong to the project")

    def _begin(
        self,
        db: sqlite3.Connection,
        authority: PlanAuthority,
        operation: str,
        action: Action,
        payload: object,
    ) -> tuple[str, str, PlanReceipt | None]:
        # Canonical JSON tuples prevent user/run/operation delimiter ambiguity.
        operation = _checked_operation(operation)
        identifier = _digest([*authority.namespace, operation])
        digest = _digest(
            {"authority": authority.model_dump(mode="json"), "action": action, "request": payload}
        )
        row = db.execute(
            "SELECT substr(CAST(record AS BLOB),1,?) FROM session_plan_records WHERE "
            "project=? AND session=? AND kind='request' AND id=? AND number=0",
            (MAX_RECORD + 1, authority.scope.project_id, authority.scope.session_id, identifier),
        ).fetchone()
        if row:
            request = _decode(PlanRequest, row[0])
            if (
                request.authority != authority
                or request.operation_id != operation
                or request.digest != digest
            ):
                raise DocumentError(CONFLICT, "operation ID already names another plan mutation")
            receipt = request.receipt
            if (
                receipt.scope != authority.scope
                or receipt.operation_id != operation
                or receipt.action != action
            ):
                raise DocumentError(INTERNAL, "retained plan receipt is incoherent")
            event = self._event(db, authority.scope, receipt.event_sequence)
            if (
                event.action,
                event.ref,
                event.author,
                event.sequence,
                event.status_revision,
                event.progress_cursor,
                event.operation_id,
                event.request_sha256,
            ) != (
                action,
                receipt.ref,
                authority,
                receipt.event_sequence,
                receipt.status_revision,
                receipt.progress_cursor,
                operation,
                digest,
            ):
                raise DocumentError(INTERNAL, "retained plan receipt does not bind its event")
            return identifier, digest, receipt
        self._session_open(db, authority.scope)
        self._budget(db, authority.scope)
        return identifier, digest, None

    def _finish(
        self,
        db: sqlite3.Connection,
        authority: PlanAuthority,
        operation: str,
        action: Action,
        identifier: str,
        digest: str,
        head: PlanHead,
        number: int,
    ) -> PlanReceipt:
        pointer = self._current(db, authority.scope)
        sequence = 1 if pointer is None else pointer.event_cursor + 1
        if sequence > MAX_COUNTER:
            raise DocumentError(INTERNAL, "session-plan event cursor is exhausted")
        event = PlanEvent(
            scope=authority.scope,
            sequence=sequence,
            operation_id=operation,
            request_sha256=digest,
            action=action,
            ref=head.ref,
            number=number,
            status_revision=head.status_revision,
            progress_cursor=head.progress_cursor,
            author=authority,
            recorded_at=datetime.now(UTC),
        )
        self._append(db, authority.scope, "event", "", sequence, event)
        self._put(
            db,
            authority.scope,
            "current",
            "",
            CurrentPlan(scope=authority.scope, ref=head.ref, event_cursor=sequence),
        )
        self._put(db, authority.scope, "head", head.ref.plan_id, head)
        receipt = PlanReceipt(
            scope=authority.scope,
            operation_id=operation,
            action=action,
            ref=head.ref,
            event_sequence=sequence,
            status_revision=head.status_revision,
            progress_cursor=head.progress_cursor,
        )
        self._append(
            db,
            authority.scope,
            "request",
            identifier,
            0,
            PlanRequest(
                authority=authority, operation_id=operation, digest=digest, receipt=receipt
            ),
        )
        self._budget(db, authority.scope)
        return receipt

    def publish(
        self,
        authority: PlanAuthority,
        operation: str,
        body: PlanBody,
        reason: str,
        *,
        expected_current: PlanRef | None = None,
    ) -> PlanReceipt:
        """Create a new current plan after an explicit terminal predecessor, or first plan."""
        authority = _checked(PlanAuthority, authority)
        self._main(authority)
        body = _checked(PlanBody, body)
        _json(body, MAX_BODY)
        expected_current = _checked(PlanRef, expected_current) if expected_current else None
        reason = _text(reason)
        payload = {
            "body": body.model_dump(mode="json"),
            "reason": reason,
            "expected_current": expected_current.model_dump(mode="json")
            if expected_current
            else None,
        }
        with self.documents.control.write() as tx:
            db = tx.sql
            identifier, digest, replay = self._begin(db, authority, operation, "publish", payload)
            if replay:
                return replay
            pointer = self._current(db, authority.scope)
            if (pointer.ref if pointer else None) != expected_current:
                raise DocumentError(
                    CONFLICT, "current plan changed; reread before publishing another plan"
                )
            if pointer:
                previous = self._head(db, authority.scope, pointer.ref)
                if previous.status not in {"completed", "abandoned"}:
                    raise DocumentError(
                        CONFLICT, "finish or abandon the current plan before replacing it"
                    )
            ref = PlanRef(plan_id=new_id("plan"), version=1)
            self._append(
                db,
                authority.scope,
                "version",
                ref.plan_id,
                1,
                PlanVersion(
                    scope=authority.scope,
                    ref=ref,
                    base=None,
                    body=body,
                    author=authority,
                    reason=reason,
                    recorded_at=datetime.now(UTC),
                ),
            )
            head = PlanHead(
                scope=authority.scope, ref=ref, status_revision=1, progress_cursor=0, status="draft"
            )
            self._append(
                db,
                authority.scope,
                "status",
                ref.plan_id,
                1,
                PlanStatusChange(
                    scope=authority.scope,
                    ref=ref,
                    revision=1,
                    progress_cursor=0,
                    status="draft",
                    reason=reason,
                    author=authority,
                    recorded_at=datetime.now(UTC),
                ),
            )
            return self._finish(db, authority, operation, "publish", identifier, digest, head, 1)

    def revise(
        self, authority: PlanAuthority, operation: str, base: PlanRef, body: PlanBody, reason: str
    ) -> PlanReceipt:
        """Append an exact-base body version, keeping progress as attributed history."""
        authority = _checked(PlanAuthority, authority)
        self._main(authority)
        base = _checked(PlanRef, base)
        body = _checked(PlanBody, body)
        _json(body, MAX_BODY)
        reason = _text(reason)
        payload = {
            "base": base.model_dump(mode="json"),
            "body": body.model_dump(mode="json"),
            "reason": reason,
        }
        with self.documents.control.write() as tx:
            db = tx.sql
            identifier, digest, replay = self._begin(db, authority, operation, "revise", payload)
            if replay:
                return replay
            head = self._head(db, authority.scope, base)
            self._open(head)
            ref = PlanRef(plan_id=base.plan_id, version=_increment(base.version))
            self._append(
                db,
                authority.scope,
                "version",
                ref.plan_id,
                ref.version,
                PlanVersion(
                    scope=authority.scope,
                    ref=ref,
                    base=base.version,
                    body=body,
                    author=authority,
                    reason=reason,
                    recorded_at=datetime.now(UTC),
                ),
            )
            head = head.model_copy(update={"ref": ref})
            return self._finish(
                db, authority, operation, "revise", identifier, digest, head, ref.version
            )

    @staticmethod
    def _open(head: PlanHead) -> None:
        if head.status in {"completed", "abandoned"}:
            raise DocumentError(CONFLICT, "terminal plans are immutable; publish a new plan")

    def progress(
        self,
        authority: PlanAuthority,
        operation: str,
        base: PlanRef,
        step_id: str,
        expected_step_cursor: int,
        status: StepStatus,
        note: str,
        *,
        evidence: tuple[PlanEvidence, ...] = (),
        expected_status_revision: int | None = None,
    ) -> PlanReceipt:
        """Append a current-version step assertion with a per-step optimistic cursor."""
        authority = _checked(PlanAuthority, authority)
        base = _checked(PlanRef, base)
        request = PlanProgress(
            scope=authority.scope,
            ref=base,
            sequence=1,
            step_id=step_id,
            step_sha256="0" * 64,
            status=status,
            note=_text(note),
            evidence=evidence,
            author=authority,
            recorded_at=datetime.now(UTC),
        )
        request = _checked(PlanProgress, request)
        self._evidence(authority.scope, request.evidence)
        expected_step_cursor = _counter(expected_step_cursor)
        if expected_status_revision is not None:
            expected_status_revision = _counter(expected_status_revision)
        payload = {
            "base": base.model_dump(mode="json"),
            "step_id": step_id,
            "expected_step_cursor": expected_step_cursor,
            "status": status,
            "note": note,
            "evidence": [item.model_dump(mode="json") for item in request.evidence],
            "expected_status_revision": expected_status_revision,
        }
        with self.documents.control.write() as tx:
            db = tx.sql
            identifier, digest, replay = self._begin(db, authority, operation, "progress", payload)
            if replay:
                return replay
            head = self._head(db, authority.scope, base)
            self._open(head)
            if (
                expected_status_revision is not None
                and head.status_revision != expected_status_revision
            ):
                raise DocumentError(
                    CONFLICT, "plan status changed; reread before asserting progress"
                )
            steps = _steps(self._version(db, authority.scope, base).body)
            if step_id not in steps:
                raise DocumentError(NOT_FOUND, "step is absent from this current plan version")
            step, fingerprint = steps[step_id]
            if authority.role == "delegated" and (
                authority.grant != base or authority.track_id != step.track_id
            ):
                raise DocumentError(
                    POLICY_DENIED, "delegated grant does not match this current step/track/version"
                )
            latest = self._latest(db, authority.scope, base, step_id)
            if (latest.sequence if latest else 0) != expected_step_cursor:
                raise DocumentError(
                    CONFLICT, "step progress changed; reread before asserting progress"
                )
            sequence = _increment(head.progress_cursor)
            entry = request.model_copy(update={"sequence": sequence, "step_sha256": fingerprint})
            self._append(db, authority.scope, "progress", base.plan_id, sequence, entry, step_id)
            head = head.model_copy(update={"progress_cursor": sequence})
            return self._finish(
                db, authority, operation, "progress", identifier, digest, head, sequence
            )

    def set_status(
        self,
        authority: PlanAuthority,
        operation: str,
        base: PlanRef,
        expected_status_revision: int,
        expected_progress_cursor: int,
        status: PlanStatus,
        reason: str,
        *,
        outcome: GoalOutcome | None = None,
    ) -> PlanReceipt:
        """Record workflow status; an active plan grants no execution."""
        authority = _checked(PlanAuthority, authority)
        self._main(authority)
        base = _checked(PlanRef, base)
        status_revision = _counter(expected_status_revision)
        cursor = _counter(expected_progress_cursor)
        change = PlanStatusChange(
            scope=authority.scope,
            ref=base,
            revision=1,
            progress_cursor=cursor,
            status=status,
            outcome=outcome,
            reason=_text(reason),
            author=authority,
            recorded_at=datetime.now(UTC),
        )
        change = _checked(PlanStatusChange, change)
        if change.outcome:
            self._evidence(authority.scope, change.outcome.evidence)
        payload = {
            "base": base.model_dump(mode="json"),
            "expected_status_revision": status_revision,
            "expected_progress_cursor": cursor,
            "status": status,
            "reason": reason,
            "outcome": change.outcome.model_dump(mode="json") if change.outcome else None,
        }
        with self.documents.control.write() as tx:
            db = tx.sql
            identifier, digest, replay = self._begin(db, authority, operation, "status", payload)
            if replay:
                return replay
            head = self._head(db, authority.scope, base)
            self._open(head)
            if (head.status_revision, head.progress_cursor) != (status_revision, cursor):
                raise DocumentError(
                    CONFLICT, "plan status/progress changed; reread before changing status"
                )
            if status == "completed":
                opened: list[str] = []
                body = self._version(db, authority.scope, base).body
                for step_id, (_, fingerprint) in _steps(body).items():
                    latest = self._latest(db, authority.scope, base, step_id)
                    if (
                        latest is None
                        or latest.step_sha256 != fingerprint
                        or latest.status not in {"completed", "blocked", "skipped"}
                    ) or (
                        change.outcome
                        and change.outcome.status == "achieved"
                        and latest.status != "completed"
                    ):
                        opened.append(step_id)
                if opened:
                    raise DocumentError(
                        CONFLICT,
                        "current steps are open, changed, or incompatible with achieved outcome",
                        details={"steps": opened},
                    )
            revision = _increment(head.status_revision)
            change = change.model_copy(update={"revision": revision})
            self._append(db, authority.scope, "status", base.plan_id, revision, change)
            head = PlanHead(
                scope=authority.scope,
                ref=base,
                status_revision=revision,
                progress_cursor=cursor,
                status=status,
                outcome=change.outcome,
            )
            return self._finish(
                db, authority, operation, "status", identifier, digest, head, revision
            )

    def current(self, authority: PlanAuthority) -> PlanSnapshot | None:
        """Read only current steps; removed-step assertions remain in explicit history."""
        authority = _checked(PlanAuthority, authority)
        if self.documents.control.missing:
            return None
        with self.documents.control.read() as tx:
            db = tx.sql
            pointer = self._current(db, authority.scope)
            if pointer is None:
                return None
            head = self._head(db, authority.scope, pointer.ref)
            version = self._version(db, authority.scope, head.ref)
            steps: list[StepProjection] = []
            used = len(
                _json(
                    {
                        "head": head.model_dump(mode="json"),
                        "version": version.model_dump(mode="json"),
                    },
                    2 * 1024 * 1024,
                )
            )
            for step_id, (_, fingerprint) in _steps(version.body).items():
                latest = self._latest(db, authority.scope, head.ref, step_id)
                if latest is not None and latest.sequence > head.progress_cursor:
                    raise DocumentError(INTERNAL, "retained progress exceeds its head")
                projection = StepProjection(
                    step_id=step_id,
                    step_sha256=fingerprint,
                    latest=latest,
                    matches_current_step=latest is not None and latest.step_sha256 == fingerprint,
                )
                used += len(_json(projection)) + 1
                if used > 2 * 1024 * 1024 - 1024:
                    raise DocumentError(
                        INVALID_REQUEST, "current plan read limit; use pinned history pages"
                    )
                steps.append(projection)
            result = PlanSnapshot(
                event_cursor=pointer.event_cursor, head=head, version=version, steps=tuple(steps)
            )
            _json(result, 2 * 1024 * 1024)
            return result

    def compact_current(self, authority: PlanAuthority) -> CompactPlan | None:
        """Read body plus every current step without returning note/evidence content."""
        authority = _checked(PlanAuthority, authority)
        if self.documents.control.missing:
            return None
        with self.documents.control.read() as tx:
            db = tx.sql
            pointer = self._current(db, authority.scope)
            if pointer is None:
                return None
            head = self._head(db, authority.scope, pointer.ref)
            version = self._version(db, authority.scope, pointer.ref)
            used = len(
                _json(
                    {
                        "head": head.model_dump(mode="json"),
                        "version": version.model_dump(mode="json"),
                    },
                    MAX_PAGE,
                )
            )
            steps: list[CompactStep] = []
            for step_id, (_, fingerprint) in _steps(version.body).items():
                latest = self._latest(db, authority.scope, pointer.ref, step_id)
                if latest is not None and latest.sequence > head.progress_cursor:
                    raise DocumentError(INTERNAL, "retained progress exceeds its head")
                projection = CompactStep(
                    step_id=step_id,
                    step_sha256=fingerprint,
                    matches_current_step=latest is not None and latest.step_sha256 == fingerprint,
                    latest=CompactProgress(
                        ref=latest.ref,
                        sequence=latest.sequence,
                        step_sha256=latest.step_sha256,
                        status=latest.status,
                        author=latest.author,
                        recorded_at=latest.recorded_at,
                        evidence_count=len(latest.evidence),
                    )
                    if latest is not None
                    else None,
                )
                used += len(_json(projection)) + 1
                if used > MAX_PAGE - 1024:
                    raise DocumentError(INVALID_REQUEST, "compact plan read limit")
                steps.append(projection)
            result = CompactPlan(
                event_cursor=pointer.event_cursor, head=head, version=version, steps=tuple(steps)
            )
            _json(result, MAX_PAGE)
            return result

    def version(self, authority: PlanAuthority, ref: PlanRef) -> PlanVersion:
        """Resolve one exact immutable version within the trusted project/session scope."""
        authority = _checked(PlanAuthority, authority)
        ref = _checked(PlanRef, ref)
        if self.documents.control.missing:
            raise DocumentError(NOT_FOUND, "plan not found")
        with self.documents.control.read() as tx:
            db = tx.sql
            return self._version(db, authority.scope, ref)

    def history(
        self,
        authority: PlanAuthority,
        plan_id: str,
        kind: Literal["version", "progress", "status"],
        *,
        after: int = 0,
        target: int | None = None,
        step_id: str | None = None,
        limit: int = 16,
    ) -> PlanPage[PlanVersion] | PlanPage[PlanProgress] | PlanPage[PlanStatusChange]:
        """Bounded pinned history; removed IDs remain queryable without a current projection."""
        authority = _checked(PlanAuthority, authority)
        plan_id = _checked_operation(plan_id)
        if kind not in {"version", "progress", "status"} or (
            step_id is not None and kind != "progress"
        ):
            raise DocumentError(INVALID_REQUEST, "invalid plan history kind/filter")
        if step_id is not None:
            step_id = _checked_operation(step_id)
        if self.documents.control.missing:
            raise DocumentError(NOT_FOUND, "plan not found")
        with self.documents.control.read() as tx:
            db = tx.sql
            head = self._projection(db, authority.scope, "head", plan_id, PlanHead)
            if head is None:
                raise DocumentError(NOT_FOUND, "plan not found in this session")
            self._head(db, authority.scope, head.ref, current=False)
            maximum = {
                "version": head.ref.version,
                "progress": head.progress_cursor,
                "status": head.status_revision,
            }[kind]
            if kind == "version":
                return self._page(
                    db,
                    authority.scope,
                    kind,
                    plan_id,
                    PlanVersion,
                    maximum,
                    after,
                    target,
                    step_id,
                    limit,
                )
            if kind == "progress":
                return self._page(
                    db,
                    authority.scope,
                    kind,
                    plan_id,
                    PlanProgress,
                    maximum,
                    after,
                    target,
                    step_id,
                    limit,
                )
            return self._page(
                db,
                authority.scope,
                kind,
                plan_id,
                PlanStatusChange,
                maximum,
                after,
                target,
                step_id,
                limit,
            )

    def events(
        self,
        authority: PlanAuthority,
        *,
        after: int = 0,
        target: int | None = None,
        limit: int = 16,
    ) -> PlanPage[PlanEvent]:
        """Local session sequence only; no other project's activity creates cursor holes."""
        authority = _checked(PlanAuthority, authority)
        if self.documents.control.missing:
            after = _counter(after)
            target = 0 if target is None else _counter(target)
            if after or target or type(limit) is not int or not 1 <= limit <= 16:
                raise DocumentError(INVALID_REQUEST, "invalid scoped plan page bounds")
            return PlanPage((), 0, 0, False)
        with self.documents.control.read() as tx:
            db = tx.sql
            pointer = self._current(db, authority.scope)
            maximum = pointer.event_cursor if pointer else 0
            return self._page(
                db, authority.scope, "event", "", PlanEvent, maximum, after, target, None, limit
            )

    def _page(
        self,
        db: sqlite3.Connection,
        scope: PlanScope,
        kind: str,
        identifier: str,
        model: type[H],
        maximum: int,
        after: int,
        target: int | None,
        key: str | None,
        limit: int,
    ) -> PlanPage[H]:
        after = _counter(after)
        target = maximum if target is None else _counter(target)
        if after > target or target > maximum or type(limit) is not int or not 1 <= limit <= 16:
            raise DocumentError(INVALID_REQUEST, "invalid scoped plan page bounds")
        rows = db.execute(
            "SELECT CASE WHEN typeof(number)='integer' THEN number END,substr(CAST(record"
            " AS BLOB),1,?) FROM session_plan_records WHERE project=? AND session=? AND "
            "kind=? AND id=? AND number>? AND number<=? AND (? IS NULL OR key=?) ORDER BY"
            " number LIMIT ?",
            (
                MAX_RECORD + 1,
                scope.project_id,
                scope.session_id,
                kind,
                identifier,
                after,
                target,
                key,
                key,
                limit + 1,
            ),
        )
        items: list[H] = []
        size = 0
        more = False
        for number, raw in rows:
            if not isinstance(raw, bytes) or len(raw) > MAX_RECORD or type(number) is not int:
                raise DocumentError(INTERNAL, "retained plan page metadata is invalid")
            if key is None and number != after + 1:
                raise DocumentError(INTERNAL, "retained scoped plan history has a gap")
            if len(items) == limit or size + len(raw) > MAX_PAGE:
                more = True
                break
            item = self._scope(_decode(model, raw), scope)
            if item.author.scope != scope:
                raise DocumentError(INTERNAL, "retained plan page attribution is invalid")
            if isinstance(item, (PlanVersion, PlanProgress, PlanStatusChange)) and (
                item.ref.plan_id != identifier or item.author.scope != scope
            ):
                raise DocumentError(INTERNAL, "retained plan page identity is invalid")
            actual = (
                item.ref.version
                if isinstance(item, PlanVersion)
                else item.revision
                if isinstance(item, PlanStatusChange)
                else item.sequence
            )
            if actual != number:
                raise DocumentError(INTERNAL, "retained plan page cursor is invalid")
            if isinstance(item, PlanEvent):
                self._event(db, scope, number)
            items.append(item)
            size += len(raw)
            after = number
        if key is None and not more and after != target:
            raise DocumentError(INTERNAL, "retained scoped plan history has a gap")
        return PlanPage(tuple(items), after, target, more)


def _counter(value: int) -> int:
    if type(value) is not int or not 0 <= value <= MAX_COUNTER:
        raise DocumentError(INVALID_REQUEST, "invalid plan counter")
    return value


def _increment(value: int) -> int:
    if type(value) is not int or not 0 <= value < MAX_COUNTER:
        raise DocumentError(INTERNAL, "retained plan counter is exhausted or invalid")
    return value + 1


def _text(value: str) -> str:
    if type(value) is not str or not 1 <= len(value) <= 4096 or "\0" in value or not value.strip():
        raise DocumentError(INVALID_REQUEST, "invalid plan reason/note")
    try:
        value.encode("utf-8", "strict")
    except UnicodeError as error:
        raise DocumentError(
            INVALID_REQUEST, "plan reason/note has invalid text encoding"
        ) from error
    return value


def _checked_operation(value: str) -> str:
    from pydantic import TypeAdapter

    try:
        return TypeAdapter[str](Identifier).validate_python(value, strict=True)
    except ValidationError as error:
        raise DocumentError(INVALID_REQUEST, "invalid plan identifier") from error
