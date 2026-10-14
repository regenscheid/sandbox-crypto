# SPDX-License-Identifier: MIT
"""Chat with a session's main agent: the user's messages and a run's events.

The workspace writes and reads the run service's records in the control store
(docs/specs/run-service.md, RS-2, RS-15); it starts no run itself. Routes:

- ``POST …/sessions/{session}/messages`` with ``{"message_id", "text"}`` queues a message
  for the session's run (RS-4). The same ID with the same text answers the message
  already written, so a retried write is safe.
- ``GET …/sessions/{session}/messages?after=N`` lists the session's messages with their
  state, the run that holds the session, if one does, and whether the run service is
  running (RS-3): without it, a message waits.
- ``POST …/sessions/{session}/resume`` with ``{}`` queues the session's held messages
  again, those an interrupted or cancelled run saw (RS-12).
- ``POST …/sessions/{session}/runs/{run}/cancel`` with ``{}`` asks the run service to
  cancel the run (RS-11).
- ``GET …/sessions/{session}/runs/{run}/events?after=N&limit=M`` reads a run's events by
  cursor, with the run's state: ``active`` while it holds the session, ``ended`` once its
  ``stopped`` event is written, and ``interrupted`` otherwise (RS-12).
"""

from __future__ import annotations

import asyncio
from typing import Final, Literal, cast

from aiohttp import web
from pydantic import TypeAdapter, ValidationError

from sandbox_science.contracts import Contract, ErrorKind, Identifier
from sandbox_science.orchestrator.projects import ProjectError
from sandbox_science.orchestrator.runstate import PAGE, ChatMessage, RunEvent, RunLease, RunState

from . import api
from .context import CONTEXT, USER_ID, identifier, json_answer, json_body, session_record

PATH: Final = "/api/v0/projects/{project}/sessions/{session}"
_IDENTIFIER: Final = TypeAdapter[str](Identifier)


class MessageAnswer(Contract):
    """A message as written, and the run that holds the session, if one does."""

    message: ChatMessage
    run: RunLease | None


class MessageListing(Contract):
    messages: list[ChatMessage]
    run: RunLease | None
    service: bool
    """Whether the run service is running: without it, messages wait."""


class Requested(Contract):
    """A resume or a cancel, written for the run service."""

    requested: bool
    resumed: int = 0
    """The held messages queued again, for a resume."""


class EventPage(Contract):
    """A page of a run's events, and the run's state now."""

    state: Literal["active", "ended", "interrupted"]
    events: list[RunEvent]


def add_routes(app: web.Application) -> None:
    app.router.add_get(PATH + "/messages", listed)
    app.router.add_post(PATH + "/messages", post)
    app.router.add_get(PATH + "/runs/{run}/events", events)
    app.router.add_post(PATH + "/resume", resume)
    app.router.add_post(PATH + "/runs/{run}/cancel", cancel)


def _scope(request: web.Request) -> tuple[str, str]:
    project, session = identifier(request, "project"), identifier(request, "session")
    return project, session


def _cursor(request: web.Request, name: str, default: int, top: int) -> int:
    raw = request.query.get(name)
    if raw is None:
        return default
    if not raw.isdigit() or not 0 <= int(raw) <= top:
        raise ProjectError(ErrorKind.INVALID_REQUEST, f"{name} is a number from 0 to {top}")
    return int(raw)


async def post(request: web.Request) -> web.Response:
    project, session = _scope(request)
    body = await json_body(request, "message")
    fields = cast(dict[str, object], body) if isinstance(body, dict) else {}
    if set(fields) != {"message_id", "text"}:
        raise ProjectError(ErrorKind.INVALID_REQUEST, "a message is {message_id, text}")
    message_id, text = fields["message_id"], fields["text"]
    try:
        message_id = _IDENTIFIER.validate_python(message_id, strict=True)
    except ValidationError:
        raise ProjectError(ErrorKind.INVALID_REQUEST, "invalid message_id") from None
    if not isinstance(text, str):
        raise ProjectError(ErrorKind.INVALID_REQUEST, "a message's text is a string")
    context = request.app[CONTEXT]

    def write() -> str:
        session_record(context.projects, project, session)
        state = RunState(context.writer)
        found = state.post(project, session, message_id, text, author=USER_ID or "user")
        answer = MessageAnswer(message=found, run=state.lease(project, session))
        return api.dumps(answer.model_dump(mode="json"))

    return json_answer(await asyncio.to_thread(write))


async def listed(request: web.Request) -> web.Response:
    project, session = _scope(request)
    after = _cursor(request, "after", 0, 2**62)
    context = request.app[CONTEXT]

    def read() -> str:
        session_record(context.projects, project, session)
        state = RunState(context.documents)
        listing = MessageListing(
            messages=state.messages(project, session, after=after),
            run=state.lease(project, session),
            service=state.service_running(),
        )
        return api.dumps(listing.model_dump(mode="json"))

    return json_answer(await asyncio.to_thread(read))


async def events(request: web.Request) -> web.Response:
    project, session = _scope(request)
    run = _run_name(request)
    after = _cursor(request, "after", 0, 999999999)
    limit = _cursor(request, "limit", PAGE, PAGE)
    context = request.app[CONTEXT]

    def read() -> str:
        session_record(context.projects, project, session)
        state = RunState(context.documents)
        if not state.known(project, session, run):
            raise ProjectError(ErrorKind.NOT_FOUND, f"session {session!r} has no run {run!r}")
        found = state.events(project, session, run, after=after, limit=limit)
        lease = state.lease(project, session)
        status: Literal["active", "ended", "interrupted"] = "interrupted"
        if state.ended(project, session, run):
            status = "ended"
        elif lease is not None and lease.run == run:
            status = "active"
        return api.dumps(EventPage(state=status, events=found).model_dump(mode="json"))

    return json_answer(await asyncio.to_thread(read))


async def _empty(request: web.Request, what: str) -> None:
    if await json_body(request, what) != {}:
        raise ProjectError(ErrorKind.INVALID_REQUEST, f"a {what} is an empty object")


def _run_name(request: web.Request) -> str:
    run = request.match_info["run"]
    if not 1 <= len(run.encode()) <= 256:
        raise ProjectError(ErrorKind.INVALID_REQUEST, "invalid run name")
    return run


async def resume(request: web.Request) -> web.Response:
    project, session = _scope(request)
    await _empty(request, "resume")
    context = request.app[CONTEXT]

    def write() -> str:
        session_record(context.projects, project, session)
        resumed = RunState(context.writer).resume(project, session)
        return api.dumps(Requested(requested=True, resumed=resumed).model_dump(mode="json"))

    return json_answer(await asyncio.to_thread(write))


async def cancel(request: web.Request) -> web.Response:
    project, session = _scope(request)
    run = _run_name(request)
    await _empty(request, "cancel")
    context = request.app[CONTEXT]

    def write() -> str:
        session_record(context.projects, project, session)
        RunState(context.writer).cancel(project, session, run)
        return api.dumps(Requested(requested=True).model_dump(mode="json"))

    return json_answer(await asyncio.to_thread(write))
