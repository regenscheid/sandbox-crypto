# SPDX-License-Identifier: MIT
"""Delegations and each project's delegation setting, in the control store.

A delegation is written when its parent starts it, with its brief, and once more when
its child ends, with its outcome: how the child ended, as the harness observed it; the
child's final answer as it returned it, its report; the result that answer gives, if it
validated, and the validation errors; the model alias and the models the child ran on;
and why the plan fence refused its calls, if it did (PD-17, PD-20, PD-29, PD-34).
Neither changes afterwards. The run that received the outcome is written once that run's
record is saved (PD-35); until then, the outcome waits for the session's next main run.
A delegation still running when its session's next main run starts has lost its
process: it ends ``interrupted`` (PD-40).

A task that returns stays open for its parent's review (PD-46): its ``review`` is
``awaiting`` until its parent accepts it or cancels it, or the platform accepts it for
the parent, which closes it for good. A message to it reopens it: the same delegation runs
again in a new run, and its row's outcome is cleared until that run ends. Each outcome is
kept, numbered, in ``delegation_results``; ``results`` counts them, and the row's outcome is
the latest. The latest outcome's notice is keyed by ``notice_key``, so that an outcome a
parent received does not stand for the next one.

Each project's setting (``DelegationSettings``) says how deep its runs may delegate (0,
the default, offers no delegation; 1 lets main runs delegate), how many delegated runs
a session's tree runs at once, and a check's and a task's tokens and turns: a limit it
leaves out keeps its default, which a later release may raise. Each method is one
transaction and may run in a thread.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated, Final, Literal, cast

from pydantic import Field, JsonValue, ValidationError, field_validator

from sandbox_science.contracts import SYSTEM_CLOCK, Clock, Identifier
from sandbox_science.contracts.model import Contract
from sandbox_science.orchestrator.continuity.blueprints import Blueprint, reserve_child
from sandbox_science.orchestrator.control import ControlError, ControlStore
from sandbox_science.orchestrator.control.errors import (
    CONFLICT,
    INVALID_REQUEST,
    NOT_FOUND,
    UNAVAILABLE,
)
from sandbox_science.orchestrator.documents import DocumentStore
from sandbox_science.orchestrator.runstate import LEASE_SECONDS, cancel_requested, close_events

Ended = Literal["returned", "failed", "cancelled", "limit_reached", "interrupted"]
"""How a delegated run ended, as the harness observed it (PD-17)."""
MAX_DEPTH: Final = 2
"""The deepest delegation a project may allow: at 1 main runs delegate; at 2 a task a
main run allows may delegate tasks once more, and its own children do not (PD-15,
PD-43)."""
REPORT_CHARS: Final = 16_000
"""The longest report a delegation keeps whole; the child's record keeps all of it."""
RESULT_BYTES: Final = 32768
ERROR_BYTES: Final = 2000
PAGE: Final = 50
FILES_BYTES: Final = 131072
"""The most a delegation's list of recorded files takes, as UTF-8 JSON."""
TREE: Final = 500
"""The most delegations a session's tree shows: its latest."""

_COLUMNS: Final = (
    "delegation_id, parent, run, kind, brief, started_at, ended, stopped, report, "
    "ended_at, refused, acknowledged, result, errors, alias, models, read_entry, "
    "read_proofs, files, environment_id, environment, results, review, reviewed_at, "
    "reviewed_by, review_reason, review_note"
)
_RESULT_COLUMNS: Final = (
    "delegation_id, number, run, ended, stopped, report, ended_at, refused, result, errors, "
    "alias, models, read_entry, read_proofs, files, environment"
)
_JSON: Final = ("brief", "result", "errors", "models", "files", "environment")
ENVIRONMENT_BYTES: Final = 65536
"""The most a delegation's environment outcome takes, as UTF-8 JSON."""
ChildEnvironments = Literal["off", "tasks", "all"]
"""Which delegated runs work in an environment of their own (CE-2): none, tasks, or every
delegation, checks included."""
_OFFERED: Final = (
    "(d.parent = ? OR (? IS NULL AND NOT EXISTS (SELECT 1 FROM delegations AS p "
    "WHERE p.project_id = d.project_id AND p.session_id = d.session_id AND p.run = d.parent)))"
)
"""Normal roots retain the session-wide PD-35 handback contract."""
_WORK_OFFERED: Final = (
    "EXISTS (SELECT 1 FROM research_work w WHERE w.project=d.project_id "
    "AND w.session=d.session_id AND w.work=? AND (EXISTS "
    "(SELECT 1 FROM research_attempts a WHERE a.project=w.project AND a.session=w.session "
    "AND a.run=d.parent AND a.work=w.work) OR EXISTS "
    "(SELECT 1 FROM research_continuations a WHERE a.project=w.project AND a.session=w.session "
    "AND a.run=d.parent AND a.work=w.work)))"
)
_WORK_ACKNOWLEDGED: Final = (
    "EXISTS (SELECT 1 FROM research_attempts a WHERE a.project=d.project_id "
    "AND a.session=d.session_id AND a.run=d.acknowledged AND a.work=?)"
)
NOTICE_KEY: Final = (
    "(CASE WHEN d.results > 1 THEN d.delegation_id || '.' || d.results ELSE d.delegation_id END)"
)
"""``notice_key`` in SQL, of a delegation row named ``d``."""
_WORK_RECEIVED: Final = (
    "EXISTS (SELECT 1 FROM response_receipts r JOIN research_attempts a "  # noqa: S608 - fixed SQL
    "ON a.project=r.project AND a.session=r.session AND a.run=r.run "
    "WHERE r.project=d.project_id AND r.session=d.session_id AND r.source='delegations' "
    f"AND r.notice={NOTICE_KEY} AND a.work=?)"
)
_WORK_JOIN: Final = (
    "LEFT JOIN research_attempts a ON a.project=d.project_id AND a.session=d.session_id "
    "AND a.run=d.run LEFT JOIN research_work w ON w.project=a.project AND w.session=a.session "
    "AND w.work=a.work "
)
"""A delegation's latest run's durable work, ``w``, if it has one."""
_ON_TRACK: Final = (
    "SELECT delegation_id FROM delegations WHERE project_id = ? AND session_id = ? "
    "AND ended IS NULL AND json_extract(brief, '$.track.track_id') = ?"
)


TASK_TURNS: Final = 300
"""A task's turns by default (the owner, 2026-10-16: a delegated task does real work)."""
TASK_TOKENS: Final = 20_000_000
"""A task's tokens by default, its calls' input and output together. Stored delegated
runs fold their own context. Tokens still bound total model usage across calls,
including cached input; they do not count only fresh context."""
MAX_TURNS: Final = 10_000
"""The most turns a project may give a check or a task."""
MAX_TOKENS: Final = 1_000_000_000
"""The most tokens a project may give a check or a task."""
ROOT_TOKENS: Final = 100_000_000
"""A root's soft token cap by default: a check-in against its plan, never a stop (D102)."""
SOFT_COST: Final = 25.0
"""Each work's soft cost cap by default, in the gateway's currency (D102)."""
RESEARCH_SECONDS: Final = 14_400
"""Each work's soft active-time cap by default: four hours, a check-in (D102)."""
FORMER_DEFAULTS: Final = {"task_tokens": 200_000, "task_turns": 20}
"""A task's limits by default before 2026-10-16. A setting written then holds every
limit, its defaults among them: one of these values in it is read as the default, so
that the new defaults reach it. A setting written since holds only the limits that were
set, under ``SET``."""
SET: Final = "set"
"""The key of the limits a setting holds as set, in its JSON object."""
MAX_MODELS: Final = 10
REVIEW_MAX: Final = 86_400
"""Seconds a returned task awaits its parent's review by default before the platform
accepts it (PD-46)."""
NOTE_BYTES: Final = 2048
"""The most a parent's note on an acceptance takes, as UTF-8; it is at most 500
characters."""
EARLIER: Final = 20
"""The most earlier results of a delegation the session's tree lists."""
Review = Literal["awaiting", "accepted", "cancelled"]
"""A task's review (PD-46): awaiting its parent's, or closed by its acceptance or cancel."""
ReviewReason = Literal["parent", "parent_ended", "session_closed", "review_max"]
"""Who closed a returned task: its parent, or the platform for it, as its parent's run
ended, as the session closed, or past ``review_max``."""
Alias = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,127}$")]
"""A model alias as the gateway names it, such as ``main`` or ``anthropic/claude``."""


