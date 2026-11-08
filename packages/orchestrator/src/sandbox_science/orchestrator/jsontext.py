# SPDX-License-Identifier: MIT
"""JSON text the orchestrator did not write itself: archived tool results and notices,
model-written arguments and answers, a connector's or a gateway's text.

Any of these texts can nest arbitrarily deep, and how deep CPython's parser reads
depends on its version: 3.13 raises ``RecursionError``, not ``ValueError``, at about
10,000 levels (``"[" * 9999`` is enough), while 3.14 reads that and fails only far
deeper. Read with ``loads`` or ``raw_decode``, text nested past ``MAX_DEPTH`` is not
JSON on any version: each raises a ``json.JSONDecodeError`` for it, so a reader that
treats what does not parse as text catches ``ValueError`` alone. ``deeper_than`` is the
cheap bound a caller checks before it keeps text that later readers parse.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from typing import Any, Final, cast

MAX_DEPTH: Final = 1000
"""The deepest nesting of arrays and objects ``loads`` and ``raw_decode`` read."""

_DECODER: Final = json.JSONDecoder()
_STRING: Final = re.compile(r'"(?:[^"\\]|\\.?)*+(?:"|\Z)', re.DOTALL)
"""A JSON string, escapes and all, or one left open, which runs to the end: once begun,
it always matches, without backtracking, so each character is read once."""
_NOT_BRACKET: Final = re.compile(r"[^\[\]{}]+")


def _too_deep(text: str | bytes | bytearray) -> json.JSONDecodeError:
    doc = text if isinstance(text, str) else ""
    return json.JSONDecodeError("nested too deeply to read", doc, 0)


def _nests_past(value: object, limit: int) -> bool:
    """Whether parsed JSON nests arrays and objects deeper than ``limit``, walked
    without recursion."""
    pending: list[tuple[object, int]] = [(value, 1)]
    while pending:
        node, depth = pending.pop()
        if depth > limit:
            return True
        children = cast("Iterable[object]", node.values() if isinstance(node, dict) else node)
        pending.extend((child, depth + 1) for child in children if _nested(child))
    return False


def _nested(value: object) -> bool:
    return isinstance(value, dict | list)


def _openers(text: str | bytes | bytearray) -> int:
    if isinstance(text, str):
        return text.count("[") + text.count("{")
    return text.count(b"[") + text.count(b"{")


def loads(text: str | bytes | bytearray) -> Any:
    """``json.loads``, which raises ``json.JSONDecodeError`` for text nested deeper than
    ``MAX_DEPTH``."""
    try:
        value = json.loads(text)
    except RecursionError:
        raise _too_deep(text) from None
    if _openers(text) > MAX_DEPTH and _nested(value) and _nests_past(value, MAX_DEPTH):
        raise _too_deep(text)
    return value


def raw_decode(text: str, start: int = 0) -> tuple[Any, int]:
    """``json.JSONDecoder().raw_decode``, which raises ``json.JSONDecodeError`` for text
    nested deeper than ``MAX_DEPTH``."""
    try:
        value, end = _DECODER.raw_decode(text, start)
    except RecursionError:
        raise _too_deep(text) from None
    if _openers(text) > MAX_DEPTH and _nested(value) and _nests_past(value, MAX_DEPTH):
        raise _too_deep(text)
    return value, end


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
