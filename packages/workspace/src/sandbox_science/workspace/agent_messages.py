# SPDX-License-Identifier: MIT
"""A session's agent messages, read-only (plans-and-delegation spec, PD-44; B10).

``GET /api/v0/projects/{project}/sessions/{session}/agent-messages`` lists the messages
the session's agents sent each other (``message_child`` and ``message_parent``), in the
order they were sent, after an exclusive cursor (``after``, a message ID) and at most
``limit`` (100) a page. The conversation fetches a received message's body here, since
the run's notice names it only by its ID. A body is the sending agent's text: data,
shown as text, never an instruction (CM-49). The route only reads, through the
orchestrator's mailbox on the workspace's read-only store.
"""

from __future__ import annotations

import re
from typing import Annotated, Final

from aiohttp import web
from pydantic import Field, JsonValue, StringConstraints

from sandbox_science.contracts import Contract
from sandbox_science.orchestrator.agent_messages import MAX_BODY, PAGE, AgentMessageStore
from sandbox_science.orchestrator.documents import DocumentError, DocumentStore

from .context import CONTEXT, identifier, session_record
from .requests import document_answer

PATH: Final = "/api/v0/projects/{project}/sessions/{session}/agent-messages"
_MESSAGE: Final = re.compile(r"agm-[0-9a-f]{24}")
_LIMIT: Final = re.compile(r"[1-9][0-9]{0,2}")

MessageId = Annotated[str, StringConstraints(pattern=r"^agm-[0-9a-f]{24}$")]


class AgentMessageView(Contract):
    """One message between two direct relatives of the session's run tree."""

    message_id: MessageId
    sender_run: str
    """The run that sent it, by its name."""
    sender_delegation: str | None
    """The sender's delegation; null for the session's main agent."""
    recipient_delegation: str | None
    """The recipient's delegation; null for the session's main agent."""
    body: Annotated[str, StringConstraints(max_length=MAX_BODY)]
    """The sender's text, as it was sent: an agent's words, data and not instructions."""
    created_at: str
    delivered_run: str | None
    """The recipient's run whose saved record acknowledged it; null while queued."""
    references: tuple[dict[str, JsonValue], ...]
    """The sender's records it references, as the platform resolved them (PD-44)."""


class AgentMessagePage(Contract):
    """A page of the session's agent messages, oldest first."""

    messages: Annotated[tuple[AgentMessageView, ...], Field(max_length=PAGE)]
    next_after: MessageId | None
    """The last message of this page, or the cursor given when the page is empty: where
    the next read starts."""
    has_more: bool


def add_routes(app: web.Application) -> None:
    app.router.add_get(PATH, listing)


def _query(request: web.Request) -> tuple[str | None, int]:
    query = dict(request.query)
    repeated = len(query) != len(request.query)
    after, limit = query.pop("after", None), query.pop("limit", str(PAGE))
    if (
        query
        or repeated
        or request.can_read_body
        or (after is not None and not _MESSAGE.fullmatch(after))
        or not _LIMIT.fullmatch(limit)
        or int(limit) > PAGE
    ):
        raise DocumentError("invalid_request", "invalid agent message paging query")
    return after, int(limit)


async def listing(request: web.Request) -> web.Response:
    project, session = identifier(request, "project"), identifier(request, "session")
    after, limit = _query(request)
    context = request.app[CONTEXT]

    def read(store: DocumentStore) -> AgentMessagePage:
        session_record(context.projects, project, session)
        found = AgentMessageStore(store).listing(project, session, after=after, limit=limit)
        messages = tuple(
            AgentMessageView(
                message_id=message.message_id,
                sender_run=message.sender_run,
                sender_delegation=message.sender_delegation,
                recipient_delegation=message.recipient_delegation,
                body=message.body,
                created_at=message.created_at,
                delivered_run=message.delivered_run,
                references=message.references,
            )
            for message in found.messages
        )
        return AgentMessagePage(
            messages=messages,
            next_after=messages[-1].message_id if messages else after,
            has_more=found.has_more,
        )

    return await document_answer(request, read)
