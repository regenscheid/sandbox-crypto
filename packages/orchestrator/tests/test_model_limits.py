# SPDX-License-Identifier: MIT
"""A real model's limits (A-09): gateway retries, the context limit and token usage.

The gateway is a fake in this process; its key is synthetic. Retries sleep on a clock
that only records the waits.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal, cast

import aiohttp
import pytest
import pytest_asyncio
from aiohttp import web
from aiohttp.test_utils import TestServer

from sandbox_science.contracts import ExecutionContext
from sandbox_science.orchestrator import cli
from sandbox_science.orchestrator.hooks import Closing, Notice, RunView, TurnHook
from sandbox_science.orchestrator.loop import run_task
from sandbox_science.orchestrator.model import (
    MAX_RETRY_WAIT,
    REQUEST_BYTES_PER_SECOND,
    ContextLimitError,
    GatewayBudgetError,
    ModelError,
    ModelTurn,
    OpenAICompatibleModel,
    ScriptedModel,
)
from sandbox_science.orchestrator.tools import Toolbox, ToolOutcome
from sandbox_science.orchestrator.worker_client import WorkerClient

from .loop_harness import GATEWAY_VERSION, Harness, Reply, call
from .loop_harness import gateway as gateway
from .loop_harness import harness as harness
from .loop_harness import worker as worker

KEY = "test-secret-model-limits-gateway-key"
CONTEXT = ExecutionContext(project_id="p", session_id="s", environment_id="env")
TOO_LONG = (
    '{"error":{"message":"litellm.ContextWindowExceededError: This model\'s maximum '
    'context length is 8192 tokens","type":"invalid_request_error",'
    '"code":"context_length_exceeded"}}'
)
SPENT = {
    "error": {
        "message": "Budget has been exceeded! Current cost: 1.0312, Max budget: 1.0",
        "type": "budget_exceeded",
        "param": None,
        "code": "400",
    }
}
"""LiteLLM's proxy refusing a key whose budget is spent (``ProxyErrorTypes.budget_exceeded``)."""
BUDGET_REFUSALS = {
    "key, by type": (400, SPENT),
    "by type alone": (
        400, {"error": {"message": "Key spend is over its limit", "type": "budget_exceeded"}}
    ),
    "user, as a 429": (
        429,
        {"error": {"message": "ExceededBudget: User=lab over budget. Spend=5.2, Budget=5.0",
                   "type": "auth_error", "param": "None", "code": "429"}},
    ),
    "token, as a 401": (
        401,
        {"error": {"message": "Authentication Error - ExceededTokenBudget: Current spend for "
                   "token: 7.1; Max Budget for Token: 7.0", "type": "auth_error", "code": "401"}},
    ),
    "the SDK's error": (
        400,
        {"error": {"message": "litellm.BudgetExceededError: Budget has been exceeded! Current "
                   "cost: 2.0, Max budget: 2.0", "type": None, "code": "400"}},
    ),
}  # fmt: skip
ANSWER = {
    "model": "resolved-main",
    "choices": [{"message": {"content": "done"}, "finish_reason": "stop"}],
    "usage": {"prompt_tokens": 1200, "completion_tokens": 34},
}


class Waits:
    """A clock whose sleeps return at once, recording how long each asked for."""

    def __init__(self) -> None:
        self.slept: list[float] = []

    def monotonic(self) -> float:
        return sum(self.slept)

    def now(self) -> datetime:
        return datetime(2026, 10, 14, tzinfo=UTC)

    async def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)


@dataclass
class Gateway:
    """Answers each request with the next scripted ``(status, body, headers)``."""

    answers: list[tuple[int, Any, dict[str, str]]] = field(default_factory=list)
    calls: int = 0
    url: str = ""

    async def handle(self, request: web.Request) -> web.Response:
        assert request.headers["Authorization"] == f"Bearer {KEY}"
        self.calls += 1
        status, body, headers = self.answers.pop(0)
        if isinstance(body, str):
            return web.Response(status=status, text=body, headers=headers)
        return web.json_response(body, status=status, headers=headers)


