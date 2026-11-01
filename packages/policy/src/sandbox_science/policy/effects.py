# SPDX-License-Identifier: MIT
"""Effect classes of MCP tool calls and their per-class actions (D96, D97).

Every ``tools/call`` resolves to one :class:`EffectClass`. The class comes from the project
policy's per-server classification (a checked-in classification file and the policy's own
``classify`` entries), else from the server's declared hints, and an argument rule can
change it for one call. The policy then gives each class an action and the widest grant a
user may give (``max_grant``), per server with cross-server defaults. docs/POLICY.md, "Effect
classes", defines the semantics; :mod:`.classification` resolves a call's class.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Final, Literal, Self, cast

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    StringConstraints,
    model_validator,
)

from .decision import Action

CATALOG: Final = Path(__file__).parent / "classifications"
"""The checked-in classifications, one directory of ``*.yaml`` files per server."""


class EffectClass(StrEnum):
    """What a tool call does to the world (D96)."""

    READ = "read"
    WRITE = "write"
    COMMUNICATE = "communicate"
    BROWSE = "browse"
    ADMIN = "admin"
    UNCLASSIFIED = "unclassified"


class MaxGrant(StrEnum):
    """The widest grant a user may give for an escalated class (D97), narrowest first."""

    NONE = "none"
    ONCE = "once"
    SESSION = "session"
    PROJECT = "project"


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ClassAction(_Strict):
    """A class's action and grant ceiling. A grant applies only to an escalation."""

    action: Action
    """``allow``, ``escalate`` or ``deny`` (D97); ``alert`` proceeds with a recorded finding."""
    max_grant: MaxGrant = MaxGrant.NONE

    @model_validator(mode="after")
    def _grants_only_for_escalations(self) -> Self:
        if self.action is not Action.ESCALATE and self.max_grant is not MaxGrant.NONE:
            raise ValueError("max_grant applies only to action: escalate")
        return self


SHIPPED: Final[Mapping[EffectClass, ClassAction]] = {
    EffectClass.READ: ClassAction(action=Action.ALLOW),
    EffectClass.WRITE: ClassAction(action=Action.ESCALATE, max_grant=MaxGrant.PROJECT),
    EffectClass.COMMUNICATE: ClassAction(action=Action.ESCALATE, max_grant=MaxGrant.SESSION),
    EffectClass.BROWSE: ClassAction(action=Action.DENY),
    EffectClass.ADMIN: ClassAction(action=Action.ESCALATE, max_grant=MaxGrant.ONCE),
    EffectClass.UNCLASSIFIED: ClassAction(action=Action.ESCALATE, max_grant=MaxGrant.ONCE),
}
"""The shipped per-class defaults (D97). ``browse`` denies while browser automation is off."""

BROWSE_ENABLED: Final = ClassAction(action=Action.ESCALATE, max_grant=MaxGrant.ONCE)
"""``browse`` once ``mcp.browser_automation`` is ``enabled`` and nothing else is set (D96)."""

ClassActions = dict[EffectClass, ClassAction]

ToolName = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_./-]{1,128}$")]
"""An exact tool name: tool-name characters, no wildcard."""

_ARGUMENT_RE: Final = re.compile(r"[A-Za-z0-9_$-]{1,64}(?:\.[A-Za-z0-9_$-]{1,64}){0,7}")


def _check_argument(value: str) -> str:
    if not _ARGUMENT_RE.fullmatch(value):
        raise ValueError(f"{value!r} is not an argument path: names joined by '.', at most 8")
    return value


ArgumentPath = Annotated[str, AfterValidator(_check_argument)]
"""A dotted path into a call's arguments, such as ``profile.saveChanges``."""


def argument_value(arguments: Mapping[str, object], path: str) -> tuple[bool, object]:
    """Whether ``path`` exists in ``arguments`` (through objects only), and its value."""
    found, value = True, cast(object, arguments)
    for part in path.split("."):
        found, value = _child(value, part)
        if not found:
            break
    return found, value


