# SPDX-License-Identifier: MIT
"""GPT's part of CM-F31 (docs/specs/context-management.md): the Responses adapter, and
the chat-completions client on the same alias, through the deployment's real gateway.
Marked ``live``, so CI and ordinary runs skip them; a lab runs them with:

- ``SS_LIVE_GATEWAY``, the gateway's URL, as ``[model] url`` names it;
- ``SS_LIVE_GATEWAY_KEY_FILE``, a file holding a key for it;
- ``SS_LIVE_GPT``, a GPT alias with reasoning, as ``[model] api`` names it ``responses``.

They report what they found, and gate nothing. Each prints the call's cost and where it
came from: the gateway, or its model information (lab 43, B2).
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from sandbox_science.orchestrator.model import (
    AssistantMessage,
    Message,
    ModelGateway,
    ModelTurn,
    OpenAICompatibleModel,
    ToolSpec,
)
from sandbox_science.orchestrator.responses import ResponsesModel, rendered

pytestmark = pytest.mark.live

ADD = ToolSpec(
    "add",
    "Add two integers.",
    {
        "type": "object",
        "properties": {"a": {"type": "integer"}, "b": {"type": "integer"}},
        "required": ["a", "b"],
    },
)
SYSTEM = "You are checking a calculator. Use the add tool for every sum, then answer."
TASK = "What is 48,213 + 77,409? Check it with the tool."


def setting(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        pytest.skip(f"{name} is not set")
    return value


def gateway() -> tuple[str, str, str]:
    key = Path(setting("SS_LIVE_GATEWAY_KEY_FILE")).read_text().strip()
    return setting("SS_LIVE_GATEWAY"), key, setting("SS_LIVE_GPT")


def answered(messages: list[Message], turn: ModelTurn) -> None:
    """The answer added, with a result for each of its calls, as the loop adds them."""
    messages.append(AssistantMessage.of(turn))
    for call in turn.tool_calls:
        arguments = json.loads(call.arguments)
        total = int(arguments.get("a", 0)) + int(arguments.get("b", 0))
        messages.append({"role": "tool", "tool_call_id": call.id, "content": str(total)})


def reported(turn: ModelTurn) -> dict[str, object]:
    return {
        "model": turn.model,
        "gateway": turn.gateway_version,
        "input": turn.input_tokens,
        "output": turn.output_tokens,
        "cost": turn.cost,
        "cost_source": turn.cost_source,
    }


async def test_cm_f31_gpt_reasoning_comes_back_and_is_sent_back() -> None:
    """CM-F31, GPT's part: a response to a request with ``store: false`` and
    ``include: ["reasoning.encrypted_content"]`` carries encrypted reasoning; the next
    request sends it back and is accepted; usage normalizes to total input (CM-28)."""
    url, key, alias = gateway()
    messages: list[Message] = [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": TASK},
    ]
    shared = ModelGateway(url, key)
    model = ResponsesModel(url, alias, key, prices=shared.prices)
    try:
        first = await model.complete(messages, [ADD])
        assert first.native is not None
        output = json.loads(first.native)["output"]
        encrypted = [i for i in output if i.get("type") == "reasoning" and i["encrypted_content"]]
        assert encrypted, "the response carries no encrypted reasoning"
        assert first.input_tokens == json.loads(first.native)["usage"]["input_tokens"]
        answered(messages, first)
        _, items = rendered(messages, alias=alias, model=first.model)
        assert any(item.get("type") == "reasoning" for item in items), "no reasoning sent back"
        second = await model.complete(messages, [ADD])
    finally:
        await model.close()
        await shared.close()
    assert second.text or second.tool_calls
    print(json.dumps({"reasoning_items": len(encrypted), "first": reported(first),
                      "second": reported(second)}))  # fmt: skip


async def test_cm_f31_chat_completions_prompt_tokens_on_gpt() -> None:
    """CM-F31: the chat-completions client's ``prompt_tokens`` on the same alias, which
    it takes as total input, as the gateway reports it."""
    url, key, alias = gateway()
    model = OpenAICompatibleModel(url, alias, key)
    try:
        turn = await model.complete(
            [{"role": "system", "content": SYSTEM}, {"role": "user", "content": TASK}], [ADD]
        )
    finally:
        await model.close()
    assert turn.native is not None
    usage = json.loads(turn.native)["usage"]
    assert turn.input_tokens == usage["prompt_tokens"]
    print(json.dumps({"chat": reported(turn), "usage": usage}))
