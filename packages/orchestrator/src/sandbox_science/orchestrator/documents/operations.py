# SPDX-License-Identifier: MIT
"""A patch's operations, and how each changes a revision's text (LD-16, LD-18).

Each operation names the blocks or math spans it touches, by the IDs of the revision it
applies to:

- ``replace`` a block with new Markdown;
- ``insert`` Markdown before or after a block, or at the ``start`` (after the front
  matter) or ``end``;
- ``move`` a block before or after another;
- ``delete`` a block;
- ``replace_math``: a math span's TeX;
- ``replace_text``: a quote that occurs exactly once in a block's text.

New Markdown is written as if at the top level: the operation lays it inside the lists
and quotes around its place, with their markers and indentation on each line, and with
blank lines between it and the blocks beside it. Each operation reports where its new
text lies, which block keeps the ID of the block it names (LD-7, rule 1), and which IDs
it touches, for the checks of LD-17 and LD-18.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, Final, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .blocks import Block, normalize_newlines
from .errors import INVALID_REQUEST, DocumentError
from .identity import BlockHint, Mapped, SpanHint

MAX_OPERATIONS: Final = 64
_ID = Annotated[str, Field(pattern=r"^[bm][0-9]{1,9}$")]


class _Operation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)


class Replace(_Operation):
    op: Literal["replace"]
    block: _ID
    markdown: str


class Insert(_Operation):
    op: Literal["insert"]
    markdown: str
    before: _ID | None = None
    after: _ID | None = None
    at: Literal["start", "end"] | None = None

    @model_validator(mode="after")
    def _one_place(self) -> Self:
        if sum(place is not None for place in (self.before, self.after, self.at)) != 1:
            raise ValueError("insert takes exactly one of before, after or at")
        return self


class Move(_Operation):
    op: Literal["move"]
    block: _ID
    before: _ID | None = None
    after: _ID | None = None

    @model_validator(mode="after")
    def _one_place(self) -> Self:
        if (self.before is None) == (self.after is None):
            raise ValueError("move takes exactly one of before or after")
        if self.block in (self.before, self.after):
            raise ValueError("a block cannot move next to itself")
        return self


class Delete(_Operation):
    op: Literal["delete"]
    block: _ID


class ReplaceMath(_Operation):
    op: Literal["replace_math"]
    math: _ID
    tex: str


class ReplaceText(_Operation):
    op: Literal["replace_text"]
    block: _ID
    quote: str = Field(min_length=1)
    with_: str = Field(alias="with")


Operation = Annotated[
    Replace | Insert | Move | Delete | ReplaceMath | ReplaceText, Field(discriminator="op")
]


@dataclass(frozen=True)
class Touches:
    """The IDs an operation names, by what it does with them."""

    changes: tuple[str, ...] = ()
    """Blocks it rewrites, which may change."""
    holds: tuple[str, ...] = ()
    """Blocks that change only because a span it rewrites is in them."""
    removes: tuple[str, ...] = ()
    """Blocks it replaces or deletes, which go with everything inside them."""
    moves: tuple[str, ...] = ()
    anchors: tuple[tuple[str, str], ...] = ()
    """Blocks it inserts or moves next to, with the side: ``before`` or ``after``."""
    ends: tuple[str, ...] = ()
    """``start`` or ``end``, where it inserts."""
    spans: tuple[str, ...] = ()

    @property
    def blocks(self) -> tuple[str, ...]:
        return (
            *self.changes,
            *self.holds,
            *self.removes,
            *self.moves,
            *(name for name, _ in self.anchors),
        )


def touches(operation: Operation, mapped: Mapped) -> Touches:
    """What ``operation`` names, in the revision ``mapped`` it was written for."""
    match operation:
        case Replace():
            return Touches(changes=(operation.block,), removes=(operation.block,))
        case Insert():
            if operation.at is not None:
                return Touches(ends=(operation.at,))
            side = "before" if operation.before is not None else "after"
            return Touches(anchors=((operation.before or operation.after or "", side),))
        case Move():
            side = "before" if operation.before is not None else "after"
            return Touches(
                moves=(operation.block,),
                anchors=((operation.before or operation.after or "", side),),
            )
        case Delete():
            return Touches(removes=(operation.block,))
        case ReplaceMath():
            index = mapped.span_index(operation.math)
            block = () if index is None else (mapped.block_ids[mapped.parsed.spans[index].block],)
            return Touches(holds=block, spans=(operation.math,))
        case ReplaceText():
            return Touches(changes=(operation.block,))


@dataclass(frozen=True)
class Step:
    """One operation applied: the new text and what the identity rules need from it."""

    text: str
    region: tuple[int, int] | None
    """Where the operation's new Markdown lies in the text, to normalize (LD-42)."""
    block_hints: tuple[BlockHint, ...] = ()
    span_hints: tuple[SpanHint, ...] = ()
    expect: tuple[str, str] | None = None
    """A span ID and the TeX it must read after a ``replace_math``."""


