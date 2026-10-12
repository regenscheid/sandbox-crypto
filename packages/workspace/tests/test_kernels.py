# SPDX-License-Identifier: MIT
"""Passive HTTP proxy, trusted registry authority and cancellation-owned cleanup."""

from __future__ import annotations

import asyncio
import json
import os
import threading
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from sandbox_science.contracts import ErrorKind, ExecutionContext, KernelInfo, KernelObservation
from sandbox_science.orchestrator.projects import ProjectError, ProjectStore
from sandbox_science.orchestrator.worker_client import WorkerAPIError, WorkerClient
from sandbox_science.workspace import kernels
from sandbox_science.workspace.app import create_app

TOKEN = "test-secret-kernel-workspace-login-0123456789"  # noqa: S105
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
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.blocked = False
        self.error: ErrorKind | None = None
        self.empty = False
        self.closed = False
        self.cancelled = False

    async def observe_kernels(self, context: ExecutionContext) -> KernelObservation:
        self.calls.append(context)
        self.entered.set()
        try:
            if self.blocked:
                await self.release.wait()
            if self.error:
                raise WorkerAPIError(self.error, "synthetic unavailable")
            result = observation(context)
            return result.model_copy(update={"kernels": ()}) if self.empty else result
        except asyncio.CancelledError:
            self.cancelled = True
            raise

    async def close(self) -> None:
        self.closed = True


