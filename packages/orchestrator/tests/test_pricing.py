# SPDX-License-Identifier: MIT
"""A call's cost when the gateway reports none (lab 43, B2; run-service spec RS-6a): priced
from the gateway's model information at the call's tokens, and marked so, on each
adapter; never 0 for a call that used tokens and was not priced. And a response that
failed because its upstream was overloaded, tried again as a 529 is (B3).

The gateway is a fake LiteLLM in this process, whose answers are scripted; its key is
synthetic. Retries sleep on a clock that only records the waits.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, cast

import pytest
import pytest_asyncio
from aiohttp import web
from aiohttp.test_utils import TestServer

from sandbox_science.orchestrator.messages import MessagesModel
from sandbox_science.orchestrator.model import (
    ModelError,
    ModelGateway,
    OpenAICompatibleModel,
    OverloadedError,
)
from sandbox_science.orchestrator.pricing import (
    RETRY_SECONDS,
    Prices,
    Tokens,
    chat_tokens,
    cost,
    messages_tokens,
    prices_of,
    responses_tokens,
)
from sandbox_science.orchestrator.responses import ResponsesModel

from .test_model_limits import Waits

KEY = "test-secret-pricing-gateway-key"
Json = dict[str, Any]
ASK = [{"role": "user", "content": "Measure the thing."}]
PRICED = {
    "input_cost_per_token": 2e-06,
    "output_cost_per_token": 8e-06,
    "cache_read_input_token_cost": 5e-07,
    "cache_creation_input_token_cost": 2.5e-06,
}


def deployment(alias: str, model: str, **prices: object) -> Json:
    return {"model_name": alias, "litellm_params": {"model": model}, "model_info": prices}


def response(status: str = "completed", **fields: Any) -> str:
    usage = {"input_tokens": 1200, "input_tokens_details": {"cached_tokens": 1000},
             "output_tokens": 34, "output_tokens_details": {"reasoning_tokens": 20}}  # fmt: skip
    output = [{"id": "msg_1", "type": "message", "role": "assistant",
               "content": [{"type": "output_text", "text": "ok"}]}]  # fmt: skip
    body = {"id": "resp_1", "object": "response", "status": status, "model": "gpt-test",
            "output": output, "usage": usage, **fields}  # fmt: skip
    return json.dumps(body)


@dataclass
class Fake:
    """A LiteLLM proxy's three endpoints and its model information."""

    url: str = ""
    info: list[Json] = field(default_factory=list[Json])
    info_status: int = 200
    info_calls: int = 0
    answers: list[tuple[str, int, dict[str, str]]] = field(
        default_factory=list[tuple[str, int, dict[str, str]]]
    )

    async def described(self, request: web.Request) -> web.Response:
        assert request.headers["Authorization"] == f"Bearer {KEY}"
        self.info_calls += 1
        return web.json_response({"data": self.info}, status=self.info_status)

    async def answer(self, request: web.Request) -> web.Response:
        assert request.headers["Authorization"] == f"Bearer {KEY}"
        body, status, headers = self.answers.pop(0)
        return web.Response(
            status=status, text=body, headers={"Content-Type": "application/json", **headers}
        )

    def then(self, body: str, status: int = 200, headers: dict[str, str] | None = None) -> None:
        self.answers.append((body, status, headers or {}))


@pytest_asyncio.fixture
async def fake() -> AsyncIterator[Fake]:
    found = Fake()
    app = web.Application()
    app.router.add_get("/v1/model/info", found.described)
    for path in ("/v1/responses", "/v1/chat/completions", "/v1/messages"):
        app.router.add_post(path, found.answer)
    async with TestServer(app) as server:
        found.url = str(server.make_url("/v1"))
        yield found


# --- The price list ---------------------------------------------------------------------


def test_an_alias_is_priced_at_the_dearest_of_its_deployments() -> None:
    info = {"data": [
        deployment("gpt", "openai/a", **PRICED),
        deployment("gpt", "azure/a", **{**PRICED, "output_cost_per_token": 1e-05}),
        deployment("other", "openai/b", input_cost_per_token=1.0, output_cost_per_token=1.0),
    ]}  # fmt: skip
    assert prices_of(info, "gpt") == Prices(2e-06, 1e-05, 5e-07, 2.5e-06)
    assert prices_of(info, "none") is None


