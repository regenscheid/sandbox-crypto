# SPDX-License-Identifier: MIT
"""The workspace API's living documents, read-only (living documents, LD-9, LD-11).

Routes, for GET and HEAD:

- ``/api/v0/projects/{project}/documents``: the project's documents, its own and its
  sessions' workbooks', without the deleted ones.
- ``/api/v0/projects/{project}/documents/{document}``: a revision of a document, the
  current one or ``?revision=N``: its text, its author, reason and warnings, its front
  matter's title and macros, and its block map, each block with its ID, kind, lines and
  status and each formula with its ID, kind, status and TeX, for the browser to attach
  to its own parse (LD-9).
- ``/api/v0/projects/{project}/documents/{document}/history``: who changed what
  (LD-11): each revision with its author, reason and the blocks it changed, each block
  with the revisions that changed it, and the agents' patches with their states.

A document is named by its ID, or by its name for one of the project's own. The
documents are read from the control store in the harness's state directory, opened
read-only: the workspace never writes it. The server parses a revision again to give
its formulas' TeX; when its parse no longer matches the block map stored with the
revision (a parser of another version made it), the blocks come without their formulas'
TeX and ``matched`` is false.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Callable
from typing import Final, cast

from aiohttp import hdrs, web

from sandbox_science.contracts import ErrorKind
from sandbox_science.orchestrator.documents import DocumentError, DocumentStore, Mapped
from sandbox_science.orchestrator.documents.models import NAME

from . import api

DOCUMENTS: Final = web.AppKey("documents", DocumentStore)

_DOCUMENT_ID: Final = re.compile(r"doc-[0-9a-f]{24}")
_REVISION: Final = re.compile(r"[1-9][0-9]{0,8}")
_STATUS: Final = {
    "invalid_request": (400, ErrorKind.INVALID_REQUEST),
    "policy_denied": (403, ErrorKind.POLICY_DENIED),
    "not_found": (404, ErrorKind.NOT_FOUND),
    "conflict": (409, ErrorKind.CONFLICT),
    "busy": (503, ErrorKind.UNAVAILABLE),
}

Json = dict[str, object]


def add_routes(app: web.Application) -> None:
    app.router.add_get("/api/v0/projects/{project}/documents", documents)
    app.router.add_get("/api/v0/projects/{project}/documents/{document}", document)
    app.router.add_get("/api/v0/projects/{project}/documents/{document}/history", history)


async def documents(request: web.Request) -> web.Response:
    project = request.match_info["project"]

    def view(store: DocumentStore) -> Json:
        return {"documents": [found.model_dump(mode="json") for found in store.documents(project)]}

    return await _answer(request, view)


async def document(request: web.Request) -> web.Response:
    project, reference = request.match_info["project"], request.match_info["document"]
    raw = request.query.get("revision")
    if raw is not None and _REVISION.fullmatch(raw) is None:
        return api_error(DocumentError("invalid_request", "revision is a revision number, from 1"))
    number = None if raw is None else int(raw)
    return await _answer(request, lambda store: _revision(store, project, reference, number))


async def history(request: web.Request) -> web.Response:
    project, reference = request.match_info["project"], request.match_info["document"]
    return await _answer(request, lambda store: store.history(project, reference))


async def _answer(request: web.Request, view: Callable[[DocumentStore], Json]) -> web.Response:
    """The view as JSON, read and encoded in a thread, once the project is known."""
    projects = request.app[api.STORE]
    store = request.app[DOCUMENTS]
    project = request.match_info["project"]
    reference = request.match_info.get("document")
    if reference is not None and not _reference(reference):
        return api_error(
            DocumentError("invalid_request", "a document is named by its ID or its name")
        )

    def read() -> str:
        projects.get(project)  # a project that does not exist is not found
        return api.dumps(view(store))

    try:
        text = await asyncio.to_thread(read)
    except DocumentError as error:
        return api_error(error)
    return web.Response(
        text=text, content_type="application/json", headers={hdrs.CACHE_CONTROL: "no-store"}
    )


def _reference(reference: str) -> bool:
    return (
        _DOCUMENT_ID.fullmatch(reference) is not None
        or NAME.fullmatch(reference.removesuffix(".md")) is not None
    )


def api_error(error: DocumentError) -> web.Response:
    status, kind = _STATUS.get(error.kind, (500, ErrorKind.INTERNAL))
    message = error.message if status != 500 else "internal workspace error"
    return web.json_response(
        {"error": {"kind": kind.value, "message": message}},
        status=status,
        headers={hdrs.CACHE_CONTROL: "no-store"},
    )


def _revision(store: DocumentStore, project: str, reference: str, number: int | None) -> Json:
    info = store.document(project, reference)
    revision = store.revision(project, info.document_id, number)
    mapped: Mapped | None
    try:
        mapped = store.mapped(project, info.document_id, revision.info.number)
    except DocumentError as error:
        if error.kind != "internal":
            raise
        mapped = None
    stored = revision.block_map
    entries = cast(list[Json], stored.get("blocks", []))
    depth: dict[str, int] = {}
    blocks: list[Json] = []
    for index, entry in enumerate(entries):
        parent = entry.get("parent")
        block_id = str(entry["id"])
        depth[block_id] = 0 if parent is None else depth.get(str(parent), 0) + 1
        texts = (
            [mapped.parsed.spans[span].tex for span in mapped.parsed.blocks[index].spans]
            if mapped is not None
            else None
        )
        spans = cast(list[Json], entry.get("spans", []))
        blocks.append(
            {
                "id": block_id,
                "kind": entry["kind"],
                "parent": parent,
                "depth": depth[block_id],
                "lines": entry["lines"],
                "status": entry["status"],
                "structural": entry.get("structural"),
                "attrs": entry.get("attrs", {}),
                "math": [
                    {
                        "id": span["id"],
                        "kind": span["kind"],
                        "status": span["status"],
                        "tex": None if texts is None else texts[position],
                    }
                    for position, span in enumerate(spans)
                ],
            }
        )
    front = mapped.parsed.front_matter if mapped is not None else None
    return {
        "document": info.model_dump(mode="json"),
        "revision": revision.info.model_dump(mode="json"),
        "text": revision.text,
        "title": None if front is None else front.title,
        "macros": {} if front is None else front.macros,
        "matched": mapped is not None,
        "blocks": blocks,
        "removed": stored.get("removed", []),
        "removed_math": stored.get("removed_spans", []),
    }
