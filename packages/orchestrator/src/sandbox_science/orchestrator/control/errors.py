# SPDX-License-Identifier: MIT
"""The control store's error and its kinds, which the tools and the API report."""

from __future__ import annotations

from typing import Final

INVALID_REQUEST: Final = "invalid_request"
NOT_FOUND: Final = "not_found"
CONFLICT: Final = "conflict"
POLICY_DENIED: Final = "policy_denied"
BUSY: Final = "busy"
"""Another change held the store for longer than a change waits: try again."""
UNAVAILABLE: Final = "unavailable"
"""The file is not a control store this version can use, such as one in another schema
version: the message says what to do."""
INTERNAL: Final = "internal"


class ControlError(Exception):
    """A request the control store or one of its repositories refuses, with a kind."""

    def __init__(self, kind: str, message: str, details: dict[str, object] | None = None) -> None:
        super().__init__(message)
        self.kind = kind
        self.message = message
        self.details = details or {}
