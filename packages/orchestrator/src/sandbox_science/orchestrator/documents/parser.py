# SPDX-License-Identifier: MIT
"""The Markdown profile of living documents, as the server parses it (LD-4, LD-5).

The profile is markdown-it's default dialect (markdown-it-py's ``js-default``: CommonMark,
tables and strikethrough) with raw HTML off and links only for http, https and mailto
addresses, as the workspace's browser parser has it (``web/workspace/src/lib/markdown.ts``).
The browser runs markdown-it 15, whose code spans markdown-it-py 4.2 does not find quite
alike, so the server runs a port of markdown-it 15's code span rule. These rules are
added, in this order on both sides:

- YAML front matter, mdit-py-plugins' ``front_matter``, at the start of the text alone;
- dollar math, mdit-py-plugins' ``dollarmath`` with the workspace's settings, and this
  project's limit: a ``$$`` block ends within ``MAX_BLOCK_LINES`` lines of its opening line;
- LaTeX's own delimiters, ``\\(…\\)``, ``\\[…\\]`` and bare display environments, read while
  parsing: a port of the browser's ``mathdelimiters.ts``, whose rules are adapted from the
  user's own cscience-crypto (``src/web/cscience-math.js``), contributed under the MIT
  License by its author;
- fenced containers for theorem-like blocks (``::: lemma``), mdit-py-plugins' ``container``
  with the kinds of LD-4.

The shared cases in ``web/workspace/src/lib/dollarmath.fixtures.json``,
``mathdelimiters.fixtures.json`` and ``documents.fixtures.json`` pin what both parsers find.

Anchors and patches need to know where each block and formula sits in the source, which
markdown-it does not record below the line. The parser therefore wraps its rules: each
block rule records, for every line of each block it makes, where that block's own text
begins on the line (after the markers and indentation of the blocks around it), and each
inline token's content is mapped back to the source it was cut from. The math, code span,
link and image rules record where their tokens begin and end in that content. The mapping is checked
against the content markdown-it produced, and left out when they differ, so a wrong
position is never used.
"""

from __future__ import annotations

import re
import threading
from array import array
from bisect import bisect_left, bisect_right
from collections.abc import Callable, MutableMapping
from dataclasses import dataclass
from importlib.metadata import version
from typing import Any, Final, cast

from markdown_it import MarkdownIt
from markdown_it.rules_block import StateBlock
from markdown_it.rules_inline import StateInline
from markdown_it.token import Token
from mdit_py_plugins.container import container_plugin
from mdit_py_plugins.dollarmath import dollarmath_plugin
from mdit_py_plugins.dollarmath.index import is_escaped as _escaped_like_dollars
from mdit_py_plugins.front_matter import front_matter_plugin

PROFILE: Final = "ss-markdown-1"
"""The profile's version. It changes whenever what the parser finds can change."""

MAX_BLOCK_LINES: Final = 200
"""The lines after a ``$$``, ``\\[`` or ``\\begin{…}`` that its block may end in."""

THEOREM_KINDS: Final = (
    "theorem",
    "lemma",
    "proposition",
    "corollary",
    "definition",
    "claim",
    "assumption",
    "conjecture",
    "proof",
    "remark",
    "example",
)
"""The kinds of theorem-like containers, ``::: lemma`` to ``:::`` (LD-4)."""

CONTAINER: Final = "theorem_like"
"""The container rule's name: its tokens are ``container_theorem_like_open`` and ``_close``."""

DISPLAY_ENVIRONMENTS: Final = frozenset(
    {
        "equation",
        "equation*",
        "align",
        "align*",
        "alignat",
        "alignat*",
        "gather",
        "gather*",
        "multline",
        "multline*",
        "flalign",
        "flalign*",
        "eqnarray",
        "eqnarray*",
        "displaymath",
    }
)
"""Display environments that are math by themselves, as the browser's rules have them."""

MAX_NESTING: Final = 100
"""markdown-it's own nesting limit, as in its default preset and the browser's parser."""

