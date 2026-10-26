# SPDX-License-Identifier: MIT
"""Detect-and-hold (D95, D103): rate and shape rules over each session's outward calls.

The policy middleware counts every outward call it decides, by whose call it is (a
``Subject``: research code's by sandbox, since its requests name no session; the agent's
MCP calls by project and session), and asks the ``Detector`` after each one:

- ``host-rate``: more than ``host_rate_per_second`` times ``host_rate_window`` calls to one
  host within that window (a sustained burst);
- ``host-spread``: more than ``distinct_hosts`` hosts within ``distinct_hosts_window``;
- ``same-url``: one method and URL more than ``same_url`` times within ``shape_window``;
- ``sequential-ids``: a run of more than ``sequential_ids`` calls to one host whose paths
  differ only in their last number, stepping by one, within ``shape_window``;
- ``host-failures``: more than ``failures_per_host`` failed responses (4xx, 5xx) or
  refusals from one host within ``shape_window``;
- ``denial-streak``: more than ``denials`` refusals within ``denials_window``;
- ``daily-ceiling``: more than ``daily_calls`` calls of one project in a UTC day.

Each rule fires on the call that takes its count past its threshold, and on every call
while the count stays past it; the middleware then holds the subject (``Holds``). Half
way, a rule reports a near miss, once per crossing, for the log pipeline's dashboards.
Two more rules hold less than a session: ``action-url`` (``action_like``) holds one GET as
a permission request of its own, and ``tool-list-drift`` (``DriftLedger``) makes a
server's non-read calls ask until its classification is re-accepted.

Counts live in memory, bounded per subject and in subjects, and are lost on a restart;
holds and drifts are records in the event store and survive one.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
from collections import OrderedDict, deque
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Final, cast
from urllib.parse import parse_qsl, unquote

from .approvals.keys import RequestKey
from .approvals.ledger import APPROVAL, DENIAL, ESCALATION, RecordLike
from .model import Detection

HOST_RATE: Final = "host-rate"
HOST_SPREAD: Final = "host-spread"
SAME_URL: Final = "same-url"
SEQUENTIAL_IDS: Final = "sequential-ids"
HOST_FAILURES: Final = "host-failures"
DENIAL_STREAK: Final = "denial-streak"
DAILY_CEILING: Final = "daily-ceiling"
ACTION_URL: Final = "action-url"
TOOL_LIST_DRIFT: Final = "tool-list-drift"
RULE_PREFIX: Final = "detect."
"""Decisions and records name a detection rule as ``detect.<rule>``."""

HOLD_TIMEOUT: Final = timedelta(days=30)
"""What a hold's permission request gives as its timeout, for the card: a hold never lifts
by itself, only by the user's answer."""
DRIFT_RECORD: Final = "mcp-tools-drift"
ACCEPTED_RECORD: Final = "mcp-tools-accepted"
"""A re-acceptance of a drifted server's classification: the operator's
(``sandbox-science-policy-middleware accept-tools``, ``{"server"}``, for every project) or
the user's through the worker (``{"project", "server", "reason", "actor"}``, for that
project)."""
CLASSIFICATION_ACCEPTED: Final = "mcp-classification-accepted"
"""A project's acceptance of a new revision of a server's classification (CL-3), which ends
a drift's hold for that project as a re-acceptance does."""
LISTING_UNREAD_RECORD: Final = "mcp-tools-listing-unread"
"""A ``tools/list`` answer the middleware let pass unread: an event stream OpenShell would
not offer it whole. The runtime's client filters it and reports it instead (D98)."""
LISTING_REPORTED_RECORD: Final = "mcp-tools-listing-reported"
"""The worker took the agent runtime's report of a listing (source ``runtime``): what pairs
with an unread listing, drifted or not (D98)."""
RUNTIME_SOURCE: Final = "runtime"
"""The source of records the worker writes from the agent runtime's listing reports."""
RECORD_KINDS: Final = (
    ESCALATION,
    APPROVAL,
    DENIAL,
    DRIFT_RECORD,
    ACCEPTED_RECORD,
    CLASSIFICATION_ACCEPTED,
    LISTING_UNREAD_RECORD,
    LISTING_REPORTED_RECORD,
)
DRIFT_KINDS: Final = (DRIFT_RECORD, ACCEPTED_RECORD, CLASSIFICATION_ACCEPTED)

