# SPDX-License-Identifier: MIT
"""Positive selected-child dispatch; no generic Toolbox or MAIN fallback."""

# The positive dispatcher shares the reviewed bounded logical encoder only.
# pyright: reportPrivateUsage=false
from __future__ import annotations

import asyncio
import json
import math
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, Literal, Protocol, cast

from pydantic import ConfigDict, Field, ValidationError

from sandbox_science.contracts import Contract, ExecutionContext, Identifier
from sandbox_science.contracts.run_api import RunControlInfo, RunControlResult, check_run_execution
from sandbox_science.contracts.run_cells import RunCellExecution, RunCellRequest, run_cell_bytes
from sandbox_science.contracts.run_kernels import (
    RunKernelBinding,
    RunKernelEnrollment,
    RunKernelScope,
    checked_run_kernel_binding,
    run_kernel_binding_sha256,
)
from sandbox_science.contracts.worker import (
    TERMINAL_STATES,
    ErrorInfo,
    ErrorKind,
    Execution,
    ExecutionState,
    IsolationEvidence,
    JobDetails,
    KernelInfo,
    Location,
    ProbeFacts,
    Result,
    RichOutput,
    StreamOutput,
)

from .child_attempts import EffectPin
from .child_attempts import _json as journal_json
from .delegated_plan_tools import DelegatedPlanProgress, GrantedRead
from .documents.errors import DocumentError
from .model import ToolSpec
from .plan_tools import Progress
from .session_plans import PlanEvidence, PlanRef, StepStatus

MAX_ARGUMENTS = 64 * 1024
MAX_TOOL_RESULT = 48 * 1024
MAX_SELECTED_RESPONSE = 512 * 1024
_RESPONSE_MODELS = (
    RunControlInfo,
    RunControlResult,
    RunCellExecution,
    RunKernelEnrollment,
    RunKernelBinding,
    RunKernelScope,
    ExecutionContext,
    Execution,
    Result,
    KernelInfo,
    Location,
    IsolationEvidence,
    ProbeFacts,
    ErrorInfo,
    StreamOutput,
    RichOutput,
    JobDetails,
)


def checked_response[M: Contract](model: type[M], value: M) -> M:
    """Bound exact supplied host DTOs before any model_dump/aggregate encoding."""
    left, nodes = MAX_SELECTED_RESPONSE, 16384

    def plain(item: object, depth: int = 0) -> object:
        nonlocal left, nodes
        nodes -= 1
        if depth > 24 or nodes < 0:
            raise ValueError("selected response exceeds structural budget")
        kind = type(item)
        if any(kind is known for known in _RESPONSE_MODELS):
            raw_fields: object = object.__getattribute__(item, "__dict__")
            if type(raw_fields) is not dict:
                raise ValueError("undeclared selected response fields")
            fields = cast(dict[str, object], raw_fields)
            model_type = cast(type[Contract], kind)
            if (
                type(fields) is not dict
                or object.__getattribute__(item, "__pydantic_extra__") is not None
                or len(fields) != len(model_type.model_fields)
                or any(type(key) is not str for key in fields)
                or set(fields) != set(model_type.model_fields)
            ):
                raise ValueError("undeclared selected response fields")
            return plain(fields, depth + 1)
        if kind is datetime:
            item = cast(datetime, item).isoformat()
            kind = str
        elif kind is ExecutionState or kind is ErrorKind:
            item = cast(ExecutionState | ErrorKind, item).value
            kind = str
        if kind is dict:
            fields = cast(dict[object, object], item)
            if len(fields) > 512 or any(type(key) is not str for key in fields):
                raise ValueError("invalid selected response object")
            result = {
                cast(str, plain(key, depth + 1)): plain(child, depth + 1)
                for key, child in fields.items()
            }
            left -= 2 * len(fields) + 2
        elif kind is list or kind is tuple:
            children = cast(list[object] | tuple[object, ...], item)
            if len(children) > 512:
                raise ValueError("selected response collection exceeds budget")
            result = [plain(child, depth + 1) for child in children]
            left -= len(children) + 2
        elif kind is str:
            value = cast(str, item)
            if len(value) > MAX_SELECTED_RESPONSE:
                raise ValueError("selected response text exceeds budget")
            value.encode("utf-8")
            left -= 2 + sum(
                12
                if ord(char) > 65535
                else 6
                if ord(char) < 32 or ord(char) >= 127
                else 2
                if char in {'"', "\\"}
                else 1
                for char in value
            )
            result = value
        elif item is None or kind is bool:
            left -= 6
            result = item
        elif kind is int:
            if not -(2**62) <= cast(int, item) <= 2**62:
                raise ValueError("selected response integer exceeds budget")
            left -= 22
            result = item
        elif kind is float and math.isfinite(cast(float, item)):
            left -= 32
            result = item
        else:
            raise ValueError("invalid selected response type")
        if left < 0:
            raise ValueError("selected response encoding exceeds budget")
        return result

    if type(value) is not model or not any(model is known for known in _RESPONSE_MODELS):
        raise ValueError("invalid selected response model")
    raw = json.dumps(
        plain(value), ensure_ascii=True, allow_nan=False, separators=(",", ":")
    ).encode("ascii")
    if len(raw) > MAX_SELECTED_RESPONSE:
        raise ValueError("selected response encoding exceeds budget")
    return model.model_validate_json(raw, strict=True)


