# SPDX-License-Identifier: MIT
"""An archived message, read whole, for the review views (docs/specs/context-management.md,
CM-3, CM-34; consistency-review.md, CR-14 as amended).

``GET /api/v0/projects/{project}/messages/{message}`` answers one message of the
project's archive by its ``msg-`` ID: its session, position, run (and parent run, for a
delegated run's), time, role, kind, source or tool, call ID, and its whole text as
``history_read`` gives it, the content as the run added it and an answer's tool calls,
one JSON object a line. It only reads.
"""

from __future__ import annotations

import base64
import re
from typing import Annotated, Final, Literal

from aiohttp import web
from pydantic import Field, StringConstraints, TypeAdapter, ValidationError

from sandbox_science.contracts import Contract, Identifier
from sandbox_science.contracts.model import Counter
from sandbox_science.orchestrator.context.history import Archive
from sandbox_science.orchestrator.context.history.operations import CHUNK_BYTES
from sandbox_science.orchestrator.context.history.operations import PAGE as RECORD_PAGE
from sandbox_science.orchestrator.documents import DocumentError, DocumentStore
from sandbox_science.orchestrator.projects import RunName

from . import documents, evidence, history
from .context import CONTEXT, identifier, session_record

PATH: Final = "/api/v0/projects/{project}/messages/{message}"
_MESSAGE: Final = re.compile(r"msg-[0-9a-f]{16}")
_INTEGER: Final = re.compile(r"[0-9]{1,19}")
_RUN: Final = TypeAdapter[str](RunName)


class ChunkQuery(Contract):
    offset: Counter = 0
    limit: Annotated[int, Field(strict=True, ge=1, le=CHUNK_BYTES)] = CHUNK_BYTES


