# SPDX-License-Identifier: MIT
"""The ledger of permission requests and their answers, folded from the store's records,
and how an escalated request stands (``resolve``)."""

from __future__ import annotations

import threading
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Final, Literal, Protocol

from pydantic import ValidationError

from sandbox_science.policy.reasons import APPROVAL_DENIED, APPROVAL_REQUIRED, ESCALATION_TIMED_OUT

from . import grants
from .keys import KEY_FIELDS, RequestKey
from .records import Answer, AnswerPayload, PermissionRequest, RequestPayload

ESCALATION: Final = "escalation"
APPROVAL: Final = "approval"
DENIAL: Final = "denial"
KINDS: Final = (ESCALATION, APPROVAL, DENIAL)


class RecordLike(Protocol):
    """What the ledger reads from an event store record."""

    @property
    def seq(self) -> int: ...

    @property
    def time(self) -> str: ...

    @property
    def kind(self) -> str: ...

    def data(self) -> dict[str, object]: ...


@dataclass(frozen=True, slots=True)
class Approved:
    """A live approval covers the request: it proceeds, once that is recorded."""

    answer: Answer


@dataclass(frozen=True, slots=True)
class Denied:
    """The newest live answer covering the request denies it."""

    answer: Answer


@dataclass(frozen=True, slots=True)
class Unrecorded:
    """No answer, and no permission request yet: the component records one, and refuses."""


@dataclass(frozen=True, slots=True)
class Waiting:
    """The permission request is recorded and has not timed out: refuse, and wait."""

    request: PermissionRequest


@dataclass(frozen=True, slots=True)
class TimedOut:
    """Nobody answered within the rule's timeout: ``request.on_timeout`` decides."""

    request: PermissionRequest


Resolution = Approved | Denied | Unrecorded | Waiting | TimedOut


