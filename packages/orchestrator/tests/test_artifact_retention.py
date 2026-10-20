# SPDX-License-Identifier: MIT
"""Durable receipts, exact ACK retries and failure isolation over real HTTP."""

from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
import tracemalloc
from collections.abc import AsyncIterator
from contextlib import closing
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import pytest
from aiohttp import web

from sandbox_science.contracts import ArtifactRef, ExecutionContext, PublishRequest
from sandbox_science.contracts.artifact_inventory import ArtifactPublicationSource, ArtifactReceipt
from sandbox_science.contracts.artifact_retention import ArtifactRetentionAcknowledgement
from sandbox_science.contracts.worker import PROTOCOL_HEADER, PROTOCOL_VERSION
from sandbox_science.orchestrator import artifacts as artifact_module
from sandbox_science.orchestrator import cli_artifacts
from sandbox_science.orchestrator.artifact_publisher import ArtifactPublisher
from sandbox_science.orchestrator.artifact_retention import (
    RetentionOutbox,
    acknowledgement,
    drain,
    enqueue,
)
from sandbox_science.orchestrator.artifacts import ArtifactInventory
from sandbox_science.orchestrator.documents import DocumentError, DocumentStore
from sandbox_science.orchestrator.worker_client import WorkerClient
from sandbox_science.testing import FakeClock

CONTEXT = ExecutionContext(project_id="p1", session_id="s1", environment_id="env1")
DATA = b"retained research evidence"


@dataclass
class Service:
    url: str = ""
    acknowledgements: list[ArtifactRetentionAcknowledgement] = field(default_factory=list)
    captures: int = 0
    reads: int = 0
    failures: int = 0
    mode: str = "ok"
    entered: asyncio.Event = field(default_factory=asyncio.Event)
    release: asyncio.Event = field(default_factory=asyncio.Event)
    snapshot: ArtifactRef = field(
        default_factory=lambda: ArtifactRef(
            artifact_id="art-original",
            project_id="p1",
            source_path="out.bin",
            media_type="application/octet-stream",
            size=len(DATA),
            sha256=hashlib.sha256(DATA).hexdigest(),
            created_at=datetime.now(UTC),
        )
    )


@pytest.fixture
async def service(aiohttp_server) -> AsyncIterator[Service]:
    service = Service()

    def response(
        body: bytes, *, status: int = 200, headers: dict[str, str] | None = None
    ) -> web.Response:
        return web.Response(
            body=body,
            status=status,
            headers={PROTOCOL_HEADER: str(PROTOCOL_VERSION), **(headers or {})},
        )

    async def capture(request: web.Request) -> web.Response:
        service.captures += 1
        return response(service.snapshot.model_dump_json().encode(), status=201)

    async def content(request: web.Request) -> web.Response:
        service.reads += 1
        return response(DATA, headers={"X-Content-SHA256": service.snapshot.sha256})

    async def acknowledge(request: web.Request) -> web.Response:
        ack = ArtifactRetentionAcknowledgement.model_validate_json(await request.read())
        assert request.query["project_id"] == ack.snapshot.project_id
        assert request.match_info["artifact_id"] == ack.snapshot.artifact_id
        service.acknowledgements.append(ack)
        service.entered.set()
        if service.mode == "wait":
            await service.release.wait()
        if service.failures:
            service.failures -= 1
            return response(
                b'{"error":{"kind":"unavailable","message":"lost ACK reply"}}', status=503
            )
        if service.mode == "wrong":
            ack = ack.model_copy(update={"acknowledgement_id": "different"})
        if service.mode == "large":
            return response(b"x" * 16385)
        if service.mode == "encoded":
            return response(b"encoded", headers={"Content-Encoding": "gzip"})
        if service.mode == "redirect":
            return response(b"", status=307, headers={"Location": service.url + "/redirect-target"})
        return response(ack.model_dump_json().encode())

    app = web.Application()
    app.add_routes(
        [
            web.post("/v1/artifacts", capture),
            web.get("/v1/artifacts/{artifact_id}/content", content),
            web.post("/v1/artifacts/{artifact_id}/retention", acknowledge),
        ]
    )
    server = await aiohttp_server(app)
    service.url = str(server.make_url("/")).rstrip("/")
    yield service
    service.release.set()


def source(endpoint: str, *, project: str = "p1") -> ArtifactPublicationSource:
    return ArtifactPublicationSource(
        context=CONTEXT.model_copy(update={"project_id": project}),
        run_id="run1",
        tool_call="call1",
        worker_endpoint_sha256=hashlib.sha256(endpoint.encode()).hexdigest(),
    )


