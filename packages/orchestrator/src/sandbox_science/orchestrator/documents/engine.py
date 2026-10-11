# SPDX-License-Identifier: MIT
"""How a patch becomes a revision, without storage (LD-7, LD-8, LD-17, LD-18, LD-39).

A patch's operations apply one after another, each to the text the one before left, by
block and span IDs. After each, the text is parsed again, the new Markdown it brought in
is normalized to the dollar form (LD-42), and IDs are carried over (LD-7) with the
operation's named block keeping its ID. At the end, every block of the revision the patch
applied to that it did not name must be there unchanged, apart from the blocks containing
the named ones (LD-18).

A patch based on an older revision is first checked against the revisions after its base
(LD-17): it conflicts when one of them changed or removed a block or span it names, moved
a block it moves, or put a block where it inserts or moves one. Otherwise it is rebased:
it applies to the current revision by ID.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Any, Final, cast

from .blocks import Budget, Parsed, parse
from .errors import INTERNAL, INVALID_REQUEST, DocumentError
from .identity import Allocator, BlockHint, Changes, Mapped, carry
from .normalize import normalize
from .operations import Operation, Touches, apply, touches, unlay
from .parser import PROFILE

MAX_COMPARED: Final = 250_000
"""Pairs of characters compared at most to count what a revision wrote in a changed block;
beyond it, all the block's changed part counts."""

Json = dict[str, Any]


@dataclass
class Applied:
    """A patch's operations applied: the final parse with IDs, and what was normalized."""

    mapped: Mapped
    converted: Counter[str] = field(default_factory=Counter[str])
    kept: list[dict[str, str]] = field(default_factory=list[dict[str, str]])

    @property
    def normalized(self) -> Json:
        return {"converted": dict(self.converted), "kept": self.kept}


def _widened(hints: Iterable[BlockHint], grown: int) -> tuple[BlockHint, ...]:
    """Hints whose regions take in what normalization added inside them."""
    if grown <= 0:
        return tuple(hints)
    return tuple(
        BlockHint(hint.block_id, hint.kind, hint.start, hint.end + grown) for hint in hints
    )


