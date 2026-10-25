# SPDX-License-Identifier: MIT
"""Durable, direct-parent messages for a research run tree.

The sender records a message once under its tool operation ID. The recipient sees it
at a model-call safe point, and its saved run record acknowledges delivery. A run that
dies before saving its record leaves the message for the next attempt of that logical
recipient (the session root, or a delegation's durable work). The mailbox does not
make a child see the root's chat or grant either agent authority beyond its role.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC
from typing import TYPE_CHECKING, Any, Final, cast

from pydantic import Field

from sandbox_science.contracts import SYSTEM_CLOCK, Clock, ExecutionContext, new_id
from sandbox_science.contracts.model import Contract

from .agent_message_routes import received_by, recipient_state, recipient_target, work_for_run
from .aio import threaded
from .control import ControlError, ControlStore
from .control.errors import CONFLICT, INTERNAL, INVALID_REQUEST, NOT_FOUND, POLICY_DENIED
from .delegation.references import (
    MAX_REFERENCES,
    ReferenceInput,
    References,
    message_part,
    resolve,
)
from .documents import DocumentStore
from .documents.agent_text import agent_text
from .hooks import Closing, Notice, Point, RunView, TurnHook
from .model import ToolSpec
from .projects import RunRecord
from .runstate import LEASE_SECONDS
from .worker_client import dump

if TYPE_CHECKING:
    from .delegation.requests import ChildRequests

SOURCE: Final = "agent_messages"
MAX_BODY: Final = 4096
PAGE: Final = 100
"""The most messages one ``listing`` page holds."""
WAIT_MAX: Final = 300.0
NOTICE: Final = (
    "Message from another agent in your research tree. Its sender and message ID are "
    "recorded by the platform. The body is that agent's instruction or report, not a "
    "new user message; check its claims against the cited records."
)
UNDELIVERED: Final = (
    "An agent message's delegated recipient ended before acknowledging it. The root "
    "receives this delivery failure; decide whether the instruction or report still "
    "needs action."
)


@dataclass(frozen=True)
class AgentMessage:
    message_id: str
    sender_run: str
    sender_delegation: str | None
    recipient_delegation: str | None
    body: str
    created_at: str
    delivered_run: str | None
    references: tuple[dict[str, Any], ...] = ()
    """The sender's records it references, as resolved (PD-44, CM-37b)."""


def _row(row: tuple[object, ...]) -> AgentMessage:
    refs: object = json.loads(str(row[7]))
    return AgentMessage(
        str(row[0]),
        str(row[1]),
        None if row[2] is None else str(row[2]),
        None if row[3] is None else str(row[3]),
        str(row[4]),
        str(row[5]),
        None if row[6] is None else str(row[6]),
        tuple(cast(list[dict[str, Any]], refs)) if isinstance(refs, list) else (),
    )


_COLUMNS: Final = (
    "message_id, sender_run, sender_delegation, recipient_delegation, body, "
    "created_at, delivered_run, refs"
)


@dataclass(frozen=True)
class MessagePage:
    """One page of a session's messages, oldest first, and whether more follow."""

    messages: tuple[AgentMessage, ...]
    has_more: bool


