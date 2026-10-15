# SPDX-License-Identifier: MIT
"""What a model call was given: pins of each prompt section and of the tool schemas.

A pin names a section by ID and version and records its size and SHA-256. The run
record keeps the pins as an observation of content, never as authority, and they are
not checked again: neither before each model call nor when a record is read (owner
decision 3). A version names exact text: ``test_prompts.py`` maps each installed
version to its SHA-256, so text cannot change under an old version.

The system message is the sections' texts joined by a blank line, in order.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Annotated, Final, Literal

from pydantic import Field

from sandbox_science.contracts.model import Contract, Identifier

from .model import ToolSpec
from .notices import FRAMINGS

FORMAT: Final = "sandbox-prompt-pins-1"
SEPARATOR: Final = "\n\n"
Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
Size = Annotated[int, Field(strict=True, ge=0)]


class PromptPin(Contract):
    """One section: ``version`` is ``None`` for text not from the catalog."""

    id: Identifier
    version: Identifier | None
    bytes: Size
    sha256: Digest


class ToolsPin(Contract):
    """The offered tools' names and their schemas as passed, in order."""

    names: tuple[Identifier, ...]
    bytes: Size
    sha256: Digest


PinnedEntry = Annotated[str, Field(pattern=r"^mem-[1-9][0-9]{0,8}@[1-9][0-9]{0,8}$")]
"""A project memory entry at a revision: ``mem-3@2``."""


class MemoryPin(Contract):
    """The project memory an agent was given (PM-6): the framing's version, the memory's
    version, the entries given in order, how many the bound left out (PM-7), and the
    section's UTF-8 byte count and SHA-256; with no entries, no section was given and
    the size is 0."""

    framing: Identifier
    version: Size
    entries: tuple[PinnedEntry, ...] = Field(max_length=100)
    left_out: Size
    bytes: Size
    sha256: Digest


class SkillsPin(Contract):
    """The skills an agent was given (docs/specs/skills.md, SK-7, SK-21, SK-38): the
    framing's version, each source's revision, how many skills the index lists and how it
    was shortened, the skills loaded in full at the start with their bodies' SHA-256, and
    the section's UTF-8 byte count and SHA-256."""

    framing: Identifier
    sources: tuple[Annotated[str, Field(max_length=200)], ...] = Field(max_length=100)
    skills: Size
    shortening: Literal["none", "descriptions", "names", "counts"]
    always: tuple[Annotated[str, Field(max_length=200)], ...] = Field(max_length=20)
    bytes: Size
    sha256: Digest


class PromptPins(Contract):
    format: Literal["sandbox-prompt-pins-1"] = FORMAT
    role: Identifier
    sections: tuple[PromptPin, ...] = Field(min_length=1)
    tools: ToolsPin
    notices: tuple[PromptPin, ...] = ()
    """The notice framings installed when the run started; notices quote them."""
    memory: MemoryPin | None = None
    """The project memory the run was given; ``None`` for a run without the project's
    stores, or older than the memory."""
    skills: SkillsPin | None = None
    """The skills the run was given; ``None`` for a run without pinned skills."""


@dataclass(frozen=True)
class Section:
    id: str
    version: str | None
    text: str


def pin(section: Section) -> PromptPin:
    raw = section.text.encode("utf-8", "surrogatepass")
    return PromptPin(
        id=section.id,
        version=section.version,
        bytes=len(raw),
        sha256=hashlib.sha256(raw).hexdigest(),
    )


def schema_json(specs: Sequence[ToolSpec], *, canonical: bool = True) -> bytes:
    """The tools as the model gets them, ``{name, description, parameters}`` in order;
    with ``canonical``, each object's keys sorted."""
    values = [
        {"name": spec.name, "description": spec.description, "parameters": spec.parameters}
        for spec in specs
    ]
    return json.dumps(
        values, ensure_ascii=False, allow_nan=False, sort_keys=canonical, separators=(",", ":")
    ).encode("utf-8")


def pin_tools(specs: Sequence[ToolSpec]) -> ToolsPin:
    raw = schema_json(specs)
    return ToolsPin(
        names=tuple(spec.name for spec in specs),
        bytes=len(raw),
        sha256=hashlib.sha256(raw).hexdigest(),
    )


def assemble(
    role: str,
    sections: Sequence[Section],
    specs: Sequence[ToolSpec],
    *,
    memory: MemoryPin | None = None,
    skills: SkillsPin | None = None,
) -> tuple[str, PromptPins]:
    """The system message and the pins a run record keeps for it."""
    if not sections:
        raise ValueError("a system message has at least one section")
    pins = PromptPins(
        role=role,
        sections=tuple(pin(section) for section in sections),
        tools=pin_tools(specs),
        notices=tuple(
            pin(Section(framing.id, framing.version, framing.text)) for framing in FRAMINGS
        ),
        memory=memory,
        skills=skills,
    )
    return SEPARATOR.join(section.text for section in sections), pins