class DelegationSettings(Contract):
    """A project's delegation setting (PD-15, PD-38): what a project leaves out keeps
    its default."""

    depth: int = Field(default=0, ge=0, le=MAX_DEPTH)
    """How deep its runs may delegate; 0 offers no delegation."""
    cap: int = Field(default=10, ge=1, le=20)
    """Delegated runs a session's tree runs at once."""
    check_tokens: int = Field(default=TASK_TOKENS, ge=50_000, le=MAX_TOKENS)
    """A check's soft token cap: it checks in with its parent there (PD-38, D102)."""
    check_turns: int = Field(default=TASK_TURNS, ge=1, le=MAX_TURNS)
    """A check's soft turn cap, independently of its parent's remaining calls."""
    task_tokens: int = Field(default=TASK_TOKENS, ge=50_000, le=MAX_TOKENS)
    """A task's soft token cap (PD-11, PD-38): a check-in with its parent, not a bound on
    its work (D102)."""
    task_turns: int = Field(default=TASK_TURNS, ge=1, le=MAX_TURNS)
    """A task's soft turn cap, independently of its parent's remaining calls."""
    turn_ceiling: int = Field(default=MAX_TURNS, ge=1, le=MAX_TURNS)
    """Cumulative turns extensions may reach, and every work's hard turn limit across its
    attempts. Lowering this does not revoke an existing recorded extension.
    """
    max_token_grant: int = Field(default=MAX_TOKENS, ge=50_000, le=MAX_TOKENS)
    """The cumulative soft token cap extensions may reach (D102; it named the retired
    USER token approvals' ceiling)."""
    root_tokens: int = Field(default=ROOT_TOKENS, ge=50_000, le=MAX_TOKENS)
    """A root's soft token cap: it checks itself against its plan there (D102)."""
    soft_cost: float | None = Field(default=SOFT_COST, gt=0, le=1_000_000)
    """Each work's soft cost cap, in the gateway's currency, a check-in; ``None`` turns
    it off. It never stops spending: LiteLLM's budget does."""
    max_idle_extensions: int = Field(default=3, ge=1, le=100)
    """Extensions in a row without new evidence after which a parent must stop or
    redirect its child, and the root asks the user (D102)."""
    research_seconds: int | None = Field(default=RESEARCH_SECONDS, ge=1, le=31_536_000)
    """Each work's soft cumulative observed active time, independent of tool deadlines;
    ``None`` turns it off."""
    time_ceiling_seconds: int = Field(default=604_800, ge=1, le=31_536_000)
    storage_bytes: int | None = Field(default=None, ge=1, le=256 * 1024**3)
    """Optional retained-artifact allowance; not research-VM disk usage."""
    storage_ceiling_bytes: int = Field(default=64 * 1024**3, ge=1, le=256 * 1024**3)
    root_resource_renewal: bool = False
    """Permit audited root self-renewal of soft allowances with persisted progress."""
    models: tuple[Alias, ...] = Field(default=(), max_length=MAX_MODELS)
    """The model aliases a brief may choose (PD-28); none, the default, allows no choice,
    and a brief that names none gets the delegated role's."""
    fallback: Alias | None = None
    """The alias a delegated run calls when the gateway lists none of the one it was to
    call (PD-30); without it, such a delegation is refused."""
    child_environments: ChildEnvironments = "off"
    """Which delegated runs get an environment of their own, made from the session's
    (docs/specs/child-environments.md, CE-2): ``off`` (every run shares the session's),
    ``tasks`` (tasks do; checks share it) or ``all``. A task brief may ask to share."""
    child_profile: Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")] | None = None
    """The worker's child profile for this project's child environments; ``None`` for the
    pool's default (CE-3)."""
    child_idle_max: int | None = Field(default=None, ge=60, le=604_800)
    """Seconds an ended delegation's child environment may sit unused (an interrupted run
    never resumed) before it is released; ``None``, the default, for
    ``child_orphan_idle`` (CE-4, question 4)."""
    child_orphan_grace: int = Field(default=900, ge=60, le=86_400)
    """Seconds a child environment no delegation names (admitted, then never written) is
    kept before it is released."""
    child_orphan_idle: int = Field(default=86_400, ge=60, le=604_800)
    """Seconds an ended delegation's child environment is kept unused without
    ``child_idle_max``."""
    review_max: int = Field(default=REVIEW_MAX, ge=60, le=2_592_000)
    """Seconds a returned task awaits its parent's review, from its latest return,
    before the platform accepts it for the parent (PD-46)."""

    @field_validator("models")
    @classmethod
    def _distinct(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(value)) != len(value):
            raise ValueError("an alias is listed once")
        return value


class Delegation(Contract):
    """A delegation: its parent and child runs, its brief and, once ended, its outcome."""

    delegation_id: Identifier
    parent: str
    """The parent run, by the name its record is kept under."""
    run: str
    """The delegated run, by the name its record is kept under."""
    kind: Literal["check", "task"]
    brief: dict[str, JsonValue]
    started_at: datetime
    ended: Ended | None = None
    stopped: str | None = None
    """The delegated run's own ``stopped``, such as ``turn_limit``."""
    report: str | None = None
    """The delegated run's final answer as it returned it, its head and tail when long."""
    ended_at: datetime | None = None
    refused: str | None = None
    """Why the plan fence refused the child's calls, as the platform observed it."""
    acknowledged: str | None = None
    """The run whose saved record says a model call of it saw the outcome."""
    result: dict[str, JsonValue] | None = None
    """The child's result, as its output schema validated it; ``None`` when missing."""
    errors: tuple[str, ...] = ()
    """Why the child's answers did not validate, in order."""
    alias: str | None = None
    """The model alias the child ran under."""
    models: tuple[str, ...] = ()
    """The models the gateway named for its calls, in order of first use."""
    read_entry: bool | None = None
    """For a check: whether a ``ledger_read`` of the child returned the entry at the
    revision its brief names, as the platform observed it."""
    read_proofs: bool | None = None
    """For a check: whether the child read the target of each of the entry's proofs at
    the revision the link pins, as the platform observed it (LG-17)."""
    files: dict[str, JsonValue] | None = None
    """The artifact revisions and tombstones the child's calls recorded, as the platform
    observed them: at most 200 (``files``), their ``total`` and the calls' ``gaps``
    (RA-36). ``None`` when its calls recorded nothing."""
    environment_id: str | None = None
    """The child environment its run works in (CE-11); ``None`` when it shares the
    session's."""
    environment: dict[str, JsonValue] | None = None
    """How its child environment ended, with the hand-back's account (CE-6, CE-11)."""
    results: int = 0
    """How many outcomes its runs have had (PD-46): the row's outcome is the latest."""
    review: Review | None = None
    """A returned task's review (PD-46): ``awaiting`` its parent's, or closed;
    ``None`` for a check, a delegation that has not returned, or one from before."""
    reviewed_at: datetime | None = None
    reviewed_by: str | None = None
    """The run that closed it, or ``platform``."""
    review_reason: ReviewReason | None = None
    review_note: str | None = None
    """The parent's note on its acceptance, agent text."""


