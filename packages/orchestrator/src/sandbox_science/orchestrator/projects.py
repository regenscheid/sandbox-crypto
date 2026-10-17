# SPDX-License-Identifier: MIT
"""Projects and their sessions, as the orchestrator records them (W3).

A project names a base profile (D46) and a policy revision, and owns a default
environment. Each session (workbook) runs in the default environment, with its own
kernels and workspace, unless it was opened with a dedicated environment of its own,
which starts from the same base profile ([projects and sessions](../../../../docs/
PROJECTS-AND-SESSIONS.md)). The worker creates the environments (D73); the
orchestrator asks for them by ID and keeps this record of which session uses which.

The records are in the control store, ``<state-dir>/control.sqlite3`` (control/), in
the ``projects``, ``sessions`` and ``runs`` tables. Each change is one transaction.
A change elsewhere that needs its session open, such as a plan change, checks the
session in its own transaction (session_plans.py), so a close cannot land in between.

Environment IDs end with a random tag, kept in the record. A worker shared with other
projects' orchestrators keeps each ID for the project that first creates it (D78), so
an ID another orchestrator could predict, such as ``p1-default``, could be taken first.

Each run of a session is kept too, as the record ``run --transcript`` writes (W4). A
run's record is named by when the run started and its run ID, so a run ID used again
adds a record rather than replacing one, and it is written once.

Project records were JSON files, ``projects/<project>.json``, before they moved into
the control store; such a file is not read, and a lookup of its project says so.
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import sqlite3
from collections import deque
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Final, Literal, cast

from pydantic import (
    Field,
    JsonValue,
    StringConstraints,
    TypeAdapter,
    ValidationError,
    computed_field,
    model_validator,
)

from sandbox_science.contracts import (
    EnvironmentInfo,
    EnvironmentRequest,
    EnvironmentState,
    ErrorKind,
    ExecutionContext,
    Identifier,
    PolicyRevision,
    new_id,
)
from sandbox_science.contracts.model import Contract

from .control import ControlStore, store_path
from .prompts import PromptPins
from .worker_client import WorkerAPIError, WorkerClient, WorkerProtocolError

RUN_READ_BYTES: Final = 8 * 1024 * 1024
"""Default limit for materializing a legacy run; larger evidence uses streamed export."""

_MAX_ID: Final = 128
_IDENTIFIER: Final = TypeAdapter(Identifier)
_TAG_BYTES: Final = 6
_STARTED: Final = "%Y%m%dT%H%M%S%fZ"
_RUN_NAME: Final = re.compile(r"([0-9]{8}T[0-9]{12}Z)-([A-Za-z0-9][A-Za-z0-9._-]{0,127})")
_TIME: Final = TypeAdapter(datetime)
_PROJECT_COLUMNS: Final = (
    "project_id",
    "profile",
    "policy",
    "default_environment_id",
    "created_at",
    "updated_at",
)
_SESSION_COLUMNS: Final = (
    "session_id",
    "environment_id",
    "dedicated",
    "opened_at",
    "closed_at",
    "kernel_notice_lifetime",
)


def _time(value: datetime) -> str:
    """A time as the records keep it: the same JSON text pydantic writes."""
    return cast(str, _TIME.dump_python(value, mode="json"))


class ProjectError(Exception):
    """A project or session that does not exist, or a request that conflicts with one."""

    def __init__(self, kind: ErrorKind, message: str) -> None:
        super().__init__(message)
        self.kind = kind
        self.message = message


class SessionRecord(Contract):
    session_id: Identifier
    environment_id: Identifier
    dedicated: bool = False
    """The session has an environment of its own instead of the project's default."""
    opened_at: datetime
    closed_at: datetime | None = None
    """When the session was last closed: its kernels were stopped. Running it reopens it."""
    kernel_notice_lifetime: Identifier | None = None
    """Opaque control-owned open lifetime; missing legacy state proves no past lifetime."""


class ProjectRecord(Contract):
    project_id: Identifier
    profile: Identifier
    policy: PolicyRevision
    default_environment_id: Identifier
    sessions: dict[str, SessionRecord] = Field(default_factory=dict[str, SessionRecord])
    created_at: datetime
    updated_at: datetime

    def environments(self) -> list[str]:
        """Every environment the project uses: the default one and the sessions' own."""
        dedicated = {s.environment_id for s in self.sessions.values() if s.dedicated}
        return [self.default_environment_id, *sorted(dedicated - {self.default_environment_id})]