ToolName = Literal["python", "sage", "plan_read", "plan_history", "plan_progress"]


class SelectedWorker(Protocol):
    async def enroll_run(
        self, scope: RunKernelScope, operation_id: str, *, timeout_seconds: float
    ) -> RunKernelEnrollment: ...
    async def submit_run_cell(
        self, request: RunCellRequest, *, timeout_seconds: float
    ) -> RunCellExecution: ...
    async def get_run(
        self,
        binding: RunKernelBinding,
        execution_id: str,
        attempt_id: str,
        *,
        timeout_seconds: float,
    ) -> RunCellExecution: ...
    async def cancel_run(
        self,
        binding: RunKernelBinding,
        execution_id: str,
        attempt_id: str,
        *,
        timeout_seconds: float,
    ) -> RunCellExecution: ...
    async def close_run(
        self, binding: RunKernelBinding, operation_id: str, *, timeout_seconds: float
    ) -> RunControlResult: ...


class Args(Contract):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class Cell(Args):
    code: Annotated[str, Field(min_length=1, max_length=MAX_ARGUMENTS)]


class Read(Args):
    pass


class History(Args):
    step_id: Identifier
    after: Annotated[int, Field(strict=True, ge=0, le=2**62)]
    target: Annotated[int, Field(strict=True, ge=0, le=2**62)] | None
    limit: Annotated[int, Field(strict=True, ge=1, le=8)]


class TrackProgress(Args):
    base: PlanRef
    step_id: Identifier
    expected_step_cursor: Annotated[int, Field(strict=True, ge=0)]
    expected_status_revision: Annotated[int, Field(strict=True, ge=0)]
    status: StepStatus
    note: Annotated[str, Field(min_length=1, max_length=4096)]
    evidence: tuple[PlanEvidence, ...] = Field(max_length=8)