class DelegationResult(Contract):
    """One of a delegation's outcomes, as its run ended, numbered from 1 (PD-46)."""

    delegation_id: Identifier
    number: int = Field(ge=1)
    run: str
    ended: Ended
    stopped: str
    report: str | None = None
    ended_at: datetime
    refused: str | None = None
    result: dict[str, JsonValue] | None = None
    errors: tuple[str, ...] = ()
    alias: str | None = None
    models: tuple[str, ...] = ()
    read_entry: bool | None = None
    read_proofs: bool | None = None
    files: dict[str, JsonValue] | None = None
    environment: dict[str, JsonValue] | None = None


@dataclass(frozen=True)
class ReopenPlan:
    """The run a parent would start to reopen a returned task, and its bounds (PD-46)."""

    run: str
    """The new run's name, as its record will be kept."""
    cap: int | None
    """The session's cap of delegated runs at once (PD-38)."""
    work: str | None = None
    """The parent's durable work, when it is a Resume's continuation: a task an earlier
    attempt of it started is its child too."""
    fenced: bool = False
    """Whether the parent's plan fence was asked, as for ``delegate``, because the task
    awaited review when the message was planned."""
    refused: tuple[str, str] | None = None
    """The fence's refusal, its kind and its reason; a reopening it refuses is refused."""


@dataclass(frozen=True)
class Reopened:
    """A returned task a message reopened: its work's next attempt, ``run``, continues
    ``previous`` in the session's ``epoch`` (PD-46)."""

    delegation_id: str
    run: str
    previous: str
    work: str
    epoch: int
    environment_id: str | None
    number: int
    """The number its next outcome will have."""


@dataclass(frozen=True)
class Closed:
    """A returned task its parent accepted or cancelled, or the platform accepted: what
    is left to release (PD-46)."""

    delegation: Delegation
    kernel: str | None
    """Its work's kernels' run, which stop when it shares the session's environment."""
    plan: tuple[tuple[str, ...], tuple[dict[str, JsonValue], ...]] | None
    """Its child environment's hand-back, when it holds one: the paths to copy and the
    files to name by their revisions (CE-6)."""


def notice_key(delegation: Delegation) -> str:
    """The key of a delegation's latest outcome in notices and their acknowledgements: its
    ID for its first, ``<id>.<n>`` for its nth (PD-46): an identifier, as a record's
    deliveries name them."""
    if delegation.results <= 1:
        return delegation.delegation_id
    return f"{delegation.delegation_id}.{delegation.results}"


def parse_key(key: str) -> tuple[str, int]:
    """A notice key's delegation and outcome number."""
    found, _, number = key.partition(".")
    return found, int(number) if number.isdigit() else 1


@dataclass(frozen=True)
class Ending:
    """A delegation's outcome, as the harness writes it."""

    ended: Ended
    stopped: str
    report: str | None = None
    refused: str | None = None
    result: dict[str, JsonValue] | None = None
    errors: tuple[str, ...] = ()
    alias: str | None = None
    models: tuple[str, ...] = ()
    read_entry: bool | None = None
    read_proofs: bool | None = None
    files: dict[str, JsonValue] | None = None
    environment: dict[str, JsonValue] | None = None


