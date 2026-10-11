# SPDX-License-Identifier: MIT
"""LaTeX's other math delimiters, saved in the dollar form (LD-41, LD-42).

The parser reads ``\\(…\\)``, ``\\[…\\]`` and bare display environments as math, as the
browser does. Text a save brings into a document is rewritten, as it is saved, so that its
formulas use dollars, which other Markdown tools read too:

- ``\\(…\\)`` becomes ``$…$``, without the spaces inside;
- ``\\[…\\]`` and ``\\begin{displaymath}…`` become ``$$…$$`` in the same place: a block
  stays a block, with a blank line before it where it followed a paragraph, and a formula
  within a paragraph stays there;
- a display environment written outside math is put inside ``$$`` lines, or between
  ``$$`` within its paragraph;
- an empty ``\\( \\)`` or ``\\[ \\]`` is removed.

A conversion is made only where it leaves the text's blocks and formulas as they were:
the converted text is parsed again and compared with the text as written, and a
conversion that changes either is left out. Code, escaped delimiters and dollar math are
never touched, since the parser does not read them as these delimiters.

Some ``\\(…\\)`` would read differently with single dollars elsewhere: one holding a
dollar sign or a line break, or followed by a digit (Pandoc, for one, then reads no
math). LD-41 drafts ``$$…$$`` for them, which this dialect reads as displayed math, so
the formula would leave its sentence. Until the user decides, they are kept as written,
which both parsers read as inline math (``AMBIGUOUS_INLINE``).
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Final, Literal

from .blocks import Block, InlineText, Parsed, Span, parse

AMBIGUOUS_INLINE: Literal["keep", "double"] = "keep"
"""What becomes of an ``\\(…\\)`` that single dollars would make ambiguous: ``keep`` it as
written (recommended, pending the user's decision), or make it ``double``, ``$$…$$``, as
LD-41 drafts."""

MAX_SINGLY: Final = 32
"""Conversions tried one at a time when converting them all together changes the parse."""

_EMPTY: Final = re.compile(r"\\\(\s*\\\)|\\\[\s*\\\]")


@dataclass(frozen=True, slots=True)
class _Edit:
    start: int
    end: int
    text: str


@dataclass
class _Candidate:
    kind: str
    """``inline``, ``display``, ``environment`` or ``empty``."""
    edits: list[_Edit]
    tex: str


@dataclass(frozen=True)
class Normalized:
    """The text after normalization, its parse, and what was converted or kept as written."""

    text: str
    parsed: Parsed
    converted: dict[str, int] = field(default_factory=dict[str, int])
    kept: tuple[dict[str, str], ...] = ()

    @property
    def changed(self) -> bool:
        return bool(self.converted)


def _ambiguous(span: Span, text: str) -> str | None:
    """Why single dollars would make this ``\\(…\\)`` read differently, if they would."""
    if "$" in span.tex:
        return "it contains a dollar sign"
    if "\n" in span.tex:
        return "it spans a line break"
    after = text[span.close_end : span.close_end + 1] if span.close_end is not None else ""
    if after.isdigit():
        return "a digit follows it"
    return None


def _previous_line_blank(parsed: Parsed, block: Block) -> bool:
    first = block.lines[0]
    if first == 0:
        return True
    text = parsed.text
    end = text.rfind("\n", 0, block.start)
    start = text.rfind("\n", 0, end) + 1
    line = text[start:end]
    return not line.strip(" \t>")


def _has_previous_sibling(parsed: Parsed, block: Block) -> bool:
    if block.parent is None:
        return any(other.parent is None for other in parsed.blocks[: block.index])
    return parsed.blocks[block.parent].children.index(block.index) > 0


def _line_start(text: str, offset: int) -> int:
    return text.rfind("\n", 0, offset) + 1


def _block_candidate(parsed: Parsed, span: Span, block: Block) -> _Candidate | None:
    assert span.open_start is not None  # noqa: S101 - the caller checked
    assert span.close_end is not None  # noqa: S101
    edits: list[_Edit] = []
    if span.open:
        kind = "display"
        edits.append(_Edit(span.open_start, span.open_start + len(span.open), "$$"))
        edits.append(_Edit(span.close_end - len(span.close), span.close_end, "$$"))
    else:
        kind = "environment"
        edits.append(_Edit(span.open_start, span.open_start, "$$\n" + block.continuation))
        edits.append(_Edit(span.close_end, span.close_end, "\n" + block.continuation + "$$"))
    # A $$ line cannot end a paragraph, as \[ can, so it needs a blank line before it.
    if _has_previous_sibling(parsed, block) and not _previous_line_blank(parsed, block):
        at = _line_start(parsed.text, block.start)
        edits.insert(0, _Edit(at, at, block.continuation.rstrip() + "\n"))
    return _Candidate(kind, edits, span.tex)


def _inline_candidate(parsed: Parsed, span: Span) -> _Candidate | str | None:
    """The edits for a formula within a paragraph, or why it is kept as written."""
    assert span.open_start is not None  # noqa: S101 - the caller checked
    assert span.close_end is not None  # noqa: S101
    text = parsed.text
    open_end = span.open_start + len(span.open)
    close_start = span.close_end - len(span.close)
    if span.open == "\\(":
        why = _ambiguous(span, text)
        if why is not None and AMBIGUOUS_INLINE == "keep":
            return why
        if why is not None:
            return _Candidate(
                "inline",
                [
                    _Edit(span.open_start, open_end, "$$"),
                    _Edit(close_start, span.close_end, "$$"),
                ],
                span.tex,
            )
        inner = text[open_end:close_start]
        lead = len(inner) - len(inner.lstrip())
        trail = len(inner) - len(inner.rstrip())
        return _Candidate(
            "inline",
            [
                _Edit(span.open_start, open_end + lead, "$"),
                _Edit(close_start - trail, span.close_end, "$"),
            ],
            span.tex,
        )
    if "$" in span.tex:
        return "it contains a dollar sign"
    if span.open:
        return _Candidate(
            "display",
            [_Edit(span.open_start, open_end, "$$"), _Edit(close_start, span.close_end, "$$")],
            span.tex,
        )
    return _Candidate(
        "environment",
        [
            _Edit(span.open_start, span.open_start, "$$"),
            _Edit(span.close_end, span.close_end, "$$"),
        ],
        span.tex,
    )


def _empty_candidates(inline: InlineText, region: tuple[int, int]) -> Iterable[_Candidate]:
    if inline.provenance is None:
        return
    for match in _EMPTY.finditer(inline.content):
        start, end = match.span()
        backslashes = len(inline.content[:start]) - len(inline.content[:start].rstrip("\\"))
        if backslashes % 2 or any(low < end and start < high for low, high, _ in inline.excluded):
            continue
        source_start = inline.provenance.source(start)
        source_end = inline.provenance.source_end(end)
        if source_start is None or source_end is None:
            continue
        if region[0] <= source_start and source_end <= region[1]:
            yield _Candidate("empty", [_Edit(source_start, source_end, "")], match[0])


def _candidates(
    parsed: Parsed, region: tuple[int, int]
) -> tuple[list[_Candidate], list[dict[str, str]]]:
    found: list[_Candidate] = []
    kept: list[dict[str, str]] = []
    for span in parsed.spans:
        if span.open in ("$", "$$") or span.open_start is None or span.close_end is None:
            continue
        if not (region[0] <= span.open_start and span.close_end <= region[1]):
            continue
        block = parsed.blocks[span.block]
        if block.kind == "equation":
            if "$" in span.tex:
                kept.append({"tex": span.tex, "why": "it contains a dollar sign"})
                continue
            candidate = _block_candidate(parsed, span, block)
        else:
            candidate = _inline_candidate(parsed, span)
        if isinstance(candidate, str):
            kept.append({"tex": span.tex, "why": candidate})
        elif candidate is not None:
            found.append(candidate)
    for block in parsed.blocks:
        for inline in block.inlines:
            found.extend(_empty_candidates(inline, region))
    return found, kept


def _apply(text: str, candidates: Iterable[_Candidate]) -> str:
    edits = sorted(
        (edit for candidate in candidates for edit in candidate.edits),
        key=lambda edit: (edit.start, edit.end),
    )
    pieces: list[str] = []
    at = 0
    for edit in edits:
        pieces.append(text[at : edit.start])
        pieces.append(edit.text)
        at = edit.end
    pieces.append(text[at:])
    return "".join(pieces)


def signature(parsed: Parsed) -> tuple[object, ...]:
    """What normalization must keep: the blocks' kinds and nesting, and each formula's
    block, kind and TeX, with whitespace collapsed."""
    return (
        tuple((block.kind, block.depth) for block in parsed.blocks),
        tuple((span.block, span.kind, " ".join(span.tex.split())) for span in parsed.spans),
    )


def normalize(parsed: Parsed, region: tuple[int, int] | None = None) -> Normalized:
    """Convert the delimiters of the formulas that lie within ``region`` of the text."""
    found, kept = _candidates(parsed, region or (0, len(parsed.text)))
    if not found:
        return Normalized(parsed.text, parsed, kept=tuple(kept))
    expected = signature(parsed)
    together = _apply(parsed.text, found)
    reparsed = parse(together)
    accepted: list[_Candidate] = found
    if signature(reparsed) != expected:
        accepted = []
        reparsed = parsed
        for candidate in found[:MAX_SINGLY]:
            trial = parse(_apply(parsed.text, [*accepted, candidate]))
            if signature(trial) == expected:
                accepted.append(candidate)
                reparsed = trial
            else:
                kept.append({"tex": candidate.tex, "why": "the dollar form would read differently"})
        kept.extend(
            {"tex": candidate.tex, "why": "too many formulas to check one at a time"}
            for candidate in found[MAX_SINGLY:]
        )
    converted: dict[str, int] = {}
    for candidate in accepted:
        converted[candidate.kind] = converted.get(candidate.kind, 0) + 1
    return Normalized(reparsed.text, reparsed, converted=converted, kept=tuple(kept))
