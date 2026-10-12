# SPDX-License-Identifier: MIT
"""Turn a source draft into named operations before reconciling it (LD-13).

Containers are edited as whole top-level blocks. This deliberately conflicts with a
concurrent edit anywhere inside that container. Separating blank lines are laid out by
the ordinary patch engine; source drafts never grant permission to change other blocks.
"""

from __future__ import annotations

import re
from collections import Counter

from .blocks import Budget, parse
from .engine import apply_operations
from .errors import INVALID_REQUEST, DocumentError
from .identity import Allocator, BlockHint, Mapped, carry
from .normalize import normalize
from .operations import MAX_OPERATIONS, Delete, Insert, Move, Operation, Replace


def operations(base: Mapped, text: str, budget: Budget | None = None) -> list[Operation]:
    """Derive a bounded patch, refusing source layouts it cannot reproduce safely."""
    budget = budget or Budget()
    budget.charge(len(text))
    # These temporary IDs are never persisted; the patch engine allocates the real IDs.
    maximum = max((int(value[1:]) for value in base.block_ids), default=0)
    spans = max((int(value[1:]) for value in base.span_ids), default=0)
    parsed = normalize(parse(text), budget=budget).parsed
    roots = [(i, block) for i, block in enumerate(base.parsed.blocks) if block.parent is None]
    new_roots = [(i, block) for i, block in enumerate(parsed.blocks) if block.parent is None]
    if len(roots) * len(new_roots) > 40_000:
        raise DocumentError(INVALID_REQUEST, "split this source edit into smaller block edits")
    # A source edit names changed blocks even when another block moved across them.
    # Equal blocks pair first; unmatched roots with shared words pair globally before
    # carry's gap rule. Otherwise that rule can assign a deleted summary's ID to a
    # changed paragraph on the other side of a moved table (LD-F1).
    paired: dict[int, int] = {}
    taken: set[int] = set()
    for ni, new_block in new_roots:
        for oi, old_block in roots:
            if (
                oi not in taken
                and old_block.kind == new_block.kind
                and old_block.text.split() == new_block.text.split()
            ):
                paired[ni] = oi
                taken.add(oi)
                break
    scores: list[tuple[float, int, int]] = []
    tokens = re.compile(r"\\[A-Za-z@]+|\w+")
    old_tokens = {i: Counter(tokens.findall(block.text.lower())) for i, block in roots}
    new_tokens = {i: Counter(tokens.findall(block.text.lower())) for i, block in new_roots}
    old_counts = Counter(block.kind for _, block in roots)
    new_counts = Counter(block.kind for _, block in new_roots)
    for ni, new_block in new_roots:
        if ni in paired:
            continue
        words = new_tokens[ni]
        for oi, old_block in roots:
            if oi in taken or old_block.kind != new_block.kind:
                continue
            old_words = old_tokens[oi]
            shared = sum((words & old_words).values())
            smaller = min(sum(words.values()), sum(old_words.values()))
            if (shared and shared * 2 >= smaller) or (
                old_counts[old_block.kind] == new_counts[new_block.kind] == 1
            ):
                scores.append((shared / max(1, smaller), oi, ni))
    for _, oi, ni in sorted(scores, key=lambda item: (-item[0], item[1], item[2])):
        if oi not in taken and ni not in paired:
            paired[ni] = oi
            taken.add(oi)
    hints = [
        BlockHint(
            base.block_ids[oi],
            parsed.blocks[ni].kind,
            parsed.blocks[ni].start,
            parsed.blocks[ni].end,
        )
        for ni, oi in paired.items()
    ]
    candidate = carry(
        base,
        parsed,
        Allocator(maximum, spans),
        block_hints=hints,
        removed=frozenset(base.block_ids[oi] for oi, _ in roots if oi not in taken),
    )
    old = {
        base.block_ids[i]: block
        for i, block in enumerate(base.parsed.blocks)
        if block.parent is None
    }
    new = {
        candidate.block_ids[i]: block
        for i, block in enumerate(candidate.parsed.blocks)
        if block.parent is None
    }
    old_ids, new_ids = list(old), list(new)
    if any(value in base.block_ids and value not in old for value in new_ids):
        raise DocumentError(INVALID_REQUEST, "edit nested structure by its containing block")
    result: list[Operation] = [
        Delete(op="delete", block=value) for value in old_ids if value not in new
    ]
    current = [value for value in old_ids if value in new]
    desired = [value for value in new_ids if value in old]
    for index, value in enumerate(desired):
        if current[index] != value:
            result.append(Move(op="move", block=value, before=current[index]))
            current.remove(value)
            current.insert(index, value)
    for value in desired:
        if old[value].text != new[value].text:
            result.append(Replace(op="replace", block=value, markdown=new[value].text))
    index = 0
    while index < len(new_ids):
        if new_ids[index] in old:
            index += 1
            continue
        start = index
        while index < len(new_ids) and new_ids[index] not in old:
            index += 1
        markdown = "\n\n".join(new[value].text.rstrip("\n") for value in new_ids[start:index])
        if index < len(new_ids):
            result.append(Insert(op="insert", markdown=markdown, before=new_ids[index]))
        elif start > 0:
            result.append(Insert(op="insert", markdown=markdown, after=new_ids[start - 1]))
        else:
            result.append(Insert(op="insert", markdown=markdown, at="end"))
    if len(result) > MAX_OPERATIONS:
        raise DocumentError(
            INVALID_REQUEST,
            f"split this source edit into at most {MAX_OPERATIONS} block operations",
        )
    if result:
        final = apply_operations(base, result, Allocator(maximum, spans), budget).mapped

        def reading(mapped: Mapped) -> list[tuple[str, str]]:
            return [
                (block.kind, block.text.strip())
                for block in mapped.parsed.blocks
                if block.parent is None
            ]

        if reading(final) != reading(candidate):
            raise DocumentError(
                INVALID_REQUEST,
                "this source layout cannot be expressed safely as block edits; "
                "edit a containing block",
            )
    return result