@dataclass(frozen=True)
class SessionSnapshot:
    """An open session as the registry recorded it when it was read. Two snapshots are
    equal when they name the same open session: a close and reopen gives the session a
    new record (``opened_at`` and ``kernel_notice_lifetime``)."""

    context: ExecutionContext
    session: SessionRecord


FailurePhase = Literal["model", "tool", "harness", "cleanup"]
"""A fixed local classification, never exception text or a provider response."""

_LEGACY_DELIVERED: Final = {
    "work_request_ids": "requests",
    "job_notice_ids": "jobs",
    "review_notice_ids": "reviews",
    "math_notice_ids": "math",
}
_LEGACY_SUMMARIES: Final = {"review_summary": "reviews", "math_summary": "math"}


class ModelUsage(Contract):
    """One model call's usage, as the gateway reported it; a count it left out is null."""

    model: str | None = None
    """The model that answered, as the gateway names it."""
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    characters: int | None = Field(default=None, ge=0)
    """What the call sent and received, in characters: the spend of a call without
    token counts is a quarter of it."""
    cost: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    """What the call cost, in the gateway's currency (LiteLLM's is USD): as the gateway
    reported it, or priced from the gateway's model information (``cost_source``); null
    when neither gives it. It is never estimated."""
    cost_source: Literal["gateway", "model_info"] | None = None
    """Where ``cost`` came from: ``gateway``, reported by it, or ``model_info``, priced
    from the gateway's model information, its own price list, at the call's tokens (lab
    43, B2); null with no cost, and in older records, whose costs the gateway reported."""
    gateway_version: str | None = Field(default=None, max_length=128)
    """The gateway's version, as its ``x-litellm-version`` header named it
    (docs/specs/context-management.md, CM-39); null when it sent none."""
    thinking_replay: str | None = Field(default=None, max_length=300)
    """Whether the call sent Claude's thinking back: ``on``, or ``off:`` and why (CM-39);
    null for a client without thinking replay."""
    retried: list[str] = Field(default_factory=list, max_length=16)
    """What the attempts before this answer's came to, in order (``HTTP 524``, or ``failed
    response``): the gateway's timeouts and rate limits the call got past (lab 44, B3).
    Only the answered attempt's tokens and cost are this entry's."""


def _summary(value: JsonValue | None) -> dict[str, JsonValue]:
    return cast(dict[str, JsonValue], value) if isinstance(value, dict) else {}


RunRole = Literal["main", "delegated"]
RunName = Annotated[str, StringConstraints(pattern=f"^{_RUN_NAME.pattern}$")]
"""The name a session keeps a run's record under (``run_name``)."""
"""A run's role in its session's tree of runs (PD-32): the root answers the user, and a
delegated run works for its parent."""


