# SPDX-License-Identifier: MIT
"""Delegations and each project's delegation setting, in the control store.

A delegation is written when its parent starts it, with its brief, and once more when
its child ends, with its outcome: how the child ended, as the harness observed it, the
child's final answer as it returned it, its report, and why the plan fence refused its
calls, if it did (PD-17, PD-20, PD-34). Neither changes afterwards. The run that
received the outcome is written once that run's record is saved (PD-35); until then,
the outcome waits for the session's next main run. A delegation still running when its
session's next main run starts has lost its process: it ends ``interrupted`` (PD-40).

Each project's setting is how deep its runs may delegate: 0, the default, offers no
delegation; 1 lets main runs delegate. Each method is one transaction and may run in a
thread.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final, Literal, cast

from pydantic import JsonValue

from sandbox_science.contracts import SYSTEM_CLOCK, Clock, Identifier
from sandbox_science.contracts.model import Contract
from sandbox_science.orchestrator.control import ControlError, ControlStore
from sandbox_science.orchestrator.control.errors import INVALID_REQUEST, NOT_FOUND
from sandbox_science.orchestrator.documents import DocumentStore

Ended = Literal["returned", "failed", "cancelled", "limit_reached", "interrupted"]
"""How a delegated run ended, as the harness observed it (PD-17)."""
MAX_DEPTH: Final = 1
"""The deepest delegation a project may allow: main runs delegate, delegated runs do not
(PD-43: depth 2 comes once one level works)."""
REPORT_CHARS: Final = 16_000
"""The longest report a delegation keeps whole; the child's record keeps all of it."""
PAGE: Final = 50

_COLUMNS: Final = (
    "delegation_id, parent, run, kind, brief, started_at, ended, stopped, report, "
    "ended_at, refused, acknowledged"
)


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


@dataclass(frozen=True)
class Ending:
    """A delegation's outcome, as the harness writes it."""

    ended: Ended
    stopped: str
    report: str | None = None
    refused: str | None = None


class DelegationRecords:
    """Delegations in one control store, which its owner closes."""

    def __init__(self, store: ControlStore | DocumentStore, *, clock: Clock = SYSTEM_CLOCK) -> None:
        self.control = store if isinstance(store, ControlStore) else store.control
        self.clock = clock

    def _now(self) -> str:
        return self.clock.now().astimezone(UTC).isoformat()

    def depth(self, project: str) -> int:
        """How deep the project's runs may delegate; 0 when it was never set."""
        with self.control.read() as tx:
            found = tx.sql.execute(
                "SELECT depth FROM project_delegation WHERE project_id = ?", (project,)
            ).fetchone()
        return 0 if found is None else int(found[0])

    def set_depth(self, project: str, depth: int) -> None:
        if not 0 <= depth <= MAX_DEPTH:
            raise ControlError(INVALID_REQUEST, f"a delegation depth is 0 to {MAX_DEPTH}")
        with self.control.write() as tx:
            if (
                tx.sql.execute("SELECT 1 FROM projects WHERE project_id = ?", (project,)).fetchone()
                is None
            ):
                raise ControlError(NOT_FOUND, f"no project {project!r}")
            tx.sql.execute(
                "INSERT OR REPLACE INTO project_delegation VALUES (?, ?, ?)",
                (project, depth, self._now()),
            )

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
                        json.dumps(delegation.brief, ensure_ascii=False),
                        delegation.started_at.astimezone(UTC).isoformat(),
                    ),
                )
            except sqlite3.IntegrityError as error:
                raise ControlError(INVALID_REQUEST, f"the delegation is refused: {error}") from None

    def end(self, project: str, session: str, delegation_id: str, ending: Ending) -> Delegation:
        """Write a delegation's outcome; one written already stands, and is returned."""
        with self.control.write() as tx:
            tx.sql.execute(
                "UPDATE delegations SET ended = ?, stopped = ?, report = ?, ended_at = ?, "
                "refused = ? WHERE project_id = ? AND session_id = ? AND delegation_id = ? "
                "AND ended IS NULL",
                (
                    ending.ended,
                    ending.stopped,
                    _kept(ending.report),
                    self._now(),
                    ending.refused[:2000] if ending.refused else None,
                    project,
                    session,
                    delegation_id,
                ),
            )
            return _one(tx.sql, project, session, delegation_id)

    def abandon(self, project: str, session: str) -> list[str]:
        """End ``interrupted`` the session's delegations that are still running: called
        as a main run starts, when none of the session's can be (PD-40). Their IDs."""
        with self.control.write() as tx:
            rows = tx.sql.execute(
                "UPDATE delegations SET ended = 'interrupted', stopped = 'interrupted', "
                "ended_at = ? WHERE project_id = ? AND session_id = ? AND ended IS NULL "
                "RETURNING delegation_id",
                (self._now(), project, session),
            ).fetchall()
        return sorted(delegation_id for (delegation_id,) in rows)

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
    fields["brief"] = json.loads(cast(str, fields["brief"]))
    return Delegation.model_validate({k: v for k, v in fields.items() if v is not None})
