# SPDX-License-Identifier: MIT
"""Catalog delivery proves exact storage targets, never environment publication."""

from __future__ import annotations

import asyncio
import sqlite3
import threading
from types import SimpleNamespace
from typing import Any, cast

import pytest
from aiohttp.test_utils import TestServer

from sandbox_science.contracts import ErrorKind
from sandbox_science.contracts.documents import (
    CatalogSnapshot,
    CatalogStatus,
    generation_id,
    manifest_sha256,
)
from sandbox_science.orchestrator.documents import USER, DocumentStore
from sandbox_science.orchestrator.documents.delivery import (
    CatalogDelivery,
    DeliveryBinding,
    DeliveryError,
    DeliveryLimits,
    endpoint_fingerprint,
)
from sandbox_science.orchestrator.documents.publication import Publications
from sandbox_science.orchestrator.worker_client import WorkerAPIError, WorkerClient
from sandbox_science.worker.app import create_app
from sandbox_science.worker.artifacts import Blobs
from sandbox_science.worker.backends import LocalBackend
from sandbox_science.worker.documents_catalog import DocumentsCatalog
from sandbox_science.worker.environments import Environments
from sandbox_science.worker.errors import WorkerError
from sandbox_science.worker.manager import ExecutionManager
from sandbox_science.worker.settings import ClientSettings, LocalBackendSettings, token_digest
from sandbox_science.worker.store import Store

pytestmark = pytest.mark.asyncio


class Client:
    """Real catalog operations with transport boundary faults and deterministic gates."""

    base_url = "http://synthetic-worker.invalid"

    def __init__(self, store: DocumentsCatalog) -> None:
        self.store = store
        self.calls: list[tuple[str, Any]] = []
        self.lose: str | None = None
        self.status: CatalogStatus | None = None
        self.pause: str | None = None
        self.entered = asyncio.Event()
        self.resume = asyncio.Event()

    async def call(self, name: str, argument: Any) -> CatalogStatus:
        self.calls.append((name, argument))
        try:
            result = (
                self.status
                if name == "status" and self.status is not None
                else getattr(self.store, name)(argument)
            )
        except WorkerError as error:
            raise WorkerAPIError(error.kind, error.message) from error
        if self.pause == name:
            self.entered.set()
            await self.resume.wait()
        if self.lose == name:
            self.lose = None
            raise WorkerAPIError(ErrorKind.UNAVAILABLE, "synthetic lost reply")
        return result

    async def catalog_status(self, project_id):
        return await self.call("status", project_id)

    async def catalog_begin(self, target):
        return await self.call("begin", target)

    async def catalog_page(self, page):
        return await self.call("page", page)

    async def catalog_delta(self, delta):
        return await self.call("delta", delta)

    async def catalog_content(self, chunk):
        return await self.call("content", chunk)

    async def catalog_commit(self, target):
        return await self.call("commit", target)


def binding(
    client: Client | WorkerClient, revision: int = 1, project: str = "p"
) -> DeliveryBinding:
    return DeliveryBinding(
        binding_id="binding",
        project_id=project,
        configuration_revision=revision,
        endpoint_sha256=endpoint_fingerprint(client.base_url),
    )


def make(source: DocumentStore, name: str = "count", text: str = "# Count\n\nCount 15.\n") -> str:
    return source.create("p", name=name, title=name, text=text, author=USER).document_id


async def drive(delivery, bound, client, maximum=50):
    for _ in range(maximum):
        before = len(client.calls) if isinstance(client, Client) else 0
        result = await delivery.step(bound, client)
        if isinstance(client, Client):
            assert len(client.calls) - before <= 2
        assert result.publication == "disabled"
        if result.state == "acknowledged":
            return result
        assert result.state in {"progress", "retry"}, result
    pytest.fail("delivery did not finish within bounded steps")