_LINK: Final = re.compile(r"(?:https?://|mailto:)", re.IGNORECASE)
_ENVIRONMENT: Final = re.compile(r"\\begin\{([A-Za-z*]+)\}")
_ASCII_PUNCTUATION: Final = frozenset("!\"#$%&'()*+,-./:;<=>?@[\\]^_`{|}~")
"""A backslash before one of these is a Markdown escape (CommonMark)."""
_PADDING: Final = frozenset(" \t\n")
"""What pads displayed math just inside its brackets."""
_MATH_CHARACTERS: Final = frozenset("{}=+<>")
"""Characters that math uses and bracketed prose seldom does."""
_JS_SPACE: Final = (
    "\t\n\v\f\r \u00a0\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009"
    "\u200a\u2028\u2029\u202f\u205f\u3000\ufeff"
)
"""What JavaScript's ``String.prototype.trim`` removes, for the rules ported from it."""


def parser_versions() -> dict[str, str]:
    """The profile and the parsers' versions, which each revision records (LD-5)."""
    return {
        "profile": PROFILE,
        "markdown-it-py": version("markdown-it-py"),
        "mdit-py-plugins": version("mdit-py-plugins"),
    }


def js_trim(text: str) -> str:
    """``text.trim()`` as JavaScript has it."""
    return text.strip(_JS_SPACE)


def _is_blank(text: str, start: int, end: int) -> bool:
    """Whether ``text[start:end]`` is only whitespace, as a ``$$`` block judges a line."""
    return start >= end or text[start:end].isspace()


def _validate_link(url: str) -> bool:
    """Links only to absolute http and https addresses and mailto, as the browser has them."""
    return _LINK.match(js_trim(url)) is not None


# LaTeX's own delimiters: a port of the browser's mathdelimiters.ts, which is the
# reference. Its rules follow dollar math's: a delimiter after an odd number of
# backslashes is escaped, counted as dollar math counts them; inline, a formula ends at
# the first closing delimiter in its paragraph, and one of only (Python's) whitespace is
# not math; a block begins a line and its closing delimiter ends one, within
# MAX_BLOCK_LINES lines, without a blank line between and inside the block's list item or
# quote. A `\[` or `\begin{…}` line may begin a block after a line of a paragraph. Within
# a paragraph, `\[…\]` is displayed math only when its content is padded just inside both
# brackets or holds what math uses and bracketed prose seldom does; otherwise it stays
# text, as Markdown writers escape brackets (`\[1\]`, `\[sic\]`, `- \[x\] done`, Pandoc's
# `\[@smith2020\]` and `\[^1\]`).


@dataclass(frozen=True, slots=True)
class _Opener:
    markup: str
    """The opening delimiter as the token keeps it; empty for an environment."""
    close: str
    body: int
    """Where the formula's body begins."""
    display: bool
    kept: bool
    """The delimiters are part of the formula, as an environment's are."""


def _opener_at(src: str, start: int) -> _Opener | None:
    """The delimiter that opens at ``start``, if one does. ``displaymath`` is only one."""
    following = src[start + 1 : start + 2]
    if following == "(":
        return _Opener("\\(", "\\)", start + 2, display=False, kept=False)
    if following == "[":
        return _Opener("\\[", "\\]", start + 2, display=True, kept=False)
    if following != "b":
        return None
    match = _ENVIRONMENT.match(src, start)
    if match is None or match[1] not in DISPLAY_ENVIRONMENTS:
        return None
    close = f"\\end{{{match[1]}}}"
    if match[1] == "displaymath":
        return _Opener(match[0], close, match.end(), display=True, kept=False)
    return _Opener("", close, match.end(), display=True, kept=True)


def _is_escaped(src: str, position: int) -> bool:
    """Whether an odd number of backslashes precede ``position``."""
    count = 0
    at = position - 1
    while at >= 0 and src[at] == "\\":
        count += 1
        at -= 1
    return count % 2 == 1


@dataclass
class _Closers:
    """The closing delimiters found in one text."""

    src: str
    positions: dict[str, list[int]]


