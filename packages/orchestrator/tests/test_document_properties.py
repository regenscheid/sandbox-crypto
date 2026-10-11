# SPDX-License-Identifier: MIT
"""Properties of the parse, normalization and IDs over random documents (seeded).

Each document is built from the profile's constructs, nested in lists, quotes and
containers. For every one: each block's own text and each formula's TeX can be read back
from the positions the parse recorded; normalization leaves the blocks and formulas as
they were; and a revision that changes nothing keeps every ID.
"""

from __future__ import annotations

import random
from typing import Any

import pytest
from pydantic import TypeAdapter

from sandbox_science.orchestrator.documents.blocks import Block, Parsed, parse
from sandbox_science.orchestrator.documents.engine import apply_operations, unnamed_changes
from sandbox_science.orchestrator.documents.errors import DocumentError
from sandbox_science.orchestrator.documents.identity import (
    Allocator,
    Mapped,
    carry,
    compare,
    fresh,
)
from sandbox_science.orchestrator.documents.normalize import normalize, signature
from sandbox_science.orchestrator.documents.operations import Operation, touches

OPERATIONS = TypeAdapter(list[Operation])

WORDS = ("prime", "group", "field", "order", "$5", "x", "\u03b1", "😀", "a\u00a0b", "\\$", "`$y$`")
FORMULAS = (
    "$a^{p-1}$",
    "$\\F_{17}^\\times$",
    "$$x = y$$",
    "\\(x^2\\)",
    "\\( y \\)",
    "\\[z\\]",
    "$x \\$ y$",
    "\\begin{align} a &= b \\end{align}",
    "$a\n= b$",
)


def _inline(rng: random.Random) -> str:
    parts = [rng.choice(WORDS + FORMULAS) for _ in range(rng.randint(1, 6))]
    return " ".join(parts)


def _leaf(rng: random.Random) -> str:
    kind = rng.randrange(9)
    if kind == 0:
        return f"# {_inline(rng)}"
    if kind == 1:
        return (
            "$$\n"
            + rng.choice(("x = 1", "a \\\\ b", "\\sum_i i"))
            + "\n$$"
            + rng.choice(("", " (eq)"))
        )
    if kind == 2:
        return "\\[\n" + rng.choice(("x", "y = 2")) + "\n\\]"
    if kind == 3:
        return "```\n" + _inline(rng) + "\n```"
    if kind == 4:
        header = _inline(rng).replace("|", "")
        return f"| {header} | b \\| c |\n| - | - |\n| {rng.choice(FORMULAS)} | 2 |"
    if kind == 5:
        return "\\begin{equation}\nE = mc^2\n\\end{equation}"
    if kind == 6:
        return f"{_inline(rng)}\n{_inline(rng)}"
    if kind == 7:
        return f"Setext {_inline(rng)}\n---"
    return _inline(rng)


def _block(rng: random.Random, depth: int, tabs: bool) -> str:
    if depth > 3 or rng.random() < 0.5:
        return _leaf(rng)
    inner = "\n\n".join(_block(rng, depth + 1, tabs) for _ in range(rng.randint(1, 3)))
    choice = rng.randrange(4)
    if choice == 0:
        return "\n".join(f"> {line}" if line else ">" for line in inner.split("\n"))
    if choice == 1:
        # A tab after a marker at a column off the tab stops leaves the generated lines
        # indented past the item's content, which the position checks want and the
        # patch checks cannot keep (operations.py, _delete).
        marker = rng.choice(("- ", "1. ", "*\t" if tabs else "*   "))
        pad = " " * len(marker.expandtabs(4))
        lines = inner.split("\n")
        return "\n".join([marker + lines[0], *(pad + line if line else "" for line in lines[1:])])
    if choice == 2:
        kind = rng.choice(("lemma", "proof", "claim"))
        return f"::: {kind}\n{inner}\n:::"
    return inner


def _document(seed: int, *, tabs: bool = True) -> str:
    rng = random.Random(seed)  # noqa: S311 - test data, not secrets
    head = "---\ntitle: T\n---\n" if rng.random() < 0.3 else ""
    body = "\n\n".join(_block(rng, 0, tabs) for _ in range(rng.randint(1, 6)))
    return head + body + rng.choice(("", "\n", "\n\n"))


def _hidden(text: str, lines: tuple[int, int], firsts: tuple[int, ...]) -> set[int]:
    starts = [0, *(index + 1 for index, character in enumerate(text) if character == "\n")]
    hidden: set[int] = set()
    for line, first in zip(range(*lines), firsts, strict=True):
        hidden.update(range(starts[line], first))
    return hidden


def _check_positions(parsed: Parsed) -> None:
    text = parsed.text
    for block in parsed.blocks:
        hidden = _hidden(text, block.lines, block.firsts)
        own = "".join(text[at] for at in range(block.start, block.end) if at not in hidden)
        assert own == block.text, block
    for span in parsed.spans:
        block = parsed.blocks[span.block]
        if span.tex_start is None or span.tex_end is None:
            continue
        if block.kind == "equation" and span.open == "$$":
            # The plugin's TeX is the text between the $$ as it is, markers and all.
            assert text[span.tex_start : span.tex_end] == span.tex, span
            continue
        hidden = _hidden(text, block.lines, block.firsts)
        own = "".join(text[at] for at in range(span.tex_start, span.tex_end) if at not in hidden)
        if block.kind == "table":
            own = own.replace("\\|", "|")
        if block.kind == "equation":
            # A \[ or \begin block's last line loses all its indentation in the TeX, as the
            # browser's rule has it, and its other lines only the blocks' around it.
            assert own.split() == span.tex.split(), span
            continue
        assert own == span.tex, (span, text[span.tex_start : span.tex_end])