MAX_SUBJECTS: Final = 10_000
MAX_HOSTS: Final = 256
"""Hosts counted per subject, the least recently called forgotten first."""
MAX_URLS: Final = 1024
MAX_TEMPLATES: Final = 256
MAX_RECENT: Final = 5
"""Calls a hold's evidence lists, newest last."""
MAX_TEXT: Final = 200

_NUMBER: Final = re.compile(r"[0-9]{1,15}")
_OPAQUE: Final = re.compile(r"[A-Za-z0-9_\-.=~+]{20,}")
_HEX: Final = re.compile(r"[0-9a-fA-F]{32,}")
_WORDS: Final = re.compile(r"[A-Z]?[a-z]+|[A-Z]+(?![a-z])")
_ACTION_STEMS: Final = (
    "confirm", "unsubscrib", "subscrib", "delet", "remov", "approv", "verif", "activat",
    "accept", "declin", "reject", "cancel", "optout", "revok", "authoriz", "authoris",
    "validat", "reset", "rsvp", "redeem", "claim",
)  # fmt: skip
"""Words that make a URL with a token in it read as an action, by their start."""


@dataclass(frozen=True, slots=True)
class Subject:
    """Whose outward calls a hold pauses: research code's by sandbox, which its requests
    name instead of a session; the agent's MCP calls by project and session."""

    sandbox_id: str
    project: str = ""
    session: str = ""

    @property
    def key(self) -> str:
        if self.session:
            return f"session:{self.project}/{self.session}"
        return f"sandbox:{self.sandbox_id}"

    def hold_key(self, rule: str, opened: datetime) -> RequestKey:
        """The key of a hold of this subject's calls by ``rule``, opened at ``opened``."""
        stamp = opened.astimezone(UTC).isoformat(timespec="microseconds")
        return RequestKey.hold(self.project, self.sandbox_id, self.session, rule, stamp)

    @classmethod
    def of_hold(cls, key: RequestKey) -> Subject:
        fields = key.fields()
        return cls(str(fields["sandbox_id"]), str(fields["project_id"]), str(fields["session"]))


@dataclass(frozen=True, slots=True)
class Call:
    """One outward call as the rules count it: its host, method and what makes it the same
    call again (``target``: a URL's path and query, or an MCP tool and its arguments'
    digest); ``path`` only for HTTP, whose numbers may enumerate."""

    host: str
    method: str
    target: str
    path: str = ""
    label: str = ""
    """What a hold's evidence shows of the call, when not ``target``: a URL's query can carry
    data and credential placeholders, so records give only its size."""

    def shown(self) -> dict[str, str]:
        return {
            "method": self.method[:16],
            "host": self.host[:MAX_TEXT],
            "target": _cut(self.label or self.target),
        }


@dataclass(frozen=True, slots=True)
class Finding:
    """What a rule saw: its count against its threshold within its window; ``hold`` when
    the count is past the threshold, else a near miss."""

    rule: str
    count: int
    threshold: int
    window_seconds: int
    host: str | None = None
    hold: bool = False

    def payload(self) -> dict[str, object]:
        found: dict[str, object] = {
            "rule": self.rule,
            "count": self.count,
            "threshold": self.threshold,
            "window_seconds": self.window_seconds,
        }
        if self.host is not None:
            found["host"] = self.host[:MAX_TEXT]
        return found


def _cut(text: str) -> str:
    return text if len(text) <= MAX_TEXT else text[: MAX_TEXT - 1] + "…"


