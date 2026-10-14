# SPDX-License-Identifier: MIT
"""A worker's HTTP API as tests use it: requests, and executions followed to their end.

It speaks HTTP only, so it checks a worker started in the test process and one
already running elsewhere alike (the conformance suite does both).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncGenerator, Mapping
from typing import Any

import aiohttp

from sandbox_science.contracts.worker import MAX_EVENT_BYTES

from .waiting import eventually

Json = dict[str, Any]


def _expect(status: int, allowed: tuple[int, ...], body: object) -> None:
    if status not in allowed:
        raise AssertionError(f"HTTP {status}: {body}")


class WorkerApi:
    """Calls a worker at ``url`` with ``token``; requests default to ``context``."""

    def __init__(
        self, http: aiohttp.ClientSession, url: str, token: str, context: Mapping[str, str]
    ) -> None:
        self.http = http
        self.url = url.rstrip("/")
        self.token = token
        self.context = dict(context)

    @property
    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"}

    async def post(self, path: str, body: Mapping[str, Any]) -> tuple[int, Json]:
        async with self.http.post(self.url + path, json=body, headers=self.headers) as response:
            return response.status, await response.json()

    async def get(self, path: str) -> tuple[int, Json]:
        async with self.http.get(self.url + path, headers=self.headers) as response:
            return response.status, await response.json()

    async def submit(
        self, operation_id: str, context: Mapping[str, str] | None = None, **request: Any
    ) -> Json:
        """Submit an execution and return it as the worker first reports it."""
        body = {"operation_id": operation_id, "context": dict(context or self.context), **request}
        status, execution = await self.post("/v1/executions", body)
        _expect(status, (200, 201), execution)
        return execution

    async def execution(self, execution_id: str) -> Json:
        status, execution = await self.get(f"/v1/executions/{execution_id}")
        _expect(status, (200,), execution)
        return execution

    async def events(self, execution_id: str, after: int = 0) -> AsyncGenerator[Json, None]:
        """The execution's events from ``after`` on, until the worker ends the stream."""
        async with self.http.get(
            f"{self.url}/v1/executions/{execution_id}/events",
            params={"after": str(after)},
            headers=self.headers,
            timeout=aiohttp.ClientTimeout(total=None),
        ) as response:
            _expect(response.status, (200,), "event stream")
            # A result event can be longer than aiohttp's own line limit.
            while line := await response.content.readline(max_line_length=MAX_EVENT_BYTES):
                if line.strip():  # an empty line is a heartbeat
                    yield json.loads(line)

    async def finish(self, execution_id: str, timeout: float = 120) -> Json:
        """Follow the execution's events to its result, and return the ended execution."""

        async def follow() -> Json:
            async with contextlib.aclosing(self.events(execution_id)) as events:
                async for event in events:
                    if event["type"] == "result":
                        return event["execution"]
            raise AssertionError("the event stream ended without a result")

        return await asyncio.wait_for(follow(), timeout)

    async def run(
        self, operation_id: str, context: Mapping[str, str] | None = None, **request: Any
    ) -> Json:
        """Submit an execution and follow it to its end."""
        execution = await self.submit(operation_id, context, **request)
        return await self.finish(execution["execution_id"])

    async def wait_running(self, execution_id: str, timeout: float = 60) -> None:
        """Until the execution's events say it runs."""

        async def follow() -> None:
            async with contextlib.aclosing(self.events(execution_id)) as events:
                async for event in events:
                    if event["type"] == "state" and event["state"] == "running":
                        return
            raise AssertionError("the execution never ran")

        await asyncio.wait_for(follow(), timeout)

    async def until_state(self, execution_id: str, state: str, timeout: float = 60) -> Json:
        """Until the worker reports the execution in ``state``; returns the execution."""

        async def reached() -> Json | None:
            execution = await self.execution(execution_id)
            return execution if execution["state"] == state else None

        return await eventually(
            reached, timeout=timeout, interval=0.02, describe=f"{execution_id} in {state}"
        )

    async def cancel(self, execution_id: str) -> Json:
        status, execution = await self.post(f"/v1/executions/{execution_id}/cancel", {})
        _expect(status, (200,), execution)
        return execution
