# SPDX-License-Identifier: MIT
"""Scoped kernel disclosure rejects incoherent or foreign HTTP responses."""

from __future__ import annotations

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from sandbox_science.contracts import ExecutionContext
from sandbox_science.orchestrator.worker_client import WorkerAPIError, WorkerClient


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    [
        {"environment_id": "other"},
        {"session_id": "other"},
        {"kernels": [{"kernel_id": "k", "language": "python", "generation": 1}] * 2},
        {
            "kernels": [
                {"kernel_id": "k", "language": "python", "generation": 1, "current_revision": 1}
            ]
        },
    ],
)
async def test_kernel_status_refuses_foreign_or_incoherent_http_response(
    change: dict[str, object],
) -> None:
    context = ExecutionContext(project_id="p", environment_id="e", session_id="s")

    async def status(request: web.Request) -> web.Response:
        assert await request.json() == {"context": context.model_dump(mode="json")}
        return web.json_response({"environment_id": "e", "session_id": "s", **change})

    app = web.Application()
    app.router.add_post("/v1/kernels/status", status)
    async with (
        TestServer(app) as server,
        WorkerClient(str(server.make_url("")), "test-secret-kernel-client") as client,
    ):
        with pytest.raises(WorkerAPIError):
            await client.kernel_status(context)
