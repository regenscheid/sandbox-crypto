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

from .blocks import Block, Budget, Parsed, normalize_newlines, parse
from .errors import INVALID_REQUEST, DocumentError
from .identity import BlockHint, Mapped, SpanHint

MAX_OPERATIONS: Final = 64
_NOT_AFTER_MARKER: Final = frozenset({"table", "thematic_break"})
"""Blocks that read otherwise on a list item's marker line."""
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
    removed: tuple[str, ...] = ()
    """Blocks the operation replaced or deleted, whose IDs no other block may take."""


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


def _blank(line: str) -> bool:
    """Whether a line is blank as Markdown reads it: only spaces and tabs."""
    return not line.strip(" \t")


def _markdown(markdown: str, what: str) -> str:
    """New Markdown without the blank lines around it."""
    lines = normalize_newlines(markdown).split("\n")
    while lines and _blank(lines[0]):
        lines.pop(0)
    while lines and _blank(lines[-1]):
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
        laid.append(continuation + line if not _blank(line) or last else continuation.rstrip())
    return "\n".join(laid)


def unlay(text: str, continuation: str) -> str:
    """Text as ``_lay`` takes it: without the prefix the lines after the first have.

    A ``$$`` block's TeX is the text between its delimiters, so in a quote its lines keep
    the quote's markers (``> ``); the agents read and write it without them.
    """
    if not continuation:
        return text
    bare = continuation.rstrip()
    lines = text.split("\n")
    for number in range(1, len(lines)):
        line = lines[number]
        if line.startswith(continuation):
            lines[number] = line[len(continuation) :]
        elif line.startswith(bare) and _blank(line[len(bare) :]):
            lines[number] = line[len(bare) :]
    return "\n".join(lines)


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


def _replace(parsed: Parsed, block: Block, laid: str) -> _Placed:
    """Edits putting ``laid`` in the place of ``block``, kept apart from the lines of
    text beside it as an insertion is. A list item that follows ends any block before
    it, and a blank line before it would make its list loose, so none is added there."""
    text = parsed.text
    item_follows = any(
        other.kind == "list_item" and other.lines[0] == block.lines[1] for other in parsed.blocks
    )
    after = block.kind == "list_item" or item_follows or _line_after_blank(text, block.end)
    closing = "" if after else "\n" + block.continuation.rstrip()
    edits = [_Edit(block.start, block.end, laid + closing)]
    if (
        block.kind != "list_item"
        and block.prefix == block.continuation
        and not _line_before_blank(text, block.start)
    ):
        at = _line_start(text, block.start)
        edits.insert(0, _Edit(at, at, block.continuation.rstrip() + "\n"))
    return _Placed(edits, len(edits) - 1, 0)


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
        trailing = len(block.prefix) - len(block.prefix.rstrip())
        if 0 <= position < len(siblings) - 1:
            following = mapped.parsed.blocks[siblings[position + 1]]
            single = block.prefix.endswith(" ") and not block.prefix.endswith("  ")
            joins_badly = following.text[:1].isspace() or following.kind in _NOT_AFTER_MARKER
            if joins_badly and single and "\t" not in block.prefix:
                # On the marker's line, a block indented further than the item's content
                # would move the content's column, and a table or a thematic break would
                # take the marker in (`- ---` is a break): the marker stays on its own line,
                # which keeps the column where one space after it puts it. (With a tab or
                # more spaces after the marker, neither keeps it, and LD-18 refuses.)
                return [_Edit(block.start - trailing, _line_start(text, following.start), "\n")]
            return [_Edit(block.start, following.start, "")]
        return [_Edit(block.start - trailing, block.end, "")]
    start = _line_start(text, block.start)
    end = min(block.end + 1, len(text))
    # A blank line of the block's own container may go with it; one with another prefix
    # keeps two blocks apart, such as two quotes in a list item.
    blank = block.continuation.rstrip()
    following = text.find("\n", end)
    after = text[end : len(text) if following == -1 else following] if end < len(text) else None
    before = text[_line_start(text, start - 1) : start - 1] if start > 0 else None
    blank_after = after is not None and after.rstrip() == blank
    blank_before = before is None or before.rstrip() == blank
    if not blank_before and after is not None and not blank_after and block.kind != "list_item":
        # Text on both sides: a blank line takes the block's place, or the text before
        # it would run on into the text after (list items follow each other directly).
        return [_Edit(start, end, blank + "\n")]
    if blank_before and blank_after:
        end = len(text) if following == -1 else following + 1
    elif blank_before and after is None and before is not None:
        # The last block: the blank line before it goes, and the text keeps its ending.
        start = _line_start(text, start - 1)
        if not text.endswith("\n"):
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


