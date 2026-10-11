# SPDX-License-Identifier: MIT
"""Block and math-span IDs carried from one revision to the next (LD-7, LD-8).

Only the server assigns IDs. A new parse takes its IDs from the parent revision's blocks
by these rules, in order:

1. a block an operation replaces or moves keeps its ID: the first block of its kind in
   the text the operation wrote;
2. a block whose kind and text, with whitespace collapsed, equal those of a parent block
   keeps that block's ID, wherever it moved. Equal blocks are paired along their longest
   common order first, so that a deleted copy of a repeated block loses its own ID, and
   then in document order;
3. a display equation whose label is unchanged keeps its ID;
4. between consecutive blocks paired in the same order in both revisions, each remaining
   block takes the ID of the remaining parent block of its kind with which it shares the
   most words and TeX commands, if they share at least half of the smaller block's, or if
   it is the only remaining block of its kind on both sides of that gap; the best pairs
   are taken first, and ties go in document order;
5. any other block gets a new ID.

Math spans are paired the same way within their block: spans an operation names, then
unchanged TeX, then the remaining spans in order between paired ones.

The statuses compare a revision with its parent by ID (LD-8): ``kept``; ``moved``, when
the block is no longer in the same order among the blocks both revisions share, or has
another parent; ``changed``, when its text changed (a container changes with its
children); ``removed``; and ``added``.
"""

from __future__ import annotations

import re
from bisect import bisect_left
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from itertools import pairwise
from typing import Final

from .blocks import Block, Parsed

_TOKEN: Final = re.compile(r"\\[A-Za-z@]+|\\.|\w+")
MAX_PAIRS: Final = 40_000
"""Pairs of blocks compared in one gap; beyond it, each block is compared only with the
parent blocks near its own place in the gap."""
BAND: Final = 100


@dataclass(frozen=True, slots=True)
class BlockHint:
    """An operation's named block: the first block of ``kind`` starting in the text the
    operation wrote, ``[start, end)``, keeps ``block_id`` (rule 1)."""

    block_id: str
    kind: str
    start: int
    end: int


@dataclass(frozen=True, slots=True)
class SpanHint:
    """An operation's named span: the span at ``position`` in the block that keeps
    ``block_id`` keeps ``span_id``."""

    span_id: str
    block_id: str
    position: int


@dataclass(frozen=True)
class Mapped:
    """A parse with an ID for each of its blocks and spans, in the parse's order."""

    parsed: Parsed
    block_ids: tuple[str, ...]
    span_ids: tuple[str, ...]

    def index(self, block_id: str) -> int | None:
        try:
            return self.block_ids.index(block_id)
        except ValueError:
            return None

    def span_index(self, span_id: str) -> int | None:
        try:
            return self.span_ids.index(span_id)
        except ValueError:
            return None

    def parent_id(self, index: int) -> str | None:
        parent = self.parsed.blocks[index].parent
        return None if parent is None else self.block_ids[parent]


@dataclass
class Allocator:
    """New IDs for a document, from its counters."""

    next_block: int
    next_span: int
    issued: list[str] = field(default_factory=list[str])

    def block(self) -> str:
        self.next_block += 1
        self.issued.append(f"b{self.next_block}")
        return f"b{self.next_block}"

    def span(self) -> str:
        self.next_span += 1
        return f"m{self.next_span}"


def _normalized(block: Block) -> tuple[str, str]:
    return block.kind, " ".join(block.text.split())


def _words(text: str) -> Counter[str]:
    return Counter(
        token if token.startswith("\\") else token.lower() for token in _TOKEN.findall(text)
    )


def increasing(pairs: Sequence[tuple[int, int]]) -> list[tuple[int, int]]:
    """The longest run of ``pairs``, sorted by their first item, increasing in the second."""
    ordered = sorted(pairs)
    tails: list[int] = []
    tail_index: list[int] = []
    previous: list[int] = [-1] * len(ordered)
    for index, (_, second) in enumerate(ordered):
        at = bisect_left(tails, second)
        if at == len(tails):
            tails.append(second)
            tail_index.append(index)
        else:
            tails[at] = second
            tail_index[at] = index
        previous[index] = tail_index[at - 1] if at > 0 else -1
    run: list[tuple[int, int]] = []
    index = tail_index[-1] if tail_index else -1
    while index != -1:
        run.append(ordered[index])
        index = previous[index]
    return run[::-1]