def _closers(env: MutableMapping[str, Any], src: str, close: str) -> list[int]:
    """Where each closing delimiter that is not escaped begins, for the text being parsed.

    Kept in the parse's environment, so concurrent parses share nothing.
    """
    found = env.get("_closers")
    if not isinstance(found, _Closers) or found.src is not src:
        found = _Closers(src, {})
        env["_closers"] = found
    positions = found.positions
    listed = positions.get(close)
    if listed is None:
        listed = list[int]()
        at = src.find(close)
        while at != -1:
            if not _is_escaped(src, at):
                listed.append(at)
            at = src.find(close, at + 1)
        positions[close] = listed
    return listed


@dataclass
class _Signs:
    """Where math's signs stand in one text (``_displayed_in_paragraph``): from each
    position, where the next one does (-1 for none), and which are scripts."""

    src: str
    following: array[int]
    script: bytearray


def _signs(env: MutableMapping[str, Any], src: str) -> _Signs:
    """The signs of the text being parsed, kept in the parse's environment.

    Escapes pair from the start of each run of backslashes, and a content starts after
    the ``[`` of an opener that is not escaped, so they pair as within it alone. Judging
    each ``\\[`` by reading its content would take time quadratic in the number of ``\\[``
    before one ``\\]``.
    """
    found = env.get("_signs")
    if isinstance(found, _Signs) and found.src is src:
        return found
    length = len(src)
    marked = bytearray(length)
    script = bytearray(length)
    at = 0
    while at < length:
        character = src[at]
        if character == "\\":
            following = src[at + 1 : at + 2]
            if following.isascii() and following.isalpha():
                marked[at] = 1
            elif following in _ASCII_PUNCTUATION:
                at += 1
        elif character in _MATH_CHARACTERS:
            marked[at] = 1
        elif character in "^_":
            marked[at] = 1
            script[at] = 1
        at += 1
    following = array("q", [-1]) * (length + 1)
    for at in range(length - 1, -1, -1):
        following[at] = at if marked[at] else following[at + 1]
    found = _Signs(src, following, script)
    env["_signs"] = found
    return found


def _displayed_in_paragraph(env: MutableMapping[str, Any], src: str, start: int, end: int) -> bool:
    """Whether ``\\[…\\]`` around ``src[start:end]``, within a paragraph, is displayed math
    rather than escaped brackets.

    Math when the content is padded just inside both brackets (``\\[ a - b \\]``), as TeX
    writers pad and Markdown writers' escapes do not; otherwise only when it holds a
    command (a backslash and a letter), a brace, one of ``= + < >``, or ``^`` or ``_``
    after its first character (``^1`` alone is a footnote's reference). A Markdown escape
    and the character it escapes count as neither. The content is read character by
    character, a line break as any other.
    """
    if src[start] in _PADDING and src[end - 1] in _PADDING:
        return True
    signs = _signs(env, src)
    first = signs.following[start]
    if first == start and signs.script[start]:
        first = signs.following[start + 1]
    return first != -1 and first < end


def _first_from(positions: list[int], start: int) -> int | None:
    index = bisect_left(positions, start)
    return positions[index] if index < len(positions) else None


def _backslash_inline(state: StateInline, silent: bool) -> bool:
    src = state.src
    start = state.pos
    if src[start] != "\\":
        return False
    # The opener first: counting the backslashes before every backslash of a long run of
    # them would take time quadratic in its length.
    opener = _opener_at(src, start)
    if opener is None or _escaped_like_dollars(state, start):
        return False
    close = _first_from(_closers(state.env, src, opener.close), opener.body)
    if close is None:
        return False
    end = close + len(opener.close)
    if end > state.posMax:
        return False
    content = src[start:end] if opener.kept else src[opener.body : close]
    if _is_blank(content, 0, len(content)):
        return False
    if opener.markup == "\\[" and not _displayed_in_paragraph(state.env, src, opener.body, close):
        return False
    if not silent:
        token = state.push("math_inline_double" if opener.display else "math_inline", "math", 0)
        token.content = content
        token.markup = opener.markup
        token.meta = {"close": "" if opener.kept else opener.close, "_start": start, "_end": end}
    state.pos = end
    return True


