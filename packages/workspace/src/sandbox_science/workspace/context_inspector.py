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
from sandbox_science.orchestrator.projects import RunName

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


class ContextHistoryPage(Contract):
    messages: tuple[ContextHistoryMessage, ...]
    next_after: Counter | None
    unsearched_oversized_tool_calls: Counter = 0
    coverage: str = (
        "Retained original text and tool-call arguments in this attempt; native provider "
        "payloads, tool-call fields over 8 MiB and separately stored tool-record bytes are "
        "not searched. Exact "
        "substring search is case-sensitive. Legacy runs without an archive have no rows."
    )


def add_routes(app: web.Application) -> None:
    app.router.add_get(PATH, history)


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
        page = OriginalHistory(store).page(
            project,
            session,
            run,
            after=query.after,
            limit=query.limit,
            contains=query.contains,
            role=query.role,
            kind=query.kind,
            include_derived=query.include_derived == "1",
        )
        return ContextHistoryPage(
            messages=tuple(
                ContextHistoryMessage.model_validate(item.__dict__) for item in page.messages
            ),
            next_after=page.next_after,
            unsearched_oversized_tool_calls=page.unsearched_oversized_tool_calls,
        ).model_dump(mode="json")

    return await documents.answer(request, read)