def apply_operations(
    head: Mapped,
    operations: Sequence[Operation],
    allocate: Allocator,
    budget: Budget | None = None,
) -> Applied:
    """The operations applied in turn to ``head``, normalizing the text they bring in.

    Each parse takes from ``budget``, so a patch of many operations on a long text is
    refused before it takes long to check.
    """
    budget = budget or Budget()
    length = max(1, len(head.parsed.text))
    if length * len(operations) > budget.left:
        most = max(1, budget.left // length)
        raise DocumentError(
            INVALID_REQUEST,
            f"the text has {len(head.parsed.text)} characters, and a patch to it has at most "
            f"{most} operation{'' if most == 1 else 's'}: split this one",
        )
    applied = Applied(head)
    current = head
    for operation in operations:
        step = apply(current, operation, budget)
        budget.charge(len(step.text))
        parsed = parse(step.text)
        hints = step.block_hints
        if step.region is not None:
            normalized = normalize(parsed, step.region, budget)
            applied.converted.update(normalized.converted)
            applied.kept.extend(normalized.kept)
            hints = _widened(hints, len(normalized.text) - len(step.text))
            parsed = normalized.parsed
        current = carry(
            current,
            parsed,
            allocate,
            block_hints=hints,
            span_hints=step.span_hints,
            removed=step.removed,
        )
        if step.expect is not None:
            # TeX reads whitespace alike, and the parser keeps a block's indentation in
            # some lines of a formula and not in others.
            span_id, tex = step.expect
            index = current.span_index(span_id)
            found = ""
            if index is not None:
                span = current.parsed.spans[index]
                found = unlay(span.tex, current.parsed.blocks[span.block].continuation)
            if index is None or found.split() != tex.split():
                raise DocumentError(
                    INVALID_REQUEST,
                    f"the new TeX of math {span_id} does not read as that one formula in its "
                    "place: it may hold its delimiters, a blank line, or spaces just inside "
                    "single dollars",
                )
    applied.mapped = current
    return applied


def _ancestors(mapped: Mapped, block_id: str) -> set[str]:
    index = mapped.index(block_id)
    if index is None:
        return set()
    return {mapped.block_ids[ancestor] for ancestor in mapped.parsed.ancestors(index)}


def _descendants(mapped: Mapped, block_id: str) -> set[str]:
    index = mapped.index(block_id)
    if index is None:
        return set()
    return {mapped.block_ids[child] for child in mapped.parsed.descendants(index)}


def _reading(mapped: Mapped, index: int) -> tuple[object, ...]:
    """What a block a patch does not name must keep: its kind, text and attributes, its
    formulas, and where its text has code, links and images, which a link reference
    definition elsewhere can change."""
    block = mapped.parsed.blocks[index]
    spans = mapped.parsed.spans
    return (
        block.kind,
        block.hash,
        block.attrs,
        [(spans[span].kind, unlay(spans[span].tex, block.continuation)) for span in block.spans],
        [inline.excluded for inline in block.inlines],
    )


def unnamed_changes(head: Mapped, final: Mapped, named: Iterable[Touches]) -> list[str]:
    """Blocks of ``head`` that ``final`` changed, moved or lost without the patch naming them.

    A patch may change the blocks it rewrites, remove those it replaces or deletes with
    everything inside them, move the blocks it moves, and change the blocks that contain
    any block it names. Every other block must read as before (``_reading``) inside the
    same block: one that an unclosed container takes in, or that a closing marker leaves
    outside its container, is changed. A container of a named block may still lose its
    ID by the identity rules (LD-7, rule 4): the blocks in it then have a new parent of
    the same kind, which is not a change of theirs.
    """
    exempt: set[str] = set()
    moved: set[str] = set()
    for touched in named:
        for block_id in touched.blocks:
            exempt |= _ancestors(head, block_id)
        exempt.update(touched.changes)
        exempt.update(touched.holds)
        moved.update(touched.moves)
        for block_id in touched.removes:
            exempt.add(block_id)
            exempt |= _descendants(head, block_id)
    head_index = {block_id: index for index, block_id in enumerate(head.block_ids)}
    final_index = {block_id: index for index, block_id in enumerate(final.block_ids)}

    def same_parent(index: int, found: int) -> bool:
        before, after = head.parent_id(index), final.parent_id(found)
        if before == after:
            return True
        return (
            before is not None
            and after is not None
            and before in exempt
            and before not in final_index
            and after not in head_index
            and head.parsed.blocks[head_index[before]].kind
            == final.parsed.blocks[final_index[after]].kind
        )

    changed: list[str] = []
    for index, block_id in enumerate(head.block_ids):
        if block_id in exempt:
            continue
        found = final_index.get(block_id)
        if (
            found is None
            or _reading(head, index) != _reading(final, found)
            or (block_id not in moved and not same_parent(index, found))
        ):
            changed.append(block_id)
    return changed


@dataclass
class Since:
    """What the revisions after a patch's base did, by ID, and who did it."""

    changed: set[str] = field(default_factory=set[str])
    removed: set[str] = field(default_factory=set[str])
    moved: set[str] = field(default_factory=set[str])
    added: set[str] = field(default_factory=set[str])
    spans_changed: set[str] = field(default_factory=set[str])
    spans_removed: set[str] = field(default_factory=set[str])
    by: defaultdict[str, list[Json]] = field(default_factory=lambda: defaultdict(list))

    def note(self, revision: int, author: Json, block_map: Json) -> None:
        """Take in one revision's block map."""
        who = {"revision": revision, "author": author}
        for entry in cast(list[Json], block_map.get("blocks", [])):
            status = entry.get("status")
            block_id = str(entry["id"])
            if status in ("changed", "moved", "added"):
                {"changed": self.changed, "moved": self.moved, "added": self.added}[
                    str(status)
                ].add(block_id)
                self.by[block_id].append(who)
            for span in cast(list[Json], entry.get("spans", [])):
                if span.get("status") == "changed":
                    self.spans_changed.add(str(span["id"]))
                    self.by[str(span["id"])].append(who)
        for entry in cast(list[Json], block_map.get("removed", [])):
            self.removed.add(str(entry["id"]))
            self.by[str(entry["id"])].append(who)
        for entry in cast(list[Json], block_map.get("removed_spans", [])):
            self.spans_removed.add(str(entry["id"]))
            self.by[str(entry["id"])].append(who)


def neighbor(mapped: Mapped, block_id: str | None, side: str) -> str | None:
    """The block beside ``block_id`` among its siblings, or at an end of the text."""
    blocks = mapped.parsed.blocks
    if block_id is None:
        top = [block.index for block in blocks if block.parent is None]
        if side == "start":
            top = [index for index in top if blocks[index].kind != "front_matter"]
            return mapped.block_ids[top[0]] if top else None
        return mapped.block_ids[top[-1]] if top else None
    index = mapped.index(block_id)
    if index is None:
        return None
    parent = blocks[index].parent
    siblings = (
        list(blocks[parent].children)
        if parent is not None
        else [block.index for block in blocks if block.parent is None]
    )
    position = siblings.index(index) + (-1 if side == "before" else 1)
    return mapped.block_ids[siblings[position]] if 0 <= position < len(siblings) else None


def _text(mapped: Mapped, block_id: str) -> str | None:
    index = mapped.index(block_id)
    return None if index is None else mapped.parsed.blocks[index].text


def conflicts(
    base: Mapped, head: Mapped, operations: Sequence[Operation], since: Since
) -> list[Json]:
    """Why the patch cannot be rebased onto ``head`` (LD-17), block by block."""
    found: dict[str, Json] = {}

    def block_conflict(block_id: str, why: str) -> None:
        if block_id in found:
            return
        index = base.index(block_id)
        kind = base.parsed.blocks[index].kind if index is not None else None
        found[block_id] = {
            "block": block_id,
            "kind": kind,
            "why": why,
            "at_base": _text(base, block_id),
            "now": _text(head, block_id),
            "changed_by": since.by.get(block_id, []),
        }

    def placed(anchor: str | None, side: str) -> None:
        then, now = neighbor(base, anchor, side), neighbor(head, anchor, side)
        if now is not None and now != then and now in (since.added | since.moved):
            block_conflict(now, f"put {side} {anchor}" if anchor else f"put at the {side}")

    for operation in operations:
        touched = touches(operation, base)
        for block_id in (*touched.changes, *touched.removes):
            if block_id in since.removed:
                block_conflict(block_id, "removed")
            elif block_id in since.changed:
                block_conflict(block_id, "changed")
        for block_id in touched.moves:
            for status, ids in (("removed", since.removed), ("changed", since.changed)):
                if block_id in ids:
                    block_conflict(block_id, status)
            if block_id in since.moved:
                block_conflict(block_id, "moved")
        for block_id, side in touched.anchors:
            if block_id in since.removed:
                block_conflict(block_id, "removed")
            elif block_id in since.changed:
                block_conflict(block_id, "changed")
            else:
                placed(block_id, side)
        for end in touched.ends:
            placed(None, end)
        for span_id in touched.spans:
            if span_id in since.spans_changed or span_id in since.spans_removed:
                index = base.span_index(span_id)
                now_index = head.span_index(span_id)
                found.setdefault(
                    span_id,
                    {
                        "math": span_id,
                        "why": "removed" if span_id in since.spans_removed else "changed",
                        "at_base": base.parsed.spans[index].tex if index is not None else None,
                        "now": head.parsed.spans[now_index].tex if now_index is not None else None,
                        "changed_by": since.by.get(span_id, []),
                    },
                )
    return list(found.values())


def check_names(mapped: Mapped, operations: Sequence[Operation]) -> None:
    """Every block and span the operations name exists in the revision they were written for."""
    for operation in operations:
        touched = touches(operation, mapped)
        for block_id in touched.blocks:
            if mapped.index(block_id) is None:
                raise DocumentError(
                    INVALID_REQUEST, f"revision has no block {block_id}: read the document again"
                )
        for span_id in touched.spans:
            if mapped.span_index(span_id) is None:
                raise DocumentError(
                    INVALID_REQUEST, f"revision has no math {span_id}: read the document again"
                )


def block_map(mapped: Mapped, changes: Changes) -> Json:
    """The block map a revision stores (LD-8), with each block's and span's status."""
    parsed = mapped.parsed
    blocks: list[Json] = []
    for block in parsed.blocks:
        block_id = mapped.block_ids[block.index]
        blocks.append(
            {
                "id": block_id,
                "kind": block.kind,
                "parent": mapped.parent_id(block.index),
                "lines": list(block.lines),
                "start": block.start,
                "end": block.end,
                "hash": block.hash,
                "status": changes.blocks[block_id],
                "structural": block.structural,
                "attrs": block.attrs,
                "spans": [
                    {
                        "id": mapped.span_ids[index],
                        "kind": parsed.spans[index].kind,
                        "hash": parsed.spans[index].hash,
                        "status": changes.spans[mapped.span_ids[index]],
                    }
                    for index in block.spans
                ],
            }
        )
    return {
        "profile": PROFILE,
        "blocks": blocks,
        "removed": [{"id": block_id, "kind": kind} for block_id, kind in changes.removed],
        "removed_spans": [
            {"id": span_id, "block": block_id} for span_id, block_id in changes.removed_spans
        ],
    }


def attach(parsed: Parsed, stored: Json) -> Mapped:
    """A stored block map's IDs on a new parse of the same revision's text."""
    entries = cast(list[Json], stored.get("blocks", []))
    if stored.get("profile") != PROFILE:
        raise DocumentError(
            INTERNAL,
            f"the block map was made with profile {stored.get('profile')}; this version reads "
            f"{PROFILE}",
        )
    if len(entries) != len(parsed.blocks) or any(
        entry["kind"] != block.kind or tuple(entry["lines"]) != block.lines
        for entry, block in zip(entries, parsed.blocks, strict=False)
    ):
        raise DocumentError(INTERNAL, "the stored block map does not match the revision's text")
    span_ids: list[str] = [""] * len(parsed.spans)
    for entry, block in zip(entries, parsed.blocks, strict=True):
        spans = cast(list[Json], entry.get("spans", []))
        if len(spans) != len(block.spans):
            raise DocumentError(INTERNAL, "the stored block map does not match the revision's math")
        for index, span in zip(block.spans, spans, strict=True):
            span_ids[index] = str(span["id"])
    return Mapped(parsed, tuple(str(entry["id"]) for entry in entries), tuple(span_ids))


def _changed_characters(old: str, new: str) -> int:
    """Characters ``new`` adds or changes in ``old``."""
    shorter = min(len(old), len(new))
    low = 0
    while low < shorter and old[low] == new[low]:
        low += 1
    high = 0
    while high < shorter - low and old[-1 - high] == new[-1 - high]:
        high += 1
    old_middle, new_middle = old[low : len(old) - high], new[low : len(new) - high]
    if not new_middle or len(old_middle) * len(new_middle) > MAX_COMPARED:
        return len(new_middle)
    matcher = SequenceMatcher(None, old_middle, new_middle, autojunk=False)
    return sum(
        end - start
        for tag, _, _, start, end in matcher.get_opcodes()
        if tag in ("insert", "replace")
    )


def written(parent: Mapped | None, child: Mapped, changes: Changes) -> int:
    """Characters of text a revision added or changed, in blocks without blocks inside."""
    total = 0
    before = (
        {} if parent is None else dict(zip(parent.block_ids, parent.parsed.blocks, strict=True))
    )
    for index, block in enumerate(child.parsed.blocks):
        if block.children:
            continue
        status = changes.blocks[child.block_ids[index]]
        if status == "added" or parent is None:
            total += len(block.text)
        elif status == "changed":
            found = before.get(child.block_ids[index])
            total += _changed_characters(found.text if found is not None else "", block.text)
    return total


def structural_added(child: Mapped, changes: Changes) -> list[Json]:
    """The structural blocks a revision added (LD-40)."""
    return [
        {"id": child.block_ids[block.index], "kind": block.structural}
        for block in child.parsed.blocks
        if block.structural is not None and changes.blocks[child.block_ids[block.index]] == "added"
    ]


def summary(changes: Changes, mapped: Mapped) -> Json:
    """The blocks and spans a revision changed, for results and events."""
    kinds = {mapped.block_ids[block.index]: block.kind for block in mapped.parsed.blocks}
    return {
        "changed": changes.ids("changed"),
        "moved": changes.ids("moved"),
        "added": [{"id": block_id, "kind": kinds[block_id]} for block_id in changes.ids("added")],
        "removed": [block_id for block_id, _ in changes.removed],
        "math_changed": changes.span_ids("changed"),
        "math_added": changes.span_ids("added"),
        "math_removed": [span_id for span_id, _ in changes.removed_spans],
    }
