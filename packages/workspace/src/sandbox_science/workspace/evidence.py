# SPDX-License-Identifier: MIT
"""Backpressured downloads of one run's stored evidence, including interrupted runs."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Final

from aiohttp import web
from pydantic import TypeAdapter, ValidationError

from sandbox_science.orchestrator.aio import threaded
from sandbox_science.orchestrator.context.history.export import EvidenceExport
from sandbox_science.orchestrator.documents import DocumentError
from sandbox_science.orchestrator.projects import RunName

from .context import CONTEXT, identifier, session_record
from .documents import DOCUMENTS

_RUN: Final = TypeAdapter[str](RunName)
_ACTIVE: Final = web.AppKey("evidence_exports", set[object])
MAX_EXPORTS: Final = 2


def add_routes(app: web.Application) -> None:
    app[_ACTIVE] = set()
    app.router.add_get(
        "/api/v0/projects/{project}/sessions/{session}/runs/{run}/evidence",
        download,
        allow_head=False,
    )


async def download(request: web.Request) -> web.StreamResponse:
    if request.query or request.can_read_body:
        raise DocumentError("invalid_request", "evidence downloads take no query or body")
    project, session = identifier(request, "project"), identifier(request, "session")
    try:
        run = _RUN.validate_python(request.match_info["run"], strict=True)
    except ValidationError:
        raise DocumentError("invalid_request", "invalid run name") from None
    active = request.app[_ACTIVE]
    if len(active) >= MAX_EXPORTS:
        raise web.HTTPTooManyRequests(text="Two evidence downloads are already active.")
    slot = object()
    active.add(slot)
    lines: Iterator[bytes] | None = None
    try:
        context = request.app[CONTEXT]
        store = request.app[DOCUMENTS]

        def prepare() -> EvidenceExport:
            session_record(context.projects, project, session)
            return EvidenceExport(store, project, session, run)

        export = await threaded(prepare)
        lines = export.lines()
        response = web.StreamResponse(
            headers={
                "Content-Type": "application/x-ndjson",
                "Content-Disposition": f'attachment; filename="{run}.evidence.ndjson"',
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
            }
        )
        await response.prepare(request)
        while (line := await threaded(_next, lines)) is not None:
            await response.write(line)
        await response.write_eof()
        return response
    finally:
        # next() closes every DB read before returning. Cancellation cannot strand a
        # transaction or a network-sized in-memory buffer. The store owns the thread.
        active.discard(slot)


def _next(lines: Iterator[bytes]) -> bytes | None:
    return next(lines, None)
