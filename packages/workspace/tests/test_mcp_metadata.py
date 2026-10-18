# SPDX-License-Identifier: MIT
"""Metadata inspection handles streaming responses without losing boundedness."""

import asyncio
import json
from collections.abc import Awaitable, Callable
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient

from sandbox_science.workspace.mcp_metadata import MetadataClient

ClientFactory = Callable[..., Awaitable[TestClient[Any, Any]]]


def metadata_body() -> bytes:
    return json.dumps(
        {
            "server": "papers",
            "observed_at": "2026-10-18T12:00:00Z",
            "policy": {
                "revision": "a" * 64,
                "protocol": "2025-11-25",
                "authentication": "bearer",
                "default_action": "deny",
                "listing_action": "allow",
                "worker_access": False,
                "worker_tools": [],
                "allow": [],
                "escalate": [],
                "deny": [],
                "argument_bytes": 1024,
                "response_bytes": 1024,
            },
        }
    ).encode()


async def test_metadata_waits_for_delayed_response_chunks(aiohttp_client: ClientFactory) -> None:
    first_sent, release = asyncio.Event(), asyncio.Event()
    raw = metadata_body()

    async def respond(request: web.Request) -> web.StreamResponse:
        assert request.headers["Authorization"] == "Bearer test-secret-metadata-caller"
        response = web.StreamResponse(headers={"Content-Type": "application/json"})
        await response.prepare(request)
        await response.write(raw[:20])
        first_sent.set()
        await release.wait()
        await response.write(raw[20:])
        await response.write_eof()
        return response

    app = web.Application()
    app.router.add_get("/metadata/papers", respond)
    proxy = await aiohttp_client(app)
    source = MetadataClient(str(proxy.make_url("")), "test-secret-metadata-caller")
    reading = asyncio.create_task(source.read("papers"))
    try:
        await asyncio.wait_for(first_sent.wait(), 1)
        # The peer withholds the rest: a short read must not become an invalid JSON result.
        done, _ = await asyncio.wait({reading}, timeout=0.02)
        assert not done
    finally:
        release.set()
        found = await reading
    assert found is not None
    assert found.server == "papers"
    assert source.last["papers"] == found


@pytest.mark.parametrize("extra", [0, 1])
async def test_metadata_response_size_bound(aiohttp_client: ClientFactory, extra: int) -> None:
    body = metadata_body()
    raw = body + b" " * (1024 * 1024 + extra - len(body))

    async def respond(_: web.Request) -> web.Response:
        return web.Response(body=raw, content_type="application/json")

    app = web.Application()
    app.router.add_get("/metadata/papers", respond)
    proxy = await aiohttp_client(app)
    source = MetadataClient(str(proxy.make_url("")), "test-secret-metadata-caller")
    found = await source.read("papers")
    assert (found is not None) == (extra == 0)
    assert ("papers" in source.last) == (extra == 0)
