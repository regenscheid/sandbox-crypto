# SPDX-License-Identifier: MIT
"""The API's one error mapping: an error kind's HTTP status, and the error body.

An error is ``{"error": {"kind", "message"}}`` with ``Cache-Control: no-store``. Every
route raises ``ProjectError`` or lets ``WorkerAPIError`` or ``TimeoutError`` through;
the application's guard answers them here. Anything else is a 500 with a fixed message,
and is logged.

A worker refusal the browser can act on keeps its kind and the worker's message. Any
other worker failure is 503 with a fixed message, since transport errors name the
worker's address; the cause is logged as a warning. A damaged project or run record
is 503 with a fixed message too, since the store's message names the record's path;
it is logged as an error.
"""

from __future__ import annotations

import logging
from typing import Final

from aiohttp import hdrs, web

from sandbox_science.contracts import ErrorKind
from sandbox_science.orchestrator.documents import DocumentError
from sandbox_science.orchestrator.projects import ProjectError
from sandbox_science.orchestrator.worker_client import WorkerAPIError

_log = logging.getLogger(__name__)

STATUS: Final = {
    ErrorKind.INVALID_REQUEST: 400,
    ErrorKind.POLICY_DENIED: 403,
    ErrorKind.NOT_FOUND: 404,
    ErrorKind.CONFLICT: 409,
    ErrorKind.UNSUPPORTED: 422,
    ErrorKind.UNAVAILABLE: 503,
    ErrorKind.TIMED_OUT: 503,
}
"""The status of each error kind; every other kind is a 500."""

WORKER_REFUSALS: Final = frozenset(
    {
        ErrorKind.INVALID_REQUEST,
        ErrorKind.POLICY_DENIED,
        ErrorKind.NOT_FOUND,
        ErrorKind.CONFLICT,
        ErrorKind.UNSUPPORTED,
    }
)
"""Worker refusals answered with the worker's own kind and message."""

INTERNAL_MESSAGE: Final = "internal workspace error"


def json_error(
    status: int,
    kind: ErrorKind,
    message: str,
    *,
    fields: dict[str, object] | None = None,
    extra: dict[str, object] | None = None,
) -> web.Response:
    """An error body with an explicit status; ``fields`` join the error object, ``extra``
    the body."""
    body: dict[str, object] = {"error": {"kind": kind.value, "message": message, **(fields or {})}}
    return web.json_response(
        {**body, **(extra or {})}, status=status, headers={hdrs.CACHE_CONTROL: "no-store"}
    )


def error(
    kind: ErrorKind,
    message: str,
    *,
    fields: dict[str, object] | None = None,
    extra: dict[str, object] | None = None,
) -> web.Response:
    """The error response for ``kind``, with the status ``STATUS`` gives it."""
    status = STATUS.get(kind)
    if status is None:
        status, kind, message = 500, ErrorKind.INTERNAL, INTERNAL_MESSAGE
    return json_error(status, kind, message, fields=fields, extra=extra)


def document_kind(caught: DocumentError) -> ErrorKind:
    """A document store error's kind: ``busy`` is unavailable."""
    if caught.kind == "busy":
        return ErrorKind.UNAVAILABLE
    try:
        return ErrorKind(caught.kind)
    except ValueError:
        return ErrorKind.INTERNAL


def log_worker_failure(caught: WorkerAPIError) -> None:
    """Log a worker failure that the browser sees only as a fixed 503."""
    _log.warning("worker request failed (%s): %s", caught.kind.value, caught.message)


def to_response(caught: Exception, *, extra: dict[str, object] | None = None) -> web.Response:
    """The error response for an exception a route raised."""
    match caught:
        case ProjectError() if caught.kind is ErrorKind.INTERNAL:
            _log.error("workspace API request failed", exc_info=caught)
            kind, message = ErrorKind.UNAVAILABLE, "a project record is unreadable"
        case ProjectError():
            kind, message = caught.kind, caught.message
        case WorkerAPIError() if caught.kind in WORKER_REFUSALS:
            kind, message = caught.kind, caught.message
        case WorkerAPIError():
            log_worker_failure(caught)
            kind, message = ErrorKind.UNAVAILABLE, "the worker is unavailable"
        case TimeoutError():
            kind, message = ErrorKind.UNAVAILABLE, "the request timed out"
        case DocumentError():  # the control store, such as one in a newer schema version
            kind, message = document_kind(caught), caught.message
            if caught.kind == "unavailable":
                _log.error("the control store is unavailable: %s", caught.message)
        case _:
            kind, message = ErrorKind.INTERNAL, INTERNAL_MESSAGE
    if kind not in STATUS:
        _log.error("workspace API request failed", exc_info=caught)
    return error(kind, message, extra=extra)
