# SPDX-License-Identifier: MIT
"""Candidate-sized checks against scoped original text, never native replay payloads.

Literals and keys span byte-page boundaries. Number normalization uses the ledger's
normalizer on complete lexical fragments; an exceptionally long numeric fragment is
reported as unproved rather than retained in memory or treated as evidence.
"""

from __future__ import annotations

import codecs
import json
import re
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from typing import Any, Final, Literal, Protocol, cast

from sandbox_science.contracts import ExecutionContext
from sandbox_science.orchestrator.ledger.provenance import numbers

from .archive import Archive, Archived, Entry
from .limits import METADATA_BYTES
from .operations import CHUNK_BYTES

FRAGMENT_CHARS: Final = 65536
# Commas need lookahead before they can split numbers: the ledger normalizer
# removes one before exactly three digits. Other separators and an ordinary word
# between whitespace cannot participate in a supported numeric expression.
_SEPARATOR: Final = re.compile(r"""[\x00,;|:"'=/!?\[\]]|\s+[A-Za-z]{2,}\s+""")
_NUMERIC: Final = re.compile(r"[\d⁰¹²³⁴⁵⁶⁷⁸⁹⁻⁺₀₁₂₃₄₅₆₇₈₉]")


@dataclass(frozen=True)
class EvidenceRequest:
    """Only the facts the bounded candidate actually asks the archive to establish."""

    numbers: frozenset[str] = frozenset()
    literals: frozenset[str] = frozenset()
    patterns: frozenset[str] = frozenset()
    keys: tuple[str, ...] = ()


@dataclass
class EvidenceMatches:
    """Supported candidate facts and first original message holders for search keys."""

    numbers: set[str] = field(default_factory=set[str])
    literals: set[str] = field(default_factory=set[str])
    patterns: set[str] = field(default_factory=set[str])
    keys: dict[str, str] = field(default_factory=dict[str, str])
    unproved_numeric_fragments: int = 0
    unproved_tool_calls: int = 0

    def diagnostic(self) -> dict[str, int]:
        """Only incomplete evidence checks, with no source or candidate text."""
        return {
            key: value
            for key, value in {
                "unproved_numeric_fragments": self.unproved_numeric_fragments,
                "unproved_tool_calls": self.unproved_tool_calls,
            }.items()
            if value
        }


class EvidenceSource(Protocol):
    """A repeatable, already scoped source for one enrichment candidate's checks."""

    def verify(self, request: EvidenceRequest) -> EvidenceMatches: ...


@dataclass(frozen=True)
class RowEvidence:
    """First-level folds already hold their bounded original rows for proposals."""

    rows: Sequence[Archived]

    def verify(self, request: EvidenceRequest) -> EvidenceMatches:
        return _verify(((row.message_id, _pieces(row.entry.text)) for row in self.rows), request)


@dataclass(frozen=True)
class ArchiveEvidence:
    """An exact original span without retaining its historical bodies or native items.

    MAIN reads main-run messages. A delegated folder supplies its admitted attempt
    name; the archive's existing continuation-chain setting retains its semantics.
    """

    archive: Archive
    context: ExecutionContext
    first: int
    last: int
    run: str | None = None

    def verify(self, request: EvidenceRequest) -> EvidenceMatches:
        result = EvidenceMatches()
        if not any((request.numbers, request.literals, request.patterns, request.keys)):
            return result
        runs = None
        if self.run is not None:
            runs = (self.run,)
            if self.archive.chain:
                runs, _ = self.archive.view_scope(
                    self.context.project_id, self.context.session_id, run=self.run, end=self.last
                )

        def messages() -> Iterator[tuple[str, Iterator[str]]]:
            start = self.first
            while start <= self.last:
                page = self.archive.message_infos(
                    self.context.project_id,
                    self.context.session_id,
                    start=start,
                    end=self.last,
                    runs=runs,
                )
                if not page:
                    return
                for info in page:
                    start = cast(int, info["position"]) + 1
                    if info["kind"] in ("fold", "state_block"):
                        continue
                    if self.run is None and info["parent"] is not None:
                        continue
                    identifier = str(info["message_id"])
                    yield identifier, self._text(identifier, result)

        return _verify(messages(), request, result)

    def _field(self, message: str, field: Literal["content", "tool_calls"]) -> Iterator[bytes]:
        offset = 0
        while True:
            chunk = self.archive.message_field_chunk(
                self.context.project_id, message, field, offset=offset
            )
            yield chunk.data
            if chunk.next_offset is None:
                return
            offset = chunk.next_offset

    def _text(self, message: str, result: EvidenceMatches) -> Iterator[str]:
        decoder = codecs.getincrementaldecoder("utf-8")()
        for raw in self._field(message, "content"):
            yield decoder.decode(raw)
        yield decoder.decode(b"", final=True)
        # Entry.text projects tool calls to id/name/arguments; raw storage may hold
        # extra fields, which must never establish a candidate's alleged fact.
        raw = bytearray()
        for chunk in self._field(message, "tool_calls"):
            if len(raw) + len(chunk) > METADATA_BYTES:
                result.unproved_tool_calls += 1
                return
            raw.extend(chunk)
        calls: object = json.loads(raw or b"[]")
        if not isinstance(calls, list):
            result.unproved_tool_calls += 1
            return
        for call in cast(list[object], calls):
            if isinstance(call, dict):
                entry = Entry(
                    "assistant", "assistant", None, tool_calls=(cast(dict[str, Any], call),)
                )
                yield "\n"
                yield from _pieces(entry.text)