def _backslash_block(state: StateBlock, start_line: int, end_line: int, silent: bool) -> bool:
    if state.sCount[start_line] - state.blkIndent >= 4:
        return False
    src = state.src
    # Only indentation or a container's marker precedes this, so it is never escaped.
    start = state.bMarks[start_line] + state.tShift[start_line]
    if src[start : start + 1] != "\\":
        return False
    opener = _opener_at(src, start)
    if opener is None or not opener.display:
        return False
    close = _first_from(_closers(state.env, src, opener.close), opener.body)
    if close is None:
        return False
    end = close + len(opener.close)
    if end > state.eMarks[min(end_line - 1, start_line + MAX_BLOCK_LINES)]:
        return False
    # The closing delimiter's line: within the limit, after no blank line.
    line = start_line
    while end > state.eMarks[line]:
        line += 1
        if line >= end_line or line - start_line > MAX_BLOCK_LINES:
            return False
        # Inside the list item or quote, as a fence's lines are (a lazy line's is -1).
        if state.sCount[line] < state.blkIndent:
            return False
        if _is_blank(src, state.bMarks[line] + state.tShift[line], state.eMarks[line]):
            return False
    # The line must end with it; more text after it is read within a paragraph.
    if not _is_blank(src, end, state.eMarks[line]):
        return False
    begin, finish = (start, end) if opener.kept else (opener.body, close)
    if line == start_line:
        content = src[begin:finish]
    else:
        first = src[begin : state.eMarks[start_line]]
        middle = state.getLines(start_line + 1, line, state.blkIndent, True)
        last = src[state.bMarks[line] + state.tShift[line] : finish]
        content = f"{first}\n{middle}{last}"
    if _is_blank(content, 0, len(content)):
        return False
    if silent:
        return True
    state.line = line + 1
    token = state.push("math_block", "math", 0)
    token.block = True
    token.content = content
    token.markup = opener.markup
    token.meta = {
        "close": "" if opener.kept else opener.close,
        "_open": start,
        "_close": close,
        "_close_end": end,
    }
    token.map = [start_line, state.line]
    return True


BlockRule = Callable[[StateBlock, int, int, bool], bool]
InlineRule = Callable[[StateInline, bool], bool]


def _limited_dollar_block(rule: BlockRule) -> BlockRule:
    """The plugin's ``$$`` block rule, ending within ``MAX_BLOCK_LINES`` lines.

    The plugin looks for the end of a block up to ``end_line``; passing a nearer one is
    the browser's ``nextLine - startLine > MAX_BLOCK_LINES`` check. It records where the
    opening ``$$`` is, for patches of the formula's TeX.
    """

    def limited(state: StateBlock, start_line: int, end_line: int, silent: bool) -> bool:
        before = len(state.tokens)
        opening = state.bMarks[start_line] + state.tShift[start_line]
        nearer = min(end_line, start_line + MAX_BLOCK_LINES + 1)
        found = rule(state, start_line, nearer, silent)
        if found and not silent and len(state.tokens) > before:
            state.tokens[-1].meta["_open"] = opening
        return found

    return limited


def _front_matter_at_start(rule: BlockRule) -> BlockRule:
    """The plugin's front matter rule, at the start of the text alone.

    The plugin also takes the first line of a list item or quote that begins the text,
    and counts its dashes from the item's second character (``- 1--2 range``).
    """

    def at_start(state: StateBlock, start_line: int, end_line: int, silent: bool) -> bool:
        if start_line != 0 or state.bMarks[0] + state.tShift[0] != 0 or state.blkIndent != 0:
            return False
        return rule(state, start_line, end_line, silent)

    return at_start


# Code spans: a port of markdown-it 15.0.2's rule (src/rules_inline/backticks.ts), which
# the browser runs. The original's license:
#
# Copyright (c) 2014 Vitaly Puzrin, Alex Kocharin.
#
# Permission is hereby granted, free of charge, to any person
# obtaining a copy of this software and associated documentation
# files (the "Software"), to deal in the Software without
# restriction, including without limitation the rights to use,
# copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the
# Software is furnished to do so, subject to the following
# conditions:
#
# The above copyright notice and this permission notice shall be
# included in all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND,
# EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES
# OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND
# NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT
# HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY,
# WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING
# FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR
# OTHER DEALINGS IN THE SOFTWARE.


