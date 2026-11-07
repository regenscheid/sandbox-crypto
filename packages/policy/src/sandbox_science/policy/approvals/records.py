# SPDX-License-Identifier: MIT
"""The payloads of permission request records and their answers, as typed models.

Components and the command line write these records to the event store (D76); every
reader parses them here, so a malformed record fails to parse and is skipped, never
trusted. A payload carries other facts too (the request, the policy, the rule); these
models read only what an answer depends on, and ignore the rest.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Annotated, Final, Literal, Self, cast

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    StringConstraints,
    model_validator,
)

from .keys import DEFAULT_SCOPES, RequestKey, check_scope, files_digest

MAX_GRANT_FILES: Final = 2000
"""Package files one ``packages`` request may list."""
MAX_GRANT_PATH: Final = 2048
CONTEXT_TEXT: Final = 300
"""Characters of a ``packages`` request's context (what was asked, and why) kept to show."""
WITHDRAWN: Final = "withdrawn"
"""Marks the denial with which the worker closes a request for good: a ``packages``
request as its operation ends, or an answered ``http`` or ``mcp`` request whose grant the
user revoked, whose approvals it ends."""
MAX_ANSWER_GRANTS: Final = 16
"""Grants one answer may name as its source."""
VALID_UNTIL: Final = "valid_until"
"""A ``packages`` request's own deadline: it grants nothing after it."""
MAX_ANSWERER: Final = 255
"""The longest name or ``sub`` an answer's user is shown with."""


@dataclass(frozen=True, slots=True)
class AnsweredBy:
    """The user an answer's record names (``by``), as the workspace relayed them: their
    name in its records and, for a login through Authentik, the token's ``sub``. Shown to
    an operator only: nothing an answer decides depends on it."""

    user: str
    sub: str | None = None

    def payload(self) -> dict[str, object]:
        found: dict[str, object] = {"kind": "user", "user": self.user}
        if self.sub is not None:
            found["sub"] = self.sub
        return found


def _key(value: object) -> RequestKey:
    if isinstance(value, RequestKey):
        return value
    key = (
        RequestKey.from_payload(cast(dict[str, object], value)) if isinstance(value, dict) else None
    )
    if key is None:
        raise ValueError("not a permission request key")
    return key


def _shown(value: object) -> str | None:
    """Text a request carries for people, safe to print: no control characters, bounded.
    Anything else is not shown, and does not make the record unreadable."""
    if not isinstance(value, str) or not value.strip():
        return None
    text = "".join(char if char.isprintable() else " " for char in value[: CONTEXT_TEXT * 2])
    text = " ".join(text.split())
    return text if len(text) <= CONTEXT_TEXT else text[: CONTEXT_TEXT - 1] + "…"


def _time(value: object) -> datetime:
    """An ISO 8601 text with a timezone, as ``datetime.fromisoformat`` reads it: no
    timestamps, no other formats."""
    if not isinstance(value, str):
        raise ValueError("a time is an ISO 8601 text")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("a time needs its timezone")
    return parsed


def _integer(value: object) -> int:
    """A JSON integer; ``true`` and ``false`` count as 1 and 0, as they always have."""
    if not isinstance(value, int):
        raise ValueError("not an integer")
    return int(value)


def _answerer_text(value: object) -> str | None:
    if not isinstance(value, str) or not 0 < len(value) <= MAX_ANSWERER:
        return None
    return value if value.isprintable() else None


def _answerer(value: object) -> AnsweredBy | None:
    """An answer's ``by``; anything malformed is not shown, and does not make the answer
    unreadable."""
    if isinstance(value, AnsweredBy) or value is None:
        return value
    if not isinstance(value, dict):
        return None
    fields = cast(dict[str, object], value)
    user = _answerer_text(fields.get("user"))
    if fields.get("kind") != "user" or user is None:
        return None
    return AnsweredBy(user, _answerer_text(fields.get("sub")))


MAX_TIMEOUT_SECONDS: Final = 10**12
"""Bounds an escalation timeout so that it fits a ``timedelta``."""
Key = Annotated[RequestKey, BeforeValidator(_key)]
Time = Annotated[datetime, BeforeValidator(_time)]
Integer = Annotated[int, BeforeValidator(_integer)]
Shown = Annotated[str | None, BeforeValidator(_shown)]
GrantPath = Annotated[str, StringConstraints(pattern=r"^/", max_length=MAX_GRANT_PATH)]
GrantName = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9._-]{1,64}$")]
"""A workspace grant's id, as an answer names it."""

_PACKAGES_ONLY: Final = frozenset({"files", "valid_until", "asked", "purpose"})


class _Payload(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True, arbitrary_types_allowed=True)


class Terms(_Payload):
    """How long a request waits for an answer, and what happens after."""

    timeout_seconds: Annotated[Integer, Field(ge=-MAX_TIMEOUT_SECONDS, le=MAX_TIMEOUT_SECONDS)]
    on_timeout: Literal["deny", "alert"]


class GrantFile(_Payload):
    path: GrantPath


