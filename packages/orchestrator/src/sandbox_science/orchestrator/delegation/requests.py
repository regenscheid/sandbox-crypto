# SPDX-License-Identifier: MIT
"""A delegated run's effect requests that wait for the user (D99; delegated agents spec
DA-8 to DA-10).

When the MCP proxy holds a child's connector call for the user's answer, the child's run
records the request here: the proxy's request number, the run and delegation that asked,
the server and tool, and the child's own words at the call. The brief its parent gave is
read from the delegation. The workspace's card in the session's chat shows them beside
the proxy's request, and the parent is told through the agent mailbox.

The parent may withdraw a request, never approve it. A withdrawal only refuses: the
workspace then denies that one call at the proxy and no longer shows its card, so the
child's call ends as a denied one does. Only the user approves (DA-8, PD-16). Records
are kept: a request's attribution, words and times never change once written, and a
withdrawal or end is written once.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import UTC
from typing import TYPE_CHECKING, Final, cast

from sandbox_science.contracts import SYSTEM_CLOCK, Clock, ExecutionContext
from sandbox_science.contracts.model import Contract
from sandbox_science.orchestrator.aio import threaded
from sandbox_science.orchestrator.control import ControlError, ControlStore
from sandbox_science.orchestrator.control.errors import CONFLICT, NOT_FOUND
from sandbox_science.orchestrator.documents import DocumentStore

if TYPE_CHECKING:
    from sandbox_science.orchestrator.agent_messages import AgentMessageStore
    from sandbox_science.orchestrator.chat import RunChat
    from sandbox_science.orchestrator.control.migrations import Schema

_log = logging.getLogger(__name__)

MAX_WORDS: Final = 2000
"""The child's own words a request keeps, in characters: the head of its step's text."""
MAX_BRIEF: Final = 2000
"""The parent's brief a request shows, in characters: the head of its task or note."""
MAX_LISTED: Final = 256

_COLUMNS: Final = (
    "r.request, r.delegation_id, r.run, d.parent, d.kind, d.brief, r.server, r.tool, "
    "r.words, r.recorded_at, r.ended_at, r.withdrawn_by, r.withdrawn_at"
)


class ChildRequest(Contract):
    """A child's call that waits, or waited, for the user's answer."""

    request: int
    """The MCP proxy's permission request number."""
    delegation_id: str
    run: str
    """The child's run, which made the call."""
    parent: str
    """The run that delegated it, which may withdraw the request."""
    kind: str
    """The delegation's kind: ``task`` or ``check``."""
    brief: str
    """What the parent asked the child to do, bounded: the parent's words, not the user's."""
    server: str
    tool: str
    words: str
    """The child's own text at the step that made the call, bounded: agent text."""
    recorded_at: str
    ended_at: str | None = None
    withdrawn_by: str | None = None
    withdrawn_at: str | None = None


def install(schema: Schema) -> None:
    """The control store's table of children's requests (its migration step)."""
    for statement in (
        """CREATE TABLE child_requests (
    project_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    request INTEGER NOT NULL CHECK (request >= 0),
    delegation_id TEXT NOT NULL,
    run TEXT NOT NULL CHECK (length(CAST(run AS BLOB)) BETWEEN 1 AND 256),
    server TEXT NOT NULL CHECK (length(CAST(server AS BLOB)) BETWEEN 1 AND 256),
    tool TEXT NOT NULL CHECK (length(CAST(tool AS BLOB)) BETWEEN 1 AND 256),
    words TEXT NOT NULL CHECK (length(CAST(words AS BLOB)) <= 8192),
    recorded_at TEXT NOT NULL CHECK (length(recorded_at) BETWEEN 1 AND 64),
    ended_at TEXT CHECK (ended_at IS NULL OR length(ended_at) BETWEEN 1 AND 64),
    withdrawn_by TEXT
        CHECK (withdrawn_by IS NULL OR length(CAST(withdrawn_by AS BLOB)) BETWEEN 1 AND 256),
    withdrawn_at TEXT CHECK (withdrawn_at IS NULL OR length(withdrawn_at) BETWEEN 1 AND 64),
    CHECK ((withdrawn_by IS NULL) = (withdrawn_at IS NULL)),
    PRIMARY KEY (project_id, session_id, request),
    FOREIGN KEY (project_id, session_id) REFERENCES sessions (project_id, session_id),
    FOREIGN KEY (delegation_id) REFERENCES delegations (delegation_id)
) STRICT""",
        """CREATE TRIGGER child_requests_kept BEFORE UPDATE ON child_requests
WHEN NEW.project_id IS NOT OLD.project_id OR NEW.session_id IS NOT OLD.session_id
OR NEW.request IS NOT OLD.request OR NEW.delegation_id IS NOT OLD.delegation_id
OR NEW.run IS NOT OLD.run OR NEW.server IS NOT OLD.server OR NEW.tool IS NOT OLD.tool
OR NEW.words IS NOT OLD.words OR NEW.recorded_at IS NOT OLD.recorded_at
OR (OLD.ended_at IS NOT NULL AND NEW.ended_at IS NOT OLD.ended_at)
OR (OLD.withdrawn_by IS NOT NULL AND (NEW.withdrawn_by IS NOT OLD.withdrawn_by
    OR NEW.withdrawn_at IS NOT OLD.withdrawn_at))
BEGIN SELECT RAISE(ABORT, 'a child request is kept'); END""",
        """CREATE TRIGGER child_requests_no_delete BEFORE DELETE ON child_requests
BEGIN SELECT RAISE(ABORT, 'child requests are kept'); END""",
    ):
        schema.execute(statement)