def record(
    inventory: ArtifactInventory, endpoint: str, *, operation: str = "op1"
) -> ArtifactReceipt:
    author = source(endpoint)
    worker = Service().snapshot.model_copy(update={"artifact_id": "art-" + operation})
    inventory.begin(author, operation, "out.bin", None)
    inventory.pin(author, operation, "out.bin", None, worker)
    return inventory.finish(author, operation, "out.bin", None, worker, DATA)


def status(documents: DocumentStore) -> tuple[int, int, str]:
    with documents.control.read() as tx:
        return tx.sql.execute(
            "SELECT delivered,attempts,last_error FROM artifact_retention_outbox"
        ).fetchone()


def test_receipt_and_ack_intent_commit_together(tmp_path: Path, monkeypatch) -> None:
    with closing(DocumentStore(tmp_path / "control.sqlite3")) as documents:
        inventory = ArtifactInventory(documents)

        def failed(db, author, receipt):
            enqueue(db, author, receipt)
            raise OSError("synthetic failed receipt commit")

        monkeypatch.setattr(artifact_module, "enqueue", failed)
        with pytest.raises(OSError, match="synthetic failed"):
            record(inventory, "http://worker.invalid")
        assert (
            inventory.begin(source("http://worker.invalid"), "op1", "out.bin", None).state
            == "transfer"
        )
        assert not inventory.events("p1").entries
        with documents.control.read() as tx:
            assert (
                tx.sql.execute("SELECT count(*) FROM artifact_retention_outbox").fetchone()[0] == 0
            )


def test_no_acks_for_metadata_only_or_unknown_captures(tmp_path: Path) -> None:
    with closing(DocumentStore(tmp_path / "control.sqlite3")) as documents:
        inventory = ArtifactInventory(documents)
        author = source("http://worker.invalid")
        inventory.begin(author, "unknown", "out.bin", None)
        inventory.record_metadata(
            author, "out.bin", sha256="a" * 64, size=99, media_type="text/plain", note="too_large"
        )
        with documents.control.read() as tx:
            assert (
                tx.sql.execute("SELECT count(*) FROM artifact_retention_outbox").fetchone()[0] == 0
            )


async def test_lost_ack_reply_retries_original_identity_after_reopen(
    tmp_path: Path, service: Service
) -> None:
    path = tmp_path / "control.sqlite3"
    with closing(DocumentStore(path)) as documents:
        receipt = record(ArtifactInventory(documents), service.url)
        service.failures = 1
        async with WorkerClient(service.url, "test-secret-retention") as client:
            result = await drain(RetentionOutbox(documents.control), client, "p1")
        assert result.failed == 1
        assert status(documents)[0:2] == (0, 1)
    with closing(DocumentStore(path)) as documents:
        async with WorkerClient(service.url, "test-secret-retention") as client:
            result = await drain(RetentionOutbox(documents.control), client, "p1")
        assert result.delivered == 1
        assert result.failed == 0
        assert status(documents) == (1, 2, "")
    assert service.acknowledgements == [acknowledgement(receipt)] * 2
    assert service.captures == service.reads == 0


@pytest.mark.parametrize("mode", ["wrong", "large", "encoded", "redirect"])
async def test_bad_or_redirected_ack_remains_pending(
    tmp_path: Path, service: Service, mode: str
) -> None:
    service.mode = mode
    with closing(DocumentStore(tmp_path / "control.sqlite3")) as documents:
        record(ArtifactInventory(documents), service.url)
        async with WorkerClient(service.url, "test-secret-retention") as client:
            result = await drain(RetentionOutbox(documents.control), client, "p1")
        assert result.failed == 1
        assert status(documents)[0] == 0


async def test_other_project_and_endpoint_cannot_receive_ack(
    tmp_path: Path, service: Service
) -> None:
    with closing(DocumentStore(tmp_path / "control.sqlite3")) as documents:
        record(ArtifactInventory(documents), "http://original-worker.invalid")
        async with WorkerClient(service.url, "test-secret-retention") as client:
            assert (await drain(RetentionOutbox(documents.control), client, "p1")).delivered == 0
            assert (await drain(RetentionOutbox(documents.control), client, "other")).delivered == 0
        assert not service.acknowledgements
        assert status(documents) == (0, 0, "")


