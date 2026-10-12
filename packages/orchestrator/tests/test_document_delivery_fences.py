# SPDX-License-Identifier: MIT
"""Lease, acknowledgement and bounded accumulation failures retain original pins."""

from __future__ import annotations

import asyncio
import sqlite3

import pytest

from sandbox_science.contracts.documents import CatalogStatus
from sandbox_science.orchestrator.documents import USER, DocumentStore
from sandbox_science.orchestrator.documents.delivery import (
    CatalogDelivery,
    DeliveryError,
    DeliveryLimits,
)
from sandbox_science.orchestrator.documents.publication import Publications
from sandbox_science.worker.documents_catalog import DocumentsCatalog

from .test_document_delivery import Client, binding, drive, make

pytestmark = pytest.mark.asyncio


async def test_lost_ack_transaction_reconciles_committed_target_after_restart(tmp_path):
    source = DocumentStore(tmp_path / "control.sqlite3")
    make(source)
    catalog = DocumentsCatalog(tmp_path / "catalog.sqlite3")
    client = Client(catalog)
    path = tmp_path / "delivery.sqlite3"
    delivery = CatalogDelivery(path, Publications(source))
    try:
        for _ in range(3):
            assert (await delivery.step(binding(client), client)).state == "progress"
        with sqlite3.connect(path) as db:
            db.execute(
                "CREATE TRIGGER refuse_ack BEFORE DELETE ON pins BEGIN "
                "SELECT RAISE(ABORT, 'synthetic ack failure'); END"
            )
        failed = await delivery.step(binding(client), client)
        assert failed.state == "held"
        assert catalog.status("p").catalog_seq == 1
        with sqlite3.connect(path) as db:
            assert db.execute("SELECT acknowledged FROM bindings").fetchone()[0] is None
            assert db.execute("SELECT count(*) FROM pins").fetchone()[0] == 1
            db.execute("DROP TRIGGER refuse_ack")
        await delivery.aclose()
        delivery = CatalogDelivery(path, Publications(source))
        before = len(client.calls)
        fixed = await delivery.step(binding(client), client)
        assert fixed.state == "acknowledged"
        assert len(client.calls) == before + 1
        assert client.calls[-1][0] == "status"
    finally:
        await delivery.aclose()
        catalog.close()
        source.close()


async def test_multiple_bindings_share_global_pin_budget(tmp_path):
    source = DocumentStore(tmp_path / "control.sqlite3")
    make(source)
    catalog = DocumentsCatalog(tmp_path / "catalog.sqlite3")
    client = Client(catalog)
    delivery = CatalogDelivery(
        tmp_path / "delivery.sqlite3",
        Publications(source),
        limits=DeliveryLimits(max_pinned_entries=1),
    )
    try:
        assert (await delivery.step(binding(client), client)).state == "progress"
        other = Client(DocumentsCatalog(tmp_path / "other.sqlite3"))
        try:
            second_binding = binding(other).model_copy(update={"binding_id": "other"})
            second = await delivery.step(second_binding, other)
            assert second.state == "held"
            assert [name for name, _ in other.calls] == ["status"]
            with sqlite3.connect(delivery._journal.path) as db:
                assert db.execute("SELECT count(*) FROM pins").fetchone()[0] == 1
            assert (await drive(delivery, binding(client), client)).state == "acknowledged"
            assert (await drive(delivery, second_binding, other)).state == "acknowledged"
        finally:
            other.store.close()
    finally:
        await delivery.aclose()
        catalog.close()
        source.close()


async def test_expired_lease_never_records_old_http_reply(tmp_path):
    now = [1000.0]
    source = DocumentStore(tmp_path / "control.sqlite3")
    make(source)
    catalog = DocumentsCatalog(tmp_path / "catalog.sqlite3")
    old = Client(catalog)
    new = Client(catalog)
    delivery = CatalogDelivery(
        tmp_path / "delivery.sqlite3",
        Publications(source),
        clock=lambda: now[0],
        limits=DeliveryLimits(lease_seconds=1),
    )
    old.pause = "begin"
    task = asyncio.create_task(delivery.step(binding(old), old))
    try:
        await asyncio.wait_for(old.entered.wait(), 2)
        now[0] += 2
        second = await delivery.step(binding(new), new)
        assert second.state == "progress"
        old.resume.set()
        stale = await task
        assert stale.state == "held"
        assert "lease" in stale.message
        assert (await drive(delivery, binding(new), new)).state == "acknowledged"
    finally:
        old.resume.set()
        await task
        await delivery.aclose()
        catalog.close()
        source.close()


async def test_mutating_injected_client_endpoint_cannot_advance_pin(tmp_path):
    source = DocumentStore(tmp_path / "control.sqlite3")
    make(source)
    catalog = DocumentsCatalog(tmp_path / "catalog.sqlite3")
    client = Client(catalog)
    delivery = CatalogDelivery(tmp_path / "delivery.sqlite3", Publications(source))
    bound = binding(client)
    client.pause = "status"
    task = asyncio.create_task(delivery.step(bound, client))
    try:
        await asyncio.wait_for(client.entered.wait(), 2)
        client.base_url = "http://changed-worker.invalid"
        client.resume.set()
        result = await task
        assert result.state == "held"
        assert len(client.calls) == 1
        with sqlite3.connect(delivery._journal.path) as db:
            assert db.execute("SELECT count(*) FROM pins").fetchone()[0] == 0
    finally:
        client.resume.set()
        await task
        await delivery.aclose()
        catalog.close()
        source.close()


