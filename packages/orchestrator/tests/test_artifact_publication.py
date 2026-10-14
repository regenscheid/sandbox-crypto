# SPDX-License-Identifier: MIT
"""Actual HTTP transfer and cancellation-owned publication crash windows."""

from __future__ import annotations

import argparse
import asyncio
import base64
import contextlib
import hashlib
import json
import sys
import threading
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import aiohttp
import pytest
import pytest_asyncio
from aiohttp import web
from aiohttp.test_utils import TestServer

from sandbox_science.contracts import (
    ArtifactRef,
    ErrorKind,
    ExecutionContext,
    PublishRequest,
    WriteFileRequest,
)
from sandbox_science.orchestrator import cli
from sandbox_science.orchestrator.artifact_publisher import ArtifactPublisher
from sandbox_science.orchestrator.artifacts import ArtifactInventory, ArtifactLimits
from sandbox_science.orchestrator.documents import DocumentStore
from sandbox_science.orchestrator.model import ToolCall
from sandbox_science.orchestrator.projects import ProjectStore
from sandbox_science.orchestrator.tools import Toolbox
from sandbox_science.orchestrator.worker_client import WorkerAPIError, WorkerClient
from sandbox_science.worker.app import create_app
from sandbox_science.worker.artifacts import Blobs
from sandbox_science.worker.backends import LocalBackend
from sandbox_science.worker.manager import ExecutionManager
from sandbox_science.worker.settings import LocalBackendSettings
from sandbox_science.worker.store import Store

pytestmark = pytest.mark.asyncio
TOKEN = "test-secret-artifact"
CONTEXT = ExecutionContext(project_id="p1", session_id="s1", environment_id="env1")


def snapshot(data: bytes, *, path: str = "out.bin", project: str = "p1") -> ArtifactRef:
    return ArtifactRef(
        artifact_id="worker-artifact",
        project_id=project,
        sha256=hashlib.sha256(data).hexdigest(),
        size=len(data),
        media_type="application/octet-stream",
        source_path=path,
        created_at=datetime.now(UTC),
    )


@pytest_asyncio.fixture
async def worker_url(tmp_path: Path) -> AsyncIterator[str]:
    root = tmp_path / "worker"
    root.mkdir()
    store = Store(root / "worker.sqlite3")
    manager = ExecutionManager(
        store,
        LocalBackend(LocalBackendSettings(python=sys.executable), root),
        Blobs(root / "artifacts"),
        pool_id="test-pool",
        max_artifact_size=1 << 20,
        state_dir=root,
    )
    await manager.start()
    try:
        async with TestServer(create_app(manager, TOKEN)) as server:
            yield str(server.make_url("/")).rstrip("/")
    finally:
        await manager.close()
        store.close()


@pytest.mark.parametrize("media", [None, "application/pdf; charset=utf8"])
async def test_real_worker_http_publication_and_trusted_gateway_model(
    worker_url: str,
    tmp_path: Path,
    media: str | None,
) -> None:
    documents = DocumentStore(tmp_path / "control.sqlite3")
    inventory = ArtifactInventory(documents)
    data = b"%PDF-untrusted bytes only\x00\xff"
    async with WorkerClient(worker_url, TOKEN) as client:
        await client.write_file(
            WriteFileRequest(
                context=CONTEXT, path="out.bin", content_b64=base64.b64encode(data).decode()
            )
        )
        publisher = ArtifactPublisher(inventory, client, CONTEXT, run_id="run1", alias="research")
        toolbox = Toolbox(client, CONTEXT, artifacts=publisher)
        call = ToolCall(
            id="call1",
            name="publish",
            arguments=json.dumps(
                {
                    "path": "out.bin",
                    "media_type": media,
                    "actor": "USER",
                    "model": "forged",
                    "context": {"project_id": "p2"},
                }
            ),
        )
        outcome = await toolbox.call(call, "op1", model="gateway-model")
        first = outcome.record["artifact_inventory"]
        assert first["state"] == "recorded"
        assert first["revision"]["mode"] == "pinned"
        await publisher.aclose()
    revision = inventory.list("p1").entries[0]
    assert inventory.content("p1", revision.reference) == data
    assert revision.publication.context == CONTEXT
    assert revision.publication.kind == "agent_publication"
    assert revision.publication.alias == "research"
    assert revision.publication.model == "gateway-model"
    assert revision.reference.media_type == (media or "application/octet-stream")
    assert revision.publication.producer == "unknown"
    assert revision.worker_last_writer_hint is None
    documents.close()


