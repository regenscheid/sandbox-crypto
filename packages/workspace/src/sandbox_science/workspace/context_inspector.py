# SPDX-License-Identifier: MIT
"""Authenticated, read-only original history for one agent attempt."""

from __future__ import annotations

from typing import Annotated, Final, Literal

from aiohttp import web
from pydantic import Field, TypeAdapter, ValidationError

from sandbox_science.contracts import Contract
from sandbox_science.contracts.model import Counter
from sandbox_science.orchestrator.documents import DocumentError, DocumentStore
from sandbox_science.orchestrator.history.inspector import OriginalHistory
from sandbox_science.orchestrator.history.inspector_index import InspectorIndex
from sandbox_science.orchestrator.history.timeline import AttemptTimeline
from sandbox_science.orchestrator.projects import ModelUsage, RunName

from . import documents
from .context import CONTEXT, identifier, session_record

PATH: Final = "/api/v0/projects/{project}/sessions/{session}/runs/{run}/context/history"
_RUN: Final = TypeAdapter[str](RunName)


class Query(Contract):
    after: Annotated[int, Field(ge=0, le=999999999999)] = 0
    limit: Annotated[int, Field(ge=1, le=50)] = 20
    contains: Annotated[str, Field(max_length=128)] = ""
    role: Literal["user", "assistant", "tool"] | None = None
    kind: Annotated[str, Field(pattern=r"^[a-z_]{1,32}$")] | None = None
    include_derived: Literal["0", "1"] = "0"
    scope: Literal["attempt", "agent"] = "attempt"
    indexed: Literal["0", "1"] = "0"


class ContextHistoryMessage(Contract):
    message: str
    position: Counter
    run: str
    parent: str | None
    created: str
    role: Literal["user", "assistant", "tool"]
    kind: str
    source: str | None
    snippet: str


class ContextAttempt(Contract):
    run: str
    predecessor: str | None
    epoch: int | None
    outcome: str | None
    saved: bool


class ContextAttemptsPage(Contract):
    attempts: tuple[ContextAttempt, ...]
    next_after: str | None


class AttemptsQuery(Contract):
    after: Annotated[str, Field(max_length=256)] = ""
    limit: Annotated[int, Field(ge=1, le=50)] = 20


class TimelineQuery(Contract):
    answers_after: Annotated[int, Field(ge=0, le=999999999)] = 0
    events_after: Annotated[int, Field(ge=0, le=999999999)] = 0
    limit: Annotated[int, Field(ge=1, le=50)] = 50
    tail: Literal["0", "1"] = "0"


class TimelineAnswer(Contract):
    ordinal: Counter
    message: str
    observed_at: str
    role: Literal["main", "delegated"]
    usage: ModelUsage


class TimelineFold(Contract):
    seq: Counter
    at: str
    state: str | None
    fold: str | None
    span_start: int | None
    span_end: int | None
    freed_estimate: int | None
    tokens_estimate: int | None


class TimelineAuxiliary(Contract):
    identity: str
    kind: Literal["reviewer", "watcher", "fold", "skills", "embedding"]
    intent_at: str
    usage: ModelUsage | None


class ContextTimelinePage(Contract):
    answers: tuple[TimelineAnswer, ...]
    folds: tuple[TimelineFold, ...]
    auxiliary: tuple[TimelineAuxiliary, ...]
    answers_after: Counter
    events_after: Counter
    more_answers: bool
    more_events: bool
    more_auxiliary: bool
    coverage: str = (
        "One selected attempt only. Answers are journaled response observations; fold times are "
        "event emission times. Auxiliary rows are a latest-intent snapshot, capped by limit; "
        "their creation time is not a response time. Older runs may lack journaled answers."
    )


class ContextHistoryPage(Contract):
    scope: Literal["attempt", "agent"] = "attempt"
    messages: tuple[ContextHistoryMessage, ...]
    next_after: Counter | None
    unsearched_oversized_tool_calls: Counter = 0
    indexed: bool = False
    index_pending: bool = False
    coverage: str = (
        "Retained original text and tool-call arguments in the selected scope; native provider "
        "payloads, tool-call fields over 8 MiB and separately stored tool-record bytes are "
        "not searched. Exact "
        "substring search is case-sensitive. Legacy runs without an archive have no rows."
    )