@pytest_asyncio.fixture
async def fake() -> AsyncIterator[Gateway]:
    found = Gateway()
    app = web.Application()
    app.router.add_post("/v1/chat/completions", found.handle)
    async with TestServer(app) as server:
        found.url = str(server.make_url("/v1"))
        yield found


async def complete(fake: Gateway, waits: Waits, **options: Any) -> Any:
    model = OpenAICompatibleModel(fake.url, "main", KEY, clock=waits, **options)
    try:
        return await model.complete([{"role": "user", "content": "hi"}], [])
    finally:
        await model.close()


# --- The gateway client ----------------------------------------------------------------


async def test_rate_limits_and_unavailable_upstreams_are_tried_again(fake: Gateway) -> None:
    fake.answers = [
        (429, "slow down", {"Retry-After": "1.5"}),
        (502, "bad gateway", {}),
        (503, "unavailable", {"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"}),
        (200, ANSWER, {}),
    ]
    waits = Waits()
    turn = await complete(fake, waits)
    assert (turn.text, turn.model, turn.input_tokens, turn.output_tokens) == (
        "done",
        "resolved-main",
        1200,
        34,
    )
    # Retry-After in seconds is honored; an HTTP date falls back to the doubling backoff.
    assert waits.slept == [1.5, 4.0, 8.0]
    assert fake.calls == 4


CLOUDFLARE_524 = (
    "<!DOCTYPE html><html><head><title>Gateway Time-out</title></head><body>"
    "<h1>A timeout occurred</h1><p>Error code 524</p></body></html>"
)
"""What Cloudflare answers when the gateway behind it takes too long (lab 44, B3)."""


async def test_a_gateway_timeout_with_a_web_page_is_tried_again_and_recorded(
    fake: Gateway,
) -> None:
    fake.answers = [
        (524, CLOUDFLARE_524, {"Content-Type": "text/html"}),
        (502, CLOUDFLARE_524, {"Content-Type": "text/html"}),
        (200, ANSWER, {}),
    ]
    waits = Waits()
    turn = await complete(fake, waits)
    assert (turn.text, turn.input_tokens, fake.calls) == ("done", 1200, 3)
    assert waits.slept == [2.0, 4.0]
    # The turn is the answered attempt's alone; the others are only named.
    assert turn.retried == ("HTTP 524", "HTTP 502")


async def test_a_gateway_timeout_that_goes_on_ends_with_its_status_not_its_page(
    fake: Gateway,
) -> None:
    fake.answers = [(524, CLOUDFLARE_524, {"Content-Type": "text/html"})] * 10
    waits = Waits()
    with pytest.raises(ModelError) as failed:
        await complete(fake, waits)
    assert failed.value.status == 524
    assert "a text/html page of" in str(failed.value)
    assert "Gateway Time-out" not in str(failed.value)
    assert (waits.slept, fake.calls) == (
        [2.0, 4.0, 8.0, 16.0, 32.0, 60.0, 60.0, 60.0, 60.0],
        10,
    )


async def test_a_call_without_retries_names_none(fake: Gateway) -> None:
    fake.answers = [(200, ANSWER, {})]
    turn = await complete(fake, Waits())
    assert turn.retried == ()


class Timeouts:
    """A session that notes the total timeout of each request it sends."""

    def __init__(self) -> None:
        self.session = aiohttp.ClientSession()
        self.totals: list[float | None] = []

    def post(self, url: str, **kwargs: Any) -> Any:
        self.totals.append(kwargs["timeout"].total)
        return self.session.post(url, **kwargs)

    def get(self, url: str, **kwargs: Any) -> Any:  # the price list's
        return self.session.get(url, **kwargs)


async def test_the_timeout_grows_with_the_size_of_the_request(fake: Gateway) -> None:
    fake.answers = [(200, ANSWER, {})] * 2
    session = Timeouts()
    model = OpenAICompatibleModel(fake.url, "main", KEY, session=cast(Any, session), timeout=100)
    try:
        await model.complete([{"role": "user", "content": "hi"}], [])
        await model.complete([{"role": "user", "content": "x" * 1_000_000}], [])
    finally:
        await session.session.close()
    small, large = session.totals
    assert small is not None
    assert 100 < small < 101
    assert large is not None
    assert large == pytest.approx(100 + 1_000_100 / REQUEST_BYTES_PER_SECOND, abs=1)
    assert large > 290


