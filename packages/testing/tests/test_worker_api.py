# SPDX-License-Identifier: MIT
"""The worker API client against a stand-in for the worker's HTTP API."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import aiohttp
import pytest
import pytest_asyncio
from aiohttp import web
from aiohttp.test_utils import TestServer

from sandbox_science.testing import WorkerApi

TOKEN = "test-secret-api-token"
CONTEXT = {"project_id": "p1", "session_id": "s1", "environment_id": "env1"}


def stand_in() -> web.Application:
    """Executions that run when first read, with their events as JSON lines."""
    executions: dict[str, dict[str, Any]] = {}
    reads: dict[str, int] = {}

    def authorized(request: web.Request) -> None:
        if request.headers.get("Authorization") != f"Bearer {TOKEN}":
            raise web.HTTPUnauthorized()

    async def submit(request: web.Request) -> web.Response:
        authorized(request)
        body = await request.json()
        if body["context"] != CONTEXT:
            return web.json_response({"error": "wrong context"}, status=400)
        execution_id = f"exe-{body['operation_id']}"
        execution = {
            "execution_id": execution_id,
            "context": body["context"],
            "state": "queued",
            "result": None,
        }
        executions[execution_id] = execution
        return web.json_response(execution, status=201)

    async def get(request: web.Request) -> web.Response:
        authorized(request)
        execution_id = request.match_info["id"]
        reads[execution_id] = reads.get(execution_id, 0) + 1
        if reads[execution_id] >= 3:
            executions[execution_id]["state"] = "running"
        return web.json_response(executions[execution_id])

    async def events(request: web.Request) -> web.StreamResponse:
        authorized(request)
        execution_id = request.match_info["id"]
        ended = {**executions[execution_id], "state": "succeeded", "result": {"exit_code": 0}}
        lines = [
            {"type": "state", "seq": 1, "state": "running"},
            {"type": "stream", "seq": 2, "name": "stdout", "text": "hi\n"},
            {"type": "result", "seq": 3, "execution": ended},
        ]
        after = int(request.query["after"])
        response = web.StreamResponse()
        await response.prepare(request)
        await response.write(b"\n")  # a heartbeat
        for line in lines[after:]:
            await response.write(json.dumps(line).encode() + b"\n")
        await response.write_eof()
        return response

    async def cancel(request: web.Request) -> web.Response:
        authorized(request)
        assert await request.json() == {"context": CONTEXT}
        return web.json_response({"error": "ended"}, status=409)

    app = web.Application()
    app.add_routes(
        [
            web.post("/v1/executions", submit),
            web.get("/v1/executions/{id}", get),
            web.get("/v1/executions/{id}/events", events),
            web.post("/v1/executions/{id}/cancel", cancel),
        ]
    )
    return app


@pytest_asyncio.fixture
async def api() -> AsyncIterator[WorkerApi]:
    server = TestServer(stand_in())
    await server.start_server()
    try:
        async with aiohttp.ClientSession() as http:
            yield WorkerApi(http, str(server.make_url("/")), TOKEN, CONTEXT)
    finally:
        await server.close()


async def test_runs_an_execution_to_its_result(api: WorkerApi) -> None:
    ended = await api.run("op-1", kind="shell", script="echo hi")
    assert (ended["execution_id"], ended["state"]) == ("exe-op-1", "succeeded")
    events = [event async for event in api.events("exe-op-1", after=1)]
    assert [event["seq"] for event in events] == [2, 3]
    await api.wait_running("exe-op-1")


async def test_waits_for_a_state_and_reports_refusals(api: WorkerApi) -> None:
    await api.submit("op-2")
    running = await api.until_state("exe-op-2", "running", timeout=5)
    assert running["state"] == "running"
    with pytest.raises(AssertionError, match="HTTP 409"):
        await api.cancel("exe-op-2")
    with pytest.raises(AssertionError, match="HTTP 400"):
        await api.submit("op-3", {**CONTEXT, "session_id": "other"})
    status, _ = await api.post("/v1/executions", {"operation_id": "op-4", "context": CONTEXT})
    assert status == 201
