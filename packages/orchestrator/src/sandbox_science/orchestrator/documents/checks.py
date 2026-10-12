# SPDX-License-Identifier: MIT
"""Warnings on a revision's math that KaTeX does not see (LD-28, LD-30).

When a revision is saved, its new and changed blocks are checked for:

- ``unbalanced_dollar``: outside formulas, code, images and the addresses of links, a
  ``$`` that does not escape anything and opens no formula, when a letter, a backslash or
  another symbol follows it (``$x`` with no closing dollar; a ``$`` before a digit or a
  space is left alone, as amounts are written), or a ``$$`` that opens or closes nothing;
- ``definition_in_math``: ``\\def``, ``\\gdef``, ``\\newcommand`` and the like inside a
  formula. The GUI shows such a formula as written, since a definition can make KaTeX's
  work grow far beyond the formula's length, while TeX can make it apply to the rest of
  an exported document;
- ``display_swallows_text``: a displayed formula holding a ``$``, or a line that reads as
  Markdown or prose, as when a ``$$`` that did not close where it should took the lines
  after it;
- ``front_matter``: front matter that is not a title and macros in YAML.

The checks are heuristics: they warn and never change the text. KaTeX's own check is
scheduled atomically by the source-event hook and runs outside source transactions
in ``mathjobs``. It creates separately attributed mechanical findings.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Final

from pydantic import JsonValue

from .blocks import Block, InlineText, Span, merged, overlaps
from .identity import Mapped
from .operations import unlay

_DEFINITION: Final = re.compile(
    r"\\(?:def|gdef|edef|xdef|let|newcommand|renewcommand|providecommand|DeclareMathOperator)"
    r"(?![A-Za-z])"
)
_TEXT_GROUP: Final = re.compile(
    r"\\(?:text[a-z]*|mathrm|operatorname\*?|mbox|hbox|textrm|textit|textbf|tag)\s*\{[^{}]*\}"
)
_COMMAND: Final = re.compile(r"\\[A-Za-z]+|\\.")
_PROSE: Final = re.compile(r"(?:\b[A-Za-z]{2,}\b[ ,;:.]+){3}\b[A-Za-z]{2,}\b")
_MARKDOWN_LINE: Final = re.compile(r"\s{0,3}(?:#{1,6}\s|>\s|```|~~~|:::|\|.*\|\s*$)")


def _escaped(text: str, position: int) -> bool:
    count = 0
    while position - count - 1 >= 0 and text[position - count - 1] == "\\":
        count += 1
    return count % 2 == 1


def _stray_dollars(inline: InlineText) -> list[str]:
    """Dollar signs in a text that open no formula and look as if they meant to."""
    found: list[str] = []
    content = inline.content
    ranges = merged(inline.excluded)
    position = content.find("$")
    while position != -1:
        inside = overlaps(ranges, position, position + 1)
        if not inside and not _escaped(content, position):
            following = content[position + 1 : position + 2]
            if following == "$":
                found.append("a $$ that opens or closes no formula")
                position = content.find("$", position + 2)
                continue
            if following and not following.isspace() and not following.isdigit():
                found.append(f"a $ before {following!r} that opens no formula")
        position = content.find("$", position + 1)
    return found


def _swallowed(tex: str) -> str | None:
    """Why a displayed formula looks as if it took text that was not meant as math."""
    for position, character in enumerate(tex):
        if character == "$" and not _escaped(tex, position):
            return "it contains a $, so it may have taken the end of another formula"
    for line in tex.split("\n")[1:]:
        if _MARKDOWN_LINE.match(line):
            return f"its line {line.strip()[:40]!r} reads as Markdown"
        plain = _COMMAND.sub(" ", _TEXT_GROUP.sub(" ", line))
        if _PROSE.search(plain):
            return f"its line {line.strip()[:40]!r} reads as text"
    return None


def warnings(mapped: Mapped, blocks: Iterable[int]) -> list[dict[str, JsonValue]]:
    """The warnings for the given blocks of a revision, by block and span ID."""
    parsed = mapped.parsed
    found: list[dict[str, JsonValue]] = []

    def add(kind: str, block: Block, message: str, span: Span | None = None) -> None:
        warning: dict[str, JsonValue] = {"kind": kind, "block": mapped.block_ids[block.index]}
        if span is not None:
            warning["math"] = mapped.span_ids[span.index]
        warning["message"] = message
        found.append(warning)

    for index in sorted(set(blocks)):
        block = parsed.blocks[index]
        if block.kind == "front_matter" and parsed.front_matter is not None:
            for problem in parsed.front_matter.problems:
                add("front_matter", block, problem)
        for inline in block.inlines:
            for message in _stray_dollars(inline):
                add("unbalanced_dollar", block, message)
        for span_index in block.spans:
            span = parsed.spans[span_index]
            # A $$ block's TeX keeps the markers of the quote it is in.
            tex = unlay(span.tex, block.continuation)
            definition = _DEFINITION.search(tex)
            if definition is not None:
                add(
                    "definition_in_math",
                    block,
                    f"{definition[0]} inside a formula: the workspace shows a formula that "
                    "defines macros as written, and TeX may apply the definition to the rest "
                    "of an exported document. Define macros in the front matter",
                    span,
                )
            if span.kind == "display":
                why = _swallowed(tex)
                if why is not None:
                    add("display_swallows_text", block, f"this displayed formula: {why}", span)
    return found
