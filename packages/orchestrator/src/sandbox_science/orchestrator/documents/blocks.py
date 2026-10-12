# SPDX-License-Identifier: MIT
"""A revision's blocks and math spans, from the parse (LD-6, LD-40).

A block is a unit the parser finds: front matter, heading, paragraph, list, list item,
quote, code block, display equation, table, theorem-like container or thematic break.
Each has its own text: its lines, without the markers and indentation of the blocks it
is in, and without trailing blank lines. A block's own text includes the blocks inside
it, so a container whose child changes changes too (LD-F5). The text's SHA-256 tells
whether a block changed.

A math span is one formula: inline, displayed within a paragraph, or a display equation.
Spans belong to the block whose inline text holds them (a paragraph, heading or table)
or are the equation block itself. Formulas in an image's description are not spans.

Positions are offsets into the text, counted in code points, as Python counts them.
"""

from __future__ import annotations

import hashlib
import re
from bisect import bisect_right
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from typing import Final, Literal

from markdown_it.token import Token

from .errors import INVALID_REQUEST, DocumentError
from .frontmatter import FrontMatter, read_front_matter
from .parser import CONTAINER, Provenance, tokens

MAX_TEXT_BYTES: Final = 1024 * 1024
"""A revision's text, in UTF-8."""
MAX_DEPTH: Final = 32
"""Blocks inside blocks: lists, list items, quotes and containers."""
MAX_BLOCKS: Final = 10_000
MAX_SPANS: Final = 20_000
MAX_PARSED: Final = 16 * 1024 * 1024
"""Characters one change may parse in all: a patch parses the text again after each
operation, and normalization parses it again to check its conversions."""

STRUCTURAL: Final = frozenset({"heading", "table", "figure", "container"})
"""The kinds of structural blocks (LD-40); a figure is a paragraph holding one image."""

_OPEN: Final = {
    "paragraph_open": "paragraph",
    "heading_open": "heading",
    "bullet_list_open": "list",
    "ordered_list_open": "list",
    "list_item_open": "list_item",
    "blockquote_open": "quote",
    "table_open": "table",
    f"container_{CONTAINER}_open": "container",
}
_LEAF: Final = {
    "front_matter": "front_matter",
    "fence": "code",
    "code_block": "code",
    "math_block": "equation",
    "math_block_label": "equation",
    "hr": "thematic_break",
    "html_block": "html",
}
_INLINE_MATH: Final = {"math_inline": "inline", "math_inline_double": "display"}
_LABEL: Final = re.compile(r"\((.+)\)")

SpanKind = Literal["inline", "display"]


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class Span:
    """One formula, with where its delimiters and TeX are in the text when that is known."""

    index: int
    block: int
    kind: SpanKind
    tex: str
    """The formula as the parser reads it."""
    open: str
    close: str
    open_start: int | None
    tex_start: int | None
    tex_end: int | None
    close_end: int | None

    @property
    def hash(self) -> str:
        return sha256(self.tex)

    @property
    def written(self) -> str:
        return f"{self.open}{self.tex}{self.close}"


@dataclass(frozen=True, slots=True)
class InlineText:
    """One inline content: a paragraph's or heading's text, or a table cell's."""

    content: str
    provenance: Provenance | None
    excluded: tuple[tuple[int, int, str], ...]
    """Content ranges of formulas, code spans, automatic links, images, and the brackets,
    address and title of links, with their token types (``link`` for a link's), in order."""


@dataclass(frozen=True, slots=True)
class Block:
    index: int
    kind: str
    parent: int | None
    depth: int
    lines: tuple[int, int]
    """The block's lines, ``[first, last + 1)``, without trailing blank lines."""
    start: int
    """Where the block's own text begins: after the markers of the blocks it is in."""
    end: int
    """The end of its last line."""
    prefix: str
    """What precedes the block on its first line: the markers of the blocks it is in."""
    continuation: str
    """What precedes its other lines: the prefix, with list markers as spaces."""
    text: str
    """The block's own text: its lines without the prefixes of the blocks it is in."""
    firsts: tuple[int, ...]
    """Where the block's own text begins on each of its lines."""
    attrs: dict[str, object] = field(default_factory=dict[str, object])
    spans: tuple[int, ...] = ()
    inlines: tuple[InlineText, ...] = ()
    children: tuple[int, ...] = ()

    @property
    def hash(self) -> str:
        return sha256(self.text)

    @property
    def structural(self) -> str | None:
        """The block's structural kind (LD-40), if it is one."""
        if self.kind == "paragraph":
            return "figure" if self.attrs.get("figure") else None
        return self.kind if self.kind in STRUCTURAL else None


