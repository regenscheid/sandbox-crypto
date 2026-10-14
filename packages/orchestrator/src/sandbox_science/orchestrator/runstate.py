# SPDX-License-Identifier: MIT
"""A session's chat messages, its run lease and its runs' events, in the control store.

The run service's records (docs/specs/run-service.md):

- **Messages** (RS-4). The user writes a message to a session; it waits ``queued`` until
  a run delivers it, and is marked ``delivered`` with that run only after the run's
  record is saved (RS-9). A message ID makes a retried write safe. A run marks the
  messages it offers, so what it saw is known without its events; a new message queues
  the session's held messages again (RS-12).
- **The run lease** (RS-5). One run of a session at a time, from the command line or the
  service. A lease whose holder stops renewing it for ``lease_seconds`` has expired.
- **Run events** (RS-13, RS-14). What a run did, appended as it happens, numbered from 1
  within the run, and read by cursor.

Each method is one transaction of the control store and may run in a thread. Times
that processes compare (a lease's renewal) are wall-clock seconds from ``clock``.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from typing import Final, Literal, cast

from pydantic import Field, JsonValue

from sandbox_science.contracts import SYSTEM_CLOCK, Clock, Identifier
from sandbox_science.contracts.model import Contract

from .control import ControlError, ControlStore
from .control.errors import BUSY, CONFLICT, INVALID_REQUEST, NOT_FOUND
from .documents import DocumentStore

LEASE_SECONDS: Final = 60.0
"""How long a run's lease outlives its last renewal (RS-5)."""
MAX_MESSAGE_BYTES: Final = 65536
MAX_EVENT_BYTES: Final = 65536
PAGE: Final = 200
"""The most messages or events one read returns."""

MessageState = Literal["queued", "delivered", "refused", "held"]
EventKind = Literal["started", "user", "model", "assistant", "tool", "notice", "waiting", "stopped"]


class ChatMessage(Contract):
    """A message the user wrote to a session (RS-4)."""

    message_id: Identifier
    number: int
    """Its place among the store's messages: a session's messages in the order written."""
    text: str
    author: Identifier
    created_at: datetime
    state: MessageState
    run: str | None = None
    """The run that delivered it: the name its record is kept under."""
    reason: str | None = None
    """Why it was refused."""


class RunEvent(Contract):
    """One thing a run did, for display (RS-14)."""

    seq: int = Field(ge=1)
    at: datetime
    kind: EventKind
    data: dict[str, JsonValue]


class RunLease(Contract):
    """The run that holds a session, and when it last renewed its lease."""

    run: str
    started_at: datetime
    renewed_at: datetime


_MESSAGE_COLUMNS: Final = "message_id, number, text, author, created_at, state, run, reason"


