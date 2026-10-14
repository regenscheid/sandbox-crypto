# SPDX-License-Identifier: MIT
"""Living document views and explicit user changes (LD-13, LD-14, LD-19).

Reads use a read-only store. Writes use a separate lazy writer, strict action schemas,
server-derived user attribution and the application's authenticated same-origin guard.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Callable
from typing import Annotated, Final, Literal, cast

from aiohttp import hdrs, web
from pydantic import Field, TypeAdapter, ValidationError

from sandbox_science.contracts.model import Contract
from sandbox_science.orchestrator.documents import DocumentError, DocumentStore, Mapped
from sandbox_science.orchestrator.documents.annotations import Annotations
from sandbox_science.orchestrator.documents.models import NAME, USER
from sandbox_science.orchestrator.documents.operations import Operation, unlay

from . import api, errors
from .context import json_body

DOCUMENTS: Final = web.AppKey("documents", DocumentStore)
WRITER: Final = web.AppKey("document_writer", DocumentStore)


_DOCUMENT_ID: Final = re.compile(r"doc-[0-9a-f]{24}")
_REVISION: Final = re.compile(r"[1-9][0-9]{0,8}")
Json = dict[str, object]


class _Change(Contract):
    base: int = Field(ge=1, le=999999999, strict=True)
    operation_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
    reason: str = Field(min_length=1, max_length=1000)


class SourceEdit(_Change):
    action: Literal["source"]
    text: str


class BlockEdit(_Change):
    action: Literal["patch"]
    operations: tuple[Operation, ...] = Field(min_length=1, max_length=64)


class Revert(_Change):
    action: Literal["revert"]
    target: int = Field(ge=1, le=999999999, strict=True)


class Promote(_Change):
    action: Literal["promote"]
    name: str = Field(min_length=1, max_length=67)


class ApplyPatch(_Change):
    action: Literal["apply_patch"]
    patch_id: str = Field(pattern=r"^patch-[0-9a-f]{24}$")


_CHANGE: Final = TypeAdapter(
    Annotated[SourceEdit | BlockEdit | Revert | Promote | ApplyPatch, Field(discriminator="action")]
)


def add_routes(app: web.Application) -> None:
    app.router.add_get("/api/v0/projects/{project}/documents", documents)
    app.router.add_get("/api/v0/projects/{project}/documents/{document}", document)
    app.router.add_get("/api/v0/projects/{project}/documents/{document}/history", history)
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
        return api_error(DocumentError("invalid_request", "invalid document revision"))
    return await answer(
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
        return api_error(DocumentError("invalid_request", "invalid entry cursor"))
    return await answer(
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
    return await answer(
        request,
        lambda store: Annotations(store).change(
            request.match_info["project"], request.match_info["document"], raw, USER
        ),
        writer=True,
    )


async def preview(request: web.Request) -> web.Response:
    return await answer(
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
        value = _CHANGE.validate_python(raw)
    except ValidationError:
        return api_error(DocumentError("invalid_request", "invalid document change"))
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

    return await answer(request, write, writer=True)


async def documents(request: web.Request) -> web.Response:
    project = request.match_info["project"]

    def view(store: DocumentStore) -> Json:
        return {"documents": [found.model_dump(mode="json") for found in store.documents(project)]}

    return await answer(request, view)


async def document(request: web.Request) -> web.Response:
    project, reference = request.match_info["project"], request.match_info["document"]
    raw = request.query.get("revision")
    if raw is not None and _REVISION.fullmatch(raw) is None:
        return api_error(DocumentError("invalid_request", "revision is a revision number, from 1"))
    number = None if raw is None else int(raw)
    return await answer(request, lambda store: _revision(store, project, reference, number))


async def history(request: web.Request) -> web.Response:
    project, reference = request.match_info["project"], request.match_info["document"]
    return await answer(request, lambda store: store.history(project, reference))


async def answer(
    request: web.Request, view: Callable[[DocumentStore], Json], *, writer: bool = False
) -> web.Response:
    """The view as JSON, read and encoded in a thread, once the project is known."""
    projects = request.app[api.STORE]
    store = request.app[WRITER if writer else DOCUMENTS]
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
    """A document store error, by the workspace's one mapping (errors.py)."""
    return errors.to_response(error)


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
    from sandbox_science.orchestrator.documents.project_macros import view

    context = view(
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
    }