@dataclass(frozen=True)
class Parsed:
    """A text's blocks in document order (a parent before its children) and its spans."""

    text: str
    blocks: tuple[Block, ...]
    spans: tuple[Span, ...]
    front_matter: FrontMatter | None

    def descendants(self, index: int) -> Iterator[int]:
        for child in self.blocks[index].children:
            yield child
            yield from self.descendants(child)

    def ancestors(self, index: int) -> Iterator[int]:
        parent = self.blocks[index].parent
        while parent is not None:
            yield parent
            parent = self.blocks[parent].parent


def merged(excluded: Iterable[tuple[int, int, str]]) -> tuple[list[int], list[int]]:
    """Ranges in order merged where they overlap, as their starts and their ends."""
    starts: list[int] = []
    ends: list[int] = []
    for low, high, _ in sorted(excluded):
        if starts and low <= ends[-1]:
            ends[-1] = max(ends[-1], high)
        else:
            starts.append(low)
            ends.append(high)
    return starts, ends


def overlaps(ranges: tuple[list[int], list[int]], start: int, end: int) -> bool:
    """Whether ``[start, end)``, which is not empty, meets one of the merged ranges."""
    starts, ends = ranges
    index = bisect_right(starts, end - 1) - 1
    return index >= 0 and start < ends[index]


@dataclass
class Budget:
    """What one change may still parse, in characters (``MAX_PARSED``)."""

    left: int = MAX_PARSED

    def spend(self, characters: int) -> bool:
        """Take ``characters`` from what is left, if that much is left."""
        if characters > self.left:
            return False
        self.left -= characters
        return True

    def charge(self, characters: int) -> None:
        """Take ``characters``, or refuse the change as too large to check."""
        if not self.spend(characters):
            raise DocumentError(
                INVALID_REQUEST,
                f"checking this change would parse more than {MAX_PARSED} characters of "
                "text: send fewer operations in each patch",
            )


def check_text(text: str) -> None:
    """Refuse text a revision cannot hold: not UTF-8, a NUL, or over the size limit."""
    try:
        size = len(text.encode())
    except UnicodeEncodeError:
        raise DocumentError(
            INVALID_REQUEST, "the text is not valid Unicode: it has an unpaired surrogate"
        ) from None
    if "\x00" in text:
        raise DocumentError(INVALID_REQUEST, "the text contains a NUL character")
    if size > MAX_TEXT_BYTES:
        raise DocumentError(
            INVALID_REQUEST, f"the text has {size} bytes; a document holds at most {MAX_TEXT_BYTES}"
        )


BYTE_ORDER_MARK: Final = "\ufeff"
"""Dropped from the start of a document's text when it is saved: an encoding's mark, not
text, which would keep a first line from reading as a heading or a table."""


def normalize_newlines(text: str) -> str:
    """Line breaks as ``\\n``, as markdown-it reads them, so positions agree."""
    return text.replace("\r\n", "\n").replace("\r", "\n")


