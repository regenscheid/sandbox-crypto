# SPDX-License-Identifier: MIT
"""Explicit control wire shapes. Scope is derived from authenticated capabilities.

The attempt identifier selects only an entry in the connection's own capability
registry. It never accepts an ExecutionContext, a path, a repository or a method.
Historical collections stay at their owning store; state carries counters and the
current bounded view, rather than all prior usage/tool/message records.
"""

from __future__ import annotations

import base64
from dataclasses import asdict
from typing import Annotated, Any, Literal, cast

from pydantic import Field, JsonValue, StringConstraints, TypeAdapter

from sandbox_science.contracts.model import Contract, Counter, Identifier
from sandbox_science.orchestrator.hooks import Effect, Notice, Point
from sandbox_science.orchestrator.memory_prompt import MemoryGiven
from sandbox_science.orchestrator.model import (
    AssistantMessage,
    Message,
    ModelTurn,
    ToolCall,
    ToolSpec,
)
from sandbox_science.orchestrator.pricing import CostSource
from sandbox_science.orchestrator.projects import FailurePhase, ModelUsage
from sandbox_science.orchestrator.prompts import PromptPins, Section
from sandbox_science.orchestrator.skills.index import SkillsGiven
from sandbox_science.orchestrator.tools import ToolOutcome

from .transport import CONTROL_PROTOCOL

ModelAlias = Annotated[
    str, StringConstraints(min_length=1, max_length=200, pattern=r"^[^\x00-\x1f\x7f]+$")
]
AttemptId = Annotated[str, Field(pattern=r"^cap-[0-9a-f]{48}$")]
MAX_VIEW_BYTES = 8 * 1024 * 1024


class Hello(Contract):
    protocol: Literal[1] = CONTROL_PROTOCOL
    image_revision: str = Field(min_length=1, max_length=128)


class Ready(Contract):
    protocol: Literal[1] = CONTROL_PROTOCOL
    generation: Identifier
    projects: tuple[Identifier, ...] = Field(min_length=1, max_length=128)
    control_schema: Counter


class Waiting(Contract):
    project: Identifier
    session: Identifier


class Queue(Contract):
    entries: tuple[Waiting, ...] = Field(max_length=128)
    more: bool = False


class Attempt(Contract):
    attempt: AttemptId


class WireMessage(Contract):
    chat: dict[str, JsonValue]
    native_b64: str | None = Field(default=None, max_length=24 * 1024 * 1024)
    native_format: str | None = Field(default=None, max_length=128)
    alias: str | None = Field(default=None, max_length=256)
    model: str | None = Field(default=None, max_length=256)
    gateway_version: str | None = Field(default=None, max_length=256)
    prefix: str | None = Field(default=None, max_length=128)
    adapter_state: dict[str, JsonValue] | None = None

    @classmethod
    def of(cls, message: Message) -> WireMessage:
        if not isinstance(message, AssistantMessage):
            return cls(chat=message)
        return cls(
            chat=message,
            native_b64=base64.b64encode(message.native).decode() if message.native else None,
            native_format=message.native_format,
            alias=message.alias,
            model=message.model,
            gateway_version=message.gateway_version,
            prefix=message.prefix,
            adapter_state=cast(dict[str, JsonValue] | None, message.adapter_state),
        )

    def message(self) -> Message:
        if self.chat.get("role") != "assistant":
            return dict(self.chat)
        result = AssistantMessage(
            cast(str | None, self.chat.get("content")),
            native=base64.b64decode(self.native_b64, validate=True) if self.native_b64 else None,
            native_format=self.native_format,
            alias=self.alias,
            model=self.model,
            gateway_version=self.gateway_version,
            prefix=self.prefix,
            adapter_state=self.adapter_state,
        )
        result.update(self.chat)
        return result


class WireTurn(Contract):
    """Native gateway bytes are explicitly base64, never Python serialization."""

    text: str | None
    tool_calls: tuple[ToolCall, ...] = ()
    finish_reason: str | None = None
    model: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost: float | None = None
    cost_source: CostSource | None = None
    retried: tuple[str, ...] = ()
    native_format: str | None = None
    gateway_version: str | None = None
    alias: str | None = None
    prefix: str | None = None
    adapter_state: dict[str, JsonValue] | None = None
    thinking_replay: str | None = None
    notes: tuple[str, ...] = ()
    content: str | None = None
    native_b64: str | None = Field(default=None, max_length=24 * 1024 * 1024)

    @classmethod
    def of(cls, turn: ModelTurn) -> WireTurn:
        fields = asdict(turn)
        native = fields.pop("native")
        return cls.model_validate(
            {
                **fields,
                "native_b64": base64.b64encode(native).decode() if native is not None else None,
            }
        )

    def turn(self) -> ModelTurn:
        return TypeAdapter(ModelTurn).validate_python(
            {
                **self.model_dump(exclude={"native_b64"}),
                "native": base64.b64decode(self.native_b64, validate=True)
                if self.native_b64 is not None
                else None,
            }
        )


class HookGiven(Contract):
    name: str = Field(min_length=1, max_length=128)
    veto: bool = False
    planning: bool = False


class ToolboxGiven(Contract):
    specs: tuple[ToolSpec, ...] = Field(max_length=1024)
    revision: Counter
    output_chars: int = Field(strict=True, ge=1, le=1_000_000)
    planning: bool = False
    skills: SkillsGiven | None = None
    image_offer: dict[str, JsonValue] | None = None


