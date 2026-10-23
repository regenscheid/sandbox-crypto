# SPDX-License-Identifier: MIT
"""Bounded inline snapshots with explicit references for paged historical collections."""

from __future__ import annotations

import json
from collections.abc import Iterable
from typing import Any, cast

from pydantic import BaseModel

from .retention import fits

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
        if not fits(value, limit - used - 2):
            return None
        encoded = json.dumps(value, ensure_ascii=False, allow_nan=False)
        used += len(encoded.encode()) + 2
        if used > limit:
            return None
        kept.append(value)
    return kept


def compact(value: Any, reference: str, *, limit: int = 16384) -> Any:
    """Bound metadata previews, explicitly locating values that remain stored whole."""
    if fits(value, limit):
        return value
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        remaining = limit - 512
        for key, child in cast(dict[str, Any], value).items():
            piece = compact(child, f"{reference}/{key}", limit=max(128, remaining // 2))
            encoded = json.dumps({key: piece}, ensure_ascii=False, allow_nan=False)
            if len(encoded.encode()) > remaining:
                result["_stored_metadata"] = reference
                break
            result[key] = piece
            remaining -= len(encoded.encode()) + 2
        return result
    if isinstance(value, (list, tuple)):
        return {"stored": reference, "items": len(cast(list[object] | tuple[object, ...], value))}
    if isinstance(value, str):
        return {"stored": reference, "characters": len(value), "preview": value[:128]}
    return {"stored": reference}
