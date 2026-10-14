# SPDX-License-Identifier: MIT
"""What every feature's routes share: the stores, the worker and the timeouts.

``create_app`` sets one ``WorkspaceContext`` as ``CONTEXT``. A route reads it from
``request.app[CONTEXT]``, checks the IDs in its path, reads a write's body with
``json_body`` or ``read_body``, does its work in a thread or through the worker under a
timeout, and answers JSON with ``json_answer``. Failures are
raised and answered by the guard through ``errors``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Final

from aiohttp import hdrs, web
from pydantic import TypeAdapter, ValidationError

from sandbox_science.contracts import ErrorKind, Identifier
from sandbox_science.orchestrator.documents import USER, DocumentStore
from sandbox_science.orchestrator.jobstate import JobState
from sandbox_science.orchestrator.projects import ProjectError, ProjectStore, SessionRecord
from sandbox_science.orchestrator.worker_client import WorkerClient

from .api import Timeouts
from .errors import TooLargeError

_IDENTIFIER: Final = TypeAdapter[str](Identifier)


@dataclass(frozen=True)
class WorkspaceContext:
    projects: ProjectStore
    documents: DocumentStore
    """The control store, read-only: GET routes cannot write even through a bug."""
    writer: DocumentStore
    """The same store, for explicit user changes."""
    worker: WorkerClient | None
    job_state: JobState | None
    timeouts: Timeouts


CONTEXT: Final = web.AppKey("workspace_context", WorkspaceContext)

USER_ID: Final = USER.user
"""The workspace's one user, as plan records name it: the same for every login, as the
documents' USER author is."""


def identifier(request: web.Request, name: str) -> str:
    """The path's ``name`` part, checked as an identifier."""
    value = request.match_info[name]
    try:
        return _IDENTIFIER.validate_python(value, strict=True)
    except ValidationError:
        raise ProjectError(ErrorKind.INVALID_REQUEST, f"invalid {name} ID") from None


def session_record(projects: ProjectStore, project: str, session: str) -> SessionRecord:
    """A recorded session of a project; a session of another project is not found."""
    found = projects.get(project).sessions.get(session)
    if found is None:
        raise ProjectError(ErrorKind.NOT_FOUND, "no such session in this project")
    return found


def no_query(request: web.Request, what: str) -> None:
    """Refuse a query or a body on a route that takes its scope from its path only."""
    if request.query or request.can_read_body:
        raise ProjectError(ErrorKind.INVALID_REQUEST, f"{what} accepts only its path scope")


async def read_body(request: web.Request, what: str, *, limit: int | None = None) -> bytes:
    """A write's body, up to ``limit`` bytes or the application's 64 KiB."""
    try:
        source = request if limit is None else request.clone(client_max_size=limit)
        return await source.read()
    except web.HTTPRequestEntityTooLarge:
        raise TooLargeError(f"{what} exceeds its byte limit") from None


async def json_body(request: web.Request, what: str, *, limit: int | None = None) -> object:
    """A write's JSON body; anything else is a 400 that names ``what``."""
    raw = await read_body(request, what, limit=limit)
    try:
        # Decoded as declared (UTF-8 by default) first: json.loads would also guess
        # UTF-16 or skip a byte-order mark in bytes.
        return json.loads(raw.decode(request.charset or "utf-8"))
    except (ValueError, LookupError, RecursionError):  # UnicodeDecodeError is a ValueError
        raise ProjectError(ErrorKind.INVALID_REQUEST, f"invalid {what}") from None


def json_answer(text: str) -> web.Response:
    """A JSON answer that is not cached."""
    return web.Response(
        text=text, content_type="application/json", headers={hdrs.CACHE_CONTROL: "no-store"}
    )