def _occurrences(text: str, quote: str) -> int:
    """How many times ``quote`` occurs in ``text``, overlapping ones too."""
    found = 0
    at = text.find(quote)
    while at != -1:
        found += 1
        at = text.find(quote, at + 1)
    return found


def apply(mapped: Mapped, operation: Operation, budget: Budget | None = None) -> Step:
    """``operation`` applied to the revision ``mapped``; a move's parse takes from
    ``budget``."""
    text = mapped.parsed.text
    match operation:
        case Replace():
            block = _block(mapped, operation.block)
            laid = _lay(_markdown(operation.markdown, "the replacement"), block.continuation)
            new, start = _place(text, _replace(mapped.parsed, block, laid), [])
            region = (start, start + len(laid))
            hint = BlockHint(operation.block, block.kind, *region)
            return Step(new, region, (hint,), removed=(operation.block,))
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
            return _move(mapped, operation, budget or Budget())
        case Delete():
            block = _block(mapped, operation.block)
            new, _ = _apply(text, _delete(mapped, block))
            inside = tuple(
                mapped.block_ids[child] for child in mapped.parsed.descendants(block.index)
            )
            return Step(new, None, removed=(operation.block, *inside))
        case ReplaceMath():
            return _replace_math(mapped, operation)
        case ReplaceText():
            block = _block(mapped, operation.block)
            quote = normalize_newlines(operation.quote)
            found = _occurrences(block.text, quote)
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


def _shifted(offset: int, edits: list[_Edit]) -> int | None:
    """Where an offset lands once the edits are made; None inside text they remove."""
    delta = 0
    for edit in edits:
        if edit.end <= offset:
            delta += len(edit.text) - (edit.end - edit.start)
        elif edit.start < offset:
            return None
    return offset + delta


def _move(mapped: Mapped, operation: Move, budget: Budget) -> Step:
    """The block taken out, then put next to its anchor as the text then stands, which
    lets a block move out of the block that holds it."""
    block = _block(mapped, operation.block)
    anchor = _block(mapped, operation.before or operation.after or "")
    if block.index in mapped.parsed.ancestors(anchor.index):
        raise DocumentError(INVALID_REQUEST, f"block {operation.block} cannot move into itself")
    removal = _delete(mapped, block)
    taken, _ = _apply(mapped.parsed.text, removal)
    start = _shifted(anchor.start, removal)
    if start is not None:
        budget.charge(len(taken))
    found = parse(taken).blocks if start is not None else ()
    target = next(
        (other for other in found if (other.start, other.kind) == (start, anchor.kind)), None
    )
    if target is None:
        other = operation.before or operation.after
        why = (
            f"it is all of {other}, which goes with it: replace {other} with it instead"
            if anchor.index in mapped.parsed.ancestors(block.index)
            else "taking it out changes the blocks around it"
        )
        raise DocumentError(
            INVALID_REQUEST, f"block {operation.block} cannot move next to {other}: {why}"
        )
    laid = _lay(block.text, target.continuation)
    if operation.before is not None:
        placed = _insert_before(taken, target, laid)
    else:
        placed = _insert_after(taken, target, laid)
    new, at = _place(taken, placed, [])
    # A move brings in no new text, so nothing in it is normalized (LD-42).
    hint = BlockHint(operation.block, block.kind, at, at + len(laid))
    return Step(new, None, (hint,))


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
