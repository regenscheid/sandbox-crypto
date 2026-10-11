# SPDX-License-Identifier: MIT
"""Errors the worker service reports with one of the spec's error kinds."""

from __future__ import annotations

from typing import Final

from sandbox_science.contracts import ErrorInfo, ErrorKind

HTTP_STATUS: Final = {
    ErrorKind.INVALID_REQUEST: 400,
    ErrorKind.UNSUPPORTED: 422,
    ErrorKind.UNAVAILABLE: 503,
    ErrorKind.POLICY_DENIED: 403,
    ErrorKind.NOT_FOUND: 404,
    ErrorKind.CONFLICT: 409,
    ErrorKind.TIMED_OUT: 504,
    ErrorKind.CANCELLED: 409,
    ErrorKind.KERNEL_DIED: 500,
    ErrorKind.EXCEPTION: 422,
    ErrorKind.INTERNAL: 500,
}


class WorkerError(Exception):
    """A failure with an error kind from docs/specs/tools.md."""

    def __init__(self, kind: ErrorKind, message: str) -> None:
        super().__init__(message)
        self.kind = kind
        self.message = message

    @property
    def status(self) -> int:
        return HTTP_STATUS[self.kind]

    def info(self) -> ErrorInfo:
        return ErrorInfo(kind=self.kind, message=self.message)