class RunRecord(Contract):
    """One run of the harness: what ``run --transcript`` writes, and what a session keeps."""

    run_id: str
    """The run's ID, which starts every operation ID of its tool calls."""
    role: RunRole = "main"
    parent: RunName | None = None
    """For a delegated run, the name its parent's record is kept under: a run of the
    same session that started before it."""
    delegation: Identifier | None = None
    """For a delegated run, the delegation it carries out."""
    alias: str | None = None
    """The model alias its calls named (PD-29); its ``usage`` names the models the gateway
    resolved it to. Older records have none."""
    limits: dict[str, JsonValue] | None = None
    """The limits it ran under, as the harness applied them (PD-29): its turns, the
    longest conversation it sent, and its token limit or its share of the budget."""
    prompt_provenance: Annotated[
        PromptPins | dict[str, JsonValue] | None, Field(union_mode="left_to_right")
    ] = None
    """The pins of the system message's sections and the tools the run was given. An
    older run's prompt bundle is kept as read; a run without either stays unknown."""
    kernel_notice_delivery: dict[str, JsonValue] | None = None
    """Older runs' receipt from the kernel-notice inbox, kept as read; new runs have
    none, and their kernel notices are in ``delivered["kernels"]``."""
    context: ExecutionContext
    task: str
    started_at: datetime
    ended_at: datetime
    stopped: str
    """``answered``, ``turn_limit``, ``context_limit``, ``user_message`` (a message came
    while the run's review held its answer), ``plan_unread`` (the session plan held the
    answer back: it has open steps the run did not read after they changed), ``cancelled``
    (by the user), ``budget`` (the project spent its model budget during the run),
    ``run_budget`` (the run used its share of it, or a delegated run its tokens),
    ``gateway_budget`` (the model gateway refused a call because its spending budget is
    exhausted), ``runaway`` (the runaway guard saw the run repeat itself without progress),
    or ``error``. A run stopped by its turn limit, a delegated run's tokens, its
    conversation's length or the runaway guard first took a report round, whose answer
    is its ``final_text`` (run-service spec, RS-6f)."""
    failure: FailurePhase | None = None
    """Failed phase; old records omit it. Partial messages are observations, not an answer."""
    delivered: dict[Identifier, list[Identifier]] = Field(
        default_factory=dict[Identifier, list[Identifier]]
    )
    """Each notice source's IDs that a successful model call saw, such as ``jobs`` or
    ``requests``. The source's hook acknowledges them only after this record is saved."""
    usage: list[ModelUsage] = Field(default_factory=list[ModelUsage])
    """Each answered model call's usage, in order; older records have none."""
    summaries: dict[Identifier, JsonValue] = Field(default_factory=dict[Identifier, JsonValue])
    """What a source reported at the run's end, such as the consistency review's outcome."""
    final_text: str | None = None
    """The run's last answer: the text its last closing accepted."""
    answer: str | None = None
    """The run's answer to the user: the text of the turn that answered, before any
    closing rounds that followed, such as its responses to review findings, whose texts
    the messages keep (lab 42, B4). Later runs' earlier conversation shows it (RS-10).
    Records from before it have none: their ``final_text`` stands in."""
    stored_history: dict[str, JsonValue] | None = None
    """Explicit scoped paged history reference when historical collections are not inline."""
    messages: list[dict[str, JsonValue]]
    """The conversation in the OpenAI chat format the harness uses, system prompt first."""
    tools: list[dict[str, JsonValue]]
    """One record per tool call, in order. Oversized archived records retain a bounded
    preview with ``_record_storage`` naming the full immutable journal record."""

    @model_validator(mode="before")
    @classmethod
    def _legacy_sources(cls, data: object) -> object:
        """Older records, and the summaries this one emits, name each source's field."""
        if not isinstance(data, dict):
            return data
        fields = dict(cast(dict[str, object], data))
        delivered, summaries = fields.get("delivered", {}), fields.get("summaries", {})
        if not isinstance(delivered, dict) or not isinstance(summaries, dict):
            return fields
        delivered = dict(cast(dict[str, object], delivered))
        summaries = dict(cast(dict[str, object], summaries))
        for name, source in _LEGACY_DELIVERED.items():
            ids = fields.pop(name, None)
            if ids:
                delivered.setdefault(source, ids)
        for name, source in _LEGACY_SUMMARIES.items():
            summary = fields.pop(name, None)
            if summary:
                summaries.setdefault(source, summary)
        return {**fields, "delivered": delivered, "summaries": summaries}

    @computed_field
    @property
    def review_summary(self) -> dict[str, JsonValue]:
        """The consistency review's summary, which the workspace shows."""
        return _summary(self.summaries.get("reviews"))

    @computed_field
    @property
    def math_summary(self) -> dict[str, JsonValue]:
        """The document math checks' summary, which the workspace shows."""
        return _summary(self.summaries.get("math"))

    @model_validator(mode="after")
    def _place_in_the_tree(self) -> RunRecord:
        delegated = self.parent is not None and self.delegation is not None
        if delegated != (self.role == "delegated") or (self.parent is None) != (
            self.delegation is None
        ):
            raise ValueError(
                "a delegated run names its parent and its delegation; a main run neither"
            )
        # Names begin with the start time, so a parent named before its child cannot be
        # the child itself or one of its descendants.
        if self.parent is not None and not self.parent < run_name(self.started_at, self.run_id):
            raise ValueError("a delegated run's parent started before it")
        return self

    @model_validator(mode="after")
    def _failure_is_not_an_answer(self) -> RunRecord:
        if self.failure is not None and (
            self.stopped != "error" or self.final_text is not None or self.answer is not None
        ):
            raise ValueError("a failed run must have error status and no final answer")
        return self

    def to_json(self) -> str:
        """The record as JSON text.

        The standard library's encoder escapes what it cannot write as UTF-8, such as a
        lone surrogate in a model's reply, where pydantic's would fail at the end of a run.
        """
        return json.dumps(self.model_dump(mode="json"), indent=2)


class RunEntry(Contract):
    """A run a session keeps, named by when it started and its run ID, with its place in
    the session's tree of runs."""

    name: str
    run_id: Identifier
    started_at: datetime
    role: RunRole = "main"
    parent: RunName | None = None
    """The name of its parent's record, for a delegated run."""


def environment_id(*parts: str) -> str:
    """An environment ID from its parts, such as a project's and a session's IDs, short
    enough to be one."""
    joined = "-".join(parts)
    if len(joined) <= _MAX_ID:
        return joined
    digest = hashlib.sha256("\0".join(parts).encode()).hexdigest()[:16]
    return f"{joined[: _MAX_ID - len(digest) - 1]}-{digest}"


