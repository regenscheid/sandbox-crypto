# SPDX-License-Identifier: MIT
"""Authenticated, paged dispatch capture and explicit derived-index maintenance."""

from __future__ import annotations

from typing import Annotated, Literal

from aiohttp import web
from pydantic import Field, TypeAdapter, ValidationError

from sandbox_science.contracts import Contract
from sandbox_science.orchestrator.documents import DocumentError, DocumentStore
from sandbox_science.orchestrator.history.captured import CapturedContexts
from sandbox_science.orchestrator.history.inspector_index import InspectorIndex
from sandbox_science.orchestrator.projects import RunName

from . import documents
from .context import CONTEXT, identifier, session_record

BASE = "/api/v0/projects/{project}/sessions/{session}/runs/{run}/context"
_RUN = TypeAdapter[str](RunName)


class CaptureQuery(Contract):
    before: Annotated[int, Field(ge=0, le=999999999999)] = 0
    scope: Literal["attempt", "agent"] = "attempt"


class PartQuery(Contract):
    after: Annotated[int, Field(ge=0, le=65536)] = 0


class TextQuery(Contract):
    offset: Annotated[int, Field(ge=0, le=16777216)] = 0


class IndexQuery(Contract):
    scope: Literal["attempt", "agent"] = "attempt"


class IndexStatus(Contract):
    chunks: int
    pending: bool


def add_routes(app: web.Application) -> None:
    app.router.add_get(BASE + "/captures", calls)
    app.router.add_get(BASE + "/captures/{identity}/parts", parts)
    app.router.add_get(BASE + "/captures/{identity}/parts/{ordinal}", text)
    app.router.add_post(BASE + "/history/index", backfill)


def query[T: Contract](request: web.Request, kind: type[T]) -> T:
    if request.can_read_body or len(request.query) != len(dict(request.query)):
        raise DocumentError("invalid_request", "invalid context query")
    try:
        _RUN.validate_python(request.match_info["run"], strict=True)
        return kind.model_validate(dict(request.query))
    except ValidationError:
        raise DocumentError("invalid_request", "invalid context query") from None


def scope(request: web.Request) -> tuple[str, str, str]:
    project, session = identifier(request, "project"), identifier(request, "session")
    session_record(request.app[CONTEXT].projects, project, session)
    return project, session, request.match_info["run"]


async def calls(request: web.Request) -> web.Response:
    given = query(request, CaptureQuery)
    return await documents.answer(
        request,
        lambda store: (
            CapturedContexts(store.control)
            .calls(
                *scope(request),
                before=given.before,
                continuations=given.scope == "agent",
            )
            .model_dump(mode="json")
        ),
    )


async def parts(request: web.Request) -> web.Response:
    given = query(request, PartQuery)
    return await documents.answer(
        request,
        lambda store: (
            CapturedContexts(store.control)
            .parts(
                *scope(request),
                identifier(request, "identity"),
                after=given.after,
            )
            .model_dump(mode="json")
        ),
    )


async def text(request: web.Request) -> web.Response:
    given = query(request, TextQuery)
    raw = request.match_info["ordinal"]
    if not raw.isascii() or not raw.isdecimal() or len(raw) > 6:
        raise DocumentError("invalid_request", "invalid captured part ordinal")
    return await documents.answer(
        request,
        lambda store: (
            CapturedContexts(store.control)
            .text(
                *scope(request),
                identifier(request, "identity"),
                int(raw),
                offset=given.offset,
            )
            .model_dump(mode="json")
        ),
    )


async def backfill(request: web.Request) -> web.Response:
    given = query(request, IndexQuery)

    def write(store: DocumentStore) -> dict[str, object]:
        progress = InspectorIndex(store.control).backfill(
            *scope(request), continuations=given.scope == "agent"
        )
        return IndexStatus(chunks=progress.chunks, pending=progress.pending).model_dump(mode="json")

    return await documents.answer(request, write, writer=True)
