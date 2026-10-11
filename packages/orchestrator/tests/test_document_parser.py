# SPDX-License-Identifier: MIT
"""The server's Markdown parser: the shared cases, positions, front matter and limits.

The shared cases are the files the browser's tests read too
(``web/workspace/src/lib/*.fixtures.json``), so both parsers are held to the same
blocks and math spans in CI (LD-5, LD-9; LD-F2).
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from markdown_it.token import Token

from sandbox_science.orchestrator.documents.blocks import (
    MAX_BLOCKS,
    MAX_DEPTH,
    MAX_TEXT_BYTES,
    parse,
)
from sandbox_science.orchestrator.documents.errors import DocumentError
from sandbox_science.orchestrator.documents.frontmatter import MAX_FRONT_MATTER, read_front_matter
from sandbox_science.orchestrator.documents.parser import MAX_BLOCK_LINES, tokens

LIB = Path(__file__).resolve().parents[3] / "web" / "workspace" / "src" / "lib"
MATH = {"math_inline", "math_inline_double", "math_block", "math_block_label"}


def _cases(name: str) -> list[dict[str, Any]]:
    return json.loads((LIB / name).read_text())["cases"]


def _math(found: list[Token]) -> Iterator[Token]:
    for token in found:
        if token.type in MATH:
            yield token
        if token.children:
            yield from _math(token.children)


@pytest.mark.parametrize("case", _cases("dollarmath.fixtures.json"), ids=lambda case: case["name"])
def test_ld_f2_the_dollar_cases(case: dict[str, Any]) -> None:
    found = [[token.type, token.content, token.info] for token in _math(tokens(case["markdown"]))]
    assert found == case["math"]


@pytest.mark.parametrize(
    "case", _cases("mathdelimiters.fixtures.json"), ids=lambda case: case["name"]
)
def test_ld_f2_latex_delimiters(case: dict[str, Any]) -> None:
    found = [
        [token.type, token.content, token.markup, token.meta.get("close", token.markup)]
        for token in _math(tokens(case["markdown"]))
    ]
    assert found == case["math"]


@pytest.mark.parametrize("case", _cases("documents.fixtures.json"), ids=lambda case: case["name"])
def test_ld_f2_blocks_and_spans(case: dict[str, Any]) -> None:
    parsed = parse(case["markdown"])
    blocks = [[block.kind, block.depth, *block.lines] for block in parsed.blocks]
    assert blocks == case["blocks"]
    assert [[span.block, span.kind, span.tex] for span in parsed.spans] == case["math"]


def test_the_shared_cases_cover_the_profile() -> None:
    # LD-F2 lists what the cases must cover; the names say where.
    names = " ".join(case["name"] for case in _cases("documents.fixtures.json"))
    for construct in (
        "not math",
        "escaped dollars",
        "inside a paragraph",
        "never closes",
        "front matter",
        "nested containers",
        "labelled equation",
        "raw HTML",
    ):
        assert construct in names


def test_the_line_limit_is_the_browsers() -> None:
    inside = "x\n" * (MAX_BLOCK_LINES - 1)
    assert [token.type for token in _math(tokens(f"$$\n{inside}$$"))] == ["math_block"]
    longer = "x\n" * MAX_BLOCK_LINES
    assert [token.type for token in _math(tokens(f"$$\n{longer}$$"))] == ["math_inline_double"]
    assert [token.type for token in _math(tokens(f"\\[\n{longer}\\]"))] == ["math_inline_double"]


def test_raw_html_is_text() -> None:
    # html is off: no html_block or html_inline token, whatever the text holds.
    found = tokens("<script>alert(1)</script>\n\n<b>bold</b> and <i>$x$</i>\n")
    types = {token.type for token in found} | {
        child.type for token in found for child in token.children or []
    }
    assert not types & {"html_block", "html_inline"}


def test_links_only_to_http_https_and_mailto() -> None:
    # As the browser has it, so that a relative link's text parses alike on both sides.
    parsed = parse("[a](javascript:alert(1)) [b](https://x.org) [c](notes$y$.md)\n")
    assert [span.tex for span in parsed.spans] == ["y"]
    links = [
        child.attrs.get("href")
        for token in tokens("[a](javascript:x) [b](https://x.org) [m](mailto:a@b.c)")
        for child in token.children or []
        if child.type == "link_open"
    ]
    assert links == ["https://x.org", "mailto:a@b.c"]


SOURCES = [
    "Let $p = 17$ and $a \\in \\F_p^\\times$.",
    "- one $a$\n- two\n  > quoted $x^2$\n  > across $y\n  > = 1$ lines\n",
    "> > deep $z$\n> > more\n",
    "| $a$ | b \\| c | $x \\| y$ |\n| - | - | - |\n| 1 | $2$ | $\\alpha$ |\n",
    "# Heading $h$ ##\n\nSetext $s$\n===\n",
    "1. \\(x\\) and \\[y\\]\n2. \\begin{align} a &= b \\end{align}\n",
    "-\tTab $t$\n\n  \tmore $u$\n",
    "::: lemma\n$$\nE = mc^2\n$$ (energy)\n:::\n",
    "> \\[\n> w\n> \\]\n",
    "Emoji 😀 then $\\mathbb{F}_{17}$ and \u00a0 $𝑥$.\n",  # noqa: RUF001 - a math letter
]


def _without_prefixes(
    source: str, block_lines: tuple[int, int], firsts: tuple[int, ...]
) -> set[int]:
    """The offsets of the markers and indentation before a block's own text on its lines."""
    starts = [0, *(index + 1 for index, character in enumerate(source) if character == "\n")]
    hidden: set[int] = set()
    for line, first in zip(range(*block_lines), firsts, strict=True):
        hidden.update(range(starts[line], first))
    return hidden


