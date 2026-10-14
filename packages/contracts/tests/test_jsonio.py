# SPDX-License-Identifier: MIT
"""Strict JSON: repeated keys and non-finite constants are refused at any depth."""

from __future__ import annotations

import pytest

from sandbox_science.contracts.jsonio import strict_loads


def test_ordinary_json_parses() -> None:
    assert strict_loads(b'{"a": [1, 2.5, {"b": null}]}') == {"a": [1, 2.5, {"b": None}]}
    assert strict_loads('"text"') == "text"


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        ('{"a": 1, "a": 2}', "repeated JSON key"),
        ('{"outer": {"x": 1, "x": 1}}', "repeated JSON key"),
        ('[{"x": 1}, {"y": 2, "y": 3}]', "repeated JSON key"),
        ('{"a": NaN}', "no NaN"),
        ("[Infinity]", "no Infinity"),
        ("-Infinity", "no -Infinity"),
        ("1e999", "too large"),
        ("[-1e400]", "too large"),
    ],
)
def test_ambiguous_or_non_finite_json_is_refused(raw: str, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        strict_loads(raw)
