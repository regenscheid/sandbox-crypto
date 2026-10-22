# SPDX-License-Identifier: MIT
"""The shared owner of an agent's working conversation.

The root and delegated loops use the same state and archive-first operations. Run
policy (turns, accepted answers, deliveries and stop reasons) stays in the loop.
The backing archive retains full history independently of the folded model view.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, MutableSequence, Sequence
from collections.abc import Set as AbstractSet
from dataclasses import dataclass, field, replace
from typing import Final

from sandbox_science.orchestrator.hooks import Appended, MessageLog
from sandbox_science.orchestrator.images import IMAGE
from sandbox_science.orchestrator.model import (
    AssistantMessage,
    ChatModel,
    Message,
    ModelTurn,
    ToolSpec,
)
from sandbox_science.orchestrator.projects import ModelUsage
from sandbox_science.orchestrator.prompts import PromptPins
from sandbox_science.orchestrator.worker_client import dump

from .folding import FoldPoint, Folds
from .history.capture import prompt_pins
from .history.collections import Collection
from .history.limits import WorkingMemoryLimitError
from .history.log import RunArchive
from .history.memory import VIEW_BYTES, RunMemory

MAX_INPUT_CHARS: Final = 400_000
"""Default character budget including tool schemas, about 100,000 English tokens."""


@dataclass
class AgentContext:
    messages: MutableSequence[Message]
    prompt_provenance: PromptPins | None = None
    max_input_chars: int = MAX_INPUT_CHARS
    tools_chars: int = 0
    usage: MutableSequence[ModelUsage] = field(default_factory=list[ModelUsage])
    log: MessageLog | None = field(default=None, repr=False)
    folds: Folds | None = field(default=None, repr=False)
    rebuilt: list[Message] | None = field(default=None, repr=False)
    base: int = field(default=0, repr=False)
    since: int = 0
    memory: RunMemory | None = field(default=None, repr=False)

    @classmethod
    def start(
        cls,
        messages: Sequence[Message],
        *,
        prompt_provenance: PromptPins | None = None,
        max_input_chars: int = MAX_INPUT_CHARS,
        tools_chars: int = 0,
        log: MessageLog | None = None,
        memory: RunMemory | None = None,
        folds: Folds | None = None,
    ) -> AgentContext:
        """Initialize one attempt against its archive or confined scratch store.

        Store lifetime belongs to the caller. Archived stores retain references,
        not repeated copies of the full transcript, across folded working views.
        """
        if memory is None and isinstance(log, RunArchive):
            memory = RunMemory.archived(log)
        return cls(
            messages=memory.messages(messages) if memory is not None else list(messages),
            prompt_provenance=prompt_provenance,
            max_input_chars=min(max_input_chars, VIEW_BYTES)
            if memory is not None
            else max_input_chars,
            tools_chars=tools_chars,
            usage=memory.usage() if memory is not None else [],
            log=log,
            folds=folds,
            memory=memory,
        )

    def working(self) -> Iterator[Message]:
        if self.rebuilt is None:
            yield from self.messages
        else:
            yield self.messages[0]
            yield from self.rebuilt
            yield from self.messages[self.base :]

    @property
    def sent(self) -> list[Message]:
        """What the next model call sends: the system message, the view a fold rebuilt,
        if one did, and the messages added since."""
        kept: list[Message] = []
        used = 0
        for message in self.working():
            if isinstance(message, AssistantMessage):
                message = message.replay()
            used += len(dump(message).encode()) + 2
            if isinstance(message, AssistantMessage):
                used += len(message.native or b"")
                used += len(dump(message.adapter_state or {}).encode())
                used += len((message.prefix or "").encode())
            if self.memory is not None and used > VIEW_BYTES:
                raise WorkingMemoryLimitError(
                    "active model payload exceeds the working-state ceiling; "
                    "original evidence remains stored"
                )
            kept.append(message)
        return kept

    def rebuild(self, view: Sequence[Message]) -> None:
        """Send ``view`` after the system message from now on, in place of everything
        the conversation holds now (a fold, CM-24): a rebuild, after which the view's
        size is estimated afresh until a call reports (CM-28)."""
        self.rebuilt, self.base, self.since = list(view), len(self.messages), len(self.usage)

    @property
    def size(self) -> int:
        """What the next model call would send, in characters."""
        return 2 + sum(len(dump(message)) + 2 for message in self.working()) + self.tools_chars

    def restore(self, view: Sequence[Message] | None, *, base: int, since: int) -> None:
        """Apply a trusted fold result atomically without changing archive scope."""
        self.rebuilt, self.base, self.since = (
            (list(view) if view is not None else None),
            base,
            since,
        )

    async def before_call(self, *, call_index: int) -> bool:
        """Prepare the working view at a safe point; execution owns the call index."""
        return self.folds is None or await self.folds.before_call(FoldPoint(self, call_index))

    async def overflow(self, *, call_index: int) -> bool:
        """Recover a refused context without replaying tools or changing run policy."""
        return self.folds is not None and await self.folds.overflow(FoldPoint(self, call_index))

    async def complete(self, model: ChatModel, specs: Sequence[ToolSpec]) -> ModelTurn:
        """Send the current view with its prompt pins.

        Failed/refused dispatches are accounted by the gateway admission journal;
        they do not become answered responses in this conversation's usage list.
        """
        with prompt_pins(self.prompt_provenance):
            return await model.complete(self.sent, specs)

    def record_reply(self, reply: ModelTurn, *, input_size: int) -> None:
        """Retain usage and bounded replay diagnostics for an answered response."""
        answered = len(reply.text or "") + sum(len(call.arguments) for call in reply.tool_calls)
        self.usage.append(
            ModelUsage(
                model=reply.model,
                input_tokens=reply.input_tokens,
                output_tokens=reply.output_tokens,
                cache_read_input_tokens=reply.cache_read_input_tokens,
                cache_creation_input_tokens=reply.cache_creation_input_tokens,
                characters=input_size + answered,
                cost=reply.cost,
                cost_source=reply.cost_source,
                gateway_version=reply.gateway_version,
                thinking_replay=(reply.thinking_replay or "")[:300] or None,
                retried=list(reply.retried[:16]),
            )
        )

    async def append(
        self,
        message: Message,
        appended: Appended,
        *,
        consumed: Mapping[str, AbstractSet[str]] | None = None,
    ) -> None:
        """Add a message to the conversation, kept first by the run's log if it has one."""
        image = message.get(IMAGE)
        if self.log is not None:
            if appended.kind == "assistant":
                appended = replace(
                    appended,
                    usage=self.usage[-1],
                    usage_index=len(self.usage),
                    consumed=consumed,
                )
            message = await self.log.append(message, appended)
        if image is not None:
            message = {**message, IMAGE: image}
        if isinstance(self.messages, Collection) and isinstance(self.log, RunArchive):
            self.messages.append_encoded(
                {"archive_message": self.log.last_message, **({IMAGE: image} if image else {})}
            )
        else:
            self.messages.append(message)
