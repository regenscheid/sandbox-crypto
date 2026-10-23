# SPDX-License-Identifier: MIT
"""Tool dispatch values shared by the registry and ordinary capability builders."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from functools import partial
from typing import Any

from .hooks import Effect, Notice
from .model import ToolCall, ToolSpec
from .recording import Written


@dataclass
class ToolOutcome:
    """What the model sees, and what the transcript keeps."""

    text: str
    record: dict[str, Any] = field(default_factory=dict[str, Any])
    whole: bool = False
    """The result must reach the model as it is, with no notices riding on it."""
    written: Written | None = None
    """The files the call changed, which the run's recorder records (RA-27)."""
    saved: dict[str, str] = field(default_factory=dict[str, str])
    """The texts saved whole behind a sample, by path (Shell 5), which the session's
    archive keeps beside the result (docs/specs/context-management.md, CM-6)."""
    image: tuple[Notice, str] | None = None
    """An image ``view_image`` gives the model, and its token: the loop adds its framed
    message after the turn's results (docs/specs/figures.md, FG-37)."""


@dataclass(frozen=True)
class Request:
    """One tool call as its handler gets it; ``args`` is empty for a raw entry."""

    call: ToolCall
    args: dict[str, Any]
    operation: str
    model: str | None


Handler = Callable[[Request], Awaitable[ToolOutcome]]


@dataclass(frozen=True)
class ToolEntry:
    """An offered tool: its spec, its handler, and the effect class a gate reads."""

    spec: ToolSpec
    effect: Effect
    handler: Handler
    raw: bool = False
    """The handler parses the call's arguments itself."""
    bound: bool = False
    """The result is bounded like a file's or an execution's text."""
    whole: bool = False
    """The result reaches the model as it is, with no notices riding on it."""


def group(
    specs: Sequence[ToolSpec],
    run: Callable[[str, Request], Awaitable[tuple[str, dict[str, Any]]]],
    effects: Mapping[str, Effect],
    **flags: bool,
) -> list[ToolEntry]:
    """A group's entries, whose ``run`` answers a call with text and a record. A tool
    ``effects`` does not name is an effect, so a new tool is gated until classified."""

    async def handler(name: str, request: Request) -> ToolOutcome:
        return ToolOutcome(*await run(name, request))

    return [
        ToolEntry(spec, effects.get(spec.name, "effect"), partial(handler, spec.name), **flags)
        for spec in specs
    ]


def plain(handler: Callable[[dict[str, Any], str], Awaitable[ToolOutcome]]) -> Handler:
    return lambda request: handler(request.args, request.operation)