def brief_text(brief: object) -> str:
    """The head of what a brief asks: a task's task, or a check's target and note."""
    if not isinstance(brief, dict):
        return ""
    found = cast(dict[str, object], brief)
    task = found.get("task")
    if isinstance(task, str):
        text = task
    else:
        target = found.get("check")
        note = found.get("note")
        named = json.dumps(target, sort_keys=True) if target is not None else ""
        text = f"Check {named}" + (f": {note}" if isinstance(note, str) and note else "")
    return text[:MAX_BRIEF]


def _row(row: tuple[object, ...]) -> ChildRequest:
    brief: object = json.loads(str(row[5]))
    return ChildRequest(
        request=int(str(row[0])),
        delegation_id=str(row[1]),
        run=str(row[2]),
        parent=str(row[3]),
        kind=str(row[4]),
        brief=brief_text(brief),
        server=str(row[6]),
        tool=str(row[7]),
        words=str(row[8]),
        recorded_at=str(row[9]),
        ended_at=None if row[10] is None else str(row[10]),
        withdrawn_by=None if row[11] is None else str(row[11]),
        withdrawn_at=None if row[12] is None else str(row[12]),
    )


class ChildRequests:
    """The children's requests of every session, in the control store."""

    def __init__(self, control: ControlStore | DocumentStore, *, clock: Clock = SYSTEM_CLOCK):
        self.control = control.control if isinstance(control, DocumentStore) else control
        self.clock = clock

    def _now(self) -> str:
        return self.clock.now().astimezone(UTC).isoformat()

    @staticmethod
    def _get(
        db: sqlite3.Connection, project: str, session: str, request: int
    ) -> ChildRequest | None:
        row = db.execute(
            f"SELECT {_COLUMNS} FROM child_requests r JOIN delegations d "  # noqa: S608 - fixed columns
            "ON d.delegation_id = r.delegation_id "
            "WHERE r.project_id=? AND r.session_id=? AND r.request=?",
            (project, session, request),
        ).fetchone()
        return None if row is None else _row(row)

    def record(
        self,
        project: str,
        session: str,
        *,
        run: str,
        delegation: str,
        request: int,
        server: str,
        tool: str,
        words: str,
    ) -> ChildRequest:
        """The child ``run`` of ``delegation`` waits on the proxy's ``request``. The
        first run to wait on a request keeps its attribution; the same again is a no-op."""
        with self.control.write() as tx:
            db = tx.sql
            owner = db.execute(
                "SELECT run FROM delegations WHERE project_id=? AND session_id=? "
                "AND delegation_id=?",
                (project, session, delegation),
            ).fetchone()
            if owner is None or str(owner[0]) != run:
                raise ControlError(NOT_FOUND, f"no delegation {delegation!r} of this run")
            db.execute(
                "INSERT INTO child_requests (project_id, session_id, request, delegation_id, "
                "run, server, tool, words, recorded_at) VALUES (?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT (project_id, session_id, request) DO NOTHING",
                (
                    project,
                    session,
                    request,
                    delegation,
                    run,
                    server[:256],
                    tool[:256],
                    words[:MAX_WORDS],
                    self._now(),
                ),
            )
            found = self._get(db, project, session, request)
        if found is None:  # pragma: no cover - written in the same transaction
            raise ControlError(NOT_FOUND, f"no request {request}")
        return found

    def ended(self, project: str, session: str, request: int, run: str) -> None:
        """The run's wait on ``request`` ended: answered, denied, timed out or cut short."""
        with self.control.write() as tx:
            tx.sql.execute(
                "UPDATE child_requests SET ended_at=? WHERE project_id=? AND session_id=? "
                "AND request=? AND run=? AND ended_at IS NULL",
                (self._now(), project, session, request, run),
            )

    def withdraw(
        self, project: str, session: str, *, parent: str, delegation: str, request: int
    ) -> ChildRequest:
        """The parent ``parent`` withdraws its direct child's waiting request. Only the
        run that delegated the child may; a request that no longer waits is refused, and
        one withdrawn already is the same withdrawal."""
        with self.control.write() as tx:
            db = tx.sql
            found = self._get(db, project, session, request)
            if found is None or found.delegation_id != delegation:
                raise ControlError(
                    NOT_FOUND, f"no request {request} of delegation {delegation!r} waits"
                )
            if found.parent != parent:
                raise ControlError(
                    CONFLICT,
                    "not_permitted_for_role: only the run that delegated the child may "
                    "withdraw its request",
                )
            if found.withdrawn_by is not None:
                return found
            if found.ended_at is not None:
                raise ControlError(CONFLICT, f"request {request} no longer waits")
            db.execute(
                "UPDATE child_requests SET withdrawn_by=?, withdrawn_at=? WHERE project_id=? "
                "AND session_id=? AND request=?",
                (parent, self._now(), project, session, request),
            )
            withdrawn = self._get(db, project, session, request)
        assert withdrawn is not None  # noqa: S101 - updated in the same transaction
        return withdrawn

    def in_session(self, project: str, session: str) -> dict[int, ChildRequest]:
        """The session's requests that still wait, withdrawn ones included, by number."""
        with self.control.read() as tx:
            rows = tx.sql.execute(
                f"SELECT {_COLUMNS} FROM child_requests r JOIN delegations d "  # noqa: S608 - fixed columns
                "ON d.delegation_id = r.delegation_id WHERE r.project_id=? AND "
                "r.session_id=? AND r.ended_at IS NULL ORDER BY r.request DESC LIMIT ?",
                (project, session, MAX_LISTED),
            ).fetchall()
        return {item.request: item for item in map(_row, rows)}

    def withdrawn(self, project: str, requests: list[int]) -> set[int]:
        """Those of the project's ``requests`` that a parent withdrew."""
        if not requests:
            return set()
        with self.control.read() as tx:
            rows = tx.sql.execute(
                "SELECT request FROM child_requests WHERE project_id=? AND "
                "withdrawn_by IS NOT NULL AND request IN (SELECT value FROM json_each(?))",
                (project, json.dumps(requests[:MAX_LISTED])),
            ).fetchall()
        return {int(str(row[0])) for row in rows}


