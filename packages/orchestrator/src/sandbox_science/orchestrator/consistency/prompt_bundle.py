# SPDX-License-Identifier: MIT
"""Historical reviewer input identity, independent of scientific authority.

Format 1 pins the rubric and the tool schemas. Format 2 (assembly
``sandbox-reviewer-bundle-2``) also pins the project memory section given after the
rubric: the reviewed run's memory (docs/specs/project-memory.md, PM-6, PM-8). Format 3
(assembly ``sandbox-reviewer-bundle-3``) pins the re-check instructions given after the
rubric and before any memory section, with their text (docs/specs/consistency-review.md,
CR-42, CR-43).
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Annotated, Literal, Self, cast

from pydantic import ConfigDict, Field, model_validator

from sandbox_science.contracts.model import Contract, Identifier
from sandbox_science.orchestrator.memory_prompt import MemoryGiven
from sandbox_science.orchestrator.model import ToolSpec
from sandbox_science.orchestrator.prompts import SEPARATOR, MemoryPin, schema_json

MAX_SYSTEM_BYTES = 64 * 1024
MAX_SCHEMA_BYTES = 512 * 1024
MAX_PROVENANCE_BYTES = 16 * 1024
ASSEMBLY_VERSION = "sandbox-reviewer-bundle-1"
MEMORY_ASSEMBLY = "sandbox-reviewer-bundle-2"
"""The assembly whose system message is the rubric, then the project memory section."""
RECHECK_ASSEMBLY = "sandbox-reviewer-bundle-3"
"""The assembly whose system message is the rubric, the re-check instructions, then the
project memory section if one is given."""
MAX_RECHECK_BYTES = 4 * 1024
Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


def _json(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _rubric(text: str) -> bytes:
    if not isinstance(cast(object, text), str) or len(text) > MAX_SYSTEM_BYTES:
        raise ValueError("reviewer rubric exceeds its bound")
    try:
        raw = text.encode("utf-8")
    except UnicodeError:
        raise ValueError("reviewer rubric is not UTF-8") from None
    if len(raw) > MAX_SYSTEM_BYTES:
        raise ValueError("reviewer rubric exceeds its bound")
    return raw


class RecheckPin(Contract):
    """Format 3: the re-check instructions given after the rubric, by version and text."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    version: Identifier
    text: str = Field(max_length=MAX_RECHECK_BYTES)
    bytes: int = Field(strict=True, ge=0, le=MAX_RECHECK_BYTES)
    sha256: Digest

    @model_validator(mode="after")
    def _pins(self) -> Self:
        raw = self.text.encode("utf-8")
        if len(raw) != self.bytes or hashlib.sha256(raw).hexdigest() != self.sha256:
            raise ValueError("reviewer re-check pin mismatch")
        return self


def recheck_pin(version: str, text: str) -> RecheckPin:
    raw = text.encode("utf-8")
    return RecheckPin(
        version=version, text=text, bytes=len(raw), sha256=hashlib.sha256(raw).hexdigest()
    )


def _fields(pin: ReviewerProvenance) -> dict[str, object]:
    """Preflight copied models before dumping or allocating aggregate JSON."""
    if (
        not isinstance(cast(object, pin), ReviewerProvenance)
        or type(pin.format_version) is not int
        or (pin.format_version, pin.assembly_version, pin.memory is None, pin.recheck is None)
        not in (
            (1, ASSEMBLY_VERSION, True, True),
            (2, MEMORY_ASSEMBLY, False, True),
            (3, RECHECK_ASSEMBLY, True, False),
            (3, RECHECK_ASSEMBLY, False, False),
        )
        or pin.role != "reviewer"
    ):
        raise ValueError("invalid reviewer provenance scalars")
    memory: dict[str, object] = {}
    if pin.memory is not None:
        if not isinstance(cast(object, pin.memory), MemoryPin):
            raise ValueError("invalid reviewer memory pin")
        memory = {"memory": MemoryPin.model_validate(pin.memory.model_dump()).model_dump()}
    if pin.recheck is not None:
        if not isinstance(cast(object, pin.recheck), RecheckPin):
            raise ValueError("invalid reviewer re-check pin")
        memory["recheck"] = RecheckPin.model_validate(pin.recheck.model_dump()).model_dump()
    raw = _rubric(pin.rubric_text)
    if (
        not isinstance(cast(object, pin.rubric_version), str)
        or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", pin.rubric_version) is None
        or type(pin.rubric_bytes) is not int
        or pin.rubric_bytes != len(raw)
        or type(pin.schema_bytes) is not int
        or not 0 <= pin.schema_bytes <= MAX_SCHEMA_BYTES
        or type(pin.tool_count) is not int
        or not 0 <= pin.tool_count <= 128
        or type(pin.tool_names) is not tuple
        or len(pin.tool_names) != pin.tool_count
    ):
        raise ValueError("invalid reviewer provenance fields")
    for name in pin.tool_names:
        if (
            not isinstance(cast(object, name), str)
            or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", name) is None
        ):
            raise ValueError("invalid reviewer tool name")
    if len(set(pin.tool_names)) != len(pin.tool_names):
        raise ValueError("duplicate reviewer tool name")
    for digest in (pin.rubric_sha256, pin.schemas_sha256, pin.bundle_sha256):
        if (
            not isinstance(cast(object, digest), str)
            or re.fullmatch(r"[0-9a-f]{64}", digest) is None
        ):
            raise ValueError("invalid reviewer provenance digest")
    # Charge escaping before JSON encoding. Other fields are fixed or bounded ASCII.
    cost = (
        len(raw)
        + sum(5 if ord(c) < 32 else 1 if c in {'"', "\\"} else 0 for c in pin.rubric_text)
        + sum(len(name) + 3 for name in pin.tool_names)
        + len(_json(memory))
    )
    if cost > MAX_PROVENANCE_BYTES:
        raise ValueError("reviewer provenance exceeds its bound")
    return {
        "format_version": pin.format_version,
        "assembly_version": pin.assembly_version,
        "role": pin.role,
        "rubric_version": pin.rubric_version,
        "rubric_text": pin.rubric_text,
        "rubric_bytes": pin.rubric_bytes,
        "rubric_sha256": pin.rubric_sha256,
        "tool_count": pin.tool_count,
        "tool_names": list(pin.tool_names),
        "schema_bytes": pin.schema_bytes,
        "schemas_sha256": pin.schemas_sha256,
        **memory,
    }


