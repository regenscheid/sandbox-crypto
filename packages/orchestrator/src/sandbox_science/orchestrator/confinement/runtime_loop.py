# SPDX-License-Identifier: MIT
"""VM-owned model/tool loops backed by scoped trusted control capabilities."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, Sequence
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Any, cast

from pydantic import JsonValue, TypeAdapter

from sandbox_science.orchestrator.aio import owned
from sandbox_science.orchestrator.history.memory import RunMemory
from sandbox_science.orchestrator.hooks import (
    Appended,
    Closing,
    Effect,
    MessageLog,
    Notice,
    Point,
    Refusal,
    RunView,
    TurnHook,
)
from sandbox_science.orchestrator.loop import Folds, Transcript, run_task
from sandbox_science.orchestrator.model import Message, ModelGateway, ToolCall, ToolSpec
from sandbox_science.orchestrator.plan_tools import Plans, PlanTools
from sandbox_science.orchestrator.skills.tool import SkillTool
from sandbox_science.orchestrator.tools import Toolbox, ToolOutcome

from .paid import PaidModel
from .transport import Capability, Channel, TransportError
from .wire import (
    MAX_MODEL_RESPONSE_BYTES,
    AppendReply,
    Attempt,
    CancelRequest,
    Consumed,
    FoldReply,
    FoldRequest,
    HookGiven,
    HookRequest,
    LogAppend,
    LogTool,
    LoopState,
    RunGiven,
    RunReply,
    ToolboxGiven,
    ToolReply,
    ToolRequest,
    WireMessage,
    WireTurn,
    json_value,
    payload,
    typed,
)


def state(run: Transcript) -> LoopState:
    return LoopState(
        system=WireMessage.of(run.messages[0]),
        turns=run.turns,
        max_turns=run.max_turns,
        max_input_chars=run.max_input_chars,
        tools_chars=run.tools_chars,
        usage_count=len(run.usage),
        last_usage=run.usage[-1] if run.usage else None,
        message_count=len(run.messages),
        tool_count=len(run.tool_records),
        delivered={},
        offered={},
        final_text=run.final_text,
        answer=run.answer,
        pending=run.pending,
        room=run.room,
        response_rounds=run.response_rounds,
        held=run.held,
        stopped=run.stopped,
        halted=run.halted,
        failure=run.failure,
        phase=run.phase,
        base=run.base,
        since=run.since,
        overflowed=run.overflowed,
        prompt_provenance=run.prompt_provenance,
    )


class RemoteHook(TurnHook):
    def __init__(self, channel: Channel, attempt: str, slot: int, given: HookGiven) -> None:
        self.channel, self.attempt, self.slot = channel, attempt, slot
        self.name, self.veto = given.name, given.veto

    async def ask(self, kind: Capability, run: RunView | None = None, **fields: Any) -> JsonValue:
        request = HookRequest(
            attempt=self.attempt,
            slot=self.slot,
            state=state(cast(Transcript, run)) if run is not None else None,
            **fields,
        )
        return await self.channel.request(kind, payload(request), timeout=1800)

    @asynccontextmanager
    async def lease(self) -> AsyncGenerator[None]:
        await self.ask(Capability.HOOK_ENTER)
        try:
            yield
        finally:
            await owned(self.ask(Capability.HOOK_EXIT))

    async def notices(self, point: Point, run: RunView) -> Sequence[Notice]:
        return TypeAdapter(tuple[Notice, ...]).validate_python(
            await self.ask(Capability.NOTICES, run, point=point)
        )

    async def halt(self, run: RunView) -> str | None:
        return TypeAdapter(str | None).validate_python(await self.ask(Capability.HALT, run))

    async def last_call(self, run: RunView) -> str | None:
        return TypeAdapter(str | None).validate_python(await self.ask(Capability.LAST_CALL, run))

    async def consumption(self) -> Sequence[str]:
        return typed(Consumed, await self.ask(Capability.CONSUMPTION)).ids

    async def model_returned(self, run: RunView) -> None:
        await self.ask(Capability.MODEL_RETURNED, run)

    async def gate(self, effect: Effect) -> Refusal | None:
        return TypeAdapter(Refusal | None).validate_python(
            await self.ask(Capability.GATE, effect=effect)
        )

    async def observe_tool(self, run: RunView, record: dict[str, Any]) -> None:
        await self.ask(Capability.OBSERVE_TOOL, run, record=record)

    async def closing(self, run: RunView, answer: str | None) -> Closing:
        return TypeAdapter(Closing).validate_python(
            await self.ask(Capability.CLOSING, run, answer=answer)
        )

    async def accept(self, run: RunView) -> None:
        await self.ask(Capability.ACCEPT, run)

    async def finishing(self, run: RunView, stopped: str) -> str | None:
        return TypeAdapter(str | None).validate_python(
            await self.ask(Capability.FINISHING, run, stopped=stopped)
        )


class RemotePlans(RemoteHook, Plans):
    """The loop's planning marker; all authoritative gates remain on the host."""


