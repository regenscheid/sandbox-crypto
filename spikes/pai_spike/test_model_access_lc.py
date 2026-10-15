# SPDX-License-Identifier: MIT
"""Step 2 for option D: LangChain's chat models against the same fake LiteLLM proxy.

Runs in the LangChain venv only (it skips where langchain_openai is missing).
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import pytest

langchain_openai = pytest.importorskip("langchain_openai")

import pytest_asyncio  # noqa: E402
from aiohttp.test_utils import TestServer  # noqa: E402
from langchain_anthropic import ChatAnthropic  # noqa: E402
from langchain_core.messages import HumanMessage, ToolMessage  # noqa: E402
from langchain_core.tools import tool  # noqa: E402

from .fake_litellm import CONTEXT_ERROR_ANTHROPIC, CONTEXT_ERROR_CHAT, FakeLiteLLM, Reply  # noqa: E402

KEY = "test-secret-langchain-spike-gateway"
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


@tool
def python(code: str) -> str:
    """Run Python code."""
    return json.dumps({"stdout": "2\n"})


def chat(fake: FakeLiteLLM, **kwargs: Any) -> Any:
    return langchain_openai.ChatOpenAI(
        model="sandbox-orchestrator",
        base_url=f"{fake.url}/v1",
        api_key=KEY,
        include_response_headers=True,
        **{"max_retries": 0, **kwargs},
    )


async def test_chat_openai_tools_and_cost_header(fake: FakeLiteLLM) -> None:
    fake.script = [
        Reply(calls=[("python", {"code": "1+1"})], cost="0.0011"),
        Reply("2", cost="0.0022"),
    ]
    model = chat(fake).bind_tools([python])
    first = await model.ainvoke([HumanMessage("1+1?")])
    assert first.tool_calls[0]["name"] == "python"
    assert first.response_metadata["headers"]["x-litellm-response-cost"] == "0.0011"
    second = await model.ainvoke(
        [HumanMessage("1+1?"), first, ToolMessage("2", tool_call_id=first.tool_calls[0]["id"])]
    )
    assert second.content == "2"
    assert [s.path for s in fake.seen] == ["/v1/chat/completions"] * 2
    assert fake.seen[1].body["messages"][-1]["role"] == "tool"


async def test_chat_openai_streaming_loses_the_cost_header(fake: FakeLiteLLM) -> None:
    fake.script = [Reply("two", cost="0.0042")]
    chunks = [c async for c in chat(fake, streaming=True).astream([HumanMessage("1+1?")])]
    headers = [c.response_metadata.get("headers") for c in chunks if c.response_metadata]
    # Recorded either way: whether the streamed path carries the header is what we learn.
    print("streamed headers:", headers[:1])
    assert fake.seen[0].body["stream"] is True


async def test_chat_openai_context_limit(fake: FakeLiteLLM) -> None:
    from langchain_core.exceptions import ContextOverflowError

    fake.script = [Reply(status=400, error=CONTEXT_ERROR_CHAT, cost=None)]
    with pytest.raises(Exception) as failed:
        await chat(fake).ainvoke([HumanMessage("hi")])
    print("raised:", type(failed.value).__mro__[:4])
    assert isinstance(failed.value, ContextOverflowError)  # LiteLLM's wording is recognized


async def test_chat_openai_retries_a_budget_refusal_by_default(fake: FakeLiteLLM) -> None:
    fake.script = [Reply(status=429, error=SPENT, cost=None) for _ in range(3)]
    with pytest.raises(Exception):  # noqa: B017
        await chat(fake, max_retries=2).ainvoke([HumanMessage("hi")])
    assert len(fake.seen) == 3


async def test_chat_anthropic_through_litellm_with_context_management(fake: FakeLiteLLM) -> None:
    fake.script = [
        Reply(calls=[("python", {"code": "1+1"})], cost="0.003", compaction="SUMMARY"),
        Reply("2", cost="0.004"),
    ]
    edits = {
        "edits": [
            {"type": "clear_tool_uses_20250919", "trigger": {"type": "input_tokens", "value": 100000},
             "keep": {"type": "tool_uses", "value": 3}},
        ]
    }  # fmt: skip
    model = ChatAnthropic(
        model="sandbox-orchestrator",
        base_url=fake.url,
        api_key=KEY,
        max_retries=0,
        max_tokens=256,
        context_management=edits,
        betas=["context-management-2025-06-27"],
    ).bind_tools([python])
    first = await model.ainvoke([HumanMessage("1+1?")])
    sent = fake.seen[0]
    assert sent.path == "/v1/messages"
    assert sent.body["context_management"] == edits
    assert "context-management-2025-06-27" in sent.headers.get("anthropic-beta", "")
    # The cost header: is it anywhere in the message's metadata?
    print("anthropic response_metadata keys:", sorted(first.response_metadata))
    blocks = first.content if isinstance(first.content, list) else []
    print("anthropic content block types:", [b.get("type") for b in blocks if isinstance(b, dict)])
    await model.ainvoke(
        [HumanMessage("1+1?"), first, ToolMessage("2", tool_call_id=first.tool_calls[0]["id"])]
    )
    resent = json.dumps(fake.seen[1].body["messages"])
    print("compaction block sent back:", '"compaction"' in resent)


async def test_chat_anthropic_context_limit(fake: FakeLiteLLM) -> None:
    from langchain_core.exceptions import ContextOverflowError

    fake.script = [Reply(status=400, error=CONTEXT_ERROR_ANTHROPIC, cost=None)]
    model = ChatAnthropic(model="m", base_url=fake.url, api_key=KEY, max_retries=0, max_tokens=64)
    with pytest.raises(Exception) as failed:
        await model.ainvoke([HumanMessage("hi")])
    print("anthropic raised:", type(failed.value).__mro__[:4])
    print("is ContextOverflowError:", isinstance(failed.value, ContextOverflowError))
