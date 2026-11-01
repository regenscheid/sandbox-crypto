# SPDX-License-Identifier: MIT
"""Resolve immutable mailbox addresses through scoped research-work attempts."""

from __future__ import annotations

import sqlite3
from typing import Final

# A leaf-first Resume reserves a parent's delegation before admitting its attempt.
# Both mappings are immutable and scoped; a matching brief is never a routing key.
_WORK_JOIN: Final = (
    "LEFT JOIN research_attempts a ON a.project=d.project_id AND a.session=d.session_id "
    "AND a.run=d.run LEFT JOIN research_continuations c ON c.project=d.project_id "
    "AND c.session=d.session_id AND c.run=d.run LEFT JOIN research_work w "
    "ON w.project=d.project_id AND w.session=d.session_id AND w.work=coalesce(a.work,c.work) "
)


def work_for_run(db: sqlite3.Connection, project: str, session: str, run: str) -> str | None:
    """An admitted or prepared attempt's exact logical identity, if recorded."""
    row = db.execute(
        "SELECT work FROM research_attempts WHERE project=? AND session=? AND run=? "
        "UNION SELECT work FROM research_continuations WHERE project=? AND session=? AND run=?",
        (project, session, run, project, session, run),
    ).fetchone()
    return None if row is None else str(row[0])


def recipient_state(
    db: sqlite3.Connection, project: str, session: str, delegation: str
) -> tuple[str, bool, str | None] | None:
    """Current run, terminal responsibility, and stable work behind an address."""
    row = db.execute(
        "SELECT d.run,d.ended,w.work,w.current,w.state,d.review FROM delegations d "  # noqa: S608 - fixed joins
        + _WORK_JOIN
        + "WHERE d.project_id=? AND d.session_id=? AND d.delegation_id=?",
        (project, session, delegation),
    ).fetchone()
    if row is None:
        return None
    # A returned task that awaits its parent's review is open: a message reopens it
    # (PD-46), so it has not ended for its mailbox.
    reviewing = row[5] == "awaiting"
    if row[2] is None:
        return str(row[0]), row[1] is not None and not reviewing, None
    return str(row[3]), row[4] in ("completed", "stopped") and not reviewing, str(row[2])


def recipient_target(
    db: sqlite3.Connection, project: str, session: str, delegation: str | None
) -> tuple[str, tuple[object, ...]]:
    """One predicate shared by inbox reads and final-save acknowledgment."""
    linked = (
        "SELECT d.delegation_id FROM delegations d "  # noqa: S608 - fixed joins
        + _WORK_JOIN
        + "WHERE d.project_id=? AND d.session_id=? AND "
    )
    if delegation is None:
        # A dead attempt is still a resumable responsibility. Do not let the root
        # consume its queued instructions as failures before Resume can deliver them;
        # nor a returned task's that awaits review, which a reopening run receives
        # (PD-46).
        return (
            "(recipient_delegation IS NULL OR recipient_delegation IN ("
            + linked
            + "d.ended IS NOT NULL AND coalesce(d.review, '') <> 'awaiting' "
            "AND (w.work IS NULL OR w.state IN ('completed','stopped'))))",
            (project, session),
        )
    state = recipient_state(db, project, session, delegation)
    if state is None or state[2] is None:
        return "recipient_delegation = ?", (delegation,)
    return "recipient_delegation IN (" + linked + "w.work=?)", (project, session, state[2])


def received_by(
    db: sqlite3.Connection, project: str, session: str, delegation: str, run: str
) -> bool:
    """Whether an acknowledgment belongs to the addressed work, not a root fallback."""
    state = recipient_state(db, project, session, delegation)
    if state is None:
        return False
    return (
        state[0] == run if state[2] is None else work_for_run(db, project, session, run) == state[2]
    )
