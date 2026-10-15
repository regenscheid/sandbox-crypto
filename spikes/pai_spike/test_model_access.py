# SPDX-License-Identifier: MIT
"""Step 2 of the evaluation: Pydantic AI's model layer against a fake LiteLLM proxy.

Each test inspects what the fake gateway received, so the claims are about the wire,
not about the docs. The key is synthetic.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio
from aiohttp.test_utils import TestServer
from pydantic_ai import Agent
from pydantic_ai.exceptions import ModelHTTPError
from pydantic_ai.models.anthropic import AnthropicCompaction, AnthropicModel
from pydantic_ai.models.openai import OpenAIChatModel, OpenAICompaction, OpenAIResponsesModel
from pydantic_ai.providers.anthropic import AnthropicProvider
from pydantic_ai.providers.litellm import LiteLLMProvider
from pydantic_ai.providers.openai import OpenAIProvider

from .fake_litellm import CONTEXT_ERROR_ANTHROPIC, CONTEXT_ERROR_CHAT, FakeLiteLLM, Reply
from .gateway import GatewayRecorder, gateway_client

KEY = "test-secret-pydantic-ai-spike-gateway"
SPENT = {
    "error": {
        "message": "ExceededBudget: User=lab over budget. Spend=5.2, Budget=5.0",
        "type": "auth_error",
        "param": "None",
        "code": "429",
    }
}


@pytest_asyncio.fixture
async def fake() -> AsyncIterator[FakeLiteLLM]:
    gateway = FakeLiteLLM()
    async with TestServer(gateway.app()) as server:
        gateway.url = str(server.make_url("/")).rstrip("/")
        yield gateway


def chat_model(fake: FakeLiteLLM, *, max_retries: int = 0) -> OpenAIChatModel:
    from openai import AsyncOpenAI

    client = AsyncOpenAI(
        base_url=f"{fake.url}/v1",
        api_key=KEY,
        http_client=gateway_client(),
        max_retries=max_retries,
    )
    return OpenAIChatModel("sandbox-orchestrator", provider=LiteLLMProvider(openai_client=client))


def python_agent(model: Any, recorder: GatewayRecorder, **kwargs: Any) -> Agent[None, str]:
    agent = Agent(recorder.wrap(model), instructions="You are a test.", **kwargs)

    @agent.tool_plain
    def python(code: str) -> str:
        """Run Python code."""
        return json.dumps({"stdout": "2\n", "code": code})

    return agent


async def test_chat_completions_with_tools_reads_cost_and_sends_openai_format(
    fake: FakeLiteLLM,
) -> None:
    fake.script = [
        Reply(calls=[("python", {"code": "1+1"})], cost="0.0011"),
        Reply("It is 2.", cost="0.0022"),
    ]
    recorder = GatewayRecorder()
    result = await python_agent(chat_model(fake), recorder).run("What is 1+1?")
    assert result.output == "It is 2."
    assert [s.path for s in fake.seen] == ["/v1/chat/completions"] * 2
    first = fake.seen[0]
    assert first.headers["authorization"] == f"Bearer {KEY}"
    assert first.body["model"] == "sandbox-orchestrator"
    assert first.body["messages"][0] == {"role": "system", "content": "You are a test."}
    assert first.body["tools"][0]["function"]["name"] == "python"
    second = fake.seen[1].body["messages"]
    assert [m["role"] for m in second] == ["system", "user", "assistant", "tool"]
    # The cost of each call, read from the header, kept per call and in the history.
    assert [c.cost for c in recorder.calls] == [0.0011, 0.0022]
    assert recorder.calls[0].sent == [first.body]
    costs = [
        m.provider_details.get("litellm_cost")
        for m in result.all_messages()
        if m.kind == "response" and m.provider_details
    ]
    assert costs == [0.0011, 0.0022]
    assert recorder.calls[0].model_name == "resolved-sandbox-orchestrator"


async def test_chat_completions_streaming_reads_cost(fake: FakeLiteLLM) -> None:
    fake.script = [
        Reply(calls=[("python", {"code": "1+1"})], cost="0.0031"),
        Reply("It is two.", cost="0.0042", input_tokens=150, output_tokens=7),
    ]
    recorder = GatewayRecorder()
    agent = python_agent(chat_model(fake), recorder)
    async with agent.run_stream("What is 1+1?") as stream:
        text = await stream.get_output()
    assert text == "It is two."
    assert all(s.body.get("stream") is True for s in fake.seen)
    assert fake.seen[0].body.get("stream_options") == {"include_usage": True}
    assert [c.cost for c in recorder.calls] == [0.0031, 0.0042]
    assert recorder.calls[1].input_tokens == 150


async def test_chat_completions_context_limit_is_a_plain_http_error(fake: FakeLiteLLM) -> None:
    fake.script = [Reply(status=400, error=CONTEXT_ERROR_CHAT, cost=None)]
    recorder = GatewayRecorder()
    with pytest.raises(ModelHTTPError) as failed:
        await python_agent(chat_model(fake), recorder).run("Hi.")
    error = failed.value
    assert error.status_code == 400
    # No dedicated exception: the caller classifies the body, as model.py does today.
    assert isinstance(error.body, dict)
    assert str(error.body.get("message", "")).startswith("litellm.ContextWindowExceededError")


async def test_the_sdk_retries_a_budget_refusal_unless_told_not_to(fake: FakeLiteLLM) -> None:
    # LiteLLM sends some budget refusals as 429s; the OpenAI SDK retries 429 by default.
    fake.script = [Reply(status=429, error=SPENT, cost=None) for _ in range(3)]
    recorder = GatewayRecorder()
    with pytest.raises(ModelHTTPError):
        await python_agent(chat_model(fake, max_retries=2), recorder).run("Hi.")
    assert len(fake.seen) == 3  # the spent budget was asked twice more
    assert recorder.calls[0].statuses == [429, 429, 429]  # one model call, three attempts
    fake.seen.clear()
    fake.script = [Reply(status=429, error=SPENT, cost=None)]
    with pytest.raises(ModelHTTPError):
        await python_agent(chat_model(fake, max_retries=0), recorder).run("Hi.")
    assert len(fake.seen) == 1


def anthropic_model(fake: FakeLiteLLM) -> AnthropicModel:
    provider = AnthropicProvider(base_url=fake.url, api_key=KEY, http_client=gateway_client())
    return AnthropicModel("sandbox-orchestrator", provider=provider)


async def test_anthropic_messages_through_litellm_reads_cost_and_caches(
    fake: FakeLiteLLM,
) -> None:
    fake.script = [
        Reply(calls=[("python", {"code": "1+1"})], cost="0.003"),
        Reply("It is 2.", cost="0.004", cached_tokens=90),
    ]
    recorder = GatewayRecorder()
    agent = python_agent(
        anthropic_model(fake),
        recorder,
        model_settings={
            "anthropic_cache_instructions": True,
            "anthropic_cache_tool_definitions": True,
            "anthropic_cache_messages": True,
            "max_tokens": 1024,
        },
    )
    result = await agent.run("What is 1+1?")
    assert result.output == "It is 2."
    assert [s.path for s in fake.seen] == ["/v1/messages"] * 2
    sent = fake.seen[1]
    assert sent.headers.get("x-api-key") == KEY
    assert sent.body["system"][0]["cache_control"]["type"] == "ephemeral"
    assert sent.body["tools"][-1]["cache_control"]["type"] == "ephemeral"
    last = sent.body["messages"][-1]["content"][-1]
    assert last["type"] == "tool_result"
    assert last["cache_control"]["type"] == "ephemeral"
    assert [c.cost for c in recorder.calls] == [0.003, 0.004]
    assert recorder.calls[1].cache_read_tokens == 90


async def test_anthropic_streaming_reads_cost(fake: FakeLiteLLM) -> None:
    fake.script = [Reply("Streamed.", cost="0.005")]
    recorder = GatewayRecorder()
    agent = python_agent(anthropic_model(fake), recorder, model_settings={"max_tokens": 64})
    async with agent.run_stream("Hi.") as stream:
        assert await stream.get_output() == "Streamed."
    assert fake.seen[0].body["stream"] is True
    assert [c.cost for c in recorder.calls] == [0.005]


async def test_anthropic_compaction_request_and_round_trip(fake: FakeLiteLLM) -> None:
    fake.script = [
        Reply(calls=[("python", {"code": "1+1"})], compaction="SUMMARY: the user asked 1+1."),
        Reply("It is 2."),
    ]
    recorder = GatewayRecorder()
    agent = python_agent(
        anthropic_model(fake),
        recorder,
        capabilities=[AnthropicCompaction(token_threshold=60_000)],
        model_settings={"max_tokens": 256},
    )
    await agent.run("What is 1+1?")
    first = fake.seen[0]
    assert first.body["context_management"] == {
        "edits": [{"type": "compact_20260112", "trigger": {"type": "input_tokens", "value": 60000}}]
    }
    assert "compact-2026-01-12" in first.headers.get("anthropic-beta", "")
    # The compaction block comes back on the next request, ahead of what followed it.
    second = fake.seen[1].body["messages"]
    blocks = [b for m in second if isinstance(m["content"], list) for b in m["content"]]
    assert {"type": "compaction", "content": "SUMMARY: the user asked 1+1."} in [
        {k: v for k, v in b.items() if k in ("type", "content")} for b in blocks
    ]
    roles_before = [m["role"] for m in second]
    assert roles_before[0] == "assistant"  # what preceded the compaction block is dropped


async def test_anthropic_context_limit_is_a_plain_http_error(fake: FakeLiteLLM) -> None:
    fake.script = [Reply(status=400, error=CONTEXT_ERROR_ANTHROPIC, cost=None)]
    recorder = GatewayRecorder()
    agent = python_agent(anthropic_model(fake), recorder, model_settings={"max_tokens": 64})
    with pytest.raises(ModelHTTPError) as failed:
        await agent.run("Hi.")
    assert failed.value.status_code == 400
    assert "prompt is too long" in json.dumps(failed.value.body)


def responses_model(fake: FakeLiteLLM) -> OpenAIResponsesModel:
    from openai import AsyncOpenAI

    client = AsyncOpenAI(
        base_url=f"{fake.url}/v1", api_key=KEY, http_client=gateway_client(), max_retries=0
    )
    return OpenAIResponsesModel(
        "sandbox-orchestrator", provider=OpenAIProvider(openai_client=client)
    )


async def test_openai_responses_with_compaction(fake: FakeLiteLLM) -> None:
    fake.script = [
        Reply(calls=[("python", {"code": "1+1"})], cost="0.006", compaction="enc-abc"),
        Reply("It is 2.", cost="0.007"),
    ]
    recorder = GatewayRecorder()
    agent = python_agent(
        responses_model(fake),
        recorder,
        capabilities=[OpenAICompaction(token_threshold=80_000)],
    )
    result = await agent.run("What is 1+1?")
    assert result.output == "It is 2."
    assert [s.path for s in fake.seen] == ["/v1/responses"] * 2
    first = fake.seen[0].body
    assert first["context_management"] == [{"type": "compaction", "compact_threshold": 80000}]
    assert [c.cost for c in recorder.calls] == [0.006, 0.007]
    second = fake.seen[1].body["input"]
    kinds = [item.get("type") for item in second]
    assert "compaction" in kinds
    # The items before the compaction item are no longer sent.
    assert kinds.index("compaction") == 0, kinds
