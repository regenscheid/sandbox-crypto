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
        self.events: _EventData | None = (
            _EventData(tracked.rpc_id) if media == "text/event-stream" else None
        )
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self._hash = hashlib.sha256()
        self._size = 0
        self._text = _Text()
        self._excerpt_limit = excerpt_limit
        self._findings: list[_Finding] = []
        self._more = 0
        self._reported = 0
        self._inside = False
        self._answered = False
        self.after_result = False
        """The stream was cut after the call's response had passed: no error was added."""
        self.inspected = True
        """False when the answer passed unread (``reason``): no pattern applied to it."""
        self.reason: str | None = None
        self._seen: set[tuple[str, int]] = set()
        self._open: tuple[str, int] | None = None
        """A secret left open at the end of the text read so far: its continuation mode
        and where it began, in characters."""
        self._spans: list[Span] | None = None
        """The current search's secret spans, found once when a finding needs them."""
        self._opening: str | None = None
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
        state = self._text
        window = state.carry + text
        held = len(state.carry)
        # A secret that began before this window (beyond the carry) goes on at its start.
        opened = self._open
        self._opening = opened[0] if opened is not None and opened[1] < state.base else None
        self._spans = None
        for pattern in self.tracked.patterns:
            for match in pattern.regex.finditer(window):
                if match.end() <= held:
                    continue  # found in the search before
                self._found(pattern, match, window, state.base + match.start())
                if self.blocked is not None:
                    return
        redactor = self._inspection.redactor
        _, left = redactor.scan(window, self._opening, only_open=True)
        if left is None:
            self._open = None
        elif not (opened is not None and self._opening is not None and left[1] == 0):
            self._open = (left[0], state.base + left[1])
        state.carry = window[-CARRY:]
        state.base += len(window) - len(state.carry)
        # Only a match that starts in the carry can be met again: forget the others.
        self._seen = {seen for seen in self._seen if seen[1] >= state.base}

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

    def _found(self, pattern: Compiled, match: LinearMatch, window: str, offset: int) -> None:
        # Seen before (a match the carry meets again) counts once, whether recorded or not.
        if (pattern.name, offset) in self._seen:
            return
        self._seen.add((pattern.name, offset))
        if len(self._findings) >= MAX_FINDINGS:
            self._more += 1
            return
        middle = (match.start() + match.end()) // 2
        start = max(0, middle - FINDING_CONTEXT // 2)
        end = min(len(window), start + FINDING_CONTEXT)
        # Redacted with the secrets of the whole text searched (and one an earlier piece
        # left open), so that a secret the window cuts goes whole, however long.
        redactor = self._inspection.redactor
        if self._spans is None:
            self._spans = redactor.scan(window, self._opening)[0]
        context = redactor.window(window, start, end, spans=self._spans)
        found = _Finding(pattern, offset, context)
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
        self._writer: threading.Thread | None = None
        self.dropped = 0
        """Plain answers not recorded because the queue was full, since the start."""
        self._drops: dict[tuple[str, str], int] = {}
        """Drops not yet reported, by project and sandbox."""
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
        with self._cond:
            if answer.important():
                if len(self._plain) >= self._max_pending:
                    answer.lean()  # the excerpt's redaction is what a backlog cannot afford
                self._urgent.append(answer)
            elif len(self._plain) >= self._max_pending:
                key = (str(answer.tracked.subject.get("project")), answer.tracked.sandbox_id)
                self._drops[key] = self._drops.get(key, 0) + 1
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
            {"project": project, "sandbox_id": sandbox, "answers": count}
            for (project, sandbox), count in sorted(drops.items())
        ]
        try:
            self._events.record(
                DROPPED_RECORD, {"answers": sum(drops.values()), "counts": counts[:256]}
            )
        except Exception:
            _log.exception("could not record %d dropped MCP answers", sum(drops.values()))

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
