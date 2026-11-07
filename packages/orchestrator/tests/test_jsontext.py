# SPDX-License-Identifier: MIT
"""JSON text the orchestrator did not write (``jsontext``): text nested past CPython's
parser is not JSON to any reader, and the bound connectors keep to is cheap and reads
strings as a parser does. The fold over such a result is in ``test_history_folds``, the
connector's bound in ``test_connectors``."""

from __future__ import annotations

import json
import time

import pytest

from sandbox_science.orchestrator import jsontext
from sandbox_science.orchestrator.context.history.markers import _same
from sandbox_science.orchestrator.notices import requests

DEEP = "[" * 9999
"""Past CPython 3.13's parser, which raises ``RecursionError`` for it, not ``ValueError``;
3.14's reads it, closed, as a list."""


def test_text_nested_past_the_parser_is_not_json() -> None:
    for text in (DEEP, DEEP.encode(), DEEP + "]" * 9999, (DEEP + "]" * 9999).encode()):
        with pytest.raises(json.JSONDecodeError):
            jsontext.loads(text)
    with pytest.raises(json.JSONDecodeError):
        jsontext.raw_decode("note " + DEEP, 5)
    assert jsontext.loads('{"a": [1]}') == {"a": [1]}
    assert jsontext.raw_decode("x [1] y", 2) == ([1], 5)


def test_text_nested_to_the_limit_is_read_and_past_it_is_not() -> None:
    limit = jsontext.MAX_DEPTH
    at = "[" * limit + "]" * limit
    past = "[" * (limit + 1) + "]" * (limit + 1)
    assert jsontext.loads(at) == json.loads(at)
    assert jsontext.raw_decode("x " + at, 2)[1] == 2 + 2 * limit
    deepest = '{"a":' * limit + "1" + "}" * limit
    assert jsontext.loads(deepest.encode())["a"]["a"]
    with pytest.raises(json.JSONDecodeError, match="nested too deeply"):
        jsontext.loads(past)
    with pytest.raises(json.JSONDecodeError, match="nested too deeply"):
        jsontext.loads('{"a":' * (limit + 1) + "1" + "}" * (limit + 1))
    with pytest.raises(json.JSONDecodeError, match="nested too deeply"):
        jsontext.raw_decode("x " + past + " y", 2)
    # Many brackets, in strings or side by side, are no depth.
    assert jsontext.loads(json.dumps({"text": "[" * 5000})) == {"text": "[" * 5000}
    assert len(jsontext.loads("[" + "[]," * 5000 + "[]]")) == 5001


def test_readers_of_archived_text_take_it_as_text() -> None:
    assert _same(DEEP) == DEEP  # a call's arguments, compared as written
    assert requests("Document work requests: " + DEEP) == []


@pytest.mark.parametrize(
    ("text", "deeper"),
    [
        (DEEP, True),
        ("[" * 201, True),
        ("[" * 200 + "]" * 200, False),
        ('{"a":' * 201 + "1" + "}" * 201, True),
        ("[1]" * 9999, False),  # many brackets, none nested
        (json.dumps({"text": DEEP}), False),  # inside a string
        ('"' + "\\" + '"' + DEEP, False),  # an escaped quote: still inside the string
        ('"a" ' + DEEP, True),
        ("]" * 300 + "[" * 201, True),  # a stray closer hides no opener
    ],
)
def test_the_bound_counts_brackets_outside_strings(text: str, deeper: bool) -> None:
    assert jsontext.deeper_than(text, 200) is deeper


@pytest.mark.parametrize(
    "text",
    [
        "[]" * 500_000,
        '"' + 'a\\"' * 300_000,  # one string left open, its quotes escaped
        '"' + 'a\\"' * 300_000 + "\\",  # and a lone backslash at its end
        '"x"' * 300_000,
    ],
)
def test_the_bound_is_linear(text: str) -> None:
    started = time.perf_counter()
    jsontext.deeper_than(text + "[" * 300, 200)
    assert time.perf_counter() - started < 1.0
