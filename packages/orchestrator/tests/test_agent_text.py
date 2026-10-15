# SPDX-License-Identifier: MIT
"""Text the user approves and agents then read holds no character the user may not see
(threat model, A3): the server's set and cases agree with the browser's
(``hidden.fixtures.json``)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from sandbox_science.orchestrator.documents.agent_text import (
    ALLOWED,
    CATEGORIES,
    DEFAULT_IGNORABLE,
    EMOJI_BASES,
    KEYCAPS,
    agent_text,
    agent_values,
    blank_run,
    check_agent_text,
    hidden_characters,
    hidden_references,
    visible,
)
from sandbox_science.orchestrator.documents.errors import DocumentError

FIXTURES = Path(__file__).resolve().parents[3] / "web" / "workspace" / "src" / "lib"
FIXTURE: dict[str, Any] = json.loads((FIXTURES / "hidden.fixtures.json").read_text())


def test_the_set_is_the_browsers() -> None:
    assert FIXTURE["allowed"] == sorted(ord(char) for char in ALLOWED)
    assert FIXTURE["categories"] == sorted(CATEGORIES)
    assert FIXTURE["default_ignorable"] == [list(pair) for pair in DEFAULT_IGNORABLE]
    assert FIXTURE["emoji_bases"] == [list(pair) for pair in EMOJI_BASES]
    assert FIXTURE["keycaps"] == {"bases": sorted(map(ord, KEYCAPS)), "mark": 0x20E3}
    # The file is ASCII, so the repository holds none of these characters as written.
    (FIXTURES / "hidden.fixtures.json").read_bytes().decode("ascii")


@pytest.mark.parametrize("case", FIXTURE["cases"], ids=lambda case: case["name"])
def test_the_shared_cases(case: dict[str, Any]) -> None:
    assert [list(found) for found in hidden_characters(case["text"])] == case["hidden"]


def test_a_refusal_names_the_character_and_where_it_is() -> None:
    check_agent_text("Write $\\mathrm{GF}(p)$, & keep \U0001f600 and שלום.\n")
    with pytest.raises(DocumentError) as refused:
        check_agent_text("first line\nrun it\u202e now", "an entry's text")
    assert refused.value.kind == "invalid_request"
    assert refused.value.message.startswith(
        "an entry's text holds U+202E RIGHT-TO-LEFT OVERRIDE at character 18 (line 2, column 7)"
    )
    with pytest.raises(DocumentError) as tag:
        check_agent_text("ok\U000e0041")
    assert "U+E0041 TAG LATIN CAPITAL LETTER A at character 3" in tag.value.message


@pytest.mark.parametrize(
    ("text", "code"),
    [
        ("evil &#x202E;gnp.exe", "U+202E"),
        ("a&#8203;b", "U+200B"),
        ("a&rlm;b", "U+200F"),
        ("a&ZeroWidthSpace;b", "U+200B"),
        ("a&shy;b", "U+00AD"),
    ],
)
def test_a_reference_to_a_hidden_character_is_refused(text: str, code: str) -> None:
    with pytest.raises(DocumentError) as refused:
        check_agent_text(text)
    assert f"holds {code} written as &" in refused.value.message


@pytest.mark.parametrize("text", ["a &amp; b", "&#x41;", "&lt;tag&gt;", "AT&T;", "&#x85;"])
def test_other_references_are_text(text: str) -> None:
    check_agent_text(text)  # &#x85; is an ellipsis to HTML, as to CommonMark


@pytest.mark.parametrize("case", FIXTURE["references"], ids=lambda case: case["name"])
def test_the_shared_reference_cases(case: dict[str, Any]) -> None:
    assert [list(found) for found in hidden_references(case["text"])] == case["hidden"]


@pytest.mark.parametrize("case", FIXTURE["blank_lines"], ids=lambda case: case["name"])
def test_the_shared_blank_line_cases(case: dict[str, Any]) -> None:
    assert blank_run(case["text"]) == case["line"]
    if case["line"] is None:
        check_agent_text(case["text"])
    else:
        with pytest.raises(DocumentError) as refused:
            check_agent_text(case["text"])
        assert f"blank lines in a row from line {case['line']}" in refused.value.message


def test_a_reference_with_leading_zeros_or_two_characters_is_refused() -> None:
    for text, code in (("&#x0000202E;", "U+202E"), ("&#0008238;", "U+202E"), ("&caps;", "U+FE00")):
        with pytest.raises(DocumentError) as refused:
            check_agent_text(text)
        assert f"holds {code} written as {text}" in refused.value.message


def test_an_emoji_may_ask_for_its_colour_form() -> None:
    """The coordinator, 2026-10-15: U+FE0F right after an emoji, or in a keycap, is text."""
    selector = chr(0xFE0F)
    check_agent_text(f"Careful {chr(0x26A0)}{selector}, done {chr(0x2714)}{selector}.")
    check_agent_text(f"Step 1{selector}{chr(0x20E3)}")
    for text, at in ((f"a{selector}", 2), (f"1{selector}", 2), (f"{selector}x", 1)):
        with pytest.raises(DocumentError) as refused:
            check_agent_text(text)
        assert f"U+FE0F VARIATION SELECTOR-16 at character {at}" in refused.value.message


@pytest.mark.parametrize("case", FIXTURE["line_ends"], ids=lambda case: case["name"])
def test_an_agents_line_ends_become_line_feeds(case: dict[str, Any]) -> None:
    assert agent_text(case["text"]) == case["kept"]
    with pytest.raises(DocumentError):
        check_agent_text(case["text"])  # what is kept holds none


def test_every_text_of_a_value_is_kept_and_checked_by_its_place() -> None:
    kept = agent_values({"goal\r\n": ("a\r\nb", 3, None), "steps": [{"title": "x\ry"}]}, "p")
    assert kept == {"goal\n": ("a\nb", 3, None), "steps": [{"title": "x\ny"}]}
    with pytest.raises(DocumentError) as refused:
        agent_values({"steps": [{"title": "ok"}, {"note": "a" + chr(0x202E)}]}, "the plan")
    assert refused.value.message.startswith("the plan.steps[1].note holds U+202E")
    with pytest.raises(DocumentError) as key:
        agent_values({"k" + chr(0x200B): 1}, "the plan")
    assert key.value.message.startswith("a key of the plan holds U+200B")
    assert chr(0x200B) not in key.value.message


@pytest.mark.parametrize("case", FIXTURE["cases"], ids=lambda case: case["name"])
def test_a_terminal_shows_each_hidden_character_as_its_code_point(case: dict[str, Any]) -> None:
    shown = visible(case["text"])
    assert hidden_characters(shown) == []
    for _, code in case["hidden"]:
        assert f"\u27e6{code}\u27e7" in shown


@pytest.mark.parametrize("case", FIXTURE["chips"]["cases"], ids=lambda case: case["name"])
def test_a_terminal_shows_look_alikes_as_the_browser_does(case: dict[str, Any]) -> None:
    shown = visible(case["text"])
    assert shown.count("\u27e6") == len(case["chipped"])