async def test_old_receipt_with_newer_current_never_acknowledges_old_pin(tmp_path):
    source = DocumentStore(tmp_path / "control.sqlite3")
    doc = make(source)
    catalog = DocumentsCatalog(tmp_path / "catalog.sqlite3")
    client = Client(catalog)
    delivery = CatalogDelivery(tmp_path / "delivery.sqlite3", Publications(source))
    try:
        result = await delivery.step(binding(client), client)
        old_target = result.target
        source.save_text("p", doc, base=1, text="New content.\n", author=USER, reason="advance")
        latest = Publications(source).plan("p").snapshot
        client.status = CatalogStatus(
            **latest.model_dump(exclude={"protocol"}), acknowledged=old_target, replayed=True
        )
        held = await delivery.step(binding(client), client)
        assert held.state == "held"
        assert held.acknowledged is None
        assert held.observed_current == latest
        with sqlite3.connect(delivery._journal.path) as db:
            assert db.execute("SELECT count(*) FROM pins").fetchone()[0] == 1
    finally:
        await delivery.aclose()
        catalog.close()
        source.close()


async def test_source_rollback_or_same_cursor_fork_cannot_reuse_historical_ack(tmp_path):
    source = DocumentStore(tmp_path / "control.sqlite3")
    make(source)
    catalog = DocumentsCatalog(tmp_path / "catalog.sqlite3")
    client = Client(catalog)
    path = tmp_path / "delivery.sqlite3"
    delivery = CatalogDelivery(path, Publications(source))
    await drive(delivery, binding(client), client)
    await delivery.aclose()
    other = DocumentStore(tmp_path / "fork.sqlite3")
    make(other, text="Different canonical bytes.\n")
    delivery = CatalogDelivery(path, Publications(other))
    try:
        result = await delivery.step(binding(client), client)
        assert result.state == "held"
        assert "forked" in result.message
    finally:
        await delivery.aclose()
        other.close()
        catalog.close()
        source.close()


async def test_canonical_database_cannot_be_used_as_delivery_journal(tmp_path):
    source = DocumentStore(tmp_path / "control.sqlite3")
    make(source)
    try:
        with pytest.raises(DeliveryError, match="separate"):
            CatalogDelivery(source.path, Publications(source))
        with sqlite3.connect(source.path) as db:
            assert db.execute("SELECT name FROM sqlite_master WHERE name='pins'").fetchone() is None
    finally:
        source.close()


async def test_large_complete_metadata_paginates_and_deduplicates_content(tmp_path):
    source = DocumentStore(tmp_path / "control.sqlite3")
    for number in range(70):
        make(source, name=f"doc{number}")
    catalog = DocumentsCatalog(tmp_path / "catalog.sqlite3")
    client = Client(catalog)
    delivery = CatalogDelivery(tmp_path / "delivery.sqlite3", Publications(source))
    try:
        result = await drive(delivery, binding(client), client)
        assert result.target.entry_count == 70
        pages = [page for name, page in client.calls if name == "page"]
        assert [page.start_index for page in pages] == [0, 64]
        assert [len(page.entries) for page in pages] == [64, 6]
        assert [name for name, _ in client.calls].count("content") == 1
    finally:
        await delivery.aclose()
        catalog.close()
        source.close()


@pytest.mark.parametrize(
    "corruption", ["deadline", "revision", "generation", "metadata-phase", "begin-phase"]
)
async def test_corrupt_lease_and_phase_refuse_before_status_or_upload(tmp_path, corruption):
    source = DocumentStore(tmp_path / "control.sqlite3")
    make(source, text="x" * 300_000)
    catalog = DocumentsCatalog(tmp_path / "catalog.sqlite3")
    client = Client(catalog)
    path = tmp_path / "delivery.sqlite3"
    delivery = CatalogDelivery(path, Publications(source))
    for _ in range(3):
        await delivery.step(binding(client), client)
    await delivery.aclose()
    statements = {
        "deadline": "UPDATE bindings SET owner='corrupt-owner',lease_until=1e999",
        "revision": "UPDATE bindings SET revision=-1",
        "generation": "UPDATE bindings SET lease_generation=-1",
        "metadata-phase": "UPDATE pins SET phase='metadata'",
        "begin-phase": "UPDATE pins SET phase='begin'",
    }
    with sqlite3.connect(path) as db:
        db.execute(statements[corruption])
    delivery = CatalogDelivery(path, Publications(source))
    try:
        before = len(client.calls)
        assert (await delivery.step(binding(client), client)).state == "held"
        assert len(client.calls) == before
    finally:
        await delivery.aclose()
        catalog.close()
        source.close()
