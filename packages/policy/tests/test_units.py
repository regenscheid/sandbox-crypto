# SPDX-License-Identifier: MIT
from __future__ import annotations

from datetime import timedelta

import pytest

from sandbox_science.policy.units import format_duration, parse_duration, parse_size


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("15s", timedelta(seconds=15)),
        ("30m", timedelta(minutes=30)),
        ("72h", timedelta(hours=72)),
        ("2d", timedelta(days=2)),
        (" 5m ", timedelta(minutes=5)),
    ],
)
def test_parse_duration(text: str, expected: timedelta) -> None:
    assert parse_duration(text) == expected


@pytest.mark.parametrize(
    "value",
    [
        "0s",
        "-5m",
        "5",
        "5 m",
        "5M",
        "1.5h",
        "\u0663\u0660m",  # Arabic-Indic digits: int() accepts them, the format does not
        "\uff15m",  # fullwidth digit
        "",
        30,
        None,
        timedelta(0),
        timedelta(seconds=-1),
        timedelta(milliseconds=500),
        "9" * 30 + "d",  # overflows timedelta; must be a ValueError, not OverflowError
    ],
)
def test_parse_duration_rejects(value: object) -> None:
    with pytest.raises(ValueError, match="duration"):
        parse_duration(value)


@pytest.mark.parametrize(
    ("value", "text"),
    [
        (timedelta(days=2), "2d"),
        (timedelta(hours=36), "36h"),
        (timedelta(minutes=90), "90m"),
        (timedelta(seconds=61), "61s"),
    ],
)
def test_format_duration_round_trips(value: timedelta, text: str) -> None:
    assert format_duration(value) == text
    assert parse_duration(text) == value


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("500Mi", 500 * 1024**2),
        ("64Gi", 64 * 1024**3),
        ("2M", 2_000_000),
        ("1T", 10**12),
        ("100B", 100),
        ("4096", 4096),
        (0, 0),
        (4096, 4096),
    ],
)
def test_parse_size(value: object, expected: int) -> None:
    assert parse_size(value) == expected


@pytest.mark.parametrize(
    "value", [True, False, -1, "-1", "1.5Gi", "64GB", "64gi", "\u0666\u0664Gi", "", None, 1.0]
)
def test_parse_size_rejects(value: object) -> None:
    with pytest.raises(ValueError, match="size"):
        parse_size(value)
