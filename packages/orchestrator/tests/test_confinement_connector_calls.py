# SPDX-License-Identifier: MIT
"""Connector calls on the direct path (D98): the host's ``Connectors`` admit each call and
relay its request to the runtime over ``mcp_request``; the runtime's client makes it
through a fake OpenShell with the real policy middleware and worker check, confirms held
calls over ``confirm_request``, and reports listings over ``report_mcp_listing``. The host
takes a held call's failure only for a request it confirmed for the attempt."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any, cast

import pytest
import pytest_asyncio
from pydantic import JsonValue

from sandbox_science.orchestrator import connectors as connectors_module
from sandbox_science.orchestrator.confinement.canaries import DirectServerSettings, DirectSettings
from sandbox_science.orchestrator.confinement.connector_calls import (
    RuntimeConnectorCalls,
    RuntimeMcpClient,
)
from sandbox_science.orchestrator.confinement.host import HostLoops
from sandbox_science.orchestrator.confinement.listings import WorkerListings
from sandbox_science.orchestrator.confinement.transport import Capability, Channel, TransportError
from sandbox_science.orchestrator.confinement.wire import (
    ListingReportReply,
    ListingReportRequest,
    McpAnswer,
    McpFailure,
    payload,
)
from sandbox_science.orchestrator.connectors import ConnectorError, Connectors
from sandbox_science.orchestrator.continuity.records import CURRENT, Ownership
from sandbox_science.orchestrator.effects import UnknownEffectError

from .test_mcp_direct import (
    DENIED_TOOL,
    ESCALATED_TOOL,
    PLACEHOLDER,
    READ_TOOL,
    SERVER,
    Gateway,
    fake_openshell,
)
from .test_mcp_direct import events as events
from .test_mcp_direct import worker_check as worker_check

RUN = "20261026T120000000000Z-main"
OWNER = Ownership(project="crypto", session="s-1", work=RUN, run=RUN, epoch=1)


@dataclass
class Reports:
    """The worker's side of listing reports, as the host forwards them."""

    taken: list[tuple[str, str, str | None, str | None]] = field(
        default_factory=list[tuple[str, str, str | None, str | None]]
    )

    async def report(
        self, value: ListingReportRequest, project: str, session: str | None, run: str | None
    ) -> ListingReportReply:
        self.taken.append((value.server, project, session, run))
        return ListingReportReply(drifted=False)


@dataclass
class Linked:
    host: HostLoops
    gateway: Gateway
    reports: Reports
    runtime: RuntimeConnectorCalls
    asked: list[dict[str, Any]]
    vm: Callable[[Capability, JsonValue], Awaitable[JsonValue]] | None = None
    """Replaces the runtime's handling of ``mcp_request`` (a runtime that lies)."""


