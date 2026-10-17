# SPDX-License-Identifier: MIT
"""Durable filesystem content, compatibility, admission and bounded bulk reads."""

from __future__ import annotations

import hashlib
import os
import sqlite3
import tracemalloc
from pathlib import Path

import pytest

from sandbox_science.orchestrator.artifact_blobs import ArtifactBlobs, Blob
from sandbox_science.orchestrator.artifacts import ArtifactInventory, ArtifactLimits
from sandbox_science.orchestrator.documents import DocumentError, DocumentStore

from .test_artifact_inventory import SOURCE, record, snapshot


def install(inventory: ArtifactInventory, data: bytes, operation: str = "external"):
    worker = snapshot(data)
    inventory.begin(SOURCE, operation, "out.bin", None)
    inventory.pin(SOURCE, operation, "out.bin", None, worker)
    transfer = inventory.reserve_transfer("p1", len(data), worker.sha256)
    writer = inventory.blobs.writer(Blob(worker.sha256, len(data)))
    try:
        for offset in range(0, len(data), 65536):
            writer.write(data[offset : offset + 65536])
        blob = writer.commit()
        return inventory.finish(
            SOURCE, operation, "out.bin", None, worker, b"", blob=blob, transfer=transfer
        )
    finally:
        writer.discard()


def test_external_and_legacy_content_survive_reopen_and_charge_same_quota(tmp_path: Path):
    documents = DocumentStore(tmp_path / "control.sqlite3")
    inventory = ArtifactInventory(documents)
    legacy = record(inventory, "legacy", b"legacy bytes")
    external = install(inventory, b"new durable bytes")
    assert legacy.revision
    assert external.revision
    with documents.control.read() as tx:
        assert (
            tx.sql.execute("SELECT sum(length(data)) FROM artifact_revisions").fetchone()[0] == 12
        )
        assert (
            tx.sql.execute("SELECT bytes FROM artifact_usage WHERE project='p1'").fetchone()[0]
            == 29
        )
    documents.close()
    reopened = ArtifactInventory(DocumentStore(documents.path, readonly=True))
    try:
        for reference, expected in (
            (legacy.revision, b"legacy bytes"),
            (external.revision, b"new durable bytes"),
        ):
            with reopened.open_content("p1", reference) as stream:
                assert stream.tell() == 0
                assert stream.read() == expected
                # Opening a stream leaves no store transaction alive.
                assert not reopened.documents.control.in_transaction
            assert reopened.content("p1", reference) == expected
        with (
            pytest.raises(DocumentError, match="project"),
            reopened.open_content("p2", external.revision),
        ):
            pass
        wrong = external.revision.model_copy(update={"sha256": "a" * 64})
        with pytest.raises(DocumentError, match="differs"), reopened.open_content("p1", wrong):
            pass
    finally:
        reopened.documents.close()


def test_corrupt_and_symlink_blobs_fail_closed(tmp_path: Path):
    inventory = ArtifactInventory(DocumentStore(tmp_path / "control.sqlite3"))
    receipt = install(inventory, b"trusted")
    assert receipt.revision
    path = inventory.blobs.path(receipt.revision.sha256)
    path.chmod(0o600)
    path.write_bytes(b"changed")
    with (
        pytest.raises(DocumentError, match="corrupt"),
        inventory.open_content("p1", receipt.revision),
    ):
        pass
    path.unlink()
    outside = tmp_path / "unrelated"
    outside.write_bytes(b"trusted")
    path.symlink_to(outside)
    with pytest.raises(DocumentError, match="corrupt"):
        inventory.content("p1", receipt.revision)
    assert outside.read_bytes() == b"trusted"
    inventory.documents.close()