@dataclass(frozen=True, slots=True)
class _Edit:
    start: int
    end: int
    text: str


def _apply(text: str, edits: list[_Edit]) -> tuple[str, list[int]]:
    """The text with the edits made, and where each edit's text begins in it."""
    order = sorted(range(len(edits)), key=lambda index: (edits[index].start, edits[index].end))
    pieces: list[str] = []
    starts = [0] * len(edits)
    at = 0
    length = 0
    for index in order:
        edit = edits[index]
        if edit.start < at:
            raise DocumentError(INVALID_REQUEST, "the operation's changes overlap")
        kept = text[at : edit.start]
        pieces.append(kept)
        length += len(kept)
        starts[index] = length
        pieces.append(edit.text)
        length += len(edit.text)
        at = edit.end
    pieces.append(text[at:])
    return "".join(pieces), starts


def _block(mapped: Mapped, block_id: str) -> Block:
    index = mapped.index(block_id)
    if index is None:
        raise DocumentError(INVALID_REQUEST, f"this revision has no block {block_id}")
    return mapped.parsed.blocks[index]


def _markdown(markdown: str, what: str) -> str:
    """New Markdown without the blank lines around it."""
    lines = normalize_newlines(markdown).split("\n")
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    if not lines:
        raise DocumentError(INVALID_REQUEST, f"{what} has no text; use delete to remove a block")
    return "\n".join(lines)


def _lay(text: str, continuation: str) -> str:
    """Text placed where a block begins: its lines after the first get the prefix of the
    blocks around it; blank ones get it without trailing spaces, so a quote stays one."""
    lines = text.split("\n")
    laid = [lines[0]]
    for number, line in enumerate(lines[1:], start=1):
        last = number == len(lines) - 1
        laid.append(continuation + line if line.strip() or last else continuation.rstrip())
    return "\n".join(laid)


def _line_start(text: str, offset: int) -> int:
    return text.rfind("\n", 0, offset) + 1


def _line_after_blank(text: str, offset: int) -> bool:
    """Whether the line after the one ending at ``offset`` is blank, or there is none."""
    if offset >= len(text):
        return True
    end = text.find("\n", offset + 1)
    line = text[offset + 1 : len(text) if end == -1 else end]
    return not line.strip(" \t>")


def _line_before_blank(text: str, offset: int) -> bool:
    """Whether the line before the one holding ``offset`` is blank, or there is none."""
    start = _line_start(text, offset)
    if start == 0:
        return True
    previous = _line_start(text, start - 1)
    return not text[previous : start - 1].strip(" \t>")


@dataclass(frozen=True)
class _Placed:
    """Edits that put new text in place: ``edits[holder].text`` holds it at ``offset``."""

    edits: list[_Edit]
    holder: int
    offset: int


