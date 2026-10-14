# SPDX-License-Identifier: MIT
"""The worker client names its protocol version and refuses a worker of another one."""

from __future__ import annotations

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from sandbox_science.contracts import ErrorKind
from sandbox_science.contracts.worker import PROTOCOL_HEADER, PROTOCOL_VERSION
from sandbox_science.orchestrator.worker_client import WorkerAPIError, WorkerClient

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
