# SPDX-License-Identifier: MIT
"""Permission requests and their answers, kept in the event store (docs/POLICY-MIDDLEWARE.md).

An escalated request is a permission request. Its first occurrence is recorded as an
``escalation``. Retries of the same request, from the same sandbox under the same
policy revision, share its fingerprint and are not recorded again. An operator
answers a request with an ``approval`` or a ``denial`` record
(``sandbox-science-policy-middleware approve`` or ``deny``), which the middleware
honours on the next retry:
- An approval lets matching requests through until it expires. Its scope is the
  request itself, or every path on that host and port with that method.
- A denial refuses them.

Answers apply to the sandbox and policy revision of the request they answer, so a new
revision asks again. Unanswered past the rule's escalation timeout, a request follows
``on_timeout``: a read proceeds as an alert, and anything else is denied as timed out.
"""

from __future__ import annotations

import hashlib
import json
import threading
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final, Literal, cast

from sandbox_science.event_store import Record

ESCALATION: Final = "escalation"
APPROVAL: Final = "approval"
DENIAL: Final = "denial"
KINDS: Final = (ESCALATION, APPROVAL, DENIAL)

Scope = Literal["request", "host"]
SCOPES: Final[tuple[Scope, ...]] = ("request", "host")


@dataclass(frozen=True, slots=True)
class Target:
    """What a permission request asks to reach, as an answer's scope compares it."""

    sandbox_id: str
    policy_digest: str
    method: str
    host: str
    port: int
    path: str

    def fingerprint(self) -> str:
        """The request's identity: the same fingerprint means a retry of the same request."""
        parts = [self.sandbox_id, self.policy_digest, self.method, self.host, self.port, self.path]
        return hashlib.sha256(json.dumps(parts, separators=(",", ":")).encode()).hexdigest()

    def covers(self, other: Target, scope: Scope) -> bool:
        """Whether an answer to this request, with ``scope``, applies to ``other``."""
        same_place = (
            self.sandbox_id,
            self.policy_digest,
            self.method,
            self.host,
            self.port,
        ) == (other.sandbox_id, other.policy_digest, other.method, other.host, other.port)
        return same_place and (scope == "host" or self.path == other.path)

    def payload(self) -> dict[str, object]:
        return {
            "sandbox_id": self.sandbox_id,
            "policy_digest": self.policy_digest,
            "method": self.method,
            "host": self.host,
            "port": self.port,
            "path": self.path,
        }

    @classmethod
    def from_payload(cls, data: Mapping[str, object]) -> Target | None:
        try:
            port = data["port"]
            if not isinstance(port, int) or isinstance(port, bool):
                return None
            fields = [data[name] for name in ("sandbox_id", "policy_digest", "method", "host")]
            path = data["path"]
        except KeyError:
            return None
        if not all(isinstance(value, str) for value in [*fields, path]):
            return None
        sandbox_id, digest, method, host = (str(value) for value in fields)
        return cls(sandbox_id, digest, method, host, port, str(path))


@dataclass(frozen=True, slots=True)
class PendingRequest:
    seq: int
    target: Target
    recorded: datetime
    timeout: timedelta
    on_timeout: str
    """``deny``, or ``alert`` when the rule lets a read proceed unanswered."""

    def expired(self, now: datetime) -> bool:
        return now - self.recorded >= self.timeout


@dataclass(frozen=True, slots=True)
class Answer:
    seq: int
    request_seq: int
    kind: Literal["approval", "denial"]
    target: Target
    scope: Scope
    expires: datetime | None

    def applies(self, target: Target, now: datetime) -> bool:
        live = self.expires is None or now < self.expires
        return live and self.target.covers(target, self.scope)


def _time(text: object) -> datetime | None:
    if not isinstance(text, str):
        return None
    try:
        value = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return value if value.tzinfo is not None else None


class Approvals:
    """Pending requests and answers, rebuilt from the store's records and kept current.

    The store is the only truth: records the middleware or the command line wrote,
    read in sequence. Malformed records are skipped, never trusted.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._last_seq = 0
        self._requests: dict[int, PendingRequest] = {}
        self._by_fingerprint: dict[str, int] = {}
        self._answers: list[Answer] = []
        self._answered: set[int] = set()

    @property
    def last_seq(self) -> int:
        with self._lock:
            return self._last_seq

    def load(self, records: Iterable[Record]) -> None:
        """Take in records newer than those seen so far, in sequence order."""
        with self._lock:
            for record in records:
                if record.seq <= self._last_seq:
                    continue
                self._last_seq = record.seq
                if record.kind in KINDS:
                    self._take(record)

    def _take(self, record: Record) -> None:
        try:
            data = record.data()
        except ValueError:
            return
        recorded = _time(record.time)
        if recorded is None:
            return
        if record.kind == ESCALATION:
            target = Target.from_payload(_mapping(data.get("target")))
            terms = _mapping(data.get("escalation"))
            seconds, on_timeout = terms.get("timeout_seconds"), terms.get("on_timeout")
            if (
                target is None
                or not isinstance(seconds, int)
                or on_timeout not in ("deny", "alert")
            ):
                return
            request = PendingRequest(
                record.seq, target, recorded, timedelta(seconds=seconds), str(on_timeout)
            )
            self._requests[record.seq] = request
            self._by_fingerprint.setdefault(target.fingerprint(), record.seq)
            return
        request_seq, scope = data.get("request_seq"), data.get("scope")
        target = Target.from_payload(_mapping(data.get("target")))
        if not isinstance(request_seq, int) or scope not in SCOPES or target is None:
            return
        expires = _time(data.get("expires")) if data.get("expires") is not None else None
        if data.get("expires") is not None and expires is None:
            return
        kind: Literal["approval", "denial"] = "approval" if record.kind == APPROVAL else "denial"
        answer = Answer(record.seq, request_seq, kind, target, scope, expires)
        self._answers.append(answer)
        self._answered.add(request_seq)

    def answer(self, target: Target, now: datetime) -> Answer | None:
        """The newest live answer that covers ``target``: a later answer replaces an earlier one."""
        with self._lock:
            matching = [answer for answer in self._answers if answer.applies(target, now)]
        if not matching:
            return None
        newest = max(answer.seq for answer in matching)
        return next(answer for answer in matching if answer.seq == newest)

    def pending(self, target: Target) -> PendingRequest | None:
        """The recorded request with this target's fingerprint, if there is one."""
        with self._lock:
            seq = self._by_fingerprint.get(target.fingerprint())
            return self._requests.get(seq) if seq is not None else None

    def request(self, seq: int) -> PendingRequest | None:
        with self._lock:
            return self._requests.get(seq)

    def unanswered(self) -> list[PendingRequest]:
        with self._lock:
            return [r for seq, r in sorted(self._requests.items()) if seq not in self._answered]


def _mapping(value: object) -> Mapping[str, object]:
    return cast(Mapping[str, object], value) if isinstance(value, dict) else {}
