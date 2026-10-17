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
from collections.abc import Sequence
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


def _response_usage(project: str, since: datetime | None) -> tuple[str, tuple[object, ...]]:
    """Answered-call usage by role: journal first, then each unjournaled ordinal
    from the saved record, or its event if no record exists. Suppression ignores
    the time window so an old journal entry cannot reappear through a fallback.
    Journal timestamps are per response; legacy fallbacks retain run-start time."""
    first = "" if since is None else run_name(since, "")
    after = 0.0 if since is None else since.timestamp()
    return (
        "SELECT j.role, j.usage AS u FROM archive_model_usage j "
        "WHERE j.project=? AND j.observed>=? "
        "UNION ALL SELECT CASE c.kind WHEN 'reviewer' THEN 'reviewer' "
        "WHEN 'watcher' THEN 'watcher' WHEN 'embedding' THEN 'embeddings' "
        "WHEN 'skills' THEN 'metadata' ELSE w.role END, "
        "json_object('cost',json_extract(o.detail,'$.resolution.cost')) "
        "FROM research_observations o JOIN research_work w USING(project,session,work) "
        "JOIN research_model_calls c ON c.project=o.project AND c.session=o.session "
        "AND c.run=json_extract(o.detail,'$.resolution.run') "
        "AND c.identity=json_extract(o.detail,'$.resolution.identity') "
        "WHERE o.project=? AND o.kind='resolution:model_usage' AND c.created>=? "
        "AND (c.usage IS NULL OR c.kind IN ('main','delegated')) "
        "UNION ALL SELECT coalesce(json_extract(r.record,'$.role'),'main'), usage.value "
        "FROM runs r, json_each(r.record,'$.usage') usage "
        "WHERE r.project_id=? AND r.name>=? AND NOT EXISTS ("
        "SELECT 1 FROM archive_model_usage j WHERE j.project=r.project_id "
        "AND j.session=r.session_id AND j.run=r.name AND j.ordinal=CAST(usage.key AS INTEGER)+1) "
        "UNION ALL SELECT coalesce((SELECT json_extract(s.data,'$.role') "
        "FROM run_events s WHERE s.project_id=e.project_id AND s.session_id=e.session_id "
        "AND s.run=e.run AND s.kind='started' LIMIT 1),'main'), json_extract(e.data,'$.usage') "
        "FROM run_events e WHERE e.project_id=? AND e.run>=? AND e.kind='assistant' "
        "AND NOT EXISTS (SELECT 1 FROM runs r WHERE r.project_id=e.project_id "
        "AND r.session_id=e.session_id AND r.name=e.run) "
        "AND NOT EXISTS (SELECT 1 FROM archive_model_usage j WHERE j.project=e.project_id "
        "AND j.session=e.session_id AND j.run=e.run AND j.ordinal=json_extract(e.data,'$.turn'))",
        (project, after, project, after, project, first, project, first),
    )


