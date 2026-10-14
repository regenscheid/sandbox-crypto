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
    AwareDatetime,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    StringConstraints,
    model_validator,
)

from .keys import RequestKey, check_scope, files_digest

MAX_GRANT_FILES: Final = 2000
"""Package files one ``packages`` request may list."""
MAX_GRANT_PATH: Final = 2048
CONTEXT_TEXT: Final = 300
"""Characters of a ``packages`` request's context (what was asked, and why) kept to show."""


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


Key = Annotated[RequestKey, BeforeValidator(_key)]
Shown = Annotated[str | None, BeforeValidator(_shown)]
GrantPath = Annotated[str, StringConstraints(pattern=r"^/", max_length=MAX_GRANT_PATH)]


class _Payload(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True, arbitrary_types_allowed=True)


class Terms(_Payload):
    """How long a request waits for an answer, and what happens after."""

    timeout_seconds: Annotated[int, Field(strict=True)]
    on_timeout: Literal["deny", "alert"]


class GrantFile(_Payload):
    path: GrantPath


class RequestPayload(_Payload):
    """An ``escalation`` record's payload: the request's key and its terms, and for a
    ``packages`` request the exact file set, its own deadline and what to show."""

    target: Key
    escalation: Terms
    files: Annotated[tuple[GrantFile, ...], Field(max_length=MAX_GRANT_FILES)] | None = None
    valid_until: AwareDatetime | None = None
    asked: Shown = None
    purpose: Shown = None

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
    worker closing a ``packages`` request for good."""

    request_seq: Annotated[int, Field(strict=True)]
    scope: str
    target: Key
    expires: AwareDatetime | None = None
    withdrawn: Annotated[bool, BeforeValidator(lambda value: value is True)] = False

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
