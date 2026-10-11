# SPDX-License-Identifier: MIT
"""Single byte ranges of stored bodies (RFC 9110, section 14).

cabal (hackage-security) updates its copy of Hackage's index with one range
request for the part of ``01-index.tar.gz`` it lacks, so the cache answers a
GET that asks for one byte range of a stored body with 206 Partial Content.
Everything else is answered in full, as RFC 9110 allows: several ranges, a
malformed header, a unit other than bytes, and any request with ``If-Range``
(the cache gives clients no validators, so one cannot name what it stores).
A range that starts at or beyond the end of the body is answered 416.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Final, Literal

_OWS: Final = " \t"
_MAX_DIGITS: Final = 19
_RANGE_RE: Final = re.compile(
    rf"bytes=(?:(?P<first>[0-9]{{1,{_MAX_DIGITS}}})-(?P<last>[0-9]{{0,{_MAX_DIGITS}}})"
    rf"|-(?P<suffix>[0-9]{{1,{_MAX_DIGITS}}}))",
    re.ASCII | re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class ByteRange:
    """Bytes ``first`` to ``last`` of a body, both included."""

    first: int
    last: int

    def __post_init__(self) -> None:
        if not 0 <= self.first <= self.last:
            raise ValueError(f"invalid byte range {self.first}-{self.last}")

    @property
    def length(self) -> int:
        """How many bytes the range holds."""
        return self.last - self.first + 1

    def content_range(self, size: int) -> str:
        """The ``Content-Range`` value for this range of a ``size``-byte body."""
        return f"bytes {self.first}-{self.last}/{size}"


class Unsatisfiable(Enum):
    """The range starts at or beyond the end of the body (416)."""

    UNSATISFIABLE = "unsatisfiable"


UNSATISFIABLE: Final = Unsatisfiable.UNSATISFIABLE


def unsatisfied_range(size: int) -> str:
    """The ``Content-Range`` value of a 416 answer for a ``size``-byte body."""
    return f"bytes */{size}"


def select_range(
    ranges: list[str], *, if_range: bool, size: int
) -> ByteRange | Literal[Unsatisfiable.UNSATISFIABLE] | None:
    """The part of a ``size``-byte body a GET asks for, or None for all of it.

    ``ranges`` are the request's Range header values (normally none or one);
    ``if_range`` says whether it carried If-Range. Returns UNSATISFIABLE when the
    single range asked for lies entirely beyond the body.
    """
    if if_range or len(ranges) != 1:
        return None
    match = _RANGE_RE.fullmatch(ranges[0].strip(_OWS))
    if match is None:
        return None  # several ranges, other units or malformed: answer in full
    suffix = match.group("suffix")
    if suffix is not None:
        length = int(suffix)
        if length == 0 or size == 0:
            return UNSATISFIABLE
        return ByteRange(max(size - length, 0), size - 1)
    first = int(match.group("first"))
    last_text = match.group("last")
    last = int(last_text) if last_text else None
    if last is not None and last < first:
        return None  # invalid, so ignored (RFC 9110, section 14.1.1)
    if first >= size:
        return UNSATISFIABLE
    return ByteRange(first, size - 1 if last is None else min(last, size - 1))
