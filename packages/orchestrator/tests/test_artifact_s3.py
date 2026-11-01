# SPDX-License-Identifier: MIT
"""Remote durability, immutable retries, bounded cold reads and operator eviction."""

from __future__ import annotations

import asyncio
import hashlib
import importlib
import io
import os
import sqlite3
import tempfile
import threading
import tracemalloc
from uuid import uuid4

import pytest
from pydantic import ValidationError

from sandbox_science.contracts import PublishRequest
from sandbox_science.orchestrator import artifact_s3
from sandbox_science.orchestrator.artifact_blobs import CHUNK_BYTES, ArtifactBlobs, Blob
from sandbox_science.orchestrator.artifact_config import S3Settings, install, load
from sandbox_science.orchestrator.artifact_publisher import ArtifactPublisher
from sandbox_science.orchestrator.artifacts import ArtifactInventory
from sandbox_science.orchestrator.cli_artifacts import main
from sandbox_science.orchestrator.documents import DocumentError, DocumentStore
from sandbox_science.testing import FakeClock, eventually

from .test_artifact_blobs import install as record_blob
from .test_artifact_inventory import record
from .test_artifact_publication import CONTEXT, SnapshotClient

SETTINGS = S3Settings(
    bucket="test-artifacts", endpoint="https://objects.invalid", addressing_style="path"
)


class SDKError(Exception):
    def __init__(self, status=500):
        self.response = {"ResponseMetadata": {"HTTPStatusCode": status}}


class FakeBody(io.BytesIO):
    def __init__(self, data, owner):
        super().__init__(data)
        self.owner = owner

    def read(self, size: int | None = -1):
        assert isinstance(size, int)
        assert 0 < size <= self.owner.read_limit
        if self.owner.on_read:
            self.owner.on_read()
        return super().read(size)


class FakeS3:
    """Model the SDK's response bodies and conditional writes, never its internals."""

    def __init__(self):
        self.objects = {}
        self.requests = []
        self.bodies = []
        self.put_failure = False
        self.get_failure = False
        self.corrupt = False
        self.size_delta = 0
        self.short_body = False
        self.long_body = False
        self.omit_length = False
        self.ignore_range = False
        self.read_limit = CHUNK_BYTES
        self.on_put = None
        self.on_read = None

    def put_object(self, **kwargs):
        self.requests.append(("put", kwargs["Key"]))
        assert kwargs["Bucket"] == SETTINGS.bucket
        assert kwargs["IfNoneMatch"] == "*"
        if self.on_put:
            self.on_put()
        if kwargs["Key"] in self.objects:
            raise SDKError(412)
        data = bytearray()
        while chunk := kwargs["Body"].read(CHUNK_BYTES):
            data.extend(chunk)
        assert len(data) == kwargs["ContentLength"]
        self.objects[kwargs["Key"]] = bytes(data)
        if self.put_failure:
            raise SDKError()  # server accepted it, but its response was lost
        return {}

    def get_object(self, **kwargs):
        self.requests.append(("get", kwargs.get("Range")))
        if self.get_failure or kwargs["Key"] not in self.objects:
            raise SDKError(404)
        data = self.objects[kwargs["Key"]]
        if self.corrupt and data:
            data = bytes([data[0] ^ 1]) + data[1:]
        size = len(data)
        response = {}
        if "Range" in kwargs and not self.ignore_range:
            end = int(kwargs["Range"].split("-")[1])
            data = data[: end + 1]
            response["ContentRange"] = f"bytes 0-{end}/{size}"
        body = FakeBody(data, self)
        if self.short_body:
            body = FakeBody(data[:-1], self)
        if self.long_body:
            body = FakeBody(data + b"extra", self)
        self.bodies.append(body)
        result = {**response, "Body": body, "ContentLength": len(data) + self.size_delta}
        if self.omit_length:
            del result["ContentLength"]
        return result

    def head_object(self, **kwargs):
        return {"ContentLength": len(self.objects[kwargs["Key"]])}

    def close(self):
        pass


@pytest.fixture
def remote(monkeypatch):
    service = FakeS3()
    monkeypatch.setattr(artifact_s3, "connect", lambda *_: (service, (SDKError,)))
    return service


def expected(data):
    return Blob(hashlib.sha256(data).hexdigest(), len(data))