@pytest.mark.parametrize(
    "fields",
    [
        {"input_cost_per_token": 2e-06},  # no output price
        {"input_cost_per_token": None, "output_cost_per_token": 1e-06},
        {"input_cost_per_token": True, "output_cost_per_token": 1e-06},
        {"input_cost_per_token": -1, "output_cost_per_token": 1e-06},
        {"input_cost_per_token": "2e-06", "output_cost_per_token": 1e-06},
    ],
)
def test_an_alias_any_deployment_of_which_is_unpriced_has_no_prices(fields: Json) -> None:
    info = {"data": [deployment("gpt", "openai/a", **PRICED), deployment("gpt", "b", **fields)]}
    assert prices_of(info, "gpt") is None
    assert prices_of({"data": "not a list"}, "gpt") is None


def test_each_apis_usage_is_priced_as_its_tokens() -> None:
    """Chat completions and OpenAI's responses count the cached tokens in their input;
    Claude's messages count them apart."""
    assert chat_tokens(
        {"prompt_tokens": 1200, "completion_tokens": 34,
         "prompt_tokens_details": {"cached_tokens": 1000}, "cache_creation_input_tokens": 100}
    ) == Tokens(100, 1000, 100, 34)  # fmt: skip
    assert responses_tokens(
        {"input_tokens": 1200, "output_tokens": 34, "input_tokens_details": {"cached_tokens": 1000}}
    ) == Tokens(200, 1000, 0, 34)  # fmt: skip
    assert messages_tokens(
        {"input_tokens": 200, "output_tokens": 34, "cache_read_input_tokens": 1000,
         "cache_creation_input_tokens": 50}
    ) == Tokens(200, 1000, 50, 34)  # fmt: skip
    assert responses_tokens({"input_tokens": 1200}) is None  # a count it needs is missing
    assert chat_tokens({"prompt_tokens": -1, "completion_tokens": 3}) is None


def test_a_missing_cache_price_is_never_cheaper() -> None:
    """Cached input without a cache-read price is priced as input; cache written without
    a cache-write price, which costs more than input, leaves the cost unknown."""
    plain = Prices(2e-06, 8e-06)
    assert cost(plain, Tokens(200, 1000, 0, 34)) == pytest.approx(1200 * 2e-06 + 34 * 8e-06)
    assert cost(plain, Tokens(200, 0, 50, 34)) is None
    assert cost(Prices(2e-06, 8e-06, None, 2.5e-06), Tokens(0, 0, 50, 0)) == pytest.approx(
        50 * 2.5e-06
    )


# --- On each adapter --------------------------------------------------------------------

RESPONSES_COST = 200 * 2e-06 + 1000 * 5e-07 + 34 * 8e-06


@pytest.mark.parametrize(
    "headers",
    [
        {},  # LiteLLM 1.102.1 on /v1/responses: no header, and no usage.cost
        {"x-litellm-response-cost": "0.0"},  # 0 for a call that used tokens: no price
        {"x-litellm-response-cost-original": "0.0"},  # not the call's cost
    ],
)
async def test_an_unpriced_response_is_priced_from_the_gateways_model_info(
    fake: Fake, headers: dict[str, str]
) -> None:
    fake.info = [deployment("gpt", "openai/gpt-test", **PRICED)]
    fake.then(response(), headers=headers)
    model = ResponsesModel(fake.url, "gpt", KEY, clock=Waits())
    try:
        turn = await model.complete(ASK, [])
    finally:
        await model.close()
    assert turn.cost == pytest.approx(RESPONSES_COST)
    assert turn.cost_source == "model_info"


async def test_a_cost_the_gateway_reports_is_kept_as_its_own(fake: Fake) -> None:
    fake.info = [deployment("gpt", "openai/gpt-test", **PRICED)]
    fake.then(response(), headers={"x-litellm-response-cost": "0.0125"})
    model = ResponsesModel(fake.url, "gpt", KEY, clock=Waits())
    try:
        turn = await model.complete(ASK, [])
    finally:
        await model.close()
    assert (turn.cost, turn.cost_source) == (0.0125, "gateway")
    assert fake.info_calls == 0  # nothing to price


@pytest.mark.parametrize("headers", [{}, {"x-litellm-response-cost": "0"}])
async def test_without_prices_the_cost_stays_unknown_never_0(
    fake: Fake, headers: dict[str, str]
) -> None:
    fake.info = [deployment("gpt", "openai/gpt-test")]
    fake.then(response(), headers=headers)
    model = ResponsesModel(fake.url, "gpt", KEY, clock=Waits())
    try:
        turn = await model.complete(ASK, [])
    finally:
        await model.close()
    assert (turn.cost, turn.cost_source) == (None, None)