class DelegationRecords:
    """Delegations in one control store, which its owner closes."""

    def __init__(self, store: ControlStore | DocumentStore, *, clock: Clock = SYSTEM_CLOCK) -> None:
        self.control = store if isinstance(store, ControlStore) else store.control
        self.clock = clock

    def _now(self) -> str:
        return self.clock.now().astimezone(UTC).isoformat()

    def settings(self, project: str) -> DelegationSettings:
        """The project's delegation setting; the defaults when it was never set."""
        with self.control.read() as tx:
            return _settings(tx.sql, project)

    def configure(self, project: str, **changes: object) -> DelegationSettings:
        """Change some of the project's delegation setting, keeping the rest; the setting
        as it now is."""
        with self.control.write() as tx:
            db = tx.sql
            if (
                db.execute("SELECT 1 FROM projects WHERE project_id = ?", (project,)).fetchone()
                is None
            ):
                raise ControlError(NOT_FOUND, f"no project {project!r}")
            depth, stored = _stored(db, project)
            # Only what was ever set is kept, so that the defaults of a later release
            # reach what was not.
            kept = {**stored, **{k: v for k, v in changes.items() if k != "depth"}}
            try:
                found = DelegationSettings.model_validate(
                    {**kept, "depth": changes.get("depth", depth)}
                )
            except ValidationError as error:
                first = error.errors()[0]
                where = ".".join(str(part) for part in first["loc"])
                raise ControlError(INVALID_REQUEST, f"{where}: {first['msg']}") from None
            dumped = found.model_dump(mode="json", exclude={"depth"})
            limits = _json({SET: {name: dumped[name] for name in kept if name in dumped}})
            db.execute(
                "INSERT OR REPLACE INTO project_delegation (project_id, depth, updated_at, "
                "limits) VALUES (?, ?, ?, ?)",
                (project, found.depth, self._now(), limits),
            )
            return found

    def start(
        self,
        project: str,
        session: str,
        delegation: Delegation,
        *,
        cap: int | None = None,
        blueprint: Blueprint | None = None,
        operation: str | None = None,
    ) -> None:
        """Write a delegation as its parent starts it. The bounds are checked in the same
        statement that writes it, so that two parents starting at once cannot pass them
        both: with ``cap``, it is refused (``limit_reached``) while ``cap`` of the
        session's delegations run, at any level (PD-38); and a task on a track is refused
        (``track_busy``) while another of the session's delegations runs on it (PD-9)."""
        found = delegation.brief.get("track")
        track = cast(dict[str, object], found).get("track_id") if isinstance(found, dict) else None
        with self.control.write() as tx:
            db = tx.sql
            if cancel_requested(db, project, session, delegation.parent):
                raise ControlError(
                    CONFLICT, "the parent tree has an explicit Stop; no child starts"
                )
            try:
                written = db.execute(
                    "INSERT INTO delegations (delegation_id, project_id, session_id, parent, "  # noqa: S608
                    "run, kind, brief, started_at, environment_id) "
                    "SELECT ?, ?, ?, ?, ?, ?, ?, ?, ? "
                    "WHERE (? IS NULL OR (SELECT count(*) FROM delegations WHERE project_id = ? "
                    "AND session_id = ? AND ended IS NULL) < ?) "
                    f"AND (? IS NULL OR NOT EXISTS ({_ON_TRACK}))",
                    (
                        delegation.delegation_id,
                        project,
                        session,
                        delegation.parent,
                        delegation.run,
                        delegation.kind,
                        _json(delegation.brief),
                        delegation.started_at.astimezone(UTC).isoformat(),
                        delegation.environment_id,
                        cap,
                        project,
                        session,
                        cap,
                        track,
                        project,
                        session,
                        track,
                    ),
                ).rowcount
            except sqlite3.IntegrityError as error:
                raise ControlError(INVALID_REQUEST, f"the delegation is refused: {error}") from None
            if written == 1:
                scope = (project, session, delegation.parent)
                db.execute(
                    "INSERT INTO run_collections SELECT ?,?,?,?,count(*),? "
                    "FROM run_collections WHERE project=? AND session=? AND run=? AND collection=?",
                    (
                        *scope,
                        "delegations:started",
                        _json(delegation.delegation_id),
                        *scope,
                        "delegations:started",
                    ),
                )
                if blueprint is not None:
                    reserve_child(
                        db, project, session, delegation.run, delegation.parent, blueprint
                    )
                if operation is not None:
                    try:
                        db.execute(
                            "INSERT INTO research_observations "
                            "(project,session,work,kind,identity,observed,detail) "
                            "SELECT project,session,work,'delegation_operation',?,?,? "
                            "FROM research_attempts WHERE project=? AND session=? AND run=?",
                            (
                                operation,
                                self.clock.now().timestamp(),
                                _json(
                                    {
                                        "delegation_id": delegation.delegation_id,
                                        "run": delegation.run,
                                    }
                                ),
                                project,
                                session,
                                delegation.parent,
                            ),
                        )
                    except sqlite3.IntegrityError:
                        raise ControlError(
                            CONFLICT, "this delegation operation is already admitted"
                        ) from None
                return
            busy = db.execute(_ON_TRACK, (project, session, track)).fetchone()
        if busy is not None:
            raise ControlError(
                CONFLICT,
                f"track_busy: delegation {busy[0]} works on track {track!r}; a track has one "
                "running task at most",
            )
        raise ControlError(
            UNAVAILABLE,
            f"limit_reached: {cap} delegated runs are running in this session; wait for one to end",
        )

    def end(
        self,
        project: str,
        session: str,
        delegation_id: str,
        ending: Ending,
        run: str | None = None,
    ) -> Delegation:
        """Write a delegation's outcome; one written already stands, and is returned. With
        ``run``, only while that run is the delegation's latest: a reopened task's earlier
        run never writes over its next (PD-46).

        Every value is bounded here to the table's checks, so that the write cannot be
        refused and the outcome lost: a result only for a delegation that returned, and
        only within its bound; the errors and the models, each clipped, the oldest left
        out until they fit; the alias and the fence's refusal, clipped. A task that
        returned awaits its parent's review; the outcome is kept as the delegation's next
        result."""
        result = _json(ending.result) if ending.result is not None else None
        errors = list(ending.errors)
        if result is not None and (
            ending.ended != "returned" or len(result.encode()) > RESULT_BYTES
        ):
            errors.append("the result was not kept: it ended otherwise, or was too long")
            result = None
        with self.control.write() as tx:
            changed = tx.sql.execute(
                "UPDATE delegations SET ended = ?, stopped = ?, report = ?, ended_at = ?, "
                "refused = ?, result = ?, errors = ?, alias = ?, models = ?, read_entry = ?, "
                "read_proofs = ?, files = ?, environment = ?, results = results + 1, "
                "review = CASE WHEN kind = 'task' AND ? = 'returned' THEN 'awaiting' END "
                "WHERE project_id = ? AND session_id = ? "
                "AND delegation_id = ? AND ended IS NULL AND (? IS NULL OR run = ?)",
                (
                    ending.ended,
                    _clipped(ending.stopped, 64),
                    _kept(ending.report),
                    self._now(),
                    _clipped(ending.refused, 2048) if ending.refused else None,
                    result,
                    _fitted([_clipped(e, ERROR_BYTES) for e in errors], 16384),
                    _clipped(ending.alias, 256) if ending.alias else None,
                    _fitted([_clipped(m, 128) for m in ending.models], 4096),
                    None if ending.read_entry is None else int(ending.read_entry),
                    None if ending.read_proofs is None else int(ending.read_proofs),
                    _files(ending.files),
                    _environment(ending.environment),
                    ending.ended,
                    project,
                    session,
                    delegation_id,
                    run,
                    run,
                ),
            ).rowcount
            del changed  # the store keeps it as the next result (delegations_archived)
            return _one(tx.sql, project, session, delegation_id)

    def by_environment(
        self, project: str, session: str, environment_id: str, delegation_id: str
    ) -> list[Delegation]:
        """The session's delegations that name the child environment, or that it was made
        for: the one it was admitted for, and continuations a Resume placed in it."""
        with self.control.read() as tx:
            rows = tx.sql.execute(
                f"SELECT {_COLUMNS} FROM delegations WHERE project_id = ? AND session_id = ? "  # noqa: S608
                "AND (environment_id = ? OR delegation_id = ?)",
                (project, session, environment_id, delegation_id),
            ).fetchall()
        return [_delegation(row) for row in rows]

    def uses_child_environments(self, project: str, session: str) -> bool:
        """Whether any of the session's delegations was placed in a child environment:
        a project switched to ``off`` may still hold some, which the reconcile ends."""
        with self.control.read() as tx:
            found = tx.sql.execute(
                "SELECT 1 FROM delegations WHERE project_id = ? AND session_id = ? "
                "AND environment_id IS NOT NULL LIMIT 1",
                (project, session),
            ).fetchone()
        return found is not None

    def work_environment(self, project: str, session: str, work: str) -> str | None:
        """The child environment the latest of a research work's delegations worked in,
        if any had one: where a Resume continues it (CE-12)."""
        with self.control.read() as tx:
            found = tx.sql.execute(
                "SELECT environment_id FROM delegations WHERE project_id = ? AND session_id = ? "
                "AND environment_id IS NOT NULL AND run IN (SELECT run FROM research_attempts "
                "WHERE project = ? AND session = ? AND work = ? UNION SELECT run FROM "
                "research_continuations WHERE project = ? AND session = ? AND work = ?) "
                "ORDER BY started_at DESC, delegation_id DESC LIMIT 1",
                (project, session, project, session, work, project, session, work),
            ).fetchone()
        return None if found is None else str(found[0])

    def place(self, project: str, session: str, delegation_id: str, environment_id: str) -> None:
        """Name the child environment of a running delegation that has none yet: a
        continuation's, which a Resume reserves before it knows where it runs (CE-12).
        Written once."""
        with self.control.write() as tx:
            changed = tx.sql.execute(
                "UPDATE delegations SET environment_id = ? WHERE project_id = ? "
                "AND session_id = ? AND delegation_id = ? AND ended IS NULL "
                "AND (environment_id IS NULL OR environment_id = ?)",
                (environment_id, project, session, delegation_id, environment_id),
            ).rowcount
        if changed != 1:
            raise ControlError(
                CONFLICT, f"delegation {delegation_id!r} has ended or works elsewhere"
            )

    def abandon(
        self,
        project: str,
        session: str,
        *,
        parent: str | None = None,
        resume: str | None = None,
    ) -> list[tuple[str, str]]:
        """End a dead tree, in one transaction (PD-40): the session's delegations still
        running, or only those of ``parent``'s tree, its children's and theirs, end
        ``interrupted`` (or ``cancelled`` under a durable Stop), and every run lacking its
        ``stopped`` event gets
        it (``close_events``). Called as a main run starts, when none of the session's
        delegations can be running, and when the run service ends a dead run. The IDs
        and runs of the delegations it ended. ``resume`` preserves exactly the new
        continuation runs that request reserved before its concurrent branches start;
        older open records are still fenced as dead."""
        now = self.clock.now().astimezone(UTC)
        # ``parent``'s tree: its children, and theirs (PD-15), as their records name them.
        tree = (
            "WITH RECURSIVE tree(run) AS (SELECT ? UNION SELECT d.run FROM delegations AS d "
            "JOIN tree AS t ON d.parent = t.run WHERE d.project_id = ? AND d.session_id = ?) "
        )
        values = (parent, project, session)
        with self.control.write() as tx:
            db = tx.sql
            rows = db.execute(
                f"{tree}SELECT delegation_id, run FROM delegations "  # noqa: S608
                "WHERE project_id = ? AND session_id = ? AND ended IS NULL "
                "AND (? IS NULL OR parent IN (SELECT run FROM tree)) "
                "AND (? IS NULL OR NOT EXISTS (SELECT 1 FROM research_continuations p "
                "WHERE p.project=delegations.project_id AND p.session=delegations.session_id "
                "AND p.request=? AND p.run=delegations.run)) "
                "AND NOT EXISTS (SELECT 1 FROM research_child_settlements c JOIN runs r "
                "ON r.project_id=c.project AND r.session_id=c.session AND r.name=c.run "
                "WHERE c.project=delegations.project_id AND c.session=delegations.session_id "
                "AND c.run=delegations.run AND c.settled=0)",
                (*values, project, session, parent, resume, resume),
            ).fetchall()
            for delegation_id, run in rows:
                reason = (
                    "cancelled" if cancel_requested(db, project, session, run) else "interrupted"
                )
                # A reopened task in its own environment still holds its earlier results'
                # files: its account keeps their hand-back for the sweep (PD-46, CE-6).
                kept = _kept_environment(db, project, session, delegation_id)
                db.execute(
                    "UPDATE delegations SET ended = ?, stopped = ?, ended_at = ?, "
                    "results = results + 1, environment = coalesce(environment, ?) "
                    "WHERE project_id = ? AND session_id = ? AND delegation_id = ?",
                    (reason, reason, now.isoformat(), kept, project, session, delegation_id),
                )

            unclosed = db.execute(
                f"{tree}SELECT d.run FROM delegations AS d WHERE d.project_id = ? "  # noqa: S608
                "AND d.session_id = ? AND (? IS NULL OR d.parent IN (SELECT run FROM tree)) "
                "AND (? IS NULL OR NOT EXISTS (SELECT 1 FROM research_continuations p "
                "WHERE p.project=d.project_id AND p.session=d.session_id "
                "AND p.request=? AND p.run=d.run)) "
                "AND NOT EXISTS (SELECT 1 FROM run_events AS e "
                "WHERE e.project_id = d.project_id AND e.session_id = d.session_id "
                "AND e.run = d.run AND e.kind = 'stopped') ORDER BY d.run",
                (*values, project, session, parent, resume, resume),
            ).fetchall()
            for (run,) in unclosed:
                close_events(db, project, session, run, now)
        return sorted((delegation_id, run) for delegation_id, run in rows)

    def get(self, project: str, session: str, delegation_id: str) -> Delegation:
        with self.control.read() as tx:
            return _one(tx.sql, project, session, delegation_id)

    def listed(
        self, project: str, session: str, *, limit: int = TREE
    ) -> tuple[list[Delegation], int]:
        """The session's delegations, the latest ``limit`` of them oldest first, with how
        many there are (PD-42)."""
        with self.control.read() as tx:
            db = tx.sql
            (total,) = db.execute(
                "SELECT count(*) FROM delegations WHERE project_id = ? AND session_id = ?",
                (project, session),
            ).fetchone()
            rows = db.execute(
                f"SELECT {_COLUMNS} FROM delegations WHERE project_id = ? AND session_id = ? "  # noqa: S608
                "ORDER BY started_at DESC, delegation_id DESC LIMIT ?",
                (project, session, limit),
            ).fetchall()
        return [_delegation(row) for row in reversed(rows)], int(total)

    def of_runs(self, project: str, session: str, runs: Sequence[str]) -> dict[str, Delegation]:
        """The session's delegations whose delegated run is one of ``runs``, by run: a main
        run, or one the session's records do not list, has none. Where a run was delegated
        twice, the later delegation."""
        wanted = sorted(set(runs))
        if not wanted:
            return {}
        with self.control.read() as tx:
            rows = tx.sql.execute(
                f"SELECT {_COLUMNS} FROM delegations WHERE project_id = ? AND session_id = ? "  # noqa: S608
                "AND run IN (SELECT value FROM json_each(?)) "
                "ORDER BY started_at, delegation_id",
                (project, session, json.dumps(wanted)),
            ).fetchall()
        return {found.run: found for found in map(_delegation, rows)}

    def handbacks(
        self, project: str, session: str, *, parent: str | None = None, work: str | None = None
    ) -> list[Delegation]:
        """The session's ended delegations that no run has acknowledged, oldest first:
        ``parent``'s, or without it those a main run started, which the session's next
        main run takes (PD-35); a delegated run's children hand back to it alone.
        Explicit continuations instead select ``work``'s parent attempts and exclude
        only that responsibility's acknowledgements or atomic response receipts."""
        with self.control.read() as tx:
            if work is not None:
                rows = tx.sql.execute(
                    f"SELECT {_COLUMNS} FROM delegations AS d "  # noqa: S608 - fixed predicates
                    "WHERE project_id=? AND session_id=? AND ended IS NOT NULL "
                    f"AND {_WORK_OFFERED} AND NOT {_WORK_ACKNOWLEDGED} AND NOT {_WORK_RECEIVED} "
                    "ORDER BY ended_at,delegation_id LIMIT ?",
                    (project, session, work, work, work, PAGE),
                ).fetchall()
                return [_delegation(row) for row in rows]
            rows = tx.sql.execute(
                f"SELECT {_COLUMNS} FROM delegations AS d WHERE project_id = ? AND session_id = ? "  # noqa: S608
                f"AND acknowledged IS NULL AND ended IS NOT NULL AND {_OFFERED} "
                "ORDER BY ended_at, delegation_id LIMIT ?",
                (project, session, parent, parent, PAGE),
            ).fetchall()
        return [_delegation(row) for row in rows]

    def running(self, project: str, session: str) -> list[str]:
        """The runs of the session's delegations still running, by their records' names:
        one tree's, the session's lease's (PD-38)."""
        with self.control.read() as tx:
            rows = tx.sql.execute(
                "SELECT run FROM delegations WHERE project_id = ? AND session_id = ? "
                "AND ended IS NULL ORDER BY run",
                (project, session),
            ).fetchall()
        return [run for (run,) in rows]

    def acknowledge(
        self,
        project: str,
        session: str,
        ids: Sequence[str],
        run: str,
        *,
        parent: str | None = None,
        work: str | None = None,
    ) -> None:
        """Mark ended delegations' outcomes received by ``run``, once its record is saved:
        only those ``handbacks`` offers it, ``parent``'s or without it a main run's, so
        that a run that read another's outcome does not take it from that run (PD-35).
        A continuation acknowledges only children of its stable ``work``; receipt
        rows remain authoritative even if final-save acknowledgement never executes.
        ``ids`` are notice keys (``notice_key``): a reopened task's next outcome is not
        acknowledged by a run that saw its earlier one (PD-46)."""
        keyed = [parse_key(key) for key in ids]
        with self.control.write() as tx:
            if work is not None:
                tx.sql.executemany(
                    "UPDATE delegations AS d SET acknowledged=? WHERE d.project_id=? "  # noqa: S608
                    "AND d.session_id=? AND d.delegation_id=? AND max(d.results, 1)=? "
                    "AND d.ended IS NOT NULL "
                    f"AND d.acknowledged IS NULL AND {_WORK_OFFERED}",
                    [(run, project, session, found, n, work) for found, n in keyed],
                )
                return
            tx.sql.executemany(
                "UPDATE delegations AS d SET acknowledged = ? WHERE d.project_id = ? "  # noqa: S608
                "AND d.session_id = ? AND d.delegation_id = ? AND max(d.results, 1) = ? "
                "AND d.ended IS NOT NULL "
                f"AND d.acknowledged IS NULL AND {_OFFERED}",
                [(run, project, session, found, n, parent, parent) for found, n in keyed],
            )

    # --- The parent's review (PD-46) -------------------------------------------------

    def accept(
        self,
        project: str,
        session: str,
        delegation_id: str,
        *,
        by: str,
        reason: ReviewReason,
        note: str | None = None,
        review: Literal["accepted", "cancelled"] = "accepted",
        parent: str | None = None,
        work: str | None = None,
    ) -> Closed:
        """Close a returned task, in one transaction that only a task still awaiting its
        review passes, so that an acceptance and a reopening message cannot both win: as
        ``review`` (its parent's acceptance or cancel, or the platform's acceptance for
        it), ``by`` a run or ``platform``, for ``reason``, with the parent's ``note``. With
        ``parent``, only the child of that run (or of an attempt of its ``work``). A child
        environment it holds is marked ``closing`` with its hand-back's plan, which
        ``closed_environment`` replaces once it is released."""
        with self.control.write() as tx:
            db = tx.sql
            row = db.execute(
                "SELECT d.parent, d.review, d.ended, d.kind, d.environment, w.kernel "  # noqa: S608
                f"FROM delegations d {_WORK_JOIN}"
                "WHERE d.project_id = ? AND d.session_id = ? AND d.delegation_id = ?",
                (project, session, delegation_id),
            ).fetchone()
            if row is None:
                raise ControlError(
                    NOT_FOUND, f"session {session!r} has no delegation {delegation_id!r}"
                )
            if parent is not None and not _parent_of(
                db, project, session, str(row[0]), parent, work
            ):
                raise ControlError(
                    CONFLICT,
                    f"not_your_child: delegation {delegation_id!r} is not one of this run's",
                )
            if parent is not None:
                # A parent acts only while its tree's root holds the session, as a
                # reopening message does (PD-44, PD-46).
                self._held(db, project, session, parent)
            if row[1] != "awaiting":
                state = _state(row[2], row[1], row[3])
                raise ControlError(
                    CONFLICT,
                    f"not_returned: delegation {delegation_id!r} is {state}; only a returned "
                    "task that awaits its parent's review is accepted",
                )
            loaded: object = json.loads(row[4]) if row[4] is not None else None
            environment = cast(dict[str, JsonValue], loaded) if isinstance(loaded, dict) else None
            plan = None
            if environment is not None and environment.get("end") == "held":
                # Every result's files come back, an earlier run's too (CE-6, PD-46).
                plan = _union(db, project, session, delegation_id, _plan(environment.get("plan")))
                marked: dict[str, JsonValue] = {
                    "files": list[JsonValue](plan[0]),
                    "retained": list[JsonValue](plan[1]),
                }
                closing: dict[str, JsonValue] = {**environment, "end": "closing"}
                closing["plan"] = marked
                environment = closing
            db.execute(
                "UPDATE delegations SET review = ?, reviewed_at = ?, reviewed_by = ?, "
                "review_reason = ?, review_note = ?, environment = ? WHERE project_id = ? "
                "AND session_id = ? AND delegation_id = ? AND review = 'awaiting'",
                (
                    review,
                    self._now(),
                    _clipped(by, 256),
                    reason,
                    _clipped(note, NOTE_BYTES) if note else None,
                    _environment(environment),
                    project,
                    session,
                    delegation_id,
                ),
            )
            return Closed(_one(db, project, session, delegation_id), row[5], plan)

    def _held(self, db: sqlite3.Connection, project: str, session: str, run: str) -> None:
        """Refuse unless ``run``'s tree's root holds the session's live lease: ``run`` itself,
        or the parent of the delegation it runs for."""
        found = db.execute(
            "SELECT parent FROM delegations WHERE project_id = ? AND session_id = ? AND run = ?",
            (project, session, run),
        ).fetchone()
        root = run if found is None else str(found[0])
        lease = db.execute(
            "SELECT run, renewed_at FROM run_leases WHERE project_id = ? AND session_id = ?",
            (project, session),
        ).fetchone()
        if (
            lease is None
            or lease[0] != root
            or lease[1] <= self.clock.now().timestamp() - LEASE_SECONDS
        ):
            raise ControlError(CONFLICT, "the research tree no longer holds its session")

    def closed_environment(
        self, project: str, session: str, delegation_id: str, environment: dict[str, JsonValue]
    ) -> Delegation:
        """Write the account of a child environment once its hand-back and release ended
        (CE-4, CE-6), in place of the mark that left them to the delegation's close or the
        sweep: a closed task's ``closing``, or an ended one's ``held`` or ``kept`` with its
        plan; written once."""
        with self.control.write() as tx:
            tx.sql.execute(
                "UPDATE delegations SET environment = ? WHERE project_id = ? AND session_id = ? "
                "AND delegation_id = ? AND ended IS NOT NULL "
                "AND coalesce(review, '') <> 'awaiting' "
                "AND json_extract(environment, '$.end') IN ('held', 'kept', 'closing') "
                "AND json_extract(environment, '$.plan') IS NOT NULL",
                (_environment(environment), project, session, delegation_id),
            )
            return _one(tx.sql, project, session, delegation_id)

    def awaiting(
        self,
        project: str,
        session: str,
        *,
        parent: str | None = None,
        work: str | None = None,
        returned_before: datetime | None = None,
    ) -> list[Delegation]:
        """The session's returned tasks that await their parent's review, oldest return
        first: ``parent``'s (or those of an attempt of its ``work``), or all; with
        ``returned_before``, only those that returned before it (``review_max``)."""
        before = None if returned_before is None else returned_before.astimezone(UTC).isoformat()
        with self.control.read() as tx:
            db = tx.sql
            rows = db.execute(
                f"SELECT {_COLUMNS} FROM delegations AS d WHERE project_id = ? "  # noqa: S608
                "AND session_id = ? AND review = 'awaiting' AND (? IS NULL OR ended_at < ?) "
                "ORDER BY ended_at, delegation_id LIMIT ?",
                (project, session, before, before, TREE),
            ).fetchall()
            found = [_delegation(row) for row in rows]
            if parent is None:
                return found
            return [d for d in found if _parent_of(db, project, session, d.parent, parent, work)]

    def union_plan(
        self,
        project: str,
        session: str,
        delegation_id: str,
        latest: tuple[tuple[str, ...], tuple[dict[str, JsonValue], ...]],
    ) -> tuple[tuple[str, ...], tuple[dict[str, JsonValue], ...]]:
        """``latest``, a run's hand-back plan, with every earlier result's of the delegation
        (CE-6, PD-46): a reopened task's environment holds all of their files."""
        with self.control.read() as tx:
            return _union(tx.sql, project, session, delegation_id, latest)

    def accepted_for(self, project: str, session: str, parent: str) -> list[Delegation]:
        """The returned tasks of ``parent`` that the platform accepted for it as its run
        ended, which its own parent is told of (PD-46)."""
        with self.control.read() as tx:
            rows = tx.sql.execute(
                f"SELECT {_COLUMNS} FROM delegations WHERE project_id = ? AND session_id = ? "  # noqa: S608
                "AND parent = ? AND review = 'accepted' AND review_reason = 'parent_ended' "
                "ORDER BY ended_at, delegation_id LIMIT ?",
                (project, session, parent, PAGE),
            ).fetchall()
        return [_delegation(row) for row in rows]

    def closing(self, project: str, session: str) -> list[Delegation]:
        """The session's closed tasks whose child environment's release did not finish:
        the process stopped between the acceptance and its account."""
        with self.control.read() as tx:
            rows = tx.sql.execute(
                f"SELECT {_COLUMNS} FROM delegations WHERE project_id = ? AND session_id = ? "  # noqa: S608
                "AND review IN ('accepted', 'cancelled') "
                "AND json_extract(environment, '$.end') = 'closing' LIMIT ?",
                (project, session, TREE),
            ).fetchall()
        return [_delegation(row) for row in rows]

    def held_kernels(self, project: str, session: str) -> set[str]:
        """The runs whose kernels the session's delegations hold: those of a running
        delegation's work, and of a returned task's that awaits review (PD-46)."""
        with self.control.read() as tx:
            rows = tx.sql.execute(
                f"SELECT w.kernel FROM delegations d {_WORK_JOIN}"  # noqa: S608
                "WHERE d.project_id = ? AND d.session_id = ? AND w.kernel IS NOT NULL "
                "AND (d.ended IS NULL OR d.review = 'awaiting')",
                (project, session),
            ).fetchall()
        return {str(kernel) for (kernel,) in rows}

    def earlier(
        self, project: str, session: str, delegation_ids: Sequence[str]
    ) -> list[DelegationResult]:
        """The earlier results of ``delegation_ids``, every one but each delegation's
        latest, at most ``EARLIER`` of each, the latest of them, oldest first (PD-46)."""
        wanted = sorted(set(delegation_ids))
        if not wanted:
            return []
        with self.control.read() as tx:
            rows = tx.sql.execute(
                f"SELECT {_RESULT_COLUMNS} FROM (SELECT r.*, row_number() OVER "  # noqa: S608
                "(PARTITION BY r.delegation_id ORDER BY r.number DESC) AS place "
                "FROM delegation_results r JOIN delegations d "
                "ON d.delegation_id = r.delegation_id AND r.number < d.results "
                "WHERE r.project_id = ? AND r.session_id = ? "
                "AND r.delegation_id IN (SELECT value FROM json_each(?))) "
                "WHERE place <= ? ORDER BY delegation_id, number",
                (project, session, json.dumps(wanted), EARLIER),
            ).fetchall()
        return [_result(row) for row in rows]

    def results(self, project: str, session: str, delegation_id: str) -> list[DelegationResult]:
        """Every result of a delegation, oldest first (PD-46)."""
        with self.control.read() as tx:
            rows = tx.sql.execute(
                f"SELECT {_RESULT_COLUMNS} FROM delegation_results WHERE project_id = ? "  # noqa: S608
                "AND session_id = ? AND delegation_id = ? ORDER BY number",
                (project, session, delegation_id),
            ).fetchall()
        return [_result(row) for row in rows]


