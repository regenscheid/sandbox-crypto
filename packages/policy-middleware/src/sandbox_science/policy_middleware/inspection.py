# SPDX-License-Identifier: MIT
"""MCP traffic inspection on the direct path (D98; docs/POLICY-MIDDLEWARE.md, "MCP traffic
inspection").

The middleware sits inline between the agent runtime's MCP client and the servers. It
records each call's arguments with its ``mcp-call`` record (``arguments``), and each
answer as an ``mcp-answer`` record linked to the call by OpenShell's request id: its
status, content type, size, an excerpt within ``answer_bytes``, its SHA-256 when it was
seen whole, and the findings of the policy's patterns. Everything recorded is redacted
first (``redaction``).

**What the client receives does not change.** A JSON answer that comes with its length is
read whole (``WHOLE_BODY_BYTES``) and passed on as it came; anything else is read piece by
piece as it streams (``STREAM_BYTES``), each piece passed on as it came the moment it is
read, never held back. Only a ``block`` pattern changes delivery: a whole answer is
refused with ``answer_blocked_<pattern>``; a stream is cut at the piece that matched,
which is replaced by an MCP error naming the finding (an event stream) and followed by
nothing else, or (another media) delivery is aborted.

**Matching across pieces.** Patterns run over the answer's text as it arrives: an event
stream's ``data:`` text, any other body's text, both decoded as UTF-8. Each piece is
searched together with the last ``CARRY`` characters before it, so a match split between
two pieces is found, once. Input per search is bounded by a piece (64 KiB) and the carry.

**JSON escapes.** A JSON answer or an event stream is searched twice: as it came, and with
its JSON string escapes decoded (``_Escapes``: ``\\uXXXX``, a surrogate pair, ``\\n``,
``\\"``, ``\\\\``, ``\\/``, ``\\b``, ``\\f``, ``\\r``, ``\\t``), so that ``\\u0069gnore`` is
found as ``ignore``. The decoder reads piece by piece and holds back an escape a piece
cuts. A malformed escape (a bad letter, short or non-hex digits, an unpaired surrogate) is
left as written, so decoding never fails and the raw search still sees it. Each view has its
own carry; a finding's offset is always in the raw text, and a match both views find counts
once.
"""

from __future__ import annotations

import bisect
import codecs
import hashlib
import json
import logging
import re
import threading
from collections import OrderedDict, deque
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Final, Literal

from sandbox_science.contracts import SYSTEM_CLOCK, Clock
from sandbox_science.policy import LoadedPolicy
from sandbox_science.policy.linear_regex import LinearMatch, LinearPattern, compile_linear
from sandbox_science.policy.mcp_inspection import effective_patterns
from sandbox_science.policy.model import InspectionPattern

from .events import EventSink
from .redaction import Redactor, Span, cut

_log = logging.getLogger(__name__)

ANSWER_RECORD: Final = "mcp-answer"
ALERT_RECORD: Final = "mcp-answer-alert"
INSPECTION_STATE: Final = "mcp-inspection-state"
"""Whether inspection is on, recorded as the middleware starts."""
FINDING_TYPE: Final = "mcp_answer_pattern"
"""The type of the finding OpenShell logs for a pattern found in an answer."""
BLOCKED_PREFIX: Final = "answer_blocked_"
CARRY: Final = 1024
"""Characters of earlier text each search includes, so that a match split between two
pieces is found: a match longer than this, split, can be missed."""
PIECE: Final = 64 * 1024
"""The most text one search reads beyond the carry: a whole body is searched in pieces."""
MAX_FINDINGS: Final = 32
FINDING_CONTEXT: Final = 200
"""Characters of text recorded around a finding."""
_SLACK: Final = 4096
"""Text kept beyond an excerpt's bound until it is redacted, so that a secret the bound
splits is still recognized as a whole before the excerpt is cut."""
MAX_TRACKED: Final = 1024
"""Calls of one sandbox whose answers the middleware waits for, oldest forgotten first:
a sandbox can only push out its own."""
MAX_SANDBOXES: Final = 4096
"""Sandboxes whose calls are tracked, least recently active forgotten first."""
MAX_PENDING: Final = 1024
"""Plain answers waiting to be recorded; past it, one is dropped and counted, never waited
for. Answers with a finding, a block or no inspection are never dropped."""
MAX_URGENT: Final = 4096
"""Answers with a finding, a block or no inspection waiting in full; past it, each is kept
compact (names and sizes, no excerpts) and counted, never dropped."""
DROPPED_RECORD: Final = "mcp-inspection-dropped"
DROP_REPORT_SECONDS: Final = 60.0
"""How often dropped answers are counted into an ``mcp-inspection-dropped`` record."""
_EDGE: Final = 256
"""Characters of an event's data kept at each end, to tell the call's response by its id."""
MAX_RUNS: Final = 4096
"""Runs whose recorded answer bytes are counted, least recently used forgotten first."""
_ERROR_CODE: Final = -32001
_LINE_END: Final = re.compile(r"\r\n|\r|\n")
_MAX_FIELD_NAME: Final = 64

