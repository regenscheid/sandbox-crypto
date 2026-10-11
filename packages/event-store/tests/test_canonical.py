# SPDX-License-Identifier: MIT
"""Canonical JSON: the RFC 8785 subset that record payloads use."""

import enum
import json

import pytest

from sandbox_science.event_store import (
    MAX_PAYLOAD_BYTES,
    RecordError,
    canonical_json,
    canonical_payload,
    parse_payload,
)


def test_objects_are_compact_with_keys_in_utf16_order() -> None:
    # RFC 8785, section 3.2.3: U+1F600 is the surrogate pair D83D DE00 in
    # UTF-16, so it sorts before U+FB33, unlike in code point order.
    keys = ["\u20ac", "\r", "\ufb33", "1", "\U0001f600", "\u0080", "\u00f6"]
    text = canonical_json({key: index for index, key in enumerate(keys)})
    ordered = ["\r", "1", "\u0080", "\u00f6", "\u20ac", "\U0001f600", "\ufb33"]
    assert list(json.loads(text)) == ordered
    assert text.startswith('{"\\r":1,"1":3,"\u0080":5,')


def test_strings_are_escaped_only_where_json_requires() -> None:
    value = "\u20ac$\x0f\nA'B\"\\/\x7f\u2028\U0001f600"
    assert canonical_json(value) == '"\u20ac$\\u000f\\nA\'B\\"\\\\/\x7f\u2028\U0001f600"'
    controls = "".join(chr(code) for code in (0x00, 0x08, 0x09, 0x0A, 0x0C, 0x0D, 0x1F))
    assert canonical_json(controls) == '"\\u0000\\b\\t\\n\\f\\r\\u001f"'


def test_literals_integers_and_arrays() -> None:
    value = {"a": [True, False, None, 0, -1, 2**53 - 1, -(2**53 - 1)], "b": ()}
    assert canonical_json(value) == (
        '{"a":[true,false,null,0,-1,9007199254740991,-9007199254740991],"b":[]}'
    )


def test_enums_are_written_as_their_values() -> None:
    class Color(enum.StrEnum):
        RED = "red"

    class Level(enum.IntEnum):
        HIGH = 3

    assert canonical_json({Color.RED: [Color.RED, Level.HIGH]}) == '{"red":["red",3]}'


@pytest.mark.parametrize(
    "value",
    [
        pytest.param(1.0, id="float"),
        pytest.param(float("nan"), id="nan"),
        pytest.param(2**53, id="integer-too-large"),
        pytest.param(-(2**53), id="integer-too-small"),
        pytest.param({1: "x"}, id="non-string-key"),
        pytest.param("\ud800", id="lone-surrogate"),
        pytest.param({"\udc00": 1}, id="lone-surrogate-key"),
        pytest.param(b"bytes", id="bytes"),
        pytest.param({"a": {1, 2}}, id="set"),
    ],
)
def test_values_without_a_canonical_form_are_refused(value: object) -> None:
    with pytest.raises(RecordError):
        canonical_json(value)


def test_deep_nesting_is_refused() -> None:
    value: object = 1
    for _ in range(100):
        value = [value]
    with pytest.raises(RecordError, match="nested"):
        canonical_json(value)


def test_payloads_are_objects_within_the_size_limit() -> None:
    assert canonical_payload({"b": 1, "a": "x"}) == '{"a":"x","b":1}'
    with pytest.raises(RecordError, match="JSON object"):
        canonical_payload(["not", "an", "object"])  # pyright: ignore[reportArgumentType]
    with pytest.raises(RecordError, match="larger"):
        canonical_payload({"a": "x" * MAX_PAYLOAD_BYTES})


def test_parsing_round_trips_canonical_text() -> None:
    text = canonical_payload({"z": [1, {"y": None}], "a": "\u00e9\U0001f600"})
    assert canonical_json(parse_payload(text)) == text


@pytest.mark.parametrize(
    "text",
    [
        pytest.param('{"b":1,"a":2}', id="key-order"),
        pytest.param('{"a": 1}', id="whitespace"),
        pytest.param('{"a":1.5}', id="float"),
        pytest.param('{"a":NaN}', id="nan"),
        pytest.param('{"a":1,"a":2}', id="repeated-key"),
        pytest.param('{"a":"\\u00e9"}', id="needless-escape"),
        pytest.param('{"a":"\\ud800"}', id="lone-surrogate"),
        pytest.param('{"a":-0}', id="negative-zero"),
        pytest.param("[1]", id="array"),
        pytest.param("{", id="not-json"),
        pytest.param("[" * 100_000, id="deeply-nested"),
    ],
)
def test_parsing_refuses_anything_but_canonical_objects(text: str) -> None:
    with pytest.raises(RecordError):
        parse_payload(text)
