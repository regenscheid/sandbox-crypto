# SPDX-License-Identifier: MIT
"""Bounded discovery and exact byte reads behind a saved run's history reference."""

from __future__ import annotations

import base64
import re
from typing import Annotated, Final

from aiohttp import web
from pydantic import Field, JsonValue, StringConstraints, TypeAdapter, ValidationError

from sandbox_science.contracts import Contract
from sandbox_science.contracts.model import Counter
from sandbox_science.orchestrator.documents import DocumentError, DocumentStore
from sandbox_science.orchestrator.history.collections import Collection
from sandbox_science.orchestrator.projects import RunName

from . import documents
from .context import CONTEXT, identifier, session_record

_RUN: Final = TypeAdapter[str](RunName)
_NAME: Final = re.compile(r"[a-z_]{1,64}")
_NUMBER: Final = re.compile(r"[0-9]{1,12}")


class HistoryItem(Contract):
    position: Counter
    value: JsonValue


class HistoryPage(Contract):
    collection: Annotated[str, StringConstraints(pattern=r"^[a-z_]{1,64}$")]
    offset: Counter
    next_offset: Counter | None
    total: Counter
    items: Annotated[list[HistoryItem], Field(max_length=100)]


class HistoryChunk(Contract):
    collection: Annotated[str, StringConstraints(pattern=r"^[a-z_]{1,64}$")]
    offset: Counter
    next_offset: Counter | None
    total: Counter
    data_base64: Annotated[str, StringConstraints(max_length=87384)]


def add_routes(app: web.Application) -> None:
    path = "/api/v0/projects/{project}/sessions/{session}/runs/{run}/history"
    app.router.add_get(path, listing)
    app.router.add_get(path + "/chunks", chunk)


def _scope(request: web.Request) -> tuple[str, str, str]:
    project, session = identifier(request, "project"), identifier(request, "session")
    try:
        run = _RUN.validate_python(request.match_info["run"], strict=True)
    except ValidationError:
        raise DocumentError("invalid_request", "invalid run name") from None
    return project, session, run


def _query(request: web.Request, allowed: set[str]) -> tuple[str, dict[str, int]]:
    query = dict(request.query)
    collection = query.pop("collection", "messages")
    if (
        request.can_read_body
        or len(request.query) != len(dict(request.query))
        or set(query) - allowed
        or not _NAME.fullmatch(collection)
        or any(not _NUMBER.fullmatch(value) for value in query.values())
    ):
        raise DocumentError("invalid_request", "invalid history paging query")
    return collection, {key: int(value) for key, value in query.items()}


async def listing(request: web.Request) -> web.Response:
    scope = _scope(request)
    collection, query = _query(request, {"offset", "limit"})
    context = request.app[CONTEXT]

    def read(store: DocumentStore) -> dict[str, object]:
        session_record(context.projects, *scope[:2])
        values = Collection[object](store, scope, collection)
        items, following = values.page(**query)
        return HistoryPage.model_validate(
            {
                "collection": collection,
                "offset": query.get("offset", 0),
                "next_offset": following,
                "total": len(values),
                "items": items,
            }
        ).model_dump(mode="json")

    return await documents.answer(request, read)


async def chunk(request: web.Request) -> web.Response:
    scope = _scope(request)
    collection, query = _query(request, {"position", "offset", "limit"})
    context = request.app[CONTEXT]

    def read(store: DocumentStore) -> dict[str, object]:
        session_record(context.projects, *scope[:2])
        result = Collection[object](store, scope, collection).chunk(**query)
        return HistoryChunk(
            collection=collection,
            offset=result.offset,
            total=result.total,
            next_offset=result.next_offset,
            data_base64=base64.b64encode(result.data).decode("ascii"),
        ).model_dump(mode="json")

    if "position" not in query:
        raise DocumentError("invalid_request", "a history byte read needs an item position")
    return await documents.answer(request, read)