def add_routes(app: web.Application) -> None:
    from . import context_captures

    context_captures.add_routes(app)
    app.router.add_get(PATH, history)
    app.router.add_get(PATH.removesuffix("history") + "attempts", attempts)
    app.router.add_get(PATH.removesuffix("history") + "timeline", timeline)


async def history(request: web.Request) -> web.Response:
    if request.can_read_body or len(request.query) != len(dict(request.query)):
        raise DocumentError("invalid_request", "invalid context-history query")
    try:
        query = Query.model_validate(dict(request.query))
        run = _RUN.validate_python(request.match_info["run"], strict=True)
    except ValidationError:
        raise DocumentError("invalid_request", "invalid context-history query") from None
    project, session = identifier(request, "project"), identifier(request, "session")
    context = request.app[CONTEXT]

    def read(store: DocumentStore) -> dict[str, object]:
        session_record(context.projects, project, session)
        search = (
            InspectorIndex(store)
            if query.indexed == "1" and len(query.contains) >= 3 and "\x00" not in query.contains
            else OriginalHistory(store)
        )
        page = search.page(
            project,
            session,
            run,
            after=query.after,
            limit=query.limit,
            contains=query.contains,
            role=query.role,
            kind=query.kind,
            include_derived=query.include_derived == "1",
            continuations=query.scope == "agent",
        )
        return ContextHistoryPage(
            scope=query.scope,
            messages=tuple(
                ContextHistoryMessage.model_validate(item.__dict__) for item in page.messages
            ),
            next_after=page.next_after,
            unsearched_oversized_tool_calls=page.unsearched_oversized_tool_calls,
            indexed=page.indexed,
            index_pending=page.index_pending,
            **(
                {
                    "coverage": "Exact case-sensitive substring search over indexed original "
                    "content and tool-call arguments, including retrieval echoes. Native payloads "
                    "and "
                    "separate tool-record bytes are not indexed. Pending legacy/large rows require "
                    "bounded indexing before a negative result is complete."
                }
                if page.indexed
                else {}
            ),
        ).model_dump(mode="json")

    return await documents.answer(request, read)


async def attempts(request: web.Request) -> web.Response:
    if request.can_read_body or len(request.query) != len(dict(request.query)):
        raise DocumentError("invalid_request", "invalid context-attempt query")
    try:
        query = AttemptsQuery.model_validate(dict(request.query))
        run = _RUN.validate_python(request.match_info["run"], strict=True)
    except ValidationError:
        raise DocumentError("invalid_request", "invalid context-attempt query") from None
    project, session = identifier(request, "project"), identifier(request, "session")
    context = request.app[CONTEXT]

    def read(store: DocumentStore) -> dict[str, object]:
        session_record(context.projects, project, session)
        page = OriginalHistory(store).attempts(
            project, session, run, after=query.after, limit=query.limit
        )
        return ContextAttemptsPage(
            attempts=tuple(ContextAttempt.model_validate(item.__dict__) for item in page.attempts),
            next_after=page.next_after,
        ).model_dump(mode="json")

    return await documents.answer(request, read)


async def timeline(request: web.Request) -> web.Response:
    if request.can_read_body or len(request.query) != len(dict(request.query)):
        raise DocumentError("invalid_request", "invalid context-timeline query")
    try:
        query = TimelineQuery.model_validate(dict(request.query))
        run = _RUN.validate_python(request.match_info["run"], strict=True)
    except ValidationError:
        raise DocumentError("invalid_request", "invalid context-timeline query") from None
    project, session = identifier(request, "project"), identifier(request, "session")
    context = request.app[CONTEXT]

    def read(store: DocumentStore) -> dict[str, object]:
        session_record(context.projects, project, session)
        page = AttemptTimeline(store).page(
            project,
            session,
            run,
            answers_after=query.answers_after,
            events_after=query.events_after,
            limit=query.limit,
            tail=query.tail == "1",
        )
        return ContextTimelinePage(
            answers=tuple(TimelineAnswer.model_validate(item.__dict__) for item in page.answers),
            folds=tuple(TimelineFold.model_validate(item.__dict__) for item in page.folds),
            auxiliary=tuple(
                TimelineAuxiliary.model_validate(item.__dict__) for item in page.auxiliary
            ),
            answers_after=page.answers_after,
            events_after=page.events_after,
            more_answers=page.more_answers,
            more_events=page.more_events,
            more_auxiliary=page.more_auxiliary,
        ).model_dump(mode="json")

    return await documents.answer(request, read)
