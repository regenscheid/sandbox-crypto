# SPDX-License-Identifier: MIT
"""Controller-only selected-run transport. These values do not issue authority."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Annotated, Literal, Self, cast

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .context import ExecutionContext
from .events import Event, ResultEvent
from .model import Contract, Identifier
from .run_cells import RunCellExecution, RunCellRequest, run_cell_bytes, run_kernel_id
from .run_kernels import RunKernelBinding, RunKernelScope
from .worker import KernelInfo

MAX_RUN_API_REQUEST = 2 * 1024 * 1024
MAX_RUN_API_RESPONSE = 512 * 1024
MAX_RUN_EVENT_PAGE = 32


class RunAPIModel(Contract):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class RunEnrollmentRequest(RunAPIModel):
    scope: RunKernelScope
    operation_id: Identifier


class RunBindingRequest(RunAPIModel):
    binding: RunKernelBinding


class RunAttemptRequest(RunBindingRequest):
    execution_id: Annotated[str, Field(pattern=r"^exe-[0-9a-f]{24}$")]
    attempt_id: Annotated[str, Field(pattern=r"^att-[0-9a-f]{24}$")]


class RunEventsRequest(RunAttemptRequest):
    after: Annotated[int, Field(strict=True, ge=0, le=2**62)]
    limit: Annotated[int, Field(strict=True, ge=1, le=MAX_RUN_EVENT_PAGE)]


class RunCloseRequest(RunBindingRequest):
    operation_id: Identifier


class RunRestartRequest(RunCloseRequest):
    language: Literal["python", "sage"]


class RunControlInfo(RunCloseRequest):
    action: Literal["restart", "close"]
    language: Literal["python", "sage"] | None
    recorded_at: Annotated[str, Field(max_length=40)]

    @model_validator(mode="after")
    def _action(self) -> Self:
        stamp = datetime.fromisoformat(self.recorded_at)
        offset = stamp.utcoffset()
        if (
            offset is None
            or offset.total_seconds() != 0
            or (self.action == "restart") != (self.language is not None)
        ):
            raise ValueError("invalid original child control")
        return self


class RunControlResult(RunAPIModel):
    request: RunControlInfo
    state: Literal["pending", "done"]
    kernel: KernelInfo | None = None

    @model_validator(mode="after")
    def _result(self) -> Self:
        if self.state == "pending" or self.request.action == "close":
            if self.kernel is not None:
                raise ValueError("control cannot claim an unobserved kernel")
        elif (
            self.kernel is None
            or self.kernel.language != self.request.language
            or self.kernel.kernel_id != run_kernel_id(self.request.binding, self.request.language)
            or type(self.kernel.generation) is not int
            or not 1 <= self.kernel.generation < 2**31
        ):
            raise ValueError("control kernel differs from its original selection")
        return self


def check_run_execution(value: RunCellExecution) -> None:
    """A nonnull kernel must name this selected binding, never a MAIN kernel."""
    kernels = (
        value.execution.kernel,
        value.execution.result.kernel if value.execution.result is not None else None,
    )
    language: str | None = None
    for kernel in kernels:
        if kernel is not None:
            if (
                kernel.kernel_id != run_kernel_id(value.binding, kernel.language)
                or type(kernel.generation) is not int
                or not 1 <= kernel.generation < 2**31
                or (language is not None and language != kernel.language)
            ):
                raise ValueError("execution kernel differs from the selected binding")
            language = kernel.language


class RunEventPage(RunAPIModel):
    selected: RunCellExecution
    after: Annotated[int, Field(strict=True, ge=0, le=2**62)]
    next_after: Annotated[int, Field(strict=True, ge=0, le=2**62)]
    last_seq: Annotated[int, Field(strict=True, ge=0, le=2**62)]
    events: tuple[Event, ...] = Field(max_length=MAX_RUN_EVENT_PAGE)

    @model_validator(mode="after")
    def _page(self) -> Self:
        check_run_execution(self.selected)
        expected = self.after
        for event in self.events:
            expected += 1
            if (
                event.execution_id != self.selected.execution.execution_id
                or type(event.seq) is not int
                or event.seq != expected
                or (isinstance(event, ResultEvent) and event.execution != self.selected.execution)
            ):
                raise ValueError("selected event page differs from its original attempt")
        if (
            expected != self.next_after
            or not self.after <= self.next_after <= self.last_seq
            or (self.next_after < self.last_seq and not self.events)
        ):
            raise ValueError("selected event cursor is incoherent")
        return self


def strict_run_json(raw: bytes, cap: int) -> object:
    """Bounded duplicate-free JSON, before schema validation or retained reads."""
    if type(raw) is not bytes or len(raw) > cap:
        raise ValueError("selected transport exceeds its byte bound")

    def pairs(values: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in values:
            if key in result:
                raise ValueError("duplicate selected transport key")
            result[key] = value
        return result

    return json.loads(
        raw.decode("utf-8"),
        object_pairs_hook=pairs,
        parse_constant=lambda _: (_ for _ in ()).throw(ValueError("nonfinite selected value")),
    )


def run_api_bytes(value: BaseModel) -> bytes:
    """Preflight exact known request models before encoding copied host inputs."""
    if type(value) is RunCellRequest:
        return run_cell_bytes(value)
    known = (
        RunEnrollmentRequest,
        RunBindingRequest,
        RunAttemptRequest,
        RunEventsRequest,
        RunCloseRequest,
        RunRestartRequest,
        RunKernelBinding,
        RunKernelScope,
        ExecutionContext,
    )

    def plain(item: object, depth: int = 0) -> object:
        if depth > 8:
            raise ValueError("selected request is too deep")
        if item is None or type(item) is bool or type(item) is int:
            return item
        if type(item) is str:
            if len(item) > 128 or not item.isascii():
                raise ValueError("selected request scalar exceeds its bound")
            return item
        if not any(type(item) is model for model in known):
            raise ValueError("invalid selected request type")
        fields = cast(dict[object, object], object.__getattribute__(item, "__dict__"))
        model = cast(type[BaseModel], type(item))
        if (
            type(fields) is not dict
            or object.__getattribute__(item, "__pydantic_extra__") is not None
            or len(fields) != len(model.model_fields)
            or any(type(key) is not str for key in fields)
            or set(fields) != set(model.model_fields)
        ):
            raise ValueError("undeclared selected request field")
        return {cast(str, key): plain(child, depth + 1) for key, child in fields.items()}

    if not any(type(value) is model for model in known[:6]):
        raise ValueError("invalid selected request envelope")
    raw = json.dumps(plain(value), sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    if len(raw) > MAX_RUN_API_REQUEST:
        raise ValueError("selected request exceeds its byte bound")
    type(value).model_validate_json(raw, strict=True)
    return raw
