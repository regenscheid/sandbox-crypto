# SPDX-License-Identifier: MIT
"""Per-server tool classifications and the class of one MCP call (D96).

A server's classification is a set of checked-in files under ``classifications/<name>/``,
one per server behind a portal, that the policy names with ``mcp.servers.<server>.
classification``. A project may have an accepted revision of its own for a server (a
classification file in the policy store, ``PolicyStore.classification``), which replaces
the checked-in files for that project wherever the policy names a classification for the
server; the functions below take it as ``project``. A call's base class is the policy's
own ``classify`` entry for the tool, else the classification's, else the server's declared
hint from its latest listing, else ``unclassified``; then the classification's argument
rules for the tool may set another class for this call. When several rules apply, the one
whose class the policy treats most restrictively wins. docs/POLICY.md, "Effect classes",
has the semantics.
"""

from __future__ import annotations

import hashlib
import itertools
import threading
from collections import OrderedDict
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal, cast

from .decision import Action, Decision, most_restrictive
from .effects import (
    BROWSE_ENABLED,
    CATALOG,
    SHIPPED,
    ArgumentRule,
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
    """``sha256:`` over the files' names and bytes, in name order; for a project revision,
    ``sha256:`` and its ``revision``."""
    revision: str | None = None
    """A project revision's name, the hex SHA-256 of its file; None for the catalog's."""

    @property
    def pins(self) -> dict[str, str]:
        """The pinned tool list: each pinned tool's digest, by name."""
        return {name: entry.sha256 for name, entry in self.tools.items() if entry.sha256}

    @property
    def pinned(self) -> str | None:
        """The day the newest pinned listing was captured, if a file says."""
        days = [found.pinned for found in self.files if found.pinned is not None]
        return max(days) if days else None


_MAX_CACHED: Final = 256
_cache: OrderedDict[tuple[str, str], ServerClassification] = OrderedDict()
_cache_lock = threading.Lock()


def _cached(key: tuple[str, str], load: Callable[[], ServerClassification]) -> ServerClassification:
    """A classification by a key that names its exact content: a catalog's name (its files
    do not change while a process runs) or a project revision's digest and server, so that
    a new revision is a new key and is seen at once."""
    with _cache_lock:
        found = _cache.get(key)
        if found is not None:
            _cache.move_to_end(key)
            return found
    loaded = load()
    with _cache_lock:
        _cache[key] = loaded
        while len(_cache) > _MAX_CACHED:
            _cache.popitem(last=False)
    return loaded


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


def catalog(name: str) -> ServerClassification:
    """The checked-in classification of server ``name``. Raises PolicyLoadError."""
    return _cached(("catalog", name), lambda: load_classification(CATALOG / name, name))


def revision_classification(
    data: bytes, server: str, *, source: str | None = None
) -> ServerClassification:
    """A project revision of server ``server``'s classification: one file's bytes, read in
    the same strict dialect as the checked-in files. Its tools may have any prefix the
    file declares; it stands for the whole server, whatever the catalog splits it into.

    Raises PolicyLoadError for an invalid file or one that classifies another server.
    """
    revision = hashlib.sha256(data).hexdigest()

    def load() -> ServerClassification:
        found = parse_model(Classification, data, kind="classification", source=source)
        if found.server != server:
            raise PolicyLoadError(
                f"{source or 'the classification'} classifies {found.server!r}, not {server!r}"
            )
        return ServerClassification(
            server, (found,), dict(found.tools), "sha256:" + revision, revision
        )

    return _cached((revision, server), load)


def catalog_names() -> frozenset[str]:
    """The servers the checked-in catalog classifies."""
    return frozenset(path.name for path in CATALOG.iterdir() if path.is_dir())


def server_classification(
    server: McpServer, project: ServerClassification | None = None
) -> ServerClassification | None:
    """The classification in force for a server whose policy entry names one: the project's
    accepted revision (``project``) when it has one, else the catalog entry it names. A
    server whose entry names none has none, a project revision included."""
    if server.classification is None:
        return None
    return project if project is not None else catalog(server.classification)


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


def tool_entry(
    server: McpServer, name: str, project: ServerClassification | None = None
) -> ToolEntry | None:
    """The classification's entry for a tool, by its name after the server's prefix."""
    found = server_classification(server, project)
    return found.tools.get(name) if found is not None else None


def named(server: McpServer, name: str, project: ServerClassification | None = None) -> bool:
    """Whether the policy names the tool: in its classification, ``classify`` or, exactly, in
    an allow or escalate list. Under ``mcp: locked`` only named tools are available (D95)."""
    return (
        name in server.classify
        or tool_entry(server, name, project) is not None
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
    project: ServerClassification | None = None,
) -> CallClass:
    """The class of a call of tool ``name`` (after the server's prefix) with ``arguments``.

    ``restrictiveness`` ranks classes under the policy, so that of several argument rules
    that apply, the one that leads to the most restrictive treatment decides; the first
    listed wins a tie. ``hint`` is the class the server's latest listing declares for it.
    ``project`` is the project's accepted revision of the server's classification.
    """
    entry = tool_entry(server, name, project)
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


def possible_classes(
    server: McpServer, name: str, project: ServerClassification | None = None
) -> frozenset[EffectClass]:
    """Every class a call of the tool can resolve to, whatever its arguments."""
    entry = tool_entry(server, name, project)
    base = server.classify.get(name) or (entry.effect if entry else EffectClass.UNCLASSIFIED)
    return frozenset({base, *(rule.effect for rule in (entry.rules if entry else ()))})


def classified_names(
    server: McpServer, project: ServerClassification | None = None
) -> frozenset[str]:
    """Every tool the server's classification and ``classify`` entries name."""
    found = server_classification(server, project)
    return frozenset(server.classify) | (frozenset(found.tools) if found else frozenset[str]())


def listed_effect(
    server: McpServer, tool: str, decision: Decision, project: ServerClassification | None = None
) -> str:
    """What a caller is told of a listed tool's class (``EFFECT_META``), from its decision as a
    call without arguments: ``read`` only when no argument can make a call of it more."""
    name = tool.removeprefix(server.tool_prefix or "")
    effect = decision.effect or EffectClass.UNCLASSIFIED.value
    if name not in classified_names(server, project):
        return effect
    possible = possible_classes(server, name, project)
    if possible == frozenset({EffectClass.READ}):
        return EffectClass.READ.value
    others = sorted(possible - {EffectClass.READ})
    return effect if effect != EffectClass.READ.value else others[0].value


_PASSES: Final = frozenset({Action.ALLOW, Action.ALERT})
_MAX_PREDICATES: Final = 10
"""Argument conditions per tool whose combinations ``widenings`` tries; past it, any
change of the tool's entry counts as a widening."""


def restrictiveness(policy: ProjectPolicy, server: str) -> Callable[[EffectClass], tuple[int, int]]:
    """How restrictively ``policy`` treats each class on ``server``, for ``resolve``: by action,
    then by the narrower grant ceiling of two escalations."""

    def rank(effect: EffectClass) -> tuple[int, int]:
        terms = class_action(policy, server, effect)
        return terms.action.restrictiveness, -list(MaxGrant).index(terms.max_grant)

    return rank


def widenings(
    policy: ProjectPolicy,
    server: str,
    current: ServerClassification | None,
    proposed: ServerClassification,
    hints: Mapping[str, EffectClass] | None = None,
) -> list[str]:
    """The tools whose classification ``proposed`` widens over ``current`` under ``policy``,
    by name (``widening_reasons``)."""
    return sorted(widening_reasons(policy, server, current, proposed, hints))


def widening_reasons(
    policy: ProjectPolicy,
    server: str,
    current: ServerClassification | None,
    proposed: ServerClassification,
    hints: Mapping[str, EffectClass] | None = None,
) -> dict[str, list[str]]:
    """Why each tool's classification in ``proposed`` widens over ``current`` under
    ``policy``, by tool.

    The rule: under any combination of the argument conditions either classification's
    rules for the tool name, a call of it widens when it would pass (``allow`` or
    ``alert``) under ``proposed`` where ``current`` escalates or denies it; when it still
    escalates but under a wider grant ceiling; or when it still escalates under a class
    it did not have, whose ceiling lets a session or project grant of that class cover it
    (grants are the user's, by class and server: the project may hold one). A tool
    ``current`` does not mention has the class decision time gives it, its hint's from
    ``hints`` (the latest listing's), else ``unclassified``; a tool ``proposed`` leaves out
    takes its hints' most permissive class, ``read``. The policy's ``classify`` entries win
    over both, as at decision time; its tool lists are not consulted, so a tool they decide
    may be reported though its calls would not change (never the reverse).
    """
    entry = policy.mcp.servers[server]
    rank = restrictiveness(policy, server)
    names = set(proposed.tools) | set(current.tools if current is not None else {})
    widened: dict[str, list[str]] = {}
    for name in sorted(names):
        before = current.tools.get(name) if current is not None else None
        after = proposed.tools.get(name)
        if before == after:
            continue
        predicates = _predicates(
            (*(before.rules if before else ()), *(after.rules if after else ()))
        )
        if len(predicates) > _MAX_PREDICATES:
            widened[name] = [f"more than {_MAX_PREDICATES} argument conditions: not compared"]
            continue
        hint = (hints or {}).get(name)
        reasons: list[str] = []
        for count in range(len(predicates) + 1):
            for chosen in itertools.combinations(predicates, count):
                arguments = _arguments(chosen)
                was = resolve(entry, name, arguments, rank, hint=hint, project=current).effect
                now = resolve(
                    entry, name, arguments, rank, hint=EffectClass.READ, project=proposed
                ).effect
                for reason in _wider(policy, server, was, now):
                    if reason not in reasons:
                        reasons.append(reason)
        if reasons:
            widened[name] = reasons
    return widened


def _wider(policy: ProjectPolicy, server: str, was: EffectClass, now: EffectClass) -> list[str]:
    """How a call's treatment widens from class ``was`` to ``now``, in words."""
    before, after = class_action(policy, server, was), class_action(policy, server, now)
    if after.action in _PASSES:
        if before.action not in _PASSES:
            return [f"{now} passes without asking, where {was} is {before.action}"]
        return []
    if after.action is not Action.ESCALATE or before.action is not Action.ESCALATE:
        return []
    reasons: list[str] = []
    grants = list(MaxGrant)
    if grants.index(after.max_grant) > grants.index(before.max_grant):
        reasons.append(f"grant ceiling {before.max_grant} -> {after.max_grant} ({was} -> {now})")
    if now != was and grants.index(after.max_grant) >= grants.index(MaxGrant.SESSION):
        reasons.append(f"any grant of {now} on {server} (up to {after.max_grant}) would cover it")
    return reasons


def _predicates(rules: Iterable[ArgumentRule]) -> list[tuple[str, object]]:
    """The distinct argument conditions of some rules: a path and the value that meets it."""
    found: dict[tuple[str, str], tuple[str, object]] = {}
    for rule in rules:
        value: object = "set" if rule.present else rule.equals
        found.setdefault((rule.argument, repr(value)), (rule.argument, value))
    return list(found.values())


def _arguments(chosen: Iterable[tuple[str, object]]) -> dict[str, object]:
    """Arguments that meet the chosen conditions, the deeper paths last."""
    arguments: dict[str, object] = {}
    for path, value in sorted(chosen, key=lambda item: item[0].count(".")):
        *parents, last = path.split(".")
        place = arguments
        for part in parents:
            child = place.get(part)
            if not isinstance(child, dict):
                child = {}
                place[part] = child
            place = cast("dict[str, object]", child)
        place[last] = value
    return arguments


def changed_tools(
    current: ServerClassification | None, proposed: ServerClassification
) -> dict[str, list[str]]:
    """The tools ``proposed`` adds, removes and changes against ``current``, by name."""
    before = dict(current.tools) if current is not None else {}
    after = proposed.tools
    return {
        "added": sorted(set(after) - set(before)),
        "removed": sorted(set(before) - set(after)),
        "changed": sorted(name for name in set(after) & set(before) if after[name] != before[name]),
    }
