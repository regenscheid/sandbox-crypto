# SPDX-License-Identifier: MIT
"""Parsing of human-written durations and byte sizes used in policy files."""

from __future__ import annotations

import re
from datetime import timedelta
from typing import Annotated, Final

from pydantic import BeforeValidator, PlainSerializer

# [0-9] rather than \d: \d also matches non-ASCII digits, which int() accepts.
_DURATION_RE: Final = re.compile(r"(?P<value>[0-9]+)(?P<unit>[smhd])")
_DURATION_UNITS: Final = {"s": 1, "m": 60, "h": 3600, "d": 86400}

_SIZE_RE: Final = re.compile(r"(?P<value>[0-9]+)(?P<unit>|B|Ki|Mi|Gi|Ti|K|M|G|T)")
_SIZE_UNITS: Final = {
    "": 1,
    "B": 1,
    "Ki": 1024,
    "Mi": 1024**2,
    "Gi": 1024**3,
    "Ti": 1024**4,
    "K": 1000,
    "M": 1000**2,
    "G": 1000**3,
    "T": 1000**4,
}


def parse_duration(value: object) -> timedelta:
    """Parse ``30m``, ``72h``, ``15s`` or ``2d`` into a positive whole-second ``timedelta``."""
    if isinstance(value, timedelta):
        if value <= timedelta(0):
            raise ValueError("duration must be positive")
        if value.microseconds:
            # Sub-second values could not be written back in the file's format.
            raise ValueError("duration must be a whole number of seconds")
        return value
    if not isinstance(value, str):
        raise ValueError("duration must be a string such as '30m' or '72h'")
    match = _DURATION_RE.fullmatch(value.strip())
    if match is None:
        raise ValueError(f"invalid duration {value!r}; use a number followed by s, m, h or d")
    seconds = int(match["value"]) * _DURATION_UNITS[match["unit"]]
    if seconds <= 0:
        raise ValueError("duration must be positive")
    try:
        return timedelta(seconds=seconds)
    except OverflowError as exc:
        raise ValueError(f"duration {value!r} is too large") from exc


def format_duration(value: timedelta) -> str:
    """Render a duration in the largest exact unit, for stable serialization."""
    seconds = int(value.total_seconds())
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if seconds % size == 0:
            return f"{seconds // size}{unit}"
    return f"{seconds}s"


def parse_size(value: object) -> int:
    """Parse ``500Mi``, ``64Gi``, ``2M`` or a plain integer into a byte count."""
    if isinstance(value, bool):
        raise ValueError("size must be a number or a string such as '64Gi'")
    if isinstance(value, int):
        if value < 0:
            raise ValueError("size must not be negative")
        return value
    if not isinstance(value, str):
        raise ValueError("size must be a number or a string such as '64Gi'")
    match = _SIZE_RE.fullmatch(value.strip())
    if match is None:
        raise ValueError(f"invalid size {value!r}; use a number with Ki, Mi, Gi, Ti, K, M, G or T")
    return int(match["value"]) * _SIZE_UNITS[match["unit"]]


Duration = Annotated[
    timedelta,
    BeforeValidator(parse_duration),
    PlainSerializer(format_duration, return_type=str),
]
"""A positive whole-second duration written as ``<n>s|m|h|d``."""

ByteSize = Annotated[int, BeforeValidator(parse_size)]
"""A non-negative byte count, optionally written with a binary or decimal suffix."""