Mode = Literal["whole", "stream", "headers"]


@dataclass(frozen=True, slots=True)
class Bounds:
    """How much is recorded: a call's arguments, an answer's excerpt, and the excerpts of
    one run's answers together; past the last, only sizes and findings."""

    argument_bytes: int = 16 * 1024
    answer_bytes: int = 64 * 1024
    run_bytes: int = 8 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class Compiled:
    """One pattern in force, compiled once per policy revision."""

    name: str
    action: Literal["alert", "block"]
    regex: LinearPattern
    servers: frozenset[str] | None

    @classmethod
    def of(cls, pattern: InspectionPattern) -> Compiled:
        return cls(
            pattern.name,
            pattern.action,
            compile_linear(pattern.regex),
            frozenset(pattern.servers) if pattern.servers is not None else None,
        )


def blocked_code(name: str) -> str:
    """The reason code a blocked answer names its pattern by."""
    return (BLOCKED_PREFIX + name.replace("-", "_"))[:64]


def arguments_record(encoded: bytes, limit: int, redactor: Redactor) -> dict[str, object]:
    """A call's ``arguments`` field: its parameters as redacted JSON text within ``limit``
    bytes, whether they were cut, their full size and the SHA-256 of their bytes as sent
    (the digest a permission request's key holds)."""
    text, truncated = cut(redactor.json_text(encoded), limit)
    return {
        "text": text,
        "truncated": truncated,
        "bytes": len(encoded),
        "sha256": hashlib.sha256(encoded).hexdigest(),
    }


class _EventData:
    """Reads an event stream's ``data:`` text piece by piece, without holding a line: a
    data line's value is passed on as it arrives. Data lines of one event are joined by
    a line feed, and events by another, as the excerpt shows them."""

    def __init__(self, rpc_id: str | int | None = None) -> None:
        self._id = (
            re.compile(r'"id"\s{0,8}:\s{0,8}' + re.escape(json.dumps(rpc_id)) + r"(?![0-9])")
            if rpc_id is not None
            else None
        )
        self._head = ""
        self._tail = ""
        self.responded = False
        """Whether an event that reads as the response to the call has ended."""
        self._name = ""
        self._field: str | None = None
        self._space = False
        self._in_event = False
        self._any = False
        self._cr = False
        self.events = 0
        self.pending = False
        """Whether what has passed ends inside an event: a cut must end it first."""

    def feed(self, text: str) -> str:
        out: list[str] = []
        if self._cr and text.startswith("\n"):
            text = text[1:]
        self._cr = text.endswith("\r")
        lines = _LINE_END.split(text)
        for line in lines[:-1]:
            self._segment(line, out)
            self._end_line(out)
        self._segment(lines[-1], out)
        return "".join(out)

    def _segment(self, segment: str, out: list[str]) -> None:
        if not segment:
            return
        self.pending = True
        if self._field is None:
            self._name += segment
            colon = self._name.find(":")
            if colon < 0:
                if len(self._name) > _MAX_FIELD_NAME:
                    self._field, self._name = "", ""  # no field this reader knows
                return
            self._start(self._name[:colon], out)
            segment, self._name = self._name[colon + 1 :], ""
            self._space = True
        if self._field == "data" and segment:
            if self._space:
                segment = segment.removeprefix(" ")
                self._space = False
            out.append(segment)
            if len(self._head) < _EDGE:
                self._head += segment[: _EDGE - len(self._head)]
            self._tail = (self._tail + segment[-_EDGE:])[-_EDGE:]

    def _start(self, name: str, out: list[str]) -> None:
        self._field = name
        if name == "data":
            if self._any:
                out.append("\n")
            self._in_event = self._any = True

    def _answers(self) -> bool:
        """Whether the event that ended reads as the call's response: its id, and a
        result or an error, at either end of its data (a heuristic over 256 characters)."""
        if self._id is None:
            return False
        ends = (self._head, self._tail)
        return any(self._id.search(end) for end in ends) and any(
            '"result"' in end or '"error"' in end for end in ends
        )

    def _end_line(self, out: list[str]) -> None:
        if self._field is None:
            if not self._name:
                # A blank line ends the event.
                if self._in_event:
                    self.events += 1
                    self.responded = self.responded or self._answers()
                self._in_event = self.pending = False
                self._head = self._tail = ""
                return
            self._start(self._name, out)  # a field name without a colon, an empty value
        self._field, self._name, self._space = None, "", False