class _Times:
    """Times of events within a window, at most ``cap`` of them: enough to tell whether a
    count is past a threshold of ``cap - 1``."""

    __slots__ = ("times",)

    def __init__(self) -> None:
        self.times: deque[float] = deque()

    def add(self, now: float, window: float, cap: int) -> int:
        times = self.times
        times.append(now)
        while len(times) > cap or (times and times[0] <= now - window):
            times.popleft()
        return len(times)


@dataclass
class _Run:
    """A run of calls whose paths differ in one number stepping by one."""

    last: int
    step: int = 0
    length: int = 1
    at: float = 0.0


@dataclass
class _State:
    """One subject's counts."""

    rate: OrderedDict[str, _Times] = field(default_factory=OrderedDict[str, _Times])
    hosts: OrderedDict[str, float] = field(default_factory=OrderedDict[str, float])
    urls: OrderedDict[str, _Times] = field(default_factory=OrderedDict[str, _Times])
    runs: OrderedDict[str, _Run] = field(default_factory=OrderedDict[str, _Run])
    failures: OrderedDict[str, _Times] = field(default_factory=OrderedDict[str, _Times])
    denials: _Times = field(default_factory=_Times)
    recent: deque[dict[str, str]] = field(default_factory=lambda: deque(maxlen=MAX_RECENT))


def _entry[T](table: OrderedDict[str, T], key: str, make: type[T], cap: int) -> T:
    found = table.get(key)
    if found is None:
        found = table[key] = make()
        while len(table) > cap:
            table.popitem(last=False)
    else:
        table.move_to_end(key)
    return found


def _seconds(window: timedelta) -> int:
    return int(window.total_seconds())


def _judge(rule: str, count: int, threshold: int, window: int, host: str | None) -> Finding | None:
    """A hold past the threshold; a near miss on the call that first passes half of it."""
    if count > threshold:
        return Finding(rule, count, threshold, window, host, hold=True)
    if count == threshold // 2 + 1:
        return Finding(rule, count, threshold, window, host)
    return None


