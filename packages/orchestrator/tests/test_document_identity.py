# SPDX-License-Identifier: MIT
"""Block and math-span IDs across revisions, rule by rule (LD-7), and their statuses (LD-8)."""

from __future__ import annotations

import time

from sandbox_science.orchestrator.documents.blocks import parse
from sandbox_science.orchestrator.documents.identity import (
    Allocator,
    BlockHint,
    Mapped,
    SpanHint,
    carry,
    compare,
    fresh,
    increasing,
)


def _first(text: str) -> tuple[Mapped, Allocator]:
    allocate = Allocator(0, 0)
    return fresh(parse(text), allocate), allocate


def _ids(mapped: Mapped) -> dict[str, str]:
    """Each block's text, first line, by its ID."""
    return {
        block_id: block.text.split("\n")[0]
        for block_id, block in zip(mapped.block_ids, mapped.parsed.blocks, strict=True)
    }


def test_equal_blocks_keep_their_ids_wherever_they_move() -> None:
    old, allocate = _first("A.\n\nB.\n\nC.\n")
    new = carry(old, parse("C.\n\nA.\n\nB.\n"), allocate)
    assert _ids(new) == {"b3": "C.", "b1": "A.", "b2": "B."}
    assert compare(old, new).blocks == {"b3": "moved", "b1": "kept", "b2": "kept"}


def test_a_deleted_copy_of_a_repeated_block_loses_its_own_id() -> None:
    old, allocate = _first("A.\n\nTODO\n\nB.\n\nTODO\n")
    new = carry(old, parse("A.\n\nB.\n\nTODO\n"), allocate)
    # The TODO that kept its place among the others keeps its ID; the first one's goes.
    assert new.block_ids == ("b1", "b3", "b4")
    assert compare(old, new).removed == (("b2", "paragraph"),)


def test_whitespace_does_not_count_for_equal_text_but_the_status_shows_the_change() -> None:
    old, allocate = _first("One two\nthree.\n")
    new = carry(old, parse("One two three.\n"), allocate)
    assert new.block_ids == ("b1",)
    assert compare(old, new).blocks == {"b1": "changed"}


def test_a_labelled_equation_keeps_its_id_through_a_rewrite() -> None:
    old, allocate = _first("Intro.\n\n$$a = b$$ (main)\n\n$$c = d$$ (other)\n")
    new = carry(old, parse("$$x^2 = y$$ (other)\n\nIntro.\n\n$$p = q$$ (main)\n"), allocate)
    assert _ids(new) == {"b3": "$$x^2 = y$$ (other)", "b1": "Intro.", "b2": "$$p = q$$ (main)"}


def test_similar_blocks_between_matched_ones_keep_their_ids() -> None:
    old, allocate = _first(
        "# Title\n\nThe group of units modulo seventeen is cyclic.\n\n"
        "An unrelated remark.\n\n# End\n"
    )
    new = carry(
        old,
        parse(
            "# Title\n\nA new opening paragraph.\n\nThe group of units modulo 17 is cyclic, "
            "of order sixteen.\n\n# End\n"
        ),
        allocate,
    )
    statuses = compare(old, new)
    # The rewritten paragraph shares most of its words with the old one. The new one
    # shares none with the remark, but each is the only paragraph left in that gap.
    assert new.block_ids[2] == "b2"
    assert statuses.blocks["b2"] == "changed"
    assert new.block_ids[1] == "b3"
    assert statuses.blocks["b3"] == "changed"


def test_dissimilar_blocks_get_new_ids_when_more_than_one_is_left() -> None:
    old, allocate = _first(
        "# Title\n\nThe group of units modulo seventeen is cyclic.\n\n"
        "An unrelated remark.\n\n# End\n"
    )
    new = carry(
        old,
        parse(
            "# Title\n\nA new opening.\n\nThe group of units modulo 17 is cyclic.\n\n"
            "Another new one.\n\n# End\n"
        ),
        allocate,
    )
    assert new.block_ids == ("b1", "b5", "b2", "b6", "b4")
    assert compare(old, new).removed == (("b3", "paragraph"),)


