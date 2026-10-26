# SPDX-License-Identifier: MIT
"""Binary results of the runtime's direct-path connector calls, kept by the host over the
attempt's ``publish_mcp_binary`` (D98): the call must be the connector call whose request
the host is relaying to the runtime, the bytes must match their digest and bounds, and a
retry is answered, not kept again."""

from __future__ import annotations

import base64
import hashlib
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast

import pytest
from pydantic import JsonValue

from sandbox_science.contracts import ArtifactRef, ErrorKind, ExecutionContext, PublishRequest
from sandbox_science.contracts.artifact_inventory import ArtifactReceipt, ArtifactRevisionRef
from sandbox_science.contracts.worker import WriteFileRequest
from sandbox_science.orchestrator.artifact_publisher import ArtifactPublisher
from sandbox_science.orchestrator.confinement.binaries import CallBinaries, ControlBinaries
from sandbox_science.orchestrator.confinement.host import HostLoops
from sandbox_science.orchestrator.confinement.transport import Capability, Channel, TransportError
from sandbox_science.orchestrator.confinement.wire import (
    MAX_BINARY_BYTES,
    MAX_BINARY_ITEMS,
    BinaryResult,
    payload,
)
from sandbox_science.orchestrator.connectors import Connectors
from sandbox_science.orchestrator.mcp_binaries import ArtifactBinaries
from sandbox_science.orchestrator.model import ToolCall
from sandbox_science.orchestrator.worker_client import WorkerAPIError, WorkerClient
from sandbox_science.policy.mcp_answers import Withheld

ATTEMPT = "cap-" + "a" * 48
CONTEXT = ExecutionContext(project_id="p1", session_id="s1", environment_id="env1")
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 24
CALL = ToolCall("call-1", "render", '{"x": 1}')


@dataclass
class FakeWorker:
    writes: list[WriteFileRequest] = field(default_factory=list[WriteFileRequest])
    fail: bool = False

    async def write_file(self, request: WriteFileRequest) -> object:
        if self.fail:
            raise WorkerAPIError(ErrorKind.UNAVAILABLE, "down")
        self.writes.append(request)
        return None


@dataclass
class FakePublisher:
    """The run's publisher: each new operation a new revision of the run's artifact."""

    run_id: str = "main-1"
    published: list[tuple[PublishRequest, str, str]] = field(
        default_factory=list[tuple[PublishRequest, str, str]]
    )

    async def publish(self, request: PublishRequest, operation: str, *, tool_call: str) -> Any:
        self.published.append((request, operation, tool_call))
        revision = ArtifactRevisionRef(
            artifact_id="art-control",
            project_id="p1",
            revision=len(self.published),
            sha256="0" * 64,
            size=8,
            media_type="image/png",
        )
        worker = ArtifactRef(
            artifact_id="art-worker", project_id="p1", sha256="0" * 64, size=8,
            media_type="image/png", source_path=request.path,
            created_at=datetime(2026, 10, 26, tzinfo=UTC),
        )  # fmt: skip
        receipt = ArtifactReceipt(state="recorded", operation_id=operation, revision=revision)
        return SimpleNamespace(worker=worker, receipt=receipt)


@dataclass
class Host:
    binaries: CallBinaries
    connectors: Connectors
    worker: FakeWorker
    publisher: FakePublisher
    pending: ToolCall | None
    vm: Channel


@pytest.fixture
async def linked() -> AsyncIterator[Host]:
    worker, publisher = FakeWorker(), FakePublisher()
    sink = ArtifactBinaries(cast(WorkerClient, worker), CONTEXT, cast(ArtifactPublisher, publisher))
    connectors = SimpleNamespace(
        tools={"render": SimpleNamespace(server="charts")}, binaries=sink, path="direct"
    )
    host: Host | None = None

    async def host_handle(kind: Capability, value: JsonValue) -> JsonValue:
        assert host is not None
        assert kind is Capability.PUBLISH_MCP_BINARY
        return await host.binaries.publish(value, host.pending, host.connectors)

    async def vm_handle(kind: Capability, value: JsonValue) -> JsonValue:
        raise TransportError("denied", "fixture refused capability")

    host_channel = Channel(lambda text: vm_channel.receive(text), host_handle)
    vm_channel = Channel(lambda text: host_channel.receive(text), vm_handle)
    host = Host(CallBinaries(), cast(Connectors, connectors), worker, publisher, CALL, vm_channel)
    try:
        yield host
    finally:
        await vm_channel.close()
        await host_channel.close()


