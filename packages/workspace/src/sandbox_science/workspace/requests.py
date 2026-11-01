# SPDX-License-Identifier: MIT
"""Shared execution for scoped workspace reads and explicit user writes.

Validation of document names, session membership, cursors and action bodies belongs
in the feature routes. The application guard translates exceptions through ``errors``.
Read deadlines are opt-in: cancelling a thread await cannot stop a write already in
progress, so user writes retain their existing lack of a request deadline.
"""

from __future__ import annotations

import asyncio
import json
import math
from collections.abc import Callable
from typing import Literal, cast

from aiohttp import web

from sandbox_science.contracts import Contract
from sandbox_science.orchestrator.documents import DocumentStore

from .context import CONTEXT, json_answer

Json = dict[str, object]
Access = Literal["read", "write"]


def document_store(request: web.Request, *, access: Access = "read") -> DocumentStore:
    """The read-only connection, or the separate writer for an explicit user action."""
    context = request.app[CONTEXT]
    return context.writer if access == "write" else context.documents


async def execute[T](
    request: web.Request,
    call: Callable[[], T],
    *,
    project: str | None = None,
    timeout: float | None = None,
) -> T:
    """Check an optional project scope and run the operation in the same thread.

    A deadline bounds the await only; it never implies that the thread has stopped.
    Callers must not use it to bound a write's lifetime.
    """
    context = request.app[CONTEXT]

    def run() -> T:
        if project is not None:
            context.projects.get(project)
        return call()

    async with asyncio.timeout(timeout):
        return await asyncio.to_thread(run)


async def encoded_response(
    request: web.Request,
    view: Callable[[], str],
    *,
    project: str | None = None,
    timeout: float | None = None,
) -> web.Response:
    """Run a view with its existing JSON encoder and return an uncached response."""
    return json_answer(await execute(request, view, project=project, timeout=timeout))


async def json_response(
    request: web.Request,
    view: Callable[[], Json | Contract],
    *,
    project: str | None = None,
    timeout: float | None = None,
) -> web.Response:
    """Read and JSON-encode in one thread, with the workspace's common error guard."""

    def encode() -> str:
        found = view()
        return dumps(found.model_dump(mode="json") if isinstance(found, Contract) else found)

    return await encoded_response(request, encode, project=project, timeout=timeout)


async def document_answer(
    request: web.Request,
    view: Callable[[DocumentStore], Json | Contract],
    *,
    access: Access = "read",
) -> web.Response:
    """A view or explicit user change in the path's project, from the selected store."""
    store = document_store(request, access=access)
    return await json_response(
        request,
        lambda: view(store),
        project=request.match_info["project"],
    )


def dumps(data: object) -> str:
    """JSON a browser parses: ASCII, with lone surrogates escaped and no NaN or infinity,
    which a record can hold (a float a tool's JSON carried) but JSON cannot."""
    try:
        return json.dumps(data, allow_nan=False)
    except ValueError:
        return json.dumps(_finite(data), allow_nan=False)


def _finite(value: object) -> object:
    if isinstance(value, float) and not math.isfinite(value):
        return str(value)
    if isinstance(value, dict):
        return {key: _finite(item) for key, item in cast(dict[str, object], value).items()}
    if isinstance(value, list):
        return [_finite(item) for item in cast(list[object], value)]
    return value