def _insert_before(text: str, block: Block, laid: str) -> _Placed:
    """Edits putting ``laid`` before ``block``, inside the same blocks as it."""
    separator = "\n" + block.continuation.rstrip() + "\n" + block.continuation
    edits = [_Edit(block.start, block.start, laid + separator)]
    if block.prefix == block.continuation and not _line_before_blank(text, block.start):
        # The block's line follows a line of text: keep the new text apart from it.
        at = _line_start(text, block.start)
        edits.insert(0, _Edit(at, at, block.continuation.rstrip() + "\n"))
    return _Placed(edits, len(edits) - 1, 0)


def _insert_after(text: str, block: Block, laid: str) -> _Placed:
    """Edits putting ``laid`` after ``block``, inside the same blocks as it."""
    separator = "\n" + block.continuation.rstrip() + "\n" + block.continuation
    closing = "" if _line_after_blank(text, block.end) else "\n" + block.continuation.rstrip()
    return _Placed([_Edit(block.end, block.end, separator + laid + closing)], 0, len(separator))


def _place(text: str, placed: _Placed, removal: list[_Edit]) -> tuple[str, int]:
    """The text with a removal and a placement made, and where the placed text begins."""
    new, starts = _apply(text, [*removal, *placed.edits])
    return new, starts[len(removal) + placed.holder] + placed.offset


def _delete(mapped: Mapped, block: Block) -> list[_Edit]:
    """Edits removing a block, and the blank line that kept it apart from the next."""
    text = mapped.parsed.text
    if block.prefix != block.continuation:
        # The block's first line holds the marker of a list item or quote it begins:
        # its next sibling takes that line, or the marker stays alone.
        siblings = mapped.parsed.blocks[block.parent].children if block.parent is not None else ()
        position = siblings.index(block.index) if block.index in siblings else -1
        if 0 <= position < len(siblings) - 1:
            following = mapped.parsed.blocks[siblings[position + 1]]
            return [_Edit(block.start, following.start, "")]
        trailing = len(block.prefix) - len(block.prefix.rstrip())
        return [_Edit(block.start - trailing, block.end, "")]
    start = _line_start(text, block.start)
    end = min(block.end + 1, len(text))
    if end < len(text) and _line_after_blank(text, block.end):
        following = text.find("\n", end)
        end = len(text) if following == -1 else following + 1
    elif start > 0 and _line_before_blank(text, block.start):
        start = _line_start(text, start - 1)
    if end == len(text) and start > 0:
        # Removing the last lines: the line break before them goes too.
        start -= 1
    return [_Edit(start, end, "")]


def _own_to_source(block: Block, offset: int) -> int:
    """The text offset of an offset into a block's own text."""
    lines = block.text.split("\n")
    at = 0
    for number, line in enumerate(lines):
        if offset <= at + len(line):
            return block.firsts[number] + offset - at
        at += len(line) + 1
    return block.end


def _top_level(mapped: Mapped) -> list[Block]:
    return [block for block in mapped.parsed.blocks if block.parent is None]


