# SPDX-License-Identifier: MIT
"""Immutable upload identities across append-only migration, remote faults and retries."""

from __future__ import annotations

import hashlib
import sqlite3
import tempfile
import threading
import tracemalloc
from concurrent.futures import ThreadPoolExecutor

import pytest

from sandbox_science.orchestrator import artifact_s3
from sandbox_science.orchestrator import uploads as limits
from sandbox_science.orchestrator.artifact_blobs import CHUNK_BYTES, Blob
from sandbox_science.orchestrator.artifact_config import install
from sandbox_science.orchestrator.cli_artifacts import main, references
from sandbox_science.orchestrator.control import migrations
from sandbox_science.orchestrator.documents import DocumentError, DocumentStore
from sandbox_science.orchestrator.uploads import MAX_UPLOAD_BYTES, UserUploads

from .test_artifact_s3 import SETTINGS, FakeS3, SDKError
from .test_uploads import legacy_original, prepared


@pytest.fixture
def documents(tmp_path):
    store = prepared(tmp_path)
    try:
        yield store
    finally:
        store.close()


@pytest.fixture
def remote(monkeypatch):
    service = FakeS3()
    monkeypatch.setattr(artifact_s3, "connect", lambda *_: (service, (SDKError,)))
    return service


def upload(store, identity="new", data=b"original"):
    return store.create("p1", "s1", identity, "source.md", "text/markdown", data)


def held(documents):
    with documents.control.read() as tx:
        return tx.sql.execute(
            "SELECT identity,bytes,sha256 FROM user_upload_transfers ORDER BY identity"
        ).fetchall()


def test_v73_migration_does_not_rewrite_inline_rows_and_shares_sequence_and_constraints(
    tmp_path, monkeypatch
):
    with monkeypatch.context() as old:
        old.setattr(migrations, "MIGRATIONS", migrations.MIGRATIONS[: 73 - migrations.BASELINE + 1])
        old.setattr(migrations, "VERSION", 73)
        documents = prepared(tmp_path)
        original = legacy_original(documents)
        with documents.control.read() as tx:
            before = tx.sql.execute("SELECT * FROM user_uploads").fetchall()
        documents.close()
    documents = DocumentStore(tmp_path / "control.sqlite3")
    try:
        store = UserUploads(documents)
        current = upload(store)
        assert current.number == original.number + 1
        assert current.author == original.author == "user"
        assert store.get("p1", "s1", "old") == original
        assert store.content("p1", "s1", "old") == b"original"
        with documents.control.read() as tx:
            assert tx.sql.execute("SELECT * FROM user_uploads").fetchall() == before
            assert tx.sql.execute("SELECT count(*) FROM user_upload_blobs").fetchone()[0] == 1
        assert [found.upload_id for found in store.list("p1", "s1").uploads] == ["old", "new"]
        assert [found.upload_id for found in store.list("p1", "s1", after=1).uploads] == ["new"]
        for statement in (
            "UPDATE user_upload_blobs SET size=size",
            "DELETE FROM user_upload_blobs",
        ):
            with (
                pytest.raises(sqlite3.IntegrityError, match="immutable"),
                documents.control.write() as tx,
            ):
                tx.sql.execute(statement)
    finally:
        documents.close()


def test_shared_byte_quota_and_committed_replay_do_not_charge_retained_bytes_twice(
    documents, monkeypatch
):
    monkeypatch.setattr(limits, "MAX_PROJECT_BYTES", 10)
    legacy_original(documents, b"old")
    store = UserUploads(documents)
    first = upload(store, data=b"x" * 7)
    with pytest.raises(DocumentError, match="quota"):
        upload(store, "over", b"!")
    assert upload(store, data=b"x" * 7) == first
    assert held(documents) == []
    with pytest.raises(DocumentError, match="different"):
        upload(store, data=b"y" * 7)


def test_transfer_count_admission_covers_projects_and_global_processes(documents):
    store = UserUploads(documents)
    writers = []
    try:
        for project in ("p1", "p2"):
            writers.extend(
                store.begin(project, "s1", f"u{i}", "x.bin", "application/octet-stream")
                for i in range(2)
            )
        assert len(held(documents)) == 4
        assert sum(row[1] for row in held(documents)) == 4 * MAX_UPLOAD_BYTES
        with pytest.raises(DocumentError, match="admission"):
            store.begin("p1", "s1", "extra", "x.bin", "application/octet-stream")
        writers[0].discard()
        with store.begin("p1", "s1", "extra", "x.bin", "application/octet-stream"):
            assert len(held(documents)) == 4
    finally:
        for writer in writers:
            writer.discard()
    assert held(documents) == []