def _new_environment_id(project_id: str, name: str) -> str:
    """A new environment's ID: readable, and unpredictable to other projects' clients."""
    return environment_id(project_id, name, secrets.token_hex(_TAG_BYTES))


def _is_identifier(value: str) -> bool:
    try:
        _IDENTIFIER.validate_python(value)
    except ValidationError:
        return False
    return True


def run_name(started_at: datetime, run_id: str) -> str:
    """The name a session keeps a run's record under: its start time in UTC and its ID."""
    return f"{started_at.astimezone(UTC).strftime(_STARTED)}-{run_id}"


def operator_ref(run: str) -> str:
    """What a run's archived task names when it is the operator's own words, typed with
    ``run --task``: the run, by its record's name (CM-9). Only the platform archives."""
    return f"cli:{run}"


def run_id_of(name: str) -> str | None:
    """The ID of the run a record's name stands for; None for any other name."""
    found = _run_entry(name)
    return None if found is None else found.run_id


def run_started(name: str) -> datetime | None:
    """When the run a record's name stands for started; None for any other name."""
    found = _run_entry(name)
    return None if found is None else found.started_at


def _run_entry(name: str) -> RunEntry | None:
    """The run a record's name stands for; None for any other name, which is never a path."""
    match = _RUN_NAME.fullmatch(name)
    if match is None:
        return None
    try:
        started = datetime.strptime(match[1], _STARTED).replace(tzinfo=UTC)
    except ValueError:
        return None
    return RunEntry(name=name, run_id=match[2], started_at=started)


