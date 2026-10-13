# SPDX-License-Identifier: MIT
"""Bounded static input identity for a producing display-repair answer, not dispatch."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Annotated, Literal, Self, cast

from pydantic import ConfigDict, Field, field_validator, model_validator

from sandbox_science.contracts.model import Contract, Identifier
from sandbox_science.orchestrator.model import Message, ToolSpec

MAX_SYSTEM_BYTES = 2048
MAX_PROVENANCE_BYTES = 4096
PROMPT_VERSION = "workspace-display-repair-1"
_BUILTIN_SHA256 = "6c3eb945a1fc5fb98ce4be807d9dc7e4ea1a1702302cde8957df22eadb541151"
EMPTY_TOOLS_SHA256 = hashlib.sha256(b"display-repair-tools-v1\0[]").hexdigest()
Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
_FORMAT_FIELDS = (
    "version",
    "mode_prefix",
    "inline",
    "display",
    "error_prefix",
    "error_default",
    "expression_prefix",
)


def _canonical(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")
    ).encode()


def _text(value: object, limit: int) -> str:
    if not isinstance(value, str) or not value or len(value) > limit:
        raise ValueError("invalid repair provenance text")
    try:
        if len(value.encode("utf-8")) > limit:
            raise ValueError("repair provenance text exceeds its byte bound")
    except UnicodeError:
        raise ValueError("repair provenance text is not UTF-8") from None
    return value


class RequestFormat(Contract):
    """Retained literal framing; private expression/error values are never retained."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    version: Identifier = "workspace-display-repair-request-1"
    mode_prefix: str = "Mode: "
    inline: str = "inline"
    display: str = "display"
    error_prefix: str = "\nKaTeX error: "
    error_default: str = "unknown"
    expression_prefix: str = "\nExpression:\n"

    def question(self, tex: str, display: bool, error: str) -> str:
        """Render the original user message without pinning or storing its values."""
        mode = self.display if display else self.inline
        return (
            self.mode_prefix
            + mode
            + self.error_prefix
            + (error or self.error_default)
            + self.expression_prefix
            + tex
        )


