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
from typing import Annotated, Final

from aiohttp import web
from pydantic import Field, StringConstraints, ValidationError

from sandbox_science.contracts import Contract, Identifier
from sandbox_science.contracts.model import Counter
from sandbox_science.orchestrator.documents import DocumentError, DocumentStore
from sandbox_science.orchestrator.history import Archive
from sandbox_science.orchestrator.history.operations import CHUNK_BYTES

from . import documents

PATH: Final = "/api/v0/projects/{project}/messages/{message}"
_MESSAGE: Final = re.compile(r"msg-[0-9a-f]{16}")
_INTEGER: Final = re.compile(r"[0-9]{1,19}")


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


def add_routes(app: web.Application) -> None:
    app.router.add_get(PATH, message)
    app.router.add_get(PATH + "/tool-record", tool_record)
    app.router.add_get(PATH + "/tool-record/chunks", tool_record_chunks)


async def message(request: web.Request) -> web.Response:
    identifier = request.match_info["message"]
    if not _MESSAGE.fullmatch(identifier):
        return documents.api_error(
            DocumentError("invalid_request", "a message is named by its msg- ID")
        )
    project = request.match_info["project"]

    def read(store: DocumentStore) -> dict[str, object]:
        found = Archive(store).message(project, identifier)
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
    query = dict(request.query)
    if (
        set(query) - {"offset", "limit"}
        or len(query) != len(request.query)
        or request.can_read_body
        or any(not _INTEGER.fullmatch(value) for value in query.values())
    ):
        raise DocumentError("invalid_request", "give only offset and limit as byte counts")
    try:
        selected = ChunkQuery.model_validate({key: int(value) for key, value in query.items()})
    except ValidationError:
        raise DocumentError(
            "invalid_request", "tool record offset or limit is out of range"
        ) from None
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