class RunState:
    """The run service's records in one control store, which its owner closes: the
    store itself, or the one a document store opened."""

    def __init__(
        self,
        store: ControlStore | DocumentStore,
        *,
        clock: Clock = SYSTEM_CLOCK,
        lease_seconds: float = LEASE_SECONDS,
    ) -> None:
        self.control = store if isinstance(store, ControlStore) else store.control
        self.clock, self.lease_seconds = clock, lease_seconds

    def _now(self) -> datetime:
        return self.clock.now().astimezone(UTC)

    # --- Messages --------------------------------------------------------------------

    def post(
        self, project: str, session: str, message_id: str, text: str, *, author: str
    ) -> ChatMessage:
        """Queue a message for the session's run. The same ID with the same text is the
        message already written; with other text, or for another session, a conflict."""
        if not text.strip() or len(text.encode()) > MAX_MESSAGE_BYTES:
            raise ControlError(
                INVALID_REQUEST, f"a message is 1 to {MAX_MESSAGE_BYTES} bytes, not blank"
            )
        now = self._now().isoformat()
        with self.control.write() as tx:
            db = tx.sql
            found = db.execute(
                f"SELECT {_MESSAGE_COLUMNS}, project_id, session_id FROM session_messages "  # noqa: S608
                "WHERE message_id = ?",
                (message_id,),
            ).fetchone()
            if found is not None:
                if tuple(found[-2:]) != (project, session) or found[2] != text:
                    raise ControlError(
                        CONFLICT, f"message {message_id!r} was written with other content"
                    )
                return _message(found)
            _session(db, project, session)
            try:
                cursor = db.execute(
                    "INSERT INTO session_messages (message_id, project_id, session_id, text, "
                    "author, created_at, state, updated_at) VALUES (?, ?, ?, ?, ?, ?, 'queued', ?)",
                    (message_id, project, session, text, author, now, now),
                )
            except sqlite3.IntegrityError as error:
                raise ControlError(INVALID_REQUEST, f"the message is refused: {error}") from None
            # Writing again is resuming: the session's held messages go with this one.
            db.execute(
                "UPDATE session_messages SET state = 'queued', updated_at = ? "
                "WHERE project_id = ? AND session_id = ? AND state = 'held'",
                (now, project, session),
            )
            return ChatMessage(
                message_id=message_id,
                number=cast(int, cursor.lastrowid),
                text=text,
                author=author,
                created_at=datetime.fromisoformat(now),
                state="queued",
            )

    def messages(
        self,
        project: str,
        session: str,
        *,
        after: int = 0,
        limit: int = PAGE,
        state: MessageState | None = None,
    ) -> list[ChatMessage]:
        """The session's messages after the ``number`` ``after``, oldest first."""
        limit = max(1, min(limit, PAGE))
        query = (
            f"SELECT {_MESSAGE_COLUMNS} FROM session_messages "  # noqa: S608
            "WHERE project_id = ? AND session_id = ? AND number > ?"
        )
        parameters: tuple[object, ...] = (project, session, after)
        if state is not None:
            query += " AND state = ?"
            parameters += (state,)
        with self.control.read() as tx:
            rows = tx.sql.execute(query + " ORDER BY number LIMIT ?", (*parameters, limit))
            return [_message(row) for row in rows.fetchall()]

    def offer(self, project: str, session: str, ids: list[str], run: str) -> None:
        """Mark queued messages offered to ``run``, which must hold the session."""
        with self.control.write() as tx:
            _holding(tx.sql, project, session, run)
            tx.sql.executemany(
                "UPDATE session_messages SET offered = ? WHERE project_id = ? "
                "AND session_id = ? AND message_id = ? AND state = 'queued'",
                [(run, project, session, message_id) for message_id in ids],
            )

    def deliver(
        self, project: str, session: str, ids: list[str], run: str, *, holding: bool = False
    ) -> None:
        """Mark queued messages delivered by ``run``, once its record is saved (RS-9).
        With ``holding``, only while ``run`` holds the session: a run that lost its lease
        delivers nothing, and its messages go to the next run."""
        now = self._now().isoformat()
        with self.control.write() as tx:
            if holding:
                _holding(tx.sql, project, session, run)
            tx.sql.executemany(
                "UPDATE session_messages SET state = 'delivered', run = ?, updated_at = ? "
                "WHERE project_id = ? AND session_id = ? AND message_id = ? "
                "AND state IN ('queued', 'held')",
                [(run, now, project, session, message_id) for message_id in ids],
            )

    # --- The run lease ---------------------------------------------------------------

    def acquire(self, project: str, session: str, run: str, holder: str) -> None:
        """Take the session's run lease for ``run``; ``busy`` while another run holds it.
        An expired lease is taken over."""
        now = self._now()
        with self.control.write() as tx:
            db = tx.sql
            _session(db, project, session)
            found = db.execute(
                "SELECT run, renewed_at FROM run_leases WHERE project_id = ? AND session_id = ?",
                (project, session),
            ).fetchone()
            if found is not None and now.timestamp() - found[1] < self.lease_seconds:
                raise ControlError(
                    BUSY,
                    f"session {session!r} has a run in progress: {found[0]}",
                    {"run": found[0]},
                )
            db.execute(
                "INSERT OR REPLACE INTO run_leases VALUES (?, ?, ?, ?, ?, ?)",
                (project, session, run, holder, now.isoformat(), now.timestamp()),
            )

    def renew(self, project: str, session: str, holder: str) -> None:
        """Renew the lease ``holder`` holds; ``conflict`` once it is lost."""
        with self.control.write() as tx:
            cursor = tx.sql.execute(
                "UPDATE run_leases SET renewed_at = ? "
                "WHERE project_id = ? AND session_id = ? AND holder = ?",
                (self._now().timestamp(), project, session, holder),
            )
            if cursor.rowcount != 1:
                raise ControlError(CONFLICT, f"the run lease of session {session!r} was lost")

    def release(self, project: str, session: str, holder: str) -> None:
        with self.control.write() as tx:
            tx.sql.execute(
                "DELETE FROM run_leases WHERE project_id = ? AND session_id = ? AND holder = ?",
                (project, session, holder),
            )

    def lease(self, project: str, session: str) -> RunLease | None:
        """The session's live lease, or ``None`` when no run holds it."""
        with self.control.read() as tx:
            found = tx.sql.execute(
                "SELECT run, started_at, renewed_at FROM run_leases "
                "WHERE project_id = ? AND session_id = ?",
                (project, session),
            ).fetchone()
        if found is None or self._now().timestamp() - found[2] >= self.lease_seconds:
            return None
        return RunLease(
            run=found[0],
            started_at=datetime.fromisoformat(found[1]),
            renewed_at=datetime.fromtimestamp(found[2], UTC),
        )

    # --- Run events ------------------------------------------------------------------

    def append(
        self,
        project: str,
        session: str,
        run: str,
        kind: EventKind,
        data: dict[str, JsonValue],
        *,
        holding: bool = False,
    ) -> int:
        """Append an event to the run's; its number. With ``holding``, only while
        ``run`` holds the session."""
        text = json.dumps(data, ensure_ascii=False, allow_nan=False)
        if len(text.encode()) > MAX_EVENT_BYTES:
            raise ControlError(INVALID_REQUEST, f"an event is at most {MAX_EVENT_BYTES} bytes")
        with self.control.write() as tx:
            db = tx.sql
            if holding:
                _holding(db, project, session, run)
            (last,) = db.execute(
                "SELECT coalesce(max(seq), 0) FROM run_events "
                "WHERE project_id = ? AND session_id = ? AND run = ?",
                (project, session, run),
            ).fetchone()
            db.execute(
                "INSERT INTO run_events VALUES (?, ?, ?, ?, ?, ?, ?)",
                (project, session, run, last + 1, self._now().isoformat(), kind, text),
            )
            return last + 1

    def known(self, project: str, session: str, run: str) -> bool:
        """Whether the run wrote an event or holds the session."""
        with self.control.read() as tx:
            found = tx.sql.execute(
                "SELECT 1 FROM run_events WHERE project_id = ? AND session_id = ? AND run = ? "
                "UNION ALL SELECT 1 FROM run_leases WHERE project_id = ? AND session_id = ? "
                "AND run = ? LIMIT 1",
                (project, session, run, project, session, run),
            ).fetchone()
        return found is not None

    def ended(self, project: str, session: str, run: str) -> bool:
        """Whether the run's ``stopped`` event is written."""
        with self.control.read() as tx:
            found = tx.sql.execute(
                "SELECT 1 FROM run_events WHERE project_id = ? AND session_id = ? AND run = ? "
                "AND kind = 'stopped'",
                (project, session, run),
            ).fetchone()
        return found is not None

    def events(
        self, project: str, session: str, run: str, *, after: int = 0, limit: int = PAGE
    ) -> list[RunEvent]:
        """The run's events after ``after``, in order."""
        with self.control.read() as tx:
            rows = tx.sql.execute(
                "SELECT seq, at, kind, data FROM run_events WHERE project_id = ? "
                "AND session_id = ? AND run = ? AND seq > ? ORDER BY seq LIMIT ?",
                (project, session, run, after, max(1, min(limit, PAGE))),
            ).fetchall()
        return [
            RunEvent(
                seq=seq,
                at=datetime.fromisoformat(at),
                kind=kind,
                data=cast(dict[str, JsonValue], json.loads(data)),
            )
            for seq, at, kind, data in rows
        ]


def _session(db: sqlite3.Connection, project: str, session: str) -> None:
    found = db.execute(
        "SELECT 1 FROM sessions WHERE project_id = ? AND session_id = ?", (project, session)
    ).fetchone()
    if found is None:
        raise ControlError(NOT_FOUND, f"project {project!r} has no session {session!r}")


def _holding(db: sqlite3.Connection, project: str, session: str, run: str) -> None:
    found = db.execute(
        "SELECT 1 FROM run_leases WHERE project_id = ? AND session_id = ? AND run = ?",
        (project, session, run),
    ).fetchone()
    if found is None:
        raise ControlError(CONFLICT, f"run {run} no longer holds session {session!r}")


def _message(row: tuple[object, ...]) -> ChatMessage:
    message_id, number, text, author, created_at, state, run, reason = row[:8]
    return ChatMessage.model_validate(
        {
            "message_id": message_id,
            "number": number,
            "text": text,
            "author": author,
            "created_at": created_at,
            "state": state,
            "run": run,
            "reason": reason,
        }
    )
