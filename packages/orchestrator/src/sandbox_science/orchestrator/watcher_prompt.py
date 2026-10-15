# SPDX-License-Identifier: MIT
"""Small historical watcher system pins; never a whole-call or authority claim.

Format 1 pins the watcher's system text. Format 2 also pins the project memory section
given after it: the memory as it is at the check (docs/specs/project-memory.md, PM-6,
PM-8); the section's text is pinned by size and SHA-256, not kept.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Annotated, Literal, Self, cast

from pydantic import ConfigDict, Field, model_validator

from sandbox_science.contracts.model import Contract

from .memory_prompt import MemoryGiven
from .model import Message
from .prompts import SEPARATOR, MemoryPin

MAX_SYSTEM_BYTES = 2048
MAX_PROVENANCE_BYTES = 8192
PROMPT_VERSION = "sandbox-job-watcher-1"
EMPTY_TOOLS_SHA256 = hashlib.sha256(b"watcher-tool-schemas-v1\0[]").hexdigest()
Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


def _canonical(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _dump(value: WatcherProvenance, *, envelope: bool = True) -> dict[str, object]:
    """The envelope's fields; a format 1 envelope has no memory field."""
    exclude = set[str]() if envelope else {"envelope_sha256"}
    if value.memory is None:
        exclude.add("memory")
    return value.model_dump(mode="json", exclude=exclude)


def _system(text: str) -> bytes:
    if not isinstance(cast(object, text), str):
        raise ValueError("invalid watcher system type")
    if len(text) > MAX_SYSTEM_BYTES:
        raise ValueError("watcher system exceeds its byte bound")
    try:
        raw = text.encode("utf-8")
    except UnicodeError:
        raise ValueError("watcher system is not UTF-8") from None
    if len(raw) > MAX_SYSTEM_BYTES:
        raise ValueError("watcher system exceeds its byte bound")
    return raw


class WatcherProvenance(Contract):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    format_version: int = Field(strict=True, ge=1, le=2, default=1)
    role: Literal["watcher"] = "watcher"
    prompt_version: Literal["sandbox-job-watcher-1"] = PROMPT_VERSION
    system_text: str = Field(max_length=MAX_SYSTEM_BYTES)
    system_bytes: int = Field(strict=True, ge=0, le=MAX_SYSTEM_BYTES)
    system_sha256: Digest
    tool_count: int = Field(strict=True, ge=0, le=0, default=0)
    tool_names: tuple[str, ...] = Field(max_length=0, default=())
    tools_sha256: Digest = EMPTY_TOOLS_SHA256
    envelope_sha256: Digest
    memory: MemoryPin | None = None
    """Format 2: the project memory section given after the system text."""

    @model_validator(mode="after")
    def _pins(self) -> Self:
        if (self.format_version == 2) != (self.memory is not None):
            raise ValueError("watcher memory pin mismatch")
        raw = _system(self.system_text)
        if self.system_bytes != len(raw) or self.system_sha256 != hashlib.sha256(raw).hexdigest():
            raise ValueError("watcher system pin mismatch")
        if self.tools_sha256 != EMPTY_TOOLS_SHA256:
            raise ValueError("watcher empty-tool pin mismatch")
        fields = _dump(self, envelope=False)
        if (
            hashlib.sha256(b"watcher-prompt-envelope-v1\0" + _canonical(fields)).hexdigest()
            != self.envelope_sha256
        ):
            raise ValueError("watcher envelope digest mismatch")
        if len(_canonical(_dump(self))) > MAX_PROVENANCE_BYTES:
            raise ValueError("watcher envelope exceeds its byte bound")
        return self


def encode_provenance(value: WatcherProvenance) -> str:
    """Revalidate even a copied/constructed model, before bounded serialization."""
    if not isinstance(cast(object, value), WatcherProvenance):
        raise ValueError("invalid watcher provenance type")
    _system(value.system_text)
    # Literal/scalar fields are bounded before model_dump or JSON encoding.
    if (
        value.format_version not in (1, 2)
        or type(value.format_version) is not int
        or (value.format_version == 2) != isinstance(cast(object, value.memory), MemoryPin)
        or value.role != "watcher"
        or value.prompt_version != PROMPT_VERSION
        or value.tool_count != 0
        or type(value.tool_count) is not int
        or value.tool_names != ()
        or type(value.system_bytes) is not int
        or not 0 <= value.system_bytes <= MAX_SYSTEM_BYTES
    ):
        raise ValueError("invalid watcher provenance scalars")
    for digest in (value.system_sha256, value.tools_sha256, value.envelope_sha256):
        if not isinstance(cast(object, digest), str) or len(digest) != 64:
            raise ValueError("invalid watcher provenance digest")
    raw = _canonical(_dump(value))
    if len(raw) > MAX_PROVENANCE_BYTES:
        raise ValueError("watcher envelope exceeds its byte bound")
    WatcherProvenance.model_validate_json(raw)
    return raw.decode("utf-8")


@dataclass(frozen=True, slots=True)
class WatcherProvenanceRead:
    status: Literal["absent", "legacy", "complete", "corrupt"]
    provenance: WatcherProvenance | None = None


@dataclass(frozen=True, slots=True)
class WatcherInput:
    provenance: WatcherProvenance
    user_text: str
    memory_text: str | None = None
    """The project memory section, which the provenance's memory pin names."""

    def messages(self) -> list[Message]:
        system = self.provenance.system_text
        if self.memory_text is not None:
            system += SEPARATOR + self.memory_text
        return [
            {"role": "system", "content": system},
            {"role": "user", "content": self.user_text},
        ]


def assemble_watcher(
    system: str, user_text: str, memory: MemoryGiven | None = None
) -> WatcherInput:
    """The watcher's input: format 1 without ``memory``, format 2 with it."""
    raw = _system(system)
    fields: dict[str, object] = {
        "format_version": 1,
        "role": "watcher",
        "prompt_version": PROMPT_VERSION,
        "system_text": system,
        "system_bytes": len(raw),
        "system_sha256": hashlib.sha256(raw).hexdigest(),
        "tool_count": 0,
        "tool_names": [],
        "tools_sha256": EMPTY_TOOLS_SHA256,
    }
    if memory is not None:
        fields |= {"format_version": 2, "memory": memory.pin.model_dump(mode="json")}
    digest = hashlib.sha256(b"watcher-prompt-envelope-v1\0" + _canonical(fields)).hexdigest()
    provenance = WatcherProvenance.model_validate_json(
        _canonical({**fields, "envelope_sha256": digest})
    )
    section = memory.section if memory is not None else None
    return WatcherInput(provenance, user_text, section.text if section is not None else None)
