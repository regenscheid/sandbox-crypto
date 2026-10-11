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
  marked ``withdrawn``), and nothing can grant its files again.

Records are read through a small protocol, so this module needs no event store.
"""

from __future__ import annotations

import hashlib
import json
import threading
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final, Literal, Protocol, cast

ESCALATION: Final = "escalation"
APPROVAL: Final = "approval"
DENIAL: Final = "denial"
KINDS: Final = (ESCALATION, APPROVAL, DENIAL)

KeyKind = Literal["http", "mcp", "packages"]
KEY_FIELDS: Final[dict[str, tuple[str, ...]]] = {
    "http": ("sandbox_id", "policy_digest", "method", "host", "port", "path"),
    "mcp": ("project_id", "caller", "policy_digest", "server", "method", "tool", "arguments"),
    "packages": ("sandbox_id", "policy_digest", "operation", "files"),
}
SCOPES: Final[dict[str, dict[str, tuple[str, ...]]]] = {
    "http": {"request": (), "host": ("path",)},
    "mcp": {"call": (), "tool": ("arguments",)},
    "packages": {"files": ()},
}
"""For each kind, each scope and the key fields it leaves out."""
ALL_SCOPES: Final = tuple(dict.fromkeys(scope for scopes in SCOPES.values() for scope in scopes))
DEFAULT_SCOPES: Final[dict[str, str]] = {"http": "request", "mcp": "call", "packages": "files"}
MAX_GRANT_FILES: Final = 2000
"""Package files one ``packages`` request may list."""
MAX_GRANT_PATH: Final = 2048
LISTED_FILES: Final = 20
"""Files of a ``packages`` request that ``pending`` lists before it counts the rest;
``pending --request`` lists them all."""
WITHDRAWN: Final = "withdrawn"
"""Marks the denial with which the worker closes a ``packages`` request for good."""


def files_digest(paths: Iterable[str]) -> str:
    """The digest a ``packages`` key holds for a set of package cache paths."""
    canonical = json.dumps(sorted(set(paths)), separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def grant_paths(value: object) -> tuple[str, ...] | None:
    """The paths of a ``packages`` request's ``files``, sorted; None if they are not a list
    of objects with an absolute ``path`` each, within the limits."""
    if not isinstance(value, list):
        return None
    items = cast(list[object], value)
    if not 0 < len(items) <= MAX_GRANT_FILES:
        return None
    paths: list[str] = []
    for item in items:
        path = cast(dict[str, object], item).get("path") if isinstance(item, dict) else None
        if not isinstance(path, str) or not path.startswith("/") or len(path) > MAX_GRANT_PATH:
            return None
        paths.append(path)
    return tuple(sorted(set(paths)))


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
class RequestKey:
    """What a permission request asks for, as its answers' scopes compare it."""

    kind: KeyKind
    values: tuple[str | int, ...]

    @classmethod
    def http(
        cls, sandbox_id: str, policy_digest: str, method: str, host: str, port: int, path: str
    ) -> RequestKey:
        return cls("http", (sandbox_id, policy_digest, method, host, port, path))

    @classmethod
    def mcp(
        cls,
        project_id: str,
        caller: str,
        policy_digest: str,
        server: str,
        method: str,
        tool: str,
        arguments: str,
    ) -> RequestKey:
        """``arguments`` is a digest of the call's arguments, never the arguments."""
        return cls("mcp", (project_id, caller, policy_digest, server, method, tool, arguments))

    @classmethod
    def packages(
        cls, sandbox_id: str, policy_digest: str, operation: str, paths: Iterable[str]
    ) -> RequestKey:
        """A provisioning operation's request for an exact set of package files, which
        ``paths`` names by their paths at the package cache. Each operation asks anew, so
        an answer to one never decides another."""
        return cls("packages", (sandbox_id, policy_digest, operation, files_digest(paths)))

    def fields(self) -> dict[str, str | int]:
        return dict(zip(KEY_FIELDS[self.kind], self.values, strict=True))

    def fingerprint(self) -> str:
        """The request's identity: the same fingerprint means a retry of the same request."""
        parts = [self.kind, *self.values]
        return hashlib.sha256(json.dumps(parts, separators=(",", ":")).encode()).hexdigest()

    def covers(self, other: RequestKey, scope: str) -> bool:
        """Whether an answer to this request, with ``scope``, applies to ``other``."""
        ignored = SCOPES[self.kind].get(scope)
        if self.kind != other.kind or ignored is None:
            return False
        mine, theirs = self.fields(), other.fields()
        return all(mine[name] == theirs[name] for name in mine if name not in ignored)

    def payload(self) -> dict[str, object]:
        return {"kind": self.kind, **self.fields()}

    @classmethod
    def from_payload(cls, data: Mapping[str, object]) -> RequestKey | None:
        """The key in a record; one without ``kind`` is an ``http`` key (the first format)."""
        kind = data.get("kind", "http")
        if kind not in KEY_FIELDS:
            return None
        values: list[str | int] = []
        for name in KEY_FIELDS[str(kind)]:
            value = data.get(name)
            if name == "port":
                if not isinstance(value, int) or isinstance(value, bool):
                    return None
            elif not isinstance(value, str):
                return None
            values.append(value)
        return cls(cast(KeyKind, kind), tuple(values))