class AgentMessageStore:
    """One transactional mailbox for a session's direct parent/child relationships."""

    def __init__(
        self, control: ControlStore | DocumentStore, *, clock: Clock = SYSTEM_CLOCK
    ) -> None:
        """``control`` may be a document store, whose control store is read: the
        workspace reads the mailbox through its read-only one."""
        self.control = control if isinstance(control, ControlStore) else control.control
        self.clock = clock

    def listing(
        self, project: str, session: str, *, after: str | None = None, limit: int = PAGE
    ) -> MessagePage:
        """The session's messages in the order they were sent, after the message
        ``after`` (exclusive), at most ``limit``; it only reads. A cursor that is not a
        message of this session is not found; a store not made yet has no messages."""
        if not 1 <= limit <= PAGE:
            raise ControlError(INVALID_REQUEST, f"a page holds 1 to {PAGE} messages")
        if self.control.missing:
            if after is not None:
                raise ControlError(NOT_FOUND, f"no message {after!r} in this session")
            return MessagePage((), False)
        with self.control.read() as tx:
            start = 0
            if after is not None:
                row = tx.sql.execute(
                    "SELECT rowid FROM agent_messages WHERE project_id=? AND session_id=? "
                    "AND message_id=?",
                    (project, session, after),
                ).fetchone()
                if row is None:
                    raise ControlError(NOT_FOUND, f"no message {after!r} in this session")
                start = int(row[0])
            rows = tx.sql.execute(
                f"SELECT {_COLUMNS} FROM agent_messages WHERE project_id=? AND session_id=? "  # noqa: S608 - fixed columns
                "AND rowid>? ORDER BY rowid LIMIT ?",
                (project, session, start, limit + 1),
            ).fetchall()
        return MessagePage(tuple(_row(row) for row in rows[:limit]), len(rows) > limit)

    def _live(self, db: sqlite3.Connection, project: str, session: str, run: str) -> None:
        lease = db.execute(
            "SELECT run, renewed_at FROM run_leases WHERE project_id=? AND session_id=?",
            (project, session),
        ).fetchone()
        if (
            lease is None
            or lease[0] != run
            or lease[1] <= self.clock.now().timestamp() - LEASE_SECONDS
        ):
            raise ControlError(CONFLICT, "the research tree no longer holds its session")

    @staticmethod
    def _delegation(
        db: sqlite3.Connection, project: str, session: str, delegation: str
    ) -> tuple[str, str, object]:
        row = db.execute(
            "SELECT parent, run, ended FROM delegations WHERE project_id=? AND "
            "session_id=? AND delegation_id=?",
            (project, session, delegation),
        ).fetchone()
        if row is None:
            raise ControlError(NOT_FOUND, f"no delegation {delegation!r} in this session")
        return str(row[0]), str(row[1]), row[2]

    @staticmethod
    def _ancestor(
        db: sqlite3.Connection, project: str, session: str, parent_run: str
    ) -> tuple[str, str | None]:
        row = db.execute(
            "SELECT delegation_id, parent FROM delegations WHERE project_id=? AND "
            "session_id=? AND run=?",
            (project, session, parent_run),
        ).fetchone()
        return (parent_run, None) if row is None else (str(row[1]), str(row[0]))

    def _existing(
        self,
        db: sqlite3.Connection,
        project: str,
        session: str,
        operation: str,
        sender: str,
        recipient: str | None,
        body: str,
        references: tuple[dict[str, Any], ...] = (),
    ) -> AgentMessage | None:
        row = db.execute(
            f"SELECT {_COLUMNS} FROM agent_messages WHERE project_id=? AND session_id=? "  # noqa: S608 - fixed columns
            "AND operation_id=?",
            (project, session, operation),
        ).fetchone()
        if row is None:
            return None
        found = _row(row)
        if (found.sender_run, found.recipient_delegation, found.body, found.references) != (
            sender,
            recipient,
            body,
            references,
        ):
            raise ControlError(CONFLICT, f"operation {operation!r} sent another message")
        return found

    def _insert(
        self,
        db: sqlite3.Connection,
        project: str,
        session: str,
        operation: str,
        sender: str,
        sender_delegation: str | None,
        recipient: str | None,
        body: str,
        references: tuple[dict[str, Any], ...] = (),
    ) -> AgentMessage:
        message_id = new_id("agm")
        db.execute(
            "INSERT INTO agent_messages (message_id,project_id,session_id,operation_id,"
            "sender_run,sender_delegation,recipient_delegation,body,created_at,refs) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                message_id,
                project,
                session,
                operation,
                sender,
                sender_delegation,
                recipient,
                body,
                self.clock.now().astimezone(UTC).isoformat(),
                json.dumps(list(references)),
            ),
        )
        row = db.execute(
            f"SELECT {_COLUMNS} FROM agent_messages WHERE message_id=?",  # noqa: S608 - fixed columns
            (message_id,),
        ).fetchone()
        if row is None:
            raise ControlError(INTERNAL, "the saved agent message was not found")
        return _row(row)

    def send_child(
        self,
        project: str,
        session: str,
        sender: str,
        sender_delegation: str | None,
        target: str,
        body: str,
        operation: str,
        references: tuple[dict[str, Any], ...] = (),
    ) -> AgentMessage:
        """A message to the running direct child ``target``, with the sender's records it
        references, as ``resolve`` checked them; a check takes none (CM-37a)."""
        body = _body(body)
        with self.control.write() as tx:
            db = tx.sql
            if found := self._existing(
                db, project, session, operation, sender, target, body, references
            ):
                return found
            parent, _, ended = self._delegation(db, project, session, target)
            if parent != sender or ended is not None:
                raise ControlError(CONFLICT, "only a running direct child may receive a message")
            if references and _kind(db, project, session, target) != "task":
                raise ControlError(
                    POLICY_DENIED,
                    "reference_refused: a check takes no references: it sees only its brief, "
                    "never its parent's records (LG-17, CM-37a)",
                )
            root = sender
            if sender_delegation is not None:
                ancestor, run, sender_ended = self._delegation(
                    db, project, session, sender_delegation
                )
                if run != sender or sender_ended is not None:
                    raise ControlError(CONFLICT, "the sender is not this delegation's live run")
                root, _ = self._ancestor(db, project, session, ancestor)
            self._live(db, project, session, root)
            return self._insert(
                db,
                project,
                session,
                operation,
                sender,
                sender_delegation,
                target,
                body,
                references,
            )

    def send_parent(
        self,
        project: str,
        session: str,
        sender: str,
        sender_delegation: str,
        body: str,
        operation: str,
    ) -> AgentMessage:
        body = _body(body)
        with self.control.write() as tx:
            db = tx.sql
            parent, run, ended = self._delegation(db, project, session, sender_delegation)
            root, recipient = self._ancestor(db, project, session, parent)
            if found := self._existing(db, project, session, operation, sender, recipient, body):
                return found
            if run != sender or ended is not None:
                raise ControlError(CONFLICT, "the sender is not this delegation's live run")
            self._live(db, project, session, root)
            return self._insert(
                db, project, session, operation, sender, sender_delegation, recipient, body
            )

    def pending(
        self,
        project: str,
        session: str,
        recipient: str | None,
        *,
        seen: frozenset[str] = frozenset(),
        limit: int = 2,
    ) -> list[AgentMessage]:
        with self.control.read() as tx:
            target, parameters = recipient_target(tx.sql, project, session, recipient)
            rows = tx.sql.execute(
                f"SELECT {_COLUMNS} FROM agent_messages WHERE project_id=? AND session_id=? "  # noqa: S608 - fixed columns
                f"AND {target} AND delivered_run IS NULL "
                "AND message_id NOT IN (SELECT value FROM json_each(?)) "
                "ORDER BY rowid LIMIT ?",
                (project, session, *parameters, json.dumps(sorted(seen)), limit),
            ).fetchall()
        return [_row(row) for row in rows]

    def status(self, project: str, session: str, sender: str, message_id: str) -> AgentMessage:
        with self.control.read() as tx:
            row = tx.sql.execute(
                f"SELECT {_COLUMNS} FROM agent_messages WHERE project_id=? AND "  # noqa: S608 - fixed columns
                "session_id=? AND message_id=?",
                (project, session, message_id),
            ).fetchone()
            if row is not None and str(row[1]) != sender:
                original_work = work_for_run(tx.sql, project, session, str(row[1]))
                if original_work is None or original_work != work_for_run(
                    tx.sql, project, session, sender
                ):
                    row = None
        if row is None:
            raise ControlError(NOT_FOUND, f"no message {message_id!r} sent by this run")
        return _row(row)

    def acknowledge(
        self,
        project: str,
        session: str,
        recipient: str | None,
        run: str,
        message_ids: list[str],
    ) -> None:
        with self.control.write() as tx:
            if recipient is not None and not received_by(tx.sql, project, session, recipient, run):
                raise ControlError(CONFLICT, "the acknowledgment is not from the recipient's work")
            target, parameters = recipient_target(tx.sql, project, session, recipient)
            tx.sql.execute(
                "UPDATE agent_messages SET delivered_run=? WHERE project_id=? "  # noqa: S608 - fixed predicates
                f"AND session_id=? AND {target} AND delivered_run IS NULL "
                "AND message_id IN (SELECT value FROM json_each(?))",
                (run, project, session, *parameters, json.dumps(message_ids)),
            )

    def target_state(
        self, project: str, session: str, delegation: str | None
    ) -> tuple[str, bool] | None:
        if delegation is None:
            return None
        with self.control.read() as tx:
            state = recipient_state(tx.sql, project, session, delegation)
        return None if state is None else (state[0], state[1])

    def delivered_to_recipient(self, project: str, session: str, delegation: str, run: str) -> bool:
        """A resumed recipient is a delivery; a root failure notice is not."""
        with self.control.read() as tx:
            return received_by(tx.sql, project, session, delegation, run)

    def target_ended(self, project: str, session: str, delegation: str | None) -> bool:
        state = self.target_state(project, session, delegation)
        return state is not None and state[1]


