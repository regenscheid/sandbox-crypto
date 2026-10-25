# SPDX-License-Identifier: MIT
"""Trusted per-attempt capability handlers; no agent loop runs in this service."""

from __future__ import annotations

import asyncio
import copy
import logging
import secrets
from collections.abc import AsyncGenerator, Awaitable, Callable, Mapping, Sequence
from contextlib import AbstractAsyncContextManager, AsyncExitStack, asynccontextmanager
from typing import Any, cast

from pydantic import JsonValue, TypeAdapter

from sandbox_science.orchestrator.aio import owned, threaded
from sandbox_science.orchestrator.context.agent import AgentContext
from sandbox_science.orchestrator.context.folding import FoldPoint, Folds
from sandbox_science.orchestrator.context.history.collections import Collection
from sandbox_science.orchestrator.context.history.log import RunArchive
from sandbox_science.orchestrator.context.history.retention import fits
from sandbox_science.orchestrator.continuity.models import ModelIntents
from sandbox_science.orchestrator.continuity.records import (
    CURRENT,
    Ownership,
    authority,
    check_owner,
)
from sandbox_science.orchestrator.control import ControlError, ControlStore
from sandbox_science.orchestrator.hooks import (
    Appended,
    Closing,
    MessageLog,
    Notice,
    TurnHook,
    wraps_calls,
)
from sandbox_science.orchestrator.images import IMAGE
from sandbox_science.orchestrator.kernel_check import KernelCheck
from sandbox_science.orchestrator.loop import (
    Transcript,
    attach_notices,
    not_run,
    notice_message,
)
from sandbox_science.orchestrator.mcp_direct import RequestCheck
from sandbox_science.orchestrator.model import AssistantMessage, ChatModel, ToolCall
from sandbox_science.orchestrator.model_profiles import PROFILES, ModelProfile, ModelRole, profiles
from sandbox_science.orchestrator.notices import DELEGATIONS, REPORT
from sandbox_science.orchestrator.plan_tools import Plans
from sandbox_science.orchestrator.purpose import without
from sandbox_science.orchestrator.tools import Toolbox, ToolOutcome, operation_id

from .models import CallbackModel
from .requests import confirm
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
    ModelIntent,
    ModelObserved,
    ModelTicket,
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

_log = logging.getLogger(__name__)


