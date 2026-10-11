# SPDX-License-Identifier: MIT
"""The tool loop: the model asks for tools, the worker runs them, until the model answers."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Final

from .model import ChatModel, Message
from .tools import Toolbox, ToolOutcome, operation_id

SYSTEM_PROMPT: Final = (
    "You are a research assistant working in a sandboxed computing environment. Every tool "
    "runs inside the project's sandbox, never on the machine that hosts this conversation. "
    "Use python for computations whose state should persist between calls, shell for "
    "commands, and the file tools to inspect and share files. Publish files that are results. "
    "Report only what the tools actually returned. Network access follows the project's "
    "policy: the first connection to a site the project has not used before can fail with "
    "'Permission denied' while the platform admits the site, which takes up to half a minute, "
    "so wait and retry before concluding that a site is blocked; a later tool result's "
    "platform_notices say when a site was admitted or refused. A request that needs "
    "approval fails with approval_required until someone approves it."
)


@dataclass
class Transcript:
    messages: list[Message]
    tool_records: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])
    final_text: str | None = None
    turns: int = 0
    stopped: str = "answered"
    """``answered``, or ``turn_limit`` when the model was still asking for tools."""


async def run_task(
    model: ChatModel,
    toolbox: Toolbox,
    task: str,
    *,
    run_id: str,
    max_turns: int = 30,
    system: str = SYSTEM_PROMPT,
    on_tool: Callable[[str, str, ToolOutcome], Awaitable[None]] | None = None,
) -> Transcript:
    """Run ``task`` to an answer. ``run_id`` prefixes every worker operation ID."""
    transcript = Transcript(
        messages=[{"role": "system", "content": system}, {"role": "user", "content": task}]
    )
    for turn in range(max_turns):
        transcript.turns = turn + 1
        reply = await model.complete(transcript.messages, toolbox.specs)
        assistant: Message = {"role": "assistant", "content": reply.text}
        if reply.tool_calls:
            assistant["tool_calls"] = [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {"name": call.name, "arguments": call.arguments},
                }
                for call in reply.tool_calls
            ]
        transcript.messages.append(assistant)
        if not reply.tool_calls:
            transcript.final_text = reply.text
            return transcript
        for call in reply.tool_calls:
            outcome = await toolbox.call(call, operation_id(f"{run_id}-t{turn}", call))
            transcript.messages.append(
                {"role": "tool", "tool_call_id": call.id, "content": outcome.text}
            )
            transcript.tool_records.append(
                {"tool": call.name, "arguments": call.arguments, **outcome.record}
            )
            if on_tool is not None:
                await on_tool(call.name, call.arguments, outcome)
    transcript.stopped = "turn_limit"
    return transcript
