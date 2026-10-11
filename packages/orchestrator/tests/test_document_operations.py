# SPDX-License-Identifier: MIT
"""Patch operations on a revision's text: lists and quotes keep their markers, the named
blocks keep their IDs, and blocks a patch does not name stay as they were (LD-16 to LD-18)."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import TypeAdapter

from sandbox_science.orchestrator.documents.blocks import parse
from sandbox_science.orchestrator.documents.engine import apply_operations, unnamed_changes
from sandbox_science.orchestrator.documents.errors import DocumentError
from sandbox_science.orchestrator.documents.identity import Allocator, Mapped, compare, fresh
from sandbox_science.orchestrator.documents.operations import Operation, touches

OPERATIONS = TypeAdapter(list[Operation])


def _mapped(text: str) -> Mapped:
    return fresh(parse(text), Allocator(0, 0))


def _apply(text: str, *operations: dict[str, Any]) -> tuple[Mapped, Mapped, list[str]]:
    """The revision, the patched one, and the blocks the patch changed without naming."""
    head = _mapped(text)
    parsed = OPERATIONS.validate_python(list(operations))
    allocate = Allocator(len(head.block_ids), len(head.span_ids))
    final = apply_operations(head, parsed, allocate).mapped
    unnamed = unnamed_changes(head, final, [touches(operation, head) for operation in parsed])
    return head, final, unnamed


def _kinds(mapped: Mapped) -> dict[str, str]:
    return {
        block_id: block.kind
        for block_id, block in zip(mapped.block_ids, mapped.parsed.blocks, strict=True)
    }


LIST = "- one\n- two\n"
#        b1 list, b2 item, b3 "one", b4 item, b5 "two"


def test_replace_in_a_list_item_keeps_the_marker_and_the_indentation() -> None:
    head, final, unnamed = _apply(LIST, {"op": "replace", "block": "b3", "markdown": "uno\nmore"})
    assert final.parsed.text == "- uno\n  more\n- two\n"
    assert final.block_ids == head.block_ids
    assert compare(head, final).blocks["b3"] == "changed"
    assert not unnamed


def test_replace_in_a_quote_with_two_paragraphs_keeps_them_in_the_quote() -> None:
    _, final, unnamed = _apply(
        "> a $x$\n>\n> b\n", {"op": "replace", "block": "b2", "markdown": "x\n\ny $z$"}
    )
    assert final.parsed.text == "> x\n>\n> y $z$\n>\n> b\n"
    assert [block.kind for block in final.parsed.blocks] == ["quote", *["paragraph"] * 3]
    # The first new block of the named kind keeps the ID (LD-7, rule 1).
    assert final.block_ids[1] == "b2"
    assert not unnamed


def test_insert_before_the_first_block_of_a_list_item() -> None:
    _, final, unnamed = _apply(LIST, {"op": "insert", "before": "b3", "markdown": "zero"})
    assert final.parsed.text == "- zero\n\n  one\n- two\n"
    assert not unnamed


def test_insert_after_a_block_in_a_quote() -> None:
    _, final, unnamed = _apply("> a\n\nafter\n", {"op": "insert", "after": "b2", "markdown": "b"})
    assert final.parsed.text == "> a\n>\n> b\n\nafter\n"
    assert not unnamed


def test_insert_keeps_new_text_apart_from_lines_around_it() -> None:
    _, final, unnamed = _apply("# H\nText.\n", {"op": "insert", "before": "b2", "markdown": "New."})
    assert final.parsed.text == "# H\n\nNew.\n\nText.\n"
    _, final, unnamed = _apply("Text.\n# H\n", {"op": "insert", "after": "b1", "markdown": "New."})
    assert final.parsed.text == "Text.\n\nNew.\n\n# H\n"
    assert not unnamed


def test_insert_at_the_start_goes_after_the_front_matter() -> None:
    text = "---\ntitle: T\n---\nBody.\n"
    _, final, _ = _apply(text, {"op": "insert", "at": "start", "markdown": "First."})
    assert final.parsed.text == "---\ntitle: T\n---\n\nFirst.\n\nBody.\n"
    _, final, _ = _apply("Body.\n", {"op": "insert", "at": "start", "markdown": "First."})
    assert final.parsed.text == "First.\n\nBody.\n"
    _, final, _ = _apply("Body.\n", {"op": "insert", "at": "end", "markdown": "Last."})
    assert final.parsed.text == "Body.\n\nLast.\n"
    _, final, _ = _apply("", {"op": "insert", "at": "end", "markdown": "Only."})
    assert final.parsed.text == "Only.\n"


def test_move_keeps_the_id_and_reads_as_moved() -> None:
    text = "# H\n\nP $p$.\n\n| a |\n| - |\n| 1 |\n"
    head, final, unnamed = _apply(text, {"op": "move", "block": "b3", "before": "b2"})
    assert final.parsed.text == "# H\n\n| a |\n| - |\n| 1 |\n\nP $p$.\n"
    assert [_kinds(final)[block_id] for block_id in final.block_ids] == [
        "heading",
        "table",
        "paragraph",
    ]
    assert compare(head, final).blocks == {"b1": "kept", "b3": "moved", "b2": "kept"}
    assert not unnamed


def test_move_a_list_item() -> None:
    _, final, unnamed = _apply(
        "- one\n- two\n- three\n", {"op": "move", "block": "b6", "before": "b2"}
    )
    assert final.parsed.text == "- three\n\n- one\n- two\n"
    assert not unnamed


def test_delete_removes_a_blank_line_with_the_block() -> None:
    _, final, _ = _apply("A.\n\nB.\n\nC.\n", {"op": "delete", "block": "b2"})
    assert final.parsed.text == "A.\n\nC.\n"
    _, final, _ = _apply("A.\n\nB.\n", {"op": "delete", "block": "b2"})
    assert final.parsed.text == "A.\n"
    _, final, _ = _apply("A.\n\nB.", {"op": "delete", "block": "b2"})
    assert final.parsed.text == "A."
    _, final, _ = _apply("> a\n>\n> b\n", {"op": "delete", "block": "b3"})
    assert final.parsed.text == "> a\n"


def test_delete_the_first_block_of_a_list_item_gives_its_line_to_the_next() -> None:
    _, final, unnamed = _apply("- one\n\n  two\n- three\n", {"op": "delete", "block": "b3"})
    assert final.parsed.text == "- two\n- three\n"
    assert not unnamed
    _, final, _ = _apply("- one\n- two\n", {"op": "delete", "block": "b3"})
    assert final.parsed.text == "-\n- two\n"


def test_replace_math_across_lines_in_a_quote() -> None:
    text = "> Let $a\n> = b$ hold.\n"
    head, final, unnamed = _apply(text, {"op": "replace_math", "math": "m1", "tex": "c\n= d"})
    assert final.parsed.text == "> Let $c\n> = d$ hold.\n"
    assert final.span_ids == ("m1",)
    assert compare(head, final).spans == {"m1": "changed"}
    assert not unnamed


def test_replace_math_in_a_display_equation_and_an_environment() -> None:
    _, final, _ = _apply(
        "$$\nx = 1\n$$ (one)\n", {"op": "replace_math", "math": "m1", "tex": "\ny = 2\n"}
    )
    assert final.parsed.text == "$$\ny = 2\n$$ (one)\n"
    text = "Text:\n\n\\begin{align}\na &= b\n\\end{align}\n"
    _, final, _ = _apply(
        text,
        {"op": "replace_math", "math": "m1", "tex": "\\begin{align}\nc &= d\n\\end{align}"},
    )
    assert "c &= d" in final.parsed.text


def test_replace_math_that_would_break_the_formula_is_refused() -> None:
    for tex in ("a$b", "a\n\nb", " a"):
        with pytest.raises(DocumentError, match="does not read as that one formula"):
            _apply("Let $x$ be.\n", {"op": "replace_math", "math": "m1", "tex": tex})


def test_replace_text_across_a_line_in_a_list_item() -> None:
    text = "- the first line\n  and the second\n"
    head, final, unnamed = _apply(
        text,
        {"op": "replace_text", "block": "b3", "quote": "line\nand the", "with": "row,\nthen the"},
    )
    assert final.parsed.text == "- the first row,\n  then the second\n"
    assert final.block_ids == head.block_ids
    assert not unnamed


@pytest.mark.parametrize(
    ("operation", "message"),
    [
        ({"op": "replace_text", "block": "b1", "quote": "a", "with": "b"}, "occurs 4 times"),
        ({"op": "replace_text", "block": "b1", "quote": "zzz", "with": "b"}, "does not occur"),
        ({"op": "replace", "block": "b9", "markdown": "x"}, "no block b9"),
        ({"op": "replace", "block": "b1", "markdown": "\n \n"}, "has no text"),
        ({"op": "replace_math", "math": "m7", "tex": "x"}, "no math m7"),
    ],
)
def test_operations_that_cannot_apply_are_refused(operation: dict[str, Any], message: str) -> None:
    with pytest.raises(DocumentError, match=message) as raised:
        _apply("a banana\n", operation)
    assert raised.value.kind == "invalid_request"


@pytest.mark.parametrize(
    "operation",
    [
        {"op": "move", "block": "b1", "before": "b1"},
        {"op": "insert", "markdown": "x"},
        {"op": "insert", "markdown": "x", "before": "b1", "at": "end"},
        {"op": "move", "block": "b1"},
        {"op": "replace", "block": "1", "markdown": "x"},
        {"op": "rewrite", "block": "b1"},
        {"op": "replace", "block": "b1", "markdown": "x", "author": {"kind": "user"}},
    ],
)
def test_malformed_operations_do_not_validate(operation: dict[str, Any]) -> None:
    with pytest.raises(ValueError, match="validation error"):
        OPERATIONS.validate_python([operation])


def test_a_block_cannot_move_into_itself() -> None:
    with pytest.raises(DocumentError, match="into itself"):
        _apply("> a\n>\n> b\n", {"op": "move", "block": "b1", "after": "b3"})


def test_ld_18_an_unclosed_fence_changes_unnamed_blocks() -> None:
    text = "P.\n\nE $$x$$.\n\nS.\n"
    _, _, unnamed = _apply(text, {"op": "replace", "block": "b1", "markdown": "```\nopen"})
    assert unnamed == ["b2", "b3"]


def test_ld_18_text_that_joins_the_block_before_it_is_caught() -> None:
    # A list item replaced by plain text becomes the previous item's lazy continuation.
    _, _, unnamed = _apply(
        "- one\n- two\n- three\n", {"op": "replace", "block": "b4", "markdown": "plain"}
    )
    assert "b3" in unnamed


def test_ld_18_containers_of_named_blocks_may_change() -> None:
    text = "::: lemma\nBody $x$.\n:::\n\nAfter.\n"
    head, final, unnamed = _apply(text, {"op": "replace", "block": "b2", "markdown": "New $y$."})
    assert not unnamed
    statuses = compare(head, final).blocks
    assert statuses == {"b1": "changed", "b2": "changed", "b3": "kept"}


def test_operations_apply_in_turn() -> None:
    text = "A.\n\nB.\n\nC.\n"
    head, final, unnamed = _apply(
        text,
        {"op": "replace", "block": "b1", "markdown": "A2."},
        {"op": "move", "block": "b3", "before": "b1"},
        {"op": "delete", "block": "b2"},
    )
    assert final.parsed.text == "C.\n\nA2.\n"
    assert compare(head, final).blocks == {"b3": "moved", "b1": "changed"}
    assert not unnamed


# Found by the random documents of test_document_properties.py.


def test_deleting_a_quotes_last_block_keeps_the_next_quote_apart() -> None:
    text = "- > a\n  >\n  > b\n\n  > c\n"
    _, final, unnamed = _apply(text, {"op": "delete", "block": "b5"})
    assert final.parsed.text == "- > a\n  >\n\n  > c\n"
    assert not unnamed
    assert [block.kind for block in final.parsed.blocks].count("quote") == 2


def test_deleting_a_block_between_lines_of_text_leaves_them_apart() -> None:
    # A table interrupts the paragraph above it, and a heading the table; without the
    # table, a blank line must stay, or the heading's text would join the paragraph.
    _, final, unnamed = _apply(
        "Text above\n| a |\n| - |\n# Heading\n", {"op": "delete", "block": "b2"}
    )
    assert final.parsed.text == "Text above\n\n# Heading\n"
    assert not unnamed
    _, final, unnamed = _apply(
        "Text above\n| a |\n| - |\n\nText below\n", {"op": "delete", "block": "b2"}
    )
    assert final.parsed.text == "Text above\n\nText below\n"
    assert not unnamed


def test_deleting_one_of_two_equal_blocks_removes_its_own_id() -> None:
    text = "$$\nx = 1\n$$\n\n$$\nx = 1\n$$\n"
    _, final, unnamed = _apply(text, {"op": "delete", "block": "b2"})
    assert final.block_ids == ("b1",)
    assert not unnamed


def test_deleting_an_items_first_block_keeps_a_table_in_the_item() -> None:
    # On the marker's line, a table would take the marker in: 1. | a | is a table row.
    text = "1. Intro\n\n   | a | b |\n   | - | - |\n"
    _, final, unnamed = _apply(text, {"op": "delete", "block": "b3"})
    assert final.parsed.text == "1.\n   | a | b |\n   | - | - |\n"
    assert not unnamed
    assert [block.kind for block in final.parsed.blocks] == ["list", "list_item", "table"]


def test_deleting_an_items_first_block_keeps_an_indented_next_block() -> None:
    text = "- Intro\n\n     indented paragraph\n"
    _, final, unnamed = _apply(text, {"op": "delete", "block": "b3"})
    assert final.parsed.text == "-\n     indented paragraph\n"
    assert not unnamed


def test_a_block_moves_out_of_the_quote_that_holds_it() -> None:
    _, final, unnamed = _apply("> a\n>\n> b\n", {"op": "move", "block": "b3", "after": "b1"})
    assert final.parsed.text == "> a\n\nb\n"
    assert not unnamed
    with pytest.raises(DocumentError, match="replace b1 with it instead"):
        _apply("> only\n", {"op": "move", "block": "b2", "after": "b1"})


def test_a_move_brings_in_no_text_to_normalize() -> None:
    _, final, unnamed = _apply("A \\(x\\).\n\nB.\n", {"op": "move", "block": "b1", "after": "b2"})
    assert final.parsed.text == "B.\n\nA \\(x\\).\n"
    assert not unnamed
