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
2026-10-15; reversible). U+FE0F, which asks for an emoji's colour form, is text right
after an emoji (a warning sign, a check mark) or in a keycap (a digit, then
U+20E3), and refused anywhere else. A character
reference that a Markdown view would show as such a character (``&#x202E;``, ``&rlm;``)
is refused as well, and so are more than two blank lines in a row between lines of text,
which could push what follows out of view. ``agent_text`` makes an agent's line ends
line feeds before the check, as a document's are, so a carriage return in a line end is
not refused. The browser marks the same set (``web/workspace/src/lib/text.ts``);
``hidden.fixtures.json`` beside it holds the set and the cases both sides must agree on.
"""

from __future__ import annotations

import html
import re
import unicodedata
from typing import Final, cast

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
_REFERENCE: Final = re.compile(r"&(?:#[0-9]+|#[xX][0-9a-fA-F]+|[A-Za-z][A-Za-z0-9]{1,31});")
"""A character reference: decimal or hexadecimal, of any number of digits (a Markdown
renderer decodes ``&#x0000202E;`` in a link's title), or named."""
MAX_BLANK_LINES: Final = 2
"""Blank lines in a row that text may hold between lines of text: more could push what
follows out of view."""
EMOJI_BASES: Final = (
    (0x00A9, 0x00A9),
    (0x00AE, 0x00AE),
    (0x203C, 0x203C),
    (0x2049, 0x2049),
    (0x2122, 0x2122),
    (0x2139, 0x2139),
    (0x2194, 0x21AA),
    (0x2300, 0x23FF),
    (0x24C2, 0x24C2),
    (0x25A0, 0x25FF),
    (0x2600, 0x27BF),
    (0x2934, 0x2935),
    (0x2B00, 0x2BFF),
    (0x3030, 0x3030),
    (0x303D, 0x303D),
    (0x3297, 0x3297),
    (0x3299, 0x3299),
    (0x1F000, 0x1FAFF),
)
"""Where U+FE0F may follow, asking for an emoji's colour form: every emoji other than the
keycaps' digits, ``#`` and ``*``, and the rest of the blocks they come from, first and
last, inclusive (the coordinator, 2026-10-15)."""
KEYCAPS: Final = frozenset("#*0123456789")
"""The characters that, followed by U+FE0F and U+20E3 COMBINING ENCLOSING KEYCAP, are a
keycap."""
_SELECTOR: Final = "\ufe0f"
_KEYCAP: Final = "\u20e3"


def is_hidden(char: str) -> bool:
    """Whether a person reading ``char`` may not see it."""
    if char in ALLOWED:
        return False
    code = ord(char)
    if any(first <= code <= last for first, last in DEFAULT_IGNORABLE):
        return True
    return unicodedata.category(char) in CATEGORIES


def _selects_emoji(text: str, index: int) -> bool:
    """Whether the U+FE0F at ``index`` asks for the colour form of the emoji before it."""
    if index == 0:
        return False
    before = text[index - 1]
    if any(first <= ord(before) <= last for first, last in EMOJI_BASES):
        return True
    return before in KEYCAPS and text[index + 1 : index + 2] == _KEYCAP


def hidden_characters(text: str) -> list[tuple[int, str]]:
    """Each hidden character's position, in characters from 0, and code point
    (``U+202E``)."""
    return [
        (index, _code(char))
        for index, char in enumerate(text)
        if is_hidden(char) and not (char == _SELECTOR and _selects_emoji(text, index))
    ]


def hidden_references(text: str) -> list[tuple[int, str, str]]:
    """Each character reference that a rendering shows as a hidden character: its
    position, the first hidden character it gives (a named one can give two, such as a
    symbol and a variation selector) and the reference as written."""
    found: list[tuple[int, str, str]] = []
    for match in _REFERENCE.finditer(text):
        hidden = [char for char in html.unescape(match.group()) if is_hidden(char)]
        if hidden:
            found.append((match.start(), _code(hidden[0]), match.group()))
    return found


def blank_run(text: str) -> int | None:
    """The line, from 1, where more than ``MAX_BLANK_LINES`` blank lines in a row begin
    between lines of text, or None; a line of only spaces and tabs is blank. Blank lines
    before the first line of text or after the last push nothing out of view."""
    run, seen = 0, False
    for number, line in enumerate(text.split("\n"), 1):
        if line.strip(" \t") == "":
            run += 1
            continue
        if seen and run > MAX_BLANK_LINES:
            return number - run
        run, seen = 0, True
    return None


def newlines(text: str) -> str:
    """Line ends as line feeds: a carriage return and line feed, or a carriage return
    alone, as a document's are kept."""
    return text.replace("\r\n", "\n").replace("\r", "\n")


def agent_text(text: str, what: str = "the text") -> str:
    """An agent's text as it is kept: its line ends made line feeds, then checked."""
    kept = newlines(text)
    check_agent_text(kept, what)
    return kept


def agent_values[T](value: T, where: str) -> T:
    """A JSON value an agent wrote, as it is kept: each text in it, keys included, with
    its line ends made line feeds and checked, named by its place. A key is checked
    before it names a place, so a refusal never repeats a hidden character."""
    found: object = value
    if isinstance(found, str):
        found = agent_text(found, where)
    elif isinstance(found, dict):
        kept: dict[str, object] = {}
        for key, item in cast(dict[str, object], found).items():
            name = agent_text(key, f"a key of {where}")
            kept[name] = agent_values(item, f"{where}.{name}")
        found = kept
    elif isinstance(found, list | tuple):
        items = [
            agent_values(item, f"{where}[{index}]")
            for index, item in enumerate(cast(list[object], found))
        ]
        found = tuple(items) if isinstance(found, tuple) else items
    return cast(T, found)


def _code(char: str) -> str:
    return f"U+{ord(char):04X}"


def _where(text: str, index: int) -> str:
    line = text.count("\n", 0, index) + 1
    column = index - (text.rfind("\n", 0, index) + 1) + 1
    return f"character {index + 1} (line {line}, column {column})"


def check_agent_text(text: str, what: str = "the text") -> None:
    """Refuse text that holds a character the user may not see, written as it is or as a
    character reference, naming the first and where it is."""
    for index, code in hidden_characters(text)[:1]:
        name = unicodedata.name(text[index], "")
        raise DocumentError(
            INVALID_REQUEST,
            f"{what} holds {code}{f' {name}' if name else ''} at "
            f"{_where(text, index)}, a character the user may not see; write it "
            "without control, format, zero-width, bidirectional, tag, private-use, "
            "separator or other invisible characters",
        )
    for index, code, reference in hidden_references(text)[:1]:
        raise DocumentError(
            INVALID_REQUEST,
            f"{what} holds {code} written as {reference} at {_where(text, index)}, a "
            "character the user may not see once it is shown; write it without such "
            "characters",
        )
    line = blank_run(text)
    if line is not None:
        raise DocumentError(
            INVALID_REQUEST,
            f"{what} has more than {MAX_BLANK_LINES} blank lines in a row from line {line}, "
            "which can push what follows out of view; write at most "
            f"{MAX_BLANK_LINES}",
        )