async def test_retries_end_and_backoff_is_capped(fake: Gateway) -> None:
    fake.answers = [(504, "timeout", {})] * 8
    waits = Waits()
    with pytest.raises(ModelError) as failed:
        await complete(fake, waits, retries=7, backoff=10)
    assert failed.value.status == 504
    assert not isinstance(failed.value, ContextLimitError)
    assert waits.slept == [10, 20, 40, *[MAX_RETRY_WAIT] * 4]
    assert fake.calls == 8


async def test_default_retries_make_ten_total_attempts(fake: Gateway) -> None:
    fake.answers = [(524, "gateway timeout", {})] * 10
    waits = Waits()
    with pytest.raises(ModelError) as failed:
        await complete(fake, waits)
    assert failed.value.status == 524
    assert fake.calls == 10
    assert waits.slept == [2, 4, 8, 16, 32, 60, 60, 60, 60]


async def test_a_gateway_asking_for_a_longer_wait_than_the_cap_is_not_waited_for(
    fake: Gateway,
) -> None:
    fake.answers = [(429, "later", {"Retry-After": "86400"}), (200, ANSWER, {})]
    waits = Waits()
    with pytest.raises(ModelError) as failed:
        await complete(fake, waits)
    assert (failed.value.status, waits.slept, fake.calls) == (429, [], 1)


async def test_an_unrelated_400_quoting_the_request_is_not_a_context_error(
    fake: Gateway,
) -> None:
    quoted = {
        "error": {
            "message": "litellm.BadRequestError: Invalid tool_call in messages[4]: "
            "'grep -c context_length_exceeded gateway.log'",
            "code": "400",
        }
    }
    fake.answers = [(400, quoted, {})]
    with pytest.raises(ModelError) as failed:
        await complete(fake, Waits())
    assert not isinstance(failed.value, ContextLimitError)


async def test_litellms_own_context_error_is_recognized(fake: Gateway) -> None:
    body = {
        "error": {
            "message": "litellm.ContextWindowExceededError: litellm.BadRequestError: too long",
            "type": None,
            "code": "400",
        }
    }
    fake.answers = [(400, body, {})]
    with pytest.raises(ContextLimitError):
        await complete(fake, Waits())


@pytest.mark.parametrize(("status", "body"), BUDGET_REFUSALS.values(), ids=list(BUDGET_REFUSALS))
async def test_a_spent_gateway_budget_is_its_own_kind_and_never_tried_again(
    fake: Gateway, status: int, body: dict[str, Any]
) -> None:
    fake.answers = [(status, body, {"Retry-After": "0"}), (200, ANSWER, {})]
    waits = Waits()
    with pytest.raises(GatewayBudgetError) as refused:
        await complete(fake, waits)
    assert (refused.value.status, fake.calls, waits.slept) == (status, 1, [])


async def test_rate_limits_and_quoted_budgets_are_not_budget_refusals(fake: Gateway) -> None:
    rate = {"error": {"message": "Rate limit exceeded: 60 rpm", "type": "rate_limit_error"}}
    fake.answers = [(429, rate, {"Retry-After": "0"}), (200, ANSWER, {})]
    waits = Waits()
    assert (await complete(fake, waits)).text == "done"
    assert waits.slept == [0.0]  # a rate limit is tried again
    quoted = {
        "error": {"message": "Invalid request", "type": "invalid_request_error"},
        "request": {"messages": [{"content": "Budget has been exceeded! type budget_exceeded"}]},
    }
    fake.answers = [(400, quoted, {})]
    with pytest.raises(ModelError) as failed:
        await complete(fake, Waits())
    assert not isinstance(failed.value, GatewayBudgetError)