class Detector:
    """Bounded in-memory counts of each subject's calls, and the rules over them."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._states: OrderedDict[str, _State] = OrderedDict()
        self._days: OrderedDict[str, tuple[str, int]] = OrderedDict()

    def observe(
        self, settings: Detection, subject: Subject, project: str, call: Call, now: datetime
    ) -> list[Finding]:
        """Count one call of ``subject``'s, of ``project``; what the rules saw of it."""
        if not settings.enabled:
            return []
        at = now.timestamp()
        found: list[Finding | None] = []
        with self._lock:
            state = _entry(self._states, subject.key, _State, MAX_SUBJECTS)
            state.recent.append(call.shown())
            window = _seconds(settings.host_rate_window)
            limit = settings.host_rate_per_second * window
            count = _entry(state.rate, call.host, _Times, MAX_HOSTS).add(at, window, limit + 1)
            found.append(_judge(HOST_RATE, count, limit, window, call.host))
            found.append(self._spread(settings, state, call.host, at))
            shape = _seconds(settings.shape_window)
            url = f"{call.method} {call.host} {call.target}"
            count = _entry(state.urls, url, _Times, MAX_URLS).add(at, shape, settings.same_url + 1)
            found.append(_judge(SAME_URL, count, settings.same_url, shape, call.host))
            if call.path:
                found.append(self._sequence(settings, state, call, at))
            found.append(self._day(settings, project, now))
        return [finding for finding in found if finding is not None]

    def outcome(
        self, settings: Detection, subject: Subject, host: str, now: datetime, *, denied: bool
    ) -> list[Finding]:
        """Count a failed call to ``host``: refused (``denied``), or answered with an error
        status; what the rules saw of it."""
        if not settings.enabled:
            return []
        at = now.timestamp()
        shape = _seconds(settings.shape_window)
        found: list[Finding | None] = []
        with self._lock:
            state = _entry(self._states, subject.key, _State, MAX_SUBJECTS)
            limit = settings.failures_per_host
            count = _entry(state.failures, host, _Times, MAX_HOSTS).add(at, shape, limit + 1)
            found.append(_judge(HOST_FAILURES, count, limit, shape, host))
            if denied:
                window = _seconds(settings.denials_window)
                count = state.denials.add(at, window, settings.denials + 1)
                found.append(_judge(DENIAL_STREAK, count, settings.denials, window, None))
        return [finding for finding in found if finding is not None]

    def recent(self, subject: Subject) -> list[dict[str, str]]:
        """The subject's latest calls, newest last: a hold's evidence."""
        with self._lock:
            state = self._states.get(subject.key)
            return list(state.recent) if state is not None else []

    def clear(self, subject: Subject, project: str | None = None) -> None:
        """Forget a subject's counts, when the user clears its hold, so that only new calls
        hold it again; with ``project``, that project's count for the day too."""
        with self._lock:
            self._states.pop(subject.key, None)
            if project is not None:
                self._days.pop(project, None)

    def _spread(self, settings: Detection, state: _State, host: str, at: float) -> Finding | None:
        window = _seconds(settings.distinct_hosts_window)
        hosts = state.hosts
        hosts[host] = at
        hosts.move_to_end(host)
        while hosts and (
            len(hosts) > settings.distinct_hosts + 1 or next(iter(hosts.values())) <= at - window
        ):
            hosts.popitem(last=False)
        return _judge(HOST_SPREAD, len(hosts), settings.distinct_hosts, window, None)

    def _sequence(
        self, settings: Detection, state: _State, call: Call, at: float
    ) -> Finding | None:
        segments = call.path.split("/")
        index = next(
            (i for i in range(len(segments) - 1, -1, -1) if _NUMBER.fullmatch(segments[i])), None
        )
        if index is None:
            return None
        number = int(segments[index])
        template = call.host + "/".join([*segments[:index], "#", *segments[index + 1 :]])
        shape = _seconds(settings.shape_window)
        run = state.runs.get(template)
        if run is None or run.at <= at - shape:
            run = _Run(number, at=at)
        elif number - run.last in (-1, 1) and run.step in (0, number - run.last):
            run = _Run(number, number - run.last, run.length + 1, at)
        elif number != run.last:
            run = _Run(number, at=at)
        run.at = at
        state.runs[template] = run
        state.runs.move_to_end(template)
        while len(state.runs) > MAX_TEMPLATES:
            state.runs.popitem(last=False)
        return _judge(SEQUENTIAL_IDS, run.length, settings.sequential_ids, shape, call.host)

    def _day(self, settings: Detection, project: str, now: datetime) -> Finding | None:
        day = now.astimezone(UTC).date().isoformat()
        seen, count = self._days.get(project, (day, 0))
        count = count + 1 if seen == day else 1
        self._days[project] = (day, count)
        self._days.move_to_end(project)
        while len(self._days) > MAX_SUBJECTS:
            self._days.popitem(last=False)
        return _judge(DAILY_CEILING, count, settings.daily_calls, 86_400, None)


def action_like(path: str, query: str) -> bool:
    """Whether a URL reads as an action a GET would trigger: a long opaque token (a link's
    one-time code) beside a word such as ``confirm``, ``unsubscribe``, ``delete``,
    ``approve`` or ``verify``, in the path or the query."""
    parts = [unquote(part) for part in path.split("/") if part]
    for name, value in parse_qsl(query, keep_blank_values=True):
        parts += [name, value]
    token = any(_opaque(part) for part in parts)
    if not token:
        return False
    words = (word.lower() for part in parts for word in _WORDS.findall(part))
    return any(word.startswith(_ACTION_STEMS) for word in words)


def _opaque(part: str) -> bool:
    if _HEX.fullmatch(part):
        return True
    if not _OPAQUE.fullmatch(part):
        return False
    digits = sum(char.isdigit() for char in part)
    letters = sum(char.isalpha() for char in part)
    return digits >= 2 and letters >= 2


