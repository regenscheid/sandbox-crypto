# SPDX-License-Identifier: MIT
"""Bounded historical tool previews after durable storage (research continuity, RC-22).

This never changes the model's result or the journal. Observers see the original
current call; only the historical transcript copy is replaced with this preview.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from itertools import islice
from typing import Any, Final, Literal, cast

from pydantic import Field, JsonValue

from sandbox_science.contracts import Contract, Identifier
from sandbox_science.orchestrator.tool_status import tool_outcome

INLINE_BYTES: Final = 65536
PREVIEW_BYTES: Final = 16384
FIELD_BYTES: Final = 4096
MAX_FIELDS: Final = 64
STORAGE: Final = "_record_storage"


class StoredToolRecord(Contract):
    """The full record's immutable reference and the limits of its inline preview."""

    format: Literal["tool-record-v1"] = "tool-record-v1"
    project: Identifier
    message: Identifier
    omitted_fields: int = Field(ge=0)
    shortened: tuple[Literal["tool", "arguments"], ...]
    outcome: dict[str, JsonValue]


def retain(record: dict[str, Any], *, project: str, message: str) -> dict[str, Any]:
    """Keep small records intact; otherwise keep bounded, whole fields and a reference.
    The caller must already have committed this exact record under that reference."""
    if _size(record, INLINE_BYTES) is not None:
        return _copy(record)
    shortened: list[Literal["tool", "arguments"]] = []
    preview: dict[str, Any] = {}
    for key, chars in (("tool", 128), ("arguments", 512)):
        value = str(record.get(key, ""))
        preview[key] = value[:chars]
        if len(value) > chars:
            shortened.append(cast(Literal["tool", "arguments"], key))
    used = _size(preview, PREVIEW_BYTES)
    if used is None:
        raise ValueError("tool preview headers exceed their byte limit")
    # Reserve space for the typed reference, outcome and escaped identifier strings.
    left = PREVIEW_BYTES - used - 4096
    kept = set(preview)
    for key, value in islice(record.items(), MAX_FIELDS):
        if key in kept or key == STORAGE:
            continue
        size = _size({key: value}, min(left, FIELD_BYTES))
        if size is not None:
            preview[key] = value
            kept.add(key)
            left -= size
    outcome = tool_outcome(record)
    preview[STORAGE] = StoredToolRecord(
        project=project,
        message=message,
        omitted_fields=len(record) - sum(key in record for key in kept),
        shortened=tuple(shortened),
        outcome={
            key: value[:128] if isinstance(value, str) else value for key, value in outcome.items()
        },
    ).model_dump(mode="json")
    if _size(preview, PREVIEW_BYTES) is None:
        raise ValueError("tool preview exceeds its byte limit")
    return _copy(preview)


def fits(value: object, limit: int) -> bool:
    """Check a JSON allocation budget without first encoding oversized values."""
    return _size(value, limit) is not None


def _copy(record: dict[str, Any]) -> dict[str, Any]:
    # Called only after the size check; detach nested values before observers can
    # mutate them. JSON also matches the representation committed to the journal.
    return cast(dict[str, Any], json.loads(json.dumps(record, ensure_ascii=False, allow_nan=False)))


def _size(value: object, limit: int, depth: int = 0) -> int | None:
    """Measure JSON under a budget without encoding an oversized string/container.
    Only bounded scalar encodings are allocated; traversal stops at the first overrun."""
    if limit < 0 or depth > 32:
        return None
    if isinstance(value, str):
        if len(value) > limit:
            return None
    elif isinstance(value, dict | list | tuple):
        size = 2
        items = (
            cast(dict[str, object], value).items()
            if isinstance(value, dict)
            else enumerate(cast(Sequence[object], value))
        )
        for index, (key, child) in enumerate(items):
            if index:
                size += 2
            if isinstance(value, dict):
                if not isinstance(key, str):
                    return None
                key_size = _size(key, limit - size, depth + 1)
                if key_size is None:
                    return None
                size += key_size + 2
            child_size = _size(child, limit - size, depth + 1)
            if child_size is None:
                return None
            size += child_size
        return size if size <= limit else None
    size = len(json.dumps(value, ensure_ascii=False, allow_nan=False).encode())
    return size if size <= limit else None