class _Builder:
    def __init__(self, text: str) -> None:
        self.text = text
        starts = [0]
        starts.extend(index + 1 for index, character in enumerate(text) if character == "\n")
        self.starts = starts
        self.blocks: list[dict[str, object]] = []
        self.spans: list[Span] = []
        self.front_matter: FrontMatter | None = None

    def line_end(self, line: int) -> int:
        return self.starts[line + 1] - 1 if line + 1 < len(self.starts) else len(self.text)

    def add(self, token: Token, kind: str, parent: int | None, depth: int) -> int:
        if depth > MAX_DEPTH:
            raise DocumentError(
                INVALID_REQUEST, f"blocks are nested more than {MAX_DEPTH} deep in this text"
            )
        if len(self.blocks) >= MAX_BLOCKS:
            raise DocumentError(INVALID_REQUEST, f"the text has more than {MAX_BLOCKS} blocks")
        lines = token.meta.get("_lines")
        firsts = token.meta.get("_firsts")
        if not isinstance(lines, tuple) or not isinstance(firsts, tuple):
            raise DocumentError(INVALID_REQUEST, f"the parser left a {kind} without its place")
        begin, end = int(lines[0]), int(lines[1])  # pyright: ignore[reportUnknownArgumentType]
        own = [int(first) for first in firsts]  # pyright: ignore[reportUnknownArgumentType, reportUnknownVariableType]
        # Trailing blank lines belong to no block.
        while end - begin > 1 and not self.text[
            own[end - begin - 1] : self.line_end(end - 1)
        ].strip(" \t"):
            end -= 1
        own = own[: end - begin]
        prefix = self.text[self.starts[begin] : own[0]]
        text = "\n".join(
            self.text[first : self.line_end(line)]
            for line, first in zip(range(begin, end), own, strict=True)
        )
        index = len(self.blocks)
        self.blocks.append(
            {
                "kind": kind,
                "parent": parent,
                "depth": depth,
                "lines": (begin, end),
                "start": own[0],
                "end": self.line_end(end - 1),
                "prefix": prefix,
                "continuation": re.sub(r"[^>\s]", " ", prefix),
                "text": text,
                "firsts": tuple(own),
                "attrs": _attrs(token, kind),
                "spans": [],
                "inlines": [],
                "children": [],
            }
        )
        if parent is not None:
            self._list(parent, "children").append(index)
        return index

    def _list(self, index: int, name: str) -> list[object]:
        value = self.blocks[index][name]
        assert isinstance(value, list)  # noqa: S101 - built here as a list
        return value  # pyright: ignore[reportUnknownVariableType]

    def span(self, block: int, **fields: object) -> None:
        if len(self.spans) >= MAX_SPANS:
            raise DocumentError(INVALID_REQUEST, f"the text has more than {MAX_SPANS} formulas")
        span = Span(index=len(self.spans), block=block, **fields)  # type: ignore[arg-type]
        self.spans.append(span)
        self._list(block, "spans").append(span.index)

    def inline(self, block: int, token: Token) -> None:
        """A paragraph's, heading's or table cell's text, and the formulas in it."""
        found = token.meta.get("_provenance")
        provenance = found if isinstance(found, Provenance) else None
        excluded: list[tuple[int, int, str]] = []
        children = token.children or []
        images = [child for child in children if child.type == "image"]
        visible = [
            child
            for child in children
            if child.type not in ("softbreak", "hardbreak")
            and not (child.type == "text" and not child.content.strip())
        ]
        if images and len(visible) == 1 and visible[0].type == "image":
            attrs = self.blocks[block]["attrs"]
            assert isinstance(attrs, dict)  # noqa: S101 - built here as a dict
            attrs["figure"] = True
        for child in children:
            start, end = child.meta.get("_start"), child.meta.get("_end")
            if not isinstance(start, int) or not isinstance(end, int):
                continue
            label_end = child.meta.get("_label_end")
            if child.type == "link_open" and isinstance(label_end, int) and label_end > start:
                # A link's text is text; its brackets, address and title are not.
                excluded.append((start, start + 1, "link"))
                excluded.append((label_end, end, "link"))
                continue
            excluded.append((start, end, child.type))
            kind = _INLINE_MATH.get(child.type)
            if kind is None:
                continue
            close = str(child.meta.get("close", child.markup))
            self.span(
                block,
                kind=kind,
                tex=child.content,
                open=child.markup,
                close=close,
                open_start=_source(provenance, start),
                tex_start=_source(provenance, start + len(child.markup)),
                tex_end=_source_end(provenance, end - len(close)),
                close_end=_source_end(provenance, end),
            )
        self._list(block, "inlines").append(
            InlineText(token.content, provenance, tuple(sorted(excluded)))
        )

    def equation(self, block: int, token: Token) -> None:
        close = str(token.meta.get("close", token.markup))
        opening = token.meta.get("_open")
        closing = token.meta.get("_close")
        closed = token.meta.get("_close_end")
        open_start = opening if isinstance(opening, int) else None
        tex_start = tex_end = close_end = None
        if open_start is not None:
            if token.markup == "$$":
                # The plugin's TeX is the text between the opening $$ and the closing one.
                tex_start = open_start + 2
                tex_end = tex_start + len(token.content)
                close_end = tex_end + 2
            elif isinstance(closing, int) and isinstance(closed, int):
                # An environment is its own delimiter: its TeX is all of it.
                kept = token.markup == ""
                tex_start = open_start if kept else open_start + len(token.markup)
                tex_end = closed if kept else closing
                close_end = closed
        self.span(
            block,
            kind="display",
            tex=token.content,
            open=token.markup,
            close=close,
            open_start=open_start,
            tex_start=tex_start,
            tex_end=tex_end,
            close_end=close_end,
        )

    def freeze(self) -> tuple[Block, ...]:
        frozen: list[Block] = []
        for index, fields in enumerate(self.blocks):
            frozen.append(
                Block(
                    index=index,
                    kind=str(fields["kind"]),
                    parent=fields["parent"],  # type: ignore[arg-type]
                    depth=fields["depth"],  # type: ignore[arg-type]
                    lines=fields["lines"],  # type: ignore[arg-type]
                    start=fields["start"],  # type: ignore[arg-type]
                    end=fields["end"],  # type: ignore[arg-type]
                    prefix=str(fields["prefix"]),
                    continuation=str(fields["continuation"]),
                    text=str(fields["text"]),
                    firsts=fields["firsts"],  # type: ignore[arg-type]
                    attrs=fields["attrs"],  # type: ignore[arg-type]
                    spans=tuple(self._list(index, "spans")),  # type: ignore[arg-type]
                    inlines=tuple(self._list(index, "inlines")),  # type: ignore[arg-type]
                    children=tuple(self._list(index, "children")),  # type: ignore[arg-type]
                )
            )
        return tuple(frozen)


