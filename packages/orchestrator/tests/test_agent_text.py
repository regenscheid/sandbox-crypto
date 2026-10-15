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
    check_agent_text,
    hidden_characters,
)
from sandbox_science.orchestrator.documents.errors import DocumentError

FIXTURES = Path(__file__).resolve().parents[3] / "web" / "workspace" / "src" / "lib"
FIXTURE: dict[str, Any] = json.loads((FIXTURES / "hidden.fixtures.json").read_text())


def test_the_set_is_the_browsers() -> None:
    assert FIXTURE["allowed"] == sorted(ord(char) for char in ALLOWED)
    assert FIXTURE["categories"] == sorted(CATEGORIES)
    assert FIXTURE["default_ignorable"] == [list(pair) for pair in DEFAULT_IGNORABLE]
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
