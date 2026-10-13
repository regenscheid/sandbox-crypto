# SPDX-License-Identifier: MIT
"""Internal child-cell envelopes; deliberately absent from public ExecutionRequest."""

from __future__ import annotations

import hashlib
import json
import math
from typing import Annotated, Literal, Self, cast

from pydantic import AwareDatetime, ConfigDict, Field, model_validator

from .context import ExecutionContext
from .kernel_lineage import KernelLineageEntry
from .model import Contract, Identifier
from .run_kernels import (
    RunKernelBinding,
    checked_run_kernel_binding,
    run_kernel_binding_sha256,
)
from .worker import (
    DEFAULT_OUTPUT_LIMIT,
    DEFAULT_TIMEOUT_SECONDS,
    MAX_OUTPUT_LIMIT,
    MAX_TIMEOUT_SECONDS,
    CellRequest,
    Execution,
    Language,
)

MAX_RUN_CODE_BYTES = 256 * 1024
MAX_RUN_REQUEST_BYTES = 2 * 1024 * 1024


class RunCellRequest(Contract):
    """Trusted internal selection; neither a public request nor controller admission."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    kind: Literal["run_cell"] = "run_cell"
    operation_id: Identifier
    context: ExecutionContext
    binding: RunKernelBinding
    language: Language = "python"
    code: str = Field(max_length=MAX_RUN_CODE_BYTES)
    timeout_seconds: float = Field(default=DEFAULT_TIMEOUT_SECONDS, gt=0, le=MAX_TIMEOUT_SECONDS)
    output_limit_bytes: int = Field(
        default=DEFAULT_OUTPUT_LIMIT, strict=True, ge=1, le=MAX_OUTPUT_LIMIT
    )

    @model_validator(mode="after")
    def _scope(self) -> Self:
        if (self.context.project_id, self.context.session_id, self.context.environment_id) != (
            self.binding.scope.project_id,
            self.binding.scope.session_id,
            self.binding.scope.environment_id,
        ):
            raise ValueError("child selection differs from its execution context")
        return self

    def cell(self) -> CellRequest:
        """Ordinary value projection for existing package admission, never submission."""
        return CellRequest(
            operation_id=self.operation_id,
            context=self.context,
            language=self.language,
            code=self.code,
            timeout_seconds=self.timeout_seconds,
            output_limit_bytes=self.output_limit_bytes,
        )


class RunCellExecution(Contract):
    """Original immutable selection paired with its ordinary attempt/result record."""

    binding: RunKernelBinding
    binding_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    execution: Execution

    @model_validator(mode="after")
    def _identity(self) -> Self:
        scope, context = self.binding.scope, self.execution.context
        if (
            self.binding_sha256 != run_kernel_binding_sha256(self.binding)
            or self.execution.kind != "cell"
            or (scope.project_id, scope.session_id, scope.environment_id)
            != (context.project_id, context.session_id, context.environment_id)
        ):
            raise ValueError("child attempt does not match its original selection")
        return self


class RunKernelObservation(Contract):
    """Selected-run passive evidence; the legacy MAIN observation is unchanged."""

    binding: RunKernelBinding
    current_revision: Annotated[int, Field(strict=True, ge=0, le=2**62)] | None = None
    kernels: tuple[KernelLineageEntry, ...] = Field(default=(), max_length=2)
    observed_at: AwareDatetime

    @model_validator(mode="after")
    def _coherent(self) -> Self:
        if len({entry.kernel.language for entry in self.kernels}) != len(self.kernels):
            raise ValueError("run observation repeats a language")
        if len({entry.kernel.kernel_id for entry in self.kernels}) != len(self.kernels):
            raise ValueError("run observation repeats a kernel identity")
        scope = self.binding.scope
        for entry in self.kernels:
            if entry.kernel.current_revision != self.current_revision:
                raise ValueError("run observation has inconsistent revisions")
            birth = entry.birth
            if birth is not None and (
                birth.project_id,
                birth.session_id,
                birth.environment_id,
                birth.incarnation,
                birth.sandbox,
                birth.sandbox_id,
            ) != (
                scope.project_id,
                scope.session_id,
                scope.environment_id,
                self.binding.incarnation,
                self.binding.sandbox,
                self.binding.sandbox_id,
            ):
                raise ValueError("run birth differs from its original selection")
        return self


def run_cell_bytes(value: RunCellRequest) -> bytes:
    """Strictly copy finite declared input before canonical aggregate encoding."""
    if type(value) is not RunCellRequest:
        raise ValueError("invalid internal child request type")
    fields = object.__getattribute__(value, "__dict__")
    extras = object.__getattribute__(value, "__pydantic_extra__")
    if type(fields) is not dict or extras is not None:
        raise ValueError("undeclared internal child fields")
    fields = cast(dict[str, object], fields)
    if (
        len(fields) != len(RunCellRequest.model_fields)
        or any(type(k) is not str for k in fields)
        or set(fields) != set(RunCellRequest.model_fields)
    ):
        raise ValueError("undeclared internal child fields")
    context = fields["context"]
    if type(context) is not ExecutionContext:
        raise ValueError("invalid internal child context")
    original = object.__getattribute__(context, "__dict__")
    if (
        type(original) is not dict
        or object.__getattribute__(context, "__pydantic_extra__") is not None
    ):
        raise ValueError("undeclared internal child context")
    original = cast(dict[str, object], original)
    if (
        len(original) != len(ExecutionContext.model_fields)
        or any(type(k) is not str for k in original)
        or set(original) != set(ExecutionContext.model_fields)
    ):
        raise ValueError("undeclared internal child context")
    for name, item in original.items():
        cap = 1024 if name == "policy_revision" else 128
        if item is None and name == "policy_revision":
            continue
        if type(item) is not str or len(item) > cap or not item.isascii():
            raise ValueError("invalid internal child context scalar")
    code = fields["code"]
    if (
        type(code) is not str
        or len(code) > MAX_RUN_CODE_BYTES
        or len(code.encode("utf-8")) > MAX_RUN_CODE_BYTES
    ):
        raise ValueError("internal child code exceeds its byte limit")
    timeout, limit = fields["timeout_seconds"], fields["output_limit_bytes"]
    if (
        type(timeout) not in (int, float)
        or not 0 < cast(float, timeout) <= MAX_TIMEOUT_SECONDS
        or not math.isfinite(cast(float, timeout))
    ):
        raise ValueError("invalid internal child timeout")
    if type(limit) is not int or not 1 <= limit <= MAX_OUTPUT_LIMIT:
        raise ValueError("invalid internal child output limit")
    if any(
        type(fields[name]) is not str or len(cast(str, fields[name])) > 128
        for name in ("kind", "operation_id", "language")
    ):
        raise ValueError("invalid internal child selector")
    binding = checked_run_kernel_binding(cast(RunKernelBinding, fields["binding"]))
    checked = RunCellRequest.model_validate(
        {
            **fields,
            "binding": binding,
            "context": ExecutionContext.model_validate(original, strict=True),
        },
        strict=True,
    )
    raw = json.dumps(
        checked.model_dump(mode="json"), ensure_ascii=True, sort_keys=True, separators=(",", ":")
    ).encode("ascii")
    if len(raw) > MAX_RUN_REQUEST_BYTES:
        raise ValueError("internal child request exceeds its envelope limit")
    return raw


def run_cell_sha256(value: RunCellRequest) -> str:
    """Canonical request digest includes the distinct marker and original selection."""
    return hashlib.sha256(run_cell_bytes(value)).hexdigest()


def run_kernel_id(binding: RunKernelBinding, language: str | None) -> str:
    """Deterministic selected identity; no VM, process or caller authority is implied."""
    binding = checked_run_kernel_binding(binding)
    if type(language) is not str or language not in ("python", "sage"):
        raise ValueError("invalid selected language")
    raw = json.dumps(
        {"binding": binding.model_dump(mode="json"), "language": language},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("ascii")
    return "krn-run-" + hashlib.sha256(raw).hexdigest()[:32]