class SnapshotClient(WorkerClient):
    def __init__(self) -> None:
        super().__init__("http://trusted-worker.invalid", "test-secret-artifact")
        self.data = b"original"
        self.refs = {}
        self.publishes = 0
        self.transfers = 0
        self.unknown = False
        self.fail_transfer = False

    async def publish_snapshot(self, request):
        self.publishes += 1
        ref = snapshot(self.data, path=request.path, project=request.context.project_id)
        self.refs[ref.sha256] = self.data
        if self.unknown:
            raise WorkerAPIError(ErrorKind.UNAVAILABLE, "reply lost after remote snapshot")
        return ref

    async def verified_artifact_content(self, reference, *, max_bytes=8 * 1024 * 1024):
        self.transfers += 1
        if self.fail_transfer:
            raise WorkerAPIError(ErrorKind.UNAVAILABLE, "transfer interrupted")
        if reference.size > max_bytes:
            raise WorkerAPIError(ErrorKind.INVALID_REQUEST, "too large")
        return self.refs[reference.sha256]


async def test_known_snapshot_retry_pins_bytes_after_path_changes_and_restart(
    tmp_path: Path,
) -> None:
    documents = DocumentStore(tmp_path / "control.sqlite3")
    client = SnapshotClient()
    client.fail_transfer = True
    publisher = ArtifactPublisher(ArtifactInventory(documents), client, CONTEXT, run_id="run1")
    request = PublishRequest(context=CONTEXT, path="out.bin")
    held = await publisher.publish(request, "op1", tool_call="call1")
    assert held.worker
    assert held.receipt.state == "held"
    await publisher.aclose()
    documents.close()
    client.data, client.fail_transfer = b"new mutable path bytes", False
    documents = DocumentStore(documents.path)
    inventory = ArtifactInventory(documents)
    publisher = ArtifactPublisher(inventory, client, CONTEXT, run_id="run1")
    complete = await publisher.publish(request, "op1", tool_call="call1")
    assert complete.receipt.revision
    assert complete.worker == held.worker
    assert inventory.content("p1", complete.receipt.revision) == b"original"
    replay = await publisher.publish(request, "op1", tool_call="call1")
    assert replay == complete
    assert client.publishes == 1
    assert client.transfers == 2
    await publisher.aclose()
    documents.close()


async def test_unknown_worker_reply_never_republishes_path(tmp_path: Path) -> None:
    documents = DocumentStore(tmp_path / "control.sqlite3")
    client = SnapshotClient()
    client.unknown = True
    publisher = ArtifactPublisher(ArtifactInventory(documents), client, CONTEXT, run_id="run1")
    request = PublishRequest(context=CONTEXT, path="out.bin")
    first = await publisher.publish(request, "op1", tool_call="call1")
    assert first.receipt.state == "held"
    assert first.worker is None
    await publisher.aclose()
    client.data = b"changed"
    publisher = ArtifactPublisher(ArtifactInventory(documents), client, CONTEXT, run_id="run1")
    held = await publisher.publish(request, "op1", tool_call="call1")
    assert "will not republish" in held.receipt.message
    assert client.publishes == 1
    assert client.transfers == 0
    await publisher.aclose()
    documents.close()


async def test_endpoint_changed_after_remote_acceptance_retains_old_pin_without_transfer(
    tmp_path: Path,
) -> None:
    accepted, release = asyncio.Event(), asyncio.Event()

    class Paused(SnapshotClient):
        async def publish_snapshot(self, request):
            result = await super().publish_snapshot(request)
            accepted.set()
            await release.wait()
            return result

    documents = DocumentStore(tmp_path / "control.sqlite3")
    inventory = ArtifactInventory(documents)
    client = Paused()
    publisher = ArtifactPublisher(inventory, client, CONTEXT, run_id="run1")
    request = PublishRequest(context=CONTEXT, path="out.bin")
    task = asyncio.create_task(publisher.publish(request, "op1", tool_call="call1"))
    await accepted.wait()
    client.base_url = "http://other-worker.invalid"
    release.set()
    held = await task
    assert held.worker
    assert held.receipt.state == "held"
    assert "endpoint changed" in held.receipt.message
    assert client.transfers == 0
    assert not inventory.list("p1").entries
    await publisher.aclose()
    publisher = ArtifactPublisher(inventory, client, CONTEXT, run_id="run1")
    conflict = await publisher.publish(request, "op1", tool_call="call1")
    assert conflict.receipt.state == "held"
    assert "another request or publisher binding" in conflict.receipt.message
    assert client.publishes == 1
    await publisher.aclose()
    documents.close()