NOTICE: Final = (
    "Platform notice: my call to {tool} of the MCP server {server} waits for the user's "
    "answer (permission request {request}). The user decides it in the session's chat, "
    "where the card shows your brief and my words. You may withdraw the request with "
    "withdraw_child_request, which refuses the call; you cannot approve it."
)


class ChildApproval:
    """A delegated run's connector waits (``Connectors``' ``on_approval``): the chat's
    waiting event, the attributed request, and a notice to the parent's mailbox."""

    def __init__(
        self,
        chat: RunChat,
        requests: ChildRequests,
        mailbox: AgentMessageStore,
        context: ExecutionContext,
        *,
        delegation: str,
    ) -> None:
        self.chat, self.requests, self.mailbox = chat, requests, mailbox
        self.context, self.delegation = context, delegation
        self.waiting: int | None = None

    async def __call__(self, wait: dict[str, object] | None) -> None:
        await self.chat.approval(wait)
        project, session = self.context.project_id, self.context.session_id
        if wait is None:
            if self.waiting is not None:
                waited, self.waiting = self.waiting, None
                await threaded(self.requests.ended, project, session, waited, self.chat.run)
            return
        request = wait.get("permission_request")
        if not isinstance(request, int):
            return
        self.waiting = request
        record = await threaded(
            self.requests.record,
            project,
            session,
            run=self.chat.run,
            delegation=self.delegation,
            request=request,
            server=str(wait.get("server", "")),
            tool=str(wait.get("tool", "")),
            words=self.chat.said,
        )
        body = NOTICE.format(tool=record.tool, server=record.server, request=request)
        # The notice is the parent's to read; a parent that cannot be told (the tree lost
        # its session) does not cost the child its wait, which the card still shows.
        try:
            await threaded(
                self.mailbox.send_parent,
                project,
                session,
                self.chat.run,
                self.delegation,
                body,
                f"child-request-{self.delegation}-{request}",
            )
        except ControlError as error:
            _log.warning("the parent of %s was not told of request %s: %s",
                         self.chat.run, request, error.message)  # fmt: skip