def apply(mapped: Mapped, operation: Operation) -> Step:
    """``operation`` applied to the revision ``mapped``."""
    text = mapped.parsed.text
    match operation:
        case Replace():
            block = _block(mapped, operation.block)
            laid = _lay(_markdown(operation.markdown, "the replacement"), block.continuation)
            new, (start,) = _apply(text, [_Edit(block.start, block.end, laid)])
            region = (start, start + len(laid))
            return Step(new, region, (BlockHint(operation.block, block.kind, *region),))
        case Insert():
            markdown = _markdown(operation.markdown, "the insertion")
            if operation.at is not None:
                return _insert_at(mapped, operation.at, markdown)
            anchor = _block(mapped, operation.before or operation.after or "")
            laid = _lay(markdown, anchor.continuation)
            if operation.before is not None:
                placed = _insert_before(text, anchor, laid)
            else:
                placed = _insert_after(text, anchor, laid)
            new, start = _place(text, placed, [])
            return Step(new, (start, start + len(laid)))
        case Move():
            return _move(mapped, operation)
        case Delete():
            block = _block(mapped, operation.block)
            new, _ = _apply(text, _delete(mapped, block))
            return Step(new, None)
        case ReplaceMath():
            return _replace_math(mapped, operation)
        case ReplaceText():
            block = _block(mapped, operation.block)
            quote = normalize_newlines(operation.quote)
            found = block.text.count(quote)
            if found != 1:
                where = "does not occur" if not found else f"occurs {found} times"
                raise DocumentError(
                    INVALID_REQUEST,
                    f"the quote {where} in block {operation.block}; quote text that occurs "
                    "exactly once in it",
                )
            offset = block.text.index(quote)
            start = _own_to_source(block, offset)
            end = _own_to_source(block, offset + len(quote))
            laid = _lay(normalize_newlines(operation.with_), block.continuation)
            new, (at,) = _apply(text, [_Edit(start, end, laid)])
            hint = BlockHint(operation.block, block.kind, block.start, block.start + 1)
            return Step(new, (at, at + len(laid)), (hint,))


def _insert_at(mapped: Mapped, end: str, markdown: str) -> Step:
    """Markdown at the start of the text, after its front matter, or at its end."""
    text = mapped.parsed.text
    top = _top_level(mapped)
    if not top:
        new = markdown + "\n" + (("\n" + text) if text.strip() else "")
        return Step(new, (0, len(markdown)))
    if end == "start" and top[0].kind != "front_matter":
        placed = _insert_before(text, top[0], markdown)
    else:
        placed = _insert_after(text, top[0] if end == "start" else top[-1], markdown)
    new, start = _place(text, placed, [])
    return Step(new, (start, start + len(markdown)))


def _move(mapped: Mapped, operation: Move) -> Step:
    text = mapped.parsed.text
    block = _block(mapped, operation.block)
    anchor = _block(mapped, operation.before or operation.after or "")
    if block.index in mapped.parsed.ancestors(anchor.index):
        raise DocumentError(INVALID_REQUEST, f"block {operation.block} cannot move into itself")
    laid = _lay(block.text, anchor.continuation)
    removal = _delete(mapped, block)
    if operation.before is not None:
        placed = _insert_before(text, anchor, laid)
    else:
        placed = _insert_after(text, anchor, laid)
    for removed in removal:
        for inserted in placed.edits:
            if removed.start < inserted.start < removed.end:
                raise DocumentError(
                    INVALID_REQUEST, f"block {operation.block} cannot move into its own place"
                )
    new, start = _place(text, placed, removal)
    region = (start, start + len(laid))
    return Step(new, region, (BlockHint(operation.block, block.kind, *region),))


def _replace_math(mapped: Mapped, operation: ReplaceMath) -> Step:
    index = mapped.span_index(operation.math)
    if index is None:
        raise DocumentError(INVALID_REQUEST, f"this revision has no math {operation.math}")
    span = mapped.parsed.spans[index]
    block = mapped.parsed.blocks[span.block]
    if span.tex_start is None or span.tex_end is None:
        raise DocumentError(
            INVALID_REQUEST,
            f"math {operation.math} cannot be found in the text exactly; replace its block "
            f"{mapped.block_ids[block.index]} instead",
        )
    tex = normalize_newlines(operation.tex)
    laid = _lay(tex, block.continuation)
    new, _ = _apply(mapped.parsed.text, [_Edit(span.tex_start, span.tex_end, laid)])
    block_id = mapped.block_ids[block.index]
    return Step(
        new,
        None,
        (BlockHint(block_id, block.kind, block.start, block.start + 1),),
        (SpanHint(operation.math, block_id, block.spans.index(span.index)),),
        expect=(operation.math, tex),
    )