def _pair_equal(
    old_items: Sequence[int],
    new_items: Sequence[int],
    old_key: Callable[[int], object],
    new_key: Callable[[int], object],
) -> list[tuple[int, int]]:
    """Pair items with equal keys: along their longest common order, then in order."""
    old_keys = [old_key(item) for item in old_items]
    new_keys = [new_key(item) for item in new_items]
    matcher = SequenceMatcher(None, old_keys, new_keys, autojunk=False)
    pairs: list[tuple[int, int]] = []
    taken_old: set[int] = set()
    taken_new: set[int] = set()
    for a, b, size in matcher.get_matching_blocks():
        for step in range(size):
            pairs.append((old_items[a + step], new_items[b + step]))
            taken_old.add(a + step)
            taken_new.add(b + step)
    waiting: defaultdict[object, list[int]] = defaultdict(list)
    for position, key in enumerate(old_keys):
        if position not in taken_old:
            waiting[key].append(old_items[position])
    for position, key in enumerate(new_keys):
        if position not in taken_new and waiting.get(key):
            pairs.append((waiting[key].pop(0), new_items[position]))
    return pairs


def _similar(
    old_blocks: Sequence[Block],
    new_blocks: Sequence[Block],
    old_gap: list[int],
    new_gap: list[int],
) -> list[tuple[int, int]]:
    """Rule 4 within one gap, for blocks of one kind."""
    if not old_gap or not new_gap:
        return []
    old_words = {item: _words(old_blocks[item].text) for item in old_gap}
    new_words = {item: _words(new_blocks[item].text) for item in new_gap}
    banded = len(old_gap) * len(new_gap) > MAX_PAIRS
    scale = len(old_gap) / len(new_gap)
    candidates: list[tuple[int, int, int]] = []
    for new_position, new_item in enumerate(new_gap):
        mine = new_words[new_item]
        mine_size = sum(mine.values())
        if banded:
            middle = int(new_position * scale)
            near = old_gap[max(0, middle - BAND) : middle + BAND]
        else:
            near = old_gap
        for old_item in near:
            theirs = old_words[old_item]
            smaller = min(mine_size, sum(theirs.values()))
            if not smaller:
                continue
            shared = sum((mine & theirs).values())
            if 2 * shared >= smaller:
                candidates.append((shared, new_item, old_item))
    candidates.sort(key=lambda found: (-found[0], found[1], found[2]))
    pairs: list[tuple[int, int]] = []
    taken_old: set[int] = set()
    taken_new: set[int] = set()
    for _, new_item, old_item in candidates:
        if new_item not in taken_new and old_item not in taken_old:
            pairs.append((old_item, new_item))
            taken_old.add(old_item)
            taken_new.add(new_item)
    rest_old = [item for item in old_gap if item not in taken_old]
    rest_new = [item for item in new_gap if item not in taken_new]
    if len(rest_old) == 1 and len(rest_new) == 1:
        pairs.append((rest_old[0], rest_new[0]))
    return pairs


