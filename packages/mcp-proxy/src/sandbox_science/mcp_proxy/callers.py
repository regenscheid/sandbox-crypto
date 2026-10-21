# SPDX-License-Identifier: MIT
"""Who is calling, and how far the proxy can vouch for it (D67, "Caller identity and attribution").

Each caller presents a bearer token from the settings, which names its kind
and project. A token in a sandbox identifies the sandbox, not the process
that sent the request, so attribution has levels:

- ``orchestrator``: the harness's own calls. The orchestrator knows which
  session's agent made a call and names it in ``Sandbox-Science-Session``;
  the label is as trustworthy as the orchestrator (zone Z2).
- ``sandbox``: worker code in a sandbox dedicated to one session, whose token
  names that session.
- ``container``: worker code in a shared project container. Any process there
  can use the token, so a session label it sends is recorded as a claim only.

An ``operator`` caller (the workspace, for its user) makes no MCP calls: it reads its
project's permission requests and answers them, as the command line does (D76).
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Final, Literal

from .settings import CallerSettings

SESSION_HEADER: Final = "Sandbox-Science-Session"
"""The header in which a caller names the session a call belongs to."""

_LABEL_RE: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
_MAX_TOKEN: Final = 4096

CallerKind = Literal["orchestrator", "worker", "operator"]
Level = Literal["orchestrator", "sandbox", "container"]


@dataclass(frozen=True, slots=True)
class Caller:
    """An authenticated caller, from the settings."""

    name: str
    kind: CallerKind
    project: str
    container: str | None = None
    session: str | None = None

    @property
    def origin(self) -> Literal["orchestrator", "worker"]:
        """Where a caller's MCP calls come from, for the policy; an operator makes none."""
        if self.kind == "operator":
            raise ValueError("an operator caller makes no MCP calls")
        return self.kind

    @property
    def level(self) -> Level:
        """The attribution level of this caller's calls."""
        if self.kind == "orchestrator":
            return "orchestrator"
        return "sandbox" if self.session is not None else "container"


@dataclass(frozen=True, slots=True)
class Attribution:
    """Whom a call is attributed to. Only ``session`` may drive session-scoped decisions."""

    caller: Caller
    session: str | None
    session_claim: str | None

    def record(self) -> dict[str, object]:
        """The attribution as recorded with every event."""
        return {
            "level": self.caller.level,
            "project": self.caller.project,
            "caller": self.caller.name,
            "kind": self.caller.kind,
            "container": self.caller.container,
            "session": self.session,
            "session_claim": self.session_claim,
        }


class Callers:
    """Callers by the SHA-256 of their bearer tokens."""

    def __init__(self, entries: Iterable[tuple[str, Caller]]) -> None:
        self._by_digest: Mapping[str, Caller] = dict(entries)

    @classmethod
    def from_settings(cls, callers: Iterable[CallerSettings]) -> Callers:
        """Build the table from the settings' ``[[callers]]``."""
        return cls(
            (
                entry.token_sha256,
                Caller(entry.name, entry.kind, entry.project, entry.container, entry.session),
            )
            for entry in callers
        )

    def authenticate(self, authorization: str | None) -> Caller | None:
        """Return the caller an ``Authorization: Bearer`` header names, or None."""
        if authorization is None:
            return None
        scheme, _, token = authorization.partition(" ")
        if scheme.lower() != "bearer" or not token or len(token) > _MAX_TOKEN:
            return None
        # A lookup by the token's hash compares no secret directly; tokens are
        # random, so the hash's prefix tells an observer nothing.
        return self._by_digest.get(token_digest(token))


def token_digest(token: str) -> str:
    """The ``token_sha256`` value for a token."""
    return hashlib.sha256(token.encode()).hexdigest()


def attribute(caller: Caller, labels: Sequence[str]) -> Attribution:
    """Attribute a call, given the values of its session headers.

    Raises ValueError for a repeated or malformed label.
    """
    if len(labels) > 1:
        raise ValueError(f"{SESSION_HEADER} given more than once")
    label = labels[0] if labels else None
    if label is not None and not _LABEL_RE.fullmatch(label):
        raise ValueError(f"{SESSION_HEADER} must be a session id")
    match caller.level:
        case "orchestrator":
            return Attribution(caller, session=label, session_claim=None)
        case "sandbox":
            claim = label if label is not None and label != caller.session else None
            return Attribution(caller, session=caller.session, session_claim=claim)
        case "container":
            return Attribution(caller, session=None, session_claim=label)