class ReviewerProvenance(Contract):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    format_version: int = Field(default=1, strict=True, ge=1, le=3)
    assembly_version: Literal[
        "sandbox-reviewer-bundle-1", "sandbox-reviewer-bundle-2", "sandbox-reviewer-bundle-3"
    ] = ASSEMBLY_VERSION
    role: Literal["reviewer"] = "reviewer"
    rubric_version: Identifier
    rubric_text: str = Field(max_length=MAX_SYSTEM_BYTES)
    rubric_bytes: int = Field(strict=True, ge=0, le=MAX_SYSTEM_BYTES)
    rubric_sha256: Digest
    tool_count: int = Field(strict=True, ge=0, le=128)
    tool_names: tuple[Identifier, ...] = Field(max_length=128)
    schema_bytes: int = Field(strict=True, ge=0, le=MAX_SCHEMA_BYTES)
    schemas_sha256: Digest
    bundle_sha256: Digest
    memory: MemoryPin | None = None
    """Formats 2 and 3: the project memory section given after the rubric."""
    recheck: RecheckPin | None = None
    """Format 3: the re-check instructions given after the rubric."""

    @model_validator(mode="after")
    def _pins(self) -> Self:
        fields = _fields(self)
        if hashlib.sha256(_rubric(self.rubric_text)).hexdigest() != self.rubric_sha256:
            raise ValueError("reviewer rubric pin mismatch")
        expected = hashlib.sha256(b"reviewer-prompt-bundle-v1\0" + _json(fields)).hexdigest()
        if expected != self.bundle_sha256:
            raise ValueError("reviewer bundle pin mismatch")
        if len(_json({**fields, "bundle_sha256": self.bundle_sha256})) > MAX_PROVENANCE_BYTES:
            raise ValueError("reviewer provenance exceeds its bound")
        return self


def encode_provenance(pin: ReviewerProvenance) -> str:
    """Bound and validate even an unvalidated copied instance before SQL."""
    raw = _json({**_fields(pin), "bundle_sha256": pin.bundle_sha256})
    if len(raw) > MAX_PROVENANCE_BYTES:
        raise ValueError("reviewer provenance exceeds its bound")
    ReviewerProvenance.model_validate_json(raw)
    return raw.decode("utf-8")


@dataclass(frozen=True, slots=True)
class ProvenanceRead:
    status: Literal["absent", "legacy", "complete", "corrupt"]
    provenance: ReviewerProvenance | None = None


@dataclass(frozen=True, slots=True)
class ReviewerBundle:
    """The pins a review's calls record; recorded once and not checked again before
    each call (owner decision 3)."""

    provenance: ReviewerProvenance
    memory_text: str | None = None
    """The project memory section, which the provenance's memory pin names."""

    @property
    def system(self) -> str:
        """The system message: the rubric, the re-check instructions in format 3, then the
        memory section if one was given."""
        parts = [self.provenance.rubric_text]
        if self.provenance.recheck is not None:
            parts.append(self.provenance.recheck.text)
        if self.memory_text is not None:
            parts.append(self.memory_text)
        return SEPARATOR.join(parts)


def assemble_reviewer(
    system: str,
    version: str,
    specs: Sequence[ToolSpec],
    memory: MemoryGiven | None = None,
    recheck: RecheckPin | None = None,
) -> ReviewerBundle:
    """The reviewer's input: format 3 with ``recheck``, as the runner gives it; otherwise
    format 1 without ``memory`` and format 2 with it, as earlier records were made."""
    raw = _rubric(system)
    canonical = schema_json(specs)
    names = tuple(spec.name for spec in specs)
    pin = ReviewerProvenance.model_construct(
        rubric_version=version,
        rubric_text=system,
        rubric_bytes=len(raw),
        rubric_sha256=hashlib.sha256(raw).hexdigest(),
        tool_count=len(names),
        tool_names=names,
        schema_bytes=len(canonical),
        schemas_sha256=hashlib.sha256(b"reviewer-tool-schemas-v1\0" + canonical).hexdigest(),
        bundle_sha256="0" * 64,
    )
    if memory is not None:
        pin = pin.model_copy(
            update={"format_version": 2, "assembly_version": MEMORY_ASSEMBLY, "memory": memory.pin}
        )
    if recheck is not None:
        pin = pin.model_copy(
            update={"format_version": 3, "assembly_version": RECHECK_ASSEMBLY, "recheck": recheck}
        )
    fields = _fields(pin)
    pin = pin.model_copy(
        update={
            "bundle_sha256": hashlib.sha256(
                b"reviewer-prompt-bundle-v1\0" + _json(fields)
            ).hexdigest()
        }
    )
    section = memory.section if memory is not None else None
    return ReviewerBundle(
        ReviewerProvenance.model_validate_json(encode_provenance(pin)),
        section.text if section is not None else None,
    )
