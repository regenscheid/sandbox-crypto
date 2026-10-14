# SPDX-License-Identifier: MIT
"""A kernel's recorded birth, from which the worker reports its start revision (CP-12)."""

from __future__ import annotations

from typing import Annotated, Self

from pydantic import Field, StringConstraints, model_validator

from .model import Contract, Counter, Identifier
from .worker import Language

BirthId = Annotated[str, StringConstraints(pattern=r"^birth-[0-9a-f]{24}$")]
Incarnation = Annotated[str, StringConstraints(pattern=r"^inc-[0-9a-f]{24}$")]


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
