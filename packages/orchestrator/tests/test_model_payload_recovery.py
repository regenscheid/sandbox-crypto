# SPDX-License-Identifier: MIT
"""Truncated HTTP bodies retry without exposing partial model tool calls."""

from __future__ import annotations

import json

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from sandbox_science.orchestrator.loop import run_task
from sandbox_science.orchestrator.model import ModelError
from sandbox_science.orchestrator.responses import ResponsesModel

from .test_model_limits import Waits
from .test_responses_model import KEY, called, response, said
from .test_tool_journal import toolbox


class TruncatedGateway:
    def __init__(self, prefix: str, *, failures: int = 1, status: int = 200):
        self.prefix, self.failures, self.status = prefix, failures, status
        self.calls = 0

    async def handle(self, request: web.Request) -> web.StreamResponse:
        assert request.headers["Authorization"] == f"Bearer {KEY}"
        assert (await request.json())["stream"] is True
        self.calls += 1
        if self.calls > self.failures:
            return web.Response(
                text="data: "
                + json.dumps(
                    {"type": "response.completed", "response": json.loads(response(said("Done")))}
                )
                + "\n\n",
                headers={"Content-Type": "text/event-stream", "x-litellm-response-cost": "0.03"},
            )
        stream = web.StreamResponse(
            status=self.status, headers={"Content-Type": "text/event-stream"}
        )
        await stream.prepare(request)
        await stream.write(self.prefix.encode())
        assert request.transport is not None
        request.transport.abort()  # No final chunk; real aiohttp ClientPayloadError.
        return stream


@pytest.mark.parametrize("terminal", [False, True])
async def test_truncated_partial_or_terminal_tool_response_is_discarded_before_retry(terminal):
    prefix = (
        {
            "type": "response.completed",
            "response": json.loads(response(called("unsafe", "arithmetic"))),
        }
        if terminal
        else {"type": "response.function_call_arguments.delta", "delta": '{"unfinished":'}
    )
    fake = TruncatedGateway("data: " + json.dumps(prefix) + "\n\n", failures=2)
    app = web.Application()
    app.router.add_post("/v1/responses", fake.handle)
    clock = Waits()

    async def forbidden(request):
        raise AssertionError("partial tool call must never dispatch")

    async with TestServer(app) as server:
        model = ResponsesModel(str(server.make_url("/v1")), "fixture", KEY, clock=clock)
        try:
            result = await run_task(model, toolbox(forbidden), task="Research", run_id="root")
        finally:
            await model.close()
    assert fake.calls == 3
    assert clock.slept == [2, 4]
    assert result.stopped == "answered"
    assert result.final_text == "Done"
    assert result.turns == 1
    assert result.context.usage[0].cost == 0.03
    assert result.context.usage[0].retried == ["incomplete response", "incomplete response"]


async def test_truncation_exhausts_ten_attempts_with_unknown_charge():
    fake = TruncatedGateway("data: ", failures=10)
    app = web.Application()
    app.router.add_post("/v1/responses", fake.handle)
    clock = Waits()
    async with TestServer(app) as server:
        model = ResponsesModel(str(server.make_url("/v1")), "fixture", KEY, clock=clock)
        try:
            with pytest.raises(ModelError, match="charge is unknown") as failed:
                await model.complete([{"role": "user", "content": "Research"}], [])
        finally:
            await model.close()
    assert fake.calls == 10
    assert clock.slept == [2, 4, 8, 16, 32, 60, 60, 60, 60]
    assert failed.value.cost is None


@pytest.mark.parametrize("status", [400, 401, 403, 429])
async def test_incomplete_error_body_does_not_retry_hidden_budget_or_auth_refusal(status):
    fake = TruncatedGateway('{"error":', failures=1, status=status)
    app = web.Application()
    app.router.add_post("/v1/responses", fake.handle)
    clock = Waits()
    async with TestServer(app) as server:
        model = ResponsesModel(str(server.make_url("/v1")), "fixture", KEY, clock=clock)
        try:
            with pytest.raises(ModelError, match="ClientPayloadError"):
                await model.complete([{"role": "user", "content": "Research"}], [])
        finally:
            await model.close()
    assert fake.calls == 1
    assert clock.slept == []