def write(store, data):
    writer = store.writer(expected(data))
    try:
        writer.write(data)
        return writer.commit()
    finally:
        writer.discard()


def configured(tmp_path):
    store = ArtifactBlobs(tmp_path / "control.sqlite3")
    install(store.root, SETTINGS)
    return store


def test_local_default_never_imports_sdk_or_creates_read_directories(tmp_path, monkeypatch):
    def forbidden(*_):
        pytest.fail("local-only artifacts reached S3")

    monkeypatch.setattr(artifact_s3, "connect", forbidden)
    store = ArtifactBlobs(tmp_path / "control.sqlite3")
    with pytest.raises(DocumentError), store.open(expected(b"missing")):
        pass
    assert not store.root.exists()
    blob = write(store, b"local")
    with store.open(blob) as stream:
        assert stream.read() == b"local"


def test_configuration_is_private_idempotent_and_cannot_redirect_archived_content(tmp_path):
    store = configured(tmp_path)
    path = store.root / "storage.toml"
    assert path.stat().st_mode & 0o777 == 0o600
    assert load(store.root) == SETTINGS
    before = path.read_bytes()
    install(store.root, SETTINGS)
    with pytest.raises(ValueError, match="migrate"):
        install(store.root, SETTINGS.model_copy(update={"prefix": "different"}))
    assert path.read_bytes() == before
    path.chmod(0o644)
    with pytest.raises(DocumentError, match="configuration"):
        store.prefix(expected(b"anything"), 4)


@pytest.mark.parametrize(
    "changes",
    [
        {"endpoint": "https://test-secret-user:test-secret-key@objects.invalid"},
        {"endpoint": "https://objects.invalid?token=test-secret-key"},
        {"endpoint": "https://objects.invalid:bad"},
        {"prefix": "../outside"},
        {"access_key": "test-secret-key"},
    ],
)
def test_config_rejects_secrets_and_ambiguous_destinations(changes):
    with pytest.raises(ValidationError):
        S3Settings.model_validate({**SETTINGS.model_dump(), **changes})


def test_symlink_and_oversized_configuration_fail_closed(tmp_path):
    store = configured(tmp_path)
    path = store.root / "storage.toml"
    saved = path.read_bytes()
    outside = tmp_path / "test-config.toml"
    outside.write_bytes(saved)
    outside.chmod(0o600)
    path.unlink()
    path.symlink_to(outside)
    with pytest.raises(DocumentError, match="configuration"):
        store.prefix(expected(b"content"), 4)
    path.unlink()
    path.write_bytes(saved + b" " * 8192)
    path.chmod(0o600)
    with pytest.raises(DocumentError, match="configuration"):
        store.prefix(expected(b"content"), 4)


@pytest.mark.parametrize("content", [b"", b"verified content" * 100])
def test_long_lived_store_observes_configuration_and_conditional_retries(tmp_path, remote, content):
    store = ArtifactBlobs(tmp_path / "control.sqlite3")
    install(store.root, SETTINGS)
    blob = expected(content)
    writer = store.writer(blob)
    writer.write(content)

    def installed():
        assert writer.installed
        assert store.path(blob.sha256).read_bytes() == content

    remote.on_put = installed
    try:
        assert writer.commit() == blob
    finally:
        writer.discard()
    remote.on_put = None
    assert write(store, content) == blob
    assert remote.objects == {SETTINGS.key(blob.sha256): content}
    assert [name for name, _ in remote.requests] == ["put", "get", "put", "get"]
    assert all(body.closed for body in remote.bodies)


@pytest.mark.parametrize("failure", ["put_failure", "get_failure", "corrupt", "size_delta"])
def test_failed_remote_durability_preserves_installed_source(tmp_path, remote, failure):
    store = configured(tmp_path)
    data = b"durable local source"
    writer = store.writer(expected(data))
    writer.write(data)
    setattr(remote, failure, True if failure != "size_delta" else 1)
    try:
        with pytest.raises(DocumentError, match="archival"):
            writer.commit()
        assert writer.installed
        assert store.path(expected(data).sha256).read_bytes() == data
    finally:
        writer.discard()
    assert all(body.closed for body in remote.bodies)


