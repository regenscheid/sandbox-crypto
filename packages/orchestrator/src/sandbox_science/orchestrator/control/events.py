# SPDX-License-Identifier: MIT
"""The event log: each change's events, committed with it, in one table.

``control_events`` numbers every event of the store in commit order (``seq``), and
names its project, session (if any), producer, and the subject it is about: a document,
a review or a finding. A repository keeps one ``EventLog`` per kind of subject; it
appends in the change it records, which tells the control store's subscribers, and it
reads its own subjects' events back (refactoring plan, B-04).
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final

from .store import ControlEvent, ControlStore, Tx

Json = dict[str, Any]

_COLUMNS: Final = "seq,time,kind,producer,project,session,subject_id,payload"


@dataclass(frozen=True)
class Event:
    """An event as the log keeps it."""

    seq: int
    time: datetime
    kind: str
    producer: str
    project: str
    session: str | None
    subject: str
    payload: Json


def _event(row: tuple[Any, ...]) -> Event:
    seq, time, kind, producer, project, session, subject, payload = row
    return Event(
        seq,
        datetime.fromisoformat(time),
        kind,
        producer,
        project,
        session,
        subject,
        json.loads(payload),
    )


class EventLog:
    """The events about one kind of subject (``subject_type``), as ``producer`` writes
    them."""

    SUBJECTS: Final = ("document", "review", "finding", "ledger_entry", "memory")

    def __init__(self, control: ControlStore, subject_type: str, producer: str) -> None:
        if subject_type not in self.SUBJECTS:
            raise ValueError(f"unknown event subject {subject_type!r}")
        self.control, self.subject_type, self.producer = control, subject_type, producer

    def append(
        self,
        db: sqlite3.Connection,
        kind: str,
        project: str,
        session: str | None,
        subject: str,
        payload: Json,
        at: datetime,
    ) -> int:
        """Record an event in the open change ``db`` and tell the subscribers of its
        kind, which run in that change. Returns its ``seq``."""
        cursor = db.execute(
            "INSERT INTO control_events(time,kind,producer,project,session,subject_type,"
            "subject_id,payload) VALUES(?,?,?,?,?,?,?,?)",
            (
                at.astimezone(UTC).isoformat(),
                kind,
                self.producer,
                project,
                session,
                self.subject_type,
                subject,
                json.dumps(payload, separators=(",", ":"), ensure_ascii=False, allow_nan=False),
            ),
        )
        seq = cursor.lastrowid
        assert seq is not None  # noqa: S101 - this event was inserted
        document = subject if self.subject_type == "document" else None
        self.control.notify(Tx(db), ControlEvent(seq, kind, project, document, payload))
        return seq

    def head(self, db: sqlite3.Connection, project: str) -> int:
        """The ``seq`` of the project's latest event of this kind, or 0."""
        return db.execute(
            "SELECT coalesce(max(seq),0) FROM control_events WHERE project=? AND subject_type=?",
            (project, self.subject_type),
        ).fetchone()[0]

    def page(
        self,
        db: sqlite3.Connection,
        project: str,
        after: int,
        limit: int,
        *,
        through: int | None = None,
    ) -> list[Event]:
        """The project's events after ``after`` (and up to ``through``), oldest first."""
        rows = db.execute(
            f"SELECT {_COLUMNS} FROM control_events WHERE project=? AND subject_type=? "  # noqa: S608
            "AND seq>? AND seq<=? ORDER BY seq LIMIT ?",
            (
                project,
                self.subject_type,
                after,
                through if through is not None else 2**63 - 1,
                limit,
            ),
        ).fetchall()
        return [_event(row) for row in rows]

    def recent(
        self, db: sqlite3.Connection, project: str, session: str, kind: str, limit: int
    ) -> list[Event]:
        """A session's latest events of ``kind``, newest first."""
        rows = db.execute(
            f"SELECT {_COLUMNS} FROM control_events WHERE project=? AND subject_type=? "  # noqa: S608
            "AND session=? AND kind=? ORDER BY seq DESC LIMIT ?",
            (project, self.subject_type, session, kind, limit),
        ).fetchall()
        return [_event(row) for row in rows]