@dataclass(frozen=True, slots=True)
class ChildToolSettings:
    names: tuple[ToolName, ...]
    cell_seconds: float
    output_bytes: int
    transport_seconds: float
    poll_seconds: float
    max_polls: int
    calls_per_turn: int

    def __post_init__(self) -> None:
        if type(self.calls_per_turn) is not int or self.calls_per_turn != 1:
            raise ValueError("only an explicit one-call child turn is supported")
        allowed = ("python", "sage", "plan_read", "plan_history", "plan_progress")
        if (
            type(self.names) is not tuple
            or len(self.names) > len(allowed)
            or any(type(name) is not str or name not in allowed for name in self.names)
            or len(set(self.names)) != len(self.names)
        ):
            raise ValueError("child tool subset must be explicit and positive")
        for value, maximum in (
            (self.cell_seconds, 86400),
            (self.transport_seconds, 190),
            (self.poll_seconds, 30),
        ):
            if (
                type(value) not in (int, float)
                or not math.isfinite(value)
                or not 0 < value <= maximum
            ):
                raise ValueError("child tool time budget must be explicit and finite")
        if (
            type(self.output_bytes) is not int
            or not 1 <= self.output_bytes <= 16384
            or type(self.max_polls) is not int
            or not 1 <= self.max_polls <= 1024
        ):
            raise ValueError("child result/poll budget must be explicit and finite")


