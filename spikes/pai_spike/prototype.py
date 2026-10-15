# SPDX-License-Identifier: MIT
"""Step 5: a minimal Pydantic AI agent on our pieces.

- The model is our LiteLLM gateway (here the fake), through Pydantic AI's OpenAI chat
  model with the SDK's retries off; ``GatewayModel`` keeps each call's exact request
  body and the gateway's cost.
- The tools are our ``Toolbox``'s (``shell``, ``write_file``, ``read_file``), run
  through the worker API with our operation IDs; recorded lab tools answer from the
  recording, so a long session can be replayed into the same agent.
- Harness ``TieredCompaction`` keeps each request under a target, and
  ``StepPersistence`` with ``ConversationSearch`` lets the model find a cleared detail.
- The run record has every call's exact body, every compaction, and the tool records.

Evidence, not product code: nothing here is wired into the harness.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from openai import AsyncOpenAI
from pydantic_ai import Agent, RunContext, UsageLimits
from pydantic_ai.messages import ModelMessage, ToolReturnPart
from pydantic_ai.models import ModelRequestContext
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.litellm import LiteLLMProvider
from pydantic_ai.tools import Tool
from pydantic_ai.toolsets import FunctionToolset
from pydantic_ai_harness import (
    ClampOversizedMessages,
    ClearToolResults,
    ConversationSearch,
    StepPersistence,
    SummarizingCompaction,
    TieredCompaction,
)
from pydantic_ai_harness.conversation_search import SnapshotHistorySource
from pydantic_ai_harness.step_persistence import InMemoryStepStore

from sandbox_science.orchestrator.model import ToolCall
from sandbox_science.orchestrator.tools import Toolbox, operation_id

from .gateway import GatewayRecorder, gateway_client

REAL_TOOLS = ("shell", "write_file", "read_file")
RECOVERY = frozenset({"search_conversation_history"})
CLEARED = "[tool result cleared]"


def gateway_model(url: str, alias: str, key: str, recorder: GatewayRecorder) -> Any:
    client = AsyncOpenAI(
        base_url=f"{url}/v1", api_key=key, http_client=gateway_client(), max_retries=0
    )
    return recorder.wrap(OpenAIChatModel(alias, provider=LiteLLMProvider(openai_client=client)))


def _size(messages: Sequence[ModelMessage]) -> tuple[int, int]:
    """(estimated tokens, cleared tool results) of a history: its text and tool arguments
    at four characters a token, as the harness estimates."""
    text = sum(
        len(str(getattr(p, "content", None) or getattr(p, "args", None) or ""))
        for m in messages
        for p in getattr(m, "parts", ())
    )
    cleared = sum(
        1
        for m in messages
        for p in getattr(m, "parts", ())
        if isinstance(p, ToolReturnPart) and p.content == CLEARED
    )
    return text // 4, cleared


@dataclass
class RunRecord:
    """What the run sent and did: each model call's exact body, each compaction, each tool
    call's record. ``write`` saves it as JSON."""

    run_id: str
    calls: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])
    compactions: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])
    tools: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])

    def take_calls(self, recorder: GatewayRecorder) -> None:
        for n, call in enumerate(recorder.calls):
            body = call.sent[-1] if call.sent else None
            self.calls.append(
                {
                    "n": n,
                    "role": "summary" if body and body.get("model") == "summarizer" else "main",
                    "sent": body,
                    "attempts": len(call.sent),
                    "statuses": call.statuses,
                    "cost": call.cost,
                    "model": call.model_name,
                    "input_tokens": call.input_tokens,
                    "output_tokens": call.output_tokens,
                }
            )

    def write(self, path: Path) -> None:
        path.write_text(json.dumps(
            {"run_id": self.run_id, "calls": self.calls, "compactions": self.compactions,
             "tools": self.tools}, indent=1,
        ))  # fmt: skip


@dataclass
class RecordedCompaction(TieredCompaction[Any]):
    """``TieredCompaction`` that writes each compaction it makes into the run record."""

    record: RunRecord | None = None
    requests: int = 0

    async def before_model_request(
        self, ctx: RunContext[Any], request_context: ModelRequestContext
    ) -> ModelRequestContext:
        self.requests += 1
        before = _size(ctx.messages)
        count = len(ctx.messages)
        result = await super().before_model_request(ctx, request_context)
        after = _size(ctx.messages)
        if self.record is not None and (after != before or len(ctx.messages) != count):
            self.record.compactions.append(
                {
                    "request": self.requests,
                    "run_step": ctx.run_step,
                    "messages": [count, len(ctx.messages)],
                    "estimated_tokens": [before[0], after[0]],
                    "cleared_tool_results": [before[1], after[1]],
                }
            )
        return result


def compaction(target: int, summarizer: Any, record: RunRecord) -> list[Any]:
    store = InMemoryStepStore()
    return [
        StepPersistence(store=store),
        ConversationSearch(SnapshotHistorySource(store), scope="conversation"),
        RecordedCompaction(
            tiers=[
                ClampOversizedMessages(max_part_tokens=20_000),
                ClearToolResults(max_tokens=1, keep_pairs=3, exclude_tools=RECOVERY),
                SummarizingCompaction(model=summarizer, max_messages=1, keep_messages=20),
            ],
            target_tokens=target,
            record=record,
        ),
    ]


class OurTools(FunctionToolset[None]):
    """Our ``Toolbox``'s tools as Pydantic AI tools, beside the recorded lab tools.

    A live call runs through ``Toolbox.call`` with our operation ID, so the worker sees
    exactly what our loop would send; a replayed call returns its recorded result."""

    def __init__(
        self,
        toolbox: Toolbox,
        run_id: str,
        record: RunRecord,
        recorded: dict[str, str],
        replay_names: Sequence[str],
    ) -> None:
        self.toolbox, self.run_id, self.record, self.recorded = toolbox, run_id, record, recorded
        specs = {spec.name: spec for spec in toolbox.specs}
        tools = [
            Tool.from_schema(
                self._runner(name), name, specs[name].description, specs[name].parameters,
                takes_ctx=True,
            )
            for name in REAL_TOOLS
        ]  # fmt: skip
        loose = {"type": "object", "additionalProperties": True}
        tools += [
            Tool.from_schema(self._runner(n), n, f"The recorded {n}.", loose, takes_ctx=True)
            for n in replay_names
            if n not in REAL_TOOLS
        ]
        super().__init__(tools)

    def _runner(self, name: str) -> Any:
        async def run(ctx: RunContext[None], **arguments: Any) -> str:
            call_id = ctx.tool_call_id or ""
            if call_id in self.recorded:
                return self.recorded[call_id]
            call = ToolCall(call_id, name, json.dumps(arguments))
            outcome = await self.toolbox.call(
                call, operation_id(f"{self.run_id}-t{ctx.run_step}", call)
            )
            self.record.tools.append({"tool": name, "arguments": arguments, **outcome.record})
            return outcome.text

        return run


def agent(
    url: str,
    key: str,
    toolbox: Toolbox,
    system: str,
    record: RunRecord,
    recorder: GatewayRecorder,
    recorded: dict[str, str],
    replay_names: Sequence[str],
    *,
    target: int,
) -> Agent[None, str]:
    summarizer = gateway_model(url, "summarizer", key, recorder)
    return Agent(
        gateway_model(url, "sandbox-orchestrator", key, recorder),
        instructions=system,
        toolsets=[OurTools(toolbox, record.run_id, record, recorded, replay_names)],
        capabilities=compaction(target, summarizer, record),
    )


LIMITS = UsageLimits(request_limit=200)
