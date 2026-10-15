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
from datetime import UTC, datetime, timedelta
from typing import Final, Literal, cast

from pydantic import Field, JsonValue

from sandbox_science.contracts import SYSTEM_CLOCK, Clock, Identifier
from sandbox_science.contracts.model import Contract

from .control import ControlError, ControlStore
from .control.errors import BUSY, CONFLICT, INVALID_REQUEST, NOT_FOUND
from .documents import DocumentStore
from .projects import ModelUsage, run_name

LEASE_SECONDS: Final = 60.0
"""How long a run's lease outlives its last renewal (RS-5)."""
SERVICE_SECONDS: Final = 30.0
"""How old the run service's heartbeat may be while it runs (RS-3)."""
MAIN_TOKENS: Final = 2_000_000
WINDOW: Final = timedelta(hours=24)
"""The model budget's window (RS-6a)."""
"""A project's default model budget: MAIN tokens over a rolling 24 hours (RS-6a)."""
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
    """Why it was refused, or held."""
    offered: str | None = None
    """The run that last offered it, which saw it whether or not it answered: what a
    cancelled or interrupted run left held, the GUI shows from it."""


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


class DeadLease(Contract):
    """A lease whose holder stopped renewing it: its run was interrupted (RS-12)."""

    project: str
    session: str
    run: str
    holder: str


