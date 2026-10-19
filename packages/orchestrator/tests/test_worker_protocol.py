# SPDX-License-Identifier: MIT
"""The worker client names its protocol version and refuses a worker of another one."""

from __future__ import annotations

from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from sandbox_science.contracts import ErrorKind
from sandbox_science.contracts.recovery import RecoveryFence
from sandbox_science.contracts.worker import PROTOCOL_HEADER, PROTOCOL_VERSION
from sandbox_science.orchestrator.projects import ProjectStore, ensure_environment
from sandbox_science.orchestrator.worker_client import (
    WorkerAPIError,
    WorkerClient,
    WorkerProtocolError,
)

TOKEN = "test-secret-protocol-client"
CAPABILITIES = {
    "protocol": PROTOCOL_VERSION,
    "languages": ["python"],
    "location": {"pool_id": "pool", "backend": "local", "hostname": "host"},
}


def worker(spoken: str | None, seen: list[str | None]) -> web.Application:
    """A worker that answers every route as protocol ``spoken`` (no header for None)."""

    async def answer(request: web.Request) -> web.StreamResponse:
        seen.append(request.headers.get(PROTOCOL_HEADER))
        headers = {} if spoken is None else {PROTOCOL_HEADER: spoken}
        if request.path.endswith("/events"):
            response = web.StreamResponse(headers=headers)
            await response.prepare(request)
            await response.write(b"\n")
            return response
        return web.json_response(CAPABILITIES, headers=headers)

    app = web.Application()
    app.router.add_route("*", "/{tail:.*}", answer)
    return app


@pytest.mark.parametrize("spoken", [None, "1", str(PROTOCOL_VERSION + 1), ""])
async def test_a_worker_of_another_version_is_refused_with_a_clear_error(
    spoken: str | None,
) -> None:
    seen: list[str | None] = []
    async with (
        TestServer(worker(spoken, seen)) as server,
        WorkerClient(str(server.make_url("")), TOKEN) as client,
    ):
        for call in (
            client.capabilities("env1"),
            anext(client.events("exe-1")),
            client.artifact_content("art-1"),
        ):
            with pytest.raises(WorkerAPIError) as error:
                await call
            assert error.value.kind is ErrorKind.UNSUPPORTED
            assert "version mismatch" in error.value.message
            assert ("no protocol version" if spoken is None else repr(spoken)) in (
                error.value.message
            )
    # Each call asked once and named this client's version: a refusal is not retried.
    assert seen == [str(PROTOCOL_VERSION)] * 3


async def test_the_same_version_is_served() -> None:
    seen: list[str | None] = []
    async with (
        TestServer(worker(str(PROTOCOL_VERSION), seen)) as server,
        WorkerClient(str(server.make_url("")), TOKEN) as client,
    ):
        assert (await client.capabilities("env1")).protocol == PROTOCOL_VERSION
    assert seen == [str(PROTOCOL_VERSION)]


def gateway(status: int) -> web.Application:
    """A proxy or TLS terminator in front of the worker, answering on its behalf."""

    async def answer(request: web.Request) -> web.Response:
        return web.Response(status=status, text="<html>upstream connect error</html>")

    app = web.Application()
    app.router.add_route("*", "/{tail:.*}", answer)
    return app


def old_worker() -> web.Application:
    """A protocol-1 worker that does not know the environment."""

    async def answer(request: web.Request) -> web.Response:
        body = {"error": {"kind": "not_found", "message": "no environment"}}
        return web.json_response(body, status=404, headers={PROTOCOL_HEADER: "1"})

    app = web.Application()
    app.router.add_route("*", "/{tail:.*}", answer)
    return app


@pytest.mark.parametrize(
    ("status", "kind"),
    [
        (502, ErrorKind.UNAVAILABLE),
        (503, ErrorKind.UNAVAILABLE),
        (504, ErrorKind.UNAVAILABLE),
        (403, ErrorKind.INTERNAL),
        (400, ErrorKind.INTERNAL),
    ],
)
async def test_an_error_from_in_front_of_the_worker_keeps_its_status(
    status: int, kind: ErrorKind
) -> None:
    async with (
        TestServer(gateway(status)) as server,
        WorkerClient(str(server.make_url("")), TOKEN) as client,
    ):
        with pytest.raises(WorkerAPIError) as error:
            await client.capabilities("env1")
    assert not isinstance(error.value, WorkerProtocolError)
    assert (error.value.kind, error.value.status) == (kind, status)
    assert f"worker returned {status}: <html>upstream" in error.value.message


async def test_an_error_from_a_worker_of_another_version_is_a_mismatch() -> None:
    async with (
        TestServer(old_worker()) as server,
        WorkerClient(str(server.make_url("")), TOKEN) as client,
    ):
        with pytest.raises(WorkerProtocolError) as error:
            await client.environment("env1")
    assert error.value.kind is ErrorKind.UNSUPPORTED


@pytest.mark.parametrize("status", [500, 503])
async def test_proxy_failure_after_mutating_request_keeps_unknown_effect(status: int) -> None:
    async with (
        TestServer(gateway(status)) as server,
        WorkerClient(str(server.make_url("")), TOKEN) as client,
    ):
        with pytest.raises(WorkerAPIError) as submitted:
            await client.fence(RecoveryFence(project_id="p", session_id="s", epoch=1))
        with pytest.raises(WorkerAPIError) as observed:
            await client.capabilities("env1")
    assert submitted.value.uncertain is True
    assert observed.value.uncertain is False


@pytest.mark.parametrize("peer", ["old", "gateway"])
async def test_ensure_environment_never_skips_the_policy_for_a_mismatch_or_a_gateway(
    tmp_path: Path, peer: str
) -> None:
    project, _ = ProjectStore(tmp_path).create("p1", "minimal", "sha256:" + "a" * 64)
    app = old_worker() if peer == "old" else gateway(502)
    async with (
        TestServer(app) as server,
        WorkerClient(str(server.make_url("")), TOKEN) as client,
    ):
        # Both used to read as "the worker does not manage environments" (None), so a run
        # started without the project's policy applied.
        with pytest.raises(WorkerAPIError) as error:
            await ensure_environment(client, project, "env1")
    assert isinstance(error.value, WorkerProtocolError) == (peer == "old")