def _last_runs(src: str) -> dict[int, int]:
    """Where the last run of each length of backticks in ``src`` begins."""
    runs: dict[int, int] = {}
    position = src.find("`")
    while position != -1:
        start = position
        position += 1
        while position < len(src) and src[position] == "`":
            position += 1
        runs[position - start] = start
        position = src.find("`", position)
    return runs


def _backticks(state: StateInline, silent: bool) -> bool:
    """Code spans as markdown-it 15 finds them.

    markdown-it-py 4.2 keeps markdown-it 14's rule, whose cache of the backticks it has
    seen misses a code span before an unclosed backtick once a link label that does not
    close has been scanned up to it (``See [the note `$p$` and the `stray``).
    """
    src = state.src
    start = state.pos
    if src[start] != "`":
        return False
    maximum = state.posMax
    pos = start + 1
    while pos < maximum and src[pos] == "`":
        pos += 1
    marker = src[start:pos]
    if not state.backticksScanned:
        state.backticks = _last_runs(src)
        state.backticksScanned = True
    if state.backticks.get(len(marker), -1) >= pos:
        match_end = pos
        while True:
            match_start = src.find("`", match_end)
            if match_start == -1 or match_start >= maximum:
                break
            match_end = match_start + 1
            while match_end < len(src) and src[match_end] == "`":
                match_end += 1
            if match_end > maximum:
                break
            if match_end - match_start == len(marker):
                if not silent:
                    token = state.push("code_inline", "code", 0)
                    token.markup = marker
                    content = src[pos:match_start].replace("\n", " ")
                    if content.startswith(" ") and content.endswith(" ") and content.strip(" "):
                        content = content[1:-1]
                    token.content = content
                state.pos = match_end
                return True
    if not silent:
        state.pending += marker
    state.pos = pos
    return True


def _container_kind(params: str, *_: object) -> bool:
    """A theorem-like container names its kind first: ``::: lemma (fermat)``."""
    return params.strip().split(" ", 2)[0] in THEOREM_KINDS


# Positions. Each block rule is wrapped to record, for the blocks it makes, where their
# own text begins on each line, and, for the inline tokens it makes, which source each
# character of their content comes from.


@dataclass(frozen=True, slots=True)
class Provenance:
    """Where an inline token's content comes from: runs of content mapped to the source.

    ``segments`` holds ``(content start, source start, length)`` in content order. Spaces
    markdown-it adds when it expands a tab, and backslashes it drops before a table's
    escaped pipe, have no run of their own.
    """

    segments: tuple[tuple[int, int, int], ...]

    def source(self, offset: int) -> int | None:
        """The source offset of the content's character at ``offset``."""
        index = bisect_right(self.segments, (offset, 1 << 62, 0)) - 1
        if index < 0:
            return None
        start, source, length = self.segments[index]
        return source + offset - start if offset < start + length else None

    def source_end(self, offset: int) -> int | None:
        """The source offset just after the content's character before ``offset``."""
        if offset <= 0:
            return None
        found = self.source(offset - 1)
        return None if found is None else found + 1


def _provenance(pieces: list[tuple[str, int | None]], content: str) -> Provenance | None:
    """The provenance of ``content``, the pieces joined and stripped as markdown-it does.

    Pieces without a source offset were added by the parser. None if the pieces do not
    give the content, which means the parser was not followed exactly.
    """
    joined = "".join(piece for piece, _ in pieces)
    if joined.strip() != content:
        return None
    lead = len(joined) - len(joined.lstrip())
    segments: list[tuple[int, int, int]] = []
    offset = 0
    for piece, source in pieces:
        if source is not None and piece:
            start = max(offset, lead)
            end = min(offset + len(piece), lead + len(content))
            if start < end:
                segments.append((start - lead, source + start - offset, end - start))
        offset += len(piece)
    return Provenance(tuple(segments))


