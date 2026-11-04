# SPDX-License-Identifier: MIT
"""JSON text the orchestrator did not write itself: archived tool results and notices,
model-written arguments and answers, a connector's or a gateway's text.

CPython's parser raises ``RecursionError``, not ``ValueError``, on text nested about
10,000 levels deep (``"[" * 9999`` is enough), and any of these texts can be that. Read
with ``loads`` or ``raw_decode``, such text is not JSON: each raises a
``json.JSONDecodeError`` for it, so a reader that treats what does not parse as text
catches ``ValueError`` alone. ``deeper_than`` is the cheap bound a caller checks before
it keeps text that later readers parse.
"""

from __future__ import annotations

import json
import re
from typing import Any, Final

_DECODER: Final = json.JSONDecoder()
_STRING: Final = re.compile(r'"(?:[^"\\]|\\.?)*+(?:"|\Z)', re.DOTALL)
"""A JSON string, escapes and all, or one left open, which runs to the end: once begun,
it always matches, without backtracking, so each character is read once."""
_NOT_BRACKET: Final = re.compile(r"[^\[\]{}]+")


def _too_deep(text: str | bytes | bytearray) -> json.JSONDecodeError:
    doc = text if isinstance(text, str) else ""
    return json.JSONDecodeError("nested too deeply to read", doc, 0)


def loads(text: str | bytes | bytearray) -> Any:
    """``json.loads``, which raises ``json.JSONDecodeError`` for text nested too deeply."""
    try:
        return json.loads(text)
    except RecursionError:
        raise _too_deep(text) from None


def raw_decode(text: str, start: int = 0) -> tuple[Any, int]:
    """``json.JSONDecoder().raw_decode``, which raises ``json.JSONDecodeError`` for text
    nested too deeply."""
    try:
        return _DECODER.raw_decode(text, start)
    except RecursionError:
        raise _too_deep(text) from None


def deeper_than(text: str, limit: int) -> bool:
    """Whether ``text``'s brackets outside JSON strings nest deeper than ``limit``.

    Linear in ``text``; text with no more than ``limit`` opening brackets is not scanned.
    Strings are read as a JSON parser reads them, so for JSON this is its nesting depth.
    """
    if text.count("[") + text.count("{") <= limit:
        return False
    depth = 0
    for char in _NOT_BRACKET.sub("", _STRING.sub("", text)):
        if char in "[{":
            depth += 1
            if depth > limit:
                return True
        elif depth:
            depth -= 1
    return False
