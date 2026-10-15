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
a session's tree runs at once, and a check's tokens and turns. Each method is one
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
from sandbox_science.orchestrator.control import ControlError, ControlStore
from sandbox_science.orchestrator.control.errors import INVALID_REQUEST, NOT_FOUND
from sandbox_science.orchestrator.documents import DocumentStore
from sandbox_science.orchestrator.runstate import close_events

Ended = Literal["returned", "failed", "cancelled", "limit_reached", "interrupted"]
"""How a delegated run ended, as the harness observed it (PD-17)."""
MAX_DEPTH: Final = 1
"""The deepest delegation a project may allow: main runs delegate, delegated runs do not
(PD-43: depth 2 comes once one level works)."""
REPORT_CHARS: Final = 16_000
"""The longest report a delegation keeps whole; the child's record keeps all of it."""
RESULT_BYTES: Final = 32768
ERROR_BYTES: Final = 2000
PAGE: Final = 50

_COLUMNS: Final = (
    "delegation_id, parent, run, kind, brief, started_at, ended, stopped, report, "
    "ended_at, refused, acknowledged, result, errors, alias, models, read_entry, "
    "read_proofs"
)
_JSON: Final = ("brief", "result", "errors", "models")


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
    check_tokens: int = Field(default=150_000, ge=50_000, le=400_000)
    """A check's tokens, at most: reserved from its root's share of the model budget."""
    check_turns: int = Field(default=12, ge=1, le=30)
    """A check's turns, at most, and no more than its parent has left."""
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
    kind: Literal["check"]
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
            try:
                found = DelegationSettings.model_validate(
                    {**_settings(db, project).model_dump(), **changes}
                )
            except ValidationError as error:
                first = error.errors()[0]
                where = ".".join(str(part) for part in first["loc"])
                raise ControlError(INVALID_REQUEST, f"{where}: {first['msg']}") from None
            limits = _json(found.model_dump(exclude={"depth"}))
            db.execute(
                "INSERT OR REPLACE INTO project_delegation (project_id, depth, updated_at, "
                "limits) VALUES (?, ?, ?, ?)",
                (project, found.depth, self._now(), limits),
            )
            return found

    def start(self, project: str, session: str, delegation: Delegation) -> None:
        """Write a delegation as its parent starts it."""
        with self.control.write() as tx:
            try:
                tx.sql.execute(
                    "INSERT INTO delegations (delegation_id, project_id, session_id, parent, "
                    "run, kind, brief, started_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        delegation.delegation_id,
                        project,
                        session,
                        delegation.parent,
                        delegation.run,
                        delegation.kind,
                        _json(delegation.brief),
                        delegation.started_at.astimezone(UTC).isoformat(),
                    ),
                )
            except sqlite3.IntegrityError as error:
                raise ControlError(INVALID_REQUEST, f"the delegation is refused: {error}") from None

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
                "read_proofs = ? WHERE project_id = ? AND session_id = ? AND delegation_id = ? "
                "AND ended IS NULL",
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
        running, or only ``parent``'s, end ``interrupted``, and every run of the
        session's delegations, or of ``parent``'s, that lacks its ``stopped`` event gets
        it (``close_events``). Called as a main run starts, when none of the session's
        delegations can be running, and when the run service ends a dead run. The IDs
        and runs of the delegations it ended."""
        now = self.clock.now().astimezone(UTC)
        with self.control.write() as tx:
            db = tx.sql
            rows = db.execute(
                "UPDATE delegations SET ended = 'interrupted', stopped = 'interrupted', "
                "ended_at = ? WHERE project_id = ? AND session_id = ? AND ended IS NULL "
                "AND (? IS NULL OR parent = ?) RETURNING delegation_id, run",
                (now.isoformat(), project, session, parent, parent),
            ).fetchall()
            unclosed = db.execute(
                "SELECT d.run FROM delegations AS d WHERE d.project_id = ? AND d.session_id = ? "
                "AND (? IS NULL OR d.parent = ?) AND NOT EXISTS (SELECT 1 FROM run_events AS e "
                "WHERE e.project_id = d.project_id AND e.session_id = d.session_id "
                "AND e.run = d.run AND e.kind = 'stopped') ORDER BY d.run",
                (project, session, parent, parent),
            ).fetchall()
            for (run,) in unclosed:
                close_events(db, project, session, run, now)
        return sorted((delegation_id, run) for delegation_id, run in rows)

    def get(self, project: str, session: str, delegation_id: str) -> Delegation:
        with self.control.read() as tx:
            return _one(tx.sql, project, session, delegation_id)

    def handbacks(self, project: str, session: str) -> list[Delegation]:
        """The session's ended delegations that no run has acknowledged, oldest first."""
        with self.control.read() as tx:
            rows = tx.sql.execute(
                f"SELECT {_COLUMNS} FROM delegations WHERE project_id = ? AND session_id = ? "  # noqa: S608
                "AND acknowledged IS NULL AND ended IS NOT NULL ORDER BY ended_at, delegation_id "
                "LIMIT ?",
                (project, session, PAGE),
            ).fetchall()
        return [_delegation(row) for row in rows]

    def acknowledge(self, project: str, session: str, ids: Sequence[str], run: str) -> None:
        """Mark ended delegations' outcomes received by ``run``, once its record is saved."""
        with self.control.write() as tx:
            tx.sql.executemany(
                "UPDATE delegations SET acknowledged = ? WHERE project_id = ? AND session_id = ? "
                "AND delegation_id = ? AND ended IS NOT NULL AND acknowledged IS NULL",
                [(run, project, session, delegation_id) for delegation_id in ids],
            )


def _settings(db: sqlite3.Connection, project: str) -> DelegationSettings:
    found = db.execute(
        "SELECT depth, limits FROM project_delegation WHERE project_id = ?", (project,)
    ).fetchone()
    if found is None:
        return DelegationSettings()
    depth, limits = found
    return DelegationSettings.model_validate(
        {**(json.loads(limits) if limits is not None else {}), "depth": depth}
    )


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