def line_first(state: StateBlock, line: int, indent: int, last: int | None = None) -> int:
    """Where a line's own text begins for a block at ``indent``, as ``getLines`` finds it."""
    line_start = first = state.bMarks[line]
    end = state.eMarks[line] if last is None else last
    line_indent = 0
    src = state.src
    while first < end and line_indent < indent:
        character = src[first]
        if character in " \t":
            if character == "\t":
                line_indent += 4 - (line_indent + state.bsCount[line]) % 4
            else:
                line_indent += 1
        elif first - line_start < state.tShift[line]:
            line_indent += 1
        else:
            break
        first += 1
    return first


def _lines_pieces(
    state: StateBlock, begin: int, end: int, indent: int
) -> list[tuple[str, int | None]]:
    """``getLines(begin, end, indent, False)`` as pieces with their source offsets."""
    pieces: list[tuple[str, int | None]] = []
    src = state.src
    for line in range(begin, end):
        last = state.eMarks[line] + 1 if line + 1 < end else state.eMarks[line]
        line_start = first = state.bMarks[line]
        line_indent = 0
        while first < last and line_indent < indent:
            character = src[first]
            if character in " \t":
                if character == "\t":
                    line_indent += 4 - (line_indent + state.bsCount[line]) % 4
                else:
                    line_indent += 1
            elif first - line_start < state.tShift[line]:
                line_indent += 1
            else:
                break
            first += 1
        if line_indent > indent:
            pieces.append((" " * (line_indent - indent), None))
        pieces.append((src[first:last], first))
    return pieces


def _heading_pieces(state: StateBlock, line: int) -> list[tuple[str, int | None]]:
    """The heading rule's content, before it is stripped, with its source offset."""
    position = state.bMarks[line] + state.tShift[line]
    maximum = state.eMarks[line]
    src = state.src
    while position < maximum and src[position] == "#":
        position += 1
    maximum = state.skipSpacesBack(maximum, position)
    trimmed = state.skipCharsStrBack(maximum, "#", position)
    if trimmed > position and src[trimmed - 1] in " \t":
        maximum = trimmed
    return [(src[position:maximum], position)]


def _cell_pieces(state: StateBlock, line: int) -> list[list[tuple[str, int | None]]]:
    """A table row's cells as the table rule splits them, with their source offsets."""
    start = state.bMarks[line] + state.tShift[line]
    raw = state.src[start : state.eMarks[line]]
    text = raw.strip()
    base = start + len(raw) - len(raw.lstrip())
    cells: list[list[tuple[str, int | None]]] = []
    current: list[tuple[str, int | None]] = []
    position = last = 0
    escaped = False
    while position < len(text):
        character = text[position]
        if character == "|":
            if not escaped:
                current.append((text[last:position], base + last))
                cells.append(current)
                current = []
                last = position + 1
            else:
                current.append((text[last : position - 1], base + last))
                last = position
        escaped = character == "\\"
        position += 1
    current.append((text[last:], base + last))
    cells.append(current)
    if cells and not "".join(piece for piece, _ in cells[0]):
        cells.pop(0)
    if cells and not "".join(piece for piece, _ in cells[-1]):
        cells.pop()
    return cells


def _record_inline(name: str, state: StateBlock, tokens: list[Token]) -> None:
    """Give the inline tokens a block rule made the provenance of their content."""
    if name in ("paragraph", "lheading"):
        for token in tokens:
            if token.type == "inline" and token.map is not None:
                pieces = _lines_pieces(state, token.map[0], token.map[1], state.blkIndent)
                token.meta["_provenance"] = _provenance(pieces, token.content)
    elif name == "heading":
        for token in tokens:
            if token.type == "inline" and token.map is not None:
                pieces = _heading_pieces(state, token.map[0])
                token.meta["_provenance"] = _provenance(pieces, token.content)
    elif name == "table":
        cells: list[list[tuple[str, int | None]]] = []
        for token in tokens:
            if token.type == "tr_open" and token.map is not None:
                cells = _cell_pieces(state, token.map[0])
            elif token.type == "inline":
                pieces = cells.pop(0) if cells else []
                token.meta["_provenance"] = _provenance(pieces, token.content)