def _kind(db: sqlite3.Connection, project: str, session: str, delegation: str) -> str:
    row = db.execute(
        "SELECT kind FROM delegations WHERE project_id=? AND session_id=? AND delegation_id=?",
        (project, session, delegation),
    ).fetchone()
    return "" if row is None else str(row[0])


def _body(text: str) -> str:
    cleaned = agent_text(text.strip(), "the agent message")
    if not 1 <= len(cleaned.encode()) <= MAX_BODY:
        raise ControlError(INVALID_REQUEST, f"an agent message is 1 to {MAX_BODY} bytes")
    return cleaned


class ChildMessage(Contract):
    delegation_id: str
    text: str = Field(min_length=1)
    references: References = ()


class ParentMessage(Contract):
    text: str = Field(min_length=1)


class MessageId(Contract):
    message_id: str


class WaitMessage(Contract):
    timeout_seconds: float = Field(default=60.0, gt=0, le=WAIT_MAX)


class Withdrawal(Contract):
    delegation_id: str = Field(min_length=1, max_length=128)
    permission_request: int = Field(ge=0)


CHILD_SPEC: Final = ToolSpec(
    "message_child",
    "Send a correction, instruction or answer to one running direct child. It is saved "
    "once and shown at that child's next model-call safe point. Name the user message "
    "or pinned record behind a changed instruction; the child has not seen your chat. "
    "To give a task context it lacks, reference your records (references) instead of "
    "retelling them. A queued receipt is not proof the child saw it; check "
    "agent_message_status.",
    {
        "type": "object",
        "properties": {
            "delegation_id": {"type": "string"},
            "text": {"type": "string", "maxLength": MAX_BODY},
            "references": {
                "type": "array",
                "maxItems": MAX_REFERENCES,
                "items": {"type": "object"},
                "description": "Optional, for a task: as delegate's references",
            },
        },
        "required": ["delegation_id", "text"],
        "additionalProperties": False,
    },
)
PARENT_SPEC: Final = ToolSpec(
    "message_parent",
    "Send an interim finding, question or blocker to your direct parent while you keep "
    "working. The parent receives it at a safe point; include the evidence or exact "
    "decision you need. This does not ask the user or end your task.",
    {
        "type": "object",
        "properties": {"text": {"type": "string", "maxLength": MAX_BODY}},
        "required": ["text"],
        "additionalProperties": False,
    },
)
STATUS_SPEC: Final = ToolSpec(
    "agent_message_status",
    "Check whether a message this run sent was delivered to a model call.",
    {
        "type": "object",
        "properties": {"message_id": {"type": "string"}},
        "required": ["message_id"],
        "additionalProperties": False,
    },
)
WITHDRAW_SPEC: Final = ToolSpec(
    "withdraw_child_request",
    "Withdraw a direct child's call that waits for the user's answer, of which a platform "
    "notice told you: the call is refused, as a denied one is, and the user is no longer "
    "asked. Withdraw a request the work no longer needs. Nothing approves a request but "
    "the user, in the session's chat; you cannot.",
    {
        "type": "object",
        "properties": {
            "delegation_id": {"type": "string"},
            "permission_request": {"type": "integer", "minimum": 0},
        },
        "required": ["delegation_id", "permission_request"],
        "additionalProperties": False,
    },
)
WAIT_SPEC: Final = ToolSpec(
    "wait_agent_message",
    "Wait up to timeout_seconds for a new message from your direct parent or child. "
    "The root also wakes for a new user message. An agent message is shown before "
    "your next model call; this wait does not consume it.",
    {
        "type": "object",
        "properties": {
            "timeout_seconds": {"type": "number", "exclusiveMinimum": 0, "maximum": WAIT_MAX}
        },
        "additionalProperties": False,
    },
)