def _pairs(items: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in items:
        if key in result:
            raise ValueError("duplicate child argument")
        result[key] = value
    return result


def arguments(raw: str, model: type[Args] | type[Progress]) -> Args | Progress:
    if type(raw) is not str or len(raw) > MAX_ARGUMENTS or len(raw.encode("utf-8")) > MAX_ARGUMENTS:
        raise ValueError("child arguments exceed boundary")
    value = json.loads(
        raw,
        object_pairs_hook=_pairs,
        parse_constant=lambda _: (_ for _ in ()).throw(ValueError("nonfinite")),
    )
    # Bound exact decoded primitives (including counter integers) before schema parse.
    journal_json(value, MAX_ARGUMENTS)
    # Decode once to reject duplicates, then preserve strict JSON array/tuple semantics.
    return model.model_validate_json(raw, strict=True)


def text(value: object) -> str:
    return journal_json(value, MAX_TOOL_RESULT).decode("utf-8")


@dataclass(frozen=True, slots=True)
class ChildToolResult:
    text: str
    held: bool = False
    read: GrantedRead | None = None
    effect: EffectPin | None = None


_HANDLERS = {
    "python": Cell,
    "sage": Cell,
    "plan_read": Read,
    "plan_history": History,
    "plan_progress": TrackProgress,
}


def tool_specs(settings: ChildToolSettings) -> tuple[ToolSpec, ...]:
    descriptions = {
        "python": (
            "Run Python only in this child's original selected "
            "kernel. Shared files are not isolation."
        ),
        "sage": "Run SageMath only in this child's original selected kernel; Sage syntax applies.",
        "plan_read": (
            "Read the complete current original granted track. "
            "A successful model delivery is required before eff"
            "ects."
        ),
        "plan_history": (
            "Read only one original granted step's bounded pinned historical progress page."
        ),
        "plan_progress": (
            "Append own-track progress using exact current vers"
            "ion/status/step cursors. No body/status/approval a"
            "uthority."
        ),
    }

    def schema(name: str) -> dict[str, object]:
        value = _HANDLERS[name].model_json_schema()

        def limit(item: object) -> None:
            if type(item) is dict:
                fields = cast(dict[str, object], item)
                for key, child in fields.items():
                    if key == "maximum" and type(child) is int and child == 2**62:
                        fields[key] = 2**62 - 1
                    else:
                        limit(child)
            elif type(item) is list:
                for child in cast(list[object], item):
                    limit(child)

        limit(value)
        return value

    return tuple(ToolSpec(name, descriptions[name], schema(name)) for name in settings.names)


class ChildTools:
    """Borrowed worker/plan owners; the controller owns actual task drainage."""

    def __init__(
        self,
        worker: SelectedWorker,
        binding: RunKernelBinding,
        context: ExecutionContext,
        settings: ChildToolSettings,
        *,
        plans: DelegatedPlanProgress | None,
    ):
        if type(settings) is not ChildToolSettings or type(context) is not ExecutionContext:
            raise ValueError("explicit child tools/context required")
        # Revalidate copied dataclass values instead of trusting construction.
        self.settings = ChildToolSettings(
            settings.names,
            settings.cell_seconds,
            settings.output_bytes,
            settings.transport_seconds,
            settings.poll_seconds,
            settings.max_polls,
            settings.calls_per_turn,
        )
        self.binding = checked_run_kernel_binding(binding)
        self.context = ExecutionContext.model_validate(context.model_dump(), strict=True)
        scope = self.binding.scope
        if (context.project_id, context.session_id, context.environment_id) != (
            scope.project_id,
            scope.session_id,
            scope.environment_id,
        ) or (any(name.startswith("plan_") for name in settings.names) and plans is None):
            raise ValueError("child tool binding/grant is unavailable")
        self.worker, self.plans = worker, plans
        self._handlers = {
            "python": Cell,
            "sage": Cell,
            "plan_read": Read,
            "plan_history": History,
            "plan_progress": TrackProgress,
        }

    def specs(self) -> tuple[ToolSpec, ...]:
        return tool_specs(self.settings)

    async def call(
        self,
        name: str,
        raw: str,
        operation: str,
        *,
        resolved_model: str | None,
        admitted: Callable[[], Awaitable[None]],
        accepted: Callable[[EffectPin], Awaitable[None]],
        intent: Callable[[str, object], Awaitable[None]],
    ) -> ChildToolResult:
        # Never parse or route an unoffered call, even if a generic worker can do it.
        if type(name) is not str or name not in self.settings.names or name not in self._handlers:
            return ChildToolResult(text({"error": "unoffered_tool"}))
        try:
            args = arguments(raw, self._handlers[name])
        except (ValueError, TypeError, UnicodeError, RecursionError, ValidationError):
            return ChildToolResult(text({"error": "invalid_arguments"}))
        await admitted()
        if name in ("python", "sage", "plan_progress") and self.plans is not None:
            status = await self.plans.safe_point()
            if status.get("planning") == "held":
                return ChildToolResult(text({"error": "granted_plan_held"}), held=True)
            if status.get("reread_required") or status.get("event_scan_held"):
                return ChildToolResult(text({"error": "complete_current_read_required"}))
        if name in ("python", "sage"):
            if type(args) is not Cell:
                raise ValueError("invalid child cell arguments")
            return await self._cell(
                name, args, operation, admitted=admitted, accepted=accepted, intent=intent
            )
        if self.plans is None:
            return ChildToolResult(text({"error": "missing_original_grant"}), held=True)
        try:
            if name == "plan_read":
                value = await self.plans.read_current()
                return ChildToolResult(text(value.model_dump(mode="json")), read=value)
            if name == "plan_history":
                if type(args) is not History:
                    raise ValueError("invalid child history arguments")
                history = await self.plans.history(
                    args.step_id, after=args.after, target=args.target, limit=args.limit
                )
                return ChildToolResult(text(history.model_dump(mode="json")))
            if type(args) is not TrackProgress:
                raise ValueError("invalid child progress arguments")
            request = Progress(operation_id=operation, **args.model_dump(mode="python"))
            await intent("own_track_progress", request.model_dump(mode="json"))
            await admitted()
            receipt = await self.plans.progress(request, resolved_model=resolved_model)
            return ChildToolResult(text(receipt.model_dump(mode="json")))
        except (DocumentError, ValueError):
            return ChildToolResult(text({"error": "granted_plan_held"}), held=True)

    def _selected(
        self,
        value: RunCellExecution,
        operation: str,
        previous: RunCellExecution | None = None,
        *,
        language: Literal["python", "sage"],
    ) -> RunCellExecution:
        if type(value) is not RunCellExecution:
            raise ValueError("invalid selected response")
        # Worker transport already bounds/strictly validates the canonical response.
        value = checked_response(RunCellExecution, value)
        execution = value.execution
        check_run_execution(value)
        for kernel in (execution.kernel, execution.result.kernel if execution.result else None):
            if kernel is not None and kernel.language != language:
                raise ValueError("selected response used a different language")
        if previous is not None and execution.last_seq < previous.execution.last_seq:
            raise ValueError("selected response event cursor regressed")
        if (
            value.binding != self.binding
            or execution.operation_id != operation
            or (
                previous is not None
                and (execution.execution_id, execution.attempt_id)
                != (previous.execution.execution_id, previous.execution.attempt_id)
            )
        ):
            raise ValueError("selected response replaced original attempt")
        return value

    async def _cell(
        self,
        language: Literal["python", "sage"],
        args: Cell,
        operation: str,
        *,
        admitted: Callable[[], Awaitable[None]],
        accepted: Callable[[EffectPin], Awaitable[None]],
        intent: Callable[[str, object], Awaitable[None]],
    ) -> ChildToolResult:
        selected: RunCellExecution | None = None
        pin: EffectPin | None = None
        try:
            request = RunCellRequest(
                operation_id=operation,
                context=self.context,
                binding=self.binding,
                language=language,
                code=args.code,
                timeout_seconds=float(self.settings.cell_seconds),
                output_limit_bytes=self.settings.output_bytes,
            )
            await admitted()
            await intent("selected_cell", json.loads(run_cell_bytes(request)))
            await admitted()
            selected = self._selected(
                await self.worker.submit_run_cell(
                    request, timeout_seconds=float(self.settings.transport_seconds)
                ),
                operation,
                language=language,
            )
            execution = selected.execution
            pin = EffectPin(
                operation_id=operation,
                execution_id=execution.execution_id,
                attempt_id=execution.attempt_id,
                binding_sha256=run_kernel_binding_sha256(self.binding),
            )
            # Persist actual original accepted IDs before any poll/other model call.
            await accepted(pin)
            for _ in range(self.settings.max_polls):
                if selected.execution.state in TERMINAL_STATES:
                    break
                await asyncio.sleep(self.settings.poll_seconds)
                await admitted()
                selected = self._selected(
                    await self.worker.get_run(
                        self.binding,
                        pin.execution_id,
                        pin.attempt_id,
                        timeout_seconds=float(self.settings.transport_seconds),
                    ),
                    operation,
                    selected,
                    language=language,
                )
            if selected.execution.state not in TERMINAL_STATES:
                return ChildToolResult(
                    text({"error": "selected_attempt_unknown"}), True, effect=pin
                )
            execution = selected.execution
            result = execution.result
            if (
                execution.state
                in (ExecutionState.LOST, ExecutionState.TIMED_OUT, ExecutionState.CANCELLED)
                or execution.kernel_state_lost
                or result is None
                or result.state_lost
            ):
                return ChildToolResult(text({"error": "selected_state_unknown"}), True, effect=pin)
            output: dict[str, object] = {
                "operation_id": operation,
                "execution_id": execution.execution_id,
                "attempt_id": execution.attempt_id,
                "binding_sha256": pin.binding_sha256,
                "state": execution.state.value,
                "kernel_state_lost": execution.kernel_state_lost,
                "stdout": result.stdout.text if result and result.stdout else None,
                "stderr": result.stderr.text if result and result.stderr else None,
                "status": result.status if result else None,
                "error_kind": result.error.kind.value if result and result.error else None,
            }
            return ChildToolResult(text(output), effect=pin)
        except asyncio.CancelledError:
            if pin is not None:
                # This owner stays alive until the selected cancellation request settles.
                await self.worker.cancel_run(
                    self.binding,
                    pin.execution_id,
                    pin.attempt_id,
                    timeout_seconds=float(self.settings.transport_seconds),
                )
            raise
        except Exception:
            # No raw exception/config text, retry, fallback or noncommit assertion.
            return ChildToolResult(text({"error": "selected_attempt_unknown"}), True, effect=pin)