async def test_actual_authenticated_worker_http_composition_has_no_execution_or_file_effects(
    tmp_path,
):
    source = DocumentStore(tmp_path / "control.sqlite3")
    make(source)
    records = Store(tmp_path / "worker.sqlite3")
    catalog = DocumentsCatalog(tmp_path / "catalog.sqlite3")
    manager = ExecutionManager(
        records,
        LocalBackend(LocalBackendSettings(), tmp_path),
        Blobs(tmp_path / "artifacts"),
        pool_id="test",
        max_artifact_size=1024,
        state_dir=tmp_path,
    )
    token = "test-control-delivery-token-" + "c" * 32
    clients = [
        ClientSettings(
            name="control", token_sha256=token_digest(token), projects=("p",), document_catalog=True
        )
    ]
    app = create_app(
        manager,
        "test-pool-delivery-token-" + "p" * 32,
        cast(Environments, SimpleNamespace(deleted=[])),
        clients,
        document_catalog=catalog,
    )
    delivery = CatalogDelivery(tmp_path / "delivery.sqlite3", Publications(source))
    try:
        async with (
            TestServer(app) as server,
            WorkerClient(str(server.make_url("")), token) as client,
        ):
            result = await drive(delivery, binding(client), client)
            assert result.acknowledged == Publications(source).plan("p").snapshot
            assert catalog.status("p").catalog_seq == 1
            assert not manager._environments
            assert not manager._live
            assert (
                catalog._db.execute("SELECT count(*) FROM document_written_revisions").fetchone()[0]
                == 0
            )
            assert not list(tmp_path.rglob("documents"))
    finally:
        await delivery.aclose()
        await manager.close()
        catalog.close()
        records.close()
        source.close()


@pytest.mark.parametrize("fault", ["begin", "page", "content", "commit"])
async def test_lost_replies_and_restart_preserve_the_exact_pin(tmp_path, fault):
    source = DocumentStore(tmp_path / "control.sqlite3")
    make(source)
    catalog = DocumentsCatalog(tmp_path / "catalog.sqlite3")
    client = Client(catalog)
    bound = binding(client)
    path = tmp_path / "delivery.sqlite3"
    delivery = CatalogDelivery(path, Publications(source))
    client.lose = fault
    try:
        for _ in range(10):
            result = await delivery.step(bound, client)
            if result.state == "retry":
                break
        else:
            pytest.fail("fault boundary not reached")
        target = result.target
        await delivery.aclose()
        delivery = CatalogDelivery(path, Publications(source))
        finished = await drive(delivery, bound, client)
        assert finished.target == target == Publications(source).plan("p").snapshot
        assert catalog.status("p").catalog_seq == 1
    finally:
        await delivery.aclose()
        catalog.close()
        source.close()


async def test_source_advancing_during_multichunk_upload_does_not_change_old_target(tmp_path):
    source = DocumentStore(tmp_path / "control.sqlite3")
    doc = make(source, text="x" * 600_000)
    exporter = Publications(source)
    original = exporter.plan("p")
    catalog = DocumentsCatalog(tmp_path / "catalog.sqlite3")
    client = Client(catalog)
    delivery = CatalogDelivery(tmp_path / "delivery.sqlite3", exporter)
    try:
        assert (await delivery.step(binding(client), client)).state == "progress"
        source.save_text("p", doc, base=1, text="Updated.\n", author=USER, reason="new source")
        first = await drive(delivery, binding(client), client)
        assert first.target == original.snapshot
        old = exporter.content("p", original.entries[0])
        assert catalog._db.execute("SELECT content FROM catalog_blobs").fetchone()[0] == old
        latest = await drive(delivery, binding(client), client)
        assert latest.target == exporter.plan("p").snapshot
        assert [name for name, _ in client.calls].count("delta") == 1
        assert catalog.status("p").catalog_seq == 2
    finally:
        await delivery.aclose()
        catalog.close()
        source.close()


async def test_stage_expiry_replays_pin_and_empty_snapshot_deletes_only_at_commit(tmp_path):
    now = [1000.0]
    source = DocumentStore(tmp_path / "control.sqlite3")
    doc = make(source)
    catalog = DocumentsCatalog(tmp_path / "catalog.sqlite3", clock=lambda: now[0])
    client = Client(catalog)
    delivery = CatalogDelivery(tmp_path / "delivery.sqlite3", Publications(source))
    try:
        await delivery.step(binding(client), client)
        await delivery.step(binding(client), client)
        now[0] += 1000
        assert (await delivery.step(binding(client), client)).state == "progress"
        assert [name for name, _ in client.calls].count("begin") == 2
        await drive(delivery, binding(client), client)
        source.delete("p", doc, author=USER, reason="remove")
        await delivery.step(binding(client), client)
        assert catalog.status("p").entry_count == 1
        result = await drive(delivery, binding(client), client)
        assert result.target.entry_count == catalog.status("p").entry_count == 0
    finally:
        await delivery.aclose()
        catalog.close()
        source.close()