def _source(provenance: Provenance | None, offset: int) -> int | None:
    return None if provenance is None else provenance.source(offset)


def _source_end(provenance: Provenance | None, offset: int) -> int | None:
    return None if provenance is None else provenance.source_end(offset)


def _attrs(token: Token, kind: str) -> dict[str, object]:
    if kind == "heading":
        return {"level": int(token.tag[1:])}
    if kind == "list":
        ordered = token.type == "ordered_list_open"
        start = token.attrs.get("start", 1) if ordered else None
        return {"ordered": ordered, **({"start": int(start)} if start is not None else {})}
    if kind == "list_item":
        return {"marker": f"{token.info}{token.markup}"}
    if kind == "code":
        return {"fence": token.markup, "info": token.info}
    if kind == "equation":
        return {"label": token.info or None, "delimiter": token.markup}
    if kind == "container":
        info = token.info.strip()
        name, _, rest = info.partition(" ")
        rest = rest.strip()
        label = _LABEL.fullmatch(rest)
        attrs: dict[str, object] = {"container": name, "info": rest or None}
        if label is not None:
            attrs["label"] = re.sub(r"\s+", "-", label[1].strip())
        return attrs
    if kind == "thematic_break":
        return {"markup": token.markup}
    return {}


def parse(text: str, *, documents: bool = True) -> Parsed:
    """The blocks and spans of ``text``, which must already have ``\\n`` line breaks."""
    check_text(text)
    builder = _Builder(text)
    stack: list[int] = []
    in_table = 0
    for token in tokens(text, documents=documents):
        if in_table:
            if token.type == "table_open":
                in_table += 1
            elif token.type == "table_close":
                in_table -= 1
                if not in_table:
                    stack.pop()
            elif token.type == "inline":
                builder.inline(stack[-1], token)
            continue
        if token.nesting == 1:
            kind = _OPEN.get(token.type, "other")
            index = builder.add(token, kind, stack[-1] if stack else None, len(stack))
            stack.append(index)
            if kind == "table":
                in_table = 1
        elif token.nesting == -1:
            stack.pop()
        elif token.type == "inline":
            if stack:
                builder.inline(stack[-1], token)
        else:
            kind = _LEAF.get(token.type, "other")
            index = builder.add(token, kind, stack[-1] if stack else None, len(stack))
            if kind == "equation":
                builder.equation(index, token)
            elif kind == "front_matter" and not stack:
                builder.front_matter = read_front_matter(token.content)
    return Parsed(
        text=text,
        blocks=builder.freeze(),
        spans=tuple(builder.spans),
        front_matter=builder.front_matter,
    )