_MESSAGE_COLUMNS: Final = (
    "message_id, number, text, author, created_at, state, run, reason, offered"
)


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

    def _set_state(
        self, project: str, session: str, ids: list[str], state: MessageState, reason: str | None
    ) -> None:
        now = self._now().isoformat()
        with self.control.write() as tx:
            tx.sql.executemany(
                "UPDATE session_messages SET state = ?, reason = ?, updated_at = ? "
                "WHERE project_id = ? AND session_id = ? AND message_id = ? AND state = 'queued'",
                [(state, reason, now, project, session, message_id) for message_id in ids],
            )

    def refuse(self, project: str, session: str, ids: list[str], reason: str) -> None:
        """Refuse queued messages, such as for a spent budget (RS-6)."""
        self._set_state(project, session, ids, "refused", reason)

    def hold(self, project: str, session: str, ids: list[str], reason: str | None = None) -> None:
        """Hold queued messages until the user resumes or writes again (RS-12), such as
        those an interrupted run saw, or a failing session's."""
        self._set_state(project, session, ids, "held", reason)

    def resume(self, project: str, session: str) -> int:
        """Queue the session's held messages again; how many."""
        now = self._now().isoformat()
        with self.control.write() as tx:
            _session(tx.sql, project, session)
            return tx.sql.execute(
                "UPDATE session_messages SET state = 'queued', updated_at = ? "
                "WHERE project_id = ? AND session_id = ? AND state = 'held'",
                (now, project, session),
            ).rowcount

    def waiting(self) -> list[tuple[str, str]]:
        """Every session with queued messages, as (project, session)."""
        with self.control.read() as tx:
            rows = tx.sql.execute(
                "SELECT DISTINCT project_id, session_id FROM session_messages "
                "WHERE state = 'queued' ORDER BY project_id, session_id"
            ).fetchall()
        return [(project, session) for project, session in rows]

    def delivered_texts(self, project: str, session: str, run: str) -> list[str]:
        """The messages ``run`` delivered, in the order they were written."""
        with self.control.read() as tx:
            rows = tx.sql.execute(
                "SELECT text FROM session_messages WHERE project_id = ? AND session_id = ? "
                "AND run = ? ORDER BY number",
                (project, session, run),
            ).fetchall()
        return [text for (text,) in rows]

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

    def reap(self, dead: DeadLease) -> bool:
        """Remove a dead lease, if it is still dead; whether it was."""
        with self.control.write() as tx:
            cursor = tx.sql.execute(
                "DELETE FROM run_leases WHERE project_id = ? AND session_id = ? AND holder = ? "
                "AND run = ? AND renewed_at <= ?",
                (
                    dead.project,
                    dead.session,
                    dead.holder,
                    dead.run,
                    self._now().timestamp() - self.lease_seconds,
                ),
            )
            return cursor.rowcount == 1

    def dead(self) -> list[DeadLease]:
        """Leases whose holders stopped renewing them."""
        with self.control.read() as tx:
            rows = tx.sql.execute(
                "SELECT project_id, session_id, run, holder FROM run_leases WHERE renewed_at <= ?",
                (self._now().timestamp() - self.lease_seconds,),
            ).fetchall()
        return [DeadLease(project=p, session=s, run=r, holder=h) for p, s, r, h in rows]

    # --- Cancels, the service's heartbeat and the budget -------------------------------

    def cancel(self, project: str, session: str, run: str) -> None:
        """Ask for the run to be cancelled (RS-11); asking again changes nothing."""
        with self.control.write() as tx:
            _session(tx.sql, project, session)
            tx.sql.execute(
                "INSERT OR IGNORE INTO run_cancels VALUES (?, ?, ?, ?)",
                (project, session, run, self._now().isoformat()),
            )

    def cancelled(self, project: str, session: str, run: str) -> bool:
        with self.control.read() as tx:
            found = tx.sql.execute(
                "SELECT 1 FROM run_cancels WHERE project_id = ? AND session_id = ? AND run = ?",
                (project, session, run),
            ).fetchone()
        return found is not None

    def beat(self, holder: str) -> None:
        """The run service is running (RS-3)."""
        with self.control.write() as tx:
            tx.sql.execute(
                "INSERT OR REPLACE INTO service_heartbeat VALUES (1, ?, ?)",
                (holder, self._now().timestamp()),
            )

    def service_running(self) -> bool:
        with self.control.read() as tx:
            found = tx.sql.execute("SELECT renewed_at FROM service_heartbeat").fetchone()
        return found is not None and self._now().timestamp() - found[0] < SERVICE_SECONDS

    def budget(self, project: str) -> int:
        """The project's MAIN tokens over a rolling 24 hours (RS-6a)."""
        with self.control.read() as tx:
            found = tx.sql.execute(
                "SELECT main_tokens FROM project_budgets WHERE project_id = ?", (project,)
            ).fetchone()
        return MAIN_TOKENS if found is None else int(found[0])

    def set_budget(self, project: str, tokens: int) -> None:
        with self.control.write() as tx:
            if (
                tx.sql.execute("SELECT 1 FROM projects WHERE project_id = ?", (project,)).fetchone()
                is None
            ):
                raise ControlError(NOT_FOUND, f"no project {project!r}")
            try:
                tx.sql.execute(
                    "INSERT OR REPLACE INTO project_budgets VALUES (?, ?, ?)",
                    (project, tokens, self._now().isoformat()),
                )
            except sqlite3.IntegrityError:
                raise ControlError(INVALID_REQUEST, "a budget is 0 to 10^12 tokens") from None

    def over_budget(self, project: str, now: datetime) -> bool:
        """Whether the project has spent its MAIN budget over the window to ``now``."""
        return self.spent(project, now - WINDOW) >= self.budget(project)

    def spent(self, project: str, since: datetime) -> int:
        """MAIN's tokens in the project's runs that started at ``since`` or later: saved
        records' ``usage``, and interrupted runs' ``assistant`` events. A call without
        token counts spends a quarter of its characters."""
        cost = (
            "CASE WHEN json_extract(u, '$.input_tokens') IS NOT NULL "
            "AND json_extract(u, '$.output_tokens') IS NOT NULL "
            "THEN json_extract(u, '$.input_tokens') + json_extract(u, '$.output_tokens') "
            "ELSE coalesce(json_extract(u, '$.characters'), 0) / 4 END"
        )
        first = run_name(since, "")
        with self.control.read() as tx:
            (saved,) = tx.sql.execute(
                f"SELECT coalesce(sum({cost}), 0) FROM (SELECT usage.value AS u "  # noqa: S608
                "FROM runs, json_each(runs.record, '$.usage') AS usage "
                "WHERE runs.project_id = ? AND runs.name >= ?)",
                (project, first),
            ).fetchone()
            (unsaved,) = tx.sql.execute(
                f"SELECT coalesce(sum({cost}), 0) FROM (SELECT json_extract(e.data, '$.usage') "  # noqa: S608
                "AS u FROM run_events AS e WHERE e.project_id = ? AND e.run >= ? "
                "AND e.kind = 'assistant' AND NOT EXISTS (SELECT 1 FROM runs WHERE "
                "runs.project_id = e.project_id AND runs.session_id = e.session_id "
                "AND runs.name = e.run))",
                (project, first),
            ).fetchone()
        return int(saved) + int(unsaved)

    def offered_to(self, project: str, session: str, run: str) -> list[str]:
        """The queued messages ``run`` was offered last: what it saw and did not answer."""
        with self.control.read() as tx:
            rows = tx.sql.execute(
                "SELECT message_id FROM session_messages WHERE project_id = ? "
                "AND session_id = ? AND offered = ? AND state = 'queued' ORDER BY number",
                (project, session, run),
            ).fetchall()
        return [message_id for (message_id,) in rows]

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
        holder: str | None = None,
    ) -> int:
        """Append an event to the run's; its number. With ``holding``, only while
        ``holder`` holds the session for it: ``run`` itself, or a delegated run's root."""
        text = json.dumps(data, ensure_ascii=False, allow_nan=False)
        if len(text.encode()) > MAX_EVENT_BYTES:
            raise ControlError(INVALID_REQUEST, f"an event is at most {MAX_EVENT_BYTES} bytes")
        with self.control.write() as tx:
            db = tx.sql
            if holding:
                _holding(db, project, session, holder or run)
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
    message_id, number, text, author, created_at, state, run, reason, offered = row[:9]
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
            "offered": offered,
        }
    )


