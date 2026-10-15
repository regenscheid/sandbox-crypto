# SPDX-License-Identifier: MIT
"""Text the user approves and agents then read: none of it may be hidden from the user
(threat model, A3).

``check_agent_text`` refuses, rather than strips, every character a person reading the
text may not see: controls but the tab and the line feed (C0, DEL and C1), format
characters (zero-width, bidirectional and tag characters among them), surrogates,
private-use and unassigned code points, line and paragraph separators, and every
Default_Ignorable code point (variation selectors, the Hangul fillers and the combining
grapheme joiner among them). The zero-width joiner and non-joiner and the
left-to-right and right-to-left marks are refused too, for now (the coordinator,
2026-10-15; reversible). A character reference that a Markdown view would show as such a
character (``&#x202E;``, ``&rlm;``) is refused as well. The browser marks the same set
(``web/workspace/src/lib/text.ts``); ``hidden.fixtures.json`` beside it holds the set
and the cases both sides must agree on.
"""

from __future__ import annotations

import html
import re
import unicodedata
from typing import Final

from .errors import INVALID_REQUEST, DocumentError

ALLOWED: Final = frozenset("\t\n")
"""The controls that are text."""
CATEGORIES: Final = frozenset(("Cc", "Cf", "Cs", "Co", "Cn", "Zl", "Zp"))
"""The general categories refused."""
DEFAULT_IGNORABLE: Final = (
    (0x00AD, 0x00AD),
    (0x034F, 0x034F),
    (0x061C, 0x061C),
    (0x115F, 0x1160),
    (0x17B4, 0x17B5),
    (0x180B, 0x180F),
    (0x200B, 0x200F),
    (0x202A, 0x202E),
    (0x2060, 0x206F),
    (0x3164, 0x3164),
    (0xFE00, 0xFE0F),
    (0xFEFF, 0xFEFF),
    (0xFFA0, 0xFFA0),
    (0xFFF0, 0xFFF8),
    (0x1BCA0, 0x1BCA3),
    (0x1D173, 0x1D17A),
    (0xE0000, 0xE0FFF),
)
"""Unicode's Default_Ignorable_Code_Point ranges, first and last, inclusive."""
_REFERENCE: Final = re.compile(r"&(?:#[0-9]{1,7}|#[xX][0-9a-fA-F]{1,6}|[A-Za-z][A-Za-z0-9]{1,31});")
"""A character reference as CommonMark reads one: decimal, hexadecimal or named."""


def is_hidden(char: str) -> bool:
    """Whether a person reading ``char`` may not see it."""
    if char in ALLOWED:
        return False
    code = ord(char)
    if any(first <= code <= last for first, last in DEFAULT_IGNORABLE):
        return True
    return unicodedata.category(char) in CATEGORIES


def hidden_characters(text: str) -> list[tuple[int, str]]:
    """Each hidden character's position, in characters from 0, and code point
    (``U+202E``)."""
    return [(index, _code(char)) for index, char in enumerate(text) if is_hidden(char)]


def _code(char: str) -> str:
    return f"U+{ord(char):04X}"


def _where(text: str, index: int) -> str:
    line = text.count("\n", 0, index) + 1
    column = index - (text.rfind("\n", 0, index) + 1) + 1
    return f"character {index + 1} (line {line}, column {column})"


def check_agent_text(text: str, what: str = "the text") -> None:
    """Refuse text that holds a character the user may not see, written as it is or as a
    character reference, naming the first and where it is."""
    for index, char in enumerate(text):
        if is_hidden(char):
            name = unicodedata.name(char, "")
            raise DocumentError(
                INVALID_REQUEST,
                f"{what} holds {_code(char)}{f' {name}' if name else ''} at "
                f"{_where(text, index)}, a character the user may not see; write it "
                "without control, format, zero-width, bidirectional, tag, private-use, "
                "separator or other invisible characters",
            )
    for found in _REFERENCE.finditer(text):
        decoded = html.unescape(found.group())
        if len(decoded) == 1 and is_hidden(decoded):
            raise DocumentError(
                INVALID_REQUEST,
                f"{what} holds {_code(decoded)} written as {found.group()} at "
                f"{_where(text, found.start())}, a character the user may not see once "
                "it is shown; write it without such characters",
            )