def test_existing_remote_key_is_never_replaced_even_if_corrupt(tmp_path, remote):
    store = configured(tmp_path)
    data = b"valid local original"
    remote.objects[SETTINGS.key(expected(data).sha256)] = b"bad remote copy"
    with pytest.raises(DocumentError, match="archival"):
        write(store, data)
    assert store.path(expected(data).sha256).read_bytes() == data
    assert remote.objects[SETTINGS.key(expected(data).sha256)] == b"bad remote copy"


async def test_unknown_put_keeps_reservation_and_retry_uses_original_worker_snapshot(
    tmp_path, remote
):
    documents = DocumentStore(tmp_path / "control.sqlite3")
    inventory = ArtifactInventory(documents)
    install(inventory.blobs.root, SETTINGS)
    worker = SnapshotClient()
    publisher = ArtifactPublisher(inventory, worker, CONTEXT, run_id="r1")
    request = PublishRequest(context=CONTEXT, path="out.bin")
    try:
        remote.put_failure = True
        first = await publisher.publish(request, "op1", tool_call="call1")
        assert first.receipt.state == "held"
        with documents.control.read() as tx:
            assert tx.sql.execute("SELECT count(*) FROM artifact_transfers").fetchone()[0] == 1
        assert inventory.list("p1").entries == ()
        worker.data = b"changed mutable path"
        remote.put_failure = False
        second = await publisher.publish(request, "op1", tool_call="call1")
        assert second.receipt.state == "recorded"
        assert second.receipt.revision is not None
        assert inventory.content("p1", second.receipt.revision) == b"original"
        assert worker.publishes == 1
        # The earlier uncertain transfer stays charged until explicit recovery.
        with documents.control.read() as tx:
            assert tx.sql.execute("SELECT count(*) FROM artifact_transfers").fetchone()[0] == 1
    finally:
        await publisher.aclose()
        documents.close()


async def test_cancellation_keeps_remote_io_and_reservation_owned_until_drained(tmp_path, remote):
    documents = DocumentStore(tmp_path / "control.sqlite3")
    inventory = ArtifactInventory(documents)
    install(inventory.blobs.root, SETTINGS)
    entered, release = threading.Event(), threading.Event()

    def blocked():
        entered.set()
        assert release.wait(5)

    remote.on_put = blocked
    publisher = ArtifactPublisher(inventory, SnapshotClient(), CONTEXT, run_id="run1")
    caller = asyncio.create_task(
        publisher.publish(PublishRequest(context=CONTEXT, path="out.bin"), "op1", tool_call="call1")
    )
    closing = None
    try:
        await eventually(entered.is_set)
        caller.cancel()
        caller.cancel()
        with pytest.raises(asyncio.CancelledError):
            await caller
        with documents.control.read() as tx:
            assert tx.sql.execute("SELECT count(*) FROM artifact_transfers").fetchone()[0] == 1
        closing = asyncio.create_task(publisher.aclose())
        await asyncio.sleep(0)
        closing.cancel()
        await asyncio.sleep(0)
        closing.cancel()
        assert not closing.done()
    finally:
        release.set()
        if closing is not None:
            with pytest.raises(asyncio.CancelledError):
                await closing
        else:
            await publisher.aclose()
        documents.close()
    reopened = DocumentStore(documents.path, readonly=True)
    try:
        assert ArtifactInventory(reopened).list("p1").entries[0].content_retained
        with reopened.control.read() as tx:
            assert tx.sql.execute("SELECT count(*) FROM artifact_transfers").fetchone()[0] == 0
    finally:
        reopened.close()


def test_remote_verified_but_metadata_commit_failed_keeps_orphan_and_charge(tmp_path, remote):
    documents = DocumentStore(tmp_path / "control.sqlite3")
    inventory = ArtifactInventory(documents)
    install(inventory.blobs.root, SETTINGS)
    try:
        with documents.control.write() as tx:
            tx.sql.execute(
                "CREATE TRIGGER fail_revision BEFORE INSERT ON artifact_revisions "
                "BEGIN SELECT RAISE(ABORT,'test metadata failure'); END"
            )
        with pytest.raises(sqlite3.DatabaseError):
            record_blob(inventory, b"remote but uncommitted")
        with documents.control.read() as tx:
            assert tx.sql.execute("SELECT count(*) FROM artifact_transfers").fetchone()[0] == 1
            assert tx.sql.execute("SELECT count(*) FROM artifact_revision_blobs").fetchone()[0] == 0
        assert len(remote.objects) == 1
        assert list(inventory.blobs.root.glob("blobs/*/*"))
    finally:
        documents.close()