async def test_a_calls_cost_is_taken_as_the_gateway_reports_it(fake: Gateway) -> None:
    """LiteLLM's header, or the answer's ``usage.cost`` without it; nothing else."""
    in_body = {**ANSWER, "usage": {**ANSWER["usage"], "cost": 0.25}}
    fake.answers = [
        (200, ANSWER, {"x-litellm-response-cost": "0.00123"}),
        (200, in_body, {}),
        (200, in_body, {"x-litellm-response-cost": "0.5"}),  # the header wins
        (200, ANSWER, {"x-litellm-response-cost": "free"}),
        (200, ANSWER, {"x-litellm-response-cost": "-1"}),
        (200, {**ANSWER, "usage": {**ANSWER["usage"], "cost": True}}, {}),
        (200, ANSWER, {}),
    ]
    costs = [(await complete(fake, Waits())).cost for _ in range(7)]
    assert costs == [0.00123, 0.25, 0.5, None, None, None, None]


async def test_an_unparseable_answer_never_quotes_the_key(fake: Gateway) -> None:
    fake.answers = [(200, f'{{"echo": {{"authorization": "Bearer {KEY}"}}}}', {})]
    with pytest.raises(ModelError) as failed:
        await complete(fake, Waits())
    assert KEY not in str(failed.value)