def _run_usage(project: str, since: datetime | None) -> tuple[str, tuple[object, ...]]:
    """Canonical observations, exactly linked legacy copies, then unlinked history."""
    from .continuity.materializations import Table, fallback
    from .continuity.reservations import UNRESERVED

    query, values = _response_usage(project, since)
    after = 0.0 if since is None else since.timestamp()
    first = "" if since is None else run_name(since, "")
    query += (
        " UNION ALL SELECT CASE c.kind WHEN 'reviewer' THEN 'reviewer' "
        "WHEN 'watcher' THEN 'watcher' WHEN 'skills' THEN 'metadata' "
        "WHEN 'embedding' THEN 'embeddings' ELSE w.role END,c.usage "
        "FROM research_model_calls c JOIN research_attempts a USING(project,session,run) "
        "JOIN research_work w USING(project,session,work) WHERE c.project=? AND c.created>=? "
        "AND c.kind NOT IN ('main','delegated') AND c.usage IS NOT NULL"
    )
    values += (project, after)
    sources: tuple[tuple[str, Table, str], ...] = (
        ("reviewer", "review_calls", "created"),
        ("watcher", "job_watcher_calls", "requested_at"),
        ("metadata", "skill_metadata_calls", "created"),
        ("embeddings", "skill_embedding_calls", "created"),
    )
    for role, table, made in sources:
        usage = (
            "json_object('cost',cost,'cost_source',cost_source,"
            "'input_tokens',input_tokens,'output_tokens',output_tokens)"
            if table != "skill_embedding_calls"
            else "json_object('cost',cost,'cost_source','gateway')"
        )
        query += (
            f" UNION ALL SELECT '{role}',{usage} FROM {table} legacy "  # noqa: S608 - fixed tables
            f"WHERE project=? AND {made}>=? AND {fallback(table, 'legacy')}"
        )
        values += (project, after)

    # Fold usage is copied into collection, saved-summary and event forms. Preserve
    # their old precedence. A linked intent suppresses its copy only after observation
    # or reservation. An older copy without an intent ID needs an exact USER link.
    def fold_copy(
        value: str,
        project_col: str,
        session: str,
        run: str,
        source: str,
        position: str,
        event_seq: str = "0",
    ) -> str:
        return (
            "NOT EXISTS (SELECT 1 FROM research_model_calls m "  # noqa: S608 - fixed columns
            f"WHERE m.project={project_col} AND m.session={session} AND m.run={run} "
            f"AND m.identity=json_extract({value},'$.intent_id') AND m.kind='fold' "
            f"AND (m.usage IS NOT NULL OR (json_extract({value},'$.cost') IS NULL "
            f"AND NOT ({UNRESERVED})))) "
            "AND NOT EXISTS (SELECT 1 FROM research_observations link "
            "JOIN research_model_calls m ON m.project=link.project "
            "AND m.session=link.session "
            "AND m.run=json_extract(link.detail,'$.resolution.run') "
            "AND m.identity=json_extract(link.detail,'$.resolution.identity') "
            "JOIN research_attempts a ON a.project=m.project AND a.session=m.session "
            "AND a.work=link.work AND a.run=m.run "
            f"WHERE link.project={project_col} AND link.session={session} "
            "AND link.kind='resolution:fold_estimate_link' "
            f"AND m.run={run} AND m.kind='fold' AND m.usage IS NULL "
            f"AND json_extract(link.detail,'$.resolution.source')='{source}' "
            f"AND json_extract(link.detail,'$.resolution.position')={position} "
            f"AND coalesce(json_extract(link.detail,'$.resolution.event_seq'),0)={event_seq} "
            f"AND json_extract(link.detail,'$.resolution.estimate_json')={value} "
            f"AND NOT ({UNRESERVED}))"
        )

    event_fold = fold_copy(
        "u.value", "e.project_id", "e.session_id", "e.run", "event", "u.key", "e.seq"
    )
    query += (
        " UNION ALL SELECT CASE WHEN EXISTS (SELECT 1 FROM archive_messages m "  # noqa: S608
        "WHERE m.project=c.project AND m.session=c.session AND m.run=c.run "
        "AND m.parent IS NOT NULL) THEN 'delegated' ELSE 'main' END,c.item "
        "FROM run_collections c WHERE c.project=? AND c.run>=? AND c.collection='fold_calls' "
        f"AND {fold_copy('c.item', 'c.project', 'c.session', 'c.run', 'collection', 'c.position')} "
        "UNION ALL SELECT coalesce(json_extract(r.record,'$.role'),'main'),u.value "
        "FROM runs r,json_each(r.record,'$.summaries.folds.calls') u "
        "WHERE r.project_id=? AND r.name>=? AND NOT EXISTS (SELECT 1 FROM run_collections h "
        "WHERE h.project=r.project_id AND h.session=r.session_id AND h.run=r.name "
        "AND h.collection='fold_calls') "
        f"AND {fold_copy('u.value', 'r.project_id', 'r.session_id', 'r.name', 'summary', 'u.key')} "
        "UNION ALL SELECT 'main',u.value FROM run_events e,json_each(e.data,'$.calls') u "
        "WHERE e.project_id=? AND e.run>=? AND e.kind='fold' "
        "AND NOT EXISTS (SELECT 1 FROM runs r WHERE r.project_id=e.project_id "
        "AND r.session_id=e.session_id AND r.name=e.run) "
        "AND NOT EXISTS (SELECT 1 FROM run_collections h WHERE h.project=e.project_id "
        "AND h.session=e.session_id AND h.run=e.run AND h.collection='fold_calls') "
        f"AND {event_fold}"
    )
    return query, (*values, project, first, project, first, project, first)


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

    def replay_message(
        self, project: str, session: str, message_id: str, text: str
    ) -> ChatMessage | Question | None:
        """Return an identical already-accepted send before repeating attachment delivery."""
        with self.control.read() as tx:
            return _found(tx.sql, project, session, message_id, text) or answered_by(
                tx.sql, project, session, message_id, text
            )

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

    def resume(
        self,
        project: str,
        session: str,
        *,
        reason: str | None = None,
        clear_reason: bool = False,
    ) -> int:
        """Queue the session's held messages again, or only those held for ``reason``;
        how many. Clear the old reason only when its prerequisite has changed.

        This does not authorize continuing durable work: its original offered inputs
        remain subject to reconciliation and an explicit research-tree Resume.
        """
        now = self._now().isoformat()
        with self.control.write() as tx:
            _session(tx.sql, project, session)
            return tx.sql.execute(
                "UPDATE session_messages SET state = 'queued', updated_at = ?, "
                "reason = CASE WHEN ? THEN NULL ELSE reason END "
                "WHERE project_id = ? AND session_id = ? AND state = 'held' "
                "AND (? IS NULL OR reason = ?)",
                (now, clear_reason, project, session, reason, reason),
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

    def reconcile_inputs(self, project: str, session: str) -> None:
        """Canonical response receipts win over a crash before chat delivery."""
        with self.control.write() as tx:
            tx.sql.execute(
                "UPDATE session_messages AS m SET state='delivered',updated_at=?,run=("
                "SELECT r.run FROM response_receipts r WHERE r.project=m.project_id "
                "AND r.session=m.session_id AND r.source='messages' AND r.notice=m.message_id "
                "ORDER BY r.run LIMIT 1) WHERE m.project_id=? AND m.session_id=? "
                "AND m.state IN ('queued','held') AND EXISTS (SELECT 1 FROM response_receipts r "
                "WHERE r.project=m.project_id AND r.session=m.session_id "
                "AND r.source='messages' AND r.notice=m.message_id)",
                (self._now().isoformat(), project, session),
            )
            tx.sql.execute(
                "UPDATE session_messages AS m SET state='held', "
                "reason=coalesce(m.reason,'interrupted'),updated_at=? "
                "WHERE m.project_id=? AND m.session_id=? AND m.state='queued' AND EXISTS "
                "(SELECT 1 FROM research_attempts a JOIN research_work w "
                "USING(project,session,work) WHERE a.project=m.project_id "
                "AND a.session=m.session_id AND a.run=m.offered "
                "AND w.state NOT IN ('completed','stopped') AND NOT EXISTS "
                "(SELECT 1 FROM research_resumes q WHERE q.project=w.project "
                "AND q.session=w.session AND q.root=w.root "
                "AND q.status IN ('queued','running'))) AND NOT EXISTS "
                "(SELECT 1 FROM run_leases l WHERE l.project_id=m.project_id "
                "AND l.session_id=m.session_id AND l.run=m.offered AND l.renewed_at>?)",
                (
                    self._now().isoformat(),
                    project,
                    session,
                    self._now().timestamp() - self.lease_seconds,
                ),
            )

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
        """Persist an explicit Stop for this run and its subtree before signalling it.
        The immutable delegation ancestry carries this intent across process loss.
        Asking again changes nothing; an already saved outcome remains authoritative."""
        with self.control.write() as tx:
            _session(tx.sql, project, session)
            tx.sql.execute(
                "INSERT OR IGNORE INTO run_cancels VALUES (?, ?, ?, ?)",
                (project, session, run, self._now().isoformat()),
            )

    def cancelled(self, project: str, session: str, run: str) -> bool:
        with self.control.read() as tx:
            return cancel_requested(tx.sql, project, session, run)

    def cancelled_runs(self, project: str, session: str, runs: Sequence[str]) -> set[str]:
        """Resolve a live sibling set's durable Stop ancestry in one read snapshot."""
        with self.control.read() as tx:
            return {run for run in runs if cancel_requested(tx.sql, project, session, run)}

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
        return self.cost_gate(project, now)[1]

    def cost_gate(
        self, project: str, now: datetime, *, require_limit: bool = False
    ) -> tuple[float | None, bool]:
        """Read cap presence and its observed/reserved spend in one snapshot.

        An explicitly renewed child requires a cap even if an operator removed the
        project's general limit after issuing the grant.
        """
        from .continuity.reservations import RESERVATIONS

        since = now - WINDOW
        runs, values = _run_usage(project, since)
        with self.control.read() as tx:
            db = tx.sql
            row = db.execute(
                "SELECT daily_cost FROM project_cost_limits WHERE project_id=?", (project,)
            ).fetchone()
            if row is None:
                return None, require_limit
            limit = float(row[0])
            spent, unknown = db.execute(
                "SELECT coalesce(sum(json_extract(u,'$.cost')),0), "  # noqa: S608 - fixed SQL
                "count(*) FILTER (WHERE json_extract(u,'$.cost') IS NULL "
                "AND coalesce(json_extract(u,'$.input_tokens'),0)+"
                "coalesce(json_extract(u,'$.output_tokens'),0)>0) "
                f"FROM ({runs})",
                values,
            ).fetchone()
            reserved = db.execute(
                f"SELECT coalesce(sum(cost),0) FROM ({RESERVATIONS}) "  # noqa: S608 - fixed SQL
                "WHERE project=? AND created>=?",
                (project, since.timestamp()),
            ).fetchone()[0]
        if float(spent) + float(reserved) >= limit:
            return limit, True
        if unknown:
            _log.warning(
                "project %s keeps a daily cost limit, and %s of its model calls in the "
                "last 24 hours have no known cost: it counts as at its limit",
                project,
                unknown,
            )
            return limit, True
        return limit, False

    def unpriced_calls(self, project: str, since: datetime) -> int:
        """How many of the project's model calls since ``since`` used tokens and have no
        cost, neither reported by the gateway nor priced from its model information.
        Run responses use journal observation time, falling back to run-start time
        for legacy records/events. Reviewer, watcher and import calls use their own
        timestamps. Each run response is counted once, as in ``model_costs``."""
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
        return int(found)

    def model_info_cost(self, project: str, since: datetime | None = None) -> float | None:
        """The part of ``model_cost`` priced from the gateway's model information rather
        than reported by it (``cost_source``, lab 43, B2); ``None`` when no call's was."""
        runs, values = _run_usage(project, since)
        with self.control.read() as tx:
            rows = tx.sql.execute(
                "SELECT sum(json_extract(u, '$.cost')), count(json_extract(u, '$.cost')) "  # noqa: S608
                f"FROM ({runs}) WHERE json_extract(u, '$.cost_source') = 'model_info'",
                values,
            ).fetchall()
        priced = [float(total) for total, count in rows if count]
        return sum(priced) if priced else None

    def model_cost(self, project: str, since: datetime | None = None) -> float | None:
        """What the project's model calls cost, as the gateway reported it (RS-6a): every
        role's, as ``model_costs`` gives them. ``None`` when the gateway reported no
        call's cost."""
        costs = self.model_costs(project, since)
        return sum(costs.values()) if costs else None

    def model_reservations(
        self, project: str, since: datetime | None = None
    ) -> dict[CostRole, float]:
        """Conservative upper-bound cost by role, separately from observed spend."""
        from .continuity.reservations import RESERVATIONS

        with self.control.read() as tx:
            rows = tx.sql.execute(
                f"SELECT role,sum(cost) FROM ({RESERVATIONS}) WHERE project=? AND created>=? "  # noqa: S608
                "GROUP BY role",
                (project, 0.0 if since is None else since.timestamp()),
            ).fetchall()
        totals = {str(role): float(cost) for role, cost in rows}
        return {role: totals[role] for role in COST_ROLES if role in totals}

    def model_costs(self, project: str, since: datetime | None = None) -> dict[CostRole, float]:
        """Known model cost by role in ``COST_ROLES`` order, after ``since`` if given.
        Main/delegated responses prefer the usage journal and observation time;
        unjournaled ordinals use saved records or events and their run-start time.
        Fold enrichment retains its run-start cutoff and record/event precedence.
        Reviewer, watcher and skills embedding calls use their own timestamps.
        A role without any priced call is left out."""
        runs, values = _run_usage(project, since)
        with self.control.read() as tx:
            rows = tx.sql.execute(
                "SELECT role,sum(json_extract(u,'$.cost')),count(json_extract(u,'$.cost')) "  # noqa: S608
                f"FROM ({runs}) GROUP BY role",
                values,
            ).fetchall()
        priced: dict[str, float] = {}
        for role, total, count in rows:
            if count:
                priced[str(role)] = priced.get(str(role), 0.0) + float(total)
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
    # Legacy held input can accompany fresh chat. Durable unfinished work is
    # re-held by reconciliation unless a research-tree Resume authorized it.
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


def cancel_requested(db: sqlite3.Connection, project: str, session: str, run: str) -> bool:
    """Whether this run or an ancestor has a durable explicit Stop, in this transaction.
    UNION also bounds traversal of malformed cyclic legacy ancestry."""
    return (
        db.execute(
            "WITH RECURSIVE ancestry(run) AS (SELECT ? UNION SELECT d.parent "
            "FROM delegations AS d JOIN ancestry AS a ON d.run = a.run "
            "WHERE d.project_id = ? AND d.session_id = ?) "
            "SELECT 1 FROM run_cancels WHERE project_id = ? AND session_id = ? "
            "AND run IN (SELECT run FROM ancestry) LIMIT 1",
            (run, project, session, project, session),
        ).fetchone()
        is not None
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
        else {
            "stopped": "cancelled"
            if cancel_requested(db, project, session, run)
            else "interrupted",
            "failure": None,
            "final_text": None,
            "record": None,
        }
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