def close_events(
    db: sqlite3.Connection, project: str, session: str, run: str, now: datetime
) -> bool:
    """In a write transaction, write the ``stopped`` event of a run whose process ended
    without it, as a delegated run's when its root died (PD-40): from the run's saved
    record, or else ``interrupted``. A run that wrote no event, or its ``stopped``
    already, is left as it is. Whether an event was written."""
    (last, ended) = db.execute(
        "SELECT coalesce(max(seq), 0), coalesce(max(kind = 'stopped'), 0) "
        "FROM run_events WHERE project_id = ? AND session_id = ? AND run = ?",
        (project, session, run),
    ).fetchone()
    if not last or ended:
        return False
    saved = db.execute(
        "SELECT json_extract(record, '$.stopped'), json_extract(record, '$.failure'), "
        "json_extract(record, '$.final_text') FROM runs WHERE project_id = ? "
        "AND session_id = ? AND name = ?",
        (project, session, run),
    ).fetchone()
    data: dict[str, JsonValue] = (
        {
            "stopped": saved[0],
            "failure": saved[1],
            "final_text": head_and_tail(saved[2]),
            "record": run,
        }
        if saved is not None
        else {"stopped": "interrupted", "failure": None, "final_text": None, "record": None}
    )
    db.execute(
        "INSERT INTO run_events VALUES (?, ?, ?, ?, ?, 'stopped', ?)",
        (
            project,
            session,
            run,
            last + 1,
            now.isoformat(),
            json.dumps(data, ensure_ascii=False, allow_nan=False),
        ),
    )
    return True


def head_and_tail(text: str | None, keep: int = 4000) -> str | None:
    """``text``'s head and tail, short enough for an event; the record keeps it whole."""
    if text is None or len(text) <= 2 * keep:
        return text
    return f"{text[:keep]}\n[…]\n{text[-keep:]}"


def cost(usage: ModelUsage) -> int:
    """One call's spending, as ``RunState.spent`` counts it (RS-6a): its tokens, or a
    quarter of its characters when the gateway gave no token counts."""
    if usage.input_tokens is not None and usage.output_tokens is not None:
        return usage.input_tokens + usage.output_tokens
    return (usage.characters or 0) // 4
