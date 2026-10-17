# SPDX-License-Identifier: MIT
"""Trusted per-attempt capability handlers; no agent loop runs in this service."""

from __future__ import annotations

import asyncio
import secrets
from collections.abc import AsyncGenerator, Awaitable, Callable, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import Any, cast

from pydantic import JsonValue, TypeAdapter

from sandbox_science.orchestrator.aio import owned
from sandbox_science.orchestrator.continuity.records import (
    CURRENT,
    Ownership,
    authority,
    check_owner,
)
from sandbox_science.orchestrator.history.log import RunArchive
from sandbox_science.orchestrator.hooks import Appended, Closing, MessageLog, Notice, TurnHook
from sandbox_science.orchestrator.loop import Folds, Transcript
from sandbox_science.orchestrator.model import ChatModel
from sandbox_science.orchestrator.plan_tools import Plans
from sandbox_science.orchestrator.tools import Toolbox, ToolOutcome

from .models import CallbackModel
from .transport import Capability, Channel, TransportError
from .wire import (
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
    json_value,
    payload,
    typed,
)


class Prepared:
    """Connection-owned authority; request payloads cannot replace its context/grants."""

    def __init__(
        self,
        given: RunGiven,
        owner: Ownership,
        toolbox: Toolbox,
        hooks: Sequence[TurnHook],
        log: MessageLog,
        folds: Folds | None,
        on_tool: Callable[[str, str, ToolOutcome], Awaitable[None]] | None,
    ) -> None:
        self.given, self.toolbox, self.hooks = given, toolbox, tuple(hooks)
        self.owner = owner
        self.log, self.folds, self.on_tool = log, folds, on_tool
        self.leases: dict[int, AbstractAsyncContextManager[None]] = {}
        self.shadow: Transcript | None = None
        self.tool: ToolOutcome | None = None
        self.tool_call: ToolRequest | None = None
        self._memory_close: Callable[[], None] | None = None
        self._lock = asyncio.Lock()

    def sync(self, state: LoopState) -> Transcript:
        """Apply loop metadata while retaining historical payloads at the host store."""
        given = self.given
        if (state.max_turns, state.max_input_chars) != (given.max_turns, given.max_input_chars):
            raise TransportError("denied", "runtime changed the admitted attempt limits")
        if self.shadow is None:
            from sandbox_science.orchestrator.history.memory import RunMemory

            if not isinstance(self.log, RunArchive):
                raise TransportError(
                    "internal", "confined attempts require canonical archive storage"
                )
            memory = RunMemory.archived(self.log)
            self.shadow = Transcript(
                memory=memory,
                messages=memory.messages(
                    [state.system.message(), *(m.message() for m in given.history)]
                ),
                tool_records=memory.tools(),
                usage=memory.usage(),
            )
        run = self.shadow
        if len(run.messages) != state.message_count or len(run.tool_records) != state.tool_count:
            raise TransportError("conflict", "runtime state does not match committed events")
        if state.usage_count == len(run.usage) + 1 and state.last_usage is not None:
            run.usage.append(state.last_usage)
        if len(run.usage) != state.usage_count:
            raise TransportError("conflict", "runtime usage is not contiguous")
        omitted = {"system", "message_count", "tool_count", "usage_count", "last_usage"}
        for name, value in state.model_dump(exclude=omitted).items():
            if name not in {"delivered", "offered", "prompt_provenance"}:
                setattr(run, name, value)
        run.delivered = {key: set(ids) for key, ids in state.delivered.items()}
        run.offered = {key: set(ids) for key, ids in state.offered.items()}
        run.prompt_provenance = state.prompt_provenance
        return run

    async def call(self, kind: Capability, value: JsonValue) -> JsonValue:
        """Restore captured epoch authority; a caller supplies neither scope nor epoch."""
        with authority(self.owner):
            if not isinstance(self.log, RunArchive):
                raise TransportError("internal", "missing canonical attempt archive")
            with self.log.archive.control.read() as tx:
                check_owner(tx.sql, self.owner)
            return await self._call(kind, value)

    async def _call(self, kind: Capability, value: JsonValue) -> JsonValue:
        async with self._lock:
            if kind is Capability.LOG_INIT:
                self.sync(typed(FoldRequest, value).state)
                return None
            if kind is Capability.LOG_APPEND:
                return await self.append(typed(LogAppend, value))
            if kind is Capability.LOG_TOOL:
                request = typed(LogTool, value)
                await self.log.start_tool(request.call, request.operation)
                return None
            if kind is Capability.TOOL:
                request = typed(ToolRequest, value)
                # Original authority, offered tools and trusted gates come from preparation.
                outcome = await self.toolbox.call(
                    request.call, request.operation, model=request.model, gates=self.hooks
                )
                self.tool, self.tool_call = outcome, request
                return payload(ToolReply(outcome=outcome, toolbox=toolbox_given(self.toolbox)))
            if kind in (Capability.FOLD_BEFORE, Capability.FOLD_OVERFLOW):
                request = typed(FoldRequest, value)
                run = self.sync(request.state)
                if self.folds is None:
                    raise TransportError("denied", "this attempt has no folding capability")
                proceed = await (
                    self.folds.before_call(run)
                    if kind is Capability.FOLD_BEFORE
                    else self.folds.overflow(run)
                )
                return payload(
                    FoldReply(
                        proceed=proceed,
                        rebuilt=tuple(WireMessage.of(m) for m in run.rebuilt)
                        if run.rebuilt is not None
                        else None,
                        base=run.base,
                        since=run.since,
                    )
                )
            request = typed(HookRequest, value)
            if request.slot >= len(self.hooks):
                raise TransportError("denied", "no such attempt hook capability")
            hook = self.hooks[request.slot]
            if kind is Capability.HOOK_ENTER:
                if request.slot in self.leases:
                    raise TransportError("conflict", "hook lease already entered")
                lease = hook.lease()
                await lease.__aenter__()
                self.leases[request.slot] = lease
                return None
            if kind is Capability.HOOK_EXIT:
                lease = self.leases.pop(request.slot, None)
                if lease is None:
                    raise TransportError("conflict", "hook lease not entered")
                await lease.__aexit__(None, None, None)
                return None
            if kind is Capability.GATE:
                if request.effect is None:
                    raise TransportError("invalid_request", "gate requires an effect")
                refusal = await hook.gate(request.effect)
                return json_value(TypeAdapter(type(refusal)).dump_python(refusal, mode="json"))
            if kind is Capability.CONSUMPTION:
                return payload(Consumed(ids=tuple(await hook.consumption())))
            if request.state is None:
                raise TransportError("invalid_request", "hook requires committed loop state")
            run = self.sync(request.state)
            if kind is Capability.NOTICES:
                if request.point is None:
                    raise TransportError("invalid_request", "notices require a safe point")
                result = await hook.notices(request.point, run)
                return json_value(
                    TypeAdapter(tuple[Notice, ...]).dump_python(tuple(result), mode="json")
                )
            if kind is Capability.HALT:
                return await hook.halt(run)
            if kind is Capability.LAST_CALL:
                return await hook.last_call(run)
            if kind is Capability.MODEL_RETURNED:
                await hook.model_returned(run)
            elif kind is Capability.OBSERVE_TOOL:
                if request.record is None:
                    raise TransportError("invalid_request", "tool observation requires its record")
                await hook.observe_tool(run, request.record)
            elif kind is Capability.CLOSING:
                return json_value(
                    TypeAdapter(Closing).dump_python(
                        await hook.closing(run, request.answer), mode="json"
                    )
                )
            elif kind is Capability.ACCEPT:
                await hook.accept(run)
            elif kind is Capability.FINISHING:
                if request.stopped is None:
                    raise TransportError("invalid_request", "finishing requires stop reason")
                return await hook.finishing(run, request.stopped)
            else:
                raise TransportError("denied", "capability is not available on an attempt")
            return None

    async def append(self, request: LogAppend) -> JsonValue:
        appended = Appended(
            request.kind,
            notice=request.notice,
            reply=request.reply.turn() if request.reply is not None else None,
            call=request.call,
            outcome=request.outcome,
            operation_id=request.operation_id,
            consumed={key: set(ids) for key, ids in request.consumed.items()}
            if request.consumed is not None
            else None,
            usage=request.usage,
            usage_index=request.usage_index,
        )
        # The canonical archive commit precedes the reply and the next effect.
        message = await self.log.append(request.message.message(), appended)
        if self.shadow is None:
            raise TransportError("conflict", "loop state has not been initialized")
        self.shadow.messages.append(message)
        if request.usage is not None and request.usage_index == len(self.shadow.usage) + 1:
            self.shadow.usage.append(request.usage)
        retained = None
        if (
            request.kind == "tool_result"
            and request.call is not None
            and request.outcome is not None
        ):
            record = {
                "tool": request.call.name,
                "arguments": request.call.arguments,
                **request.outcome.record,
            }
            retained = self.log.retained_tool(record)
            self.shadow.tool_records.append(retained)
            if self.on_tool is not None:
                await self.on_tool(request.call.name, request.call.arguments, request.outcome)
            self.tool = self.tool_call = None
        return payload(AppendReply(message=WireMessage.of(message), retained=retained))

    async def close(self) -> None:
        for slot in reversed(tuple(self.leases)):
            await self.leases.pop(slot).__aexit__(None, None, None)


