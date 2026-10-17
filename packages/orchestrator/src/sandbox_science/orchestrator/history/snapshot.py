# SPDX-License-Identifier: MIT
"""Bounded inline snapshots with explicit references for paged historical collections."""

from __future__ import annotations

import json
from collections.abc import Iterable
from typing import Any

from pydantic import BaseModel

INLINE_BYTES = 128 * 1024


def inline(values: Iterable[Any], *, limit: int = INLINE_BYTES) -> list[Any] | None:
    """At most ``limit`` bytes; never collect the unbounded input before checking."""
    kept: list[Any] = []
    used = 2
    for value in values:
        if isinstance(value, BaseModel):
            value = value.model_dump(mode="json")
        # Per-item retention/storage already bounds the item. The active result can
        # be larger, but is never duplicated as a historical collection snapshot.
        encoded = json.dumps(value, ensure_ascii=False, allow_nan=False)
        used += len(encoded.encode()) + 2
        if used > limit:
            return None
        kept.append(value)
    return kept
