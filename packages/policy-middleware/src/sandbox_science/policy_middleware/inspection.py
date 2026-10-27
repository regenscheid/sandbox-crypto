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
"""

from __future__ import annotations

import codecs
import hashlib
import json
import logging
import re
import threading
from collections import OrderedDict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Final, Literal

from sandbox_science.policy import LoadedPolicy
from sandbox_science.policy.mcp_inspection import effective_patterns
from sandbox_science.policy.model import InspectionPattern

from .events import EventSink
from .redaction import Redactor, cut

_log = logging.getLogger(__name__)

ANSWER_RECORD: Final = "mcp-answer"
ALERT_RECORD: Final = "mcp-answer-alert"
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
MAX_TRACKED: Final = 4096
"""Calls whose answers the middleware waits for, oldest forgotten first."""
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
    regex: re.Pattern[str]
    servers: frozenset[str] | None

    @classmethod
    def of(cls, pattern: InspectionPattern) -> Compiled:
        return cls(
            pattern.name,
            pattern.action,
            re.compile(pattern.regex),
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

    def __init__(self) -> None:
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

    def _start(self, name: str, out: list[str]) -> None:
        self._field = name
        if name == "data":
            if self._any:
                out.append("\n")
            self._in_event = self._any = True

    def _end_line(self, out: list[str]) -> None:
        if self._field is None:
            if not self._name:
                # A blank line ends the event.
                if self._in_event:
                    self.events += 1
                self._in_event = self.pending = False
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


@dataclass(slots=True)
class _Text:
    """The answer's text so far: what the excerpt keeps, and the carry for the next search."""

    excerpt: list[str] = field(default_factory=list[str])
    excerpt_bytes: int = 0
    over: bool = False
    carry: str = ""
    base: int = 0
    """Characters of text before the carry."""


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
        self.events: _EventData | None = _EventData() if media == "text/event-stream" else None
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self._hash = hashlib.sha256()
        self._size = 0
        self._text = _Text()
        self._excerpt_limit = excerpt_limit
        self._findings: list[_Finding] = []
        self._more = 0
        self._reported = 0
        self._inside = False
        self._seen: set[tuple[str, int]] = set()
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
        state = self._text
        window = state.carry + text
        held = len(state.carry)
        for pattern in self.tracked.patterns:
            for match in pattern.regex.finditer(window):
                if match.end() <= held:
                    continue  # found in the search before
                self._found(pattern, match, window, state.base + match.start())
                if self.blocked is not None:
                    return
        state.carry = window[-CARRY:]
        state.base += len(window) - len(state.carry)

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

    def _found(self, pattern: Compiled, match: re.Match[str], window: str, offset: int) -> None:
        if len(self._findings) >= MAX_FINDINGS:
            self._more += 1  # counted, not remembered: the set of seen matches stays small
            return
        if (pattern.name, offset) in self._seen:
            return
        self._seen.add((pattern.name, offset))
        middle = (match.start() + match.end()) // 2
        start = max(0, middle - FINDING_CONTEXT // 2)
        context = window[start : start + FINDING_CONTEXT]
        found = _Finding(pattern, offset, self._inspection.redactor.text(context))
        self._findings.append(found)
        if pattern.action == "block" and self.blocked is None:
            self.blocked = found
            self.decision = "blocked"

    def new_findings(self) -> list[tuple[str, str]]:
        """The patterns found since the last call, with their actions, for OpenShell's
        log of the piece they were found in."""
        found = self._findings[self._reported :]
        self._reported = len(self._findings)
        return [(item.pattern.name, item.pattern.action) for item in found]

    def cut_event(self) -> bytes:
        """What replaces the piece a ``block`` pattern matched in an event stream: an end
        to the event the client was receiving, if it was inside one, and an MCP error
        naming the finding, as the answer to the call."""
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
        return payload, charged

    def alert(self) -> dict[str, object] | None:
        """The ``mcp-answer-alert`` record, when a pattern was found."""
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
        self._waiting: OrderedDict[str, Tracked] = OrderedDict()
        self._runs: OrderedDict[tuple[str, ...], int] = OrderedDict()

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
            self._waiting[request_id] = tracked
            while len(self._waiting) > MAX_TRACKED:
                self._waiting.popitem(last=False)

    def answer(
        self, request_id: str, sandbox_id: str, *, status: int, media: str | None
    ) -> AnswerInspection | None:
        """The inspection of the answer to an admitted call, once; None for any other."""
        with self._lock:
            tracked = self._waiting.pop(request_id, None)
            if tracked is None or tracked.sandbox_id != sandbox_id:
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
    """Whose run an answer counts towards: project, session and run."""
    return tuple(str(subject.get(name)) for name in ("project", "session", "run"))
