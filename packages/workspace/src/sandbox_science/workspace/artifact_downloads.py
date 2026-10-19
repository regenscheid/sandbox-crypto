# SPDX-License-Identifier: MIT
"""Authenticated downloads of immutable retained artifacts, independent of workers."""

from __future__ import annotations

import asyncio
from typing import Final
from urllib.parse import quote

from aiohttp import hdrs, web

from sandbox_science.contracts import ErrorKind
from sandbox_science.contracts.artifact_inventory import ArtifactLink
from sandbox_science.orchestrator.aio import held, threaded
from sandbox_science.orchestrator.artifacts import ArtifactInventory
from sandbox_science.orchestrator.projects import ProjectError

from .context import CONTEXT, identifier

_TRANSFERS: Final = web.AppKey("artifact_downloads", asyncio.Semaphore)
_CHUNK: Final = 512 * 1024


def add_routes(app: web.Application) -> None:
    app[_TRANSFERS] = asyncio.Semaphore(2)
    app.router.add_get(
        "/api/v0/projects/{project}/artifacts/{artifact}/revisions/{revision}/content",
        download,
    )


def _link(request: web.Request) -> ArtifactLink:
    raw = request.match_info["revision"]
    sha256 = request.query.get("sha256", "")
    if (
        set(request.query) != {"sha256"}
        or not raw.isascii()
        or not raw.isdigit()
        or len(raw) > 9
        or int(raw) < 1
        or len(sha256) != 64
        or not set(sha256) <= set("0123456789abcdef")
    ):
        raise ProjectError(ErrorKind.INVALID_REQUEST, "an exact revision and sha256 are required")
    return ArtifactLink(
        project_id=identifier(request, "project"),
        artifact_id=identifier(request, "artifact"),
        mode="pinned",
        revision=int(raw),
        sha256=sha256,
    )


def _range(request: web.Request, size: int, etag: str) -> tuple[int, int, int]:
    if hdrs.RANGE not in request.headers or request.headers.get(hdrs.IF_RANGE, etag) != etag:
        return 0, size, 200
    try:
        if request.headers[hdrs.RANGE].strip() == "bytes=-0":
            raise ValueError("empty suffix range")
        selected = request.http_range
        start = selected.start or 0
        if start < 0:
            start = max(0, size + start)
        end = min(size, selected.stop) if selected.stop is not None else size
        if start >= end or start >= size:
            raise ValueError("unsatisfiable range")
    except ValueError:
        raise web.HTTPRequestRangeNotSatisfiable(
            headers={hdrs.CONTENT_RANGE: f"bytes */{size}"}
        ) from None
    return start, end, 206


async def download(request: web.Request) -> web.StreamResponse:
    """Verify before headers, then stream a bounded range with owned file cleanup."""
    context = request.app[CONTEXT]
    link = _link(request)
    response: web.StreamResponse | None = None
    try:
        async with asyncio.timeout(300), request.app[_TRANSFERS]:
            await threaded(context.projects.get, link.project_id)
            inventory = ArtifactInventory(context.documents)
            revision = await threaded(inventory.resolve, link.project_id, link)
            reference = revision.reference
            etag = '"' + reference.sha256 + '"'
            start, end, status = _range(request, reference.size, etag)
            manager = inventory.open_content(link.project_id, reference)
            # Cancellation during verification still owns the returned open file.
            stream, cancelled = await held(asyncio.to_thread(manager.__enter__))
            try:
                if cancelled:
                    raise asyncio.CancelledError
                headers = {
                    hdrs.CONTENT_TYPE: "application/octet-stream",
                    hdrs.CONTENT_LENGTH: str(end - start),
                    hdrs.CONTENT_DISPOSITION: "attachment; filename*=UTF-8''"
                    + quote(revision.path.rsplit("/", 1)[-1], safe=""),
                    hdrs.CACHE_CONTROL: "no-store",
                    hdrs.ACCEPT_RANGES: "bytes",
                    hdrs.ETAG: etag,
                    "X-Content-SHA256": reference.sha256,
                    "X-Content-Type-Options": "nosniff",
                }
                if status == 206:
                    headers[hdrs.CONTENT_RANGE] = f"bytes {start}-{end - 1}/{reference.size}"
                response = web.StreamResponse(status=status, headers=headers)
                await response.prepare(request)
                if request.method != hdrs.METH_HEAD:
                    await threaded(stream.seek, start)
                    remaining = end - start
                    while remaining:
                        chunk = await threaded(stream.read, min(_CHUNK, remaining))
                        if not chunk:
                            raise OSError("retained artifact was truncated while streaming")
                        await response.write(chunk)
                        remaining -= len(chunk)
                await response.write_eof()
            finally:
                await threaded(manager.__exit__, None, None, None)
            return response
    except (OSError, TimeoutError, asyncio.CancelledError) as error:
        if response is not None and response.prepared:
            response.force_close()
            if request.transport is not None:
                request.transport.close()
            if isinstance(error, asyncio.CancelledError):
                raise
            return response
        if isinstance(error, asyncio.CancelledError):
            raise
        raise ProjectError(
            ErrorKind.UNAVAILABLE, "artifact download failed; retry the file"
        ) from None