def test_shared_retained_count_quotas_include_pending_uploads_and_allow_receipt_replay(
    documents, monkeypatch
):
    monkeypatch.setattr(limits, "MAX_PROJECT_UPLOADS", 2)
    monkeypatch.setattr(limits, "MAX_TOTAL_UPLOADS", 3)
    legacy_original(documents, b"")
    store = UserUploads(documents)
    with store.begin("p1", "s1", "pending", "x.bin", "application/octet-stream", 0) as writer:
        with pytest.raises(DocumentError, match="quota"):
            store.begin("p1", "s1", "over", "x.bin", "application/octet-stream", 0)
        other = store.create("p2", "s1", "other", "x.bin", "application/octet-stream", b"")
        with pytest.raises(DocumentError, match="quota"):
            store.begin("p2", "s1", "over", "x.bin", "application/octet-stream", 0)
        found = writer.commit()
    assert store.create("p1", "s1", "pending", "x.bin", "application/octet-stream", b"") == found
    assert store.create("p2", "s1", "other", "x.bin", "application/octet-stream", b"") == other
    assert held(documents) == []


def test_live_same_id_owner_cannot_be_replaced_and_failed_begin_keeps_original_hold(documents):
    store, other = UserUploads(documents), UserUploads(documents)
    with store.begin("p1", "s1", "same", "x.bin", "application/octet-stream", 8) as owner:
        owner.write(b"original")
        prior = held(documents)
        with pytest.raises(DocumentError, match="owns"):
            other.begin("p1", "s1", "same", "x.bin", "application/octet-stream", 8)
        assert held(documents) == prior
        assert owner.commit().sha256 == hashlib.sha256(b"original").hexdigest()
    assert held(documents) == []


@pytest.mark.parametrize("backed", [False, True])
def test_zero_byte_originals_remain_exact_and_replay_after_optional_remote_eviction(
    documents, remote, backed
):
    store = UserUploads(documents)
    if backed:
        install(store.blobs.root, SETTINGS)
    found = upload(store, data=b"")
    assert found.size == 0
    assert found.sha256 == hashlib.sha256(b"").hexdigest()
    assert held(documents) == []
    if backed:
        assert store.blobs.evict(Blob(found.sha256, 0), apply=True)
    with store.open_content("p1", "s1", "new") as stream:
        assert stream.read() == b""
        assert stream.tell() == 0
    assert upload(store, data=b"") == found
    assert store.content("p1", "s1", "new") == b""


def test_discarded_ids_use_a_bounded_stable_lock_namespace(documents):
    store = UserUploads(documents)
    for index in range(100):
        with store.begin("p1", "s1", f"discard-{index}", "x.bin", "application/octet-stream", 0):
            pass
    locks = list((store.blobs.root / "upload-locks").iterdir())
    assert locks
    assert all(len(path.name) == 3 and set(path.name) <= set("0123456789abcdef") for path in locks)
    assert len(locks) <= 4096
    assert held(documents) == []


def test_distinct_ids_on_the_same_lock_stripe_refuse_overlap_without_changing_admission(documents):
    store = UserUploads(documents)
    # These distinct fixture IDs collide in the bounded ownership namespace.
    with store.begin("p1", "s1", "collision-20", "x.bin", "application/octet-stream", 0):
        before = held(documents)
        with pytest.raises(DocumentError, match="owns"):
            store.begin("p1", "s1", "collision-66", "x.bin", "application/octet-stream", 0)
        assert held(documents) == before
    with store.begin("p1", "s1", "collision-66", "x.bin", "application/octet-stream", 0):
        pass
    assert held(documents) == []


def test_scratch_close_failure_still_releases_preinstall_admission_and_owner(
    documents, monkeypatch
):
    original = tempfile.TemporaryFile

    class FailingClose:
        def __init__(self):
            self.file = original(mode="w+b")

        def close(self):
            self.file.close()
            raise OSError("test scratch flush failed")

    monkeypatch.setattr(tempfile, "TemporaryFile", lambda **kwargs: FailingClose())
    store = UserUploads(documents)
    writer = store.begin("p1", "s1", "close", "x.bin", "application/octet-stream", 0)
    with pytest.raises(DocumentError, match="unavailable"):
        writer.discard()
    assert writer.lease.closed
    assert held(documents) == []
    # The exact same lock can be reacquired; cleanup did not leave a live owner.
    retry = store.begin("p1", "s1", "close", "x.bin", "application/octet-stream", 0)
    with pytest.raises(DocumentError, match="unavailable"):
        retry.discard()


@pytest.mark.parametrize("failure", ["put_failure", "get_failure", "corrupt"])
def test_unknown_remote_retries_reuse_exact_reservation_and_reject_changed_original(
    documents, remote, failure
):
    store = UserUploads(documents)
    install(store.blobs.root, SETTINGS)
    setattr(remote, failure, True)
    with pytest.raises(DocumentError, match="unavailable") as failed:
        upload(store)
    assert failed.value.kind == "unavailable"
    assert "objects.invalid" not in failed.value.message
    before = held(documents)
    assert len(before) == 1
    assert before[0][1:] == (8, hashlib.sha256(b"original").hexdigest())
    assert store.list("p1", "s1").uploads == []
    with pytest.raises(DocumentError, match="different"):
        upload(store, data=b"modified")
    assert held(documents) == before
    with pytest.raises(DocumentError, match="different"):
        store.begin("p1", "s1", "new", "different.md", "text/markdown", 8)
    assert held(documents) == before
    setattr(remote, failure, False)
    found = upload(store)
    assert found.sha256 == before[0][2]
    assert held(documents) == []
    remote.get_failure = True
    assert (
        upload(store) == found
    )  # committed receipt replay does not require another remote request


