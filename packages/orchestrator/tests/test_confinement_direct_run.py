# SPDX-License-Identifier: MIT
"""A confined run's connector calls take the deployment's MCP path (D98): on the direct path
the host's toolbox admits each call and the runtime's client makes it, through a fake
OpenShell with the real policy middleware, and the result's image comes back to the host
over ``publish_mcp_binary`` as an artifact of the run; through the proxy the host's client
makes it and the runtime is never asked."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast

import pytest
from pydantic import JsonValue

from sandbox_science.orchestrator.confinement.canaries import DirectServerSettings, DirectSettings
from sandbox_science.orchestrator.confinement.connector_calls import (
    RuntimeConnectorCalls,
    RuntimeMcpClient,
)
from sandbox_science.orchestrator.confinement.host import HostLoops
from sandbox_science.orchestrator.confinement.listings import WorkerListings
from sandbox_science.orchestrator.confinement.models import CallbackModel, RuntimeModels
from sandbox_science.orchestrator.confinement.runtime_loop import RuntimeLoops
from sandbox_science.orchestrator.confinement.transport import Capability, Channel, TransportError
from sandbox_science.orchestrator.harness import McpAccess, Role
from sandbox_science.orchestrator.loop import Transcript
from sandbox_science.orchestrator.model import ChatModel, ModelGateway
from sandbox_science.orchestrator.model_profiles import ModelProfile
from sandbox_science.orchestrator.tools import Toolbox

from .loop_harness import API_KEY, PROJECT, SESSION, Harness, Reply, call
from .loop_harness import gateway as gateway
from .loop_harness import harness as harness
from .loop_harness import worker as worker
from .test_confinement_connector_calls import Reports
from .test_mcp_direct import PLACEHOLDER, PNG, READ_TOOL, SERVER, fake_openshell
from .test_mcp_direct import events as events
from .test_mcp_direct import worker_check as worker_check
from .test_run_service import records, serving

pytestmark = pytest.mark.slow


class ProxyStandIn:
    """The host's client on the proxy path: it answers a listing and a call itself."""

    classifies = False

    def __init__(self) -> None:
        self.asked: list[str] = []

    async def request(
        self, server: str, method: str, params: Mapping[str, object], **_: object
    ) -> dict[str, Any]:
        self.asked.append(method)
        if method == "tools/list":
            return {"tools": [{"name": READ_TOOL, "inputSchema": {"type": "object"}}]}
        return {"content": [{"type": "text", "text": "through the proxy"}]}


async def confined_run(
    harness: Harness, tmp_path: Path, access: McpAccess | None, direct: DirectSettings | None
) -> tuple[Any, list[Capability]]:
    """One confined run that calls ``READ_TOOL``, with ``access`` as its connectors; the
    runtime makes MCP requests with ``direct``'s client, when given. Its record, and the
    capabilities the host asked of the runtime."""
    asked: list[Capability] = []
    async with serving(
        harness, Reply(calls=[call(READ_TOOL, query="SNOVA")]), Reply("Done."), serve=False
    ) as service:
        models = ModelGateway(harness.gateway.url, API_KEY)

        async def host_handle(kind: Capability, value: JsonValue) -> JsonValue:
            return await host.capability(kind, value)

        async def vm_handle(kind: Capability, value: JsonValue) -> JsonValue:
            asked.append(kind)
            if kind is Capability.RUN:
                return await runtime.run(value)
            if kind is Capability.CANCEL:
                return await runtime.cancel(value)
            if kind is Capability.MODEL:
                return await vm_models.complete(value)
            if kind is Capability.MODEL_INFO:
                return await vm_models.info(value)
            if kind is Capability.MCP_REQUEST and calls is not None:
                return await calls.request(value)
            raise TransportError("denied", "fixture refused capability")

        host_channel = Channel(lambda text: vm_channel.receive(text), host_handle)
        vm_channel = Channel(lambda text: host_channel.receive(text), vm_handle)
        vm_models = RuntimeModels(models, frozenset({"main"}), vm_channel)
        host = HostLoops(
            host_channel,
            frozenset({PROJECT}),
            listings=cast(WorkerListings, Reports()),
            mcp_servers=(SERVER,),
        )
        calls = RuntimeConnectorCalls(direct, vm_channel) if direct is not None else None
        runtime = RuntimeLoops(
            vm_channel,
            models,
            tmp_path / "scratch",
            released=calls.release if calls is not None else None,
        )
        runtime.scratch.mkdir()
        service.harness.main = Role(
            CallbackModel(host_channel, "main", role="main", profile=ModelProfile(alias="main")),
            "main",
        )
        if access is not None and access.path == "direct":
            access = McpAccess(
                RuntimeMcpClient(host), access.servers, access.project, path="direct"
            )
        service.harness.mcp = access

        async def runner(
            model: ChatModel, toolbox: Toolbox, task: str | None, **options: Any
        ) -> Transcript:
            return await host.run(model, toolbox, task, **options)

        service.harness.loop_runner = runner
        service.harness.prepare_attempt = lambda admitted: host.model_scope()
        try:
            service.runs.post(PROJECT, SESSION, "msg-mcp", "Find SNOVA", author="user")
            assert await service.start_selected(PROJECT, SESSION)
            task = service.active[PROJECT, SESSION].task
            assert task is not None
            await task
            (record,) = records(harness)
            return record, asked
        finally:
            await runtime.close()
            if calls is not None:
                await calls.close()
            await host_channel.close()
            await vm_channel.close()
            await models.close()


async def test_on_the_direct_path_the_runtime_makes_the_call_and_the_host_keeps_its_image(
    harness: Harness,
    tmp_path: Path,
    events: Any,
    worker_check: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CF_ACCESS_CLIENT_ID", PLACEHOLDER)
    async with fake_openshell(tmp_path / "openshell", events, worker_check) as openshell:
        openshell.image = True
        direct = DirectSettings(
            project="crypto",
            servers={
                SERVER: DirectServerSettings(
                    url=openshell.url,
                    credentials={"CF-Access-Client-Id": "CF_ACCESS_CLIENT_ID"},
                    tools=openshell.tools,
                )
            },
        )
        stand_in = cast(Any, ProxyStandIn())
        access = McpAccess(stand_in, (SERVER,), PROJECT, path="direct")
        record, asked = await confined_run(harness, tmp_path, access, direct)
    # The call left from the runtime, through OpenShell's rules and the middleware.
    assert openshell.reached == [READ_TOOL]
    assert stand_in.asked == []
    assert asked.count(Capability.MCP_REQUEST) == 2  # the run's listing, then the call
    assert record.failure is None
    (tool,) = record.tools
    assert tool["tool"] == READ_TOOL
    assert tool["connector"] == SERVER
    assert tool["binary_results"] == 1
    (kept,) = tool["mcp_binaries"]
    assert kept["source"] == "runtime"
    assert kept["media_type"] == "image/png"
    assert kept["artifact"]
    # The model saw the host's note, never the base64.
    text = str(record.tools)
    assert PNG not in text
    assert openshell.records("mcp-call")


async def test_through_the_proxy_the_host_makes_the_call_and_never_asks_the_runtime(
    harness: Harness, tmp_path: Path
) -> None:
    stand_in = ProxyStandIn()
    access = McpAccess(stand_in, (SERVER,), PROJECT)
    record, asked = await confined_run(harness, tmp_path, access, None)
    assert stand_in.asked == ["tools/list", "tools/call"]
    assert Capability.MCP_REQUEST not in asked
    (tool,) = record.tools
    assert tool["connector"] == SERVER
    assert "mcp_binaries" not in tool