def reopen_in(
    db: sqlite3.Connection,
    project: str,
    session: str,
    delegation_id: str,
    run: str,
    *,
    cap: int | None,
) -> Reopened:
    """Reopen a returned task inside the caller's write transaction (PD-46): its row's
    outcome cleared and ``run`` its latest, its durable work waiting for that attempt to
    continue its last, as Resume prepares one (``WorkRecords.admit``). Refused unless it
    awaits review, its work can continue in the session's current tree, the session runs
    fewer than ``cap`` delegated runs (PD-38) and no other task runs on its track (PD-9).
    The caller checks that its sender is the task's parent."""
    row = db.execute(
        "SELECT d.review, d.ended, d.kind, d.run, d.brief, d.environment_id, d.results, "  # noqa: S608
        "w.work, w.state, w.current, w.root, s.root, s.epoch "
        f"FROM delegations d {_WORK_JOIN}"
        "LEFT JOIN research_sessions s ON s.project = d.project_id AND s.session = d.session_id "
        "WHERE d.project_id = ? AND d.session_id = ? AND d.delegation_id = ?",
        (project, session, delegation_id),
    ).fetchone()
    if row is None:
        raise ControlError(NOT_FOUND, f"no delegation {delegation_id!r} in this session")
    review, ended, kind, previous, brief, environment_id, results = row[:7]
    work, state, current, root, session_root, epoch = row[7:]
    if review != "awaiting":
        raise ControlError(
            CONFLICT,
            f"not_returned: delegation {delegation_id!r} is {_state(ended, review, kind)}",
        )
    if work is None or state != "completed" or current != previous or root != session_root:
        raise ControlError(
            CONFLICT,
            f"delegation {delegation_id!r} cannot continue: its work is not this tree's latest",
        )
    if cap is not None and (
        db.execute(
            "SELECT count(*) FROM delegations WHERE project_id = ? AND session_id = ? "
            "AND ended IS NULL",
            (project, session),
        ).fetchone()[0]
        >= cap
    ):
        raise ControlError(
            UNAVAILABLE,
            f"limit_reached: {cap} delegated runs are running in this session; wait for one to end",
        )
    loaded: object = json.loads(brief)
    track = (
        cast(dict[str, object], cast(dict[str, object], loaded).get("track") or {}).get("track_id")
        if isinstance(loaded, dict)
        else None
    )
    if track is not None and (busy := db.execute(_ON_TRACK, (project, session, track)).fetchone()):
        raise ControlError(
            CONFLICT,
            f"track_busy: delegation {busy[0]} works on track {track!r}; a track has one "
            "running task at most",
        )
    db.execute(
        "UPDATE delegations SET run = ?, ended = NULL, stopped = NULL, report = NULL, "
        "ended_at = NULL, refused = NULL, result = NULL, errors = NULL, alias = NULL, "
        "models = NULL, read_entry = NULL, read_proofs = NULL, files = NULL, "
        "environment = NULL, acknowledged = NULL, review = NULL WHERE project_id = ? "
        "AND session_id = ? AND delegation_id = ? AND review = 'awaiting'",
        (run, project, session, delegation_id),
    )
    db.execute(
        "UPDATE research_work SET state = 'waiting', reason = NULL WHERE project = ? "
        "AND session = ? AND work = ? AND state = 'completed' AND current = ?",
        (project, session, work, previous),
    )
    return Reopened(
        delegation_id, run, str(previous), str(work), int(epoch), environment_id, int(results) + 1
    )