async def test_successful_publication_stays_recorded_and_replay_never_recaptures(
    tmp_path: Path, service: Service
) -> None:
    with closing(DocumentStore(tmp_path / "control.sqlite3")) as documents:
        inventory = ArtifactInventory(documents)
        service.failures = 1
        async with WorkerClient(service.url, "test-secret-retention") as client:
            publisher = ArtifactPublisher(inventory, client, CONTEXT, run_id="run1")
            try:
                first = await publisher.publish(
                    PublishRequest(context=CONTEXT, path="out.bin"), "op1", tool_call="call1"
                )
                assert first.receipt.state == "recorded"
                assert status(documents)[0] == 0
                second = await publisher.publish(
                    PublishRequest(context=CONTEXT, path="out.bin"), "op1", tool_call="call1"
                )
                assert second.receipt == first.receipt
                assert first.worker is not None
                assert second.worker is not None
                assert second.worker.model_dump() == first.worker.model_dump()
                assert status(documents)[0] == 1
            finally:
                await publisher.aclose()
        assert service.captures == service.reads == 1
        assert service.acknowledgements[0] == service.acknowledgements[1]


async def test_cancelled_ack_stays_recoverable(tmp_path: Path, service: Service) -> None:
    service.mode = "wait"
    with closing(DocumentStore(tmp_path / "control.sqlite3")) as documents:
        record(ArtifactInventory(documents), service.url)
        async with WorkerClient(service.url, "test-secret-retention") as client:
            task = asyncio.create_task(drain(RetentionOutbox(documents.control), client, "p1"))
            try:
                await asyncio.wait_for(service.entered.wait(), 5)
                assert not documents.control.in_transaction
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
                assert status(documents)[0] == 0
                service.mode = "ok"
                service.release.set()
                assert (
                    await drain(RetentionOutbox(documents.control), client, "p1")
                ).delivered == 1
            finally:
                service.release.set()


def test_delivered_audit_and_intent_are_immutable(tmp_path: Path) -> None:
    with closing(DocumentStore(tmp_path / "control.sqlite3")) as documents:
        receipt = record(ArtifactInventory(documents), "http://worker.invalid")
        outbox = RetentionOutbox(documents.control)
        endpoint = source("http://worker.invalid").worker_endpoint_sha256
        (pending,) = outbox.pending("p1", endpoint)
        assert pending.acknowledgement == acknowledgement(receipt)
        outbox.settle("p1", endpoint, pending, error="")
        outbox.settle("p1", endpoint, pending, error="late failure")
        assert status(documents) == (1, 1, "")
        with (
            pytest.raises(sqlite3.IntegrityError, match="intent is kept"),
            documents.control.write() as tx,
        ):
            tx.sql.execute("UPDATE artifact_retention_outbox SET delivered=0")
        with (
            pytest.raises(sqlite3.IntegrityError, match="intent is kept"),
            documents.control.write() as tx,
        ):
            tx.sql.execute("DELETE FROM artifact_retention_outbox")


@pytest.mark.parametrize("field", ["acknowledgement", "receipt", "request"])
def test_corrupt_or_oversized_intent_cannot_authorize_delivery(tmp_path: Path, field: str) -> None:
    with closing(DocumentStore(tmp_path / "control.sqlite3")) as documents:
        record(ArtifactInventory(documents), "http://worker.invalid")
        db = sqlite3.connect(documents.path)
        try:
            db.execute("PRAGMA ignore_check_constraints=ON")
            db.execute("DROP TRIGGER artifact_retention_kept")
            table = (
                "artifact_retention_outbox" if field == "acknowledgement" else "artifact_operations"
            )
            db.execute(f"UPDATE {table} SET {field}=?", ("x" * 20000,))
            db.commit()
        finally:
            db.close()
        with pytest.raises((DocumentError, ValueError), match=r"retention|truncated"):
            RetentionOutbox(documents.control).pending(
                "p1", source("http://worker.invalid").worker_endpoint_sha256
            )


async def test_ack_drain_pages_and_shares_one_clock_budget(
    tmp_path: Path, service: Service, monkeypatch: pytest.MonkeyPatch
) -> None:
    with closing(DocumentStore(tmp_path / "control.sqlite3")) as documents:
        inventory = ArtifactInventory(documents)
        first = record(inventory, service.url, operation="op1")
        second = record(inventory, service.url, operation="op2")
        clock = FakeClock()
        async with WorkerClient(service.url, "test-secret-retention", clock=clock) as client:
            original = client.acknowledge_artifact_retention

            async def taking_budget(ack: ArtifactRetentionAcknowledgement, *, timeout: float = 5):
                echoed = await original(ack, timeout=timeout)
                clock.advance(5)
                return echoed

            monkeypatch.setattr(client, "acknowledge_artifact_retention", taking_budget)
            result = await drain(RetentionOutbox(documents.control), client, "p1", limit=2)
            assert result.delivered == 1
            assert service.acknowledgements == [acknowledgement(first)]
            result = await drain(
                RetentionOutbox(documents.control), client, "p1", after=result.after
            )
            assert result.delivered == 1
            assert service.acknowledgements == [acknowledgement(first), acknowledgement(second)]


