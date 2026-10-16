# SPDX-License-Identifier: MIT
"""A session's chat messages, its run lease and its runs' events, in the control store.

The run service's records (docs/specs/run-service.md):

- **Messages** (RS-4). The user writes a message to a session; it waits ``queued`` until
  a run delivers it, and is marked ``delivered`` with that run only after the run's
  record is saved (RS-9). A message ID makes a retried write safe. A run marks the
  messages it offers, so what it saw is known without its events; a new message queues
  the session's held messages again (RS-12). While the run that holds the session waits
  for the user's answer to a question, the user's own message is that answer instead
  (``say``; PD-23).
- **The run lease** (RS-5). One run of a session at a time, from the command line or the
  service. A lease whose holder stops renewing it for ``lease_seconds`` has expired.
- **Run events** (RS-13, RS-14). What a run did, appended as it happens, numbered from 1
  within the run, and read by cursor.

Each method is one transaction of the control store and may run in a thread. Times
that processes compare (a lease's renewal) are wall-clock seconds from ``clock``.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import UTC, datetime, timedelta
from typing import Final, Literal, cast

from pydantic import Field, JsonValue

from sandbox_science.contracts import SYSTEM_CLOCK, Clock, Identifier
from sandbox_science.contracts.model import Contract

from .control import ControlError, ControlStore
from .control.errors import BUSY, CONFLICT, INVALID_REQUEST, NOT_FOUND
from .documents import DocumentStore
from .documents.pins import pin as pin_references
from .documents.pins import text_references
from .projects import ModelUsage, ProjectStore, run_name
from .questions import Question, answer_with, answered_by, by_message, waiting

LEASE_SECONDS: Final = 60.0
"""How long a run's lease outlives its last renewal (RS-5)."""
SERVICE_SECONDS: Final = 30.0
"""How old the run service's heartbeat may be while it runs (RS-3)."""
_log = logging.getLogger(__name__)

TOKEN_CALLS: Final = (
    ("review_calls", "created"),
    ("job_watcher_calls", "requested_at"),
    ("skill_metadata_calls", "created"),
)
"""The tables of the other roles' model calls that keep their tokens, with when each
call was made: the reviewer's, the job watcher's and the import pass's. Each keeps where
its cost came from (``cost_source``, version 53)."""


def _run_usage(project: str, since: datetime | None) -> tuple[str, tuple[object, ...]]:
    """A query of each usage entry (``u``) of the project's runs that started at ``since``
    or later: saved records' and the ``assistant`` events' of runs without a record."""
    first = "" if since is None else run_name(since, "")
    return (
        "SELECT usage.value AS u FROM runs, json_each(runs.record, '$.usage') AS usage "
        "WHERE runs.project_id = ? AND runs.name >= ? "
        "UNION ALL SELECT json_extract(e.data, '$.usage') FROM run_events AS e "
        "WHERE e.project_id = ? AND e.run >= ? AND e.kind = 'assistant' "
        "AND NOT EXISTS (SELECT 1 FROM runs WHERE runs.project_id = e.project_id "
        "AND runs.session_id = e.session_id AND runs.name = e.run)",
        (project, first, project, first),
    )


WINDOW: Final = timedelta(hours=24)
"""The daily cost limit's window (RS-6a)."""
MAX_COST: Final = 1_000_000_000
"""The largest daily cost limit, in the gateway's currency."""
CostRole = Literal["main", "delegated", "reviewer", "watcher", "embeddings", "metadata"]
"""Whose model calls a cost is for: the main agent's runs, delegated runs, the reviewer,
the job watcher (lab 40, B5), the skills' embeddings (docs/specs/skills.md, SK-29) and the
import's model pass over a pack's catalog, when the import names the project (SK-30)."""
COST_ROLES: Final[tuple[CostRole, ...]] = (
    "main",
    "delegated",
    "reviewer",
    "watcher",
    "embeddings",
    "metadata",
)
MAX_MESSAGE_BYTES: Final = 65536
MAX_EVENT_BYTES: Final = 65536
PAGE: Final = 200
"""The most messages or events one read returns."""

MessageState = Literal["queued", "delivered", "refused", "held"]
EventKind = Literal[
    "started", "user", "model", "assistant", "tool", "notice", "waiting", "stopped", "fold"
]


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
    pins: list[dict[str, JsonValue]] = Field(default_factory=list[dict[str, JsonValue]])
    """Its references to recorded files, pinned when it was written (figures, FG-7)."""