class RunGiven(Attempt):
    run_id: Identifier
    task: str | None = Field(max_length=MAX_VIEW_BYTES)
    history: tuple[WireMessage, ...]
    max_turns: int = Field(strict=True, ge=1, le=100_000)
    max_input_chars: int = Field(strict=True, ge=1, le=MAX_VIEW_BYTES)
    system: str | Section
    research_template: str | None = None
    role: str = Field(min_length=1, max_length=128)
    memory: MemoryGiven | None = None
    toolbox: ToolboxGiven
    hooks: tuple[HookGiven, ...] = Field(max_length=128)
    logged: bool
    folding: bool
    model: ModelAlias
    cache_key: str | None = Field(default=None, max_length=256)
    max_output: int | None = Field(default=None, ge=1, le=1_000_000)


class LoopState(Contract):
    """Mutable loop metadata; historical payloads are never included."""

    system: WireMessage
    turns: Counter
    max_turns: Counter
    max_input_chars: Counter
    tools_chars: Counter
    usage_count: Counter
    last_usage: ModelUsage | None
    message_count: Counter
    tool_count: Counter
    delivered: dict[str, tuple[str, ...]]
    offered: dict[str, tuple[str, ...]]
    final_text: str | None = Field(default=None, max_length=MAX_VIEW_BYTES)
    answer: str | None = Field(default=None, max_length=MAX_VIEW_BYTES)
    pending: str | None = Field(default=None, max_length=MAX_VIEW_BYTES)
    room: bool
    response_rounds: Counter
    held: Counter
    stopped: str = Field(min_length=1, max_length=128)
    halted: str | None = Field(default=None, max_length=128)
    failure: FailurePhase | None
    phase: FailurePhase
    base: Counter
    since: Counter
    overflowed: bool
    prompt_provenance: PromptPins | None


class HookRequest(Attempt):
    slot: int = Field(strict=True, ge=0, lt=128)
    state: LoopState | None = None
    point: Point | None = None
    effect: Effect | None = None
    answer: str | None = Field(default=None, max_length=MAX_VIEW_BYTES)
    stopped: str | None = Field(default=None, max_length=128)
    record: dict[str, JsonValue] | None = None


class ToolRequest(Attempt):
    call: ToolCall
    operation: str = Field(min_length=1, max_length=256)
    model: str | None = Field(default=None, max_length=256)


class ModelRequest(Contract):
    model: ModelAlias
    max_output: int | None = Field(default=None, ge=1, le=1_000_000)
    max_response_bytes: int = Field(default=16 * 1024 * 1024, ge=1, le=64 * 1024 * 1024)
    tool_choice: Literal["auto", "none", "required"] = "auto"
    cache_key: str | None = Field(default=None, max_length=256)
    messages: tuple[WireMessage, ...]
    tools: tuple[ToolSpec, ...] = Field(max_length=1024)


class FoldRequest(Attempt):
    state: LoopState


class FoldReply(Contract):
    proceed: bool
    rebuilt: tuple[WireMessage, ...] | None
    base: Counter
    since: Counter


class LogTool(ToolRequest):
    pass


class LogAppend(Attempt):
    message: WireMessage
    kind: Literal["task", "notice", "assistant", "tool_result"]
    notice: Notice | None = None
    reply: WireTurn | None = None
    outcome: ToolOutcome | None = None
    call: ToolCall | None = None
    operation_id: str | None = Field(default=None, max_length=256)
    consumed: dict[str, tuple[str, ...]] | None = None
    usage: ModelUsage | None = None
    usage_index: Counter | None = None


def payload(value: Contract) -> JsonValue:
    return cast(JsonValue, value.model_dump(mode="json"))


def typed[T](kind: type[T], value: JsonValue) -> T:
    """Validate before a named capability's handler observes its arguments."""
    return TypeAdapter(kind).validate_python(value)


def json_value(value: Any) -> JsonValue:
    """Reject non-JSON host values, instead of invoking an arbitrary encoder."""
    return TypeAdapter(JsonValue).validate_python(value)


class ModelReply(Contract):
    turn: WireTurn | None = None
    error: Literal["context_limit", "gateway_budget", "model"] | None = None
    message: str = Field(default="", max_length=512)
    status: int | None = Field(default=None, ge=100, le=599)
    cost: float | None = Field(default=None, ge=0)


class ModelInfo(Contract):
    kind: Literal["aliases", "tool_calling", "image_input", "input_limits"]


class ToolReply(Contract):
    outcome: ToolOutcome
    toolbox: ToolboxGiven


class AppendReply(Contract):
    message: WireMessage
    retained: dict[str, JsonValue] | None = None


class RunReply(Attempt):
    state: LoopState | None = None
    summaries: dict[str, JsonValue] = Field(default_factory=dict[str, JsonValue])
    interrupted: bool = False


class CancelRequest(Attempt):
    explicit: bool


class Consumed(Contract):
    ids: tuple[Identifier, ...] = Field(max_length=8192)


class EmbeddingRequest(Contract):
    alias: ModelAlias
    texts: tuple[Annotated[str, Field(max_length=100_000)], ...] = Field(
        min_length=1, max_length=64
    )


class EmbeddingReply(Contract):
    model: ModelAlias | None = None
    dimensions: int = Field(default=0, ge=0, le=16384)
    vectors: tuple[tuple[float, ...], ...] = Field(default=(), max_length=64)
    cost: float | None = None
    paid: tuple[tuple[str, int, float | None], ...] = Field(default=(), max_length=64)
    error: str | None = Field(default=None, max_length=512)