def _pair_blocks(
    old: Mapped, new: Parsed, hints: Iterable[BlockHint], removed: Iterable[str] = ()
) -> dict[int, int]:
    """New block index to the parent block index whose ID it takes."""
    old_blocks, new_blocks = old.parsed.blocks, new.blocks
    pairs: dict[int, int] = {}
    used: set[int] = set()
    # Blocks an operation replaced or deleted keep their IDs by rule 1 alone, so that an
    # equal block elsewhere does not take the ID of the one that went.
    gone = {index for index in (old.index(block_id) for block_id in removed) if index is not None}

    def take(old_item: int, new_item: int) -> None:
        pairs[new_item] = old_item
        used.add(old_item)

    # 1. Named blocks keep their IDs.
    for hint in hints:
        old_item = old.index(hint.block_id)
        if old_item is None or old_item in used:
            continue
        for new_item, block in enumerate(new_blocks):
            if (
                new_item not in pairs
                and block.kind == hint.kind
                and hint.start <= block.start < max(hint.end, hint.start + 1)
            ):
                take(old_item, new_item)
                break
    used |= gone
    # 2. Equal blocks.
    for old_item, new_item in _pair_equal(
        [item for item in range(len(old_blocks)) if item not in used],
        [item for item in range(len(new_blocks)) if item not in pairs],
        lambda item: _normalized(old_blocks[item]),
        lambda item: _normalized(new_blocks[item]),
    ):
        take(old_item, new_item)
    # 3. Equations with an unchanged label.
    labelled: defaultdict[object, list[int]] = defaultdict(list)
    for old_item, block in enumerate(old_blocks):
        if old_item not in used and block.kind == "equation" and block.attrs.get("label"):
            labelled[block.attrs["label"]].append(old_item)
    for new_item, block in enumerate(new_blocks):
        label = block.attrs.get("label") if block.kind == "equation" else None
        if new_item not in pairs and label and labelled.get(label):
            take(labelled[label].pop(0), new_item)
    # 4. Similar blocks between blocks paired in the same order.
    anchors = increasing(list(pairs.items()))
    bounds = [(-1, -1), *anchors, (len(new_blocks), len(old_blocks))]
    for (new_low, old_low), (new_high, old_high) in pairwise(bounds):
        new_gap = [item for item in range(new_low + 1, new_high) if item not in pairs]
        old_gap = [item for item in range(old_low + 1, old_high) if item not in used]
        kinds = {new_blocks[item].kind for item in new_gap} & {
            old_blocks[item].kind for item in old_gap
        }
        for kind in sorted(kinds):
            for old_item, new_item in _similar(
                old_blocks,
                new_blocks,
                [item for item in old_gap if old_blocks[item].kind == kind],
                [item for item in new_gap if new_blocks[item].kind == kind],
            ):
                take(old_item, new_item)
    return pairs


def _pair_spans(
    old: Mapped, new: Parsed, old_block: int, new_block: int, named: dict[int, str]
) -> dict[int, int]:
    """New span index to parent span index, within one pair of blocks."""
    old_spans = list(old.parsed.blocks[old_block].spans)
    new_spans = list(new.blocks[new_block].spans)
    pairs: dict[int, int] = {}
    used: set[int] = set()
    # 1. Named spans.
    for position, span_id in named.items():
        old_span = old.span_index(span_id)
        if old_span in old_spans and position < len(new_spans) and old_span not in used:
            pairs[new_spans[position]] = old_span
            used.add(old_span)
    # 2. Unchanged TeX.
    for old_span, new_span in _pair_equal(
        [span for span in old_spans if span not in used],
        [span for span in new_spans if span not in pairs],
        lambda span: old.parsed.spans[span].tex,
        lambda span: new.spans[span].tex,
    ):
        pairs[new_span] = old_span
        used.add(old_span)
    # 3. The rest, in order, between paired spans.
    order_old = {span: position for position, span in enumerate(old_spans)}
    order_new = {span: position for position, span in enumerate(new_spans)}
    anchors = increasing(
        [(order_new[new_span], order_old[old_span]) for new_span, old_span in pairs.items()]
    )
    bounds = [(-1, -1), *anchors, (len(new_spans), len(old_spans))]
    for (new_low, old_low), (new_high, old_high) in pairwise(bounds):
        new_gap = [
            new_spans[at] for at in range(new_low + 1, new_high) if new_spans[at] not in pairs
        ]
        old_gap = [
            old_spans[at] for at in range(old_low + 1, old_high) if old_spans[at] not in used
        ]
        for old_span, new_span in zip(old_gap, new_gap, strict=False):
            pairs[new_span] = old_span
            used.add(old_span)
    return pairs