def _instrumented(name: str, rule: BlockRule) -> BlockRule:
    """A block rule that records where each block it makes has its own text."""

    def recording(state: StateBlock, start_line: int, end_line: int, silent: bool) -> bool:
        if silent:
            return rule(state, start_line, end_line, silent)
        before = len(state.tokens)
        found = rule(state, start_line, end_line, silent)
        if not found or len(state.tokens) == before:
            return found
        made = state.tokens[before:]
        for index, token in enumerate(made):
            if token.map is None or "_firsts" in token.meta:
                continue
            # The rule's first token is the block it made, which ends where it left
            # the parse; a container's own map leaves out its closing marker.
            end = state.line if index == 0 else token.map[1]
            begin = token.map[0]
            token.meta["_lines"] = (begin, end)
            token.meta["_firsts"] = tuple(
                line_first(state, line, state.blkIndent) for line in range(begin, end)
            )
        _record_inline(name, state, made)
        return found

    return recording


_POSITIONED: Final = {
    "math_inline": ("math_inline", "math_inline_double"),
    "backticks": ("code_inline",),
    "autolink": ("link_open",),
    "link": ("link_open",),
    "image": ("image",),
}
"""Inline rules whose tokens record their place in the content, and those tokens' types."""


def _positioned(rule: InlineRule, types: tuple[str, ...]) -> InlineRule:
    """An inline rule whose token records where it begins and ends in the content; a
    link's also records where its text ends, before its address or reference."""

    def recording(state: StateInline, silent: bool) -> bool:
        start = state.pos
        before = len(state.tokens)
        found = rule(state, silent)
        if found and not silent:
            # Text pending before the rule's token is pushed first, so look by type.
            for token in state.tokens[before:]:
                if token.type in types:
                    token.meta.setdefault("_start", start)
                    token.meta.setdefault("_end", state.pos)
                    if token.type == "link_open" and state.src[start] == "[":
                        # Found again from the parse's cache; the position is restored.
                        label_end = state.md.helpers.parseLinkLabel(state, start, True)
                        token.meta.setdefault("_label_end", label_end)
                    break
        return found

    return recording


def _instrument(md: MarkdownIt) -> None:
    for block_rule in md.block.ruler.__rules__:
        block_rule.fn = _instrumented(block_rule.name, block_rule.fn)
    md.block.ruler.__cache__ = None
    for inline_rule in md.inline.ruler.__rules__:
        types = _POSITIONED.get(inline_rule.name)
        if types is not None:
            inline_rule.fn = _positioned(inline_rule.fn, types)
    md.inline.ruler.__cache__ = None


def _make() -> MarkdownIt:
    md = MarkdownIt(
        "js-default",
        {"html": False, "linkify": False, "typographer": False, "maxNesting": MAX_NESTING},
    )
    md.validateLink = _validate_link  # type: ignore[method-assign]
    md.inline.ruler.at("backticks", _backticks)
    front_matter_plugin(md)
    plugin_front_matter = next(
        rule.fn for rule in md.block.ruler.__rules__ if rule.name == "front_matter"
    )
    md.block.ruler.at("front_matter", _front_matter_at_start(plugin_front_matter))
    dollarmath_plugin(
        md,
        allow_labels=True,
        allow_space=False,
        allow_digits=True,
        allow_blank_lines=False,
        double_inline=True,
    )
    plugin_block = next(rule.fn for rule in md.block.ruler.__rules__ if rule.name == "math_block")
    md.block.ruler.at("math_block", _limited_dollar_block(plugin_block))
    md.inline.ruler.before("escape", "math_inline_backslash", _backslash_inline)
    md.block.ruler.before(
        "fence",
        "math_block_backslash",
        _backslash_block,
        {"alt": ["paragraph", "reference", "blockquote", "list"]},
    )
    container_plugin(md, CONTAINER, validate=_container_kind)
    _instrument(md)
    return md


_local = threading.local()


def markdown() -> MarkdownIt:
    """This thread's parser. Parses share no state, but the rulers' caches are not locked."""
    md = cast(MarkdownIt | None, getattr(_local, "md", None))
    if md is None:
        md = _make()
        _local.md = md
    return md


def tokens(text: str) -> list[Token]:
    """The tokens of ``text``, with positions recorded in their ``meta``."""
    return markdown().parse(text, {})