@dataclass(slots=True)
class _Finding:
    pattern: Compiled
    offset: int
    excerpt: str


@dataclass(frozen=True, slots=True)
class Tracked:
    """An admitted MCP call whose answer is inspected."""

    sandbox_id: str
    subject: Mapping[str, object]
    rpc_id: str | int | None
    patterns: tuple[Compiled, ...]
    run_key: tuple[str, ...]
    """Whose recorded bytes the answer counts towards (``run_key``)."""


@dataclass(slots=True)
class _Text:
    """The answer's text so far, as the excerpt keeps it."""

    excerpt: list[str] = field(default_factory=list[str])
    excerpt_bytes: int = 0
    over: bool = False


@dataclass(slots=True)
class _View:
    """One reading of the answer's text that the patterns run over, with the carry for the
    next search."""

    carry: str = ""
    base: int = 0
    """Characters of this reading before the carry."""
    open: tuple[str, int] | None = None
    """A secret left open at the end of the text read so far: its continuation mode and
    where it began, in characters."""
    spans: list[Span] | None = None
    """The current search's secret spans, found once when a finding needs them."""
    opening: str | None = None
    window: str = ""
    start: int = 0
    """The current search's text, and the position of its first character in this reading."""


_SIMPLE: Final = {
    '"': '"',
    "\\": "\\",
    "/": "/",
    "b": "\b",
    "f": "\f",
    "n": "\n",
    "r": "\r",
    "t": "\t",
}
_HEX: Final = "0123456789abcdefABCDEF"
_HEX4: Final = re.compile(r"[0-9a-fA-F]{4}")
_LOW: Final = re.compile(r"\\u[dD][c-fC-F][0-9a-fA-F]{2}")
_LOW_SHAPE: Final = ("\\", "u", "dD", "cdefCDEF", _HEX, _HEX)
_ESCAPED_MEDIA: Final = ("application/json", "text/event-stream")


def _json_text(media: str | None) -> bool:
    """Whether the answer's text is JSON strings, in which a backslash starts an escape."""
    kind = (media or "").lower()
    return kind in _ESCAPED_MEDIA or kind.endswith("+json")


def _escape(data: str, at: int) -> tuple[str, int] | None:
    """The escape that starts at ``data[at]`` (a backslash): its text and how many characters
    it takes; None if ``data`` ends before it can be told. One that is no JSON escape is
    the backslash alone, as written."""
    if at + 1 >= len(data):
        return None
    letter = data[at + 1]
    if letter in _SIMPLE:
        return _SIMPLE[letter], 2
    if letter != "u":
        return "\\", 1
    digits = data[at + 2 : at + 6]
    if len(digits) < 4 and all(char in _HEX for char in digits):
        return None
    if not _HEX4.fullmatch(digits):
        return "\\", 1
    code = int(digits, 16)
    if 0xDC00 <= code < 0xE000:
        return "\\", 1  # a low surrogate with no high one before it
    if code < 0xD800 or code >= 0xE000:
        return chr(code), 6
    following = data[at + 6 : at + 12]
    if len(following) < 6:
        if all(char in allowed for char, allowed in zip(following, _LOW_SHAPE, strict=False)):
            return None  # the low half may still come
        return "\\", 1
    if not _LOW.fullmatch(following):
        return "\\", 1  # a high surrogate alone
    low = int(following[2:], 16)
    return chr(0x10000 + ((code - 0xD800) << 10) + (low - 0xDC00)), 12


def _union(first: list[Span], second: list[Span]) -> list[Span]:
    """Two lists of secret spans as one, sorted and merged."""
    merged: list[Span] = []
    for start, end, kind in sorted([*first, *second], key=lambda span: (span[0], -span[1])):
        if merged and start < merged[-1][1]:
            last = merged[-1]
            merged[-1] = (last[0], max(last[1], end), last[2])
        else:
            merged.append((start, end, kind))
    return merged


