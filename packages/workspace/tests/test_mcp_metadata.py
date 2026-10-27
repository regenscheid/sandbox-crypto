# SPDX-License-Identifier: MIT
"""The user's availability check asks the control service, with the workspace's token, for
one listing by the agent runtime's client (D98), and keeps nothing but the status."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient

from sandbox_science.workspace.mcp_metadata import AvailabilityClient

ClientFactory = Callable[..., Awaitable[TestClient[Any, Any]]]
TOKEN = "test-secret-workspace-check-0123456789abcdef"


async def test_a_check_asks_the_control_service_and_keeps_the_status(
    aiohttp_client: ClientFactory,
) -> None:
    asked: list[dict[str, Any]] = []
    answer: dict[str, Any] = {
        "status": "listed",
        "tool_count": 12,
        "digest": "d" * 64,
        "error_kind": None,
    }

    async def check(request: web.Request) -> web.Response:
        assert request.headers["Authorization"] == f"Bearer {TOKEN}"
        asked.append(await request.json())
        return web.json_response(answer)

    app = web.Application()
    app.router.add_post("/mcp-check", check)
    control = await aiohttp_client(app)
    client = AvailabilityClient(str(control.make_url("")), TOKEN)
    found = await client.check("papers")
    assert (found.status, found.tool_count) == ("listed", 12)
    assert asked == [{"server": "papers"}]
    assert client.checks["papers"] == found
    assert "digest" not in found.model_dump()  # only the status and the count are kept
    answer = {"status": "timeout", "tool_count": None, "digest": None, "error_kind": "timeout"}
    assert (await client.check("papers")).status == "timeout"


@pytest.mark.parametrize(
    "reply",
    [
        web.Response(status=404),
        web.Response(status=500),
        web.Response(body=b"not json"),
        web.json_response({"status": "everything is fine"}),
        web.Response(body=b"{" + b" " * (64 * 1024 + 1) + b"}", content_type="application/json"),
    ],
)
async def test_anything_but_a_listing_outcome_is_unavailable(
    aiohttp_client: ClientFactory, reply: web.Response
) -> None:
    async def check(request: web.Request) -> web.Response:
        return reply

    app = web.Application()
    app.router.add_post("/mcp-check", check)
    control = await aiohttp_client(app)
    found = await AvailabilityClient(str(control.make_url("")), TOKEN).check("papers")
    assert found.status == "unavailable"


async def test_an_absent_control_service_is_unavailable() -> None:
    found = await AvailabilityClient("http://127.0.0.1:9", TOKEN).check("papers")
    assert found.status == "unavailable"


async def test_one_check_at_a_time(aiohttp_client: ClientFactory) -> None:
    running, most = 0, 0

    async def check(request: web.Request) -> web.Response:
        nonlocal running, most
        running += 1
        most = max(most, running)
        await asyncio.sleep(0.05)
        running -= 1
        return web.json_response({"status": "listed", "tool_count": 1})

    app = web.Application()
    app.router.add_post("/mcp-check", check)
    control = await aiohttp_client(app)
    client = AvailabilityClient(str(control.make_url("")), TOKEN)
    await asyncio.gather(client.check("a"), client.check("b"), client.check("c"))
    assert most == 1


@pytest.mark.parametrize(
    ("url", "token"),
    [
        ("http://control.example.org:8202", TOKEN),  # plain http off loopback
        ("https://user:pw@control.example.org", TOKEN),
        ("ftp://127.0.0.1", TOKEN),
        ("http://127.0.0.1:8202", "test-secret-line1\ntest-secret-line2-0123456789"),
        ("http://127.0.0.1:8202", "short"),
    ],
)
def test_an_address_or_token_the_client_should_not_use_is_refused(url: str, token: str) -> None:
    with pytest.raises(ValueError, match=r"control service|token") as refused:
        AvailabilityClient(url, token)
    assert "test-secret" not in str(refused.value)
