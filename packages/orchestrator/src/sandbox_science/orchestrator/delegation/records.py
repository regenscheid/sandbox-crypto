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
from sandbox_science.orchestrator.runstate import cancel_requested, close_events

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
    "read_proofs, files"
)
_JSON: Final = ("brief", "result", "errors", "models", "files")
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
_WORK_RECEIVED: Final = (
    "EXISTS (SELECT 1 FROM response_receipts r JOIN research_attempts a "
    "ON a.project=r.project AND a.session=r.session AND a.run=r.run "
    "WHERE r.project=d.project_id AND r.session=d.session_id AND r.source='delegations' "
    "AND r.notice=d.delegation_id AND a.work=?)"
)
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
FORMER_DEFAULTS: Final = {"task_tokens": 200_000, "task_turns": 20}
"""A task's limits by default before 2026-10-16. A setting written then holds every
limit, its defaults among them: one of these values in it is read as the default, so
that the new defaults reach it. A setting written since holds only the limits that were
set, under ``SET``."""
SET: Final = "set"
"""The key of the limits a setting holds as set, in its JSON object."""
MAX_MODELS: Final = 10
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
    """A check's tokens, at most: a bound on a runaway check (PD-38)."""
    check_turns: int = Field(default=TASK_TURNS, ge=1, le=MAX_TURNS)
    """A check's turns, at most, independently of its parent's remaining calls."""
    task_tokens: int = Field(default=TASK_TOKENS, ge=50_000, le=MAX_TOKENS)
    """A task's tokens, at most (PD-11, PD-38): a bound on a runaway task, not on its
    work."""
    task_turns: int = Field(default=TASK_TURNS, ge=1, le=MAX_TURNS)
    """A task's turns, at most, independently of its parent's remaining calls."""
    turn_ceiling: int = Field(default=MAX_TURNS, ge=1, le=MAX_TURNS)
    """Cumulative turns a direct parent may approve; initial explicit turns remain.
    Lowering this does not revoke an existing recorded grant.
    """
    max_token_grant: int = Field(default=MAX_TOKENS, ge=50_000, le=MAX_TOKENS)
    """Hard ceiling for explicit USER token extensions, separate from initial grants."""
    research_seconds: int | None = Field(default=None, ge=1, le=31_536_000)
    """Optional cumulative observed active-time allowance, independent of tool deadlines."""
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
                    "run, kind, brief, started_at) SELECT ?, ?, ?, ?, ?, ?, ?, ? "
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

    def end(self, project: str, session: str, delegation_id: str, ending: Ending) -> Delegation:
        """Write a delegation's outcome; one written already stands, and is returned.

        Every value is bounded here to the table's checks, so that the write cannot be
        refused and the outcome lost: a result only for a delegation that returned, and
        only within its bound; the errors and the models, each clipped, the oldest left
        out until they fit; the alias and the fence's refusal, clipped."""
        result = _json(ending.result) if ending.result is not None else None
        errors = list(ending.errors)
        if result is not None and (
            ending.ended != "returned" or len(result.encode()) > RESULT_BYTES
        ):
            errors.append("the result was not kept: it ended otherwise, or was too long")
            result = None
        with self.control.write() as tx:
            tx.sql.execute(
                "UPDATE delegations SET ended = ?, stopped = ?, report = ?, ended_at = ?, "
                "refused = ?, result = ?, errors = ?, alias = ?, models = ?, read_entry = ?, "
                "read_proofs = ?, files = ? WHERE project_id = ? AND session_id = ? "
                "AND delegation_id = ? AND ended IS NULL",
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
                    project,
                    session,
                    delegation_id,
                ),
            )
            return _one(tx.sql, project, session, delegation_id)

    def abandon(
        self, project: str, session: str, *, parent: str | None = None
    ) -> list[tuple[str, str]]:
        """End a dead tree, in one transaction (PD-40): the session's delegations still
        running, or only those of ``parent``'s tree, its children's and theirs, end
        ``interrupted`` (or ``cancelled`` under a durable Stop), and every run lacking its
        ``stopped`` event gets
        it (``close_events``). Called as a main run starts, when none of the session's
        delegations can be running, and when the run service ends a dead run. The IDs
        and runs of the delegations it ended."""
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
                "AND NOT EXISTS (SELECT 1 FROM research_child_settlements c JOIN runs r "
                "ON r.project_id=c.project AND r.session_id=c.session AND r.name=c.run "
                "WHERE c.project=delegations.project_id AND c.session=delegations.session_id "
                "AND c.run=delegations.run AND c.settled=0)",
                (*values, project, session, parent),
            ).fetchall()
            for delegation_id, run in rows:
                reason = (
                    "cancelled" if cancel_requested(db, project, session, run) else "interrupted"
                )
                db.execute(
                    "UPDATE delegations SET ended = ?, stopped = ?, ended_at = ? "
                    "WHERE project_id = ? AND session_id = ? AND delegation_id = ?",
                    (reason, reason, now.isoformat(), project, session, delegation_id),
                )
            unclosed = db.execute(
                f"{tree}SELECT d.run FROM delegations AS d WHERE d.project_id = ? "  # noqa: S608
                "AND d.session_id = ? AND (? IS NULL OR d.parent IN (SELECT run FROM tree)) "
                "AND NOT EXISTS (SELECT 1 FROM run_events AS e "
                "WHERE e.project_id = d.project_id AND e.session_id = d.session_id "
                "AND e.run = d.run AND e.kind = 'stopped') ORDER BY d.run",
                (*values, project, session, parent),
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
        rows remain authoritative even if final-save acknowledgement never executes."""
        with self.control.write() as tx:
            if work is not None:
                tx.sql.executemany(
                    "UPDATE delegations AS d SET acknowledged=? WHERE d.project_id=? "  # noqa: S608
                    "AND d.session_id=? AND d.delegation_id=? AND d.ended IS NOT NULL "
                    f"AND d.acknowledged IS NULL AND {_WORK_OFFERED}",
                    [(run, project, session, identity, work) for identity in ids],
                )
                return
            tx.sql.executemany(
                "UPDATE delegations AS d SET acknowledged = ? WHERE d.project_id = ? "  # noqa: S608
                "AND d.session_id = ? AND d.delegation_id = ? AND d.ended IS NOT NULL "
                f"AND d.acknowledged IS NULL AND {_OFFERED}",
                [(run, project, session, delegation_id, parent, parent) for delegation_id in ids],
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


def _delegation(row: tuple[object, ...]) -> Delegation:
    names = [name.strip() for name in _COLUMNS.split(",")]
    fields = dict(zip(names, row, strict=True))
    for name in _JSON:
        if fields[name] is not None:
            fields[name] = json.loads(cast(str, fields[name]))
    return Delegation.model_validate({k: v for k, v in fields.items() if v is not None})