def _follow(view: _View, text: str) -> None:
    """Move ``view`` past ``text`` without searching it."""
    window = view.carry + text
    view.carry = window[-CARRY:]
    view.base += len(window) - len(view.carry)


class _Escapes:
    """Decodes JSON string escapes in text that arrives piece by piece, and maps a position
    in the decoded text to the position in the text as it came."""

    def __init__(self) -> None:
        self._rest = ""
        """An escape the last piece ended in the middle of, as written."""
        self._produced = 0
        self._delta = 0
        self.changed = False
        """Whether any escape was decoded: until then the decoded text is the text read."""
        self._at: list[int] = []
        self._shift: list[int] = []
        """After each decoded escape, the decoded position and the characters the text as
        it came is longer by from there on."""

    def feed(self, text: str) -> str:
        data = self._rest + text
        out: list[str] = []
        end = len(data)
        position = 0
        rest = ""
        while position < end:
            start = data.find("\\", position)
            if start < 0:
                out.append(data[position:])
                self._produced += end - position
                break
            if start > position:
                out.append(data[position:start])
                self._produced += start - position
            found = _escape(data, start)
            if found is None:
                rest = data[start:]
                break
            decoded, used = found
            out.append(decoded)
            self._produced += len(decoded)
            if used != len(decoded):
                self._delta += used - len(decoded)
                self._at.append(self._produced)
                self._shift.append(self._delta)
                self.changed = True
            position = start + used
        self._rest = rest
        return "".join(out)

    def raw_at(self, position: int) -> int:
        """Where the decoded text's ``position`` is in the text as it came."""
        index = bisect.bisect_right(self._at, position)
        return position + (self._shift[index - 1] if index else 0)

    def forget(self, before: int) -> None:
        """Drop what positions before ``before`` need."""
        index = bisect.bisect_right(self._at, before) - 1
        if index > 0:
            del self._at[:index]
            del self._shift[:index]


