# SPDX-License-Identifier: MIT
"""Values read from an executor's replies, checked: what an executor sends is untrusted."""

from __future__ import annotations

import contextlib
from dataclasses import dataclass
from datetime import UTC, datetime


def integer(value: object, low: int = -(2**31), high: int = 2**31 - 1) -> int | None:
    """``value`` if it is an integer from ``low`` to ``high``; None otherwise."""
    if isinstance(value, int) and not isinstance(value, bool) and low <= value <= high:
        return value
    return None


def epoch(value: object) -> datetime | None:
    """The time ``value`` seconds after the epoch, or None if it is not one."""
    if isinstance(value, int | float) and not isinstance(value, bool):
        with contextlib.suppress(OverflowError, OSError, ValueError):
            return datetime.fromtimestamp(value, UTC)
    return None


@dataclass(frozen=True)
class Range:
    """A range of a file as the executor read it; ``sha256`` only when it was asked for."""

    size: int
    sha256: str | None
    modified: datetime | None
    offset: int
    data: bytes
    content_b64: str
    eof: bool
    stamp: str | None = None