def item(data: bytes, mime: str | None = "image/png") -> Withheld:
    return Withheld(0, "image", mime, base64.b64encode(data).decode())


def asked(data: bytes, *, call: str = "call-1", mime: str = "image/png", sha: str = "") -> Any:
    return payload(
        BinaryResult(
            attempt=ATTEMPT,
            call=call,
            mime=mime,
            sha256=sha or hashlib.sha256(data).hexdigest(),
            data_b64=base64.b64encode(data).decode(),
        )
    )


async def send(host: Host, value: Any) -> Any:
    return await host.vm.request(Capability.PUBLISH_MCP_BINARY, value)


async def test_an_item_becomes_a_revision_of_the_runs_artifact_as_on_the_proxy_path(
    linked: Host,
) -> None:
    note = await ControlBinaries(linked.vm, ATTEMPT, "call-1").keep("charts", "render", item(PNG))
    digest = hashlib.sha256(PNG).hexdigest()
    (write,) = linked.worker.writes
    assert write.path == f"mcp-results/charts/{digest[:24]}.png"
    assert base64.b64decode(write.content_b64) == PNG
    (request, operation, tool_call), = linked.publisher.published  # fmt: skip
    # Published as the runtime's: its client made the call, not the host's proxy client.
    assert (request.path, request.media_type, tool_call) == (
        write.path,
        "image/png",
        "runtime-mcp:render",
    )
    # The operation is the proxy path's: one per run and content.
    assert operation == "mcp-" + hashlib.sha256(f"main-1:{digest}".encode()).hexdigest()[:40]
    assert "artifact art-control revision 1" in note
    assert digest in note
    assert base64.b64encode(PNG).decode() not in note
    # Recorded with the call, once.
    assert linked.binaries.recorded("call-1") == [
        {"sha256": digest, "size": len(PNG), "media_type": "image/png",
         "artifact": "art-control", "revision": 1, "source": "runtime"}
    ]  # fmt: skip
    assert linked.binaries.recorded("call-1") == []


async def test_a_retry_by_call_id_is_answered_without_keeping_it_again(linked: Host) -> None:
    first = await send(linked, asked(PNG))
    linked.pending = None  # even once the call is no longer pending
    again = await send(linked, asked(PNG))
    assert again == first
    assert len(linked.worker.writes) == len(linked.publisher.published) == 1
    # Another item of the same call is a new one; another call ID is not a retry.
    linked.pending = CALL
    await send(linked, asked(PNG + b"\x01"))
    assert len(linked.publisher.published) == 2
    with pytest.raises(TransportError) as error:
        await send(linked, asked(PNG, call="call-2"))
    assert error.value.kind == "denied"


async def test_an_item_the_worker_could_not_take_is_tried_again(linked: Host) -> None:
    linked.worker.fail = True
    failed = await send(linked, asked(PNG))
    assert failed["artifact"] is None
    assert "could not be kept" in failed["note"]
    linked.worker.fail = False
    kept = await send(linked, asked(PNG))
    assert kept["artifact"] == "art-control"
    assert len(linked.binaries.recorded("call-1")) == 1


async def test_a_digest_that_does_not_match_is_refused(linked: Host) -> None:
    with pytest.raises(TransportError) as error:
        await send(linked, asked(PNG, sha="0" * 64))
    assert (error.value.kind, str(error.value)) == (
        "conflict",
        "binary result does not match its SHA-256",
    )
    assert linked.worker.writes == []


async def test_an_item_over_its_bound_is_refused(linked: Host) -> None:
    with pytest.raises(TransportError) as error:
        await send(linked, asked(b"\x00" * (MAX_BINARY_BYTES + 1)))
    assert (error.value.kind, str(error.value)) == (
        "invalid_request",
        "binary result exceeds its byte bound",
    )
    assert linked.worker.writes == []