async def test_reset_does_not_inherit_cached_ack_and_ambiguous_delta_reset_is_held(tmp_path):
    source = DocumentStore(tmp_path / "control.sqlite3")
    doc = make(source)
    catalog = DocumentsCatalog(tmp_path / "catalog.sqlite3")
    client = Client(catalog)
    delivery = CatalogDelivery(tmp_path / "delivery.sqlite3", Publications(source))
    other = DocumentsCatalog(tmp_path / "reset.sqlite3")
    try:
        original = await drive(delivery, binding(client), client)
        client.store = other
        reset = await delivery.step(binding(client), client)
        assert reset.state == "progress"
        assert reset.observed_current is None
        assert reset.acknowledged == original.target  # historical evidence only
        await drive(delivery, binding(client), client)
        source.save_text("p", doc, base=1, text="Updated.\n", author=USER, reason="new")
        await delivery.step(binding(client), client)
        assert client.calls[-1][0] == "delta"
        client.store = catalog
        catalog._db.execute("DELETE FROM catalog_projects")
        before = len(client.calls)
        held = await delivery.step(binding(client), client)
        assert held.state == "held"
        assert "reset" in held.message
        assert len(client.calls) == before + 1
        with sqlite3.connect(delivery._journal.path) as db:
            assert db.execute("SELECT mode FROM pins").fetchone()[0] == "delta"
    finally:
        await delivery.aclose()
        other.close()
        catalog.close()
        source.close()


@pytest.mark.parametrize(
    "fault", ["fork", "ahead", "partial", "foreign-project", "foreign-stage", "receipt"]
)
async def test_untrusted_status_cannot_advance_ack(tmp_path, fault):
    source = DocumentStore(tmp_path / "control.sqlite3")
    make(source)
    target = Publications(source).plan("p").snapshot
    catalog = DocumentsCatalog(tmp_path / "catalog.sqlite3")
    client = Client(catalog)
    if fault in {"fork", "ahead"}:
        seq = target.catalog_seq + (fault == "ahead")
        digest = manifest_sha256("p", seq, [])
        fake = CatalogSnapshot(
            project_id="p",
            generation_id=generation_id(seq, digest),
            catalog_seq=seq,
            entry_count=0,
            total_content_bytes=0,
            manifest_sha256=digest,
        )
        client.status = CatalogStatus(
            **fake.model_dump(exclude={"protocol"}), publication="disabled"
        )
    elif fault == "partial":
        client.status = CatalogStatus(project_id="p", entry_count=1)
    elif fault == "foreign-project":
        client.status = CatalogStatus(project_id="other")
    elif fault == "receipt":
        client.status = CatalogStatus(project_id="p", acknowledged=target)
    else:
        client.status = catalog.begin(target)
    delivery = CatalogDelivery(tmp_path / "delivery.sqlite3", Publications(source))
    try:
        result = await delivery.step(binding(client), client)
        assert result.state == "held"
        assert result.acknowledged is None
        assert len(client.calls) == 1
        with sqlite3.connect(delivery._journal.path) as db:
            assert db.execute("SELECT acknowledged FROM bindings").fetchone()[0] is None
    finally:
        await delivery.aclose()
        catalog.close()
        source.close()


async def test_endpoint_and_configuration_fences_old_http_reply(tmp_path):
    source = DocumentStore(tmp_path / "control.sqlite3")
    make(source)
    first = DocumentsCatalog(tmp_path / "first.sqlite3")
    second = DocumentsCatalog(tmp_path / "second.sqlite3")
    old = Client(first)
    new = Client(second)
    new.base_url = "http://second-worker.invalid"
    delivery = CatalogDelivery(tmp_path / "delivery.sqlite3", Publications(source))
    old.pause = "begin"
    task = asyncio.create_task(delivery.step(binding(old), old))
    try:
        await asyncio.wait_for(old.entered.wait(), 2)
        assert (await delivery.step(binding(old), old)).state == "busy"
        with pytest.raises(DeliveryError, match="endpoint"):
            await delivery.step(binding(old), new)
        wrong_revision = await delivery.step(binding(new), new)
        assert wrong_revision.state == "held"
        assert "revision" in wrong_revision.message
        fresh = await delivery.step(binding(new, 2), new)
        assert fresh.state == "progress"
        old.resume.set()
        displaced = await task
        assert displaced.state == "held"
        assert "lease" in displaced.message
        assert (await drive(delivery, binding(new, 2), new)).state == "acknowledged"
    finally:
        old.resume.set()
        await task
        await delivery.aclose()
        first.close()
        second.close()
        source.close()


