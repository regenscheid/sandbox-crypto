# SPDX-License-Identifier: MIT
"""Canonical JSON and request digests: stable when a defaulted field is added."""

from __future__ import annotations

import pytest

from sandbox_science.contracts.canonical import canonical_json, request_digest
from sandbox_science.contracts.model import Contract


class Before(Contract):
    name: str
    count: int = 1


class After(Contract):
    """``Before`` once a release has added a field with a default."""

    name: str
    count: int = 1
    note: str | None = None


def test_canonical_json_is_sorted_compact_and_ascii() -> None:
    assert canonical_json({"b": [1, 2.5], "a": "é"}) == b'{"a":"\\u00e9","b":[1,2.5]}'
    with pytest.raises(ValueError, match="JSON compliant"):
        canonical_json({"x": float("nan")})


def test_a_new_defaulted_field_keeps_every_digest() -> None:
    for fields in ({"name": "x"}, {"name": "x", "count": 3}):
        assert request_digest(Before.model_validate(fields), "d") == request_digest(
            After.model_validate(fields), "d"
        )
    # A value given for the new field is part of the request.
    assert request_digest(After(name="x", note="n"), "d") != request_digest(After(name="x"), "d")


def test_a_default_given_explicitly_is_the_same_request() -> None:
    assert request_digest(Before(name="x", count=1), "d") == request_digest(Before(name="x"), "d")


def test_domains_separate_equal_fields() -> None:
    assert request_digest(Before(name="x"), "one") != request_digest(Before(name="x"), "two")
    for domain in ("", "a\x00b"):
        with pytest.raises(ValueError, match="digest domain"):
            request_digest(Before(name="x"), domain)
