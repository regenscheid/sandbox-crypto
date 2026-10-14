# SPDX-License-Identifier: MIT
"""Small historical watcher system pins; never a whole-call or authority claim."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Annotated, Literal, Self, cast

from pydantic import ConfigDict, Field, model_validator

from sandbox_science.contracts.model import Contract

from .model import Message

MAX_SYSTEM_BYTES = 2048
MAX_PROVENANCE_BYTES = 4096
PROMPT_VERSION = "sandbox-job-watcher-1"
EMPTY_TOOLS_SHA256 = hashlib.sha256(b"watcher-tool-schemas-v1\0[]").hexdigest()
Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


def _canonical(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


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
    format_version: int = Field(strict=True, ge=1, le=1, default=1)
    role: Literal["watcher"] = "watcher"
    prompt_version: Literal["sandbox-job-watcher-1"] = PROMPT_VERSION
    system_text: str = Field(max_length=MAX_SYSTEM_BYTES)
    system_bytes: int = Field(strict=True, ge=0, le=MAX_SYSTEM_BYTES)
    system_sha256: Digest
    tool_count: int = Field(strict=True, ge=0, le=0, default=0)
    tool_names: tuple[str, ...] = Field(max_length=0, default=())
    tools_sha256: Digest = EMPTY_TOOLS_SHA256
    envelope_sha256: Digest

    @model_validator(mode="after")
    def _pins(self) -> Self:
        raw = _system(self.system_text)
        if self.system_bytes != len(raw) or self.system_sha256 != hashlib.sha256(raw).hexdigest():
            raise ValueError("watcher system pin mismatch")
        if self.tools_sha256 != EMPTY_TOOLS_SHA256:
            raise ValueError("watcher empty-tool pin mismatch")
        fields = self.model_dump(mode="json", exclude={"envelope_sha256"})
        if (
            hashlib.sha256(b"watcher-prompt-envelope-v1\0" + _canonical(fields)).hexdigest()
            != self.envelope_sha256
        ):
            raise ValueError("watcher envelope digest mismatch")
        if len(_canonical(self.model_dump(mode="json"))) > MAX_PROVENANCE_BYTES:
            raise ValueError("watcher envelope exceeds its byte bound")
        return self


def encode_provenance(value: WatcherProvenance) -> str:
    """Revalidate even a copied/constructed model, before bounded serialization."""
    if not isinstance(cast(object, value), WatcherProvenance):
        raise ValueError("invalid watcher provenance type")
    _system(value.system_text)
    # Literal/scalar fields are bounded before model_dump or JSON encoding.
    if (
        value.format_version != 1
        or type(value.format_version) is not int
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
    raw = _canonical(value.model_dump(mode="json"))
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

    def messages(self) -> list[Message]:
        return [
            {"role": "system", "content": self.provenance.system_text},
            {"role": "user", "content": self.user_text},
        ]


def assemble_watcher(system: str, user_text: str) -> WatcherInput:
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
    digest = hashlib.sha256(b"watcher-prompt-envelope-v1\0" + _canonical(fields)).hexdigest()
    provenance = WatcherProvenance.model_validate_json(
        _canonical({**fields, "envelope_sha256": digest})
    )
    return WatcherInput(provenance, user_text)