class Approvals:
    """Pending requests and answers, rebuilt from the store's records and kept current.

    The store is the only truth: records a component or the command line wrote, read in
    sequence. Malformed records are skipped, never trusted.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._last_seq = 0
        self._requests: dict[int, PermissionRequest] = {}
        self._by_fingerprint: dict[str, int] = {}
        self._answers: list[Answer] = []
        self._answered: set[int] = set()
        self._withdrawn: set[int] = set()
        """The ``packages`` requests the worker withdrew: closed for good."""

    @property
    def last_seq(self) -> int:
        with self._lock:
            return self._last_seq

    def load(self, records: Iterable[RecordLike]) -> None:
        """Take in records newer than those seen so far, in sequence order."""
        with self._lock:
            for record in records:
                if record.seq <= self._last_seq:
                    continue
                self._last_seq = record.seq
                if record.kind in KINDS:
                    self._take(record)

    def _take(self, record: RecordLike) -> None:
        # A malformed record is skipped, never trusted.
        try:
            data = record.data()
            recorded = datetime.fromisoformat(record.time.replace("Z", "+00:00"))
        except ValueError:
            return
        if recorded.tzinfo is None:
            return
        if record.kind == ESCALATION:
            try:
                payload = RequestPayload.model_validate(data)
            except ValidationError:
                return
            request = PermissionRequest.from_record(record.seq, recorded, payload)
            self._requests[record.seq] = request
            self._by_fingerprint.setdefault(request.key.fingerprint(), record.seq)
            return
        try:
            answer = AnswerPayload.model_validate(data)
        except ValidationError:
            return
        kind: Literal["approval", "denial"] = "approval" if record.kind == APPROVAL else "denial"
        self._answers.append(
            Answer(
                record.seq, answer.request_seq, kind, answer.target, answer.scope, answer.expires
            )
        )
        self._answered.add(answer.request_seq)
        request = self._requests.get(answer.request_seq)
        if (
            kind == "denial"
            and answer.withdrawn
            and request is not None
            and request.key == answer.target
            and answer.target.kind == "packages"
        ):
            self._withdrawn.add(answer.request_seq)

    def resolve(self, key: RequestKey, now: datetime) -> Resolution:
        """How a request with ``key`` stands: the one state machine the policy middleware
        and the MCP proxy share. The newest live answer covering it decides; without one,
        its recorded request waits until the rule's timeout."""
        answer = self.answer(key, now)
        if answer is not None:
            return Denied(answer) if answer.kind == "denial" else Approved(answer)
        request = self.pending(key)
        if request is None:
            return Unrecorded()
        return TimedOut(request) if request.expired(now) else Waiting(request)

    def confirms(self, seq: int, reason: str, call: Mapping[str, str | int], now: datetime) -> bool:
        """Whether a refusal with ``reason`` that names request ``seq`` is one ``resolve``
        gives a call whose key has the fields ``call``: the request exists, and

        - ``approval_required``: it is that call's own request, and it waits or was
          approved since (the call's next try proceeds);
        - ``escalation_timed_out``: it is that call's own request, and nobody answered it;
        - ``approval_denied``: the newest live answer covering the call denies request
          ``seq``, which a ``tool`` or ``server`` scope lets differ from the call.

        Fields ``call`` leaves out are taken from the request. A component that only
        relays the refusal (an untrusted server could have written it) checks it here
        before it shows or waits on a request.
        """
        request = self.request(seq)
        if request is None or not call.keys() <= set(KEY_FIELDS[request.key.kind]):
            return False
        fields = {**request.key.fields(), **call}
        key = RequestKey(
            request.key.kind, tuple(fields[name] for name in KEY_FIELDS[request.key.kind])
        )
        resolution = self.resolve(key, now)
        if reason == APPROVAL_DENIED:
            return isinstance(resolution, Denied) and resolution.answer.request_seq == seq
        if key != request.key:
            return False
        if reason == APPROVAL_REQUIRED:
            return isinstance(resolution, Approved) or (
                isinstance(resolution, Waiting) and resolution.request.seq == seq
            )
        if reason == ESCALATION_TIMED_OUT:
            return isinstance(resolution, TimedOut) and resolution.request.seq == seq
        return False

    def answer(self, key: RequestKey, now: datetime) -> Answer | None:
        """The newest live answer that covers ``key``: a later answer replaces an earlier one."""
        with self._lock:
            matching = [answer for answer in self._answers if answer.applies(key, now)]
        if not matching:
            return None
        newest = max(answer.seq for answer in matching)
        return next(answer for answer in matching if answer.seq == newest)

    def pending(self, key: RequestKey) -> PermissionRequest | None:
        """The recorded request with this key's fingerprint, if there is one."""
        with self._lock:
            seq = self._by_fingerprint.get(key.fingerprint())
            return self._requests.get(seq) if seq is not None else None

    def request(self, seq: int) -> PermissionRequest | None:
        with self._lock:
            return self._requests.get(seq)

    def unanswered(self) -> list[PermissionRequest]:
        with self._lock:
            return [r for seq, r in sorted(self._requests.items()) if seq not in self._answered]

    def files(self, seq: int) -> tuple[str, ...]:
        """The package cache paths a ``packages`` request lists, sorted; empty for others."""
        request = self.request(seq)
        return request.files if request is not None else ()

    def withdrawn(self, seq: int) -> bool:
        """Whether the worker withdrew ``packages`` request ``seq``: nothing grants it again."""
        with self._lock:
            return seq in self._withdrawn

    def answer_to(self, seq: int) -> Answer | None:
        """The newest answer to request ``seq`` itself, expired or not: a later answer
        replaces an earlier one. An answer whose target is not the request's key does not
        count."""
        with self._lock:
            request = self._requests.get(seq)
            matching = [
                answer
                for answer in self._answers
                if request is not None and answer.request_seq == seq and answer.key == request.key
            ]
        return max(matching, key=lambda answer: answer.seq) if matching else None

    def packages_requests(self, operations: Iterable[str]) -> list[int]:
        """The ``packages`` requests of these provisioning operations not yet withdrawn."""
        with self._lock:
            return grants.open_requests(self._requests, self._withdrawn, operations)

    def granted(
        self, sandbox_id: str, policy_digest: str, path: str, now: datetime
    ) -> Answer | None:
        """The approval that lets ``sandbox_id`` download the package file at ``path`` under
        ``policy_digest``, if any (``grants.granted``)."""
        with self._lock:
            return grants.granted(
                self._requests, self._answers, self._withdrawn, sandbox_id, policy_digest, path, now
            )