def test_failed_metadata_commit_preserves_same_hold_for_exact_retry(documents, remote):
    store = UserUploads(documents)
    install(store.blobs.root, SETTINGS)
    with documents.control.write() as tx:
        tx.sql.execute(
            "CREATE TRIGGER fail_upload BEFORE INSERT ON user_upload_blobs "
            "BEGIN SELECT RAISE(ABORT,'test refused commit'); END"
        )
    with pytest.raises(DocumentError, match="unavailable"):
        upload(store)
    before = held(documents)
    with documents.control.write() as tx:
        tx.sql.execute("DROP TRIGGER fail_upload")
    with store.begin("p1", "s1", "new", "source.md", "text/markdown", 8) as retry:
        assert held(documents) == before
        retry.write(b"original")
        found = retry.commit()
    assert found.size == 8
    assert held(documents) == []
    assert len(store.list("p1", "s1").uploads) == 1


@pytest.mark.parametrize("failure", ["short", "long", "body", "scratch"])
def test_preinstall_failures_release_only_drained_admission(documents, monkeypatch, failure):
    store = UserUploads(documents)
    if failure == "scratch":

        def no_space(*args, **kwargs):
            raise OSError("test full scratch")

        monkeypatch.setattr(tempfile, "TemporaryFile", no_space)
        with pytest.raises(DocumentError, match="unavailable"):
            store.begin("p1", "s1", "x", "x.bin", "application/octet-stream", 8)
    else:

        def receive():
            with store.begin("p1", "s1", "x", "x.bin", "application/octet-stream", 8) as writer:
                if failure == "body":
                    raise OSError("test disconnected body")
                writer.write(b"short" if failure == "short" else b"original!")
                writer.commit()

        with pytest.raises((DocumentError, OSError)):
            receive()
    assert held(documents) == []
    assert store.list("p1", "s1").uploads == []


def test_archive_evict_cold_originals_and_inline_reads_use_shared_reference_cli(
    documents, remote, capsys
):
    store = UserUploads(documents)
    old = legacy_original(documents)
    found = upload(store)
    blob = Blob(found.sha256, found.size)
    install(store.blobs.root, SETTINGS)
    args = ["--database", str(documents.path)]
    assert list(references(documents.path)) == [blob]
    assert main(["archive", *args]) == 0
    assert main(["evict", *args]) == 0
    assert store.blobs.path(found.sha256).exists()
    assert main(["evict", *args, "--apply"]) == 0
    assert not store.blobs.path(found.sha256).exists()
    with store.open_content("p1", "s1", found.upload_id) as stream:
        assert stream.tell() == 0
        assert not documents.control.in_transaction
        stream.seek(2)
        assert stream.read() == b"iginal"
    assert stream.closed
    assert store.content("p1", "s1", old.upload_id) == b"original"
    with pytest.raises(DocumentError, match="no such"):
        store.content("p2", "s1", found.upload_id)
    with pytest.raises(DocumentError, match="bound"):
        store.content("p1", "s1", found.upload_id, max_bytes=1)
    assert "would_evict" in capsys.readouterr().out


def test_unknown_length_stream_and_legacy_stream_have_bounded_python_memory(documents):
    store = UserUploads(documents)
    chunk = b"bounded original" * 32768
    total = len(chunk) * 24
    # A real inline predecessor also streams without materializing its BLOB.
    old = legacy_original(documents, chunk * 24)
    tracemalloc.start()
    try:
        with store.begin("p1", "s1", "new", "x.bin", "application/octet-stream") as writer:
            for _ in range(24):
                writer.write(chunk)
            found = writer.commit()
        assert found.size == total
        for identity in ("old", "new"):
            with store.open_content("p1", "s1", identity) as stream:
                assert not documents.control.in_transaction
                digest = hashlib.sha256()
                while data := stream.read(CHUNK_BYTES):
                    digest.update(data)
                assert digest.hexdigest() == (old.sha256 if identity == "old" else found.sha256)
        assert tracemalloc.get_traced_memory()[1] < 8 * CHUNK_BYTES
    finally:
        tracemalloc.stop()


def test_remote_io_does_not_hold_sql_transaction_and_active_retry_cannot_overlap(documents, remote):
    store = UserUploads(documents)
    install(store.blobs.root, SETTINGS)
    entered, release = threading.Event(), threading.Event()

    def blocked():
        assert not documents.control.in_transaction
        entered.set()
        assert release.wait(5)

    remote.on_put = blocked
    with ThreadPoolExecutor(max_workers=1) as pool:
        work = pool.submit(upload, store)
        try:
            assert entered.wait(5)
            with pytest.raises(DocumentError, match="owns"):
                upload(store)
            with documents.control.read() as tx:
                assert tx.sql.execute("SELECT count(*) FROM user_upload_blobs").fetchone()[0] == 0
        finally:
            release.set()
        assert work.result(5).size == 8
    assert held(documents) == []
