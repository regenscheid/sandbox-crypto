# SPDX-License-Identifier: MIT
"""Visible working-state ceilings; original evidence remains in the archive."""

from sandbox_science.orchestrator.control import ControlError

METADATA_BYTES = 8 * 1024 * 1024
METADATA_ROWS = 8192
WORKING_BYTES = 64 * 1024 * 1024
"""Per-agent active replay payload; token geometry normally triggers folding first."""


class WorkingMemoryLimitError(ControlError):
    """A run must pause rather than silently discard irreducible research state."""

    def __init__(self, message: str) -> None:
        super().__init__("unsupported", message, {"reason": "memory_limit"})
