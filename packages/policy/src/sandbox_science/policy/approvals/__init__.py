# SPDX-License-Identifier: MIT
"""Permission requests and their answers (D76), for the policy middleware and the MCP proxy.

An escalated operation is a permission request. Its first occurrence is recorded in the
event store as an ``escalation`` whose payload carries a ``target``: the request's key.
Retries of the same request share the key's fingerprint and are not recorded again. An
operator answers with an ``approval`` or a ``denial`` record, which the component
honours on the next retry until the answer expires. Unanswered past the rule's
escalation timeout, a request follows ``on_timeout``.

Keys come in three kinds, each with the scopes an answer can take:
- ``http`` (the middleware): sandbox, policy revision, method, host, port and path.
  ``request`` covers the request itself; ``host`` every path on that host and port
  with that method.
- ``mcp`` (the MCP proxy): project, caller, policy revision, server, method, tool and a
  digest of the arguments. ``call`` covers the call itself; ``tool`` the tool with any
  arguments.
- ``packages`` (the worker's provisioning operations, Conda provisioning CP-8): sandbox,
  policy revision, operation and a digest of an exact set of package files, which the
  request's record lists by their paths at the package cache. ``files`` covers exactly
  those files: the middleware allows their downloads to that sandbox under that revision,
  and nothing else. When its operation ends, the worker withdraws the request (a denial
  marked ``withdrawn``), and nothing can grant its files again. The request also names
  the time after which it grants nothing (``valid_until``), whatever its answers say, so
  a withdrawal the worker failed to record still ends the grant.

Records are read through a small protocol, so this module needs no event store.
"""

from __future__ import annotations

import threading
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final, Literal, Protocol

from pydantic import ValidationError

from .keys import ALL_SCOPES, DEFAULT_SCOPES, RequestKey, check_scope, files_digest
from .records import AnswerPayload, PermissionRequest, RequestPayload

__all__ = [
    "ALL_SCOPES",
    "APPROVAL",
    "DEFAULT_SCOPES",
    "DENIAL",
    "ESCALATION",
    "KINDS",
    "VALID_UNTIL",
    "WITHDRAWN",
    "Answer",
    "Approvals",
    "PermissionRequest",
    "RecordLike",
    "RequestKey",
    "answer_payload",
    "check_scope",
    "describe",
    "files_digest",
    "pending_lines",
    "prepare_answer",
    "request_lines",
    "withdrawal_payload",
]

ESCALATION: Final = "escalation"
APPROVAL: Final = "approval"
DENIAL: Final = "denial"
KINDS: Final = (ESCALATION, APPROVAL, DENIAL)

LISTED_FILES: Final = 20
"""Files of a ``packages`` request that ``pending`` lists before it counts the rest;
``pending --request`` lists them all."""
WITHDRAWN: Final = "withdrawn"
"""Marks the denial with which the worker closes a ``packages`` request for good."""
VALID_UNTIL: Final = "valid_until"
"""A ``packages`` request's own deadline: it grants nothing after it."""


class RecordLike(Protocol):
    """What this module reads from an event store record."""

    @property
    def seq(self) -> int: ...

    @property
    def time(self) -> str: ...

    @property
    def kind(self) -> str: ...

    def data(self) -> dict[str, object]: ...


@dataclass(frozen=True, slots=True)
class Answer:
    seq: int
    request_seq: int
    kind: Literal["approval", "denial"]
    key: RequestKey
    scope: str
    expires: datetime | None

    def applies(self, key: RequestKey, now: datetime) -> bool:
        live = self.expires is None or now < self.expires
        return live and self.key.covers(key, self.scope)


def answer_payload(
    request: PermissionRequest, scope: str, expires: datetime | None
) -> dict[str, object]:
    """The payload of an approval or a denial of ``request``."""
    return {
        "request_seq": request.seq,
        "scope": scope,
        "target": request.key.payload(),
        "expires": expires.isoformat(timespec="seconds") if expires is not None else None,
    }


def withdrawal_payload(seq: int, key: RequestKey, reason: str) -> dict[str, object]:
    """The payload of the denial with which the worker closes ``packages`` request ``seq``
    for good, built from its key alone, so that no read of the store is needed."""
    return {
        "request_seq": seq,
        "scope": DEFAULT_SCOPES[key.kind],
        "target": key.payload(),
        "expires": None,
        "reason": reason[:500],
        WITHDRAWN: True,
    }


def describe(key: RequestKey) -> str:
    """A permission request's key on one line, for an operator."""
    fields = key.fields()
    if key.kind == "http":
        where = f"{fields['host']}:{fields['port']}{fields['path']}"
        return f"{fields['sandbox_id']}  {fields['method']} {where}"
    if key.kind == "packages":
        return f"{fields['sandbox_id']}  install package files for {fields['operation']}"
    return f"{fields['caller']}  {fields['server']} {fields['method']} {fields['tool']}"


