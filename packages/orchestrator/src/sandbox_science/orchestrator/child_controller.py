# SPDX-License-Identifier: MIT
"""Explicit bounded child conversation; no launcher, policy defaults or MAIN fallback.

All dependencies are borrowed. This owner drains its actual async/thread work
before returning; callers must keep original FDs/stores/clients alive until then.
"""

# These narrow owners reuse reviewed pure bounded journal/record encoding helpers.
# pyright: reportPrivateUsage=false
from __future__ import annotations

import asyncio
import hashlib
import json
import re
import threading
import time
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Literal, cast

from sandbox_science.contracts import new_id
from sandbox_science.contracts.run_api import RunControlResult
from sandbox_science.contracts.run_kernels import (
    RunKernelBinding,
    RunKernelEnrollment,
    RunKernelScope,
    checked_run_kernel_binding,
    run_kernel_binding_sha256,
)

from .child_attempts import (
    AppliedBudget,
    AttemptAuthority,
    AttemptEvent,
    Begin,
    Checkpoint,
    ChildAttempts,
    EffectPin,
    Observe,
    RecordedMessage,
    RequestedProfile,
    Reserve,
    Seal,
)
from .child_attempts import _checked as checked_journal
from .child_attempts import _json as journal_json
from .child_prompt import ChildPromptBundle, response, to_message
from .child_tools import (
    ChildTools,
    ChildToolSettings,
    SelectedWorker,
    checked_response,
    text,
    tool_specs,
)
from .delegated_plan_tools import DelegatedPlanProgress, GrantedRead
from .delegation_records import (
    DelegationMutation,
    DelegationRecords,
    HandbackAuthority,
    HostObservation,
    ParentAuthority,
)
from .delegation_records import _checked as checked_record
from .documents.errors import CONFLICT, POLICY_DENIED, DocumentError
from .model import ChatModel, ModelTurn
from .projects import SessionSnapshot

Ending = Literal["returned", "failed", "cancelled", "limit_reached", "interrupted"]
Hold = Literal[
    "model_unknown",
    "retention_failed",
    "tool_unknown",
    "admission_changed",
    "plan_held",
    "budget_exhausted",
    "historical_attempt",
    "cleanup_unknown",
]


@dataclass(frozen=True, slots=True)
class AppliedChildModel:
    """Trusted composition assertion of caller-applied config, not provider proof."""

    model: ChatModel = field(repr=False)
    per_call_output_tokens: int
    monetary_limit: object = field(repr=False)

    def __post_init__(self) -> None:
        if (
            type(self.per_call_output_tokens) is not int
            or not 1 <= self.per_call_output_tokens <= 1048576
            or self.monetary_limit is not None
        ):
            raise ValueError("unsupported or missing child model budget")


@dataclass(frozen=True, slots=True)
class ChildConfiguration:
    profile: RequestedProfile
    budget: AppliedBudget
    tools: ChildToolSettings


@dataclass(frozen=True, slots=True)
class ChildResult:
    attempt_id: str
    ending: Ending
    handback: DelegationMutation | None
    held: Hold | None
    cleanup: Literal["not_enrolled", "done", "pending", "unknown"]


