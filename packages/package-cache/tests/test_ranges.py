# SPDX-License-Identifier: MIT
"""Selecting one byte range of a stored body (RFC 9110, section 14)."""

import pytest

from sandbox_science.package_cache.ranges import (
    UNSATISFIABLE,
    ByteRange,
    select_range,
    unsatisfied_range,
)

SIZE = 10


@pytest.mark.parametrize(
    ("header", "first", "last"),
    [
        ("bytes=0-4", 0, 4),
        ("bytes=5-", 5, 9),
        ("bytes=-3", 7, 9),
        ("bytes=-20", 0, 9),  # longer than the body: all of it
        ("bytes=8-100", 8, 9),  # past the end: up to the end
        ("bytes=0-0", 0, 0),
        ("bytes=9-9", 9, 9),
        ("bytes=0-", 0, 9),
        ("BYTES=1-2", 1, 2),  # the unit is case-insensitive
        (" bytes=1-2\t", 1, 2),  # surrounding whitespace is not part of the value
        ("bytes=0000-0003", 0, 3),
    ],
)
def test_one_byte_range_is_selected(header: str, first: int, last: int) -> None:
    selected = select_range([header], if_range=False, size=SIZE)
    assert selected == ByteRange(first, last)
    assert isinstance(selected, ByteRange)
    assert selected.length == last - first + 1
    assert selected.content_range(SIZE) == f"bytes {first}-{last}/{SIZE}"


def test_hackage_securitys_update_request() -> None:
    # cabal asks for the last 64 KiB of its copy up to the end of the new index.
    size = 139_277_470
    selected = select_range(["bytes=139211934-139277469"], if_range=False, size=size)
    assert selected == ByteRange(139_211_934, 139_277_469)
    assert isinstance(selected, ByteRange)
    assert selected.length == 65_536


@pytest.mark.parametrize(
    ("header", "size"),
    [
        ("bytes=10-", SIZE),
        ("bytes=10-20", SIZE),
        ("bytes=-0", SIZE),
        ("bytes=0-", 0),
        ("bytes=-5", 0),
        ("bytes=0-0", 0),
    ],
)
def test_a_range_beyond_the_body_is_unsatisfiable(header: str, size: int) -> None:
    assert select_range([header], if_range=False, size=size) is UNSATISFIABLE


@pytest.mark.parametrize(
    "header",
    [
        "bytes=5-4",  # invalid: last before first
        "bytes=0-1,3-4",  # several ranges are answered in full
        "bytes=0-1, 3-4",
        "bytes=0-1,",
        "items=0-1",
        "bytes 0-1",
        "bytes=",
        "bytes=-",
        "bytes=a-b",
        "bytes=1-2-3",
        "bytes= 0-1",
        "bytes=0x1-2",
        "bytes=+1-2",
        "bytes=1.0-2",
        "bytes=\u0661-2",  # Arabic-Indic digit one
        "bytes=" + "1" * 20 + "-",
        "",
    ],
)
def test_other_range_headers_are_ignored(header: str) -> None:
    assert select_range([header], if_range=False, size=SIZE) is None


def test_no_range_one_range_or_several_headers() -> None:
    assert select_range([], if_range=False, size=SIZE) is None
    assert select_range(["bytes=0-1", "bytes=2-3"], if_range=False, size=SIZE) is None


def test_if_range_makes_the_answer_complete() -> None:
    # The cache gives clients no validators, so If-Range cannot name what it stores.
    assert select_range(["bytes=0-4"], if_range=True, size=SIZE) is None
    assert select_range(["bytes=10-"], if_range=True, size=SIZE) is None


def test_unsatisfied_range() -> None:
    assert unsatisfied_range(SIZE) == "bytes */10"


@pytest.mark.parametrize(("first", "last"), [(-1, 0), (2, 1)])
def test_byte_ranges_are_well_formed(first: int, last: int) -> None:
    with pytest.raises(ValueError, match="invalid byte range"):
        ByteRange(first, last)