def close_awaiting(
    db: sqlite3.Connection, project: str, session: str, reason: ReviewReason, now: str
) -> int:
    """Accept, in the caller's transaction, every returned task of the session that awaits
    review, as the platform: what the session's close does (PD-46). Its child environments
    and kernels go with the session's. How many."""
    # A child environment it holds goes with the session's, released without retention
    # and without a hand-back (CE-4, JS-25): its account says so.
    return db.execute(
        "UPDATE delegations SET review = 'accepted', reviewed_at = ?, reviewed_by = 'platform', "
        "review_reason = ?, environment = CASE WHEN json_extract(environment, '$.end') = 'held' "
        "THEN json_set(json_remove(environment, '$.plan'), '$.end', 'released', '$.message', "
        "'released with the session, without a hand-back') ELSE environment END "
        "WHERE project_id = ? AND session_id = ? AND review = 'awaiting'",
        (now, reason, project, session),
    ).rowcount


def delegation_state(delegation: Delegation) -> str:
    """A delegation's state in a word (PD-46): ``running``, ``returned`` (awaiting
    review), ``accepted``, ``cancelled``, or how it ended."""
    return _state(delegation.ended, delegation.review, delegation.kind)


def _state(ended: object, review: object, kind: object) -> str:
    if ended is None:
        return "running"
    if review == "awaiting":
        return "returned"
    if review in ("accepted", "cancelled"):
        return str(review)
    return str(ended)