def check_scope(kind: str, scope: str) -> str | None:
    """Why ``scope`` cannot answer a request of ``kind``, or None if it can."""
    scopes = SCOPES.get(kind, {})
    if scope in scopes:
        return None
    return f"a {kind} request takes the scope {' or '.join(scopes)}, not {scope!r}"


@dataclass(frozen=True, slots=True)
class PendingRequest:
    seq: int
    key: RequestKey
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
    key: RequestKey
    scope: str
    expires: datetime | None

    def applies(self, key: RequestKey, now: datetime) -> bool:
        live = self.expires is None or now < self.expires
        return live and self.key.covers(key, self.scope)


def answer_payload(
    request: PendingRequest, scope: str, expires: datetime | None
) -> dict[str, object]:
    """The payload of an approval or a denial of ``request``."""
    return {
        "request_seq": request.seq,
        "scope": scope,
        "target": request.key.payload(),
        "expires": expires.isoformat(timespec="seconds") if expires is not None else None,
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
            files = approvals.files(request.seq)
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
    files = approvals.files(seq)
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

    The store is the only truth: records a component or the command line wrote, read in
    sequence. Malformed records are skipped, never trusted.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._last_seq = 0
        self._requests: dict[int, PendingRequest] = {}
        self._by_fingerprint: dict[str, int] = {}
        self._answers: list[Answer] = []
        self._answered: set[int] = set()
        self._files: dict[int, frozenset[str]] = {}
        """The package cache paths each ``packages`` request lists."""
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
        try:
            data = record.data()
        except ValueError:
            return
        recorded = _time(record.time)
        if recorded is None:
            return
        key = RequestKey.from_payload(_mapping(data.get("target")))
        if key is None:
            return
        if record.kind == ESCALATION:
            terms = _mapping(data.get("escalation"))
            seconds, on_timeout = terms.get("timeout_seconds"), terms.get("on_timeout")
            if not isinstance(seconds, int) or on_timeout not in ("deny", "alert"):
                return
            if key.kind == "packages":
                # The files must be the ones the key names, so a list cannot be swapped.
                paths = grant_paths(data.get("files"))
                if paths is None or files_digest(paths) != key.fields()["files"]:
                    return
                self._files[record.seq] = frozenset(paths)
            request = PendingRequest(
                record.seq, key, recorded, timedelta(seconds=seconds), str(on_timeout)
            )
            self._requests[record.seq] = request
            self._by_fingerprint.setdefault(key.fingerprint(), record.seq)
            return
        request_seq, scope = data.get("request_seq"), data.get("scope")
        if not isinstance(request_seq, int) or not isinstance(scope, str):
            return
        if check_scope(key.kind, scope) is not None:
            return
        expires = _time(data.get("expires")) if data.get("expires") is not None else None
        if data.get("expires") is not None and expires is None:
            return
        kind: Literal["approval", "denial"] = "approval" if record.kind == APPROVAL else "denial"
        self._answers.append(Answer(record.seq, request_seq, kind, key, scope, expires))
        self._answered.add(request_seq)
        request = self._requests.get(request_seq)
        if (
            kind == "denial"
            and data.get(WITHDRAWN) is True
            and request is not None
            and request.key == key
            and key.kind == "packages"
        ):
            self._withdrawn.add(request_seq)

    def answer(self, key: RequestKey, now: datetime) -> Answer | None:
        """The newest live answer that covers ``key``: a later answer replaces an earlier one."""
        with self._lock:
            matching = [answer for answer in self._answers if answer.applies(key, now)]
        if not matching:
            return None
        newest = max(answer.seq for answer in matching)
        return next(answer for answer in matching if answer.seq == newest)

    def pending(self, key: RequestKey) -> PendingRequest | None:
        """The recorded request with this key's fingerprint, if there is one."""
        with self._lock:
            seq = self._by_fingerprint.get(key.fingerprint())
            return self._requests.get(seq) if seq is not None else None

    def request(self, seq: int) -> PendingRequest | None:
        with self._lock:
            return self._requests.get(seq)

    def unanswered(self) -> list[PendingRequest]:
        with self._lock:
            return [r for seq, r in sorted(self._requests.items()) if seq not in self._answered]

    def files(self, seq: int) -> tuple[str, ...]:
        """The package cache paths a ``packages`` request lists, sorted; empty for others."""
        with self._lock:
            return tuple(sorted(self._files.get(seq, ())))

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
        nothing, whatever answers it later. Among the requests that list the file, the
        newest live approval is returned.
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
                    or path not in self._files.get(answer.request_seq, frozenset())
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


def _mapping(value: object) -> Mapping[str, object]:
    return cast(Mapping[str, object], value) if isinstance(value, dict) else {}