async def test_a_calls_items_are_bounded_in_bytes_and_count(linked: Host) -> None:
    half = MAX_BINARY_BYTES // 2 + 1
    await send(linked, asked(b"\x01" * half))
    with pytest.raises(TransportError) as error:
        await send(linked, asked(b"\x02" * half))
    assert str(error.value) == "the call's binary results exceed their bound"
    linked.pending = ToolCall("call-3", "render", "{}")
    for number in range(MAX_BINARY_ITEMS):
        await send(linked, asked(number.to_bytes(4), call="call-3"))
    with pytest.raises(TransportError) as error:
        await send(linked, asked(b"one more", call="call-3"))
    assert error.value.kind == "invalid_request"
    assert len(linked.binaries.recorded("call-3")) == MAX_BINARY_ITEMS


@pytest.mark.parametrize(
    ("pending", "call"),
    [
        (None, "call-1"),  # no call being relayed: none, or one the runtime did not make
        (CALL, "call-9"),  # a call ID the run did not make
        (ToolCall("call-1", "shell", "{}"), "call-1"),  # not a connector tool's call
    ],
)
async def test_a_call_the_run_did_not_make_is_refused(
    linked: Host, pending: ToolCall | None, call: str
) -> None:
    linked.pending = pending
    with pytest.raises(TransportError) as error:
        await send(linked, asked(PNG, call=call))
    assert error.value.kind == "denied"
    assert linked.worker.writes == []


@pytest.mark.parametrize("mime", ["text/plain", "text/html", "video/mp4", "multipart/mixed"])
async def test_a_media_type_that_is_not_binary_is_refused(linked: Host, mime: str) -> None:
    with pytest.raises(TransportError) as error:
        await send(linked, asked(PNG, mime=mime))
    assert (error.value.kind, str(error.value)) == ("denied", "media type is not binary")
    # The runtime's sink does not send it: it is withheld and said.
    note = await ControlBinaries(linked.vm, ATTEMPT, "call-1").keep(
        "charts", "render", item(PNG, mime)
    )
    assert note.startswith(f"[{mime} withheld:")
    assert linked.worker.writes == []


async def test_the_runtimes_sink_withholds_what_the_host_refuses(linked: Host) -> None:
    sink = ControlBinaries(linked.vm, ATTEMPT, "call-9")
    note = await sink.keep("charts", "render", item(PNG))
    assert note.startswith("[image/png withheld:")
    assert "did not keep it" in note
    # No media type is sent as application/octet-stream, and kept.
    note = await ControlBinaries(linked.vm, ATTEMPT, "call-1").keep(
        "charts", "render", item(PNG, None)
    )
    assert "application/octet-stream from charts" in note
    assert linked.worker.writes[0].path.endswith(".bin")


async def test_a_host_attempt_that_does_not_exist_is_refused() -> None:
    async def vm_handle(kind: Capability, value: JsonValue) -> JsonValue:
        raise TransportError("denied", "fixture refused capability")

    async def host_handle(kind: Capability, value: JsonValue) -> JsonValue:
        return await loops.capability(kind, value)

    host_channel = Channel(lambda text: vm_channel.receive(text), host_handle)
    vm_channel = Channel(lambda text: host_channel.receive(text), vm_handle)
    loops = HostLoops(host_channel, frozenset({"p1"}))
    try:
        with pytest.raises(TransportError) as error:
            await vm_channel.request(Capability.PUBLISH_MCP_BINARY, asked(PNG))
        assert error.value.kind == "denied"
    finally:
        await vm_channel.close()
        await host_channel.close()


async def test_nothing_is_published_while_the_runs_calls_take_the_proxy_path(
    linked: Host,
) -> None:
    linked.connectors.path = "proxy"
    with pytest.raises(TransportError) as error:
        await send(linked, asked(PNG))
    assert (error.value.kind, str(error.value)) == (
        "denied",
        "this run's connector calls are not on the direct path",
    )
    assert linked.worker.writes == []
