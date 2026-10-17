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

import re
from typing import Final

from aiohttp import web

from sandbox_science.orchestrator.documents import DocumentError, DocumentStore
from sandbox_science.orchestrator.history import Archive

from . import documents

PATH: Final = "/api/v0/projects/{project}/messages/{message}"
_MESSAGE: Final = re.compile(r"msg-[0-9a-f]{16}")


def add_routes(app: web.Application) -> None:
    app.router.add_get(PATH, message)
    app.router.add_get(PATH + "/tool-record", tool_record)


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