def _parent_of(
    db: sqlite3.Connection,
    project: str,
    session: str,
    parent_run: str,
    caller: str,
    work: str | None,
) -> bool:
    """Whether ``caller`` is ``parent_run``, or, with its ``work``, a later attempt of the
    same durable work, as a Resume's continuation is."""
    if parent_run == caller:
        return True
    if work is None:
        return False
    return (
        db.execute(
            "SELECT 1 FROM research_attempts WHERE project = ? AND session = ? AND run = ? "
            "AND work = ? UNION SELECT 1 FROM research_continuations WHERE project = ? "
            "AND session = ? AND run = ? AND work = ?",
            (project, session, parent_run, work, project, session, parent_run, work),
        ).fetchone()
        is not None
    )


def _kept_environment(
    db: sqlite3.Connection, project: str, session: str, delegation_id: str
) -> str | None:
    """The account an interrupted delegation's child environment keeps: kept for a Resume,
    with its earlier results' hand-back; ``None`` without either."""
    row = db.execute(
        "SELECT environment_id FROM delegations WHERE project_id = ? AND session_id = ? "
        "AND delegation_id = ?",
        (project, session, delegation_id),
    ).fetchone()
    if row is None or row[0] is None:
        return None
    files, retained = _union(db, project, session, delegation_id, ((), ()))
    if not files and not retained:
        return None
    return _environment(
        {
            "environment_id": row[0],
            "end": "kept",
            "message": "kept for a Resume of the run to continue in",
            "plan": {"files": list[JsonValue](files), "retained": list[JsonValue](retained)},
        }
    )