def _pieces(text: str) -> Iterator[str]:
    for offset in range(0, len(text), CHUNK_BYTES):
        yield text[offset : offset + CHUNK_BYTES]


def _comma_separator(text: str, after: int, *, final: bool) -> bool:
    """Split only after ruling out a normalized three-digit thousands group.

    Retain an incomplete group at a page edge. Four digits already rule out the
    grouping; one to three digits need the next character (or the original EOF).
    Checking only the following group is conservative when a power or superscript
    before the comma normalizes to a decimal digit.
    """
    count = 0
    while after + count < len(text):
        char = text[after + count]
        if not (char.isdecimal() or char in "₀₁₂₃₄₅₆₇₈₉"):
            return count != 3
        count += 1
        if count == 4:
            return True
    return final and count != 3


class _Numbers:
    def __init__(self, wanted: frozenset[str], found: EvidenceMatches) -> None:
        self.wanted, self.found = wanted, found
        self.tail: str = ""
        self.skipping = False

    def _complete(self, text: str) -> None:
        if not self.skipping and _NUMERIC.search(text):
            try:
                self.found.numbers.update(numbers(text) & self.wanted)
            except (ValueError, OverflowError):
                self.found.unproved_numeric_fragments += 1
        self.skipping = False

    def feed(self, text: str, *, final: bool = False) -> None:
        if not self.wanted:
            return
        text = self.tail + text
        start = 0
        for match in _SEPARATOR.finditer(text):
            if match[0] == "," and not _comma_separator(text, match.end(), final=final):
                continue
            self._complete(text[start : match.start()])
            start = match.end()
        self.tail = text[start:]
        if final:
            self._complete(self.tail)
            self.tail = ""
        elif len(self.tail) > FRAGMENT_CHARS:
            if not self.skipping and _NUMERIC.search(self.tail):
                self.found.unproved_numeric_fragments += 1
                self.skipping = True
            # Retain enough context to recognize a separator split at a byte page.
            # A numeric-free prefix cannot contribute numbers; retaining its word
            # suffix also preserves platform-ID masking if digits follow it later.
            self.tail = self.tail[-128:]


class _Text:
    def __init__(self, request: EvidenceRequest, found: EvidenceMatches, message: str) -> None:
        self.request, self.found, self.message = request, found, message
        self.patterns = {pattern: re.compile(pattern) for pattern in request.patterns}
        self.keep = (
            max((len(t) for t in (*request.literals, *request.keys, *request.patterns)), default=0)
            + 4
        )
        self.tail = ""
        self.started = False

    def feed(self, text: str, *, final: bool = False, keys: bool = True) -> None:
        text = self.tail + text
        for literal in self.request.literals - self.found.literals:
            if literal in text:
                self.found.literals.add(literal)
        if keys:
            for key in self.request.keys:
                if key not in self.found.keys and key in text:
                    self.found.keys[key] = self.message
        for pattern, compiled in self.patterns.items():
            if pattern in self.found.patterns:
                continue
            # Signed decimals reject a following period-plus-digit; wait for
            # both real characters across page and incremental decoder boundaries.
            if any(
                (match.start() > 0 or not self.started) and (len(text) - match.end() >= 2 or final)
                for match in compiled.finditer(text)
            ):
                self.found.patterns.add(pattern)
        self.started = self.started or len(text) > self.keep
        self.tail = text[-self.keep :]


def _proved(request: EvidenceRequest, matches: EvidenceMatches) -> bool:
    return (
        request.numbers <= matches.numbers
        and request.literals <= matches.literals
        and request.patterns <= matches.patterns
        and all(key in matches.keys for key in request.keys)
    )


def _verify(
    messages: Iterable[tuple[str, Iterator[str]]],
    request: EvidenceRequest,
    found: EvidenceMatches | None = None,
) -> EvidenceMatches:
    result = found if found is not None else EvidenceMatches()
    if _proved(request, result):
        return result
    for identifier, pieces in messages:
        literal = _Text(request, result, identifier)
        # IDs are part of the old validation span, but never source search keys.
        literal.feed(identifier + "\n", keys=False)
        if _proved(request, result):
            return result
        literal.tail = ""
        numeric = _Numbers(request.numbers, result)
        for text in pieces:
            literal.feed(text)
            numeric.feed(text)
            if _proved(request, result):
                return result
        literal.feed("", final=True)
        numeric.feed("", final=True)
        if _proved(request, result):
            return result
    return result
