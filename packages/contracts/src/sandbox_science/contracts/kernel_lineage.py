# SPDX-License-Identifier: MIT
"""Exact retained kernel birth evidence for internal notice consumers (CP-12)."""

from __future__ import annotations

import hashlib
import json
from typing import Annotated, Literal, Self

from pydantic import AwareDatetime, Field, StringConstraints, field_validator, model_validator

from .model import Contract, Counter, Identifier
from .worker import KernelInfo, Language

BirthId = Annotated[str, StringConstraints(pattern=r"^birth-[0-9a-f]{24}$")]
Incarnation = Annotated[str, StringConstraints(pattern=r"^inc-[0-9a-f]{24}$")]
Generation = Annotated[int, Field(strict=True, ge=1, le=2**62)]


class KernelBirthPin(Contract):
    """The recorded first birth, never reconstructed from today's environment."""

    project_id: Identifier
    environment_id: Identifier
    session_id: Identifier
    language: Language
    incarnation: Incarnation
    kernel_id: Identifier
    sandbox: Identifier | None = None
    sandbox_id: Identifier | None = None
    generation: Annotated[int, Field(strict=True, ge=1, le=2**31 - 1)]
    """Executor-local generation; distinct from the worker's monotonic generation."""
    birth_id: BirthId
    start_revision: Counter | None = None

    @model_validator(mode="after")
    def _binding(self) -> Self:
        if (self.sandbox is None) != (self.sandbox_id is None):
            raise ValueError("kernel VM binding must be complete")
        return self


class KernelLineageIdentity(Contract):
    """Complete known birth plus normalized generation; no delivery/config authority."""

    birth: KernelBirthPin
    generation: Generation

    @field_validator("birth", mode="before")
    @classmethod
    def _strict_birth(cls, value: object) -> KernelBirthPin:
        if isinstance(value, KernelBirthPin):
            value = value.model_dump(mode="python")
        return KernelBirthPin.model_validate(value, strict=True)

    @model_validator(mode="after")
    def _known(self) -> Self:
        if self.birth.sandbox is None or self.birth.start_revision is None:
            raise ValueError("kernel lineage needs its original VM and known start revision")
        if self.generation < self.birth.generation:
            raise ValueError("normalized generation predates the raw generation")
        return self


def kernel_lineage_sha256(identity: KernelLineageIdentity) -> str:
    """Canonical birth key; current revision/time and caller-owned bindings stay outside it."""
    checked = KernelLineageIdentity.model_validate(identity.model_dump(mode="python"), strict=True)
    raw = json.dumps(
        checked.model_dump(mode="json"), sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("ascii")
    return hashlib.sha256(b"kernel-lineage-v1\0" + raw).hexdigest()


LineageUnknown = Literal[
    "missing_birth",
    "corrupt_birth",
    "identity_mismatch",
    "binding_unavailable",
    "generation_unavailable",
    "unknown_start_revision",
]


class KernelLineageEntry(Contract):
    """A current kernel with exact retained birth proof, or an explicit unknown."""

    kernel: KernelInfo
    birth: KernelBirthPin | None = None
    lineage_status: Literal["proved", "unknown"]
    unknown_reason: LineageUnknown | None = None

    @field_validator("kernel", mode="before")
    @classmethod
    def _strict_kernel(cls, value: object) -> KernelInfo:
        if isinstance(value, KernelInfo):
            value = value.model_dump(mode="python")
        return KernelInfo.model_validate(value, strict=True)

    @model_validator(mode="after")
    def _proof(self) -> Self:
        if self.lineage_status == "unknown":
            if self.birth is not None or self.unknown_reason is None:
                raise ValueError("unknown lineage must have a reason and no birth authority")
            return self
        if self.birth is None or self.unknown_reason is not None:
            raise ValueError("proved lineage needs a birth and no unknown reason")
        KernelLineageIdentity(birth=self.birth, generation=self.kernel.generation)
        if (self.birth.kernel_id, self.birth.language, self.birth.start_revision) != (
            self.kernel.kernel_id,
            self.kernel.language,
            self.kernel.start_revision,
        ):
            raise ValueError("kernel lineage does not match the observed kernel")
        return self


class KernelLineageObservation(Contract):
    """Passive, bounded scope evidence; it grants no inbox, lookup or execution authority."""

    project_id: Identifier
    environment_id: Identifier
    session_id: Identifier
    current_revision: Counter | None = None
    kernels: tuple[KernelLineageEntry, ...] = Field(default=(), max_length=2)
    observed_at: AwareDatetime = Field(strict=True)

    @model_validator(mode="after")
    def _coherent(self) -> Self:
        if len({entry.kernel.language for entry in self.kernels}) != len(self.kernels):
            raise ValueError("kernel lineage repeats a language")
        if len({entry.kernel.kernel_id for entry in self.kernels}) != len(self.kernels):
            raise ValueError("kernel lineage repeats a kernel identity")
        for entry in self.kernels:
            if entry.kernel.current_revision != self.current_revision:
                raise ValueError("kernel lineage has inconsistent current revisions")
            if entry.birth is not None and (
                entry.birth.project_id,
                entry.birth.environment_id,
                entry.birth.session_id,
            ) != (self.project_id, self.environment_id, self.session_id):
                raise ValueError("kernel lineage has another birth scope")
        return self