class RepairProvenance(Contract):
    """Static host input content, retained only with an in-memory producing answer."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    format_version: Literal[1] = 1
    role: Literal["display-repair"] = "display-repair"
    system_kind: Literal["builtin_content", "custom_content"]
    prompt_version: Identifier | None
    system_text: str = Field(min_length=1, max_length=MAX_SYSTEM_BYTES)
    system_bytes: int = Field(strict=True, ge=1, le=MAX_SYSTEM_BYTES)
    system_sha256: Digest
    tool_count: Literal[0] = 0
    tools_sha256: Digest = EMPTY_TOOLS_SHA256
    request_format: RequestFormat
    formatter_sha256: Digest
    envelope_sha256: Digest

    @field_validator("format_version", "tool_count", mode="before")
    @classmethod
    def _integers(cls, value: object) -> object:
        # Literal validation otherwise normalizes bool/float before model validators.
        if type(value) is not int:
            raise ValueError("repair provenance counters require strict integers")
        return value

    @model_validator(mode="after")
    def _pins(self) -> Self:
        _preflight(self)
        if (self.system_kind == "custom_content") != (self.prompt_version is None):
            raise ValueError("invalid repair content version")
        raw = self.system_text.encode()
        if self.system_bytes != len(raw) or self.system_sha256 != hashlib.sha256(raw).hexdigest():
            raise ValueError("repair system pin mismatch")
        if self.tools_sha256 != EMPTY_TOOLS_SHA256:
            raise ValueError("repair empty-tool pin mismatch")
        if (
            self.formatter_sha256
            != hashlib.sha256(_canonical(self.request_format.model_dump())).hexdigest()
        ):
            raise ValueError("repair formatter pin mismatch")
        fields = self.model_dump(mode="json", exclude={"envelope_sha256"})
        if (
            self.envelope_sha256
            != hashlib.sha256(b"display-repair-envelope-v1\0" + _canonical(fields)).hexdigest()
        ):
            raise ValueError("repair envelope digest mismatch")
        if len(_canonical(self.model_dump(mode="json"))) > MAX_PROVENANCE_BYTES:
            raise ValueError("repair provenance exceeds its byte bound")
        return self


def _preflight(value: RepairProvenance) -> None:
    """Also bound forged model_copy/model_construct values before aggregate encoding."""
    if type(value) is not RepairProvenance or type(value.request_format) is not RequestFormat:
        raise ValueError("invalid repair provenance type")
    if (
        type(value.format_version) is not int
        or value.format_version != 1
        or type(value.tool_count) is not int
        or value.tool_count != 0
    ):
        raise ValueError("invalid repair provenance counters")
    if type(value.system_bytes) is not int or not 1 <= value.system_bytes <= MAX_SYSTEM_BYTES:
        raise ValueError("invalid repair provenance size")
    texts = [(value.system_text, MAX_SYSTEM_BYTES), (value.role, 32), (value.system_kind, 32)]
    if value.prompt_version is not None:
        texts.append((value.prompt_version, 128))
    texts.extend((getattr(value.request_format, name), 128) for name in _FORMAT_FIELDS)
    texts.extend(
        (digest, 64)
        for digest in (
            value.system_sha256,
            value.tools_sha256,
            value.formatter_sha256,
            value.envelope_sha256,
        )
    )
    # Reserve fixed keys, punctuation and bounded integer encoding before dumps.
    budget = 1024
    for candidate, limit in texts:
        text = _text(candidate, limit)
        # Also cover the existing HTTP encoder's ASCII escapes, which can exceed
        # UTF-8 size. The canonical descriptor itself remains strict UTF-8.
        budget += sum(
            12
            if ord(c) > 0xFFFF
            else 6
            if ord(c) < 32 or ord(c) >= 127
            else 2
            if c in {'"', "\\"}
            else 1
            for c in text
        )
        if budget > MAX_PROVENANCE_BYTES:
            raise ValueError("repair provenance exceeds its encoding budget")


def provenance_json(value: RepairProvenance) -> dict[str, object]:
    """Revalidate copied instances without consulting today's installed prompt."""
    _preflight(value)
    raw = _canonical(value.model_dump(mode="json"))
    if len(raw) > MAX_PROVENANCE_BYTES:
        raise ValueError("repair provenance exceeds its byte bound")
    return cast(
        dict[str, object], RepairProvenance.model_validate_json(raw).model_dump(mode="json")
    )


def read_provenance(raw: str) -> RepairProvenance:
    """Validate an exported static descriptor with a byte bound before JSON parsing."""
    text = _text(raw, MAX_PROVENANCE_BYTES)
    return RepairProvenance.model_validate_json(text)


@dataclass(frozen=True, slots=True)
class RepairInput:
    provenance: RepairProvenance
    user_text: str

    def verify(self, messages: Sequence[Message], tools: Sequence[ToolSpec]) -> None:
        """Check the actual fresh host inputs; no provider receipt is asserted."""
        provenance_json(self.provenance)
        if tools or list(messages) != [
            {"role": "system", "content": self.provenance.system_text},
            {"role": "user", "content": self.user_text},
        ]:
            raise ValueError("actual repair input changed")


def assemble_repair(system: str, tex: str, display: bool, error: str) -> RepairInput:
    """Capture static installed identity and fresh user framing without retaining it."""
    text = _text(system, MAX_SYSTEM_BYTES)
    sha = hashlib.sha256(text.encode()).hexdigest()
    builtin = sha == _BUILTIN_SHA256
    formatter = RequestFormat()
    packet = RepairProvenance.model_construct(
        system_kind="builtin_content" if builtin else "custom_content",
        prompt_version=PROMPT_VERSION if builtin else None,
        system_text=text,
        system_bytes=len(text.encode()),
        system_sha256=sha,
        request_format=formatter,
        formatter_sha256="0" * 64,
        envelope_sha256="0" * 64,
    )
    _preflight(packet)
    fields = packet.model_dump(mode="json", exclude={"envelope_sha256"})
    fields["formatter_sha256"] = hashlib.sha256(_canonical(formatter.model_dump())).hexdigest()
    fields["envelope_sha256"] = hashlib.sha256(
        b"display-repair-envelope-v1\0" + _canonical(fields)
    ).hexdigest()
    provenance = RepairProvenance.model_validate_json(_canonical(fields))
    return RepairInput(provenance, formatter.question(tex, display, error))