def test_the_only_remaining_block_of_a_kind_keeps_its_id() -> None:
    old, allocate = _first("# Old heading\n\nText.\n")
    new = carry(old, parse("# Something else entirely\n\nText.\n"), allocate)
    assert new.block_ids == ("b1", "b2")
    assert compare(old, new).blocks == {"b1": "changed", "b2": "kept"}


def test_blocks_of_another_kind_never_take_an_id() -> None:
    old, allocate = _first("Text.\n\nA paragraph about groups.\n")
    new = carry(old, parse("Text.\n\n# A paragraph about groups\n"), allocate)
    assert new.block_ids == ("b1", "b3")


def test_a_named_block_keeps_its_id_as_the_first_of_its_kind() -> None:
    old, allocate = _first("Intro.\n\nOld.\n")
    text = "Intro.\n\n# Heading\n\nFirst new.\n\nSecond new.\n"
    region = (text.index("# Heading"), len(text))
    new = carry(old, parse(text), allocate, block_hints=[BlockHint("b2", "paragraph", *region)])
    assert _ids(new) == {"b1": "Intro.", "b3": "# Heading", "b2": "First new.", "b4": "Second new."}


def test_math_spans_keep_their_ids_within_their_block() -> None:
    old, allocate = _first("Let $a$, $b$ and $c$ be given.\n")
    new = carry(old, parse("Let $c$, $a$, $b'$ and $d$ be given.\n"), allocate)
    # Unchanged TeX first, wherever it went; then the rest in order between them.
    assert new.span_ids == ("m3", "m1", "m2", "m4")
    assert compare(old, new).spans == {"m3": "kept", "m1": "kept", "m2": "changed", "m4": "added"}


def test_a_named_span_keeps_its_id() -> None:
    old, allocate = _first("Let $a$ and $b$ be.\n")
    new = carry(
        old,
        parse("Let $b$ and $z$ be.\n"),
        allocate,
        span_hints=[SpanHint("m1", "b1", 0)],
    )
    assert new.span_ids == ("m1", "m2")
    assert compare(old, new).spans == {"m1": "changed", "m2": "changed"}


def test_spans_of_a_removed_block_are_removed_and_a_new_blocks_are_added() -> None:
    old, allocate = _first("A $x$.\n\nB $y$.\n")
    new = carry(old, parse("B $y$.\n\nC $z$.\n"), allocate)
    changes = compare(old, new)
    assert changes.removed_spans == (("m1", "b1"),)
    assert changes.spans == {"m2": "kept", "m3": "added"}


def test_moving_into_another_container_reads_as_moved() -> None:
    old, allocate = _first("> quoted\n\nplain\n")
    new = carry(old, parse("> quoted\n>\n> plain\n"), allocate)
    statuses = compare(old, new).blocks
    assert statuses["b3"] == "moved"
    assert statuses["b1"] == "changed"


def test_ids_are_never_reused() -> None:
    old, allocate = _first("A.\n\nB.\n")
    once = carry(old, parse("A.\n"), allocate)
    twice = carry(once, parse("A.\n\nB.\n"), allocate)
    assert twice.block_ids == ("b1", "b3")


def test_increasing_finds_the_longest_run() -> None:
    assert increasing([(0, 0), (1, 4), (2, 1), (3, 2), (4, 3)]) == [(0, 0), (2, 1), (3, 2), (4, 3)]
    assert increasing([]) == []


def test_a_rewrite_of_every_block_stays_quick() -> None:
    old_text = "\n\n".join(
        f"Paragraph {index} about the group of order {index}." for index in range(3000)
    )
    new_text = "\n\n".join(
        f"Section {index}: the group of order {index} again." for index in range(3000)
    )
    old, allocate = _first(old_text)
    started = time.monotonic()
    new = carry(old, parse(new_text), allocate)
    assert time.monotonic() - started < 30
    assert len(new.block_ids) == 3000
