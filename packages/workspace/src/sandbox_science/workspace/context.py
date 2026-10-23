# SPDX-License-Identifier: MIT
"""What every feature's routes share: the stores, the worker and the timeouts.

``create_app`` sets one ``WorkspaceContext`` as ``CONTEXT``. A route reads it from
``request.app[CONTEXT]``, checks the IDs in its path, reads a write's body with
``json_body`` or ``read_body``, uses ``requests`` for scoped store work in a thread
and JSON responses, or reaches the worker under a timeout. Failures are raised and
answered by the guard through ``errors``.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import Final, cast

from aiohttp import hdrs, web
from pydantic import TypeAdapter, ValidationError

from sandbox_science.contracts import ErrorKind, Identifier
from sandbox_science.orchestrator.documents import USER, DocumentStore
from sandbox_science.orchestrator.jobstate import JobState
from sandbox_science.orchestrator.projects import ProjectError, ProjectStore, SessionRecord
from sandbox_science.orchestrator.worker_client import WorkerClient

from .errors import TooLargeError

_IDENTIFIER: Final = TypeAdapter[str](Identifier)


MAX_RUN_BYTES: Final = 64 * 1024 * 1024


@dataclass(frozen=True)
class Timeouts:
    """Seconds the workspace waits for its reads and for the worker."""

    call: float = 6.0
    """A kernel, plan, plan-edit, ledger read or provisioning request as a whole."""
    upload: float = 300.0
    """One upload or attachment delivery, including transfer admission; observations stay short."""
    comparison: float = 10.0
    """A lockfile comparison or a revision's package page as a whole."""
    worker_call: float = 5.0
    """One provisioning observation from the worker."""
    worker_comparison: float = 9.0
    """One comparison or package page from the worker, within its eight-second budget."""
    environment: float = 2.0
    """The worker's record of an environment, which the chat reads at every poll while a
    message waits for its run: it never holds the chat up for long."""


TIMEOUTS: Final = Timeouts()


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
    max_run_bytes: int = MAX_RUN_BYTES
    uploads: asyncio.Lock = field(default_factory=asyncio.Lock)
    """One upload, original download or delivery is materialized at a time across tabs."""


CONTEXT: Final = web.AppKey("workspace_context", WorkspaceContext)
LOOPBACK_NAMES: Final = ("localhost", "127.0.0.1", "[::1]")

USER_ID: Final = USER.user
"""The workspace's one user, as plan records name it: the same for every login, as the
documents' USER author is."""


def listener(request: web.Request) -> tuple[str, int] | None:
    """The address and port the request came in on."""
    transport = request.transport
    sockname: object = transport.get_extra_info("sockname") if transport is not None else None
    if not isinstance(sockname, tuple):
        return None
    address = cast(tuple[object, ...], sockname)
    if len(address) < 2 or not isinstance(address[0], str) or not isinstance(address[1], int):
        return None
    return address[0], address[1]


def own_hosts(request: web.Request) -> frozenset[str]:
    """The ``Host`` values that name this server: its loopback names and its address."""
    address = listener(request)
    if address is None:
        return frozenset()
    ip, port = address
    names = {*LOOPBACK_NAMES, f"[{ip}]" if ":" in ip else ip}
    hosts = {f"{name}:{port}" for name in names}
    if port == 80:
        hosts.update(names)
    return frozenset(hosts)


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