async def test_the_chat_and_messages_adapters_are_priced_the_same_way(fake: Fake) -> None:
    """The same gap on the other two routes, were the gateway to send no cost there."""
    fake.info = [
        deployment("chat", "openai/gpt-chat", **PRICED),
        deployment("claude", "anthropic/claude-opus-4-1", **PRICED),
    ]
    fake.then(json.dumps({
        "model": "gpt-chat", "choices": [{"message": {"content": "ok"}}],
        "usage": {"prompt_tokens": 1200, "completion_tokens": 34,
                  "prompt_tokens_details": {"cached_tokens": 1000}},
    }))  # fmt: skip
    fake.then(json.dumps({
        "id": "msg_1", "type": "message", "role": "assistant", "model": "claude-opus-4-1",
        "content": [{"type": "text", "text": "ok"}], "stop_reason": "end_turn",
        "usage": {"input_tokens": 200, "output_tokens": 34, "cache_read_input_tokens": 1000,
                  "cache_creation_input_tokens": 0},
    }))  # fmt: skip
    gateway = ModelGateway(fake.url, KEY, apis={"claude": "messages"})
    chat, claude = gateway.model("chat"), gateway.model("claude")
    try:
        assert isinstance(chat, OpenAICompatibleModel)
        assert isinstance(claude, MessagesModel)
        priced = await chat.complete(ASK, [])
        claimed = await claude.complete(ASK, [])
    finally:
        await gateway.close()
    assert (priced.cost, priced.cost_source) == (pytest.approx(RESPONSES_COST), "model_info")
    assert (claimed.cost, claimed.cost_source) == (pytest.approx(RESPONSES_COST), "model_info")
    # The gateway's models share its prices: the chat model's read priced the other's.
    assert fake.info_calls == 2  # once for the prices, once for Claude's model


async def test_a_failed_read_of_the_prices_is_tried_again_later(fake: Fake) -> None:
    waits = Waits()
    fake.info_status = 500
    model = ResponsesModel(fake.url, "gpt", KEY, clock=waits)
    try:
        for _ in range(2):
            fake.then(response())
            assert (await model.complete(ASK, [])).cost is None
        assert fake.info_calls == 1  # not asked again at once
        waits.slept.append(RETRY_SECONDS)
        fake.info_status, fake.info = 200, [deployment("gpt", "openai/gpt-test", **PRICED)]
        fake.then(response())
        turn = await model.complete(ASK, [])
    finally:
        await model.close()
    assert fake.info_calls == 2
    assert turn.cost == pytest.approx(RESPONSES_COST)


# --- An overloaded upstream (B3) --------------------------------------------------------

OVERLOADED = {"code": "server_error", "message": "Our servers are currently overloaded."}


@pytest.mark.parametrize(
    ("failed", "status"),
    [
        (response("failed", error=OVERLOADED), 200),
        (response("failed", error={"code": "overloaded"}), 200),
        (json.dumps({"error": {"type": "overloaded_error", "message": "Overloaded"}}), 500),
        (json.dumps({"error": {"message": "upstream overloaded, try later"}}), 500),
    ],
)
async def test_an_overloaded_upstream_is_tried_again(fake: Fake, failed: str, status: int) -> None:
    fake.info = [deployment("gpt", "openai/gpt-test", **PRICED)]
    waits = Waits()
    fake.then(failed, status, {"x-litellm-response-cost": "0.01"})
    fake.then(response(), headers={"x-litellm-response-cost": "0.02"})
    model = ResponsesModel(fake.url, "gpt", KEY, clock=waits, backoff=2.0)
    try:
        turn = await model.complete(ASK, [])
    finally:
        await model.close()
    assert (turn.text, turn.cost) == ("ok", 0.02)
    assert waits.slept == [2.0]


async def test_an_overloaded_response_that_keeps_failing_ends_as_overloaded(fake: Fake) -> None:
    waits = Waits()
    for _ in range(3):
        fake.then(response("failed", error=OVERLOADED))
    model = ResponsesModel(fake.url, "gpt", KEY, clock=waits, retries=2, backoff=1.0)
    try:
        with pytest.raises(OverloadedError, match="overloaded"):
            await model.complete(ASK, [])
    finally:
        await model.close()
    assert waits.slept == [1.0, 2.0]


async def test_a_server_error_that_is_not_an_overload_is_not_tried_again(fake: Fake) -> None:
    waits = Waits()
    fake.then(json.dumps({"error": {"message": "internal error"}}), 500)
    model = ResponsesModel(fake.url, "gpt", KEY, clock=waits)
    try:
        with pytest.raises(ModelError) as error:
            await model.complete(ASK, [])
    finally:
        await model.close()
    assert (error.value.status, waits.slept) == (500, [])
    assert not cast(list[object], fake.answers)