@pytest_asyncio.fixture
async def linked(
    tmp_path: Any, events: Any, worker_check: Any, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[Linked]:
    monkeypatch.setenv("CF_ACCESS_CLIENT_ID", PLACEHOLDER)
    async with fake_openshell(tmp_path, events, worker_check) as gateway:
        reports = Reports()
        found: Linked | None = None

        async def host_handle(kind: Capability, value: JsonValue) -> JsonValue:
            assert found is not None
            return await found.host.capability(kind, value)

        async def vm_handle(kind: Capability, value: JsonValue) -> JsonValue:
            assert found is not None
            assert kind is Capability.MCP_REQUEST
            found.asked.append(cast(dict[str, Any], value))
            if found.vm is not None:
                return await found.vm(kind, value)
            return await found.runtime.request(value)

        host_channel = Channel(lambda text: vm_channel.receive(text), host_handle)
        vm_channel = Channel(lambda text: host_channel.receive(text), vm_handle)
        host = HostLoops(
            host_channel,
            frozenset({"crypto"}),
            requests=gateway.check,
            listings=cast(WorkerListings, reports),
            mcp_servers=(SERVER,),
        )
        settings = DirectSettings(
            project="crypto",
            servers={
                SERVER: DirectServerSettings(
                    url=gateway.url,
                    credentials={"CF-Access-Client-Id": "CF_ACCESS_CLIENT_ID"},
                    tools=gateway.tools,
                )
            },
        )
        runtime = RuntimeConnectorCalls(settings, vm_channel)
        found = Linked(host, gateway, reports, runtime, [])
        token = CURRENT.set(OWNER)
        try:
            async with host.model_scope():
                yield found
        finally:
            CURRENT.reset(token)
            await runtime.close()
            await vm_channel.close()
            await host_channel.close()


def attempt(linked: Linked) -> Any:
    """The attempt's state, as preparation leaves it while the run's toolbox calls."""
    identity = linked.host.model_capability()
    prepared = SimpleNamespace(owner=OWNER, intent=None, tool=None, relaying=None, confirmed={})
    linked.host.attempts[identity] = cast(Any, prepared)
    return prepared


def connectors(linked: Linked, heard: list[Any] | None = None) -> Connectors:
    async def on_approval(wait: dict[str, object] | None) -> None:
        assert heard is not None
        heard.append(wait)

    return Connectors(
        RuntimeMcpClient(linked.host),
        [SERVER],
        session="s-1",
        run=RUN,
        on_approval=on_approval if heard is not None else None,
        path="direct",
    )


async def test_the_runs_listing_is_made_and_filtered_by_the_runtime_and_reported(
    linked: Linked,
) -> None:
    # Prepared, not yet running: the attempt's owner names the project, session and run.
    found = connectors(linked)
    await found.load()
    assert READ_TOOL in found.tools
    assert ESCALATED_TOOL in found.tools
    assert DENIED_TOOL not in found.tools  # the compiled native rules deny it
    assert found.path == "direct"
    assert linked.reports.taken == [(SERVER, "crypto", "s-1", RUN)]
    (asked,) = linked.asked
    assert (asked["method"], asked["call"], asked["session"]) == ("tools/list", None, "s-1")


async def test_a_held_call_waits_on_the_hosts_confirmation_and_passes_once_approved(
    linked: Linked, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(connectors_module, "APPROVAL_POLL", (0.05, 0.05))
    heard: list[Any] = []
    found = connectors(linked, heard)
    await found.load()
    prepared = attempt(linked)
    call = asyncio.create_task(found.call(ESCALATED_TOOL, {"url": "https://example.org"}))
    while not heard:
        await asyncio.sleep(0.01)
    request = cast(int, heard[0]["permission_request"])
    # The runtime's client asked the host, which asked the worker over the middleware's
    # records, and remembered the request for the attempt.
    assert set(prepared.confirmed) == {request}
    assert prepared.confirmed[request].tool == ESCALATED_TOOL
    assert linked.gateway.reached == []
    linked.gateway.approve(request)
    text, record = await asyncio.wait_for(call, 10)
    assert text == f"{ESCALATED_TOOL} ran"
    assert record["permission_request"] == request
    assert heard[-1] is None
    assert linked.gateway.reached == [ESCALATED_TOOL]
    (escalation,) = linked.gateway.records("escalation")
    assert (escalation["project"], escalation["session"], escalation["run"]) == (
        "crypto",
        "s-1",
        RUN,
    )


async def test_a_hold_the_host_did_not_confirm_is_the_servers_error(linked: Linked) -> None:
    found = connectors(linked, [])
    await found.load()
    attempt(linked)

    async def lying(kind: Capability, value: JsonValue) -> JsonValue:
        # A runtime that skips confirm_request and claims a hold on request 5.
        held = McpFailure(kind="approval_required", message="waits", request=5)
        return payload(McpAnswer(failure=held))

    linked.vm = lying
    text, record = await found.call(READ_TOOL, {"query": "SNOVA"})
    assert record["error"] == "server_error"
    assert record["unconfirmed_request"] == 5
    assert "permission_request" not in record
    assert "server_error" in text


async def test_a_hold_confirmed_for_one_call_is_refused_when_another_call_cites_it(
    linked: Linked, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review of a2/direct-wiring: a request confirmed for tool X, then cited by the answer
    to a relayed call of tool Y, is that answer's failure: no wait and no card."""
    monkeypatch.setattr(connectors_module, "APPROVAL_POLL", (0.05, 0.05))
    heard: list[Any] = []
    found = connectors(linked, heard)
    await found.load()
    prepared = attempt(linked)
    waiting = asyncio.create_task(found.call(ESCALATED_TOOL, {"url": "https://example.org"}))
    while not heard:
        await asyncio.sleep(0.01)
    request = cast(int, heard[0]["permission_request"])
    waiting.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await waiting
    assert set(prepared.confirmed) == {request}

    async def citing(kind: Capability, value: JsonValue) -> JsonValue:
        # The runtime answers the relayed call of READ_TOOL with X's confirmed request.
        held = McpFailure(kind="approval_required", message="waits", request=request)
        return payload(McpAnswer(failure=held))

    linked.vm = citing
    heard.clear()
    text, record = await asyncio.wait_for(found.call(READ_TOOL, {"query": "SNOVA"}), 5)
    assert record["error"] == "server_error"
    assert record["unconfirmed_request"] == request
    assert "permission_request" not in record
    assert heard == []
    assert "server_error" in text
    # The same request cited for the same tool with other arguments is refused too.
    text, record = await asyncio.wait_for(
        found.call(ESCALATED_TOOL, {"url": "https://example.net"}), 5
    )
    assert record["error"] == "server_error"
    assert heard == []


@pytest.mark.parametrize(("method", "kind"), [("tools/call", None), ("tools/list", "unavailable")])
async def test_a_lost_answer_is_an_unknown_effect_only_for_a_call(
    linked: Linked, method: str, kind: str | None
) -> None:
    found = connectors(linked)
    await found.load()
    attempt(linked)

    async def lost(kind: Capability, value: JsonValue) -> JsonValue:
        raise TransportError("unavailable", "control request outcome is unknown")

    linked.vm = lost
    if method == "tools/call":
        with pytest.raises(UnknownEffectError):
            await found.call(READ_TOOL, {"query": "SNOVA"})
        return
    with pytest.raises(ConnectorError) as error:
        await RuntimeMcpClient(linked.host).request(SERVER, method, {}, session="s-1")
    assert error.value.kind == kind


async def test_a_call_names_the_models_call_only_while_the_host_relays_it(
    linked: Linked,
) -> None:
    found = connectors(linked)
    await found.load()
    prepared = attempt(linked)
    seen: list[Any] = []

    async def observed(kind: Capability, value: JsonValue) -> JsonValue:
        seen.append(prepared.relaying)
        return await linked.runtime.request(value)

    linked.vm = observed
    prepared.intent = SimpleNamespace(call=SimpleNamespace(id="call-7", name=READ_TOOL))
    await found.call(READ_TOOL, {"query": "SNOVA"})
    assert linked.asked[-1]["call"] == "call-7"
    assert [getattr(item, "id", None) for item in seen] == ["call-7"]
    assert prepared.relaying is None


async def test_a_server_the_runtime_does_not_declare_is_unavailable() -> None:
    from sandbox_science.orchestrator.confinement.wire import McpRequest

    asked = McpRequest(
        attempt="cap-" + "a" * 48, server=SERVER, method="tools/list", params={}, session="s"
    )
    runtime = RuntimeConnectorCalls(
        DirectSettings(project="crypto", servers={"other": DirectServerSettings(url="https://x")}),
        cast(Channel, None),
    )
    answer = await runtime.request(payload(asked))
    assert isinstance(answer, dict)
    assert cast(dict[str, Any], answer["failure"])["kind"] == "unavailable"


def test_control_takes_the_proxy_or_the_direct_path_never_both(tmp_path: Any) -> None:
    from pydantic import ValidationError

    from sandbox_science.orchestrator.confinement.server import (
        ControlSettings,
        DirectAccessSettings,
        ProxySettings,
    )

    base: dict[str, Any] = {
        "state_dir": tmp_path,
        "worker": "http://127.0.0.1:1",
        "worker_token_file": tmp_path / "w",
        "runtime_token_file": tmp_path / "r",
        "generation": "runtime-test",
        "projects": ("crypto",),
        "main": "main",
        "image_revision": "a" * 40,
    }
    direct = DirectAccessSettings(project="crypto", servers=(SERVER,))
    proxy = ProxySettings(
        url="http://127.0.0.1:2", token_file=tmp_path / "t", project="crypto", servers=(SERVER,)
    )
    assert ControlSettings(**base, direct=direct).direct == direct
    with pytest.raises(ValidationError, match="not both"):
        ControlSettings(**base, direct=direct, proxy=proxy)