class AnswerInspection:
    """One answer, from its head to its end: fed piece by piece, recorded once."""

    def __init__(
        self,
        inspection: Inspection,
        tracked: Tracked,
        request_id: str,
        *,
        status: int,
        media: str | None,
        excerpt_limit: int,
    ) -> None:
        self._inspection = inspection
        self.tracked = tracked
        self._request_id = request_id
        self._status = status
        self._media = media
        self.events: _EventData | None = (
            _EventData(tracked.rpc_id) if media == "text/event-stream" else None
        )
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self._hash = hashlib.sha256()
        self._size = 0
        self._text = _Text()
        self._raw = _View()
        self._decoded = _View()
        self._escapes: _Escapes | None = _Escapes() if _json_text(media) else None
        self._excerpt_limit = excerpt_limit
        self._findings: list[_Finding] = []
        self._more = 0
        self._reported = 0
        self._inside = False
        self._answered = False
        self.after_result = False
        """The stream was cut after the call's response had passed: no error was added."""
        self.compacted = False
        """Recorded with names and sizes only: the never-dropped queue was full."""
        self.inspected = True
        """False when the answer passed unread (``reason``): no pattern applied to it."""
        self.reason: str | None = None
        self._seen: set[tuple[str, int]] = set()
        """Matches found, by pattern and offset in the text as it came, in either reading."""
        self.mode: Mode = "headers"
        self.note: str | None = None
        self.blocked: _Finding | None = None
        """The first ``block`` finding: delivery stops at the piece it is in."""
        self.decision: str = "passed"
        self.complete = False
        self._recorded = False

    @property
    def stream(self) -> bool:
        """Whether the answer is an event stream."""
        return self.events is not None

    def feed(self, data: bytes) -> None:
        """Take the next piece as it came, and search it unless delivery was stopped."""
        self._size += len(data)
        self._hash.update(data)
        if self.blocked is not None:
            return
        # Where the client stands before this piece: a cut replaces the whole piece.
        self._inside = self.events is not None and self.events.pending
        self._answered = self.events is not None and self.events.responded
        for start in range(0, len(data), PIECE):
            self._search(data[start : start + PIECE])
            if self.blocked is not None:
                return

    def _search(self, data: bytes) -> None:
        text = self._decoder.decode(data)
        if self.events is not None:
            text = self.events.feed(text)
        if not text:
            return
        self._keep(text)
        self._scan(self._raw, text, None)
        if self.blocked is not None:
            return
        floor = self._raw.base
        escapes = self._escapes
        if escapes is not None:
            decoded = escapes.feed(text)
            if escapes.changed:
                if decoded:
                    self._scan(self._decoded, decoded, escapes)
                    if self.blocked is not None:
                        return
            else:
                # No escape yet: the decoded text is the text as it came, which the raw
                # search covered. Only follow it, so that the carry is there when one comes.
                _follow(self._decoded, decoded)
                self._decoded.open = self._raw.open
            floor = min(floor, escapes.raw_at(self._decoded.base))
            escapes.forget(self._decoded.base)
        # Only a match that starts in the carry can be met again: forget the others.
        self._seen = {seen for seen in self._seen if seen[1] >= floor}

    def _scan(self, view: _View, text: str, escapes: _Escapes | None) -> None:
        """Search ``view``'s carry and the text after it; ``escapes`` maps a decoded view's
        positions to the text as it came."""
        window = view.carry + text
        held = len(view.carry)
        view.window, view.start = window, view.base
        # A secret that began before this window (beyond the carry) goes on at its start.
        opened = view.open
        view.opening = opened[0] if opened is not None and opened[1] < view.base else None
        view.spans = None
        for pattern in self.tracked.patterns:
            for match in pattern.regex.finditer(window):
                if match.end() <= held:
                    continue  # found in the search before
                self._found(view, escapes, pattern, match, window)
                if self.blocked is not None:
                    return
        redactor = self._inspection.redactor
        _, left = redactor.scan(window, view.opening, only_open=True)
        if left is None:
            view.open = None
        elif not (opened is not None and view.opening is not None and left[1] == 0):
            view.open = (left[0], view.base + left[1])
        view.carry = window[-CARRY:]
        view.base += len(window) - len(view.carry)

    def _keep(self, text: str) -> None:
        state = self._text
        if state.over or self._excerpt_limit <= 0:
            return
        room = self._excerpt_limit + _SLACK - state.excerpt_bytes
        data = text.encode("utf-8")
        if len(data) > room:
            text = data[:room].decode("utf-8", "ignore")
            state.over = True
        state.excerpt.append(text)
        state.excerpt_bytes += len(text.encode("utf-8"))

    def _found(
        self,
        view: _View,
        escapes: _Escapes | None,
        pattern: Compiled,
        match: LinearMatch,
        window: str,
    ) -> None:
        offset = view.base + match.start()
        if escapes is not None:
            offset = escapes.raw_at(offset)  # always in the text as it came
        # Seen before (a match the carry meets again, or the other reading found) counts
        # once, whether recorded or not.
        if (pattern.name, offset) in self._seen:
            return
        self._seen.add((pattern.name, offset))
        if len(self._findings) >= MAX_FINDINGS:
            self._more += 1
            return
        context = self._context(view, escapes, match, window)
        found = _Finding(pattern, offset, context)
        self._findings.append(found)
        if pattern.action == "block" and self.blocked is None:
            self.blocked = found
            self.decision = "blocked"

    def _context(
        self, view: _View, escapes: _Escapes | None, match: LinearMatch, window: str
    ) -> str:
        """The text around a match, redacted as part of the text around it. A match in the
        decoded text is shown as the server wrote it, escapes undecoded, with the secrets
        either reading finds: a stray quote that decoding brings can hide a secret's name
        from the decoded reading, and an escaped character can hide its shape from the raw."""
        redactor = self._inspection.redactor
        # A secret an earlier piece left open goes on at the start of the text, so that a
        # secret the window cuts goes whole, however long.
        if view.spans is None:
            view.spans = redactor.scan(window, view.opening)[0]
        if escapes is None:
            middle = (match.start() + match.end()) // 2
            start = max(0, middle - FINDING_CONTEXT // 2)
            end = min(len(window), start + FINDING_CONTEXT)
            return redactor.window(window, start, end, spans=view.spans)
        raw = self._raw
        if raw.spans is None:
            raw.spans = redactor.scan(raw.window, raw.opening)[0]

        def local(position: int) -> int:
            """A position of the decoded search in the raw search's text, kept inside it."""
            found = escapes.raw_at(view.start + position) - raw.start
            return max(0, min(len(raw.window), found))

        middle = (local(match.start()) + local(match.end())) // 2
        start = max(0, middle - FINDING_CONTEXT // 2)
        end = min(len(raw.window), start + FINDING_CONTEXT)
        both = [(local(low), local(high), kind) for low, high, kind in view.spans]
        spans = _union(raw.spans, [span for span in both if span[0] < span[1]])
        return redactor.window(raw.window, start, end, spans=spans)

    def new_findings(self) -> list[tuple[str, str]]:
        """The patterns found since the last call, with their actions, for OpenShell's
        log of the piece they were found in."""
        found = self._findings[self._reported :]
        self._reported = len(self._findings)
        return [(item.pattern.name, item.pattern.action) for item in found]

    def cut_event(self) -> bytes:
        """What replaces the piece a ``block`` pattern matched in an event stream: an end
        to the event the client was receiving, if it was inside one, and an MCP error
        naming the finding, as the answer to the call. When the call's response has
        already passed, nothing: a second response to one call would confuse the client,
        so the stream is only cut (``after_result``)."""
        if self._answered:
            self.after_result = True
            return b""
        name = self.blocked.pattern.name if self.blocked is not None else "a pattern"
        message = {
            "jsonrpc": "2.0",
            "id": self.tracked.rpc_id,
            "error": {
                "code": _ERROR_CODE,
                "message": (
                    f"{blocked_code(name)}: the platform withheld the rest of this answer: "
                    f"MCP traffic inspection found {name}"
                ),
            },
        }
        text = json.dumps(message, separators=(",", ":"))
        event = b"event: message\ndata: " + text.encode() + b"\n\n"
        return (b"\n\n" if self._inside else b"") + event

    def finish(self, *, complete: bool) -> None:
        """Record the answer once: ``complete`` when its last piece was read."""
        if self._recorded:
            return
        self._recorded = True
        self.complete = complete
        self._inspection.record(self)

    def payload(self) -> tuple[dict[str, object], int]:
        """The ``mcp-answer`` record, and the excerpt bytes it charges its run."""
        state = self._text
        excerpt: dict[str, object] | None = None
        charged = 0
        if self.mode != "headers" and self._excerpt_limit > 0:
            redacted = self._inspection.redactor.text("".join(state.excerpt))
            text, truncated = cut(redacted, self._excerpt_limit)
            excerpt = {"text": text, "truncated": truncated or state.over}
            charged = len(text.encode("utf-8"))
        payload: dict[str, object] = {
            **self.tracked.subject,
            "request_id": self._request_id,
            "status": self._status,
            "content_type": self._media,
            "mode": self.mode,
            "bytes": self._size,
            "complete": self.complete,
            "sha256": self._hash.hexdigest() if self.complete and self.mode != "headers" else None,
            "excerpt": excerpt,
            "note": self.note,
            "findings": [
                {
                    "name": found.pattern.name,
                    "action": found.pattern.action,
                    "offset": found.offset,
                    "excerpt": found.excerpt,
                }
                for found in self._findings
            ],
            "decision": {
                "action": self.decision,
                "pattern": self.blocked.pattern.name if self.blocked is not None else None,
                "reason_code": (
                    blocked_code(self.blocked.pattern.name) if self.blocked is not None else None
                ),
            },
        }
        if self.events is not None:
            payload["events"] = self.events.events
        if self._more:
            payload["more_findings"] = self._more
        if self.after_result:
            payload["cut_after_result"] = True
        if self.compacted:
            payload["compacted"] = True
        if not self.inspected:
            payload["inspected"] = False
            payload["reason"] = self.reason
        return payload, charged

    def important(self) -> bool:
        """Whether the answer's records must not be dropped: a finding, a block, a cut or
        an answer that was not inspected."""
        return bool(self._findings) or self.decision != "passed" or not self.inspected

    def lean(self) -> None:
        """Record this answer without its excerpt (a backlog of records)."""
        self._text.excerpt.clear()
        self._excerpt_limit = 0
        self.note = self.note or "excerpt left out: the record queue was full"

    def compact(self) -> None:
        """Keep only what the record needs at least: names, sizes and the decision, no
        excerpt, no text around a finding, nothing held for searching."""
        self.lean()
        for found in self._findings:
            found.excerpt = ""
        self._seen.clear()
        self._raw = _View()
        self._decoded = _View()
        self._escapes = None
        self.events = None if self.events is None else _EventData()
        self.compacted = True

    def pass_unread(self, reason: str) -> None:
        """Let the answer pass without reading it (``reason``, such as ``capacity``): its
        record has its head only, and no pattern, ``block`` included, applies to it."""
        self.inspected = False
        self.reason = reason
        self.mode = "headers"
        self.note = f"not inspected: {reason}"

    def alert(self) -> dict[str, object] | None:
        """The ``mcp-answer-alert`` record, when a pattern was found or the answer passed
        uninspected (``action`` ``uninspected``, with its reason)."""
        if not self.inspected:
            return {
                **self.tracked.subject,
                "request_id": self._request_id,
                "action": "block" if self.decision == "blocked" else "uninspected",
                "reason": self.reason,
                "findings": [],
            }
        if not self._findings:
            return None
        return {
            **self.tracked.subject,
            "request_id": self._request_id,
            "action": "block" if self.blocked is not None else "alert",
            "findings": [
                {"name": found.pattern.name, "action": found.pattern.action}
                for found in self._findings
            ],
        }


class Inspection:
    """The middleware's side of MCP traffic inspection: which calls' answers it waits for,
    the patterns in force per policy revision, and each run's recorded bytes."""

    def __init__(
        self,
        events: EventSink,
        *,
        bounds: Bounds | None = None,
        patterns: Sequence[InspectionPattern] = (),
        defaults: bool = True,
        redactor: Redactor | None = None,
        max_pending: int = MAX_PENDING,
        max_urgent: int = MAX_URGENT,
        drop_report_seconds: float = DROP_REPORT_SECONDS,
        clock: Clock = SYSTEM_CLOCK,
    ) -> None:
        """``patterns`` and ``defaults`` are the deployment's (``[inspect]``), under each
        project policy's ``mcp.inspection``."""
        self.bounds = bounds if bounds is not None else Bounds()
        self.redactor = redactor if redactor is not None else Redactor()
        self._events = events
        self._patterns = tuple(patterns)
        self._defaults = defaults
        self._lock = threading.Lock()
        self._compiled: OrderedDict[str, tuple[Compiled, ...]] = OrderedDict()
        self._waiting: OrderedDict[str, OrderedDict[str, Tracked]] = OrderedDict()
        """Each sandbox's admitted calls by request id, so that one cannot push out another's."""
        self._runs: OrderedDict[tuple[str, ...], int] = OrderedDict()
        self._cond = threading.Condition()
        self._urgent: deque[AnswerInspection] = deque()
        """Answers with a finding, a block or no inspection: never dropped, written first."""
        self._plain: deque[AnswerInspection] = deque()
        self._busy = 0
        self._max_pending = max_pending
        self._max_urgent = max_urgent
        self._writer: threading.Thread | None = None
        self.dropped = 0
        """Plain answers not recorded because the queue was full, since the start."""
        self.compacted = 0
        """Answers with a finding, a block or no inspection recorded compact, since the
        start."""
        self._drops: dict[tuple[str, str], list[int]] = {}
        """Plain answers dropped and important ones compacted, not yet reported, by
        project and sandbox."""
        self._drop_report = drop_report_seconds
        self._clock = clock
        self._reported_at = clock.monotonic()

    def arguments(self, encoded: bytes) -> dict[str, object]:
        """A call's ``arguments`` field, redacted and bounded."""
        return arguments_record(encoded, self.bounds.argument_bytes, self.redactor)

    def patterns(self, loaded: LoadedPolicy, server: str) -> tuple[Compiled, ...]:
        """The patterns in force for ``server`` under a policy revision, compiled once."""
        with self._lock:
            compiled = self._compiled.get(loaded.digest)
            if compiled is None:
                chosen = effective_patterns(
                    self._patterns, self._defaults, loaded.policy.mcp.inspection
                )
                compiled = tuple(Compiled.of(pattern) for pattern in chosen)
                self._compiled[loaded.digest] = compiled
                while len(self._compiled) > 64:
                    self._compiled.popitem(last=False)
        return tuple(p for p in compiled if p.servers is None or server in p.servers)

    def expect(self, request_id: str, tracked: Tracked) -> None:
        """Note an admitted call, so that its answer is inspected."""
        if not request_id:
            return
        with self._lock:
            calls = self._waiting.setdefault(tracked.sandbox_id, OrderedDict())
            self._waiting.move_to_end(tracked.sandbox_id)
            calls[request_id] = tracked
            while len(calls) > MAX_TRACKED:
                calls.popitem(last=False)
            while len(self._waiting) > MAX_SANDBOXES:
                self._waiting.popitem(last=False)

    def answer(
        self, request_id: str, sandbox_id: str, *, status: int, media: str | None
    ) -> AnswerInspection | None:
        """The inspection of the answer to an admitted call, once; None for any other."""
        with self._lock:
            calls = self._waiting.get(sandbox_id)
            tracked = calls.pop(request_id, None) if calls is not None else None
            if tracked is None:
                return None
            used = self._runs.get(tracked.run_key, 0)
        room = max(0, self.bounds.run_bytes - used)
        answer = AnswerInspection(
            self,
            tracked,
            request_id,
            status=status,
            media=media,
            excerpt_limit=min(self.bounds.answer_bytes, room),
        )
        if room <= 0:
            answer.note = "run_bytes: this run's answers reached their recorded bound"
        return answer

    def record(self, answer: AnswerInspection) -> None:
        """Queue an answer for its records, written on a thread of their own so that the
        response never waits for redaction or the store. An answer with a finding, a
        block or no inspection is never dropped: it goes ahead of the others, without
        its excerpt when the queue is full. A plain answer is dropped when the queue is
        full, counted by sandbox into a periodic ``mcp-inspection-dropped`` record."""
        key = (str(answer.tracked.subject.get("project")), answer.tracked.sandbox_id)
        with self._cond:
            if answer.important():
                if len(self._urgent) >= self._max_urgent:
                    answer.compact()  # memory stays bounded per answer, the record kept
                    self._drops.setdefault(key, [0, 0])[1] += 1
                    self.compacted += 1
                elif len(self._urgent) + len(self._plain) >= self._max_pending:
                    answer.lean()  # the excerpt's redaction is what a backlog cannot afford
                self._urgent.append(answer)
            elif len(self._plain) >= self._max_pending:
                self._drops.setdefault(key, [0, 0])[0] += 1
                self.dropped += 1
                _log.warning("MCP answer records dropped, the queue being full: %d", self.dropped)
            else:
                self._plain.append(answer)
            self._cond.notify_all()
            if self._writer is None:
                self._writer = threading.Thread(
                    target=self._write_all, name="mcp-answer-records", daemon=True
                )
                self._writer.start()

    def flush(self) -> None:
        """Wait until every queued answer is recorded, then report drops (shutdown,
        tests)."""
        with self._cond:
            while self._urgent or self._plain or self._busy:
                self._cond.wait(0.1)
        self._report_drops(force=True)

    def _write_all(self) -> None:
        while True:
            with self._cond:
                while not self._urgent and not self._plain:
                    self._cond.wait(self._drop_report if self._drops else None)
                    if self._drops and self._due():
                        break
                queue = self._urgent or self._plain
                answer = queue.popleft() if queue else None
                self._busy += answer is not None
            try:
                if answer is not None:
                    self._write(answer)
                self._report_drops()
            except Exception:
                _log.exception("could not record an MCP answer")
            finally:
                with self._cond:
                    self._busy -= answer is not None
                    self._cond.notify_all()

    def _due(self) -> bool:
        return self._clock.monotonic() - self._reported_at >= self._drop_report

    def _report_drops(self, *, force: bool = False) -> None:
        """Write the drops counted since the last report, when one is due."""
        with self._cond:
            if not self._drops or not (force or self._due()):
                return
            drops, self._drops = self._drops, {}
            self._reported_at = self._clock.monotonic()
        counts = [
            {"project": project, "sandbox_id": sandbox, "answers": dropped, "compacted": kept}
            for (project, sandbox), (dropped, kept) in sorted(drops.items())
        ]
        total = sum(item[0] for item in drops.values())
        compacted = sum(item[1] for item in drops.values())
        try:
            self._events.record(
                DROPPED_RECORD,
                {"answers": total, "compacted": compacted, "counts": counts[:256]},
            )
        except Exception:
            _log.exception("could not record %d dropped MCP answers", total)

    def _write(self, answer: AnswerInspection) -> None:
        """Write an answer's records; a store that fails is logged, never the client's
        problem: inspection changes no answer but a blocked one."""
        payload, charged = answer.payload()
        run_key = answer.tracked.run_key
        with self._lock:
            self._runs[run_key] = self._runs.get(run_key, 0) + charged
            self._runs.move_to_end(run_key)
            while len(self._runs) > MAX_RUNS:
                self._runs.popitem(last=False)
        try:
            seq = self._events.record(ANSWER_RECORD, payload)
            alert = answer.alert()
            if alert is not None:
                self._events.record(ALERT_RECORD, {**alert, "answer_seq": seq})
        except Exception:
            _log.exception("could not record the MCP answer to request %s", payload["request_id"])


def run_key(subject: Mapping[str, object]) -> tuple[str, ...]:
    """Whose recorded bytes an answer counts towards: its run's, by project, session and
    run; a call that names no run counts towards its session's calls without one, a key no
    run's name can take."""
    project, session, run = (subject.get(name) for name in ("project", "session", "run"))
    if isinstance(run, str):
        return ("run", str(project), str(session), run)
    return ("session", str(project), str(session))