def test_cli_archives_references_only_then_evicts_and_reopens_pins(tmp_path, remote, capsys):
    database = tmp_path / "control.sqlite3"
    documents = DocumentStore(database)
    inventory = ArtifactInventory(documents)
    receipt = record_blob(inventory, b"recorded data")
    legacy = record(inventory, "inline", b"inline unchanged")
    assert receipt.revision is not None
    assert legacy.revision is not None
    assert SETTINGS.endpoint is not None
    orphan = write(inventory.blobs, b"unreferenced orphan")
    staging = inventory.blobs.root / "staging" / "owned-partial"
    staging.write_bytes(b"do not sweep")
    held = inventory.reserve_transfer("p1", 10, "a" * 64)
    base = ["--database", str(database)]
    try:
        assert (
            main(
                [
                    "configure",
                    *base,
                    "--bucket",
                    SETTINGS.bucket,
                    "--endpoint",
                    SETTINGS.endpoint,
                    "--addressing-style",
                    "path",
                ]
            )
            == 0
        )
        assert main(["archive", *base]) == 0
        blob = expected(b"recorded data")
        assert set(remote.objects) == {SETTINGS.key(blob.sha256)}
        assert main(["evict", *base]) == 0
        assert inventory.blobs.path(blob.sha256).exists()
        assert '"state": "would_evict"' in capsys.readouterr().out
        with inventory.open_content("p1", receipt.revision) as pinned:
            assert main(["evict", *base, "--apply"]) == 0
            assert pinned.read() == b"recorded data"
        assert not inventory.blobs.path(blob.sha256).exists()
        assert inventory.blobs.path(orphan.sha256).read_bytes() == b"unreferenced orphan"
        assert staging.read_bytes() == b"do not sweep"
        with documents.control.read() as tx:
            assert tx.sql.execute("SELECT identity FROM artifact_transfers").fetchone()[0] == held
        documents.close()
        documents = DocumentStore(database, readonly=True)
        inventory = ArtifactInventory(documents)
        with inventory.open_content("p1", receipt.revision) as stream:
            assert stream.tell() == 0
            stream.seek(9)
            assert stream.read() == b"data"
        assert inventory.content("p1", legacy.revision) == b"inline unchanged"
        remote.read_limit = 5
        assert inventory.blobs.prefix(blob, 4) == b"reco"
        assert remote.requests[-1] == ("get", "bytes=0-3")
        assert not inventory.blobs.path(blob.sha256).exists()
    finally:
        documents.close()


@pytest.mark.parametrize("failure", ["get_failure", "corrupt", "size_delta"])
def test_eviction_refuses_missing_or_corrupt_remote_copy(tmp_path, remote, failure):
    store = configured(tmp_path)
    blob = write(store, b"preserve local data")
    setattr(remote, failure, True if failure != "size_delta" else 1)
    with pytest.raises((OSError, ValueError)):
        store.evict(blob, apply=True)
    assert store.path(blob.sha256).exists()


@pytest.mark.parametrize(
    "failure", ["get_failure", "corrupt", "size_delta", "short_body", "long_body", "omit_length"]
)
def test_cold_read_failure_closes_spool_and_does_not_return_content(
    tmp_path, remote, monkeypatch, failure
):
    store = configured(tmp_path)
    blob = write(store, b"cold content")
    store.evict(blob, apply=True)
    spools = []
    original = tempfile.TemporaryFile

    def spool(*args, **kwargs):
        stream = original(*args, **kwargs)
        spools.append(stream)
        return stream

    monkeypatch.setattr(tempfile, "TemporaryFile", spool)
    setattr(remote, failure, True if failure != "size_delta" else 1)
    with pytest.raises(DocumentError), store.open(blob):
        pytest.fail("unverified remote data escaped")
    assert spools
    assert all(stream.closed for stream in spools)
    assert all(body.closed for body in remote.bodies)


def test_missing_sdk_cannot_acknowledge_remote_durability(tmp_path, monkeypatch):
    store = configured(tmp_path)

    def missing(_):
        raise ModuleNotFoundError("optional SDK unavailable")

    monkeypatch.setattr(importlib, "import_module", missing)
    with pytest.raises(DocumentError, match="archival"):
        write(store, b"preserve when SDK absent")
    assert store.path(expected(b"preserve when SDK absent").sha256).exists()