@pytest.mark.parametrize("source", SOURCES)
def test_each_span_knows_its_place_in_the_text(source: str) -> None:
    """Every formula's TeX, and its delimiters, sit where the span says."""
    parsed = parse(source)
    assert parsed.spans
    for span in parsed.spans:
        assert span.open_start is not None
        assert span.tex_start is not None
        assert span.tex_end is not None
        assert span.close_end is not None
        written = source[span.open_start : span.close_end]
        assert written.startswith(span.open)
        assert written.endswith(span.close)
        block = parsed.blocks[span.block]
        # The TeX in the text holds the markers of the lines it crosses, and a table's
        # escaped pipes; without them it is the span's TeX.
        hidden = _without_prefixes(source, block.lines, block.firsts)
        own = "".join(
            source[offset] for offset in range(span.tex_start, span.tex_end) if offset not in hidden
        )
        if block.kind == "table":
            own = own.replace("\\|", "|")
        assert own == span.tex, (span, source[span.tex_start : span.tex_end])


def test_blocks_know_their_own_text() -> None:
    source = "- item one\n- item two\n  > quoted $x$\n  > more\n\n::: lemma\nBody.\n:::\n"
    parsed = parse(source)
    by_kind = [
        (block.kind, block.prefix, block.continuation, block.text) for block in parsed.blocks
    ]
    assert ("paragraph", "- ", "  ", "item one") in by_kind
    assert ("quote", "  ", "  ", "> quoted $x$\n> more") in by_kind
    assert ("paragraph", "  > ", "  > ", "quoted $x$\nmore") in by_kind
    assert ("container", "", "", "::: lemma\nBody.\n:::") in by_kind
    for block in parsed.blocks:
        hidden = _without_prefixes(source, block.lines, block.firsts)
        own = "".join(
            source[offset] for offset in range(block.start, block.end) if offset not in hidden
        )
        assert own == block.text


def test_front_matter_gives_the_title_and_macros() -> None:
    parsed = parse(
        "---\ntitle: Fermat\nmacros:\n  \\F: \\mathbb{F}\n  Z: \\mathbb{Z}\n---\nText.\n"
    )
    assert parsed.front_matter is not None
    assert parsed.front_matter.title == "Fermat"
    assert parsed.front_matter.macros == {"\\F": "\\mathbb{F}", "\\Z": "\\mathbb{Z}"}
    assert parsed.front_matter.problems == ()


@pytest.mark.parametrize(
    ("yaml", "problem"),
    [
        ("a: &x [1]\nb: *x", "aliases are not allowed"),
        ("- a\n- b", "not a mapping"),
        ("title: [1, 2]", "title is not text"),
        ("macros: [1]", "macros is not a mapping"),
        ("macros:\n  '\\a b': x", "is not a name"),
        ("title: 'open", "not YAML"),
        ("[" * 200 + "]" * 200, ""),
    ],
)
def test_front_matter_that_is_not_a_title_and_macros_is_reported(yaml: str, problem: str) -> None:
    found = read_front_matter(yaml)
    assert found.problems
    assert problem in " ".join(found.problems)
    assert found.macros == {}


def test_front_matter_is_limited_in_size() -> None:
    found = read_front_matter("title: " + "x" * MAX_FRONT_MATTER)
    assert found.title is None
    assert "not read" in found.problems[0]


def test_text_over_the_size_limit_is_refused() -> None:
    with pytest.raises(DocumentError, match="bytes"):
        parse("é" * (MAX_TEXT_BYTES // 2 + 1))


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("a\x00b", "NUL"),
        ("a\ud800b", "surrogate"),
        (">" * (MAX_DEPTH + 2) + " deep", "nested"),
        ("".join("  " * depth + "- a\n" for depth in range(MAX_DEPTH)), "nested"),
        ("x\n\n" * (MAX_BLOCKS + 1), "blocks"),
    ],
)
def test_text_the_parser_must_not_take_is_refused(text: str, message: str) -> None:
    with pytest.raises(DocumentError, match=message) as raised:
        parse(text)
    assert raised.value.kind == "invalid_request"


def test_nesting_within_the_limit_parses() -> None:
    parsed = parse(">" * (MAX_DEPTH - 2) + " deep $x$")
    assert max(block.depth for block in parsed.blocks) == MAX_DEPTH - 2
    assert [span.tex for span in parsed.spans] == ["x"]


def test_texts_that_took_long_before_parse_quickly() -> None:
    # Every unclosed $$ used to look to the end of the text; a long list nests deeply.
    import time

    started = time.monotonic()
    parse("$$\n" + "a\n" * 20000)
    parse("$$ x\n" * 5000)
    parse("\\[ x\n" * 5000)
    assert time.monotonic() - started < 10
