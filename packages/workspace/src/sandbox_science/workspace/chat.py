# SPDX-License-Identifier: MIT
"""Chat with a session's main agent: the user's messages and a run's events.

The workspace writes and reads the run service's records in the control store
(docs/specs/run-service.md, RS-2, RS-15); it starts no run itself. Routes:

- ``POST …/sessions/{session}/messages`` with ``{"message_id", "text"}`` queues a message
  for the session's run (RS-4). The same ID with the same text answers the message
  already written, so a retried write is safe. While the run that holds the session
  waits for the user's answer to a question, the message answers the oldest such
  question in the user's own words instead, and the answer names the question rather
  than a message (PD-23).
- ``GET …/sessions/{session}/messages?after=N`` lists the session's messages with their
  state, the run that holds the session, if one does, and whether the run service is
  running (RS-3): without it, a message waits. While a message is queued, no run holds
  the session and the service is running, the listing also says what the worker's record
  of the session's environment is, since the first run of a project waits for its sandbox
  to be made (``creating``), which takes a minute or more.
- ``POST …/sessions/{session}/resume`` with ``{}`` queues the session's held messages
  again, those an interrupted or cancelled run saw (RS-12).
- ``POST …/sessions/{session}/runs/{run}/cancel`` with ``{}`` asks the run service to
  cancel the run (RS-11).
- ``GET …/sessions/{session}/runs/{run}/events?after=N&limit=M`` reads a run's events by
  cursor, with the run's state: ``active`` while it holds the session, ``ended`` once its
  ``stopped`` event is written, and ``interrupted`` otherwise (RS-12).
- ``GET …/sessions/{session}/questions`` lists the session's latest questions to the
  user, open and closed, oldest first, with the run that holds the session (PD-26).
- ``POST …/sessions/{session}/questions/{question}/answer`` with ``{"kind", "picked",
  "text"}`` answers an open question: options picked, the user's own words, the agent to
  decide, or a cancel (PD-23). The same answer again is the answer already given. An
  answer to an open question whose run no longer holds the session is refused, a
  conflict with the reason ``run_ended``: no one would read it, and it can be sent as a
  chat message instead.
"""

from __future__ import annotations

import asyncio
from typing import Final, Literal, cast

from aiohttp import web
from pydantic import TypeAdapter, ValidationError

from sandbox_science.contracts import Contract, EnvironmentState, ErrorKind, Identifier
from sandbox_science.orchestrator.projects import ProjectError
from sandbox_science.orchestrator.questions import Answer, Question, Questions
from sandbox_science.orchestrator.runstate import PAGE, ChatMessage, RunEvent, RunLease, RunState
from sandbox_science.orchestrator.runtree import RunTrees
from sandbox_science.orchestrator.worker_client import WorkerAPIError

from . import api
from .context import (
    CONTEXT,
    USER_ID,
    WorkspaceContext,
    identifier,
    json_answer,
    json_body,
    session_record,
)

PATH: Final = "/api/v0/projects/{project}/sessions/{session}"
_IDENTIFIER: Final = TypeAdapter[str](Identifier)


class MessageAnswer(Contract):
    """A message as written, or the question it answered, and the run that holds the
    session, if one does."""

    message: ChatMessage | None
    question: Question | None = None
    """The question the message answered, instead of being queued."""
    run: RunLease | None


class QuestionListing(Contract):
    """A session's latest questions to the user, oldest first, and the run that holds
    the session: a message the user writes answers its oldest open question."""

    questions: list[Question]
    run: RunLease | None


class QuestionAnswer(Contract):
    question: Question


class MessageListing(Contract):
    messages: list[ChatMessage]
    run: RunLease | None
    service: bool
    """Whether the run service is running: without it, messages wait."""
    environment: EnvironmentState | None = None
    """The worker's record of the session's environment, read only while a message is
    queued, no run holds the session and the service is running: what the message waits
    on before its run starts. ``None`` otherwise, and when the worker does not say."""


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
    app.router.add_get(PATH + "/questions", questions)
    app.router.add_post(PATH + "/questions/{question}/answer", answer)


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
        written = MessageAnswer(
            message=found if isinstance(found, ChatMessage) else None,
            question=found if isinstance(found, Question) else None,
            run=state.lease(project, session),
        )
        return api.dumps(written.model_dump(mode="json"))

    return json_answer(await asyncio.to_thread(write))


async def listed(request: web.Request) -> web.Response:
    project, session = _scope(request)
    after = _cursor(request, "after", 0, 2**62)
    context = request.app[CONTEXT]

    def read() -> tuple[MessageListing, str]:
        found = session_record(context.projects, project, session)
        state = RunState(context.documents)
        listing = MessageListing(
            messages=state.messages(project, session, after=after),
            run=state.lease(project, session),
            service=state.service_running(),
        )
        return listing, found.environment_id

    listing, environment = await asyncio.to_thread(read)
    if (
        listing.service
        and listing.run is None
        and any(message.state == "queued" for message in listing.messages)
    ):
        state = await _environment_state(context, environment)
        listing = listing.model_copy(update={"environment": state})
    return json_answer(api.dumps(listing.model_dump(mode="json")))


async def _environment_state(
    context: WorkspaceContext, environment: str
) -> EnvironmentState | None:
    """The worker's record of ``environment``, which only reads it: no sandbox is started.
    The chat asks at every poll while a message waits, so a worker that does not answer in
    a moment, or at all, leaves the state unknown and the chat unhurried."""
    if context.worker is None:
        return None
    try:
        async with asyncio.timeout(context.timeouts.environment):
            return (await context.worker.environment(environment)).state
    except (WorkerAPIError, TimeoutError):
        return None


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
        # A delegated run holds no lease: it is active while its root holds the session.
        now = RunTrees(context.documents).state(project, session, run)
        status: Literal["active", "ended", "interrupted"] = "active" if now == "running" else now
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


async def questions(request: web.Request) -> web.Response:
    project, session = _scope(request)
    context = request.app[CONTEXT]

    def read() -> str:
        session_record(context.projects, project, session)
        listing = QuestionListing(
            questions=Questions(context.documents).listed(project, session),
            run=RunState(context.documents).lease(project, session),
        )
        return api.dumps(listing.model_dump(mode="json"))

    return json_answer(await asyncio.to_thread(read))


async def answer(request: web.Request) -> web.Response:
    project, session = _scope(request)
    question_id = identifier(request, "question")
    body = await json_body(request, "answer")
    try:
        given = Answer.model_validate(body)
    except ValidationError:
        raise ProjectError(
            ErrorKind.INVALID_REQUEST,
            'an answer is {"kind", "picked", "text"}: kind picked, free, decide or cancel',
        ) from None
    context = request.app[CONTEXT]

    def write() -> str:
        session_record(context.projects, project, session)
        live_after = RunState(context.writer).live_after()
        answered = Questions(context.writer).answer(
            project, session, question_id, given, live_after=live_after
        )
        return api.dumps(QuestionAnswer(question=answered).model_dump(mode="json"))

    return json_answer(await asyncio.to_thread(write))