class ToolRecordChunk(Contract):
    """One page of exact JSON bytes; join decoded bytes before UTF-8/JSON parsing."""

    message: Identifier
    offset: Counter
    total: Counter
    next_offset: Counter | None
    data_base64: Annotated[str, StringConstraints(max_length=4 * ((CHUNK_BYTES + 2) // 3))]


class MessageFieldChunk(ToolRecordChunk):
    field: Literal["content", "tool_calls"]


class RecordQuery(Contract):
    after: Counter = 0
    limit: Annotated[int, Field(strict=True, ge=1, le=RECORD_PAGE)] = RECORD_PAGE


class ToolRecordReference(Contract):
    message: Identifier
    position: Counter
    operation_id: Annotated[str, StringConstraints(min_length=1, max_length=128)] | None
    record_bytes: Counter | None
    """Null marks a legacy message without a full record in the journal."""


class ToolRecordListing(Contract):
    after: Counter
    next_after: Counter
    has_more: bool
    records: Annotated[tuple[ToolRecordReference, ...], Field(max_length=RECORD_PAGE)]


def add_routes(app: web.Application) -> None:
    evidence.add_routes(app)
    history.add_routes(app)
    app.router.add_get(PATH, message)
    app.router.add_get(PATH + "/chunks", message_chunks)
    app.router.add_get(PATH + "/tool-record", tool_record)
    app.router.add_get(PATH + "/tool-record/chunks", tool_record_chunks)
    app.router.add_get(
        "/api/v0/projects/{project}/sessions/{session}/runs/{run}/tool-records", tool_records
    )


async def message(request: web.Request) -> web.Response:
    identifier = request.match_info["message"]
    if not _MESSAGE.fullmatch(identifier):
        return documents.api_error(
            DocumentError("invalid_request", "a message is named by its msg- ID")
        )
    project = request.match_info["project"]

    def read(store: DocumentStore) -> dict[str, object]:
        found = Archive(store).message(project, identifier, max_bytes=1024 * 1024)
        entry = found.entry
        return {
            "message": found.message_id,
            "session": found.session,
            "position": found.position,
            "run": found.run,
            "parent": found.parent,
            "created": found.created,
            "role": entry.role,
            "kind": entry.kind,
            "source": entry.source,
            "call_id": entry.call_id,
            "text": entry.text,
        }

    return await documents.answer(request, read)


async def message_chunks(request: web.Request) -> web.Response:
    """A bounded exact byte page of a large message's text or stored tool calls."""
    message_id = request.match_info["message"]
    if not _MESSAGE.fullmatch(message_id):
        raise DocumentError("invalid_request", "a message is named by its msg- ID")
    query = dict(request.query)
    field = query.pop("field", "content")
    if field not in ("content", "tool_calls") or len(dict(request.query)) != len(request.query):
        raise DocumentError("invalid_request", "invalid message field query")
    # Reuse the checked numeric paging contract without admitting any other fields.
    selected = _query_numbers(query, request, ChunkQuery)
    project = request.match_info["project"]

    def read(store: DocumentStore) -> dict[str, object]:
        found = Archive(store).message_field_chunk(
            project, message_id, field, offset=selected.offset, limit=selected.limit
        )
        return MessageFieldChunk(
            message=message_id,
            field=field,
            offset=found.offset,
            total=found.total,
            next_offset=found.next_offset,
            data_base64=base64.b64encode(found.data).decode("ascii"),
        ).model_dump(mode="json")

    return await documents.answer(request, read)


async def tool_record(request: web.Request) -> web.Response:
    """The original tool record, at most 1 MiB; a larger record returns a bounded
    refusal with its byte size. Legacy messages without a journal record return 404."""
    identifier = request.match_info["message"]
    if not _MESSAGE.fullmatch(identifier):
        return documents.api_error(
            DocumentError("invalid_request", "a message is named by its msg- ID")
        )
    project = request.match_info["project"]
    return await documents.answer(
        request, lambda store: Archive(store).tool_record(project, identifier)
    )


async def tool_record_chunks(request: web.Request) -> web.Response:
    """A bounded page behind the same project scope and authentication as the message."""
    identifier = request.match_info["message"]
    if not _MESSAGE.fullmatch(identifier):
        raise DocumentError("invalid_request", "a message is named by its msg- ID")
    selected = _query(request, ChunkQuery)
    project = request.match_info["project"]

    def read(store: DocumentStore) -> dict[str, object]:
        found = Archive(store).tool_record_chunk(
            project, identifier, offset=selected.offset, limit=selected.limit
        )
        return ToolRecordChunk(
            message=identifier,
            offset=found.offset,
            total=found.total,
            next_offset=found.next_offset,
            data_base64=base64.b64encode(found.data).decode("ascii"),
        ).model_dump(mode="json")

    return await documents.answer(request, read)


async def tool_records(request: web.Request) -> web.Response:
    """Bounded discovery for unfinished and finished runs, without loading their bodies."""
    project, session = identifier(request, "project"), identifier(request, "session")
    try:
        run = _RUN.validate_python(request.match_info["run"], strict=True)
    except ValidationError:
        raise DocumentError("invalid_request", "invalid run name") from None
    selected = _query(request, RecordQuery)
    context = request.app[CONTEXT]

    def read(store: DocumentStore) -> dict[str, object]:
        session_record(context.projects, project, session)
        found = Archive(store).tool_records(
            project, session, run, after=selected.after, limit=selected.limit
        )
        return ToolRecordListing(
            after=selected.after,
            next_after=found.next_after,
            has_more=found.has_more,
            records=tuple(
                ToolRecordReference(
                    message=record.message_id,
                    position=record.position,
                    operation_id=record.operation_id,
                    record_bytes=record.record_bytes,
                )
                for record in found.records
            ),
        ).model_dump(mode="json")

    return await documents.answer(request, read)


def _query[Q: Contract](request: web.Request, model: type[Q]) -> Q:
    query = dict(request.query)
    if len(query) != len(request.query):
        raise DocumentError("invalid_request", "invalid tool record paging query")
    return _query_numbers(query, request, model)


def _query_numbers[Q: Contract](query: dict[str, str], request: web.Request, model: type[Q]) -> Q:
    if (
        set(query) - model.model_fields.keys()
        or request.can_read_body
        or any(not _INTEGER.fullmatch(value) for value in query.values())
    ):
        raise DocumentError("invalid_request", "invalid tool record paging query")
    try:
        return model.model_validate({key: int(value) for key, value in query.items()})
    except ValidationError:
        raise DocumentError("invalid_request", "tool record paging value is out of range") from None