@pytest.mark.parametrize("seed", range(300))
def test_random_documents(seed: int) -> None:
    text = _document(seed)
    parsed = parse(text)
    _check_positions(parsed)
    normalized = normalize(parsed)
    assert signature(normalized.parsed) == signature(parsed), text
    _check_positions(normalized.parsed)
    first = fresh(parsed, Allocator(0, 0))
    again = carry(first, parse(text), Allocator(len(first.block_ids), len(first.span_ids)))
    changes = compare(first, again)
    assert set(changes.blocks.values()) <= {"kept"}
    assert set(changes.spans.values()) <= {"kept"}


def _patched(head: Mapped, operation: dict[str, Any]) -> tuple[Mapped, list[str]]:
    parsed = OPERATIONS.validate_python([operation])
    allocate = Allocator(len(head.block_ids), len(head.span_ids))
    final = apply_operations(head, parsed, allocate).mapped
    return final, unnamed_changes(head, final, [touches(found, head) for found in parsed])


@pytest.mark.parametrize("seed", range(120))
def test_patches_in_random_documents_touch_only_what_they_name(seed: int) -> None:
    head = fresh(parse(_document(seed, tabs=False)), Allocator(0, 0))
    for block, block_id in zip(head.parsed.blocks, head.block_ids, strict=True):
        if block.children:
            continue
        if block.kind == "paragraph":
            final, unnamed = _patched(
                head, {"op": "replace", "block": block_id, "markdown": "Replaced $q$."}
            )
            assert not unnamed, (head.parsed.text, block_id, final.parsed.text)
            index = final.index(block_id)
            assert index is not None
            assert final.parsed.blocks[index].text == "Replaced $q$."
        if block.kind != "front_matter":
            final, unnamed = _patched(
                head, {"op": "insert", "after": block_id, "markdown": "Inserted $r$."}
            )
            assert not unnamed, (head.parsed.text, block_id, final.parsed.text)
            assert "Inserted $r$." in [found.text for found in final.parsed.blocks]
        final, unnamed = _patched(head, {"op": "delete", "block": block_id})
        if _between_like_lists(head, block):
            # Two lists with the same marker become one, whatever separates them:
            # LD-18 refuses the delete rather than merge them.
            continue
        assert not unnamed, (head.parsed.text, block_id, final.parsed.text)
        assert block_id not in final.block_ids


@pytest.mark.parametrize("seed", range(120))
def test_moves_and_formulas_in_random_documents(seed: int) -> None:
    head = fresh(parse(_document(seed, tabs=False)), Allocator(0, 0))
    top = [block for block in head.parsed.blocks if block.parent is None]
    last = head.block_ids[top[-1].index]
    for block, block_id in zip(head.parsed.blocks, head.block_ids, strict=True):
        if block.children or block.kind == "front_matter" or block_id == last:
            continue
        if _between_like_lists(head, block):
            continue
        operation = {"op": "move", "block": block_id, "after": last}
        if head.parsed.blocks[top[-1].index].children == (block.index,):
            # The only block of the last one moves after it only when an empty line of
            # it stays; otherwise the move is refused, since the last block goes too.
            refused = _refusal(head, operation)
            if refused is not None:
                assert "replace" in refused
                continue
        final, unnamed = _patched(head, operation)
        assert not unnamed, (head.parsed.text, block_id, final.parsed.text)
        index = final.index(block_id)
        assert index is not None
        assert final.parsed.blocks[index].text == block.text
    for span, span_id in zip(head.parsed.spans, head.span_ids, strict=True):
        if span.tex_start is None or span.open not in ("$", "$$", "\\(", "\\["):
            continue
        final, unnamed = _patched(head, {"op": "replace_math", "math": span_id, "tex": "z"})
        assert not unnamed, (head.parsed.text, span_id, final.parsed.text)
        index = final.span_index(span_id)
        assert index is not None
        assert final.parsed.spans[index].tex.strip() == "z"


def _refusal(head: Mapped, operation: dict[str, Any]) -> str | None:
    """The message an operation is refused with, if it is."""
    try:
        _patched(head, operation)
    except DocumentError as error:
        return error.message
    return None


def _between_like_lists(mapped: Mapped, block: Block) -> bool:
    """Whether the block alone keeps two lists with the same marker apart."""
    blocks = mapped.parsed.blocks
    siblings = (
        list(blocks[block.parent].children)
        if block.parent is not None
        else [found.index for found in blocks if found.parent is None]
    )
    position = siblings.index(block.index)
    if not 0 < position < len(siblings) - 1:
        return False
    before, after = blocks[siblings[position - 1]], blocks[siblings[position + 1]]

    def marker(found: Block) -> object:
        return (
            str(blocks[found.children[0]].attrs.get("marker", ""))[-1:] if found.children else None
        )

    return before.kind == after.kind == "list" and marker(before) == marker(after)