def remote_outcome(outcome: ToolOutcome) -> ToolOutcome:
    """Send bounded control-flow metadata while full evidence stays with its host owner."""
    record = (
        outcome.record
        if fits(outcome.record, 65536)
        else {
            "transport_preview": True,
            **({"error": str(outcome.record["error"])[:256]} if "error" in outcome.record else {}),
        }
    )
    return ToolOutcome(outcome.text, record, whole=outcome.whole, image=outcome.image)


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
        self.model_profiles = dict(PROFILES.get() or {})
        self.log, self.folds, self.on_tool = log, folds, on_tool
        self.leases: dict[int, AbstractAsyncContextManager[None]] = {}
        self.shadow: Transcript | None = None
        self.task_pending = given.task is not None
        self.tool: ToolOutcome | None = None
        self.tool_call: ToolRequest | None = None
        self.tool_notices: list[Notice] = []
        self.notices: list[Notice] = []
        self.images: list[tuple[Notice, str]] = []
        self.observed_tool: tuple[ToolCall, ToolOutcome] | None = None
        self.pending_calls: dict[str, ToolCall] = {}
        self.intent: LogTool | None = None
        self.observation: dict[str, Any] | None = None
        self.observed_slots: set[int] = set()
        self._memory_close: Callable[[], None] | None = None
        self._lock = asyncio.Lock()
        self._closing = False
        self._child_wait = False
        self._passive_wait: asyncio.Task[Any] | None = None
        self._close_task: asyncio.Task[None] | None = None

    def sync(self, state: LoopState) -> Transcript:
        """Apply loop metadata while retaining historical payloads at the host store."""
        given = self.given
        if (state.max_turns, state.max_input_chars) != (given.max_turns, given.max_input_chars):
            raise TransportError("denied", "runtime changed the admitted attempt limits")
        if self.shadow is None:
            from sandbox_science.orchestrator.context.history.memory import RunMemory

            if not isinstance(self.log, RunArchive):
                raise TransportError(
                    "internal", "confined attempts require canonical archive storage"
                )
            memory = RunMemory.archived(self.log)
            self.shadow = Transcript(
                context=AgentContext(
                    memory=memory,
                    messages=memory.messages(
                        [state.system.message(), *(m.message() for m in given.history)]
                    ),
                    usage=memory.usage(),
                ),
                tool_records=memory.tools(),
            )
        run = self.shadow
        if (
            len(run.context.messages) != state.message_count
            or len(run.tool_records) != state.tool_count
        ):
            raise TransportError("conflict", "runtime state does not match committed events")
        if state.usage_count == len(run.context.usage) + 1 and state.last_usage is not None:
            run.context.usage.append(state.last_usage)
        if len(run.context.usage) != state.usage_count:
            raise TransportError("conflict", "runtime usage is not contiguous")
        omitted = {
            "system",
            "message_count",
            "tool_count",
            "usage_count",
            "last_usage",
            "max_input_chars",
            "tools_chars",
            "base",
            "since",
        }
        for name, value in state.model_dump(exclude=omitted).items():
            if name not in {"delivered", "offered", "prompt_provenance"}:
                setattr(run, name, value)
        if state.delivered or state.offered:
            raise TransportError("invalid_request", "historical receipts do not cross loop state")
        run.context.max_input_chars = state.max_input_chars
        run.context.tools_chars = state.tools_chars
        run.context.restore(run.context.rebuilt, base=state.base, since=state.since)
        run.context.prompt_provenance = state.prompt_provenance
        return run

    async def call(self, kind: Capability, value: JsonValue) -> JsonValue:
        """Restore captured epoch authority; a caller supplies neither scope nor epoch."""
        with authority(self.owner), profiles(self.model_profiles):
            if not isinstance(self.log, RunArchive):
                raise TransportError("internal", "missing canonical attempt archive")
            with self.log.archive.control.read() as tx:
                check_owner(tx.sql, self.owner)
            return await self._call(kind, value)

    async def _call(self, kind: Capability, value: JsonValue) -> JsonValue:
        async with self._active(kind, value):
            if kind is Capability.LOG_INIT:
                self.sync(typed(FoldRequest, value).state)
                return None
            if kind is Capability.LOG_APPEND:
                return await self.append(typed(LogAppend, value))
            if kind is Capability.LOG_TOOL:
                request = typed(LogTool, value)
                if self.shadow is None or self.pending_calls.get(request.call.id) != request.call:
                    raise TransportError("conflict", "tool intent has no committed model call")
                expected = operation_id(
                    f"{self.given.run_id}-t{self.shadow.turns - 1}", request.call
                )
                if request.operation != expected or self.intent is not None:
                    raise TransportError("conflict", "tool intent identity differs or is pending")
                await self.log.start_tool(request.call, request.operation)
                self.intent = request
                return None
            if kind is Capability.TOOL:
                request = typed(ToolRequest, value)
                if (
                    self.tool is not None
                    or self.intent is None
                    or (self.intent.call, self.intent.operation)
                    != (request.call, request.operation)
                ):
                    raise TransportError(
                        "conflict", "tool dispatch has no matching unexecuted intent"
                    )
                self.tool_notices = []
                # Original authority, offered tools and trusted gates come from preparation.
                outcome = await self.toolbox.call(
                    request.call, request.operation, model=request.model, gates=self.hooks
                )
                self.tool, self.tool_call = outcome, request
                return payload(
                    ToolReply(outcome=remote_outcome(outcome), toolbox=toolbox_given(self.toolbox))
                )
            if kind in (Capability.FOLD_BEFORE, Capability.FOLD_OVERFLOW):
                request = typed(FoldRequest, value)
                run = self.sync(request.state)
                if self.folds is None:
                    raise TransportError("denied", "this attempt has no folding capability")
                proceed = await (
                    self.folds.before_call(FoldPoint(run.context, run.turns))
                    if kind is Capability.FOLD_BEFORE
                    else self.folds.overflow(FoldPoint(run.context, run.turns))
                )
                return payload(
                    FoldReply(
                        proceed=proceed,
                        rebuilt=tuple(WireMessage.of(m) for m in run.context.rebuilt)
                        if run.context.rebuilt is not None
                        else None,
                        base=run.context.base,
                        since=run.context.since,
                    )
                )
            if kind is Capability.TOOL_DONE:
                typed(Attempt, value)
                if self.observed_tool is None:
                    raise TransportError("conflict", "no completed tool observation")
                call, outcome = self.observed_tool
                if self.on_tool is not None:
                    await self.on_tool(call.name, without(call.arguments), outcome)
                self.observed_tool, self.observation = None, None
                return None
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
                ids = tuple(await hook.consumption())
                if self.shadow is not None:
                    self.shadow.identities(hook.name).update(ids)
                return payload(Consumed(ids=ids))
            if request.state is None:
                raise TransportError("invalid_request", "hook requires committed loop state")
            run = self.sync(request.state)
            if kind is Capability.NOTICES:
                if request.point is None:
                    raise TransportError("invalid_request", "notices require a safe point")
                try:
                    result = await hook.notices(request.point, run)
                except Exception:
                    _log.error(
                        "notice hook failed: slot=%d type=%s point=%s",
                        request.slot,
                        type(hook).__name__,
                        request.point,
                    )
                    raise
                if request.point == "tool":
                    self.tool_notices.extend(result)
                else:
                    self.notices.extend(result)
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
                if (
                    self.observation is None
                    or self.observed_tool is None
                    or request.slot in self.observed_slots
                ):
                    raise TransportError("conflict", "no unobserved committed tool result")
                tool_record = (
                    self.observation
                    if isinstance(hook, KernelCheck)
                    else self.observed_tool[1].record
                )
                await hook.observe_tool(run, tool_record)
                self.observed_slots.add(request.slot)
            elif kind is Capability.CLOSING:
                closing = await hook.closing(run, request.answer)
                self.notices.extend(closing.notices)
                return json_value(TypeAdapter(Closing).dump_python(closing, mode="json"))
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
        if self.shadow is None:
            raise TransportError("conflict", "loop state has not been initialized")
        call, outcome, operation = request.call, request.outcome, request.operation_id
        source = request.message.message()
        if request.kind == "assistant":
            if request.reply is None:
                raise TransportError("invalid_request", "assistant append requires its model turn")
            source = AssistantMessage.of(request.reply.turn())
        if request.kind == "task":
            if not self.task_pending or self.given.task is None:
                raise TransportError("conflict", "this attempt has no uncommitted task")
            source = {"role": "user", "content": self.given.task}
        if request.kind == "notice":
            notice = request.notice
            if notice is None:
                raise TransportError("invalid_request", "notice body is absent")
            if notice in self.notices:
                self.notices.remove(notice)
            elif (
                notice.source != "report"
                or notice.ids
                or notice.user
                or notice.framing != REPORT.text
            ):
                raise TransportError("denied", "notice was not returned by trusted capabilities")
            source = notice_message(notice)
            for known, token in tuple(self.images):
                if known == notice:
                    source = {**source, IMAGE: token}
                    self.images.remove((known, token))
                    break
            self.shadow.identities(notice.source).update(notice.ids)
        if request.kind == "tool_result":
            if self.tool_call is None or self.tool is None:
                # A report round may decline a model's tool call without dispatch.
                reason = outcome.record.get("not_run") if outcome is not None else None
                if (
                    self.pending_calls.get(call.id if call is not None else "") != call
                    or operation is not None
                    or call is None
                    or not isinstance(reason, str)
                    or len(reason) > 128
                ):
                    raise TransportError("conflict", "tool result has no trusted execution")
                outcome = not_run(reason)
            else:
                if call != self.tool_call.call or operation != self.tool_call.operation:
                    raise TransportError(
                        "conflict", "tool result differs from its trusted dispatch"
                    )
                call, outcome, operation = (
                    self.tool_call.call,
                    copy.deepcopy(self.tool),
                    self.tool_call.operation,
                )
                attach_notices(self.shadow, outcome, self.toolbox.output_chars, self.tool_notices)
            source = {"role": "tool", "tool_call_id": call.id, "content": outcome.text}
        consumed = (
            {key: set(ids) for key, ids in request.consumed.items()}
            if request.consumed is not None
            else None
        )
        if consumed is not None:
            for key, ids in consumed.items():
                offered = self.shadow.offered.get(key, set())
                if any(identity not in offered for identity in ids):
                    raise TransportError(
                        "denied", "receipt was not offered by trusted capabilities"
                    )
        appended = Appended(
            request.kind,
            notice=request.notice,
            reply=request.reply.turn() if request.reply is not None else None,
            call=call,
            outcome=outcome,
            operation_id=operation,
            consumed=consumed,
            usage=request.usage,
            usage_index=request.usage_index,
        )
        message = await self.log.append(source, appended)
        if IMAGE in source:
            message = {**message, IMAGE: source[IMAGE]}
        if (
            not isinstance(self.shadow.context.messages, Collection)
            or not isinstance(self.log, RunArchive)
            or self.log.last_message is None
        ):
            raise TransportError("internal", "confined history has no canonical archive entry")
        self.shadow.context.messages.append_encoded(
            {
                "archive_message": self.log.last_message,
                **({IMAGE: source[IMAGE]} if IMAGE in source else {}),
            }
        )
        if request.kind == "task":
            self.task_pending = False
        if consumed is not None:
            for key, ids in consumed.items():
                self.shadow.identities(key, delivered=True).update(ids)
        if request.kind == "assistant" and request.reply is not None:
            self.pending_calls = {call.id: call for call in request.reply.tool_calls}
        if request.usage is not None and request.usage_index == len(self.shadow.context.usage) + 1:
            self.shadow.context.usage.append(request.usage)
        retained = None
        if request.kind == "tool_result" and call is not None and outcome is not None:
            record = {"tool": call.name, "arguments": without(call.arguments), **outcome.record}
            retained = self.log.retained_tool(record)
            self.shadow.tool_records.append(retained)
            self.observation, self.observed_slots = record, set()
            self.observed_tool = call, outcome
            if outcome.image is not None:
                self.notices.append(outcome.image[0])
                self.images.append(outcome.image)
            self.pending_calls.pop(call.id, None)
            self.tool = self.tool_call = self.intent = None
            self.tool_notices = []
        return payload(AppendReply(message=WireMessage.of(message), retained=retained))

    @asynccontextmanager
    async def _active(self, kind: Capability, value: JsonValue) -> AsyncGenerator[None]:
        async with self._lock:
            if self._closing:
                raise TransportError("denied", "attempt capability is closing")
            if kind is Capability.TOOL:
                name = typed(ToolRequest, value).call.name
                self._child_wait = name == "wait_delegations"
                if name == "wait_agent_message":
                    self._passive_wait = asyncio.current_task()
            elif kind in (Capability.CLOSING, Capability.FINISHING):
                slot = typed(HookRequest, value).slot
                self._child_wait = (
                    slot < len(self.hooks) and self.hooks[slot].name == DELEGATIONS.id
                )
            try:
                yield
            finally:
                self._child_wait = False
                self._passive_wait = None

    async def close(self) -> None:
        """Drain admitted effects before the harness releases per-attempt resources."""
        self._closing = True
        if self._close_task is None:
            self._close_task = asyncio.create_task(self._close())
        await owned(self._close_task)

    async def _close(self) -> None:
        # This wait neither consumes messages nor owns worker effects. Otherwise
        # a disconnected runtime holds the attempt lock until its 300s timeout.
        # Do not generalize to all reads: job waits can own watcher/recording work.
        if self._passive_wait is not None:
            self._passive_wait.cancel()
        try:
            if self._child_wait:
                # A child wait holds _lock, but its delegation lease owns the child
                # tasks that must settle to release that lock. An active admission
                # cannot take this path: it must finish before the lease snapshots it.
                for slot in reversed(tuple(self.leases)):
                    if self.hooks[slot].name == DELEGATIONS.id:
                        await self.leases.pop(slot).__aexit__(None, None, None)
        finally:
            # One failing lease must not leave the other async generators to
            # GC finalization after their stores/resources have been closed.
            async with self._lock, AsyncExitStack() as exits:
                for slot in tuple(self.leases):
                    exits.push_async_exit(self.leases.pop(slot))


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

    def __init__(
        self,
        channel: Channel,
        projects: frozenset[str],
        *,
        control: ControlStore | None = None,
        model_roles: Mapping[str, frozenset[str]] | None = None,
        run_share: float | None = None,
        requests: RequestCheck | None = None,
    ) -> None:
        """``run_share`` is the part of a configured project cost limit one run may use;
        ``requests`` checks a refusal that names an MCP permission request for an attempt
        (``confirm_request``, D98), for the attempt's project only."""
        self.channel, self.projects = channel, projects
        self.requests = requests
        self.control, self.model_roles = control, model_roles or {}
        self.run_share = run_share
        self.attempts: dict[str, Prepared] = {}
        self.owners: dict[str, Ownership] = {}
        self.profiles: dict[str, dict[ModelRole, ModelProfile]] = {}
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
        self.profiles[identity] = dict(PROFILES.get() or {})
        try:
            yield
        finally:
            self.tokens.pop(key, None)
            self.owners.pop(identity, None)
            self.profiles.pop(identity, None)

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
        if kind in (Capability.MODEL_INTENT, Capability.MODEL_OBSERVED):
            owner = self.owners.get(identity.attempt)
            if owner is None or self.control is None:
                raise TransportError("denied", "model capability is absent or revoked")
            with authority(owner):
                intents = ModelIntents(self.control, run_share=self.run_share)
                if kind is Capability.MODEL_INTENT:
                    request = typed(ModelIntent, value)
                    pinned = self.profiles.get(identity.attempt, {})
                    roles: dict[str, tuple[ModelRole, ...]] = {
                        "main": ("main",),
                        "delegated": ("delegated",),
                        "reviewer": ("reviewer",),
                        "watcher": ("watcher",),
                        "fold": ("fold", "overview"),
                        "skills": ("skills_metadata",),
                    }
                    if request.role is not None and request.role not in roles.get(request.kind, ()):
                        raise TransportError("denied", "model role differs from its paid kind")
                    if request.kind == "embedding":
                        if request.profile is not None:
                            raise TransportError("denied", "embeddings have no reasoning profile")
                    elif pinned and (
                        request.profile is None
                        or request.profile
                        not in (
                            pinned[role]
                            for role in (
                                (request.role,)
                                if request.role is not None
                                else roles.get(request.kind, ())
                            )
                            if role in pinned
                        )
                    ):
                        raise TransportError("denied", "model profile is outside the admitted role")
                    if request.kind in ("main", "delegated"):
                        expected = (
                            "delegated"
                            if prepared is not None and prepared.toolbox.delegated
                            else "main"
                        )
                        if (
                            prepared is None
                            or request.kind != expected
                            or request.alias != prepared.given.model
                            or request.profile != prepared.given.profile
                        ):
                            raise TransportError(
                                "denied", "model dispatch is outside the admitted role"
                            )
                    elif request.alias not in self.model_roles.get(
                        request.kind, frozenset()
                    ) and not (
                        request.kind == "fold"
                        and prepared is not None
                        and request.alias == prepared.given.model
                        and request.role in ("fold", "overview")
                        and request.profile == pinned.get(request.role)
                    ):
                        raise TransportError(
                            "denied", "auxiliary model role is outside configuration"
                        )
                    try:
                        ticket = await threaded(
                            intents.begin,
                            owner,
                            kind=request.kind,
                            alias=request.alias,
                            profile=request.profile,
                            role=request.role,
                            captured=request.captured,
                        )
                    except ControlError as exc:
                        if exc.message.startswith("model_budget:"):
                            raise TransportError("denied", exc.message) from None
                        raise
                    return payload(ModelTicket(identity=ticket))
                observed = typed(ModelObserved, value)
                with self.control.read() as tx:
                    row = tx.sql.execute(
                        "SELECT kind FROM research_model_calls WHERE project=? AND session=? "
                        "AND run=? AND identity=?",
                        (owner.project, owner.session, owner.run, observed.identity),
                    ).fetchone()
                if row is None or row[0] in ("main", "delegated"):
                    raise TransportError(
                        "denied", "main model receipts require the assistant archive transaction"
                    )
                await threaded(intents.observe, owner, observed.identity, observed.usage)
                return None
        if prepared is None:
            raise TransportError("denied", "attempt capability is absent or revoked")
        if kind is Capability.CONFIRM_REQUEST:
            # The attempt's project, never one the runtime names: a yes or a no about one
            # request, read from the middleware's records through the worker.
            return await confirm(self.requests, prepared.owner.project, value)
        return await prepared.call(kind, value)

    async def run(
        self, model: ChatModel, toolbox: Toolbox, task: str | None, **options: Any
    ) -> Transcript:
        """Match Harness.loop_runner; options are its internal run_task keyword arguments."""
        if toolbox.context.project_id not in self.projects:
            raise TransportError("denied", "project is outside this control capability")
        hooks = tuple(cast(Sequence[TurnHook], options.get("hooks") or toolbox.turn_hooks()))
        if any(wraps_calls(hook) for hook in hooks):
            # The runtime's proxies of these hooks carry no wrap: it would be skipped.
            raise TransportError(
                "denied", "a hook that wraps model or tool calls cannot be confined"
            )
        if not isinstance(model, CallbackModel):
            raise TransportError("denied", "host model execution is forbidden in confined mode")
        log = cast(MessageLog | None, options.get("log"))
        if not isinstance(log, RunArchive):
            raise TransportError("denied", "confined attempts require durable archive logging")
        identity = self.model_capability()
        if self.control is None:
            self.control = log.archive.control
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
            profile=model.requested_profile,
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
                RunReply, await self.channel.request(Capability.RUN, payload(given), timeout=None)
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
            try:
                await owned(prepared.close())
            finally:
                self.attempts.pop(identity, None)