class ChildController:
    """One explicit single-use run owner; persistent replay is historical only."""

    def __init__(
        self,
        journal: ChildAttempts,
        records: DelegationRecords,
        worker: SelectedWorker,
        applied_model: AppliedChildModel,
        configuration: ChildConfiguration,
        *,
        plans: DelegatedPlanProgress | None,
    ):
        if (
            journal.records is not records
            or journal.documents is not records.documents
            or type(applied_model) is not AppliedChildModel
            or type(configuration) is not ChildConfiguration
            or type(configuration.tools) is not ChildToolSettings
        ):
            raise ValueError("explicit coherent child dependencies required")
        self.journal, self.records, self.worker = journal, records, worker
        self.model = AppliedChildModel(
            applied_model.model, applied_model.per_call_output_tokens, applied_model.monetary_limit
        )
        self.profile = checked_journal(RequestedProfile, configuration.profile)
        self.budget = checked_journal(AppliedBudget, configuration.budget)
        if self.budget.per_call_output_tokens != self.model.per_call_output_tokens:
            raise ValueError("caller-applied model allowance differs from reservation")
        settings = configuration.tools
        self.settings = ChildToolSettings(
            settings.names,
            settings.cell_seconds,
            settings.output_bytes,
            settings.transport_seconds,
            settings.poll_seconds,
            settings.max_polls,
            settings.calls_per_turn,
        )
        self.plans = plans
        self._task: asyncio.Task[ChildResult] | None = None
        self._used = self._closing = False
        self._withdrawn = threading.Event()
        self._binding: RunKernelBinding | None = None
        self._attempt: str | None = None
        self._cursor = 0
        self._messages: tuple[RecordedMessage, ...] = ()
        self._active_effect: EffectPin | None = None
        self._held_effects: tuple[EffectPin, ...] = ()
        self._pending_read: GrantedRead | None = None
        self._last_model: str | None = None
        self._admitted: Callable[[], bool] = lambda: False
        self._started = 0.0

    async def _owned[T](self, work: Awaitable[T], *, cancel_on_interrupt: bool = True) -> T:
        task = asyncio.ensure_future(work)
        first: asyncio.CancelledError | None = None
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError as error:
                if first is None:
                    first = error
                    self._withdrawn.set()
                    if cancel_on_interrupt:
                        task.cancel()
            except BaseException:
                if first is not None:
                    raise first from None
                raise
        if first is not None:
            # Retrieving an owned failure cannot replace the caller interruption.
            with suppress(BaseException):
                task.result()
            raise first
        return task.result()

    async def _thread[T](self, action: Callable[[], T]) -> T:
        task = asyncio.create_task(asyncio.to_thread(action))
        first: asyncio.CancelledError | None = None
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError as error:
                if first is None:
                    first = error
                    self._withdrawn.set()
            except BaseException:
                if first is not None:
                    raise first from None
                raise
        try:
            value = task.result()
        except BaseException:
            if first is not None:
                raise first from None
            raise
        if type(value) is AttemptEvent:
            self._cursor = cast(AttemptEvent, value).sequence
        if first is not None:
            raise first
        return value

    def _live(self) -> bool:
        return not self._withdrawn.is_set() and not self._closing and self._admitted() is True

    async def _admission(self) -> None:
        def check() -> None:
            self._remaining()
            if not self._live():
                raise DocumentError(POLICY_DENIED, "original child admission changed")
            current = self.records.get(self._parent, self._authority.delegation_id)
            if current.prepared != self._prepared or current.outcome is not None:
                raise DocumentError(CONFLICT, "original child is no longer current")
            self.journal.pin_session(self._authority, self._original)
            if not self._live():
                raise DocumentError(POLICY_DENIED, "original child admission changed")

        await self._thread(check)

    def _attempt_id(self) -> str:
        if self._attempt is None:
            raise DocumentError(CONFLICT, "original child attempt is unavailable")
        return self._attempt

    async def _checkpoint(self) -> None:
        attempt = self._attempt_id()
        body = Checkpoint(messages=self._messages, held_effects=self._held_effects)
        await self._thread(
            lambda: self.journal.checkpoint(
                self._authority, f"cp-{self._cursor + 1}", attempt, self._cursor, body
            )
        )

    async def _unknown(
        self,
        reservation: AttemptEvent,
        reason: Literal["timeout", "cancelled", "interrupted", "model_error", "retention_failed"],
    ) -> None:
        attempt = self._attempt_id()
        state = await self._thread(lambda: self.journal.get(self._authority, attempt))
        self._cursor, self._messages = state.cursor, state.messages
        if state.pending is not None and state.pending.sequence == reservation.sequence:
            await self._thread(
                lambda: self.journal.observe_turn(
                    self._authority,
                    f"unknown-{reservation.sequence}",
                    attempt,
                    self._cursor,
                    Observe(
                        reservation=reservation.sequence,
                        outcome="unknown",
                        response=None,
                        resolved_model=None,
                        finish_reason=None,
                        input_tokens=None,
                        output_tokens=None,
                        reason=reason,
                    ),
                )
            )

    async def _exchange(self, reservation: AttemptEvent, bundle: ChildPromptBundle) -> ModelTurn:
        attempt = self._attempt_id()
        specs = bundle.tool_specs()
        bundle.verify(self._messages, specs)
        messages = [to_message(message) for message in self._messages]
        delivered = self._pending_read
        self._last_model = None
        await self._admission()
        try:
            turn = await self.model.model.complete(messages, specs)
        except asyncio.CancelledError:
            await self._unknown(reservation, "cancelled")
            raise
        except Exception:
            await self._unknown(reservation, "model_error")
            raise _HeldError("model_unknown") from None
        try:
            value = response(turn)
            await self._thread(
                lambda: self.journal.observe_turn(
                    self._authority,
                    f"returned-{reservation.sequence}",
                    attempt,
                    self._cursor,
                    Observe(
                        reservation=reservation.sequence,
                        outcome="returned",
                        response=value,
                        resolved_model=turn.model,
                        finish_reason=turn.finish_reason,
                        input_tokens=turn.input_tokens,
                        output_tokens=turn.output_tokens,
                        reason=None,
                    ),
                )
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            await self._unknown(reservation, "retention_failed")
            raise _HeldError("retention_failed") from None
        self._messages = (*self._messages, value)
        self._last_model = turn.model
        # A late returned fact is durable, but cannot clear a live delivery fence.
        await self._admission()
        if delivered is not None and self.plans is not None:
            try:
                await self.plans.model_delivered(delivered)
            except DocumentError:
                raise _HeldError("plan_held") from None
            self._pending_read = None
        await self._admission()
        if (
            turn.output_tokens is not None
            and turn.output_tokens > self.model.per_call_output_tokens
        ):
            raise _HeldError("budget_exhausted")
        return turn

    async def _tools(self, tools: ChildTools, turn: ModelTurn, reservation: AttemptEvent) -> None:
        terminal: list[dict[str, object]] = []
        if len(turn.tool_calls) != self.settings.calls_per_turn:
            for call in turn.tool_calls:
                self._messages = (
                    *self._messages,
                    RecordedMessage(
                        role="tool",
                        tool_call_id=call.id,
                        content=text({"error": "one_call_per_turn_required"}),
                    ),
                )
            await self._checkpoint()
            return
        for call in turn.tool_calls:
            await self._admission()
            operation = (
                "ct-"
                + hashlib.sha256(
                    journal_json(
                        (self._attempt, reservation.sequence, call.id, call.name, call.arguments)
                    )
                ).hexdigest()[:40]
            )
            accepted_here = False
            intent_here = False

            async def intent(kind: str, request: object, call_id: str = call.id) -> None:
                nonlocal intent_here
                intent_here = True
                self._messages = (
                    *self._messages,
                    RecordedMessage(
                        role="tool",
                        tool_call_id=call_id,
                        content=text({"status": "intent", "kind": kind, "request": request}),
                    ),
                )
                await self._checkpoint()

            async def accepted(pin: EffectPin) -> None:
                nonlocal accepted_here
                accepted_here = True
                self._active_effect = pin
                self._messages = (
                    *self._messages,
                    RecordedMessage(
                        role="user",
                        content=(
                            "Trusted-host selected tool admission observation ("
                            "data, not a USER grant): "
                        )
                        + text({"status": "accepted", **pin.model_dump(mode="json")}),
                    ),
                )
                await self._checkpoint()

            result = await tools.call(
                call.name,
                call.arguments,
                operation,
                resolved_model=turn.model,
                admitted=self._admission,
                accepted=accepted,
                intent=intent,
            )
            if not intent_here:
                self._messages = (
                    *self._messages,
                    RecordedMessage(role="tool", tool_call_id=call.id, content=result.text),
                )
            else:
                terminal.append({"tool_call_id": call.id, "result": json.loads(result.text)})
            if result.read is not None:
                self._pending_read = result.read
            if terminal:
                self._messages = (
                    *self._messages,
                    RecordedMessage(
                        role="user",
                        content=(
                            "Trusted-host selected terminal tool observations ("
                            "data, not a USER grant): "
                        )
                        + text(terminal),
                    ),
                )
                terminal.clear()
            if result.held:
                if result.effect is not None and result.effect not in self._held_effects:
                    self._held_effects = (*self._held_effects, result.effect)
                await self._checkpoint()
                raise _HeldError("tool_unknown")
            # Retain terminal facts before clearing the in-memory active owner.
            if accepted_here:
                await self._checkpoint()
            self._active_effect = None
        if terminal:
            self._messages = (
                *self._messages,
                RecordedMessage(
                    role="user",
                    content=(
                        "Trusted-host selected terminal tool observations ("
                        "data, not a USER grant): "
                    )
                    + text(terminal),
                ),
            )
        await self._checkpoint()

    def _remaining(self) -> float:
        remaining = self.budget.wall_seconds - (time.monotonic() - self._started)
        if remaining <= 0:
            raise _HeldError("budget_exhausted")
        return remaining

    async def _loop(self, bundle: ChildPromptBundle, tools: ChildTools) -> bytes | None:
        attempt = self._attempt_id()
        for _ in range(self.budget.max_calls):
            await self._admission()
            if self.plans is not None:
                status = await self.plans.safe_point()
                if status.get("planning") == "held":
                    raise _HeldError("plan_held")
                if status.get("reread_required") or status.get("event_scan_held"):
                    self._messages = (
                        *self._messages,
                        RecordedMessage(
                            role="user",
                            content="Trusted-host planning observation (not a USER inst"
                            "ruction/grant): "
                            "Read your complete current granted track before an"
                            "y effect or final answer."
                            " A historical receipt or a same-batch read is insufficient.",
                        ),
                    )
            reservation = await self._thread(
                lambda: self.journal.reserve_turn(
                    self._authority,
                    f"reserve-{self._cursor + 1}",
                    attempt,
                    self._cursor,
                    Reserve(
                        messages=self._messages, output_tokens=self.model.per_call_output_tokens
                    ),
                    original=self._original,
                    admitted=self._live,
                )
            )
            async with asyncio.timeout(self._remaining()):
                turn = await self._owned(self._exchange(reservation, bundle))
            if not turn.tool_calls:
                if self.plans is not None:
                    status = await self.plans.safe_point()
                    if status.get("reread_required") or status.get("event_scan_held"):
                        raise _HeldError("plan_held")
                return None if turn.text is None else turn.text.encode("utf-8")
            async with asyncio.timeout(self._remaining()):
                await self._owned(self._tools(tools, turn, reservation))
        raise _HeldError("budget_exhausted")

    async def _end(self, ended: Ending, raw: bytes | None, held: Hold | None) -> ChildResult:
        attempt = self._attempt_id()
        state = await self._thread(lambda: self.journal.get(self._authority, attempt))
        self._cursor, self._messages = state.cursor, state.messages
        if self._active_effect is not None and self._active_effect not in self._held_effects:
            self._held_effects = (*self._held_effects, self._active_effect)
            await self._checkpoint()
        cleanup: Literal["not_enrolled", "done", "pending", "unknown"] = "not_enrolled"
        if self._binding is not None:
            close_operation = f"close-{attempt}"
            intent = {
                "binding": self._binding.model_dump(mode="json"),
                "operation_id": close_operation,
                "action": "close",
            }
            self._messages = (
                *self._messages,
                RecordedMessage(
                    role="user",
                    content="Trusted-host selected cleanup intent (data, not a USER grant): "
                    + text(intent),
                ),
            )
            await self._checkpoint()
            fact: object
            try:
                value = await self.worker.close_run(
                    self._binding,
                    close_operation,
                    timeout_seconds=float(self.settings.transport_seconds),
                )
                value = checked_response(RunControlResult, value)
                if (
                    value.request.binding != self._binding
                    or value.request.operation_id != close_operation
                    or value.request.action != "close"
                    or value.request.language is not None
                ):
                    raise ValueError("selected cleanup returned a foreign receipt")
                cleanup = value.state
                fact = value.model_dump(mode="json")
                if cleanup != "done":
                    held = held or "cleanup_unknown"
            except (Exception, asyncio.CancelledError):
                cleanup, held = "unknown", held or "cleanup_unknown"
                fact = {"state": "unknown"}
            self._messages = (
                *self._messages,
                RecordedMessage(
                    role="user",
                    content="Trusted-host selected cleanup observation (data, not a USER grant): "
                    + text(fact),
                ),
            )
            await self._checkpoint()
        await self._thread(
            lambda: self.journal.seal_local_attempt(
                self._authority,
                f"seal-{self._attempt}",
                attempt,
                self._cursor,
                Seal(ended=ended),
            )
        )
        authority = HandbackAuthority(
            scope=self._authority.scope,
            parent_run_id=self._authority.parent_run_id,
            delegation_id=self._authority.delegation_id,
            child_run_id=self._authority.child_run_id,
        )
        try:
            handback = await self._thread(
                lambda: self.records.finish(
                    authority,
                    f"finish-{self._attempt}",
                    1,
                    HostObservation(ended=ended, resolved_model=self._last_model),
                    raw,
                )
            )
        except Exception:
            handback, held = None, held or "retention_failed"
        return ChildResult(attempt, ended, handback, held, cleanup)

    async def run(
        self,
        authority: AttemptAuthority,
        prepared: DelegationMutation,
        original_session: SessionSnapshot,
        operation_id: str,
        *,
        admitted: Callable[[], bool],
    ) -> ChildResult:
        if self._used or self._closing:
            raise DocumentError(CONFLICT, "child controller is single-use")
        if (
            type(operation_id) is not str
            or len(operation_id) > 128
            or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", operation_id) is None
        ):
            raise DocumentError(CONFLICT, "invalid original controller operation")
        self._used = True
        invocation = new_id("cci")
        begin_operation = (
            "begin-" + hashlib.sha256(journal_json((operation_id, invocation))).hexdigest()
        )
        self._authority = checked_journal(AttemptAuthority, authority)
        self._prepared = checked_record(DelegationMutation, prepared)
        if self.plans is not None:
            if (
                type(self.plans) is not DelegatedPlanProgress
                or self.plans._prepared != self._prepared
                or self.plans._plans.documents is not self.journal.documents
                or self.plans._authority.run_id != self._authority.child_run_id
                or self.plans._authority.alias != self.profile.alias
                or "plan_read" not in self.settings.names
            ):
                raise DocumentError(POLICY_DENIED, "original own-track adapter disagrees")
        elif (self._prepared.brief is not None and self._prepared.brief.track is not None) or any(
            name.startswith("plan_") for name in self.settings.names
        ):
            raise DocumentError(POLICY_DENIED, "explicit original track grant is unavailable")
        self._parent = ParentAuthority(scope=authority.scope, parent_run_id=authority.parent_run_id)
        self._original, self._admitted = original_session, admitted
        self._started = time.monotonic()

        async def drive() -> ChildResult:
            # Build the positive table before any remote admission or model call.
            scope = RunKernelScope(
                **self._authority.scope.model_dump(),
                parent_run_id=self._authority.parent_run_id,
                delegation_id=self._authority.delegation_id,
                child_run_id=self._authority.child_run_id,
            )
            bundle = ChildPromptBundle.assemble(tool_specs(self.settings))
            pin = await self._thread(
                lambda: self.journal.pin_session(self._authority, self._original)
            )
            first = await self._thread(
                lambda: self.journal.begin_attempt(
                    self._authority,
                    begin_operation,
                    prepared=self._prepared,
                    original=self._original,
                    session=pin,
                    profile=self.profile,
                    budget=self.budget,
                    system=bundle.system,
                    tools=bundle.tools,
                    messages=(
                        *bundle.initial(self._prepared),
                        RecordedMessage(
                            role="user",
                            content=(
                                "Trusted-host original controller invocation (data,"
                                " not a USER grant): "
                            )
                            + text({"operation_id": operation_id, "invocation_id": invocation}),
                        ),
                    ),
                    admitted=self._live,
                )
            )
            self._attempt, self._cursor, self._messages = (
                first.attempt_id,
                first.sequence,
                cast(Begin, first.body).messages,
            )
            attempt = first.attempt_id
            state = await self._thread(lambda: self.journal.get(self._authority, attempt))
            if state.cursor != 1:
                return ChildResult(
                    first.attempt_id, "interrupted", None, "historical_attempt", "not_enrolled"
                )
            ended: Ending = "returned"
            held: Hold | None = None
            raw: bytes | None = None
            try:
                await self._admission()
                # Mark remote enrollment intent before any await, including lost ACK/crash.
                await self._checkpoint()
                await self._admission()
                receipt = await self.worker.enroll_run(
                    scope,
                    f"enroll-{first.attempt_id}",
                    timeout_seconds=float(self.settings.transport_seconds),
                )
                if type(receipt) is not RunKernelEnrollment:
                    raise _HeldError("admission_changed")
                receipt = checked_response(RunKernelEnrollment, receipt)
                if receipt.operation_id != f"enroll-{first.attempt_id}":
                    raise _HeldError("admission_changed")
                binding = checked_run_kernel_binding(receipt.binding)
                expected_request = hashlib.sha256(
                    b"run-kernel-enrollment-v1\0"
                    + json.dumps(
                        {
                            "binding": binding.model_dump(mode="json"),
                            "operation_id": receipt.operation_id,
                        },
                        sort_keys=True,
                        separators=(",", ":"),
                        ensure_ascii=True,
                    ).encode("ascii")
                ).hexdigest()
                if (
                    binding.scope != scope
                    or receipt.binding_sha256 != run_kernel_binding_sha256(binding)
                    or receipt.request_sha256 != expected_request
                ):
                    raise _HeldError("admission_changed")
                self._binding = binding
                self._messages = (
                    *self._messages,
                    RecordedMessage(
                        role="user",
                        content=(
                            "Trusted-host original selected kernel enrollment o"
                            "bservation (data, not a USER grant): "
                        )
                        + text(receipt.model_dump(mode="json")),
                    ),
                )
                await self._checkpoint()
                tools = ChildTools(
                    self.worker, binding, original_session.context, self.settings, plans=self.plans
                )
                raw = await self._loop(bundle, tools)
            except asyncio.CancelledError:
                self._withdrawn.set()
                await self._owned(
                    self._end("cancelled", None, "admission_changed"), cancel_on_interrupt=False
                )
                raise
            except (TimeoutError, _HeldError) as error:
                held = error.reason if isinstance(error, _HeldError) else "budget_exhausted"
                ended = "limit_reached" if held == "budget_exhausted" else "interrupted"
            except Exception:
                held = "admission_changed"
                ended = "cancelled" if self._withdrawn.is_set() else "failed"
            return await self._owned(self._end(ended, raw, held), cancel_on_interrupt=False)

        self._task = asyncio.create_task(drive())
        return await self._owned(self._task)

    async def close(self) -> None:
        first_close = not self._closing
        self._closing = True
        self._withdrawn.set()
        task = self._task
        if task is None:
            return
        if first_close and not task.done():
            task.cancel()

        async def drain() -> None:
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                if not task.done():
                    raise
                # The selected run's requested cancellation is not caller cancellation.

        await self._owned(drain(), cancel_on_interrupt=False)


class _HeldError(Exception):
    def __init__(self, reason: Hold):
        self.reason: Hold = reason