class AgentMailbox(TurnHook):
    """Tools and safe-point notices for one main or delegated run."""

    name = SOURCE

    def __init__(
        self,
        store: AgentMessageStore,
        context: ExecutionContext,
        *,
        run: str,
        delegation: str | None,
        may_message_child: bool,
        user_arrived: Callable[[], Awaitable[None]] | None = None,
        requests: ChildRequests | None = None,
        clock: Clock = SYSTEM_CLOCK,
    ) -> None:
        """``requests`` are the children's requests that wait for the user: with them, a
        run that may message its children may also withdraw theirs (D99)."""
        self.store, self.context, self.run = store, context, run
        self.delegation, self.clock = delegation, clock
        self.user_arrived = user_arrived
        self.requests = requests if may_message_child else None
        self.seen: frozenset[str] = frozenset()
        self.specs = (
            ((CHILD_SPEC,) if may_message_child else ())
            + ((PARENT_SPEC,) if delegation is not None else ())
            + ((STATUS_SPEC, WAIT_SPEC) if may_message_child or delegation is not None else ())
            + ((WITHDRAW_SPEC,) if self.requests is not None else ())
        )

    async def call(
        self, name: str, args: dict[str, Any], operation: str
    ) -> tuple[str, dict[str, Any]]:
        project, session = self.context.project_id, self.context.session_id
        if name == WITHDRAW_SPEC.name:
            return await self._withdraw(args)
        if name == "message_child":
            if CHILD_SPEC not in self.specs:
                raise ControlError(CONFLICT, "this run cannot message a child")
            request = ChildMessage.model_validate(args)
            references = (
                await threaded(self._references, request.references) if request.references else ()
            )
            message = await threaded(
                self.store.send_child,
                project,
                session,
                self.run,
                self.delegation,
                request.delegation_id,
                request.text,
                operation,
                references,
            )
        elif name == "message_parent":
            if self.delegation is None:
                raise ControlError(CONFLICT, "the root has no parent agent")
            request = ParentMessage.model_validate(args)
            message = await threaded(
                self.store.send_parent,
                project,
                session,
                self.run,
                self.delegation,
                request.text,
                operation,
            )
        elif name == "wait_agent_message":
            request = WaitMessage.model_validate(args)
            try:
                async with asyncio.timeout(request.timeout_seconds):
                    reason = await self.wake()
            except TimeoutError:
                return dump({"state": "timeout"}), {"agent_message_wait": "timeout"}
            if reason == "user_message":
                return dump({"state": "user_message"}), {"agent_message_wait": "user_message"}
            found = await threaded(
                self.store.pending,
                project,
                session,
                self.delegation,
                seen=self.seen,
                limit=2,
            )
            ids = [item.message_id for item in found]
            return dump({"state": "available", "message_ids": ids}), {"agent_message_wait": ids}
        else:
            request = MessageId.model_validate(args)
            message = await threaded(
                self.store.status, project, session, self.run, request.message_id
            )
        target = await threaded(
            self.store.target_state, project, session, message.recipient_delegation
        )
        ended = target is not None and target[1]
        failed_delivery = (
            target is not None
            and message.recipient_delegation is not None
            and message.delivered_run is not None
            and not await threaded(
                self.store.delivered_to_recipient,
                project,
                session,
                message.recipient_delegation,
                message.delivered_run,
            )
        )
        result = {
            "message_id": message.message_id,
            "state": (
                "delivery_failed"
                if failed_delivery
                else "delivered"
                if message.delivered_run is not None
                else "recipient_ended"
                if ended
                else "queued"
            ),
            "recipient": message.recipient_delegation or "root",
            "delivered_run": message.delivered_run,
        }
        return dump(result), {"agent_message": result}

    def _references(self, items: tuple[ReferenceInput, ...]) -> tuple[dict[str, Any], ...]:
        """The message's references, checked where this run may read (PD-44, PD-11)."""
        with self.store.control.read() as tx:
            return resolve(
                tx.sql,
                self.context.project_id,
                self.context.session_id,
                self.run,
                delegated=self.delegation is not None,
                items=items,
            )

    async def _withdraw(self, args: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        """A child's waiting request, withdrawn by its parent: refused, never approved."""
        if self.requests is None:
            raise ControlError(CONFLICT, "this run has no child requests to withdraw")
        request = Withdrawal.model_validate(args)
        withdrawn = await threaded(
            self.requests.withdraw,
            self.context.project_id,
            self.context.session_id,
            parent=self.run,
            delegation=request.delegation_id,
            request=request.permission_request,
        )
        result = {
            "permission_request": withdrawn.request,
            "delegation_id": withdrawn.delegation_id,
            "state": "withdrawn",
            "withdrawn_at": withdrawn.withdrawn_at,
        }
        return dump(result), {"child_request": result}

    async def fresh(self, run: RunView) -> list[AgentMessage]:
        return await threaded(
            self.store.pending,
            self.context.project_id,
            self.context.session_id,
            self.delegation,
            seen=frozenset(run.offered.get(self.name, ())),
        )

    def _notices(self, messages: list[AgentMessage]) -> tuple[Notice, ...]:
        """Each message as its notice; a task's shows the records it references, which
        its archived notice then makes readable (CM-37b), and a root's delivery failure
        names them as resolved."""
        found: list[Notice] = []
        for message in messages:
            payload: dict[str, Any] = {
                "message_id": message.message_id,
                "sender_run": message.sender_run,
                "sender_delegation": message.sender_delegation,
                "recipient_delegation": message.recipient_delegation,
                "text": message.body,
                "sent_at": message.created_at,
            }
            if message.references:
                payload["references"] = (
                    list(message.references)
                    if self.delegation is None
                    else self._referenced(message.message_id)
                )
            failed = self.delegation is None and message.recipient_delegation is not None
            found.append(
                Notice(self.name, (message.message_id,), UNDELIVERED if failed else NOTICE, payload)
            )
        return tuple(found)

    def _referenced(self, message: str) -> str | None:
        assert self.delegation is not None  # noqa: S101 - a delegated recipient's
        with self.store.control.read() as tx:
            return message_part(
                tx.sql, self.context.project_id, self.context.session_id, self.delegation, message
            )

    async def notices(self, point: Point, run: RunView) -> tuple[Notice, ...]:
        if point != "turn":
            return ()
        return await threaded(self._notices, await self.fresh(run))

    async def closing(self, run: RunView, answer: str | None) -> Closing:
        if run.turns_left <= 0:
            return Closing()
        found = await self.fresh(run)
        return Closing("continue", await threaded(self._notices, found)) if found else Closing()

    async def acknowledge(self, record: RunRecord) -> None:
        ids = list(record.delivered.get(self.name, ()))
        if ids:
            await threaded(
                self.store.acknowledge,
                self.context.project_id,
                self.context.session_id,
                self.delegation,
                self.run,
                ids,
            )

    async def model_returned(self, run: RunView) -> None:
        self.seen = frozenset(run.offered.get(self.name, ()))

    async def arrived(self) -> None:
        """Wake a parent's delegation wait when a child has sent something new."""
        while not await threaded(
            self.store.pending,
            self.context.project_id,
            self.context.session_id,
            self.delegation,
            seen=self.seen,
            limit=1,
        ):
            await self.clock.sleep(1.0)

    async def wake(self) -> str:
        """Wake for an agent message, or for the root's new user message."""
        waits: dict[asyncio.Future[None], str] = {
            asyncio.ensure_future(self.arrived()): "agent_message"
        }
        if self.user_arrived is not None:
            waits[asyncio.ensure_future(self.user_arrived())] = "user_message"
        try:
            done, _ = await asyncio.wait(waits, return_when=asyncio.FIRST_COMPLETED)
            return waits[next(iter(done))]
        finally:
            for wait in waits:
                wait.cancel()
            await asyncio.gather(*waits, return_exceptions=True)