async def test_cancelled_caller_keeps_commit_owned_and_close_drains(tmp_path, monkeypatch):
    source = DocumentStore(tmp_path / "control.sqlite3")
    make(source)
    catalog = DocumentsCatalog(tmp_path / "catalog.sqlite3")
    client = Client(catalog)
    delivery = CatalogDelivery(
        tmp_path / "delivery.sqlite3", Publications(source), limits=DeliveryLimits(max_pending=1)
    )
    entered = threading.Event()
    release = threading.Event()
    original = delivery._journal.pin

    def blocked(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return original(*args, **kwargs)

    monkeypatch.setattr(delivery._journal, "pin", blocked)
    caller = asyncio.create_task(delivery.step(binding(client), client))
    closing = None
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        caller.cancel()
        caller.cancel()
        with pytest.raises(asyncio.CancelledError):
            await caller
        assert (await delivery.step(binding(client), client)).state == "busy"
        assert await asyncio.wait_for(asyncio.to_thread(lambda: "default executor free"), 1)
        closing = asyncio.create_task(delivery.aclose())
        await asyncio.sleep(0)
        closing.cancel()
        closing.cancel()
        await asyncio.sleep(0)
        assert not closing.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await closing
        with sqlite3.connect(delivery._journal.path) as db:
            assert db.execute("SELECT count(*) FROM pins").fetchone()[0] == 1
        assert not delivery._steps
        assert not delivery._sql_work
    finally:
        release.set()
        if closing is not None:
            await asyncio.gather(closing, return_exceptions=True)
        await delivery.aclose()
        catalog.close()
        source.close()


@pytest.mark.parametrize("corrupt", ["entries", "target", "delta", "progress", "input"])
async def test_corrupt_pin_refuses_before_network_upload(tmp_path, corrupt):
    source = DocumentStore(tmp_path / "control.sqlite3")
    make(source)
    catalog = DocumentsCatalog(tmp_path / "catalog.sqlite3")
    client = Client(catalog)
    path = tmp_path / "delivery.sqlite3"
    delivery = CatalogDelivery(path, Publications(source))
    await delivery.step(binding(client), client)
    await delivery.aclose()
    with sqlite3.connect(path) as db:
        if corrupt in {"entries", "target", "delta"}:
            statements = {
                "entries": "UPDATE pins SET entries=?",
                "target": "UPDATE pins SET target=?",
                "delta": "UPDATE pins SET delta=?",
            }
            db.execute(statements[corrupt], ("x" * (2 * 1024 * 1024),))
        elif corrupt == "input":
            db.execute("UPDATE pins SET input_sha256=?", ("a" * 64,))
        else:
            db.execute("UPDATE pins SET next_entry=1000000")
    delivery = CatalogDelivery(path, Publications(source))
    try:
        before = len(client.calls)
        assert (await delivery.step(binding(client), client)).state == "held"
        assert len(client.calls) == before
    finally:
        await delivery.aclose()
        catalog.close()
        source.close()


async def test_global_quota_and_tightened_reopen_preserve_unresolved_pin(tmp_path):
    source = DocumentStore(tmp_path / "control.sqlite3")
    make(source)
    catalog = DocumentsCatalog(tmp_path / "catalog.sqlite3")
    client = Client(catalog)
    path = tmp_path / "delivery.sqlite3"
    delivery = CatalogDelivery(
        path, Publications(source), limits=DeliveryLimits(max_pinned_entries=0)
    )
    assert (await delivery.step(binding(client), client)).state == "held"
    assert [name for name, _ in client.calls] == ["status"]
    await delivery.aclose()
    delivery = CatalogDelivery(path, Publications(source))
    await delivery.step(binding(client), client)
    await delivery.aclose()
    delivery = CatalogDelivery(
        path, Publications(source), limits=DeliveryLimits(max_pinned_entries=0)
    )
    try:
        before = len(client.calls)
        assert (await delivery.step(binding(client), client)).state == "held"
        assert len(client.calls) == before
        with sqlite3.connect(path) as db:
            assert db.execute("SELECT count(*) FROM pins").fetchone()[0] == 1
    finally:
        await delivery.aclose()
        catalog.close()
        source.close()


@pytest.mark.parametrize(
    "endpoint",
    [
        "file:///tmp/worker",
        "http://u:p@worker",
        "http://worker/?token=x",
        "http://worker/#x",
        "http://worker:bad",
        "http://worker/\n",
    ],
)
async def test_approved_endpoint_refuses_credential_or_ambiguous_values(endpoint):
    with pytest.raises(DeliveryError):
        endpoint_fingerprint(endpoint)


@pytest.mark.parametrize("revision", [True, 1.0, 0, -1])
async def test_configuration_revision_is_exact_positive_integer(revision):
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        DeliveryBinding(
            binding_id="b",
            project_id="p",
            configuration_revision=revision,
            endpoint_sha256="a" * 64,
        )