class RunEvent(Contract):
    """One thing a run did, for display (RS-14)."""

    seq: int = Field(ge=1)
    at: datetime
    kind: EventKind
    data: dict[str, JsonValue]
    pins: list[dict[str, JsonValue]] = Field(default_factory=list[dict[str, JsonValue]])
    """An answer's references to recorded files, pinned when it was written (FG-7)."""


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
    "message_id, number, text, author, created_at, state, run, reason, offered, "
    "(SELECT pins FROM chat_pins c WHERE c.project_id = session_messages.project_id AND "
    "c.session_id = session_messages.session_id AND c.source = 'message' "
    "AND c.key = session_messages.message_id)"
)


class RunState:
    """The run service's records in one control store, which its owner closes: the
    store itself, or the one a document store opened."""

    def __init__(
        self,
        store: ControlStore | DocumentStore | ProjectStore,
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
        message already written; with other text, or for another session, a conflict. It
        never answers a question to the user: the user's own messages from the chat are
        written with ``say``, which nothing else writes with."""
        _check_text(text)
        now = self._now().isoformat()
        with self.control.write() as tx:
            db = tx.sql
            found = _found(db, project, session, message_id, text)
            if found is not None:
                return found
            _session(db, project, session)
            if by_message(db, message_id):
                raise ControlError(CONFLICT, f"message {message_id!r} answered a question")
            return _queue(db, project, session, message_id, text, author, now)

    def say(
        self,
        project: str,
        session: str,
        message_id: str,
        text: str,
        *,
        author: str,
        answers: str | None,
    ) -> ChatMessage | Question:
        """Write the user's own message from the chat. While the run that holds the
        session waits for the user's answer, the message answers that run's oldest open
        question in the user's own words and is not queued, and the question is returned,
        answered (PD-23; a coordinator decision, reversible: the message could instead
        wait for the run's next turn). Otherwise it is queued, as ``post`` queues it.

        ``answers`` is the question the user was shown the message would answer, or
        ``None`` when they were shown none. When it is not the question the message would
        answer now, as when that question was answered or closed meanwhile, or another
        opened, nothing is written: a conflict whose details name the question it would
        answer now (``question_changed``). The same ID with the same text is the message,
        or the answer, already written."""
        _check_text(text)
        now = self._now().isoformat()
        with self.control.write() as tx:
            db = tx.sql
            found = _found(db, project, session, message_id, text)
            if found is not None:
                return found
            earlier = answered_by(db, project, session, message_id, text)
            if earlier is not None:
                return earlier
            _session(db, project, session)
            question = waiting(db, project, session, live_after=self.live_after())
            current = None if question is None else question.question_id
            if current != answers:
                raise ControlError(
                    CONFLICT,
                    "the question this message answers has changed: read the chat again "
                    + (f"(it now answers {current})" if current else "(no question waits)"),
                    {"reason": "question_changed", "question": current},
                )
            if question is not None:
                return answer_with(db, question, message_id, text, now)
            return _queue(db, project, session, message_id, text, author, now)

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

    def resume(self, project: str, session: str, *, reason: str | None = None) -> int:
        """Queue the session's held messages again, or only those held for ``reason``;
        how many."""
        now = self._now().isoformat()
        with self.control.write() as tx:
            _session(tx.sql, project, session)
            return tx.sql.execute(
                "UPDATE session_messages SET state = 'queued', updated_at = ? "
                "WHERE project_id = ? AND session_id = ? AND state = 'held' "
                "AND (? IS NULL OR reason = ?)",
                (now, project, session, reason, reason),
            ).rowcount

    def held_for(self, reason: str) -> list[tuple[str, str]]:
        """Every session with messages held for ``reason``, as (project, session)."""
        with self.control.read() as tx:
            rows = tx.sql.execute(
                "SELECT DISTINCT project_id, session_id FROM session_messages "
                "WHERE state = 'held' AND reason = ? ORDER BY project_id, session_id",
                (reason,),
            ).fetchall()
        return [(project, session) for project, session in rows]

    def waiting(self) -> list[tuple[str, str]]:
        """Every session with queued messages, as (project, session)."""
        with self.control.read() as tx:
            rows = tx.sql.execute(
                "SELECT DISTINCT project_id, session_id FROM session_messages "
                "WHERE state = 'queued' ORDER BY project_id, session_id"
            ).fetchall()
        return [(project, session) for project, session in rows]

    def delivered_texts(self, project: str, session: str, run: str) -> list[tuple[float, str]]:
        """The messages ``run`` delivered, in the order they were written: each one's
        time, in seconds since the epoch, and its text."""
        with self.control.read() as tx:
            rows = tx.sql.execute(
                "SELECT created_at, text FROM session_messages WHERE project_id = ? "
                "AND session_id = ? AND run = ? ORDER BY number",
                (project, session, run),
            ).fetchall()
        return [(datetime.fromisoformat(at).timestamp(), text) for at, text in rows]

    # --- The run lease ---------------------------------------------------------------

    def live_after(self) -> float:
        """The time, in wall-clock seconds, after which a lease's last renewal keeps it
        live (RS-5)."""
        return self._now().timestamp() - self.lease_seconds

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

    def leased(self, project: str, session: str) -> bool:
        """Whether the session has a lease, live or expired. An expired one is ended
        before the session runs again (RS-12): ``acquire`` would take it over, and its run
        would never end."""
        with self.control.read() as tx:
            found = tx.sql.execute(
                "SELECT 1 FROM run_leases WHERE project_id = ? AND session_id = ?",
                (project, session),
            ).fetchone()
        return found is not None

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

    def cost_limit(self, project: str) -> float | None:
        """The project's daily cost limit, in the gateway's currency (RS-6a); ``None``,
        as by default, for none."""
        with self.control.read() as tx:
            found = tx.sql.execute(
                "SELECT daily_cost FROM project_cost_limits WHERE project_id = ?", (project,)
            ).fetchone()
        return None if found is None else float(found[0])

    def set_cost_limit(self, project: str, limit: float | None) -> None:
        """Set the project's daily cost limit, or remove it with ``None``."""
        if limit is not None and not 0 <= limit <= MAX_COST:
            raise ControlError(INVALID_REQUEST, f"a daily cost limit is 0 to {MAX_COST}")
        with self.control.write() as tx:
            if (
                tx.sql.execute("SELECT 1 FROM projects WHERE project_id = ?", (project,)).fetchone()
                is None
            ):
                raise ControlError(NOT_FOUND, f"no project {project!r}")
            if limit is None:
                tx.sql.execute("DELETE FROM project_cost_limits WHERE project_id = ?", (project,))
                return
            tx.sql.execute(
                "INSERT OR REPLACE INTO project_cost_limits VALUES (?, ?, ?)",
                (project, float(limit), self._now().isoformat()),
            )

    def over_cost_limit(self, project: str, now: datetime) -> bool:
        """Whether the project has a daily cost limit and its calls in the window to
        ``now`` reached it, at their costs; or whether, with a limit, a call in the
        window used tokens and has no known cost (``unpriced_calls``), so that what was
        spent cannot be known (lab 43, B2): the limit counts as reached until that call
        leaves the window."""
        limit = self.cost_limit(project)
        if limit is None:
            return False
        since = now - WINDOW
        spent = self.model_cost(project, since)
        if spent is not None and spent >= limit:
            return True
        if unknown := self.unpriced_calls(project, since):
            _log.warning(
                "project %s keeps a daily cost limit, and %s of its model calls in the "
                "last 24 hours have no known cost: it counts as at its limit",
                project,
                unknown,
            )
            return True
        return False

    def unpriced_calls(self, project: str, since: datetime) -> int:
        """How many of the project's model calls since ``since`` used tokens and have no
        cost, neither reported by the gateway nor priced from its model information: its
        runs' that started then or later, from saved records' ``usage`` and the
        ``assistant`` events of runs without a record, as ``model_costs`` reads them; and
        the reviewer's, the job watcher's and the import pass's made then or later."""
        unpriced = (
            "json_extract(u, '$.cost') IS NULL AND coalesce(json_extract(u, "
            "'$.input_tokens'), 0) + coalesce(json_extract(u, '$.output_tokens'), 0) > 0"
        )
        runs, values = _run_usage(project, since)
        with self.control.read() as tx:
            (found,) = tx.sql.execute(
                f"SELECT count(*) FROM ({runs}) WHERE {unpriced}",  # noqa: S608
                values,
            ).fetchone()
            for table, made in TOKEN_CALLS:
                (more,) = tx.sql.execute(
                    f"SELECT count(*) FROM {table} WHERE project = ? AND {made} >= ? "  # noqa: S608
                    "AND cost IS NULL AND coalesce(input_tokens, 0) + "
                    "coalesce(output_tokens, 0) > 0",
                    (project, since.timestamp()),
                ).fetchone()
                found += more
        return int(found)

    def model_info_cost(self, project: str, since: datetime | None = None) -> float | None:
        """The part of ``model_cost`` priced from the gateway's model information rather
        than reported by it (``cost_source``, lab 43, B2); ``None`` when no call's was."""
        runs, values = _run_usage(project, since)
        after = 0.0 if since is None else since.timestamp()
        with self.control.read() as tx:
            rows = tx.sql.execute(
                "SELECT sum(json_extract(u, '$.cost')), count(json_extract(u, '$.cost')) "  # noqa: S608
                f"FROM ({runs}) WHERE json_extract(u, '$.cost_source') = 'model_info'",
                values,
            ).fetchall()
            for table, made in TOKEN_CALLS:
                rows += tx.sql.execute(
                    f"SELECT sum(cost), count(cost) FROM {table} WHERE project = ? "  # noqa: S608
                    f"AND {made} >= ? AND cost_source = 'model_info'",
                    (project, after),
                ).fetchall()
        priced = [float(total) for total, count in rows if count]
        return sum(priced) if priced else None

    def model_cost(self, project: str, since: datetime | None = None) -> float | None:
        """What the project's model calls cost, as the gateway reported it (RS-6a): every
        role's, as ``model_costs`` gives them. ``None`` when the gateway reported no
        call's cost."""
        costs = self.model_costs(project, since)
        return sum(costs.values()) if costs else None

    def model_costs(self, project: str, since: datetime | None = None) -> dict[CostRole, float]:
        """What the project's model calls cost by role, as the gateway reported it, in
        ``COST_ROLES``' order: the calls of its runs that started at ``since`` or later,
        main and delegated, from saved records' ``usage`` and the ``assistant`` events of
        runs without a record, and their folds' enrichment calls, from the records'
        ``summaries.folds.calls`` and the ``fold`` events of runs without one; and the
        reviewer's, the job watcher's and the skills'
        embedding calls made at ``since`` or later (lab 40, B5). Every call without
        ``since``. A role none of whose calls the gateway priced is left out."""
        first = "" if since is None else run_name(since, "")
        after = 0.0 if since is None else since.timestamp()
        cost = "json_extract(u, '$.cost')"
        with self.control.read() as tx:
            rows = tx.sql.execute(
                f"SELECT role, sum({cost}), count({cost}) FROM ("  # noqa: S608
                "SELECT coalesce(json_extract(runs.record, '$.role'), 'main') AS role, "
                "usage.value AS u FROM runs, json_each(runs.record, '$.usage') AS usage "
                "WHERE runs.project_id = ? AND runs.name >= ? "
                "UNION ALL SELECT coalesce((SELECT json_extract(s.data, '$.role') "
                "FROM run_events AS s WHERE s.project_id = e.project_id "
                "AND s.session_id = e.session_id AND s.run = e.run AND s.kind = 'started' "
                "LIMIT 1), 'main'), json_extract(e.data, '$.usage') "
                "FROM run_events AS e WHERE e.project_id = ? AND e.run >= ? "
                "AND e.kind = 'assistant' AND NOT EXISTS (SELECT 1 FROM runs WHERE "
                "runs.project_id = e.project_id AND runs.session_id = e.session_id "
                "AND runs.name = e.run) "
                # A fold's enrichment calls: in its run's record, or its fold events.
                "UNION ALL SELECT coalesce(json_extract(runs.record, '$.role'), 'main'), "
                "c.value FROM runs, json_each(runs.record, '$.summaries.folds.calls') AS c "
                "WHERE runs.project_id = ? AND runs.name >= ? "
                "UNION ALL SELECT 'main', c.value FROM run_events AS e, "
                "json_each(e.data, '$.calls') AS c WHERE e.project_id = ? AND e.run >= ? "
                "AND e.kind = 'fold' AND NOT EXISTS (SELECT 1 FROM runs WHERE "
                "runs.project_id = e.project_id AND runs.session_id = e.session_id "
                "AND runs.name = e.run)) GROUP BY role",
                (project, first, project, first, project, first, project, first),
            ).fetchall()
            for role, table, made in (
                ("reviewer", "review_calls", "created"),
                ("watcher", "job_watcher_calls", "requested_at"),
                ("embeddings", "skill_embedding_calls", "created"),
                ("metadata", "skill_metadata_calls", "created"),
            ):
                rows += tx.sql.execute(
                    f"SELECT ?, sum(cost), count(cost) FROM {table} "  # noqa: S608
                    f"WHERE project = ? AND {made} >= ?",
                    (role, project, after),
                ).fetchall()
        priced = {str(role): float(total) for role, total, count in rows if count}
        return {role: priced[role] for role in COST_ROLES if role in priced}

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
            answer = data.get("text")
            if kind == "assistant" and isinstance(answer, str):
                _pin_chat(db, project, session, "event", f"{run}:{last + 1}", answer)
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
                "SELECT seq, at, kind, data, (SELECT pins FROM chat_pins c "
                "WHERE c.project_id = e.project_id AND c.session_id = e.session_id "
                "AND c.source = 'event' AND c.key = e.run || ':' || e.seq) "
                "FROM run_events e WHERE project_id = ? "
                "AND session_id = ? AND run = ? AND seq > ? ORDER BY seq LIMIT ?",
                (project, session, run, after, max(1, min(limit, PAGE))),
            ).fetchall()
        return [
            RunEvent(
                seq=seq,
                at=datetime.fromisoformat(at),
                kind=kind,
                data=cast(dict[str, JsonValue], json.loads(data)),
                pins=json.loads(pins) if pins else [],
            )
            for seq, at, kind, data, pins in rows
        ]


def _check_text(text: str) -> None:
    if not text.strip() or len(text.encode()) > MAX_MESSAGE_BYTES:
        raise ControlError(
            INVALID_REQUEST, f"a message is 1 to {MAX_MESSAGE_BYTES} bytes, not blank"
        )


def _found(
    db: sqlite3.Connection, project: str, session: str, message_id: str, text: str
) -> ChatMessage | None:
    """The message written under ``message_id`` already; with other text, or for
    another session, a conflict."""
    found = db.execute(
        f"SELECT {_MESSAGE_COLUMNS}, project_id, session_id FROM session_messages "  # noqa: S608
        "WHERE message_id = ?",
        (message_id,),
    ).fetchone()
    if found is None:
        return None
    if tuple(found[-2:]) != (project, session) or found[2] != text:
        raise ControlError(CONFLICT, f"message {message_id!r} was written with other content")
    return _message(found)


def _queue(
    db: sqlite3.Connection,
    project: str,
    session: str,
    message_id: str,
    text: str,
    author: str,
    now: str,
) -> ChatMessage:
    try:
        cursor = db.execute(
            "INSERT INTO session_messages (message_id, project_id, session_id, text, "
            "author, created_at, state, updated_at) VALUES (?, ?, ?, ?, ?, ?, 'queued', ?)",
            (message_id, project, session, text, author, now, now),
        )
    except sqlite3.IntegrityError as error:
        raise ControlError(INVALID_REQUEST, f"the message is refused: {error}") from None
    pinned = _pin_chat(db, project, session, "message", message_id, text)
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
        pins=pinned,
    )


def _pin_chat(
    db: sqlite3.Connection, project: str, session: str, source: str, key: str, text: str
) -> list[dict[str, JsonValue]]:
    """Pin a chat text's references to recorded files, in its write (figures, FG-7):
    a message's or an answer's pins never move (FG-8)."""
    pins, _ = pin_references(db, project, session, text_references(text, "text"))
    if pins:
        db.execute(
            "INSERT INTO chat_pins (project_id, session_id, source, key, pins) "
            "VALUES (?, ?, ?, ?, ?)",
            (project, session, source, key, json.dumps(pins, sort_keys=True)),
        )
    return cast(list[dict[str, JsonValue]], pins)


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
    message_id, number, text, author, created_at, state, run, reason, offered, pins = row[:10]
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
            "pins": json.loads(cast(str, pins)) if pins else [],
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


def call_tokens(usage: ModelUsage) -> int:
    """One call's tokens, as a delegated run's limit counts them (PD-38), or a quarter of
    its characters when the gateway gave no token counts."""
    if usage.input_tokens is not None and usage.output_tokens is not None:
        return usage.input_tokens + usage.output_tokens
    return (usage.characters or 0) // 4
