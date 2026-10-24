# SPDX-License-Identifier: MIT
"""What a permission request asks for (its key), and the scopes an answer can take."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Final, Literal, cast

KeyKind = Literal["http", "mcp", "packages"]
KEY_FIELDS: Final[dict[str, tuple[str, ...]]] = {
    "http": ("sandbox_id", "policy_digest", "method", "host", "port", "path", "body"),
    "mcp": ("project_id", "caller", "policy_digest", "server", "method", "tool", "arguments"),
    "packages": ("sandbox_id", "policy_digest", "operation", "files"),
}
OPTIONAL_FIELDS: Final[dict[tuple[str, str], str]] = {("http", "body"): ""}
"""Key fields a record from before them may lack, and the value such a record has: an
``http`` key without ``body`` is one of a request recorded before body digests (M1)."""
SCOPES: Final[dict[str, dict[str, tuple[str, ...]]]] = {
    # ``request`` is the exact request, its body included (M1); ``host`` every path and
    # body on the host and port with that method.
    "http": {"request": (), "host": ("path", "body")},
    # No scope reaches other tools of a server: a grant of a class on a server is the
    # workspace's, which answers each call it covers on its own (D97).
    "mcp": {"call": (), "tool": ("arguments",)},
    "packages": {"files": ()},
}
"""For each kind, each scope and the key fields it leaves out."""
ALL_SCOPES: Final = tuple(dict.fromkeys(scope for scopes in SCOPES.values() for scope in scopes))
DEFAULT_SCOPES: Final[dict[str, str]] = {"http": "request", "mcp": "call", "packages": "files"}


def body_digest(body: bytes) -> str:
    """The digest an ``http`` key holds for a request's body: empty for no body (M1)."""
    return hashlib.sha256(body).hexdigest() if body else ""


def files_digest(paths: Iterable[str]) -> str:
    """The digest a ``packages`` key holds for a set of package cache paths."""
    canonical = json.dumps(sorted(set(paths)), separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class RequestKey:
    """What a permission request asks for, as its answers' scopes compare it."""

    kind: KeyKind
    values: tuple[str | int, ...]

    @classmethod
    def http(
        cls,
        sandbox_id: str,
        policy_digest: str,
        method: str,
        host: str,
        port: int,
        path: str,
        body: str = "",
    ) -> RequestKey:
        """``body`` is the request body's digest (``body_digest``), never the body."""
        return cls("http", (sandbox_id, policy_digest, method, host, port, path, body))

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
        if not isinstance(kind, str) or kind not in KEY_FIELDS:
            return None
        values: list[str | int] = []
        for name in KEY_FIELDS[str(kind)]:
            value = data.get(name, OPTIONAL_FIELDS.get((kind, name)))
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
