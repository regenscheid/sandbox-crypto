# SPDX-License-Identifier: MIT
"""Per-server tool classifications and the class of one MCP call (D96).

A server's classification is a set of checked-in files under ``classifications/<name>/``,
one per server behind a portal, that the policy names with ``mcp.servers.<server>.
classification``. A call's base class is the policy's own ``classify`` entry for the tool,
else the file's, else the server's declared hint from its latest listing, else
``unclassified``; then the file's argument rules for the tool may set another class for
this call. When several rules apply, the one whose class the policy treats most
restrictively wins. docs/POLICY.md, "Effect classes", has the semantics.
"""

from __future__ import annotations

import functools
import hashlib
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal

from .decision import Action, Decision, most_restrictive
from .effects import (
    BROWSE_ENABLED,
    CATALOG,
    SHIPPED,
    ClassAction,
    Classification,
    EffectClass,
    MaxGrant,
    ToolEntry,
)
from .errors import PolicyLoadError
from .loader import parse_model, read_limited
from .model import McpServer, ProjectPolicy

Source = Literal["policy", "classification", "hint", "none"]

EFFECT_META: Final = "sandbox-science/effect"
"""The ``_meta`` key under which the MCP proxy tells its callers a listed tool's class:
``read`` when every call of it is a read, otherwise a class a call can have."""


@dataclass(frozen=True, slots=True)
class ServerClassification:
    """A server's classification files, merged."""

    name: str
    files: tuple[Classification, ...]
    tools: Mapping[str, ToolEntry]
    digest: str
    """``sha256:`` over the files' names and bytes, in name order."""

    @property
    def pins(self) -> dict[str, str]:
        """The pinned tool list: each pinned tool's digest, by name."""
        return {name: entry.sha256 for name, entry in self.tools.items() if entry.sha256}


def load_classification(directory: Path, name: str) -> ServerClassification:
    """Read and merge the classification files of server ``name`` in ``directory``.

    Raises PolicyLoadError for a missing directory, an invalid file, a file for another
    server, or a tool two files classify.
    """
    paths = sorted(directory.glob("*.yaml"))
    if not paths:
        raise PolicyLoadError(f"no classification files for MCP server {name!r} in {directory}")
    digest = hashlib.sha256()
    files: list[Classification] = []
    tools: dict[str, ToolEntry] = {}
    for path in paths:
        data = read_limited(path)
        digest.update(f"{path.name}\0{len(data)}\0".encode())
        digest.update(data)
        found = parse_model(Classification, data, kind="classification", source=str(path))
        if found.server != name:
            raise PolicyLoadError(f"{path} classifies {found.server!r}, not {name!r}")
        twice = sorted(set(tools) & set(found.tools))
        if twice:
            raise PolicyLoadError(f"{path} classifies tools another file has: {', '.join(twice)}")
        tools.update(found.tools)
        files.append(found)
    return ServerClassification(name, tuple(files), tools, "sha256:" + digest.hexdigest())


@functools.cache
def catalog(name: str) -> ServerClassification:
    """The checked-in classification of server ``name``. Raises PolicyLoadError."""
    return load_classification(CATALOG / name, name)


def catalog_names() -> frozenset[str]:
    """The servers the checked-in catalog classifies."""
    return frozenset(path.name for path in CATALOG.iterdir() if path.is_dir())


def server_classification(server: McpServer) -> ServerClassification | None:
    """The catalog entry a server's ``classification`` names, if it names one."""
    return catalog(server.classification) if server.classification is not None else None


