# SPDX-License-Identifier: MIT
"""MCP canaries on the direct path (D98, D103): the control service asks the runtime to list
a server's tools through its direct client (``list_mcp_tools``), bounded, never calling a
tool, and stores the outcome as any canary's; the workspace user's availability check asks
the same of one server (``POST /mcp-check``) and stores nothing."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from pydantic import JsonValue, ValidationError

from sandbox_science.orchestrator.confinement.canaries import (
    DirectServerSettings,
    DirectSettings,
    RuntimeListings,
    RuntimeSource,
)
from sandbox_science.orchestrator.confinement.runtime import RuntimeSettings
from sandbox_science.orchestrator.confinement.server import ControlSettings
from sandbox_science.orchestrator.confinement.transport import Capability, Channel, TransportError
from sandbox_science.orchestrator.confinement.wire import ToolsListed
from sandbox_science.orchestrator.connectors import ConnectorError
from sandbox_science.orchestrator.control import ControlStore, store_path
from sandbox_science.orchestrator.mcp_canaries import CanaryRunner, CanaryStore
from sandbox_science.testing import FakeClock

TOOL = {"name": "search", "description": "Find papers", "inputSchema": {"type": "object"}}


class Direct:
    """A fake direct client: answers each request from ``answers`` in turn, and remembers
    every method it was asked."""

    def __init__(self, *answers: dict[str, Any] | BaseException | None) -> None:
        self.answers = list(answers)
        self.methods: list[str] = []

    async def request(
        self,
        server: str,
        method: str,
        params: Mapping[str, object],
        *,
        session: str,
        run: str | None = None,
    ) -> dict[str, Any]:
        self.methods.append(method)
        answer = self.answers.pop(0) if self.answers else None
        if answer is None:
            await asyncio.Event().wait()  # a server that never answers
            raise AssertionError("unreachable")
        if isinstance(answer, BaseException):
            raise answer
        return answer


@dataclass
class Linked:
    source: RuntimeSource
    listings: RuntimeListings
    host: Channel


@pytest.fixture
async def linked() -> AsyncIterator[Linked]:
    # The host's bound is shorter than the runtime's, so the host's is the one tested.
    listings = RuntimeListings(None, frozenset({"papers"}), seconds=1.0)

    async def host_handle(kind: Capability, value: JsonValue) -> JsonValue:
        raise TransportError("denied", "fixture refused capability")

    async def vm_handle(kind: Capability, value: JsonValue) -> JsonValue:
        if kind is Capability.LIST_MCP_TOOLS:
            return await listings.list(value)
        raise TransportError("denied", "host requested an unavailable runtime capability")

    host_channel = Channel(lambda text: vm_channel.receive(text), host_handle)
    vm_channel = Channel(lambda text: host_channel.receive(text), vm_handle)
    try:
        yield Linked(RuntimeSource(lambda: host_channel, seconds=0.2), listings, host_channel)
    finally:
        await vm_channel.close()
        await host_channel.close()


async def test_the_runtime_lists_through_its_direct_client_and_never_calls_a_tool(
    linked: Linked,
) -> None:
    client = Direct({"tools": [TOOL], "nextCursor": "2"}, {"tools": [TOOL]})
    linked.listings.client = client
    listing = await linked.source.list("papers")
    assert (listing.status, listing.tool_count, listing.error_kind) == ("listed", 2, None)
    assert listing.digest is not None
    assert len(listing.digest) == 64
    assert client.methods == ["tools/list", "tools/list"]


async def test_a_listing_that_never_answers_is_a_timeout(linked: Linked) -> None:
    client = Direct()
    linked.listings.client = client
    listing = await linked.source.list("papers")
    assert (listing.status, listing.error_kind) == ("timeout", "timeout")
    assert client.methods == ["tools/list"]


async def test_failures_are_statuses_and_no_tool_is_ever_called(linked: Linked) -> None:
    # No runtime connected: unavailable, and nothing is asked.
    alone = RuntimeSource(lambda: None)
    assert (await alone.list("papers")).error_kind == "runtime_unavailable"
    # A runtime with no direct client refuses.
    listing = await linked.source.list("papers")
    assert (listing.status, listing.error_kind) == ("unavailable", "runtime_refused")
    # A server the runtime does not declare is never asked.
    client = Direct(ConnectorError("policy_denied", "no"))
    linked.listings.client = client
    listing = await linked.source.list("elsewhere")
    assert (listing.status, listing.error_kind) == ("unavailable", "undeclared_server")
    assert client.methods == []
    # OpenShell's refusal is denied, as through the proxy.
    listing = await linked.source.list("papers")
    assert (listing.status, listing.error_kind) == ("denied", "policy_denied")
    # A closed connection is unavailable.
    await linked.host.close()
    assert (await linked.source.list("papers")).error_kind == "runtime_unavailable"
    assert client.methods == ["tools/list"]


async def test_the_runner_stores_the_runtimes_listings(linked: Linked, tmp_path: Path) -> None:
    linked.listings.client = Direct({"tools": [TOOL]}, ConnectorError("upstream_error", "down"))
    control = ControlStore(store_path(tmp_path))
    control.initialize()
    try:
        store = CanaryStore(control)
        runner = CanaryRunner(store, linked.source, ["papers"], clock=FakeClock())
        await runner.run_once()
        await runner.run_once()
        first, second = store.history("papers")
        assert (first.status, first.tool_count) == ("listed", 1)
        assert (second.status, second.error_kind) == ("unavailable", "upstream_error")
    finally:
        control.close()


def test_a_listing_reply_is_consistent_or_refused() -> None:
    with pytest.raises(ValidationError):
        ToolsListed(status="listed", tool_count=1)
    with pytest.raises(ValidationError):
        ToolsListed(status="unavailable")
    with pytest.raises(ValidationError):
        ToolsListed(status="timeout", error_kind="Not A Kind")


def test_the_direct_client_and_the_canaries_are_trusted_settings() -> None:
    settings = DirectSettings(
        project="crypto",
        servers={
            "papers": DirectServerSettings(
                url="https://mcp.example.test/mcp",
                credentials={"CF-Access-Client-Id": "CF_ACCESS_CLIENT_ID"},
            )
        },
    )
    assert settings.client(None) is not None
    for credentials in ({"Bad Header": "X"}, {"X-Key": "not a variable"}):
        with pytest.raises(ValidationError):
            DirectServerSettings(url="https://mcp.example.test/mcp", credentials=credentials)
    with pytest.raises(ValidationError):
        DirectServerSettings(url="file:///etc/passwd")
    with pytest.raises(ValidationError):
        DirectSettings(project="crypto", servers={})
    fields = RuntimeSettings.model_fields
    assert fields["mcp"].default is None
    canaries = ControlSettings.model_fields["canaries"]
    assert canaries.default is None


async def test_the_control_service_runs_them_over_the_runtimes_connection(
    tmp_path: Path,
) -> None:
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    from sandbox_science.contracts.worker import PROTOCOL_HEADER, PROTOCOL_VERSION
    from sandbox_science.orchestrator.confinement.server import CanarySettings, create_app
    from sandbox_science.orchestrator.confinement.transport import (
        CONTROL_HEADER,
        CONTROL_PROTOCOL,
        Reply,
        Request,
    )
    from sandbox_science.testing import eventually

    from .test_confinement_control import TOKEN, WORKER, private

    async def ready(request: web.Request) -> web.Response:
        return web.json_response(
            {"protocol": PROTOCOL_VERSION, "ready": True},
            headers={PROTOCOL_HEADER: str(PROTOCOL_VERSION)},
        )

    forwarded: list[dict[str, Any]] = []

    async def listings(request: web.Request) -> web.Response:
        forwarded.append(await request.json())
        return web.json_response(
            {"drifted": True, "record": 7}, headers={PROTOCOL_HEADER: str(PROTOCOL_VERSION)}
        )

    worker = web.Application()
    worker.router.add_get("/v1/ready", ready)
    worker.router.add_post("/v1/mcp-listings", listings)
    async with TestServer(worker) as backend:
        settings = ControlSettings(
            state_dir=tmp_path / "state",
            worker=str(backend.make_url("")),
            worker_token_file=private(tmp_path / "worker.token", WORKER),
            runtime_token_file=private(tmp_path / "runtime.token", TOKEN),
            generation="runtime-test",
            projects=("p",),
            main="main",
            image_revision="a" * 40,
            canaries=CanarySettings(project="p", servers=("papers",), interval=60),
        )
        async with TestClient(TestServer(create_app(settings))) as client:
            headers = {"Authorization": "Bearer " + TOKEN, CONTROL_HEADER: str(CONTROL_PROTOCOL)}
            async with client.ws_connect("/control", headers=headers) as socket:
                hello = Request(
                    id=1, capability=Capability.HANDSHAKE, payload={"image_revision": "a" * 40}
                )
                await socket.send_str(hello.model_dump_json())
                asked: list[Request] = []
                # The handshake's reply, then the canary's request, in either order.
                for _ in range(2):
                    text = (await socket.receive(timeout=5)).data
                    if '"kind":"request"' in text:
                        asked.append(Request.model_validate_json(text))
                (request,) = asked
                assert request.capability is Capability.LIST_MCP_TOOLS
                assert request.payload == {"server": "papers"}

                async def ask(identity: int, server: str) -> Reply:
                    tool = {"name": "search", "digest": "e" * 64, "hint": "read"}
                    value = {"server": server, "tools": [tool], "listing": "f" * 64}
                    sent = Request(
                        id=identity, capability=Capability.REPORT_MCP_LISTING, payload=value
                    )
                    await socket.send_str(sent.model_dump_json())
                    return Reply.model_validate_json((await socket.receive(timeout=5)).data)

                # The runtime's canary client reports the listing it read: control names the
                # canaries' project, and forwards only their servers' listings to the worker.
                answered = await ask(2, "papers")
                assert (answered.error, answered.payload) == (None, {"drifted": True})
                (body,) = forwarded
                assert (body["project_id"], body["server"], body["listing"]) == (
                    "p",
                    "papers",
                    "f" * 64,
                )
                refused = await ask(3, "elsewhere")
                assert refused.error == "denied"
                assert len(forwarded) == 1
                listed = ToolsListed(status="listed", tool_count=3, digest="d" * 64)
                reply = Reply(id=request.id, payload=listed.model_dump(mode="json"))
                await socket.send_str(reply.model_dump_json())
                control = ControlStore(store_path(tmp_path / "state"), readonly=True)
                try:
                    store = CanaryStore(control)
                    await eventually(lambda: bool(store.history("papers")))
                    (row,) = store.history("papers")
                    assert (row.status, row.tool_count, row.digest) == ("listed", 3, "d" * 64)
                finally:
                    control.close()


async def test_the_users_check_lists_one_server_through_the_runtime(tmp_path: Path) -> None:
    """The workspace's "Check availability" (D98): with the workspace's token, one listing
    of a configured server by the connected runtime's client; anything else is 404."""
    import hashlib

    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    from sandbox_science.contracts.worker import PROTOCOL_HEADER, PROTOCOL_VERSION
    from sandbox_science.orchestrator.confinement.server import (
        DirectAccessSettings,
        create_app,
    )
    from sandbox_science.orchestrator.confinement.transport import (
        CONTROL_HEADER,
        CONTROL_PROTOCOL,
        Reply,
        Request,
    )

    from .test_confinement_control import TOKEN, WORKER, private

    async def ready(request: web.Request) -> web.Response:
        return web.json_response(
            {"protocol": PROTOCOL_VERSION, "ready": True},
            headers={PROTOCOL_HEADER: str(PROTOCOL_VERSION)},
        )

    worker = web.Application()
    worker.router.add_get("/v1/ready", ready)
    backend = TestServer(worker)
    await backend.start_server()
    check = "test-secret-workspace-check-0123456789abcdef"
    digest = private(tmp_path / "check.sha256", hashlib.sha256(check.encode()).hexdigest())
    base: dict[str, Any] = {
        "state_dir": tmp_path / "state",
        "worker": str(backend.make_url("")),
        "worker_token_file": private(tmp_path / "worker.token", WORKER),
        "runtime_token_file": private(tmp_path / "runtime.token", TOKEN),
        "generation": "runtime-test",
        "projects": ("p",),
        "main": "main",
        "image_revision": "a" * 40,
        "direct": DirectAccessSettings(project="p", servers=("papers",)),
    }
    allowed = {"Authorization": "Bearer " + check}
    # Without the workspace's token digest the route does not exist.
    async with TestClient(TestServer(create_app(ControlSettings(**base)))) as client:
        answer = await client.post("/mcp-check", json={"server": "papers"}, headers=allowed)
        assert answer.status == 404
    settings = ControlSettings(**base, check_token_sha256_file=digest)
    async with TestClient(TestServer(create_app(settings))) as client:
        wrong = {"Authorization": "Bearer test-secret-not-the-workspace-0123456789"}
        answer = await client.post("/mcp-check", json={"server": "papers"}, headers=wrong)
        assert answer.status == 404
        answer = await client.post("/mcp-check", json={"server": "other"}, headers=allowed)
        assert answer.status == 404
        answer = await client.post("/mcp-check", json={"tool": "papers"}, headers=allowed)
        assert answer.status == 400
        # No runtime connected: unavailable, said as a canary says it.
        answer = await client.post("/mcp-check", json={"server": "papers"}, headers=allowed)
        assert answer.status == 200
        assert await answer.json() == {
            "status": "unavailable",
            "tool_count": None,
            "digest": None,
            "error_kind": "runtime_unavailable",
        }
        headers = {"Authorization": "Bearer " + TOKEN, CONTROL_HEADER: str(CONTROL_PROTOCOL)}
        async with client.ws_connect("/control", headers=headers) as socket:
            hello = Request(
                id=1, capability=Capability.HANDSHAKE, payload={"image_revision": "a" * 40}
            )
            await socket.send_str(hello.model_dump_json())
            await socket.receive(timeout=5)  # the handshake's reply
            asked = asyncio.create_task(
                client.post("/mcp-check", json={"server": "papers"}, headers=allowed)
            )
            request = Request.model_validate_json((await socket.receive(timeout=5)).data)
            assert request.capability is Capability.LIST_MCP_TOOLS
            assert request.payload == {"server": "papers"}
            listed = ToolsListed(status="listed", tool_count=3, digest="d" * 64)
            reply = Reply(id=request.id, payload=listed.model_dump(mode="json"))
            await socket.send_str(reply.model_dump_json())
            answer = await asked
            assert answer.status == 200
            assert (await answer.json())["tool_count"] == 3
    await backend.close()
    # A check is not a canary: nothing was stored.
    control = ControlStore(store_path(tmp_path / "state"), readonly=True)
    try:
        assert CanaryStore(control).latest(["papers"]) == {}
    finally:
        control.close()