async def test_well_formed_ack_for_another_revision_is_not_dispatched(
    tmp_path: Path, service: Service
) -> None:
    with closing(DocumentStore(tmp_path / "control.sqlite3")) as documents:
        receipt = record(ArtifactInventory(documents), service.url)
        original = acknowledgement(receipt)
        forged = original.model_copy(
            update={"revision": original.revision.model_copy(update={"revision": 2})}
        )
        with documents.control.write() as tx:
            tx.sql.execute("DROP TRIGGER artifact_retention_kept")
            tx.sql.execute(
                "UPDATE artifact_retention_outbox SET acknowledgement=?",
                (forged.model_dump_json(),),
            )
        async with WorkerClient(service.url, "test-secret-retention") as client:
            result = await drain(RetentionOutbox(documents.control), client, "p1")
        assert result.failed == 1
        assert not service.acknowledgements
        assert status(documents)[0] == 0


async def test_operator_command_drains_only_a_bounded_original_endpoint_page(
    tmp_path: Path, service: Service, capsys: pytest.CaptureFixture[str]
) -> None:
    database = tmp_path / "control.sqlite3"
    with closing(DocumentStore(database)) as documents:
        record(ArtifactInventory(documents), service.url, operation="op1")
        record(ArtifactInventory(documents), service.url, operation="op2")
    token = tmp_path / "worker.token"
    token.write_text("test-secret-retention")
    result = await asyncio.to_thread(
        cli_artifacts.main,
        [
            "acknowledge",
            "--database",
            str(database),
            "--worker",
            service.url,
            "--token-file",
            str(token),
            "--project",
            "p1",
            "--limit",
            "1",
        ],
    )
    assert result == 0
    printed = capsys.readouterr().out
    assert json.loads(printed)["delivered"] == 1
    assert "test-secret" not in printed
    assert len(service.acknowledgements) == 1
    with closing(DocumentStore(database)) as documents:
        assert (
            len(
                RetentionOutbox(documents.control).pending(
                    "p1", source(service.url).worker_endpoint_sha256
                )
            )
            == 1
        )


async def test_execution_replay_from_later_run_retries_pending_ack_without_capture(
    tmp_path: Path, service: Service
) -> None:
    with closing(DocumentStore(tmp_path / "control.sqlite3")) as documents:
        inventory = ArtifactInventory(documents)
        attribution = {
            "kind": "execution_record",
            "execution_id": "job-original",
            "attempt_state": "succeeded",
        }
        service.failures = 1
        async with WorkerClient(service.url, "test-secret-retention") as client:
            first = ArtifactPublisher(inventory, client, CONTEXT, run_id="run1")
            try:
                receipt = await first.publish(
                    PublishRequest(context=CONTEXT, path="out.bin"),
                    "op1",
                    tool_call="call1",
                    attribution=attribution,
                )
                assert receipt.receipt.state == "recorded"
                assert status(documents)[0] == 0
            finally:
                await first.aclose()
            later = ArtifactPublisher(inventory, client, CONTEXT, run_id="run2")
            try:
                replay = await later.prior_execution(
                    "op1", "out.bin", later.source("call1", **attribution)
                )
                assert replay is not None
                assert replay.content_retained
                assert replay.receipt.revision == receipt.receipt.revision
                assert status(documents)[0] == 1
            finally:
                await later.aclose()
        assert service.captures == service.reads == 1
        assert service.acknowledgements[0] == service.acknowledgements[1]


@pytest.mark.parametrize("field", ["actor", "operation", "state"])
def test_corrupt_join_keys_are_bounded_before_python_allocation(tmp_path: Path, field: str) -> None:
    with closing(DocumentStore(tmp_path / "control.sqlite3")) as documents:
        record(ArtifactInventory(documents), "http://worker.invalid")
        huge = "x" * (2 * 1024 * 1024)
        db = sqlite3.connect(documents.path)
        try:
            db.execute("PRAGMA ignore_check_constraints=ON")
            db.execute("DROP TRIGGER artifact_retention_kept")
            db.execute(f"UPDATE artifact_operations SET {field}=?", (huge,))
            if field != "state":
                db.execute(f"UPDATE artifact_retention_outbox SET {field}=?", (huge,))
            db.commit()
        finally:
            db.close()
        tracemalloc.start()
        try:
            with pytest.raises(DocumentError, match="invalid artifact retention record"):
                RetentionOutbox(documents.control).pending(
                    "p1", source("http://worker.invalid").worker_endpoint_sha256
                )
            assert tracemalloc.get_traced_memory()[1] < 1024 * 1024
        finally:
            tracemalloc.stop()
