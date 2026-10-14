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
        if not isinstance(kind, str) or kind not in KEY_FIELDS:
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