async def create(
    aiohttp_client: ClientFactory,
    state_dir: Path,
    static_dir: Path,
    worker: Worker | WorkerClient | None,
    *,
    login: bool = True,
) -> TestClient[Any, Any]:
    client = await aiohttp_client(
        create_app(
            ProjectStore(state_dir),
            static_dir,
            login_token=TOKEN,
            job_worker=cast(WorkerClient, worker),
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
@pytest.mark.parametrize("invalid", ["scope", "duplicate", "revision", "timestamp", "generation"])
async def test_unvalidated_model_copy_cannot_escape_response_validation(
    aiohttp_client: ClientFactory, state_dir: Path, static_dir: Path, invalid: str
) -> None:
    class InvalidWorker(Worker):
        async def observe_kernels(self, context: ExecutionContext) -> KernelObservation:
            value = await super().observe_kernels(context)
            if invalid == "scope":
                return value.model_copy(update={"environment_id": "another"})
            if invalid == "duplicate":
                return value.model_copy(update={"kernels": value.kernels * 2})
            if invalid == "revision":
                return value.model_copy(update={"current_revision": 3})
            if invalid == "timestamp":
                return value.model_copy(update={"observed_at": datetime(2026, 10, 12)})
            return value.model_copy(
                update={"kernels": (value.kernels[0].model_copy(update={"generation": True}),)}
            )

    client = await create(aiohttp_client, state_dir, static_dir, InvalidWorker())
    response = await client.get(PATH)
    assert response.status == 503
    assert "kernels" not in await response.json()


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["close", "aba", "identical-aba", "rebind", "policy"])
async def test_delayed_reply_cannot_survive_registry_transition(
    aiohttp_client: ClientFactory, state_dir: Path, static_dir: Path, change: str
) -> None:
    worker = Worker()
    worker.blocked = True
    client = await create(aiohttp_client, state_dir, static_dir, worker)
    call = asyncio.create_task(client.get(PATH))
    await worker.entered.wait()
    store = ProjectStore(state_dir)
    original = (store.root / "p1.json").read_bytes()
    if change in {"close", "aba", "identical-aba"}:
        store.close_session("p1", "a")
        if change != "close":
            store.open_session("p1", "a", dedicated=False)
        if change == "identical-aba":
            path = store.root / "restored"
            path.write_bytes(original)
            os.replace(path, store.root / "p1.json")
    elif change == "policy":
        store.set_policy("p1", "sha256:" + "b" * 64)
    else:
        value = json.loads(original)
        value["sessions"]["a"]["environment_id"] = "other-environment"
        (store.root / "p1.json").write_text(json.dumps(value))
    worker.release.set()
    response = await call
    assert response.status == 503
    assert "kernels" not in await response.json()


@pytest.mark.asyncio
async def test_logout_revokes_the_original_inflight_cookie(
    aiohttp_client: ClientFactory, state_dir: Path, static_dir: Path
) -> None:
    worker = Worker()
    worker.blocked = True
    client = await create(aiohttp_client, state_dir, static_dir, worker)
    call = asyncio.create_task(client.get(PATH))
    await worker.entered.wait()
    origin = str(client.make_url("/")).rstrip("/")
    assert (await client.post("/api/v0/logout", headers={"Origin": origin})).status == 204
    await client.get("/login", params={"token": TOKEN}, allow_redirects=False)
    worker.release.set()
    assert (await call).status == 401
    assert (await client.get(PATH)).status == 200


@pytest.mark.asyncio
async def test_http_disconnect_is_not_remote_cleanup_proof(
    aiohttp_client: ClientFactory, state_dir: Path, static_dir: Path
) -> None:
    worker = Worker()
    worker.blocked = True
    client = await create(aiohttp_client, state_dir, static_dir, worker)
    proxy = client.app[kernels.PROXY]
    call = asyncio.create_task(client.get(PATH))
    try:
        await worker.entered.wait()
        call.cancel()
        await asyncio.gather(call, return_exceptions=True)
        # Default aiohttp handling continues the admitted server read after the
        # peer disappears; no global handler_cancellation switch is introduced.
        await asyncio.sleep(0)
        assert len(proxy._tasks) == 1
        assert not worker.cancelled
        worker.release.set()
        for _ in range(100):
            if not proxy._tasks:
                break
            await asyncio.sleep(0.001)
        assert not proxy._tasks
        assert (await client.get(PATH)).status == 200
    finally:
        worker.release.set()
        await asyncio.gather(call, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("damage", ["project", "session", "oversize"])
async def test_bounded_damaged_registry_cannot_dispatch(
    aiohttp_client: ClientFactory, state_dir: Path, static_dir: Path, damage: str
) -> None:
    worker = Worker()
    client = await create(aiohttp_client, state_dir, static_dir, worker)
    path = state_dir / "projects" / "p1.json"
    data = json.loads(path.read_text())
    if damage == "project":
        data["project_id"] = "p2"
    elif damage == "session":
        data["sessions"]["a"]["session_id"] = "c"
    path.write_text("x" * (1024 * 1024 + 1) if damage == "oversize" else json.dumps(data))
    assert (await client.get(PATH)).status == 503
    assert worker.calls == []


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
            await asyncio.sleep(0.1)
        return web.json_response(result)

    async def stolen(request: web.Request) -> web.Response:
        redirected.append(True)
        return web.json_response({})

    app = web.Application()
    app.router.add_post("/v1/kernels/observe", peer)
    app.router.add_post("/stolen", stolen)
    server = await aiohttp_server(app)
    worker = WorkerClient(str(server.make_url("/")).rstrip("/"), "test-secret-observer-worker")
    if mode == "slow":
        monkeypatch.setattr(kernels, "CALL_TIMEOUT", 0.03)
    client = await create(aiohttp_client, state_dir, static_dir, worker)
    response = await client.get(PATH)
    assert response.status == (200 if mode == "valid" else 503)
    assert response.headers["Cache-Control"] == "no-store"
    assert calls == ["/v1/kernels/observe"]
    assert not redirected
    if mode == "valid":
        result = kernels.WorkspaceObservation.model_validate(await response.json())
        assert result.kernels[0].revision_status == "predates"
        assert "test-secret" not in await response.text()


@pytest.mark.asyncio
async def test_admission_is_immediate_bounded_and_released_on_rpc_cancel(
    state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    worker = Worker()
    worker.blocked = True
    store = ProjectStore(state_dir)
    store.open_session("p1", "d", dedicated=False)
    proxy = kernels.KernelProxy(store, cast(WorkerClient, worker))
    first = asyncio.create_task(proxy.observe("p1", "a", "login", lambda: True))
    second = asyncio.create_task(proxy.observe("p1", "c", "login", lambda: True))
    await worker.entered.wait()
    try:
        with pytest.raises(ProjectError) as error:
            await proxy.observe("p1", "d", "login", lambda: True)
        assert error.value.kind is ErrorKind.UNAVAILABLE
        with pytest.raises(ProjectError):
            await proxy.observe("p1", "a", "other-login", lambda: True)
        monkeypatch.setattr(kernels, "MAX_CALLS", 2)
        with pytest.raises(ProjectError):
            await proxy.observe("p1", "d", "other-login", lambda: True)
        assert len(proxy._tasks) == 2
        first.cancel()
        second.cancel()
        await asyncio.gather(first, second, return_exceptions=True)
        await proxy.close()
        assert not proxy._tasks
        assert not proxy._scopes
        assert not proxy._logins
        assert worker.cancelled
    finally:
        worker.release.set()
        await proxy.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True])
async def test_thread_work_retains_admission_through_timeout_or_repeated_cancel_and_close(
    state_dir: Path, monkeypatch: pytest.MonkeyPatch, cancel: bool
) -> None:
    entered, release = threading.Event(), threading.Event()
    store = ProjectStore(state_dir)
    original = store.session_snapshot

    def blocked(project: str, session: str):
        entered.set()
        assert release.wait(5)
        return original(project, session)

    monkeypatch.setattr(store, "session_snapshot", blocked)
    monkeypatch.setattr(kernels, "CALL_TIMEOUT", 0.05)
    worker = Worker()
    proxy = kernels.KernelProxy(store, cast(WorkerClient, worker))
    call = asyncio.create_task(proxy.observe("p1", "a", "login", lambda: True))
    closing: asyncio.Task[None] | None = None
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        if cancel:
            call.cancel()
            call.cancel()
            with pytest.raises(asyncio.CancelledError):
                await call
        else:
            with pytest.raises(ProjectError) as error:
                await call
            assert error.value.kind is ErrorKind.UNAVAILABLE
        assert len(proxy._tasks) == 1
        with pytest.raises(ProjectError):
            await proxy.observe("p1", "a", "another-login", lambda: True)
        # This private file executor cannot monopolize the default worker pool.
        assert await asyncio.wait_for(asyncio.to_thread(lambda: 7), 0.2) == 7
        closing = asyncio.create_task(proxy.close())
        await asyncio.sleep(0)
        closing.cancel()
        await asyncio.sleep(0)
        closing.cancel()
        await asyncio.sleep(0)
        assert not closing.done()
        assert not proxy._closed
        assert worker.calls == []
    finally:
        release.set()
        if closing:
            await asyncio.gather(closing, return_exceptions=True)
        await proxy.close()
    assert proxy._closed
    assert not proxy._tasks


@pytest.mark.asyncio
async def test_app_cleanup_drains_file_owner_before_shared_client_close(
    aiohttp_client: ClientFactory,
    state_dir: Path,
    static_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker = Worker()
    client = await create(aiohttp_client, state_dir, static_dir, worker)
    proxy = client.app[kernels.PROXY]
    entered, release = threading.Event(), threading.Event()
    original = proxy.store.session_snapshot

    def blocked(project: str, session: str):
        entered.set()
        assert release.wait(5)
        return original(project, session)

    monkeypatch.setattr(proxy.store, "session_snapshot", blocked)
    call = asyncio.create_task(proxy.observe("p1", "a", "login", lambda: True))
    assert await asyncio.to_thread(entered.wait, 2)
    call.cancel()
    await asyncio.gather(call, return_exceptions=True)
    closing = asyncio.create_task(client.close())
    try:
        await asyncio.sleep(0.02)
        assert not closing.done()
        assert not worker.closed
    finally:
        release.set()
        await closing
    assert worker.closed
    assert proxy._closed
    assert not proxy._tasks
