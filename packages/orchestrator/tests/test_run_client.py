# SPDX-License-Identifier: MIT
"""Selected client refuses cross-scope, malformed and uncertain transport replies."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from typing import cast

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from sandbox_science.contracts import ErrorKind, Execution, ExecutionContext, ExecutionState
from sandbox_science.contracts.run_api import MAX_RUN_API_RESPONSE
from sandbox_science.contracts.run_cells import RunCellExecution, RunCellRequest
from sandbox_science.contracts.run_kernels import (
    RunKernelBinding,
    RunKernelScope,
    run_kernel_binding_sha256,
)
from sandbox_science.orchestrator.worker_client import WorkerAPIError, WorkerClient

# Synthetic transport owners intentionally inspect actual request/session ownership.
# pyright: reportPrivateUsage=false
pytestmark = pytest.mark.asyncio
BINDING = RunKernelBinding(
    scope=RunKernelScope(
        project_id="p",
        session_id="s",
        environment_id="e",
        parent_run_id="parent",
        delegation_id="dlg-child",
        child_run_id="child",
    ),
    sandbox="synthetic-vm",
    sandbox_id="synthetic-vm-id",
    incarnation="inc-" + "a" * 24,
)
SELECTED = RunCellExecution(
    binding=BINDING,
    binding_sha256=run_kernel_binding_sha256(BINDING),
    execution=Execution(
        execution_id="exe-" + "a" * 24,
        attempt_id="att-" + "b" * 24,
        operation_id="cell",
        kind="cell",
        context=ExecutionContext(project_id="p", session_id="s", environment_id="e"),
        state=ExecutionState.QUEUED,
        submitted_at=datetime.now(UTC),
    ),
)


@pytest.mark.parametrize(
    "damage", ["wrong_attempt", "wrong_binding", "duplicate", "extra", "redirect", "oversize_error"]
)
async def test_actual_http_response_strictness_no_redirect_or_legacy_fallback(damage: str) -> None:
    calls: list[str] = []

    async def handle(request: web.Request) -> web.StreamResponse:
        calls.append(request.path)
        assert request.path == "/v1/run-kernels/get"
        value = SELECTED.model_dump(mode="json")
        if damage == "wrong_attempt":
            value["execution"]["attempt_id"] = "att-" + "c" * 24
        elif damage == "wrong_binding":
            value["binding"]["scope"]["child_run_id"] = "other"
        elif damage == "extra":
            value["authority"] = "forged"
        elif damage == "redirect":
            return web.Response(status=307, headers={"Location": "/v1/executions/legacy"})
        elif damage == "oversize_error":
            response = web.StreamResponse(status=503)
            await response.prepare(request)
            try:
                for _ in range(MAX_RUN_API_RESPONSE // 4096 + 2):
                    await response.write(b"x" * 4096)
            except ConnectionResetError:
                pass
            return response
        raw = json.dumps(value).encode()
        if damage == "duplicate":
            raw = raw.replace(
                b'"operation_id": "cell"', b'"operation_id": "old", "operation_id": "cell"'
            )
        return web.Response(body=raw, content_type="application/json")

    app = web.Application()
    app.router.add_route("*", "/{path:.*}", handle)
    async with (
        TestServer(app) as server,
        WorkerClient(str(server.make_url("")), "test-secret-selected-client") as client,
    ):
        with pytest.raises(WorkerAPIError):
            await client.get_run(
                BINDING, SELECTED.execution.execution_id, SELECTED.execution.attempt_id
            )
    assert calls == ["/v1/run-kernels/get"]


async def test_submit_unknown_transport_is_single_attempt_and_no_legacy_fallback() -> None:
    calls = 0

    async def handle(request: web.Request) -> web.Response:
        nonlocal calls
        calls += 1
        assert request.path == "/v1/run-kernels/submit"
        await request.read()
        return web.Response(
            status=503, body=b'{"error":{"kind":"unavailable","message":"synthetic lost ack"}}'
        )

    app = web.Application()
    app.router.add_post("/v1/run-kernels/submit", handle)
    async with (
        TestServer(app) as server,
        WorkerClient(str(server.make_url("")), "test-secret-selected-client") as client,
    ):
        with pytest.raises(WorkerAPIError) as error:
            await client.submit_run_cell(
                RunCellRequest(
                    context=SELECTED.execution.context,
                    binding=BINDING,
                    operation_id="cell",
                    code="synthetic",
                )
            )
        assert error.value.kind is ErrorKind.UNAVAILABLE
    assert calls == 1


async def test_copied_host_preflight_refuses_before_http() -> None:
    client = WorkerClient("http://127.0.0.1:1", "test-secret-selected-client")
    copied = BINDING.model_copy(update={"undeclared": "x"})
    with pytest.raises(WorkerAPIError) as error:
        await client.get_run(copied, SELECTED.execution.execution_id, SELECTED.execution.attempt_id)
    assert error.value.kind is ErrorKind.INVALID_REQUEST
    assert client._session is None
    await client.close()


@pytest.mark.parametrize("late_error", [False, True])
async def test_ignored_abort_and_repeated_close_keep_actual_body_owner(late_error: bool) -> None:
    entered, release = asyncio.Event(), asyncio.Event()
    exits = 0

    class Content:
        async def iter_chunked(self, _: int):
            yield SELECTED.model_dump_json().encode()

    class Response:
        status = 503 if late_error else 200
        content = Content()

    class Context:
        async def __aenter__(self):
            entered.set()
            while not release.is_set():
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    continue
            return Response()

        async def __aexit__(self, *_: object):
            nonlocal exits
            exits += 1

    class Session:
        closed = False
        closes = 0

        def post(self, *_: object, **__: object):
            return Context()

        async def close(self):
            self.closes += 1
            self.closed = True

    session = Session()
    client = WorkerClient(
        "http://127.0.0.1:1",
        "test-secret-selected-client",
        session=cast(aiohttp.ClientSession, session),
    )
    client._owns_session = True
    request = asyncio.create_task(
        client.get_run(BINDING, SELECTED.execution.execution_id, SELECTED.execution.attempt_id)
    )
    await entered.wait()
    request.cancel("original")
    await asyncio.sleep(0)
    request.cancel("second")
    closer = asyncio.create_task(client.close())
    await asyncio.sleep(0)
    closer.cancel("original close")
    await asyncio.sleep(0)
    closer.cancel("second close")
    assert len(client._run_work) == 1
    assert not request.done()
    assert not closer.done()
    assert session.closes == 0
    release.set()
    with pytest.raises(asyncio.CancelledError) as error:
        await request
    assert error.value.args == ("original",)
    with pytest.raises(asyncio.CancelledError):
        await closer
    assert not client._run_work
    assert exits == 1
    assert session.closes == 1
    assert session.closed


@pytest.mark.parametrize("field", ["kernel", "result_kernel"])
async def test_foreign_main_kernel_in_selected_execution_or_result_refuses(field: str) -> None:
    value = SELECTED.model_dump(mode="json")
    kernel = {"kernel_id": "MAIN-kernel", "language": "python", "generation": 1}
    if field == "kernel":
        value["execution"]["kernel"] = kernel
    else:
        value["execution"]["result"] = {"kernel": kernel}

    async def get(_: web.Request) -> web.Response:
        return web.json_response(value)

    app = web.Application()
    app.router.add_post("/v1/run-kernels/get", get)
    async with (
        TestServer(app) as server,
        WorkerClient(str(server.make_url("")), "test-secret-client") as client,
    ):
        with pytest.raises(WorkerAPIError) as error:
            await client.get_run(
                BINDING, SELECTED.execution.execution_id, SELECTED.execution.attempt_id
            )
        assert error.value.kind is ErrorKind.UNAVAILABLE