def carry(
    old: Mapped,
    new: Parsed,
    allocate: Allocator,
    *,
    block_hints: Iterable[BlockHint] = (),
    span_hints: Iterable[SpanHint] = (),
    removed: Iterable[str] = (),
) -> Mapped:
    """IDs for ``new`` from ``old`` (LD-7); the blocks in ``removed`` were replaced or
    deleted by the operation that made ``new``, and keep their IDs only by its hints."""
    block_pairs = _pair_blocks(old, new, block_hints, removed)
    block_ids = [
        old.block_ids[block_pairs[item]] if item in block_pairs else allocate.block()
        for item in range(len(new.blocks))
    ]
    named: defaultdict[str, dict[int, str]] = defaultdict(dict)
    for hint in span_hints:
        named[hint.block_id][hint.position] = hint.span_id
    span_ids: list[str | None] = [None] * len(new.spans)
    for new_block, old_block in block_pairs.items():
        for new_span, old_span in _pair_spans(
            old, new, old_block, new_block, named.get(block_ids[new_block], {})
        ).items():
            span_ids[new_span] = old.span_ids[old_span]
    return Mapped(
        parsed=new,
        block_ids=tuple(block_ids),
        span_ids=tuple(span_id if span_id is not None else allocate.span() for span_id in span_ids),
    )


def fresh(parsed: Parsed, allocate: Allocator) -> Mapped:
    """New IDs for every block and span: a document's first revision."""
    return Mapped(
        parsed=parsed,
        block_ids=tuple(allocate.block() for _ in parsed.blocks),
        span_ids=tuple(allocate.span() for _ in parsed.spans),
    )


@dataclass(frozen=True)
class Changes:
    """How a revision's blocks and spans differ from its parent's, by ID (LD-8)."""

    blocks: dict[str, str]
    """Each block ID of either revision: kept, moved, changed, removed or added."""
    spans: dict[str, str]
    removed: tuple[tuple[str, str], ...]
    """Removed blocks, with their kinds."""
    removed_spans: tuple[tuple[str, str], ...]
    """Removed spans, with the IDs of the blocks they were in."""

    def ids(self, *statuses: str) -> list[str]:
        return [block_id for block_id, status in self.blocks.items() if status in statuses]

    def span_ids(self, *statuses: str) -> list[str]:
        return [span_id for span_id, status in self.spans.items() if status in statuses]


def compare(parent: Mapped | None, child: Mapped) -> Changes:
    """The statuses of ``child``'s blocks and spans against ``parent``'s."""
    if parent is None:
        return Changes(
            blocks=dict.fromkeys(child.block_ids, "added"),
            spans=dict.fromkeys(child.span_ids, "added"),
            removed=(),
            removed_spans=(),
        )
    old_position = {block_id: index for index, block_id in enumerate(parent.block_ids)}
    shared = [
        (index, old_position[block_id])
        for index, block_id in enumerate(child.block_ids)
        if block_id in old_position
    ]
    in_order = {child.block_ids[index] for index, _ in increasing(shared)}
    statuses: dict[str, str] = {}
    for index, block_id in enumerate(child.block_ids):
        old = old_position.get(block_id)
        if old is None:
            statuses[block_id] = "added"
        elif parent.parsed.blocks[old].hash != child.parsed.blocks[index].hash:
            statuses[block_id] = "changed"
        elif block_id not in in_order or parent.parent_id(old) != child.parent_id(index):
            statuses[block_id] = "moved"
        else:
            statuses[block_id] = "kept"
    removed = tuple(
        (block_id, parent.parsed.blocks[index].kind)
        for index, block_id in enumerate(parent.block_ids)
        if block_id not in statuses
    )
    old_spans = {span_id: index for index, span_id in enumerate(parent.span_ids)}
    span_statuses: dict[str, str] = {}
    for index, span_id in enumerate(child.span_ids):
        old = old_spans.get(span_id)
        if old is None:
            span_statuses[span_id] = "added"
        else:
            before, after = parent.parsed.spans[old], child.parsed.spans[index]
            same = (before.tex, before.kind) == (after.tex, after.kind)
            span_statuses[span_id] = "kept" if same else "changed"
    removed_spans = tuple(
        (span_id, parent.block_ids[parent.parsed.spans[index].block])
        for index, span_id in enumerate(parent.span_ids)
        if span_id not in span_statuses
    )
    return Changes(
        blocks=statuses, spans=span_statuses, removed=removed, removed_spans=removed_spans
    )