async def test_a_broken_answer_chains_no_request_and_logs_no_key(
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def garbage(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await reader.read(65536)
        writer.write(b"HTTP/1.1 200 OK\r\nBroken header line\r\n\r\n")
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(garbage, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    model = OpenAICompatibleModel(f"http://127.0.0.1:{port}/v1", "main", KEY, clock=Waits())
    try:
        with pytest.raises(ModelError) as failed:
            await model.complete([{"role": "user", "content": "hi"}], [])
        assert failed.value.__cause__ is None
        async with WorkerClient("http://127.0.0.1:1", "test-secret-worker") as client:
            with caplog.at_level(logging.DEBUG):
                transcript = await run_task(model, Toolbox(client, CONTEXT), "Hi.", run_id="g")
        assert transcript.failure == "model"
        assert "cannot reach the model gateway" in caplog.text
        assert KEY not in caplog.text
    finally:
        await model.close()
        server.close()
        await server.wait_closed()


@pytest.mark.parametrize("status", [400, 401, 404, 500])
async def test_other_errors_are_not_tried_again(fake: Gateway, status: int) -> None:
    fake.answers = [(status, "no", {})]
    waits = Waits()
    with pytest.raises(ModelError) as failed:
        await complete(fake, waits)
    assert (failed.value.status, waits.slept, fake.calls) == (status, [], 1)
    assert not isinstance(failed.value, ContextLimitError)


async def test_a_context_length_error_is_its_own_kind(fake: Gateway) -> None:
    fake.answers = [(400, TOO_LONG, {})]
    with pytest.raises(ContextLimitError) as failed:
        await complete(fake, Waits())
    assert failed.value.status == 400
    assert fake.calls == 1


async def test_an_error_never_quotes_the_key(fake: Gateway) -> None:
    fake.answers = [(401, f"Invalid proxy server token passed. Received API Key = {KEY}", {})]
    with pytest.raises(ModelError) as failed:
        await complete(fake, Waits())
    assert KEY not in str(failed.value)
    assert "[the gateway key]" in str(failed.value)


async def test_retry_logs_hold_only_the_status(
    fake: Gateway, caplog: pytest.LogCaptureFixture
) -> None:
    fake.answers = [(429, f"over the limit for {KEY}", {"Retry-After": "0"}), (200, ANSWER, {})]
    with caplog.at_level(logging.WARNING):
        await complete(fake, Waits())
    assert "HTTP 429" in caplog.text
    assert "over the limit" not in caplog.text


def test_retries_and_backoff_are_not_negative() -> None:
    with pytest.raises(ValueError, match="not negative"):
        OpenAICompatibleModel("http://127.0.0.1:1", "main", KEY, retries=-1)


# --- The loop --------------------------------------------------------------------------


async def read(*args: Any, **kwargs: Any) -> ToolOutcome:
    return ToolOutcome("x" * 100_000, {"path": "big"})


async def test_a_conversation_over_the_budget_stops_cleanly_before_the_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(Toolbox, "call", read)  # the worker is not called
    model = ScriptedModel.from_json(
        [{"text": None, "tool_calls": [{"name": "read_file", "arguments": {"path": "big"}}]}]
    )
    async with WorkerClient("http://127.0.0.1:1", "test-secret-worker") as client:
        transcript = await run_task(
            model, Toolbox(client, CONTEXT), "Read it.", run_id="limits", max_input_chars=100_000
        )
    # The first call fit; the tool result made the next one too long, so it was not made.
    assert len(model.requests) == 1
    assert (transcript.stopped, transcript.failure, transcript.final_text) == (
        "context_limit",
        None,
        None,
    )
    assert transcript.context.messages[-1]["role"] == "tool"
    assert [usage.model for usage in transcript.context.usage] == ["scripted"]


async def test_a_failure_is_logged_with_its_cause(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    model = ScriptedModel([])

    async def broken(*args: Any) -> None:
        raise ModelError("cannot reach the model gateway: synthetic refusal")

    monkeypatch.setattr(model, "complete", broken)
    async with WorkerClient("http://127.0.0.1:1", "test-secret-worker") as client:
        transcript = await run_task(model, Toolbox(client, CONTEXT), "Hi.", run_id="logged")
    assert (transcript.stopped, transcript.failure) == ("error", "model")
    assert "run logged failed during model" in caplog.text
    assert "ModelError: cannot reach the model gateway: synthetic refusal" in caplog.text


class Findings(TurnHook):
    """The first closing answer gets one finding, to respond to or continue with."""

    name = "findings"

    def __init__(self, action: Literal["respond", "continue"], size: int) -> None:
        self.action: Literal["respond", "continue"] = action
        self.size, self.asked = size, False

    async def closing(self, run: RunView, answer: str | None) -> Closing:
        if self.asked:
            return Closing("accept")
        self.asked = True
        notice = Notice("findings", ("f-1",), "Findings: ", {"finding": "y" * self.size})
        return Closing(self.action, (notice,))


@pytest.mark.parametrize("action", ["respond", "continue"])
async def test_a_closing_round_past_the_budget_keeps_the_answer(
    action: Literal["respond", "continue"],
) -> None:
    model = ScriptedModel([ModelTurn(text="The answer.", model="scripted")])
    async with WorkerClient("http://127.0.0.1:1", "test-secret-worker") as client:
        transcript = await run_task(
            model,
            Toolbox(client, CONTEXT),
            "Hi.",
            run_id="limits",
            max_input_chars=60_000,
            hooks=[Findings(action, 70_000)],
        )
    assert len(model.requests) == 1
    assert (transcript.stopped, transcript.final_text) == ("answered", "The answer.")
    assert "Findings: " not in str(transcript.context.messages)  # no model would see them


@pytest.mark.parametrize("action", ["respond", "continue"])
async def test_a_closing_round_the_model_has_no_room_for_keeps_the_answer(
    action: Literal["respond", "continue"], monkeypatch: pytest.MonkeyPatch
) -> None:
    model = ScriptedModel([ModelTurn(text="The answer.", model="scripted")])
    answered = model.complete

    async def complete(messages: Any, tools: Any) -> ModelTurn:
        if model.requests:
            raise ContextLimitError("the model gateway returned 400: too long", status=400)
        return await answered(messages, tools)

    monkeypatch.setattr(model, "complete", complete)  # a gateway that refuses the round
    async with WorkerClient("http://127.0.0.1:1", "test-secret-worker") as client:
        transcript = await run_task(
            model, Toolbox(client, CONTEXT), "Hi.", run_id="limits", hooks=[Findings(action, 10)]
        )
    assert (transcript.stopped, transcript.failure, transcript.final_text) == (
        "answered",
        None,
        "The answer.",
    )
    assert transcript.delivered == {}  # the round's finding reached no model


@pytest.mark.parametrize("budget", [0, -1])
async def test_a_budget_that_admits_nothing_is_refused(budget: int) -> None:
    async with WorkerClient("http://127.0.0.1:1", "test-secret-worker") as client:
        with pytest.raises(ValueError, match="at least 1"):
            await run_task(
                ScriptedModel([]),
                Toolbox(client, CONTEXT),
                "Hi.",
                run_id="limits",
                max_input_chars=budget,
            )


def test_the_budget_flag_must_admit_something(capsys: pytest.CaptureFixture[str]) -> None:
    argv = [
        "run", "--worker", "http://127.0.0.1:1", "--token-file", "unused",
        "--project", "p", "--session", "s", "--environment", "env",
        "--script", "unused.json", "--max-input-chars", "0", "Hi.",
    ]  # fmt: skip
    with pytest.raises(SystemExit) as exited:
        cli.main(argv)
    assert exited.value.code == 2
    assert "--max-input-chars must be at least 1" in capsys.readouterr().err


# --- Through the command line ----------------------------------------------------------


async def test_a_run_rides_out_a_rate_limit_and_records_its_usage(harness: Harness) -> None:
    record = await harness.run(
        "run-1",
        [
            Reply(calls=[call("plan_read")]),
            Reply(status=429, retry_after="0"),
            Reply("The answer."),
        ],
    )
    assert (record.stopped, record.final_text) == ("answered", "The answer.")
    # One entry per answered call; the scripted gateway counts messages as input tokens.
    first, _, last = (len(messages) for messages in harness.gateway.main)
    assert [usage.model_dump(exclude={"characters"}) for usage in record.usage] == [
        {
            "intent_id": None,  # the main response intent is journaled separately
            "model": "scripted-main",
            "input_tokens": tokens,
            "output_tokens": 1,
            "cache_read_input_tokens": None,
            "cache_creation_input_tokens": None,
            "cost": None,
            "cost_source": None,  # the scripted gateway gives no prices either
            "gateway_version": GATEWAY_VERSION,  # each call's, as the run record keeps it
            "thinking_replay": None,  # chat completions sends no thinking back
            "retried": retried,
        }
        for tokens, retried in ((first, []), (last, ["HTTP 429"]))
    ]
    assert all(usage.characters for usage in record.usage)  # what each call sent and got


async def test_a_run_rides_out_a_gateway_timeout_and_counts_only_the_answered_call(
    harness: Harness,
) -> None:
    """Lab 44, B3: Cloudflare's 524 for a long call did not end the run in error; the
    retried attempt left no usage, cost or turn of its own."""
    record = await harness.run(
        "run-1",
        [
            Reply(calls=[call("plan_read")]),
            Reply(status=524, error=CLOUDFLARE_524, retry_after="0"),
            Reply("The answer.", cost="0.0021"),
        ],
    )
    assert (record.stopped, record.failure, record.final_text) == (
        "answered",
        None,
        "The answer.",
    )
    assert [(usage.retried, usage.cost) for usage in record.usage] == [
        ([], None),
        (["HTTP 524"], 0.0021),
    ]


async def test_a_run_records_each_calls_cost_beside_its_tokens(harness: Harness) -> None:
    record = await harness.run(
        "run-1", [Reply(calls=[call("plan_read")], cost="0.0021"), Reply("The answer.")]
    )
    assert [usage.cost for usage in record.usage] == [0.0021, None]  # none is made up
    assert record.usage[0].input_tokens is not None


async def test_the_gateways_context_error_stops_the_run_without_an_answer(
    harness: Harness,
) -> None:
    record = await harness.run(
        "run-1",
        [Reply(calls=[call("plan_read")]), Reply(status=400, error=TOO_LONG)],
        status=3,
    )
    assert (record.stopped, record.failure, record.final_text) == ("context_limit", None, None)
    assert len(record.usage) == 1


async def test_a_spent_gateway_budget_stops_the_run_where_it_refused(harness: Harness) -> None:
    record = await harness.run(
        "run-1",
        [Reply(calls=[call("plan_read")]), Reply(status=400, error=json.dumps(SPENT))],
        status=3,
    )
    assert (record.stopped, record.failure, record.final_text) == ("gateway_budget", None, None)
    assert len(harness.gateway.main) == 2  # the refused call was not tried again
    assert [tool["tool"] for tool in record.tools] == ["plan_read"]
    assert len(record.usage) == 1


async def test_the_budget_flag_stops_the_run_before_the_first_call(harness: Harness) -> None:
    record = await harness.run("run-1", [], status=3, max_input_chars=100)
    assert harness.gateway.main == []
    assert (record.stopped, record.usage) == ("context_limit", [])
