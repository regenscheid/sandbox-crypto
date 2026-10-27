# SPDX-License-Identifier: MIT
"""Regular expressions that match in time linear in their input (RE2), for patterns whose
text comes from a policy and whose input comes from an MCP server.

Python's ``re`` backtracks: ``\\w+\\w+!`` takes seconds on a few thousand characters.
RE2 (``google-re2``, BSD-3-Clause) never does, and refuses what it cannot match in linear
time: backreferences and lookaround. Positions are in characters of the ``str`` searched.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Final, Protocol, cast

import re2  # pyright: ignore[reportMissingTypeStubs] -- google-re2 ships no stubs


class LinearMatch(Protocol):
    """A match: its span in characters."""

    def start(self) -> int: ...
    def end(self) -> int: ...


class LinearPattern(Protocol):
    """A compiled pattern."""

    def finditer(self, text: str) -> Iterator[LinearMatch]: ...
    def search(self, text: str) -> LinearMatch | None: ...


class PatternError(ValueError):
    """A pattern RE2 cannot compile."""


_OPTIONS: Final = re2.Options()
_OPTIONS.log_errors = False  # refusals are reported here, not on standard error
_ERROR: Final = cast("type[Exception]", re2.error)  # pyright: ignore[reportUnknownMemberType] -- no stubs


def compile_linear(pattern: str) -> LinearPattern:
    """Compile ``pattern`` with RE2; PatternError when RE2 refuses it, as it refuses
    backreferences and lookaround, which cannot match in linear time."""
    try:
        compiled: object = re2.compile(pattern, _OPTIONS)  # pyright: ignore[reportUnknownMemberType] -- no stubs
    except _ERROR as exc:
        first: object = exc.args[0] if exc.args else exc
        said = first.decode("utf-8", "replace") if isinstance(first, bytes) else str(first)
        raise PatternError(
            f"not an RE2 pattern ({said}): inspection patterns match in linear time, so "
            "backreferences and lookaround are not available"
        ) from None
    return cast("LinearPattern", compiled)