class RemoteSkills:
    def __init__(self, given: ToolboxGiven) -> None:
        self.given = given.skills

    def summary(self, first: str | None) -> None:
        return None  # authoritative skill receipts are collected by host finalization


class RemoteToolbox(Toolbox):
    def __init__(self, channel: Channel, given: RunGiven) -> None:
        # Do not construct local worker clients, grants, repositories or tool handlers.
        self.channel, self.attempt = channel, given.attempt
        self._given = given.toolbox
        self.revision = given.toolbox.revision
        self.output_chars = given.toolbox.output_chars
        self.plans = cast(PlanTools, object()) if given.toolbox.planning else None
        self.skills = cast(SkillTool, RemoteSkills(given.toolbox)) if given.toolbox.skills else None
        self.connectors = None
        self.image_offer = given.toolbox.image_offer

    @property
    def specs(self) -> tuple[ToolSpec, ...]:
        return self._given.specs

    async def call(
        self,
        call: ToolCall,
        operation: str,
        *,
        model: str | None = None,
        gates: Sequence[TurnHook] = (),
    ) -> ToolOutcome:
        result = typed(
            ToolReply,
            await self.channel.request(
                Capability.TOOL,
                payload(
                    ToolRequest(attempt=self.attempt, call=call, operation=operation, model=model)
                ),
                timeout=1800,
            ),
        )
        self._given, self.revision = result.toolbox, result.toolbox.revision
        return result.outcome


class RemoteLog(MessageLog):
    def __init__(self, channel: Channel, attempt: str) -> None:
        self.channel, self.attempt = channel, attempt
        self._retained: dict[str, Any] | None = None

    async def started(self, run: Transcript) -> None:
        self.run = run
        await self.channel.request(
            Capability.LOG_INIT, payload(FoldRequest(attempt=self.attempt, state=state(run)))
        )

    async def start_tool(self, call: ToolCall, operation_id: str) -> None:
        await self.channel.request(
            Capability.LOG_TOOL,
            payload(LogTool(attempt=self.attempt, call=call, operation=operation_id)),
        )

    async def append(self, message: Message, appended: Appended) -> Message:
        reply = typed(
            AppendReply,
            await self.channel.request(
                Capability.LOG_APPEND,
                payload(
                    LogAppend(
                        attempt=self.attempt,
                        message=WireMessage.of({} if appended.reply is not None else message),
                        kind=appended.kind,
                        notice=appended.notice,
                        reply=WireTurn.of(appended.reply) if appended.reply else None,
                        outcome=appended.outcome,
                        call=appended.call,
                        operation_id=appended.operation_id,
                        consumed={
                            key: tuple(
                                value for value in ids if value not in self.run.delivered_ids(key)
                            )
                            for key, ids in appended.consumed.items()
                        }
                        if appended.consumed is not None
                        else None,
                        usage=appended.usage,
                        usage_index=appended.usage_index,
                    )
                ),
            ),
        )
        self._retained = reply.retained
        return reply.message.message()

    async def observed_tool(self, name: str, arguments: str, outcome: ToolOutcome) -> None:
        await self.channel.request(Capability.TOOL_DONE, payload(Attempt(attempt=self.attempt)))

    def retained_tool(self, record: dict[str, Any]) -> dict[str, Any]:
        if self._retained is None:
            raise TransportError("conflict", "tool result has no committed archive receipt")
        result, self._retained = self._retained, None
        return result