def class_action(policy: ProjectPolicy, server: str, effect: EffectClass) -> ClassAction:
    """The action and grant ceiling of ``effect`` on ``server``: the server's setting, the
    cross-server one, else the shipped default. ``browse`` follows the browser-automation
    switch; ``unclassified`` is never looser than ``defaults.action``."""
    mcp = policy.mcp
    declared = mcp.servers[server].effects if server in mcp.servers else {}
    if effect in declared:
        return declared[effect]
    if effect in mcp.effects:
        return mcp.effects[effect]
    if effect is EffectClass.BROWSE:
        return BROWSE_ENABLED if mcp.browser_automation == "enabled" else SHIPPED[effect]
    shipped = SHIPPED[effect]
    if effect is EffectClass.UNCLASSIFIED:
        action = most_restrictive((shipped.action, policy.defaults.action))
        if action is Action.DENY:
            return ClassAction(action=Action.DENY, max_grant=MaxGrant.NONE)
    return shipped


@dataclass(frozen=True, slots=True)
class CallClass:
    """The class one call resolves to, and where it came from."""

    effect: EffectClass
    base: EffectClass
    source: Source
    """Where the base class came from."""
    rule: str | None = None
    """The argument rule that set ``effect``, when one did."""

    @property
    def label(self) -> str:
        """How decisions name the class."""
        where = f"argument rule {self.rule}" if self.rule else self.source
        return f"{self.effect} ({where})"


def tool_entry(server: McpServer, name: str) -> ToolEntry | None:
    """The classification file's entry for a tool, by its name after the server's prefix."""
    found = server_classification(server)
    return found.tools.get(name) if found is not None else None


def named(server: McpServer, name: str) -> bool:
    """Whether the policy names the tool: in its classification, ``classify`` or, exactly, in
    an allow or escalate list. Under ``mcp: locked`` only named tools are available (D95)."""
    return (
        name in server.classify
        or tool_entry(server, name) is not None
        or name in server.tools.allow
        or name in server.tools.escalate
    )


def resolve(
    server: McpServer,
    name: str,
    arguments: Mapping[str, object] | None,
    restrictiveness: Callable[[EffectClass], tuple[int, int]],
    *,
    hint: EffectClass | None = None,
) -> CallClass:
    """The class of a call of tool ``name`` (after the server's prefix) with ``arguments``.

    ``restrictiveness`` ranks classes under the policy, so that of several argument rules
    that apply, the one that leads to the most restrictive treatment decides; the first
    listed wins a tie. ``hint`` is the class the server's latest listing declares for it.
    """
    entry = tool_entry(server, name)
    source: Source
    if name in server.classify:
        base, source = server.classify[name], "policy"
    elif entry is not None:
        base, source = entry.effect, "classification"
    elif hint is not None:
        base, source = hint, "hint"
    else:
        base, source = EffectClass.UNCLASSIFIED, "none"
    fired = [rule for rule in (entry.rules if entry else ()) if rule.fires(arguments or {})]
    if not fired:
        return CallClass(base, base, source)
    rule = max(fired, key=lambda rule: restrictiveness(rule.effect))  # first of the ties
    return CallClass(rule.effect, base, source, rule.label)


def possible_classes(server: McpServer, name: str) -> frozenset[EffectClass]:
    """Every class a call of the tool can resolve to, whatever its arguments."""
    entry = tool_entry(server, name)
    base = server.classify.get(name) or (entry.effect if entry else EffectClass.UNCLASSIFIED)
    return frozenset({base, *(rule.effect for rule in (entry.rules if entry else ()))})


def classified_names(server: McpServer) -> frozenset[str]:
    """Every tool the server's classification and ``classify`` entries name."""
    found = server_classification(server)
    return frozenset(server.classify) | (frozenset(found.tools) if found else frozenset[str]())


def listed_effect(server: McpServer, tool: str, decision: Decision) -> str:
    """What a caller is told of a listed tool's class (``EFFECT_META``), from its decision as a
    call without arguments: ``read`` only when no argument can make a call of it more."""
    name = tool.removeprefix(server.tool_prefix or "")
    effect = decision.effect or EffectClass.UNCLASSIFIED.value
    if name not in classified_names(server):
        return effect
    possible = possible_classes(server, name)
    if possible == frozenset({EffectClass.READ}):
        return EffectClass.READ.value
    others = sorted(possible - {EffectClass.READ})
    return effect if effect != EffectClass.READ.value else others[0].value