async def test_pending_admission_is_bounded_and_cancelled_waiters_retain_slots(
    tmp_path: Path,
) -> None:
    release = asyncio.Event()

    class Paused(SnapshotClient):
        async def publish_snapshot(self, request):
            await release.wait()
            return await super().publish_snapshot(request)

    documents = DocumentStore(tmp_path / "control.sqlite3")
    inventory = ArtifactInventory(documents)
    client = Paused()
    publisher = ArtifactPublisher(inventory, client, CONTEXT, run_id="run1")
    request = PublishRequest(context=CONTEXT, path="out.bin")
    tasks = [
        asyncio.create_task(publisher.publish(request, f"op{i}", tool_call=f"call{i}"))
        for i in range(8)
    ]
    try:
        await asyncio.sleep(0)
        refused = await publisher.publish(request, "ninth", tool_call="call9")
        assert "admission limit" in refused.receipt.message
        tasks[0].cancel()
        with pytest.raises(asyncio.CancelledError):
            await tasks[0]
        refused = await publisher.publish(request, "tenth", tool_call="call10")
        assert "admission limit" in refused.receipt.message
    finally:
        release.set()
        await publisher.aclose()
        await asyncio.gather(*tasks, return_exceptions=True)
    assert len(inventory.events("p1").entries) == 8
    assert client.publishes == 8
    documents.close()


async def test_control_quota_preserves_actual_successful_worker_reference(tmp_path: Path) -> None:
    documents = DocumentStore(tmp_path / "control.sqlite3")
    inventory = ArtifactInventory(documents, limits=ArtifactLimits(artifact_bytes=1))
    client = SnapshotClient()
    publisher = ArtifactPublisher(inventory, client, CONTEXT, run_id="run1")
    outcome = await publisher.publish(
        PublishRequest(context=CONTEXT, path="out.bin"), "op1", tool_call="call1"
    )
    assert outcome.worker
    assert outcome.worker.sha256 == hashlib.sha256(b"original").hexdigest()
    assert outcome.receipt.state == "held"
    assert not inventory.list("p1").entries
    replay = await publisher.publish(
        PublishRequest(context=CONTEXT, path="out.bin"), "op1", tool_call="call1"
    )
    assert replay.worker == outcome.worker
    assert client.publishes == 1
    await publisher.aclose()
    documents.close()