def held_plan(
    environment: dict[str, JsonValue] | None,
) -> tuple[tuple[str, ...], tuple[dict[str, JsonValue], ...]] | None:
    """The hand-back plan a held or closing child environment's account keeps (PD-46);
    ``None`` for any other account."""
    if environment is None or environment.get("end") not in ("held", "kept", "closing"):
        return None
    if environment.get("plan") is None:
        return None
    return _plan(environment.get("plan"))


def _union(
    db: sqlite3.Connection,
    project: str,
    session: str,
    delegation_id: str,
    latest: tuple[tuple[str, ...], tuple[dict[str, JsonValue], ...]],
) -> tuple[tuple[str, ...], tuple[dict[str, JsonValue], ...]]:
    """The latest hand-back plan with its delegation's earlier results' plans, each path
    once: a file the latest names by its revision is not copied."""
    plans = [latest]
    for (found,) in db.execute(
        "SELECT environment FROM delegation_results WHERE project_id = ? AND session_id = ? "
        "AND delegation_id = ? AND environment IS NOT NULL ORDER BY number DESC",
        (project, session, delegation_id),
    ):
        loaded: object = json.loads(found)
        if isinstance(loaded, dict) and (earlier := held_plan(cast(dict[str, JsonValue], loaded))):
            plans.append(earlier)
    files: dict[str, None] = {}
    retained: dict[str, dict[str, JsonValue]] = {}
    for copy, named in plans:
        for item in named:
            path = item.get("path")
            if isinstance(path, str) and path not in retained and path not in files:
                retained[path] = item
        for path in copy:
            if path not in retained:
                files.setdefault(path, None)
    return tuple(files), tuple(retained.values())


def _plan(
    found: object,
) -> tuple[tuple[str, ...], tuple[dict[str, JsonValue], ...]]:
    """A held environment's hand-back plan, as its account keeps it."""
    plan = cast(dict[str, object], found) if isinstance(found, dict) else {}
    files, retained = plan.get("files"), plan.get("retained")
    return (
        tuple(str(f) for f in cast(list[object], files)) if isinstance(files, list) else (),
        tuple(
            cast(dict[str, JsonValue], r)
            for r in cast(list[object], retained)
            if isinstance(r, dict)
        )
        if isinstance(retained, list)
        else (),
    )


def _settings(db: sqlite3.Connection, project: str) -> DelegationSettings:
    depth, stored = _stored(db, project)
    return DelegationSettings.model_validate({**stored, "depth": depth})


def settings_in(db: sqlite3.Connection, project: str) -> DelegationSettings:
    """Read the current project limits inside a caller's control transaction."""
    return _settings(db, project)


def _stored(db: sqlite3.Connection, project: str) -> tuple[int, dict[str, object]]:
    """The project's depth and the limits it set, without a former default that a
    setting written before 2026-10-16 holds (``FORMER_DEFAULTS``)."""
    found = db.execute(
        "SELECT depth, limits FROM project_delegation WHERE project_id = ?", (project,)
    ).fetchone()
    if found is None:
        return 0, {}
    depth, limits = found
    loaded: object = json.loads(limits) if limits is not None else None
    stored = cast(dict[str, object], loaded) if isinstance(loaded, dict) else {}
    if isinstance(kept := stored.get(SET), dict):
        return depth, dict(cast(dict[str, object], kept))
    return depth, {
        name: value
        for name, value in stored.items()
        if name not in FORMER_DEFAULTS or value != FORMER_DEFAULTS[name]
    }


def _files(files: dict[str, JsonValue] | None) -> str | None:
    """The recorded files as the table keeps them: entries left out from the end until
    the whole fits ``FILES_BYTES``, with the total kept."""
    if files is None:
        return None
    entries = files.get("files")
    listed = list(entries) if isinstance(entries, list) else []
    while True:
        text = _json({**files, "files": listed})
        if len(text.encode()) <= FILES_BYTES or not listed:
            return text
        listed = listed[: len(listed) // 2]


def _environment(environment: dict[str, JsonValue] | None) -> str | None:
    """The environment's outcome as the table keeps it: its hand-back's lists cut until
    the whole fits ``ENVIRONMENT_BYTES``."""
    if environment is None:
        return None
    found = dict(environment)
    while len(_json(found).encode()) > ENVIRONMENT_BYTES:
        plan = found.get("plan")
        if isinstance(plan, dict) and len(_json(plan).encode()) > ENVIRONMENT_BYTES // 2:
            # A held environment's plan is cut first: what it leaves out stays on the
            # environment, which a cut hand-back keeps (CE-6).
            cut_plan = {
                k: (v[: len(v) // 2] if isinstance(v, list) else v)
                for k, v in cast(dict[str, JsonValue], plan).items()
            }
            found["plan"] = {**cut_plan, "cut": True}
            continue
        back = found.get("hand_back")
        if not isinstance(back, dict):
            return _json({k: v for k, v in found.items() if k not in ("hand_back", "plan")})
        cut = dict(back)
        longest = max(
            (k for k, v in cut.items() if isinstance(v, list) and v),
            key=lambda k: len(_json(cut[k])),
            default=None,
        )
        if longest is None:
            found.pop("hand_back")
            continue
        listed = cast(list[JsonValue], cut[longest])
        cut[longest] = listed[: len(listed) // 2]
        cut["cut"] = True
        found["hand_back"] = cut
    return _json(found)


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _clipped(text: str, size: int) -> str:
    """``text`` cut to at most ``size`` bytes of UTF-8, at a character."""
    return text.encode()[:size].decode(errors="ignore")


def _fitted(items: list[str], size: int) -> str | None:
    """``items`` as a JSON array of at most ``size`` bytes, the oldest left out until
    they fit; ``None`` for none."""
    while items and len(_json(items).encode()) > size:
        items = items[1:]
    return _json(items) if items else None


def _kept(report: str | None) -> str | None:
    if report is None or len(report) <= REPORT_CHARS:
        return report
    half = REPORT_CHARS // 2
    left_out = len(report) - 2 * half
    return (
        f"{report[:half]}\n[… {left_out} characters left out; the delegated run's record "
        f"keeps them …]\n{report[-half:]}"
    )


def _one(db: sqlite3.Connection, project: str, session: str, delegation_id: str) -> Delegation:
    found = db.execute(
        f"SELECT {_COLUMNS} FROM delegations WHERE project_id = ? AND session_id = ? "  # noqa: S608
        "AND delegation_id = ?",
        (project, session, delegation_id),
    ).fetchone()
    if found is None:
        raise ControlError(NOT_FOUND, f"session {session!r} has no delegation {delegation_id!r}")
    return _delegation(found)


def _result(row: tuple[object, ...]) -> DelegationResult:
    names = [name.strip() for name in _RESULT_COLUMNS.split(",")]
    fields = dict(zip(names, row, strict=True))
    for name in ("result", "errors", "models", "files", "environment"):
        if fields[name] is not None:
            fields[name] = json.loads(cast(str, fields[name]))
    return DelegationResult.model_validate({k: v for k, v in fields.items() if v is not None})


def _delegation(row: tuple[object, ...]) -> Delegation:
    names = [name.strip() for name in _COLUMNS.split(",")]
    fields = dict(zip(names, row, strict=True))
    for name in _JSON:
        if fields[name] is not None:
            fields[name] = json.loads(cast(str, fields[name]))
    return Delegation.model_validate({k: v for k, v in fields.items() if v is not None})