@dataclass(frozen=True, slots=True)
class HoldAnswer:
    """A hold the user answered: ``cleared`` (an approval) or stopped (a denial)."""

    subject: Subject
    rule: str
    cleared: bool


class Holds:
    """The holds an event store records, each open until the user answers it: a permission
    request with a ``hold`` key (``escalation``), and its ``approval`` (clear) or
    ``denial`` (stop). Malformed records are skipped, never trusted."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._last_seq = 0
        self._keys: dict[int, RequestKey] = {}
        self._open: dict[str, int] = {}

    def load(self, records: Iterable[RecordLike]) -> list[HoldAnswer]:
        """Take in records newer than those seen so far; the holds answered among them."""
        answered: list[HoldAnswer] = []
        with self._lock:
            for record in records:
                if record.seq <= self._last_seq:
                    continue
                self._last_seq = record.seq
                if record.kind in (ESCALATION, APPROVAL, DENIAL):
                    found = self._take(record)
                    if found is not None:
                        answered.append(found)
        return answered

    def _take(self, record: RecordLike) -> HoldAnswer | None:
        try:
            data = record.data()
        except ValueError:
            return None
        target = data.get("target")
        key = (
            RequestKey.from_payload(cast("dict[str, object]", target))
            if isinstance(target, dict)
            else None
        )
        if key is None or key.kind != "hold":
            return None
        if record.kind == ESCALATION:
            self._keys[record.seq] = key
            self._open[Subject.of_hold(key).key] = record.seq
            return None
        seq = data.get("request_seq")
        if not isinstance(seq, int) or self._keys.get(seq) != key:
            return None
        subject = Subject.of_hold(key)
        if self._open.get(subject.key) == seq:
            del self._open[subject.key]
        return HoldAnswer(subject, str(key.fields()["rule"]), record.kind == APPROVAL)

    def note(self, seq: int, key: RequestKey) -> None:
        """A hold this component just recorded, known before the next read."""
        with self._lock:
            self._keys[seq] = key
            self._open[Subject.of_hold(key).key] = seq

    def active(self, subject: Subject) -> int | None:
        """The open hold of ``subject``'s calls, by its request's sequence number."""
        with self._lock:
            return self._open.get(subject.key)


def pin_digest(pin: Mapping[str, str | None]) -> str:
    """A server's pinned tool list as one digest, which a drift names: a new pin (the
    classification reviewed and accepted again) ends the drift's escalation."""
    canonical = json.dumps(sorted(pin.items(), key=lambda item: item[0]), separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class DriftSeen:
    """A server's newest ``mcp-tools-drift`` record from one pin: its number, time, pin,
    the digest of the whole listing that drifted (``listing``; None in a record from before
    listings were digested) and its payload."""

    seq: int
    time: str
    pin: str | None
    listing: str | None
    data: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class AcceptanceSeen:
    """A project's newest acceptance of a server's drift from one pin: the listing it named
    (else the newest drift's) and that drift's record."""

    seq: int
    time: str
    listing: str | None
    drift_seq: int
    data: Mapping[str, object]


@dataclass
class _Accepted:
    listings: set[str] = field(default_factory=set[str])
    """The listings the project accepted from this pin."""
    legacy_seq: int = 0
    """The newest drift without a listing digest that an acceptance came after."""
    latest: AcceptanceSeen | None = None


def _text(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _texts(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in cast("list[object]", value) if isinstance(item, str)]


class DriftLedger:
    """Which servers' tool listings drifted from their pins and were not re-accepted since
    (D96, D103), by server and pin.

    Pins differ by project once a project has its own classification revision, so drifts
    are kept per server and pin: they hold every project whose classification in force has
    that pin. Every drift record is kept, by the digest of the listing it saw, so a later
    record never displaces an earlier one: a record can only add or keep a hold, never end
    one, whoever wrote it (the middleware, or the worker from the agent runtime's report).
    Only an acceptance ends a hold, for the project it names (``mcp-tools-accepted`` from the
    workspace, or ``mcp-classification-accepted``) and only for the listings it names
    (``listing`` and ``listings``; one that names none accepts the drifts recorded before
    it). A project's server is held while any drift from its pin is not accepted for it.
    The operator's ``mcp-tools-accepted``, which names no project, ends a server's drifts
    for every project; a project whose pin changes (a new revision in force) is no longer
    held by a drift from its old pin."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._last_seq = 0
        self._drifts: dict[tuple[str, str | None], dict[str | None, DriftSeen]] = {}
        self._accepted: dict[tuple[str, str, str | None], _Accepted] = {}

    def load(self, records: Iterable[RecordLike]) -> None:
        with self._lock:
            for record in records:
                if record.seq <= self._last_seq:
                    continue
                self._last_seq = record.seq
                if record.kind not in DRIFT_KINDS:
                    continue
                try:
                    data = record.data()
                except ValueError:
                    continue
                server, project = data.get("server"), data.get("project")
                if not isinstance(server, str):
                    continue
                if record.kind == DRIFT_RECORD:
                    pin, listing = _text(data.get("pin")), _text(data.get("listing"))
                    seen = DriftSeen(record.seq, record.time, pin, listing, data)
                    self._drifts.setdefault((server, pin), {})[listing] = seen
                elif isinstance(project, str):
                    self._accept(project, server, record, data)
                elif record.kind == ACCEPTED_RECORD:
                    for key in [key for key in self._drifts if key[0] == server]:
                        del self._drifts[key]

    def _accept(
        self, project: str, server: str, record: RecordLike, data: Mapping[str, object]
    ) -> None:
        """A project's acceptance: of the drifts from its ``pin`` if it names one, else of
        the server's from every pin, for the listings it names, else for those recorded
        before it."""
        pin = _text(data.get("pin"))
        named = {
            listing
            for listing in (_text(data.get("listing")), *_texts(data.get("listings")))
            if listing is not None
        }
        for (name, from_pin), seen in self._drifts.items():
            if name != server or ("pin" in data and from_pin != pin) or not seen:
                continue
            accepted = self._accepted.setdefault((project, server, from_pin), _Accepted())
            accepted.listings |= named or {key for key in seen if key is not None}
            legacy = seen.get(None)
            if legacy is not None and legacy.seq < record.seq:
                accepted.legacy_seq = max(accepted.legacy_seq, legacy.seq)
            newest = max(seen.values(), key=lambda drift: drift.seq)
            listing = _text(data.get("listing")) or newest.listing
            accepted.latest = AcceptanceSeen(record.seq, record.time, listing, newest.seq, data)

    def note(
        self, server: str, pin: str | None, seq: int | None, listing: str | None = None
    ) -> None:
        """A drift this component just recorded, known before the next read."""
        with self._lock:
            found = self._drifts.setdefault((server, pin), {})
            if listing not in found:
                found[listing] = DriftSeen(seq or 0, "", pin, listing, {})

    def known(self, server: str, pin: str | None, listing: str) -> bool:
        """Whether a drift of this listing from ``pin`` was recorded already: a listing
        seen again is no new drift, accepted or not."""
        with self._lock:
            return listing in self._drifts.get((server, pin), {})

    def _held(self, server: str, pin: str | None, project: str | None) -> list[DriftSeen]:
        found = [
            seen
            for key in dict.fromkeys(((server, pin), (server, None)))
            for seen in self._drifts.get(key, {}).values()
        ]
        if project is None:
            return found
        held: list[DriftSeen] = []
        for seen in found:
            accepted = self._accepted.get((project, server, seen.pin))
            if accepted is None or not (
                seen.listing in accepted.listings
                if seen.listing is not None
                else accepted.legacy_seq >= seen.seq
            ):
                held.append(seen)
        return held

    def drifted(self, server: str, pin: str | None, project: str | None = None) -> int | None:
        """The newest drift record that still makes ``server``'s non-read calls ask, if one
        does: one from the pin in force now (``pin``) not accepted for ``project``."""
        with self._lock:
            held = self._held(server, pin, project)
        return max(seen.seq for seen in held) if held else None

    def unaccepted(self, server: str, pin: str | None, project: str) -> list[DriftSeen]:
        """The drifts from ``pin`` the project has not accepted, newest first."""
        with self._lock:
            held = self._held(server, pin, project)
        return sorted(held, key=lambda seen: seen.seq, reverse=True)

    def accepted_listing(self, server: str, pin: str | None, project: str, listing: str) -> bool:
        """Whether the project accepted this listing's drift from ``pin``."""
        with self._lock:
            accepted = self._accepted.get((project, server, pin))
        return accepted is not None and listing in accepted.listings

    def latest(self, server: str, pin: str | None) -> DriftSeen | None:
        """The server's newest drift record from ``pin``, ended or not, unless the operator
        accepted it for every project."""
        with self._lock:
            for key in ((server, pin), (server, None)):
                found = self._drifts.get(key)
                if found:
                    return max(found.values(), key=lambda seen: seen.seq)
        return None

    def accepted(self, project: str, server: str, pin: str | None) -> AcceptanceSeen | None:
        """The project's newest acceptance of the server's drift from ``pin``."""
        with self._lock:
            found = self._accepted.get((project, server, pin))
        return found.latest if found is not None else None


LISTING_REPORT_GRACE: Final = timedelta(seconds=30)
"""How long after the middleware let a listing pass unread the runtime has to report it."""
LISTING_KINDS: Final = (
    LISTING_UNREAD_RECORD,
    LISTING_REPORTED_RECORD,
    ACCEPTED_RECORD,
    CLASSIFICATION_ACCEPTED,
)


def _record_time(text: str) -> datetime:
    """A record's time; one that does not read as a time counts as long past."""
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return datetime.min.replace(tzinfo=UTC)
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)


class UnreadListings:
    """Listings the middleware let pass unread (an event stream OpenShell would not offer it
    whole, D98) that the agent runtime has not reported since, by project and server.

    The runtime's client must report every complete listing; the worker records each report
    it takes (``mcp-tools-listing-reported``), drifted or not. An unread listing with no
    report for its project and server within ``grace`` is an unknown listing: the server's
    non-read calls ask, as for a drift, until a report arrives (which then starts a drift
    or not) or the project's user, or the operator, accepts the server's tools. A runtime
    that never reports is caught here, on the trusted side."""

    def __init__(self, grace: timedelta = LISTING_REPORT_GRACE) -> None:
        self.grace = grace
        self._lock = threading.Lock()
        self._last_seq = 0
        self._unread: dict[tuple[str, str], tuple[int, datetime]] = {}

    def load(self, records: Iterable[RecordLike]) -> None:
        with self._lock:
            for record in records:
                if record.seq <= self._last_seq:
                    continue
                self._last_seq = record.seq
                if record.kind not in LISTING_KINDS:
                    continue
                try:
                    data = record.data()
                except ValueError:
                    continue
                server, project = data.get("server"), data.get("project")
                if not isinstance(server, str):
                    continue
                if not isinstance(project, str):
                    if record.kind == ACCEPTED_RECORD:  # the operator's, for every project
                        for key in [key for key in self._unread if key[1] == server]:
                            del self._unread[key]
                    continue
                key = (project, server)
                if record.kind == LISTING_UNREAD_RECORD:
                    # The first unanswered one: its grace is not restarted by later ones.
                    self._unread.setdefault(key, (record.seq, _record_time(record.time)))
                else:
                    self._unread.pop(key, None)

    def note(self, project: str, server: str, seq: int | None, at: datetime) -> None:
        """An unread listing this component just recorded, known before the next read."""
        with self._lock:
            self._unread.setdefault((project, server), (seq or 0, at))

    def pending(self, project: str, server: str, now: datetime) -> int | None:
        """The unread listing record whose report is overdue, if one is."""
        with self._lock:
            found = self._unread.get((project, server))
        if found is None or now - found[1] < self.grace:
            return None
        return found[0]