class RequestPayload(_Payload):
    """An ``escalation`` record's payload: the request's key and its terms, and for a
    ``packages`` request the exact file set, its own deadline and what to show."""

    target: Key
    escalation: Terms
    files: Annotated[tuple[GrantFile, ...], Field(max_length=MAX_GRANT_FILES)] | None = None
    valid_until: Time | None = None
    asked: Shown = None
    purpose: Shown = None

    @model_validator(mode="before")
    @classmethod
    def _packages_only(cls, data: object) -> object:
        # Only a packages request has a file set, a deadline and words to show: another
        # kind's record is neither refused nor shown for them.
        if not isinstance(data, dict):
            return data
        fields = cast(dict[str, object], data)
        target = fields.get("target")
        kind = cast(dict[str, object], target).get("kind") if isinstance(target, dict) else None
        if kind == "packages":
            return fields
        return {name: value for name, value in fields.items() if name not in _PACKAGES_ONLY}

    @model_validator(mode="after")
    def _file_set(self) -> Self:
        if self.target.kind == "packages":
            # The files must be the ones the key names, so a list cannot be swapped, and
            # every file set is granted for a bounded time.
            if not self.files or self.valid_until is None:
                raise ValueError("a packages request needs its files and its deadline")
            if files_digest(self.paths()) != self.target.fields()["files"]:
                raise ValueError("the files are not the ones the key names")
        return self

    def paths(self) -> tuple[str, ...]:
        return tuple(sorted({file.path for file in self.files or ()}))


class AnswerPayload(_Payload):
    """An ``approval`` or ``denial`` record's payload. A denial marked ``withdrawn`` is the
    worker closing a request for good. ``grants`` names the workspace's grants an answer
    comes from (D97), so that revoking one can end it, and ``by`` the user who answered or
    revoked the grant."""

    request_seq: Integer
    scope: str
    target: Key
    expires: Time | None = None
    withdrawn: Annotated[bool, BeforeValidator(lambda value: value is True)] = False
    grants: Annotated[tuple[GrantName, ...], Field(max_length=MAX_ANSWER_GRANTS)] = ()
    by: Annotated[AnsweredBy | None, BeforeValidator(_answerer)] = None

    @model_validator(mode="after")
    def _scope(self) -> Self:
        problem = check_scope(self.target.kind, self.scope)
        if problem is not None:
            raise ValueError(problem)
        return self


@dataclass(frozen=True, slots=True)
class PermissionRequest:
    """A recorded permission request: the record's number and time, and its payload."""

    seq: int
    key: RequestKey
    recorded: datetime
    timeout: timedelta
    on_timeout: Literal["deny", "alert"]
    """``deny``, or ``alert`` when the rule lets a read proceed unanswered."""
    files: tuple[str, ...] = ()
    """The package cache paths a ``packages`` request lists, sorted; empty for others."""
    valid_until: datetime | None = None
    """A ``packages`` request's own deadline: it grants nothing after it."""
    asked: str | None = None
    purpose: str | None = None
    """What a ``packages`` request says was asked for, and why (the agent's words)."""

    @classmethod
    def from_record(cls, seq: int, recorded: datetime, payload: RequestPayload) -> Self:
        return cls(
            seq,
            payload.target,
            recorded,
            timedelta(seconds=payload.escalation.timeout_seconds),
            payload.escalation.on_timeout,
            payload.paths(),
            payload.valid_until,
            payload.asked,
            payload.purpose,
        )

    def expired(self, now: datetime) -> bool:
        return now - self.recorded >= self.timeout


@dataclass(frozen=True, slots=True)
class Answer:
    """A recorded approval or denial of request ``request_seq``."""

    seq: int
    request_seq: int
    kind: Literal["approval", "denial"]
    key: RequestKey
    scope: str
    expires: datetime | None
    grants: tuple[str, ...] = ()
    """The workspace's grants the answer comes from."""
    by: AnsweredBy | None = None
    """The user who answered, when the workspace named one."""

    def live(self, now: datetime) -> bool:
        return self.expires is None or now < self.expires

    def applies(self, key: RequestKey, now: datetime) -> bool:
        return self.live(now) and self.key.covers(key, self.scope)


def answer_payload(
    request: PermissionRequest,
    scope: str,
    expires: datetime | None,
    grants: tuple[str, ...] = (),
    by: AnsweredBy | None = None,
) -> dict[str, object]:
    """The payload of an approval or a denial of ``request``, naming the grants it comes
    from and the user who gave it, if any."""
    payload: dict[str, object] = {
        "request_seq": request.seq,
        "scope": scope,
        "target": request.key.payload(),
        "expires": expires.isoformat(timespec="seconds") if expires is not None else None,
    }
    if grants:
        payload["grants"] = list(grants)
    if by is not None:
        payload["by"] = by.payload()
    return payload


def withdrawal_payload(
    seq: int, key: RequestKey, reason: str, by: AnsweredBy | None = None
) -> dict[str, object]:
    """The payload of the denial with which the worker closes request ``seq`` for good,
    built from its key alone, so that no read of the store is needed, naming the user who
    revoked its grant, if one did."""
    payload: dict[str, object] = {
        "request_seq": seq,
        "scope": DEFAULT_SCOPES[key.kind],
        "target": key.payload(),
        "expires": None,
        "reason": reason[:500],
        WITHDRAWN: True,
    }
    if by is not None:
        payload["by"] = by.payload()
    return payload