def toolbox_given(toolbox: Toolbox) -> ToolboxGiven:
    return ToolboxGiven(
        specs=toolbox.specs,
        revision=toolbox.revision,
        output_chars=toolbox.output_chars,
        planning=toolbox.plans is not None,
        skills=toolbox.skills.given if toolbox.skills is not None else None,
        image_offer=toolbox.image_offer,
    )


class HostLoops:
    """The harness runner seam: prepare capabilities, dispatch to VM, finalize its receipts."""

    def __init__(self, channel: Channel, projects: frozenset[str]) -> None:
        self.channel, self.projects = channel, projects
        self.attempts: dict[str, Prepared] = {}
        self.owners: dict[str, Ownership] = {}
        self.tokens: dict[tuple[str, str, str], str] = {}

    @asynccontextmanager
    async def model_scope(self) -> AsyncGenerator[None]:
        owner = CURRENT.get()
        if owner is None or owner.project not in self.projects:
            raise TransportError("denied", "no durable attempt ownership in this scope")
        key = owner.project, owner.session, owner.run
        if key in self.tokens or len(self.tokens) >= 128:
            raise TransportError("conflict", "attempt model capability is already bound or full")
        identity = "cap-" + secrets.token_hex(24)
        self.tokens[key], self.owners[identity] = identity, owner
        try:
            yield
        finally:
            self.tokens.pop(key, None)
            self.owners.pop(identity, None)

    def model_capability(self) -> str:
        owner = CURRENT.get()
        key = (owner.project, owner.session, owner.run) if owner is not None else None
        if key is None or key not in self.tokens:
            raise TransportError("denied", "attempt model capability is absent or revoked")
        return self.tokens[key]

    async def capability(self, kind: Capability, value: JsonValue) -> JsonValue:
        # Only the connection's random opaque IDs select an existing authority object.
        identity = typed(
            Attempt, {"attempt": value.get("attempt") if isinstance(value, dict) else None}
        )
        prepared = self.attempts.get(identity.attempt)
        if prepared is None:
            raise TransportError("denied", "attempt capability is absent or revoked")
        return await prepared.call(kind, value)

    async def run(
        self, model: ChatModel, toolbox: Toolbox, task: str | None, **options: Any
    ) -> Transcript:
        """Match Harness.loop_runner; options are its internal run_task keyword arguments."""
        if toolbox.context.project_id not in self.projects:
            raise TransportError("denied", "project is outside this control capability")
        if not isinstance(model, CallbackModel):
            raise TransportError("denied", "host model execution is forbidden in confined mode")
        log = cast(MessageLog | None, options.get("log"))
        if log is None:
            raise TransportError("denied", "confined attempts require durable archive logging")
        identity = self.model_capability()
        hooks = tuple(cast(Sequence[TurnHook], options.get("hooks") or toolbox.turn_hooks()))
        given = RunGiven(
            attempt=identity,
            run_id=options["run_id"],
            task=task,
            history=tuple(WireMessage.of(message) for message in options.get("history", ())),
            max_turns=options["max_turns"],
            max_input_chars=min(options["max_input_chars"], 8 * 1024 * 1024),
            system=options["system"],
            research_template=options.get("research_template"),
            role=options["role"],
            memory=options.get("memory"),
            toolbox=toolbox_given(toolbox),
            hooks=tuple(
                HookGiven(name=hook.name, veto=hook.veto, planning=isinstance(hook, Plans))
                for hook in hooks
            ),
            logged=True,
            folding=options.get("folds") is not None,
            model=model.model,
            cache_key=model.cache_identity,
            max_output=model.max_output,
        )
        owner = CURRENT.get()
        if owner is None:
            raise TransportError("denied", "confined attempt has no durable ownership")
        prepared = Prepared(
            given, owner, toolbox, hooks, log, options.get("folds"), options.get("on_tool")
        )
        self.attempts[identity] = prepared
        try:
            result = typed(
                RunReply, await self.channel.request(Capability.RUN, payload(given), timeout=86400)
            )
            if result.attempt != identity:
                raise TransportError("conflict", "runtime returned another attempt's receipt")
            if result.interrupted:
                raise asyncio.CancelledError
            if result.state is None:
                raise TransportError("conflict", "runtime returned no terminal receipt")
            run = prepared.sync(result.state)
            run.summaries.update(result.summaries)
            for hook in hooks:
                if (summary := hook.summary()) is not None:
                    run.summaries[hook.name] = summary
            return run
        except asyncio.CancelledError:
            stop = options.get("stop")
            await owned(
                self.channel.request(
                    Capability.CANCEL,
                    payload(
                        CancelRequest(attempt=identity, explicit=stop is not None and stop.is_set())
                    ),
                    timeout=1800,
                )
            )
            raise
        finally:
            await owned(prepared.close())
            self.attempts.pop(identity, None)
