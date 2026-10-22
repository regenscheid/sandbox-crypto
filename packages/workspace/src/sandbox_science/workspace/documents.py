# SPDX-License-Identifier: MIT
"""Living document views and explicit user changes (LD-13, LD-14, LD-19).

Reads use a read-only store. Writes use a separate lazy writer, strict action schemas,
server-derived user attribution and the application's authenticated same-origin guard.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Final, Literal, cast

from aiohttp import hdrs, web
from pydantic import ValidationError

from sandbox_science.orchestrator.documents import DocumentError, DocumentStore, Mapped
from sandbox_science.orchestrator.documents.annotations import Annotations
from sandbox_science.orchestrator.documents.models import NAME, USER
from sandbox_science.orchestrator.documents.operations import unlay
from sandbox_science.orchestrator.documents.project_macros import view as macro_view
from sandbox_science.orchestrator.figurechecks import shown as figure_checks

from . import errors
from .context import json_body
from .domain_contracts import (
    DOCUMENT_CHANGE,
    BlockEdit,
    Decline,
    Promote,
    Revert,
    SourceEdit,
)
from .requests import document_answer, document_store, execute

_DOCUMENT_ID: Final = re.compile(r"doc-[0-9a-f]{24}")
_REVISION: Final = re.compile(r"[1-9][0-9]{0,8}")
Json = dict[str, object]


def add_routes(app: web.Application) -> None:
    app.router.add_get("/api/v0/projects/{project}/documents", documents)
    app.router.add_get("/api/v0/projects/{project}/documents/{document}", document)
    app.router.add_get("/api/v0/projects/{project}/documents/{document}/history", history)
    app.router.add_get("/api/v0/projects/{project}/documents/{document}/source", source)
    app.router.add_get("/api/v0/projects/{project}/documents/{document}/comments", comments)
    app.router.add_post("/api/v0/projects/{project}/documents/{document}/comments", comment_change)
    app.router.add_get(
        "/api/v0/projects/{project}/documents/{document}/comments/{thread}", comment_thread
    )
    app.router.add_post("/api/v0/projects/{project}/documents/{document}/changes", change)
    app.router.add_get(
        "/api/v0/projects/{project}/documents/{document}/patches/{patch}/preview", preview
    )


async def comments(request: web.Request) -> web.Response:
    raw = request.query.get("revision")
    if raw is not None and _REVISION.fullmatch(raw) is None:
        return errors.to_response(DocumentError("invalid_request", "invalid document revision"))
    return await _answer(
        request,
        lambda store: Annotations(store).list(
            request.match_info["project"],
            request.match_info["document"],
            revision=int(raw) if raw else None,
            after=request.query.get("after"),
        ),
    )


async def comment_thread(request: web.Request) -> web.Response:
    raw = request.query.get("before")
    if raw is not None and _REVISION.fullmatch(raw) is None:
        return errors.to_response(DocumentError("invalid_request", "invalid entry cursor"))
    return await _answer(
        request,
        lambda store: Annotations(store).read_thread(
            request.match_info["project"],
            request.match_info["document"],
            request.match_info["thread"],
            int(raw) if raw else None,
        ),
    )


async def comment_change(request: web.Request) -> web.Response:
    raw = await json_body(request, "annotation change")
    return await _answer(
        request,
        lambda store: Annotations(store).change(
            request.match_info["project"], request.match_info["document"], raw, USER
        ),
        access="write",
    )


async def preview(request: web.Request) -> web.Response:
    return await _answer(
        request,
        lambda store: store.preview_patch(
            request.match_info["project"],
            request.match_info["document"],
            request.match_info["patch"],
        ),
    )


async def change(request: web.Request) -> web.Response:
    # JSON escaping can expand the one-MiB source bound; other routes keep 64 KiB.
    raw = await json_body(request, "document change", limit=8 * 1024 * 1024)
    try:
        value = DOCUMENT_CHANGE.validate_python(raw)
    except ValidationError:
        return errors.to_response(DocumentError("invalid_request", "invalid document change"))
    project, reference = request.match_info["project"], request.match_info["document"]

    def write(store: DocumentStore) -> Json:
        if isinstance(value, SourceEdit):
            result = store.edit_source(
                project,
                reference,
                text=value.text,
                base=value.base,
                author=USER,
                reason=value.reason,
                operation_id=value.operation_id,
            )
        elif isinstance(value, BlockEdit):
            result = store.patch(
                project,
                reference,
                operations=[op.model_dump(mode="json", by_alias=True) for op in value.operations],
                base=value.base,
                author=USER,
                reason=value.reason,
                operation_id=value.operation_id,
            )
        elif isinstance(value, Revert):
            result = store.revert(
                project,
                reference,
                target=value.target,
                base=value.base,
                author=USER,
                reason=value.reason,
                operation_id=value.operation_id,
            )
        elif isinstance(value, Decline):
            result = store.decide(project, reference, value.patch_id, accept=False, author=USER)
        elif isinstance(value, Promote):
            result = store.promote(
                project,
                reference,
                name=value.name,
                base=value.base,
                author=USER,
                reason=value.reason,
                operation_id=value.operation_id,
            )
        else:
            result = store.apply_patch(
                project,
                reference,
                value.patch_id,
                base=value.base,
                author=USER,
                reason=value.reason,
                operation_id=value.operation_id,
            )
        return result.model_dump(mode="json")

    return await _answer(request, write, access="write")


async def documents(request: web.Request) -> web.Response:
    project = request.match_info["project"]

    def view(store: DocumentStore) -> Json:
        return {"documents": [found.model_dump(mode="json") for found in store.documents(project)]}

    return await _answer(request, view)


async def document(request: web.Request) -> web.Response:
    project, reference = request.match_info["project"], request.match_info["document"]
    raw = request.query.get("revision")
    if raw is not None and _REVISION.fullmatch(raw) is None:
        return errors.to_response(
            DocumentError("invalid_request", "revision is a revision number, from 1")
        )
    number = None if raw is None else int(raw)
    return await _answer(request, lambda store: _revision(store, project, reference, number))


async def source(request: web.Request) -> web.Response:
    """A revision's stored Markdown as a file to save: the export is the source (LD-38)."""
    project, reference = request.match_info["project"], request.match_info["document"]
    raw = request.query.get("revision")
    if raw is not None and _REVISION.fullmatch(raw) is None:
        return errors.to_response(
            DocumentError("invalid_request", "revision is a revision number, from 1")
        )
    if not _reference(reference):
        return errors.to_response(
            DocumentError("invalid_request", "a document is named by its ID or its name")
        )
    store = document_store(request)

    def read() -> tuple[str, str]:
        info = store.document(project, reference)
        revision = store.revision(project, info.document_id, None if raw is None else int(raw))
        return f"{info.name}-r{revision.info.number}.md", revision.text

    name, text = await execute(request, read, project=project)
    return web.Response(
        body=text.encode("utf-8"),
        headers={
            hdrs.CONTENT_TYPE: "text/markdown; charset=utf-8",
            # The name is the document's (NAME: letters, digits, '.', '_', '-') and a number.
            hdrs.CONTENT_DISPOSITION: f'attachment; filename="{name}"',
            hdrs.CACHE_CONTROL: "no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )


async def history(request: web.Request) -> web.Response:
    project, reference = request.match_info["project"], request.match_info["document"]
    return await _answer(request, lambda store: store.history(project, reference))


async def _answer(
    request: web.Request,
    view: Callable[[DocumentStore], Json],
    *,
    access: Literal["read", "write"] = "read",
) -> web.Response:
    reference = request.match_info.get("document")
    if reference is not None and not _reference(reference):
        return errors.to_response(
            DocumentError("invalid_request", "a document is named by its ID or its name")
        )
    return await document_answer(request, view, access=access)


def _reference(reference: str) -> bool:
    return (
        _DOCUMENT_ID.fullmatch(reference) is not None
        or NAME.fullmatch(reference.removesuffix(".md")) is not None
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
                "source": None
                if mapped is None
                else unlay(
                    mapped.parsed.blocks[index].text, mapped.parsed.blocks[index].continuation
                ),
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
    context = macro_view(
        store,
        project,
        document=info.document_id,
        revision=revision.info.number,
        historical=number is not None,
    )
    return {
        "document": info.model_dump(mode="json"),
        "revision": revision.info.model_dump(mode="json"),
        "text": revision.text,
        "title": None if front is None else front.title,
        "macros": context["macros"],
        "macro_context": context,
        "matched": mapped is not None,
        "blocks": blocks,
        "removed": stored.get("removed", []),
        "removed_math": stored.get("removed_spans", []),
        # Its references to recorded files, each pinned, with the file's newer revision
        # when there is one, or with why it is not (figures spec, FG-8, FG-9).
        "pins": store.pins(project, info.document_id, revision.info.number),
        # The checks of its figures, when this revision was checked as the current one
        # (FG-23).
        "figure_checks": figure_checks(store, "document", info.document_id, revision.info.number),
    }