async def test_cli_closes_borrowed_store_and_models_after_publisher_drain_cancellation(
    worker_url: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    closed: list[str] = []
    borrowed: list[DocumentStore] = []

    class Model:
        def __init__(self, url, alias, key, **kwargs):
            self.alias = alias

        async def close(self):
            closed.append(self.alias)

    class CancelledClosePublisher(ArtifactPublisher):
        def __init__(self, inventory, *args, **kwargs):
            borrowed.append(inventory.documents)
            super().__init__(inventory, *args, **kwargs)

        async def aclose(self):
            await super().aclose()
            closed.append("publisher-drained")
            raise asyncio.CancelledError

    original_close = DocumentStore.close

    def close_store(store):
        if store in borrowed:
            assert "publisher-drained" in closed
            closed.append("store")
        original_close(store)

    async def context(*args):
        return CONTEXT.model_copy(update={"environment_id": recorded.environment_id})

    async def interrupted(*args, **kwargs):
        raise asyncio.CancelledError

    monkeypatch.setattr(cli, "OpenAICompatibleModel", Model)
    monkeypatch.setattr(cli, "ArtifactPublisher", CancelledClosePublisher)
    monkeypatch.setattr(cli, "_context", context)
    monkeypatch.setattr(cli, "_read_secret", lambda _: TOKEN)
    monkeypatch.setattr(cli, "run_task", interrupted)
    monkeypatch.setattr(DocumentStore, "close", close_store)
    args = argparse.Namespace(
        script=None,
        model_url="http://unused.invalid",
        model="main",
        api_key_file=None,
        run_id="run1",
        worker=worker_url,
        token_file=None,
        allow_unisolated=True,
        state_dir=tmp_path / "control",
        watcher_model="watcher",
        watcher_script=None,
        reviewer_model="reviewer",
        reviewer_script=None,
        math_node=None,
        math_launcher=None,
        math_bundle=None,
        watcher_calls_per_hour=1,
        watcher_first_check=1,
        watcher_interval=1,
        task="test",
        max_turns=1,
    )
    projects = ProjectStore(args.state_dir)
    projects.create("p1", "minimal", "sha256:" + "a" * 64)
    recorded = projects.open_session("p1", "s1", dedicated=False)
    with pytest.raises(asyncio.CancelledError):
        await cli._run(args)
    assert closed == ["publisher-drained", "reviewer", "watcher", "store", "main"]


async def test_sql_ownership_survives_repeated_caller_and_close_cancellation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    documents = DocumentStore(tmp_path / "control.sqlite3")
    inventory = ArtifactInventory(documents)
    entered, release = threading.Event(), threading.Event()
    original = inventory.begin

    def blocked(*args):
        entered.set()
        assert release.wait(5)
        return original(*args)

    monkeypatch.setattr(inventory, "begin", blocked)
    client = SnapshotClient()
    publisher = ArtifactPublisher(inventory, client, CONTEXT, run_id="run1")
    caller = asyncio.create_task(
        publisher.publish(PublishRequest(context=CONTEXT, path="out.bin"), "op1", tool_call="call1")
    )
    try:
        while not entered.is_set():
            await asyncio.sleep(0.001)
        caller.cancel()
        caller.cancel()
        with pytest.raises(asyncio.CancelledError):
            await caller
        assert await asyncio.wait_for(asyncio.to_thread(lambda: 42), 0.2) == 42
        closing = asyncio.create_task(publisher.aclose())
        await asyncio.sleep(0)
        closing.cancel()
        await asyncio.sleep(0)
        closing.cancel()
        await asyncio.sleep(0.01)
        assert not closing.done()
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await closing
    assert inventory.events("p1").entries[0].kind == "artifact.revision_created"
    documents.close()


@pytest.mark.parametrize(
    "case", ["hash", "short", "large", "header", "encoded", "redirect", "error", "bool_metadata"]
)
async def test_hostile_http_transfer_boundaries(case: str) -> None:
    calls = []
    data = b"exact bytes"
    ref = snapshot(data)

    async def content(request: web.Request):
        calls.append(request)
        assert request.headers["Authorization"] == "Bearer test-secret-artifact"
        assert request.query["project_id"] == "p1"
        headers = {"X-Content-SHA256": ref.sha256}
        if case == "redirect":
            raise web.HTTPFound("/destination")
        if case == "error":
            return web.Response(status=500, body=b"x" * 32768)
        if case == "header":
            headers["X-Content-SHA256"] = "b" * 64
        if case == "encoded":
            headers["Content-Encoding"] = "gzip"
        body = {"hash": b"wrong bytes", "short": data[:-1], "large": data + b"x"}.get(case, data)
        return web.Response(body=body, headers=headers)

    async def publish(request: web.Request):
        body = ref.model_dump(mode="json")
        body["size"] = True
        return web.json_response(body, status=201)

    async def destination(request: web.Request):
        raise AssertionError("redirect must not be followed")

    app = web.Application()
    app.router.add_get("/v1/artifacts/{id}/content", content)
    app.router.add_get("/destination", destination)
    app.router.add_post("/v1/artifacts", publish)
    async with (
        TestServer(app) as server,
        WorkerClient(str(server.make_url("/")).rstrip("/"), "test-secret-artifact") as client,
    ):
        if case == "bool_metadata":
            with pytest.raises(WorkerAPIError):
                await client.publish_snapshot(PublishRequest(context=CONTEXT, path="out.bin"))
        else:
            with pytest.raises(WorkerAPIError):
                await client.verified_artifact_content(ref)
    assert len(calls) == (0 if case == "bool_metadata" else 1)


async def test_transfer_total_deadline_and_size_refusal_before_network(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = []
    data = b"ab"
    ref = snapshot(data)
    actual_timeout = aiohttp.ClientTimeout

    def timeout(**kwargs):
        if kwargs.get("total") == 10:
            calls.append(kwargs)
            return actual_timeout(total=0.05)
        return actual_timeout(**kwargs)

    async def content(request: web.Request):
        response = web.StreamResponse(headers={"X-Content-SHA256": ref.sha256})
        await response.prepare(request)
        await response.write(b"a")
        await asyncio.sleep(0.15)
        with contextlib.suppress(ConnectionResetError):
            await response.write(b"b")
        return response

    app = web.Application()
    app.router.add_get("/v1/artifacts/{id}/content", content)
    async with (
        TestServer(app) as server,
        WorkerClient(str(server.make_url("/")).rstrip("/"), "test-secret-artifact") as client,
    ):
        with pytest.raises(WorkerAPIError, match="limit"):
            await client.verified_artifact_content(ref, max_bytes=1)
        monkeypatch.setattr(aiohttp, "ClientTimeout", timeout)
        with pytest.raises(WorkerAPIError, match="transport"):
            await client.verified_artifact_content(ref)
    assert calls == [{"total": 10}]