def test_reservations_bound_concurrent_bytes_and_keep_installed_orphans_charged(tmp_path: Path):
    inventory = ArtifactInventory(
        DocumentStore(tmp_path / "control.sqlite3"),
        limits=ArtifactLimits(project_bytes=100, total_bytes=200),
    )
    one = inventory.reserve_transfer("p1", 70, "a" * 64)
    with pytest.raises(DocumentError, match="quota"):
        inventory.reserve_transfer("p1", 31, "b" * 64)
    inventory.release_transfer("p2", one)  # another project cannot free this hold
    with pytest.raises(DocumentError, match="quota"):
        inventory.reserve_transfer("p1", 31, "b" * 64)
    inventory.release_transfer("p1", one)
    assert inventory.reserve_transfer("p1", 100, "c" * 64)
    inventory.documents.close()


def test_completed_replay_releases_only_its_exact_transfer_reservation(tmp_path: Path):
    inventory = ArtifactInventory(DocumentStore(tmp_path / "control.sqlite3"))
    receipt = install(inventory, b"already installed")
    assert receipt.worker_snapshot is not None
    worker = receipt.worker_snapshot
    wrong = inventory.reserve_transfer("p1", worker.size, "a" * 64)
    blob = Blob(worker.sha256, worker.size)
    with pytest.raises(DocumentError, match="reservation differs"):
        inventory.finish(
            SOURCE, "external", "out.bin", None, worker, b"", blob=blob, transfer=wrong
        )
    inventory.release_transfer("p1", wrong)
    duplicate = inventory.reserve_transfer("p1", worker.size, worker.sha256)
    assert (
        inventory.finish(
            SOURCE, "external", "out.bin", None, worker, b"", blob=blob, transfer=duplicate
        )
        == receipt
    )
    with inventory.documents.control.read() as tx:
        assert tx.sql.execute("SELECT count(*) FROM artifact_transfers").fetchone()[0] == 0
        assert tx.sql.execute("SELECT count(*) FROM artifact_revisions").fetchone()[0] == 1
    inventory.documents.close()


def test_failed_metadata_commit_keeps_blob_and_reservation_without_false_revision(tmp_path: Path):
    inventory = ArtifactInventory(DocumentStore(tmp_path / "control.sqlite3"))
    data = b"retained orphan for explicit recovery"
    worker = snapshot(data)
    inventory.begin(SOURCE, "orphan", "out.bin", None)
    inventory.pin(SOURCE, "orphan", "out.bin", None, worker)
    transfer = inventory.reserve_transfer("p1", len(data), worker.sha256)
    writer = inventory.blobs.writer(Blob(worker.sha256, len(data)))
    writer.write(data)
    blob = writer.commit()
    with inventory.documents.control.write() as tx:
        tx.sql.execute(
            "CREATE TRIGGER fail_revision BEFORE INSERT ON artifact_revisions "
            "BEGIN SELECT RAISE(ABORT,'test commit refused'); END"
        )
    with pytest.raises(sqlite3.DatabaseError, match="test commit refused"):
        inventory.finish(
            SOURCE, "orphan", "out.bin", None, worker, b"", blob=blob, transfer=transfer
        )
    assert inventory.list("p1").entries == ()
    with inventory.documents.control.read() as tx:
        assert tx.sql.execute("SELECT bytes FROM artifact_transfers").fetchone()[0] == len(data)
    with inventory.blobs.open(blob) as stream:
        assert stream.read() == data
    inventory.documents.close()


@pytest.mark.slow
def test_exact_one_gib_blob_has_bounded_python_memory(tmp_path: Path):
    store = ArtifactBlobs(tmp_path / "control.sqlite3")
    chunk = b"bounded-artifact" * 32768  # 512 KiB
    digest = hashlib.sha256()
    for _ in range(2048):
        digest.update(chunk)
    expected = Blob(digest.hexdigest(), 1024**3)
    writer = store.writer(expected)
    tracemalloc.start()
    try:
        for _ in range(2048):
            writer.write(chunk)
        assert writer.commit() == expected
        with store.open(expected) as stream:
            stream.seek(-16, os.SEEK_END)
            assert stream.read() == chunk[-16:]
        _, peak = tracemalloc.get_traced_memory()
        assert peak < 4 * 1024 * 1024
    finally:
        tracemalloc.stop()
        writer.discard()
        store.path(expected.sha256).unlink(missing_ok=True)