class ProjectStore:
    """The orchestrator's projects, their sessions and the sessions' runs, in the control
    store of ``state_dir``.

    A ``readonly`` store only reads, and never waits for a writer, so a repository can
    look a session up while its own connection holds a write transaction.
    """

    def __init__(self, state_dir: Path, *, readonly: bool = False) -> None:
        self.root = state_dir / "projects"
        """Where projects were JSON files before they moved into the control store."""
        self.readonly = readonly
        self.control = ControlStore(store_path(state_dir), readonly=readonly)

    def close(self) -> None:
        self.control.close()

    def get(self, project_id: str) -> ProjectRecord:
        self._checked(project_id)
        if self._unmade():
            raise self._absent(project_id)
        with self.control.read() as tx:
            return self._project(tx.sql, project_id)

    def session_snapshot(self, project_id: str, session_id: str) -> SessionSnapshot:
        """An open session's record and context, for a caller that checks later that the
        session is still the same open one."""
        if not _is_identifier(project_id):
            raise ProjectError(ErrorKind.INVALID_REQUEST, "invalid project ID")
        if not _is_identifier(session_id):
            raise ProjectError(ErrorKind.INVALID_REQUEST, "invalid session ID")
        record = self.get(project_id)
        found = record.sessions.get(session_id)
        if found is None or found.closed_at is not None:
            raise ProjectError(ErrorKind.NOT_FOUND, "no open session in this project")
        return SessionSnapshot(
            ExecutionContext(
                project_id=project_id, session_id=session_id, environment_id=found.environment_id
            ),
            found,
        )

    def create(self, project_id: str, profile: str, policy: str) -> tuple[ProjectRecord, bool]:
        """Record a new project; return the existing one when it has the same settings."""
        self._checked(project_id)
        with self.control.write() as tx:
            row = tx.sql.execute(
                "SELECT 1 FROM projects WHERE project_id = ?", (project_id,)
            ).fetchone()
            if row is not None:
                existing = self._project(tx.sql, project_id)
                if (existing.profile, existing.policy) == (profile, policy):
                    return existing, False
                raise ProjectError(
                    ErrorKind.CONFLICT,
                    f"project {project_id!r} exists with profile {existing.profile} and policy "
                    f"{existing.policy}; revise its policy instead",
                )
            now = datetime.now(UTC)
            record = self._validated(
                project_id=project_id,
                profile=profile,
                policy=policy,
                default_environment_id=_new_environment_id(project_id, "default"),
                created_at=now,
                updated_at=now,
            )
            saved = record.model_dump(mode="json")
            tx.sql.execute(
                "INSERT INTO projects (project_id, profile, policy, default_environment_id, "
                "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                tuple(saved[name] for name in _PROJECT_COLUMNS),
            )
            return record, True

    def set_policy(self, project_id: str, policy: str) -> ProjectRecord:
        """Move the project to another policy revision; its environments follow on next use."""
        self._checked(project_id)
        with self.control.write() as tx:
            record = self._project(tx.sql, project_id)
            if record.policy == policy:
                return record
            updated = self._validated(
                **{**record.model_dump(), "policy": policy, "updated_at": datetime.now(UTC)}
            )
            tx.sql.execute(
                "UPDATE projects SET policy = ?, updated_at = ? WHERE project_id = ?",
                (updated.policy, _time(updated.updated_at), project_id),
            )
            return updated

    def open_session(self, project_id: str, session_id: str, *, dedicated: bool) -> SessionRecord:
        """The session's record, created on first use; a closed session opens again."""
        self._checked(project_id)
        with self.control.write() as tx:
            record = self._project(tx.sql, project_id)
            session = record.sessions.get(session_id)
            now = datetime.now(UTC)
            if session is None:
                session = self._session(
                    session_id=session_id,
                    environment_id=_new_environment_id(project_id, session_id)
                    if dedicated
                    else record.default_environment_id,
                    dedicated=dedicated,
                    opened_at=now,
                    kernel_notice_lifetime=new_id("kns"),
                )
            elif session.dedicated != dedicated:
                where = "its own environment" if session.dedicated else "the project's"
                raise ProjectError(
                    ErrorKind.CONFLICT,
                    f"session {session_id!r} runs in {where}; a session keeps its environment, "
                    "since its kernels and installed tools live there",
                )
            elif session.closed_at is not None:
                session = session.model_copy(
                    update={
                        "closed_at": None,
                        "opened_at": now,
                        "kernel_notice_lifetime": new_id("kns"),
                    }
                )
            else:
                return session
            self._put_session(tx.sql, project_id, session, now)
            return session

    def close_session(self, project_id: str, session_id: str) -> SessionRecord:
        self._checked(project_id)
        with self.control.write() as tx:
            session = self._project(tx.sql, project_id).sessions.get(session_id)
            if session is None:
                raise ProjectError(
                    ErrorKind.NOT_FOUND, f"project {project_id!r} has no session {session_id!r}"
                )
            now = datetime.now(UTC)
            closed = session.model_copy(update={"closed_at": now})
            self._put_session(tx.sql, project_id, closed, now)
            # Its questions to the user can no longer be answered (PD-23). Imported here:
            # the questions import the documents, which import this module.
            from .questions import SESSION_CLOSED, close_open

            close_open(
                tx.sql,
                project_id,
                session_id,
                now.isoformat(),
                state="cancelled",
                reason=SESSION_CLOSED,
            )
            return closed

    def project_ids(self) -> list[str]:
        """The IDs of the projects recorded here, sorted, with those recorded only in an
        earlier JSON file, whose lookup says so."""
        found: set[str] = set()
        if not self._unmade():
            with self.control.read() as tx:
                found.update(row[0] for row in tx.sql.execute("SELECT project_id FROM projects"))
        earlier = (path for path in self.root.glob("*.json") if path.is_file())
        found.update(name for path in earlier if _is_identifier(name := path.stem))
        return sorted(found)

    def save_run(self, record: RunRecord) -> RunEntry:
        """Keep a run of one of the project's sessions; the record is written once."""
        project_id, session_id = record.context.project_id, record.context.session_id
        entry = _run_entry(run_name(record.started_at, record.run_id))
        if entry is None:
            raise ProjectError(
                ErrorKind.INVALID_REQUEST,
                f"run ID {record.run_id!r} cannot name a record: use letters, digits, '.', "
                "'_' and '-', starting with a letter or digit",
            )
        text = record.to_json()
        with self.control.write() as tx:
            if session_id not in self._project(tx.sql, project_id).sessions:
                raise ProjectError(
                    ErrorKind.NOT_FOUND, f"project {project_id!r} has no session {session_id!r}"
                )
            known = tx.sql.execute(
                "SELECT 1 FROM runs WHERE project_id = ? AND session_id = ? AND name = ?",
                (project_id, session_id, entry.name),
            ).fetchone()
            if known is not None:
                raise ProjectError(
                    ErrorKind.CONFLICT, f"session {session_id!r} has a run {entry.name!r} already"
                )
            if (
                record.parent is not None
                and not self._knows_run(tx.sql, project_id, session_id, record.parent)
                and not self._planned_parent(
                    tx.sql, project_id, session_id, entry.name, record.parent
                )
            ):
                raise ProjectError(
                    ErrorKind.INVALID_REQUEST,
                    f"the parent {record.parent!r} is no run of session {session_id!r}",
                )
            try:
                tx.sql.execute(
                    "INSERT INTO runs (project_id, session_id, name, run_id, started_at, record, "
                    "role, parent, delegation) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        project_id,
                        session_id,
                        entry.name,
                        entry.run_id,
                        _time(entry.started_at),
                        text,
                        record.role,
                        record.parent,
                        record.delegation,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise ProjectError(
                    ErrorKind.INVALID_REQUEST, f"the store refuses run {entry.name!r}: {exc}"
                ) from None
        return entry

    @staticmethod
    def _planned_parent(
        db: sqlite3.Connection, project: str, session: str, child: str, parent: str
    ) -> bool:
        """Leaf-first Resume reserves the exact parent before its loop starts."""
        return (
            db.execute(
                "SELECT 1 FROM research_continuations c JOIN research_continuations p "
                "USING(project,session,request) JOIN research_work w ON w.project=c.project "
                "AND w.session=c.session AND w.work=c.work AND w.parent=p.work "
                "JOIN research_attempts a ON a.project=w.project AND a.session=w.session "
                "AND a.work=w.work AND a.run=w.current JOIN research_resumes r "
                "ON r.project=c.project AND r.session=c.session AND r.request=c.request "
                "JOIN research_sessions s ON s.project=r.project AND s.session=r.session "
                "AND s.root=r.root AND s.epoch=a.epoch JOIN research_continuations root "
                "ON root.project=r.project AND root.session=r.session "
                "AND root.request=r.request AND root.work=r.root JOIN run_leases l "
                "ON l.project_id=r.project AND l.session_id=r.session AND l.run=root.run "
                "WHERE c.project=? AND c.session=? AND c.run=? AND p.run=? "
                "AND a.run=c.run AND r.status='running' LIMIT 1",
                (project, session, child, parent),
            ).fetchone()
            is not None
        )

    @staticmethod
    def _knows_run(db: sqlite3.Connection, project_id: str, session_id: str, name: str) -> bool:
        """Whether the session has a run of that name: kept, or still running (its
        events or mandatory starting inputs). A parent's record is kept after its
        children's, so this is no foreign key."""
        return (
            db.execute(
                "SELECT 1 FROM runs WHERE project_id = ? AND session_id = ? AND name = ? "
                "UNION ALL SELECT 1 FROM run_events WHERE project_id = ? AND session_id = ? "
                "AND run = ? UNION ALL SELECT 1 FROM run_inputs "
                "WHERE project = ? AND session = ? AND run = ? LIMIT 1",
                (project_id, session_id, name) * 3,
            ).fetchone()
            is not None
        )

    def runs(self, project_id: str, session_id: str) -> list[RunEntry]:
        """The runs a session keeps, oldest first."""
        self._checked(project_id, session_id)
        if self._unmade():
            return []
        with self.control.read() as tx:
            rows = tx.sql.execute(
                "SELECT name, run_id, started_at, role, parent FROM runs WHERE project_id = ? "
                "AND session_id = ? ORDER BY name",
                (project_id, session_id),
            ).fetchall()
        try:
            return [
                RunEntry(name=name, run_id=run, started_at=started, role=role, parent=parent)
                for name, run, started, role, parent in rows
            ]
        except ValidationError as exc:
            raise ProjectError(
                ErrorKind.INTERNAL,
                f"the run list of {project_id}/{session_id} is damaged: {str(exc)[:300]}",
            ) from exc

    def iter_runs(
        self, project_id: str, session_id: str, *, newest_first: bool = False
    ) -> Iterator[RunEntry]:
        """Stable keyset pages, never a list proportional to session history."""
        self._checked(project_id, session_id)
        after = "" if not newest_first else "\uffff"
        query = (
            "SELECT name,run_id,started_at,role,parent FROM runs WHERE project_id=? "
            "AND session_id=? AND name<? ORDER BY name DESC LIMIT 100"
            if newest_first
            else "SELECT name,run_id,started_at,role,parent FROM runs WHERE project_id=? "
            "AND session_id=? AND name>? ORDER BY name ASC LIMIT 100"
        )
        while not self._unmade():
            with self.control.read() as tx:
                rows = tx.sql.execute(query, (project_id, session_id, after)).fetchall()
            if not rows:
                return
            for name, run, started, role, parent in rows:
                yield RunEntry(name=name, run_id=run, started_at=started, role=role, parent=parent)
            after = rows[-1][0]

    def unarchived_runs(self, project_id: str, session_id: str) -> list[str]:
        """Only legacy main attempts need fallback words; current history stays on disk."""
        with self.control.read() as tx:
            rows = tx.sql.execute(
                "SELECT name FROM runs r WHERE r.project_id=? AND r.session_id=? AND r.role='main' "
                "AND NOT EXISTS (SELECT 1 FROM archive_messages m WHERE m.project=r.project_id "
                "AND m.session=r.session_id AND m.run=r.name AND m.parent IS NULL "
                "AND m.kind NOT IN ('state_block','spill')) ORDER BY name LIMIT 8193",
                (project_id, session_id),
            ).fetchall()
        if len(rows) > 8192:
            raise ProjectError(
                ErrorKind.UNSUPPORTED,
                "legacy history exceeds the bounded fallback; use stored evidence",
            )
        return [name for (name,) in rows]

    def usage(
        self, project_id: str, session_id: str, alias: str | None, *, runs: int = 10
    ) -> list[ModelUsage]:
        """The calls of the session's last ``runs`` saved runs on ``alias``, oldest first:
        what calibrates a view's characters per token (docs/specs/context-management.md,
        CM-28). A damaged entry is left out."""
        self._checked(project_id, session_id)
        if self._unmade():
            return []
        from .tokens import CALIBRATED  # bounded calibration, not full usage accounting

        with self.control.read() as tx:
            rows = tx.sql.execute(
                "SELECT name FROM runs WHERE project_id=? AND session_id=? "
                "AND json_extract(record,'$.alias') IS ? "
                "ORDER BY name DESC LIMIT ?",
                (project_id, session_id, alias, max(0, min(runs, 100))),
            ).fetchall()
        found: deque[ModelUsage] = deque(maxlen=CALIBRATED)
        for (name,) in reversed(rows):
            record = self.run(project_id, session_id, name)
            if record.alias != alias:
                continue
            if record.stored_history is not None:
                with self.control.read() as tx:
                    calls = tx.sql.execute(
                        "SELECT item FROM run_collections WHERE project=? AND session=? AND run=? "
                        "AND collection='usage' ORDER BY position DESC LIMIT ?",
                        (project_id, session_id, name, CALIBRATED),
                    ).fetchall()
                values = (json.loads(text) for (text,) in reversed(calls))
            else:
                values = (call.model_dump(mode="json") for call in record.usage)
            for value in values:
                try:
                    call = ModelUsage.model_validate(value)
                    if (
                        call.characters
                        and call.input_tokens is not None
                        and call.output_tokens is not None
                    ):
                        found.append(call)
                except ValidationError:
                    continue
        return list(found)

    def run(
        self, project_id: str, session_id: str, name: str, *, max_bytes: int | None = RUN_READ_BYTES
    ) -> RunRecord:
        """A run the session keeps, by its name; larger than ``max_bytes`` is refused."""
        self._checked(project_id, session_id)
        if _run_entry(name) is None:
            raise ProjectError(ErrorKind.INVALID_REQUEST, f"invalid run name {name!r}")
        missing = ProjectError(
            ErrorKind.NOT_FOUND, f"session {session_id!r} of {project_id!r} has no run {name!r}"
        )
        if self._unmade():
            raise missing
        with self.control.read() as tx:
            key = (project_id, session_id, name)
            row = tx.sql.execute(
                "SELECT rowid FROM runs WHERE project_id = ? AND session_id = ? AND name = ?",
                key,
            ).fetchone()
            if row is None:
                raise missing
            # SQLite's incremental handle knows the size without materializing the
            # legacy JSON value, including in SQLite's own allocation heap.
            with tx.sql.blobopen("runs", "record", row[0], readonly=True) as blob:
                size = len(blob)
                if max_bytes is not None and size > max_bytes:
                    raise ProjectError(
                        ErrorKind.UNSUPPORTED,
                        f"run {name!r} has a {size}-byte record, larger than {max_bytes} bytes; "
                        "use the streamed stored-evidence download",
                    )
                text = blob.read()

        try:
            return RunRecord.model_validate(json.loads(text))
        except (ValueError, RecursionError) as exc:  # ValidationError is a ValueError
            raise ProjectError(
                ErrorKind.INTERNAL, f"run record {name} is damaged: {str(exc)[:300]}"
            ) from exc

    def _unmade(self) -> bool:
        """Whether there is no control store to read yet: a read never makes one."""
        return self.control.missing or not self.control.path.exists()

    def _project(self, db: sqlite3.Connection, project_id: str) -> ProjectRecord:
        row = db.execute(
            "SELECT project_id, profile, policy, default_environment_id, created_at, updated_at "
            "FROM projects WHERE project_id = ?",
            (project_id,),
        ).fetchone()
        if row is None:
            raise self._absent(project_id)
        sessions = {
            found[0]: dict(zip(_SESSION_COLUMNS, found, strict=True))
            for found in db.execute(
                "SELECT session_id, environment_id, dedicated, opened_at, closed_at, "
                "kernel_notice_lifetime FROM sessions WHERE project_id = ? ORDER BY rowid",
                (project_id,),
            )
        }
        try:
            return ProjectRecord.model_validate(
                {**dict(zip(_PROJECT_COLUMNS, row, strict=True)), "sessions": sessions}
            )
        except ValidationError as exc:
            raise ProjectError(
                ErrorKind.INTERNAL, f"project record {project_id} is damaged: {str(exc)[:300]}"
            ) from exc

    @staticmethod
    def _put_session(
        db: sqlite3.Connection, project_id: str, session: SessionRecord, now: datetime
    ) -> None:
        saved = session.model_dump(mode="json")
        db.execute(
            "INSERT INTO sessions (project_id, session_id, environment_id, dedicated, opened_at, "
            "closed_at, kernel_notice_lifetime) VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (project_id, session_id) DO UPDATE SET "
            "environment_id = excluded.environment_id, dedicated = excluded.dedicated, "
            "opened_at = excluded.opened_at, closed_at = excluded.closed_at, "
            "kernel_notice_lifetime = excluded.kernel_notice_lifetime",
            (project_id, *(saved[name] for name in _SESSION_COLUMNS)),
        )
        db.execute(
            "UPDATE projects SET updated_at = ? WHERE project_id = ?",
            (_time(now), project_id),
        )

    def _absent(self, project_id: str) -> ProjectError:
        if (self.root / f"{project_id}.json").is_file():
            return ProjectError(
                ErrorKind.UNAVAILABLE,
                f"project {project_id!r} is recorded only in projects/{project_id}.json, from "
                "before projects moved into the control store, and is not read: create the "
                "project again (project create), with its profile and policy",
            )
        return ProjectError(ErrorKind.NOT_FOUND, f"no project {project_id!r}")

    @staticmethod
    def _checked(project_id: str, session_id: str | None = None) -> None:
        if not _is_identifier(project_id):
            raise ProjectError(ErrorKind.INVALID_REQUEST, f"invalid project ID {project_id!r}")
        if session_id is not None and not _is_identifier(session_id):
            raise ProjectError(ErrorKind.INVALID_REQUEST, f"invalid session ID {session_id!r}")

    @staticmethod
    def _validated(**fields: object) -> ProjectRecord:
        try:
            return ProjectRecord.model_validate(fields)
        except ValidationError as exc:
            raise ProjectError(ErrorKind.INVALID_REQUEST, str(exc)[:500]) from exc

    @staticmethod
    def _session(**fields: object) -> SessionRecord:
        try:
            return SessionRecord.model_validate(fields)
        except ValidationError as exc:
            raise ProjectError(ErrorKind.INVALID_REQUEST, str(exc)[:500]) from exc


def recorded(state_dir: Path, project_id: str) -> ProjectRecord:
    """A project's record, read through a read-only connection of its own, which a
    repository can use while its own connection holds a write transaction."""
    store = ProjectStore(state_dir, readonly=True)
    try:
        return store.get(project_id)
    finally:
        store.close()


async def ensure_environment(
    client: WorkerClient, project: ProjectRecord, environment_id: str
) -> EnvironmentInfo | None:
    """Have the worker run the environment under the project's profile and policy.

    Creates it when it does not exist, and revises it when the project's policy has
    changed. Returns None when the worker does not manage environments (a local
    worker, or sandboxes made by hand): the ID is then used as it is.
    """
    try:
        info: EnvironmentInfo | None = await client.environment(environment_id)
    except WorkerAPIError as error:
        # A worker of another version manages environments as well; never skip the policy.
        if error.kind is ErrorKind.UNSUPPORTED and not isinstance(error, WorkerProtocolError):
            return None
        if error.kind is not ErrorKind.NOT_FOUND:
            raise
        info = None
    if info is None or info.state is EnvironmentState.DELETED:
        return await client.create_environment(
            EnvironmentRequest(
                environment_id=environment_id,
                project_id=project.project_id,
                profile=project.profile,
                policy=project.policy,
            )
        )
    if (info.project_id, info.profile) != (project.project_id, project.profile):
        raise WorkerAPIError(
            ErrorKind.CONFLICT,
            f"environment {environment_id} belongs to project {info.project_id} with profile "
            f"{info.profile}",
        )
    if info.policy != project.policy:
        return await client.revise_environment(environment_id, project.policy)
    return info
