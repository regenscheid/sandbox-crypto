# SPDX-License-Identifier: MIT
"""Scoped kernel disclosure rejects incoherent or foreign HTTP responses."""

from __future__ import annotations

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from sandbox_science.contracts import ExecutionContext
from sandbox_science.orchestrator.worker_client import WorkerAPIError, WorkerClient
from sandbox_science.testing import fake_worker_app


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

    app = fake_worker_app()
    app.router.add_post("/v1/kernels/status", status)
    async with (
        TestServer(app) as server,
        WorkerClient(str(server.make_url("")), "test-secret-kernel-client") as client,
    ):
        with pytest.raises(WorkerAPIError):
            await client.kernel_status(context)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    [
        {"project_id": "other"},
        {"environment_id": "other"},
        {"session_id": "other"},
        {"observed_at": None},
        {"observed_at": "2026-10-12T12:00:00"},
        {"kernels": [{"kernel_id": "k", "language": "python", "generation": 1}] * 2},
        {
            "kernels": [
                {"kernel_id": "k", "language": "python", "generation": 1, "current_revision": 1}
            ]
        },
    ],
)
@pytest.mark.parametrize("lineage", [False, True])
async def test_kernel_observation_refuses_foreign_or_incoherent_http_response(
    change: dict[str, object], lineage: bool
) -> None:
    path = "/v1/kernels/lineage" if lineage else "/v1/kernels/observe"
    context = ExecutionContext(project_id="p", environment_id="e", session_id="s")

    async def observe(request: web.Request) -> web.Response:
        assert await request.json() == {"context": context.model_dump(mode="json")}
        return web.json_response(
            {
                **({"project_id": "p"} if lineage else {}),
                "environment_id": "e",
                "session_id": "s",
                "observed_at": "2026-10-12T12:00:00Z",
                **change,
            }
        )

    app = fake_worker_app()
    app.router.add_post(path, observe)
    async with (
        TestServer(app) as server,
        WorkerClient(str(server.make_url("")), "test-secret-kernel-client") as client,
    ):
        with pytest.raises(WorkerAPIError):
            await (
                client.observe_kernel_lineage(context)
                if lineage
                else client.observe_kernels(context)
            )


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [200, 503])
@pytest.mark.parametrize("lineage", [False, True])
async def test_kernel_observation_bounds_chunked_success_and_error_bodies(
    status: int, lineage: bool
) -> None:
    path = "/v1/kernels/lineage" if lineage else "/v1/kernels/observe"
    context = ExecutionContext(project_id="p", environment_id="e", session_id="s")

    async def oversized(request: web.Request) -> web.StreamResponse:
        response = web.StreamResponse(status=status)
        await response.prepare(request)
        await response.write(b" " * (8 * 1024))
        await response.write(b"x")
        return response

    app = fake_worker_app()
    app.router.add_post(path, oversized)
    async with (
        TestServer(app) as server,
        WorkerClient(str(server.make_url("")), "test-secret-kernel-client") as client,
    ):
        with pytest.raises(WorkerAPIError, match="over 8192 bytes"):
            await (
                client.observe_kernel_lineage(context)
                if lineage
                else client.observe_kernels(context)
            )


@pytest.mark.asyncio
@pytest.mark.parametrize("lineage", [False, True])
async def test_kernel_observation_does_not_follow_redirect_or_fallback_to_status(
    lineage: bool,
) -> None:
    path = "/v1/kernels/lineage" if lineage else "/v1/kernels/observe"
    context = ExecutionContext(project_id="p", environment_id="e", session_id="s")
    targets: list[str] = []

    async def target(request: web.Request) -> web.Response:
        targets.append(request.path)
        raise AssertionError("observer followed redirect or opened the explicit status path")

    redirected = fake_worker_app()
    redirected.router.add_route("*", "/target", target)
    async with TestServer(redirected) as destination:
        app = fake_worker_app()

        async def redirect(request: web.Request) -> web.Response:
            return web.Response(
                status=307, headers={"Location": str(destination.make_url("/target"))}
            )

        app.router.add_post(path, redirect)
        app.router.add_post("/v1/kernels/status", target)
        async with (
            TestServer(app) as server,
            WorkerClient(str(server.make_url("")), "test-secret-kernel-client") as client,
        ):
            with pytest.raises(WorkerAPIError) as error:
                await (
                    client.observe_kernel_lineage(context)
                    if lineage
                    else client.observe_kernels(context)
                )
            assert error.value.status == 307
    assert targets == []

    legacy = fake_worker_app()
    legacy.router.add_post("/v1/kernels/status", target)
    async with (
        TestServer(legacy) as server,
        WorkerClient(str(server.make_url("")), "test-secret-kernel-client") as client,
    ):
        with pytest.raises(WorkerAPIError) as error:
            await (
                client.observe_kernel_lineage(context)
                if lineage
                else client.observe_kernels(context)
            )
        assert error.value.status == 404
    assert targets == []


@pytest.mark.asyncio
@pytest.mark.parametrize("headers_sent", [False, True])
@pytest.mark.parametrize("lineage", [False, True])
async def test_kernel_observation_whole_http_deadline_covers_headers_and_body(
    headers_sent: bool, lineage: bool
) -> None:
    import asyncio

    path = "/v1/kernels/lineage" if lineage else "/v1/kernels/observe"
    context = ExecutionContext(project_id="p", environment_id="e", session_id="s")
    release = asyncio.Event()

    async def delayed(request: web.Request) -> web.StreamResponse:
        response = web.StreamResponse()
        if headers_sent:
            await response.prepare(request)
        await release.wait()
        return response

    app = fake_worker_app()
    app.router.add_post(path, delayed)
    # The handler waits until released, so the client's deadline is the only way out.
    async with (
        TestServer(app) as server,
        WorkerClient(
            str(server.make_url("")), "test-secret-kernel-client", observation_timeout=0.05
        ) as client,
    ):
        try:
            with pytest.raises(WorkerAPIError) as error:
                await asyncio.wait_for(
                    (
                        client.observe_kernel_lineage(context)
                        if lineage
                        else client.observe_kernels(context)
                    ),
                    0.5,
                )
            assert error.value.kind == "unavailable"
        finally:
            release.set()
