# SPDX-License-Identifier: MIT
"""A task brief's narrowing of the child's connectors (delegated agents spec DA-6, DA-2).

A parent may name the connectors a task child gets (``connectors`` in its brief), each
entry one of:

- ``SERVER``: every tool of a configured server;
- ``SERVER/BEHIND``: every tool of a server behind a portal, as the index names it
  (``crypto-research-mcp/e-print-mcp``); a bare ``BEHIND`` that the parent's index names
  under one configured server is written so;
- ``SERVER/TOOL``: one tool by its full name;
- ``SERVER/PATTERN``: the server's tools whose names match a shell-style pattern
  (``*``, ``?`` and ``[...]``), such as ``crypto-research-mcp/e-print-mcp_search_*``.

A narrowing only narrows. The child's connectors are its default, which is its parent's
(the project's servers the user did not turn off, and for a check only the tools classed
``read``, D99), less what each narrowing above it leaves out: a tool is offered only when
every level admits it, the brief's own and each one its parent was given. At delegation a
server no configured connector names is refused (``invalid_request``), and so is an entry
that admits nothing the parent is offered (``not_permitted_for_role``): a brief that
names it would ask for more than the parent has. The brief records the entries as
normalized and the narrowings above them (``inherited_connectors``), so a resumed or
reopened child gets the same tools.

Agent text never widens anything here: entries only remove tools from a set the trusted
role factories chose (DA-2).
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from fnmatch import fnmatchcase
from typing import TYPE_CHECKING, Final, cast

from pydantic import JsonValue

from sandbox_science.contracts import ErrorKind
from sandbox_science.orchestrator.worker_client import WorkerAPIError

if TYPE_CHECKING:
    from sandbox_science.orchestrator.connectors import Connectors

MAX_ENTRIES: Final = 32
"""Entries a brief's ``connectors`` may name."""
MAX_ENTRY: Final = 200
"""Characters of one entry."""
MAX_LEVELS: Final = 4
"""Narrowings a child inherits at most: delegation is two levels deep (PD-15)."""
ENTRY: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}(/[A-Za-z0-9._*?\[\]!-]{1,128})?")
"""A server, then optionally a slash and a tool, a server behind it, or a pattern."""
_GLOB: Final = re.compile(r"[*?\[]")


def _admits(entry: str, server: str, name: str, group: str) -> bool:
    """Whether ``entry`` admits the tool ``name`` of the configured ``server``, listed
    under the index key ``group``."""
    named, _, rest = entry.partition("/")
    if named != server:
        return False
    if not rest:
        return True
    if _GLOB.search(rest):
        return fnmatchcase(name, rest)
    return group == entry or name == rest


@dataclass(frozen=True)
class Narrowing:
    """The narrowings a child's connectors are under, outermost first: a tool is offered
    only when every level admits it."""

    levels: tuple[tuple[str, ...], ...]

    def admits(self, server: str, name: str, group: str) -> bool:
        """Whether every level admits the tool ``name`` of ``server`` (index key ``group``)."""
        return all(
            any(_admits(entry, server, name, group) for entry in level) for level in self.levels
        )

    def admits_server(self, server: str) -> bool:
        """Whether every level names some tool of ``server``: one no level names is not
        listed for the child at all."""
        return all(
            any(entry.partition("/")[0] == server for entry in level) for level in self.levels
        )

    def recorded(self) -> list[JsonValue]:
        """The levels as a brief records them."""
        return [list[JsonValue](level) for level in self.levels]

    @property
    def described(self) -> str:
        """The innermost level, as the child's index says it."""
        return ", ".join(self.levels[-1]) if self.levels and self.levels[-1] else "none"


def of_brief(brief: Mapping[str, JsonValue]) -> Narrowing | None:
    """The narrowing a delegation's recorded brief puts its child under; ``None`` when
    neither it nor any level above it narrows. A malformed record narrows to nothing,
    failing closed."""
    levels: list[tuple[str, ...]] = []
    inherited = brief.get("inherited_connectors")
    if inherited is not None:
        if not isinstance(inherited, list):
            return Narrowing(((),))
        for level in cast(list[object], inherited)[:MAX_LEVELS]:
            if not isinstance(level, list):
                return Narrowing(((),))
            levels.append(tuple(str(entry) for entry in cast(list[object], level)))
    own = brief.get("connectors")
    if own is not None:
        if not isinstance(own, list):
            return Narrowing(((),))
        levels.append(tuple(str(entry) for entry in cast(list[object], own)))
    return Narrowing(tuple(levels)) if levels else None


def _unknown(entry: str, configured: Sequence[str]) -> WorkerAPIError:
    named = ", ".join(configured) if configured else "none"
    return WorkerAPIError(
        ErrorKind.INVALID_REQUEST,
        f"unknown_connector: {entry!r} names no configured connector server "
        f"(configured: {named}); name a server, SERVER/BEHIND as mcp_tools' index shows "
        "it, SERVER/TOOL or SERVER/PATTERN",
    )


def _widening(entry: str, why: str) -> WorkerAPIError:
    return WorkerAPIError(
        ErrorKind.POLICY_DENIED,
        f"not_permitted_for_role: {entry!r} {why}; a brief only narrows a child's "
        "connectors to some of those you are offered, never more",
    )


def narrow(
    entries: Sequence[str], *, configured: Sequence[str], offered: Connectors | None
) -> tuple[str, ...]:
    """A brief's ``connectors`` checked against the parent's own and normalized, as the
    delegation records them. Refuses an entry that is malformed or names no configured
    server, and one that admits nothing the parent is ``offered``."""
    if len(entries) > MAX_ENTRIES:
        raise WorkerAPIError(
            ErrorKind.INVALID_REQUEST, f"a brief names at most {MAX_ENTRIES} connectors"
        )
    groups: dict[str, set[str]] = {}
    if offered is not None:
        for tool in offered.tools.values():
            owner, _, under = tool.group.partition("/")
            if under:
                groups.setdefault(under, set()).add(owner)
    found: list[str] = []
    for raw in entries:
        entry = raw.strip()
        if len(entry) > MAX_ENTRY or not ENTRY.fullmatch(entry):
            raise WorkerAPIError(
                ErrorKind.INVALID_REQUEST,
                f"{raw[:MAX_ENTRY]!r} is not a connector: a server, SERVER/BEHIND, "
                "SERVER/TOOL or SERVER/PATTERN",
            )
        server = entry.partition("/")[0]
        if server not in configured:
            owners = groups.get(entry, set[str]()) if "/" not in entry else set[str]()
            if len(owners) != 1:
                raise _unknown(entry, configured)
            server = next(iter(owners))
            entry = f"{server}/{entry}"
        if offered is None or server not in offered.servers:
            raise _widening(entry, "names a connector server you are not offered")
        # Where its listing came, an entry past the server must admit a tool the parent has.
        if (
            entry != server
            and server not in offered.failed
            and not any(
                tool.server == server and _admits(entry, server, name, tool.group)
                for name, tool in offered.tools.items()
            )
        ):
            raise _widening(entry, "names no connector tool you are offered")
        found.append(entry)
    return tuple(dict.fromkeys(found))
