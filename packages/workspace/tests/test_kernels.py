# SPDX-License-Identifier: MIT
"""The kernel observation route with a stub worker and with the real worker client."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from sandbox_science.contracts import ErrorKind, ExecutionContext, KernelInfo, KernelObservation
from sandbox_science.orchestrator.projects import ProjectStore
from sandbox_science.orchestrator.worker_client import WorkerAPIError, WorkerClient
from sandbox_science.workspace import kernels
from sandbox_science.workspace.api import TIMEOUTS, Timeouts
from sandbox_science.workspace.app import create_app

TOKEN = "test-secret-kernel-workspace-login-0123456789"
PATH = "/api/v0/projects/p1/sessions/a/kernels"
ClientFactory = Callable[..., Awaitable[TestClient[Any, Any]]]
ServerFactory = Callable[..., Awaitable[TestServer]]


def observation(context: ExecutionContext) -> KernelObservation:
    return KernelObservation(
        environment_id=context.environment_id,
        session_id=context.session_id,
        current_revision=2,
        kernels=(
            KernelInfo(
                kernel_id="k-1",
                language="python",
                generation=1,
                start_revision=1,
                current_revision=2,
                revision_status="predates",
            ),
        ),
        observed_at=datetime.now(UTC),
    )


class Worker:
    def __init__(self) -> None:
        self.calls: list[ExecutionContext] = []
        self.error: ErrorKind | None = None
        self.empty = False
        self.closed = False

    async def observe_kernels(self, context: ExecutionContext) -> KernelObservation:
        self.calls.append(context)
        if self.error:
            raise WorkerAPIError(self.error, "synthetic unavailable")
        result = observation(context)
        return result.model_copy(update={"kernels": ()}) if self.empty else result

    async def close(self) -> None:
        self.closed = True


async def create(
    aiohttp_client: ClientFactory,
    state_dir: Path,
    static_dir: Path,
    worker: Worker | WorkerClient | None,
    *,
    login: bool = True,
    timeouts: Timeouts = TIMEOUTS,
) -> TestClient[Any, Any]:
    client = await aiohttp_client(
        create_app(
            ProjectStore(state_dir),
            static_dir,
            login_token=TOKEN,
            job_worker=cast(WorkerClient, worker),
            timeouts=timeouts,
        )
    )
    if login:
        assert (
            await client.get("/login", params={"token": TOKEN}, allow_redirects=False)
        ).status == 303
    return client


@pytest.mark.asyncio
async def test_passive_response_and_empty_are_typed_no_store(
    aiohttp_client: ClientFactory, state_dir: Path, static_dir: Path
) -> None:
    worker = Worker()
    store = ProjectStore(state_dir)
    original = (store.root / "p1.json").read_bytes()
    client = await create(aiohttp_client, state_dir, static_dir, worker)
    response = await client.get(PATH)
    assert response.status == 200
    assert response.headers["Cache-Control"] == "no-store"
    result = kernels.WorkspaceObservation.model_validate(await response.json())
    assert result.project_id == "p1"
    assert result.kernels[0].revision_status == "predates"
    assert worker.calls == [
        ExecutionContext(project_id="p1", session_id="a", environment_id=result.environment_id)
    ]
    assert (store.root / "p1.json").read_bytes() == original
    worker.empty = True
    response = await client.get(PATH)
    assert response.status == 200
    assert (await response.json())["kernels"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("path", "status"),
    [
        (PATH.replace("/a/", "/b/"), 404),
        (PATH.replace("/a/", "/missing/"), 404),
        (PATH.replace("/p1/", "/p2/"), 404),
        (PATH.replace("/p1/", "/missing/"), 404),
        (PATH + "?environment_id=forged", 400),
    ],
)
async def test_missing_closed_and_browser_context_never_call_worker(
    aiohttp_client: ClientFactory, state_dir: Path, static_dir: Path, path: str, status: int
) -> None:
    worker = Worker()
    client = await create(aiohttp_client, state_dir, static_dir, worker)
    response = await client.get(path)
    assert response.status == status
    assert response.headers["Cache-Control"] == "no-store"
    assert worker.calls == []


@pytest.mark.asyncio
async def test_unconfigured_is_explicit_and_unauthenticated_never_queries(
    aiohttp_client: ClientFactory, state_dir: Path, static_dir: Path
) -> None:
    client = await create(aiohttp_client, state_dir, static_dir, None, login=False)
    assert (await client.get(PATH)).status == 401
    await client.get("/login", params={"token": TOKEN}, allow_redirects=False)
    response = await client.get(PATH)
    assert response.status == 422
    assert (await response.json())["error"]["kind"] == "unsupported"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kind", "status"), [(ErrorKind.UNSUPPORTED, 422), (ErrorKind.UNAVAILABLE, 503)]
)
async def test_unavailable_does_not_become_empty_or_fallback(
    aiohttp_client: ClientFactory, state_dir: Path, static_dir: Path, kind: ErrorKind, status: int
) -> None:
    worker = Worker()
    worker.error = kind
    client = await create(aiohttp_client, state_dir, static_dir, worker)
    response = await client.get(PATH)
    assert response.status == status
    assert response.headers["Cache-Control"] == "no-store"
    assert "kernels" not in await response.json()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["valid", "scope", "duplicate", "large", "redirect", "slow"])
async def test_real_worker_client_http_composition(
    aiohttp_client: ClientFactory,
    aiohttp_server: ServerFactory,
    state_dir: Path,
    static_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
) -> None:
    calls: list[str] = []
    redirected: list[bool] = []
    never = asyncio.Event()

    async def peer(request: web.Request) -> web.Response:
        calls.append(request.path)
        assert request.headers["Authorization"] == "Bearer test-secret-observer-worker"
        value = await request.json()
        context = ExecutionContext.model_validate(value["context"])
        assert context.project_id == "p1"
        assert context.session_id == "a"
        result = observation(context).model_dump(mode="json")
        if mode == "scope":
            result["session_id"] = "c"
        if mode == "duplicate":
            result["kernels"] *= 2
        if mode == "large":
            return web.Response(body=b" " * 8193)
        if mode == "redirect":
            raise web.HTTPTemporaryRedirect("/stolen")
        if mode == "slow":
            await never.wait()  # so the workspace's deadline is the only way out
        return web.json_response(result)

    async def stolen(request: web.Request) -> web.Response:
        redirected.append(True)
        return web.json_response({})

    app = web.Application()
    app.router.add_post("/v1/kernels/observe", peer)
    app.router.add_post("/stolen", stolen)
    server = await aiohttp_server(app)
    worker = WorkerClient(str(server.make_url("/")).rstrip("/"), "test-secret-observer-worker")
    # The deadline covers the registry read and the connection too; leave room for them.
    timeouts = Timeouts(call=0.5) if mode == "slow" else TIMEOUTS
    client = await create(aiohttp_client, state_dir, static_dir, worker, timeouts=timeouts)
    response = await client.get(PATH)
    assert response.status == (200 if mode == "valid" else 503)
    assert response.headers["Cache-Control"] == "no-store"
    assert calls == ["/v1/kernels/observe"]
    assert not redirected
    if mode == "valid":
        result = kernels.WorkspaceObservation.model_validate(await response.json())
        assert result.kernels[0].revision_status == "predates"
        assert "test-secret" not in await response.text()
