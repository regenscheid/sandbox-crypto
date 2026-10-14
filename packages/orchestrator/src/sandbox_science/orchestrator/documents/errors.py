# SPDX-License-Identifier: MIT
"""The errors of living documents: the control store's, and one kind of their own."""

from __future__ import annotations

from typing import Final

from sandbox_science.orchestrator.control.errors import (
    BUSY,
    CONFLICT,
    INTERNAL,
    INVALID_REQUEST,
    NOT_FOUND,
    POLICY_DENIED,
    ControlError,
)

UNCHANGED_BLOCKS: Final = "patch_changes_unnamed_blocks"

DocumentError = ControlError
"""The control store's error, by the name most repositories still use."""

__all__ = [
    "BUSY",
    "CONFLICT",
    "INTERNAL",
    "INVALID_REQUEST",
    "NOT_FOUND",
    "POLICY_DENIED",
    "UNCHANGED_BLOCKS",
    "ControlError",
    "DocumentError",
]
