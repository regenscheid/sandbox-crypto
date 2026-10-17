# SPDX-License-Identifier: MIT
"""Visible working-state ceilings; original evidence remains in the archive."""

METADATA_BYTES = 8 * 1024 * 1024
METADATA_ROWS = 8192


class WorkingMemoryLimitError(Exception):
    """A run must pause rather than silently discard irreducible research state."""
