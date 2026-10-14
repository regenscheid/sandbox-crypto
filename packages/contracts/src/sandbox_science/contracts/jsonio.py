# SPDX-License-Identifier: MIT
"""Strict JSON parsing for contract data read from storage or a peer.

``json.loads`` keeps the last of a repeated key, accepts ``NaN`` and ``Infinity``, and reads
``1e999`` as infinity; pydantic would then validate a value the sender may not have meant.
Parse with ``strict_loads`` before ``model_validate_json`` where such input must be refused.
"""

from __future__ import annotations

import json
import math
from typing import Never


def _unique(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, child in pairs:
        if key in value:
            raise ValueError(f"repeated JSON key {key[:64]!r}")
        value[key] = child
    return value


def _constant(name: str) -> Never:
    raise ValueError(f"JSON has no {name}")


def _finite(text: str) -> float:
    value = float(text)
    if not math.isfinite(value):
        raise ValueError(f"JSON number {text[:32]} is too large to represent")
    return value


def strict_loads(raw: str | bytes) -> object:
    """Parse ``raw``; a repeated key, ``NaN``, ``Infinity`` or a number too large for a
    float (``1e999``) raises ValueError."""
    return json.loads(raw, object_pairs_hook=_unique, parse_constant=_constant, parse_float=_finite)