def pending_lines(approvals: Approvals) -> list[str]:
    """One line per request nobody answered, for ``pending`` commands, and below a
    ``packages`` request the files it would let the sandbox download."""
    lines: list[str] = []
    for request in approvals.unanswered():
        lines.append(
            f"{request.seq}  {request.recorded:%Y-%m-%d %H:%M:%S}  {describe(request.key)}  "
            f"(after {request.timeout.total_seconds() / 60:.0f} min: {request.on_timeout})"
        )
        if request.key.kind == "packages":
            files = request.files
            lines += [f"      {path}" for path in files[:LISTED_FILES]]
            if len(files) > LISTED_FILES:
                lines.append(
                    f"      … {len(files) - LISTED_FILES} more: pending --request {request.seq} "
                    "lists them all"
                )
    return lines


def request_lines(approvals: Approvals, seq: int) -> list[str] | None:
    """Request ``seq`` in full, for ``pending --request``: what it asks, how it stands,
    and every file of a ``packages`` request. None if there is no such request."""
    request = approvals.request(seq)
    if request is None:
        return None
    answer = approvals.answer_to(seq)
    if approvals.withdrawn(seq):
        state = "withdrawn: its operation has ended"
    elif answer is None:
        state = f"waiting (after {request.timeout.total_seconds() / 60:.0f} min: "
        state += f"{request.on_timeout})"
    else:
        state = f"{'approved' if answer.kind == APPROVAL else 'denied'} by record {answer.seq}"
        if answer.expires is not None:
            state += f", until {answer.expires:%Y-%m-%d %H:%M:%S}"
    lines = [f"{request.seq}  {request.recorded:%Y-%m-%d %H:%M:%S}  {describe(request.key)}"]
    lines.append(f"      {state}")
    asked, purpose, until = request.asked, request.purpose, request.valid_until
    if asked is not None:
        lines.append(f"      asked for: {asked}")
    if purpose is not None:
        lines.append(f"      purpose (the agent's words): {purpose}")
    if until is not None:
        lines.append(f"      grants nothing after {until:%Y-%m-%d %H:%M:%S}")
    files = request.files
    if files:
        lines.append(f"      {len(files)} package files:")
        lines += [f"      {path}" for path in files]
    return lines


MAX_APPROVAL_HOURS: Final = 24 * 30


def prepare_answer(
    approvals: Approvals,
    seq: int,
    *,
    approve: bool,
    scope: str | None,
    hours: float | None,
    now: datetime,
) -> tuple[str, dict[str, object]]:
    """The kind and payload of an approval or denial of request ``seq``; ValueError if invalid.

    The scope defaults to the request itself; an approval lasts ``hours``.
    """
    request = approvals.request(seq)
    if request is None:
        raise ValueError(f"no permission request {seq}")
    if approvals.withdrawn(seq):
        raise ValueError(
            f"permission request {seq} was withdrawn: its provisioning operation has ended, "
            "and its files can no longer be granted"
        )
    scope = scope or DEFAULT_SCOPES[request.key.kind]
    problem = check_scope(request.key.kind, scope)
    if problem is not None:
        raise ValueError(problem)
    expires = None
    if approve and hours is not None:
        if not 0 < hours <= MAX_APPROVAL_HOURS:
            raise ValueError(
                f"an approval lasts more than 0 and at most {MAX_APPROVAL_HOURS} hours"
            )
        expires = now + timedelta(hours=hours)
    return (APPROVAL if approve else DENIAL), answer_payload(request, scope, expires)


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

    def packages_requests(self, operations: Iterable[str]) -> list[int]:
        """The ``packages`` requests of these provisioning operations not yet withdrawn."""
        wanted = set(operations)
        with self._lock:
            return [
                seq
                for seq, request in sorted(self._requests.items())
                if request.key.kind == "packages"
                and request.key.fields()["operation"] in wanted
                and seq not in self._withdrawn
            ]

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

    def granted(
        self, sandbox_id: str, policy_digest: str, path: str, now: datetime
    ) -> Answer | None:
        """The approval that lets ``sandbox_id`` download the package file at ``path`` under
        ``policy_digest``, if any (Conda provisioning, CP-8).

        A ``packages`` request's newest answer decides it, as for other requests: an
        approval grants the files the request lists, until it expires; a later denial
        withdraws them. A request the worker withdrew, as its operation ended, grants
        nothing, whatever answers it later, and neither does one past its own deadline.
        Among the requests that list the file, the newest live approval is returned.
        """
        with self._lock:
            newest: dict[int, Answer] = {}
            for answer in self._answers:
                request = self._requests.get(answer.request_seq)
                if (
                    answer.key.kind != "packages"
                    or request is None
                    or request.key != answer.key
                    or answer.request_seq in self._withdrawn
                    or request.valid_until is None
                    or now >= request.valid_until
                    or path not in request.files
                ):
                    continue
                fields = answer.key.fields()
                if fields["sandbox_id"] != sandbox_id or fields["policy_digest"] != policy_digest:
                    continue
                current = newest.get(answer.request_seq)
                if current is None or answer.seq > current.seq:
                    newest[answer.request_seq] = answer
        live = [
            answer
            for answer in newest.values()
            if answer.kind == "approval" and (answer.expires is None or now < answer.expires)
        ]
        return max(live, key=lambda answer: answer.seq) if live else None
