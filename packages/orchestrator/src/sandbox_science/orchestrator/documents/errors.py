# SPDX-License-Identifier: MIT
"""The errors of living documents."""

from __future__ import annotations

from typing import Final

INVALID_REQUEST: Final = "invalid_request"
NOT_FOUND: Final = "not_found"
CONFLICT: Final = "conflict"
POLICY_DENIED: Final = "policy_denied"
UNCHANGED_BLOCKS: Final = "patch_changes_unnamed_blocks"
INTERNAL: Final = "internal"


class DocumentError(Exception):
    """A request the document store refuses, with a kind the tool and the API report."""

    def __init__(self, kind: str, message: str, details: dict[str, object] | None = None) -> None:
        super().__init__(message)
        self.kind = kind
        self.message = message
        self.details = details or {}
