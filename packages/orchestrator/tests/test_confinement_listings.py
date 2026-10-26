# SPDX-License-Identifier: MIT
"""Listings the runtime's direct client reads (D98): it never shows a tool the compiled
native rules deny, and reports the server's whole listing over ``report_mcp_listing``,
which the host binds to the attempt's project, session and run and forwards to the worker,
for one of the attempt's connector servers only."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any, cast

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from pydantic import JsonValue

from sandbox_science.orchestrator.confinement.host import HostLoops
from sandbox_science.orchestrator.confinement.listings import (
    ControlListingReports,
    WorkerListings,
)
from sandbox_science.orchestrator.confinement.transport import Capability, Channel, TransportError
from sandbox_science.orchestrator.confinement.wire import ListingReportReply, ListingReportRequest
from sandbox_science.orchestrator.connectors import ConnectorError
from sandbox_science.orchestrator.mcp_direct import DirectClient, DirectServer
from sandbox_science.policy.effects import EffectClass
from sandbox_science.policy.mcp_listings import listing_digest
from sandbox_science.policy.mcp_wire import ListedTool
from sandbox_science.policy.openshell import ToolAdmission

ATTEMPT = "cap-" + "a" * 48
SERVER = "papers"
PAGES = (
    [
        {"name": "search", "description": "Find", "inputSchema": {"type": "object"}},
        {"name": "read_a", "description": "Read", "inputSchema": {"type": "object"}},
        {"name": "read_secret", "description": "Denied", "inputSchema": {"type": "object"}},
    ],
    [{"name": "write_x", "description": "Write", "annotations": {"readOnlyHint": False}}],
)
RULES = ToolAdmission(allow=("search", "read_*"), deny=("read_secret",))


@pytest.fixture
async def server() -> AsyncIterator[str]:
    async def mcp(request: web.Request) -> web.Response:
        message = json.loads(await request.read())
        if "id" not in message:
            return web.Response(status=202)
        if message["method"] == "initialize":
            result: dict[str, Any] = {"protocolVersion": "2025-11-25", "capabilities": {}}
        else:
            cursor = (message.get("params") or {}).get("cursor")
            page = 1 if cursor == "2" else 0
            result = {"tools": PAGES[page], **({"nextCursor": "2"} if page == 0 else {})}
        return web.json_response({"jsonrpc": "2.0", "id": message["id"], "result": result})

    app = web.Application()
    app.router.add_post("/mcp", mcp)
    async with TestServer(app) as backend:
        yield str(backend.make_url("/mcp"))


@dataclass
class Reporter:
    reports: list[tuple[str, list[ListedTool], str]] = field(
        default_factory=list[tuple[str, list[ListedTool], str]]
    )
    fail: bool = False

    async def __call__(self, server: str, tools: Sequence[ListedTool], listing: str) -> None:
        if self.fail:
            raise TransportError("denied", "the server is not one this caller lists")
        self.reports.append((server, list(tools), listing))


async def pages(client: DirectClient) -> list[list[str]]:
    first = await client.request(SERVER, "tools/list", {}, session="s-1")
    second = await client.request(SERVER, "tools/list", {"cursor": "2"}, session="s-1")
    return [[tool["name"] for tool in page["tools"]] for page in (first, second)]


async def test_no_denied_tool_is_shown_and_the_servers_whole_listing_is_reported(
    server: str,
) -> None:
    reporter = Reporter()
    client = DirectClient(
        {SERVER: DirectServer(server, tools=RULES)}, project="crypto", reporter=reporter
    )
    try:
        assert await pages(client) == [["search", "read_a"], []]
    finally:
        await client.close()
    ((name, tools, listing),) = reporter.reports  # once, when the listing is complete
    assert name == SERVER
    assert [tool.name for tool in tools] == ["search", "read_a", "read_secret", "write_x"]
    assert listing == listing_digest({tool.name: tool for tool in tools})


async def test_a_server_whose_rules_are_not_given_shows_no_tool(server: str) -> None:
    client = DirectClient({SERVER: DirectServer(server)}, project="crypto", reporter=Reporter())
    try:
        assert await pages(client) == [[], []]
    finally:
        await client.close()


async def test_a_listing_the_control_side_does_not_take_is_not_shown(server: str) -> None:
    client = DirectClient(
        {SERVER: DirectServer(server, tools=RULES)}, project="crypto", reporter=Reporter(fail=True)
    )
    try:
        await client.request(SERVER, "tools/list", {}, session="s-1")
        with pytest.raises(ConnectorError) as error:
            await client.request(SERVER, "tools/list", {"cursor": "2"}, session="s-1")
    finally:
        await client.close()
    assert error.value.kind == "unavailable"


@dataclass
class Sink:
    """The host's forwarder to the worker, as a fake: what it was asked."""

    asked: list[tuple[ListingReportRequest, str, str | None, str | None]] = field(
        default_factory=list[tuple[ListingReportRequest, str, str | None, str | None]]
    )

    async def report(
        self, value: ListingReportRequest, project: str, session: str | None, run: str | None
    ) -> ListingReportReply:
        self.asked.append((value, project, session, run))
        return ListingReportReply(drifted=True)


@pytest.fixture
async def linked() -> AsyncIterator[tuple[HostLoops, Channel, Sink]]:
    sink = Sink()
    host: HostLoops | None = None

    async def host_handle(kind: Capability, value: JsonValue) -> JsonValue:
        assert host is not None
        return await host.capability(kind, value)

    async def vm_handle(kind: Capability, value: JsonValue) -> JsonValue:
        raise TransportError("denied", "fixture refused capability")

    host_channel = Channel(lambda text: vm_channel.receive(text), host_handle)
    vm_channel = Channel(lambda text: host_channel.receive(text), vm_handle)
    host = HostLoops(host_channel, frozenset({"crypto"}), listings=cast(WorkerListings, sink))
    host.attempts[ATTEMPT] = cast(
        Any,
        SimpleNamespace(
            owner=SimpleNamespace(project="crypto", session="s-1", run="main-1"),
            toolbox=SimpleNamespace(connectors=SimpleNamespace(servers=(SERVER,))),
        ),
    )
    try:
        yield host, vm_channel, sink
    finally:
        await vm_channel.close()
        await host_channel.close()


TOOLS = [ListedTool("search", "e" * 64, EffectClass.READ)]


async def test_the_host_binds_the_attempts_project_session_and_run(
    linked: tuple[HostLoops, Channel, Sink],
) -> None:
    _, vm, sink = linked
    await ControlListingReports(vm, ATTEMPT)(SERVER, TOOLS, "f" * 64)
    ((value, project, session, run),) = sink.asked
    assert (value.server, value.listing, project, session, run) == (
        SERVER,
        "f" * 64,
        "crypto",
        "s-1",
        "main-1",
    )


async def test_a_server_attempt_or_host_the_report_is_not_bound_to_is_refused(
    linked: tuple[HostLoops, Channel, Sink],
) -> None:
    host, vm, sink = linked
    with pytest.raises(TransportError) as error:
        await ControlListingReports(vm, ATTEMPT)("elsewhere", TOOLS, "f" * 64)
    assert error.value.kind == "denied"
    with pytest.raises(TransportError):
        await ControlListingReports(vm, "cap-" + "c" * 48)(SERVER, TOOLS, "f" * 64)
    host.listings = None
    with pytest.raises(TransportError):
        await ControlListingReports(vm, ATTEMPT)(SERVER, TOOLS, "f" * 64)
    assert sink.asked == []