def test_cold_read_is_seekable_closes_on_consumer_failure_and_uses_bounded_memory(tmp_path, remote):
    store = configured(tmp_path)
    data = b"bounded data" * (1024 * 1024)
    blob = write(store, data)
    store.evict(blob, apply=True)
    streams = []

    def consume():
        with store.open(blob) as stream:
            streams.append(stream)
            assert stream.tell() == 0
            stream.seek(-12, os.SEEK_END)
            assert stream.read() == data[-12:]
            raise RuntimeError("consumer failed")

    tracemalloc.start()
    try:
        with pytest.raises(RuntimeError, match="consumer"):
            consume()
        assert all(stream.closed for stream in streams)
        assert tracemalloc.get_traced_memory()[1] < 3 * CHUNK_BYTES
    finally:
        tracemalloc.stop()


def test_prefix_refuses_ignored_range_and_full_read_has_total_deadline(tmp_path, remote):
    store = configured(tmp_path)
    blob = write(store, b"abcdef")
    store.evict(blob, apply=True)
    remote.ignore_range = True
    with pytest.raises(DocumentError):
        store.prefix(blob, 2)
    clock = FakeClock()
    backing = artifact_s3.S3Backing(SETTINGS, clock=clock, timeout=1)
    remote.on_read = lambda: clock.advance(2)
    with pytest.raises(TimeoutError):
        backing.copy(blob)
    assert all(body.closed for body in remote.bodies)


def test_sdk_contract_uses_standard_chain_finite_timeouts_and_compatible_checksums(
    tmp_path, monkeypatch
):
    boto = pytest.importorskip("boto3")
    stub = pytest.importorskip("botocore.stub")
    response = pytest.importorskip("botocore.response")
    client = boto.client(
        "s3",
        region_name="us-east-1",
        aws_access_key_id="test-secret-id",
        aws_secret_access_key="test-secret-key",
    )
    data = b"SDK contract"
    blob = expected(data)

    def sdk(service, **kwargs):
        assert service == "s3"
        assert set(kwargs) == {"region_name", "endpoint_url", "config"}
        config = kwargs["config"]
        assert config.s3 == {"addressing_style": "path"}
        assert config.request_checksum_calculation == "when_required"
        assert config.response_checksum_validation == "when_required"
        assert config.retries == {"total_max_attempts": 1}
        assert 0 < config.connect_timeout <= config.read_timeout < 60
        return client

    monkeypatch.setattr(boto, "client", sdk)
    with stub.Stubber(client) as pending:
        pending.add_response(
            "put_object",
            {},
            {
                "Bucket": SETTINGS.bucket,
                "Key": SETTINGS.key(blob.sha256),
                "Body": stub.ANY,
                "ContentLength": blob.size,
                "IfNoneMatch": "*",
            },
        )
        pending.add_response(
            "get_object",
            {
                "Body": response.StreamingBody(io.BytesIO(data), len(data)),
                "ContentLength": len(data),
            },
            {"Bucket": SETTINGS.bucket, "Key": SETTINGS.key(blob.sha256)},
        )
        assert write(configured(tmp_path), data) == blob
        pending.assert_no_pending_responses()


@pytest.mark.integration
def test_opt_in_compatible_endpoint(tmp_path):
    """Leaves one small immutable test object; no test issues a remote delete."""
    bucket = os.environ.get("SS_ARTIFACT_S3_TEST_BUCKET")
    if not bucket:
        pytest.skip("set SS_ARTIFACT_S3_TEST_BUCKET to a dedicated existing test bucket")
    pytest.importorskip("boto3")
    settings = S3Settings(
        bucket=bucket,
        prefix=f"qualification/{uuid4().hex}",
        endpoint=os.environ.get("SS_ARTIFACT_S3_TEST_ENDPOINT"),
        region=os.environ.get("SS_ARTIFACT_S3_TEST_REGION", "us-east-1"),
        addressing_style="path",
    )
    store = ArtifactBlobs(tmp_path / "control.sqlite3")
    install(store.root, settings)
    data = b"sandbox-science S3 compatibility qualification\n" * 1024
    blob = write(store, data)
    store.evict(blob, apply=True)
    with store.open(blob) as stream:
        assert stream.read() == data
    assert store.prefix(blob, 16) == data[:16]