class RemoteFolds(Folds):
    def __init__(self, channel: Channel, attempt: str) -> None:
        self.channel, self.attempt = channel, attempt

    async def apply(self, kind: Capability, run: Transcript) -> bool:
        result = typed(
            FoldReply,
            await self.channel.request(
                kind, payload(FoldRequest(attempt=self.attempt, state=state(run))), timeout=1800
            ),
        )
        run.rebuilt = (
            [message.message() for message in result.rebuilt]
            if result.rebuilt is not None
            else None
        )
        run.base, run.since = result.base, result.since
        return result.proceed

    async def before_call(self, transcript: Transcript) -> bool:
        return await self.apply(Capability.FOLD_BEFORE, transcript)

    async def overflow(self, transcript: Transcript) -> bool:
        return await self.apply(Capability.FOLD_OVERFLOW, transcript)


class RuntimeLoops:
    """VM-local registry owns every actual root/child model loop's task and cancellation."""

    def __init__(self, channel: Channel, gateway: ModelGateway, scratch: Path) -> None:
        self.channel, self.gateway, self.scratch = channel, gateway, scratch
        self.active: dict[str, tuple[asyncio.Task[Transcript], asyncio.Event]] = {}

    async def run(self, value: JsonValue) -> JsonValue:
        given = typed(RunGiven, value)
        if given.attempt in self.active:
            raise TransportError("conflict", "attempt already scheduled in this runtime")
        if len(self.active) >= 64:
            raise TransportError("unavailable", "runtime active-tree limit reached")
        stop = asyncio.Event()
        scratch = self.scratch / (given.attempt + ".sqlite3")
        memory = RunMemory.scratch(scratch, ("runtime", "attempt", given.attempt))
        log = RemoteLog(self.channel, given.attempt)
        hooks = tuple(
            (RemotePlans if hook.planning else RemoteHook)(self.channel, given.attempt, slot, hook)
            for slot, hook in enumerate(given.hooks)
        )
        model = self.gateway.model(given.model).limited(
            max_output=given.max_output, max_response_bytes=MAX_MODEL_RESPONSE_BYTES
        )
        if given.cache_key is not None:
            model = model.cached_as(given.cache_key)
        task = asyncio.create_task(
            run_task(
                PaidModel(
                    self.channel,
                    model,
                    given.attempt,
                    given.model,
                    "delegated" if given.role == "delegated" else "main",
                ),
                RemoteToolbox(self.channel, given),
                given.task,
                run_id=given.run_id,
                history=[message.message() for message in given.history],
                stop=stop,
                max_turns=given.max_turns,
                max_input_chars=given.max_input_chars,
                system=given.system,
                research_template=given.research_template,
                hooks=hooks,
                memory=given.memory,
                role=given.role,
                log=log,
                folds=RemoteFolds(self.channel, given.attempt) if given.folding else None,
                memory_store=memory,
                on_started=log.started,
                on_tool=log.observed_tool,
            )
        )
        self.active[given.attempt] = task, stop
        try:
            try:
                run = await asyncio.shield(task)
            except asyncio.CancelledError:
                return payload(RunReply(attempt=given.attempt, interrupted=True))
            return payload(
                RunReply(
                    attempt=given.attempt,
                    state=state(run),
                    summaries=cast(dict[str, JsonValue], json_value(run.summaries)),
                )
            )
        finally:
            # A cancelled transport handler must join its shielded model loop before
            # closing scratch storage or dropping the only remaining task owner.
            if not task.done():
                task.cancel()
            try:
                with suppress(asyncio.CancelledError):
                    await owned(task)
            finally:
                self.active.pop(given.attempt, None)
                memory.control.close()
                for suffix in ("", "-wal", "-shm"):
                    scratch.with_name(scratch.name + suffix).unlink(missing_ok=True)

    async def cancel(self, value: JsonValue) -> JsonValue:
        request = typed(CancelRequest, value)
        active = self.active.get(request.attempt)
        if active is None:
            return None
        task, stop = active
        if request.explicit:
            stop.set()
        task.cancel()
        with suppress(asyncio.CancelledError):
            await asyncio.shield(task)
        return None

    async def close(self) -> None:
        for task, _ in self.active.values():
            task.cancel()
        await asyncio.gather(*(task for task, _ in self.active.values()), return_exceptions=True)