def _child(value: object, part: str) -> tuple[bool, object]:
    if not isinstance(value, Mapping):
        return False, None
    fields = cast("Mapping[str, object]", value)
    return (True, fields[part]) if part in fields else (False, None)


def _set(value: object) -> bool:
    """A value that says something: not null, false, zero or empty."""
    if value is None or value is False:
        return False
    if isinstance(value, str | list | dict):
        return bool(cast("str | list[object] | dict[str, object]", value))
    return True


class ArgumentRule(_Strict):
    """A call's class when an argument is set (``present: true``) or has a value (``equals``).

    ``present`` treats null, false, zero-length strings, lists and objects as absent, so
    ``actions: []`` is no browser action.
    """

    argument: ArgumentPath
    present: Literal[True] | None = None
    equals: StrictBool | StrictInt | Annotated[str, StringConstraints(max_length=256)] | None = None
    effect: EffectClass = Field(alias="class")

    @model_validator(mode="after")
    def _one_condition(self) -> Self:
        if (self.present is None) == (self.equals is None):
            raise ValueError("an argument rule needs exactly one of present: true and equals")
        return self

    def fires(self, arguments: Mapping[str, object]) -> bool:
        """Whether the rule applies to a call with these arguments."""
        found, value = argument_value(arguments, self.argument)
        if not found:
            return False
        if self.present:
            return _set(value)
        # A boolean is not the integer 1, though Python compares them equal.
        return type(value) is type(self.equals) and value == self.equals

    @property
    def label(self) -> str:
        """How decisions name the rule."""
        condition = "set" if self.present else f"= {self.equals!r}"
        return f"{self.argument} {condition}"


SignificantKind = Literal["url", "host", "recipient", "title", "size", "query"]
"""What an approval card shows of a call (D97): the argument holding each."""


class ToolEntry(_Strict):
    """One tool's classification: its base class, why, its pin and its argument rules."""

    effect: EffectClass = Field(alias="class")
    reason: Annotated[str, StringConstraints(min_length=1, max_length=200, pattern=r"^[^\n\r]+$")]
    sha256: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")] | None = None
    """The digest of the tool as the pinned listing has it (``mcp_wire.tool_digest``); none
    for a tool classified from an earlier listing that the pinned one lacks."""
    significant: dict[SignificantKind, ArgumentPath] = Field(
        default_factory=dict[SignificantKind, ArgumentPath]
    )
    rules: tuple[ArgumentRule, ...] = ()

    @property
    def classes(self) -> frozenset[EffectClass]:
        """Every class a call of the tool can have."""
        return frozenset({self.effect, *(rule.effect for rule in self.rules)})


class Classification(_Strict):
    """A classification file: one server's tools, or those behind a portal with one prefix."""

    api_version: Literal["sandbox-science/v0alpha1"] = Field(alias="apiVersion")
    kind: Literal["ToolClassification"]
    server: Annotated[str, StringConstraints(pattern=r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")]
    prefix: Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_./-]{0,64}$")] = ""
    """The start of every tool name in the file, such as ``firecrawl-mcp_``."""
    pinned: Annotated[str, StringConstraints(pattern=r"^\d{4}-\d{2}-\d{2}$")] | None = None
    """The day the pinned listing was captured."""
    tools: dict[ToolName, ToolEntry]

    @model_validator(mode="after")
    def _names_have_prefix(self) -> Self:
        stray = sorted(name for name in self.tools if not name.startswith(self.prefix))
        if stray:
            raise ValueError(f"tools without the prefix {self.prefix!r}: {', '.join(stray)}")
        return self


def hint_class(annotations: object) -> EffectClass:
    """A tool's class from its MCP annotations: ``read`` only for ``readOnlyHint`` without
    ``destructiveHint``; ``destructiveHint`` or no hint is ``unclassified`` (D96)."""
    if not isinstance(annotations, Mapping):
        return EffectClass.UNCLASSIFIED
    hints = cast("Mapping[str, object]", annotations)
    if hints.get("readOnlyHint") is True and hints.get("destructiveHint") is not True:
        return EffectClass.READ
    return EffectClass.UNCLASSIFIED
