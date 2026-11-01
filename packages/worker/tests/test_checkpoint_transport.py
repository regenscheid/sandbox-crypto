# SPDX-License-Identifier: MIT
"""SDK-stubbed durable chunk transport with exact lost-reply reconciliation."""

from __future__ import annotations

import hashlib
import importlib
import io
import json
import shutil
import tracemalloc
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from sandbox_science.contracts import EnvironmentInfo, EnvironmentState
from sandbox_science.contracts.workspace_checkpoint import CheckpointReceipt, CheckpointSource
from sandbox_science.testing import FakeClock
from sandbox_science.worker import checkpoint_s3, checkpoint_transport
from sandbox_science.worker.checkpoint_operator import install, publish_directory, verify
from sandbox_science.worker.checkpoint_s3 import Objects, Storage
from sandbox_science.worker.checkpoint_stream import CHUNK, encode
from sandbox_science.worker.checkpoint_transport import CHUNK_BYTES, fetch, upload
from sandbox_science.worker.cli import main
from sandbox_science.worker.environments import EnvironmentRecords
from sandbox_science.worker.store import Store as WorkerStore


class FakeSdkError(Exception):
    pass


class Store:
    def __init__(self, root: Path) -> None:
        root.mkdir()
        self.root = root
        self.objects: dict[str, Path] = {}
        self.calls: list[tuple[str, str]] = []
        self.lose: set[str] = set()
        self.deny_get: set[str] = set()
        self.closed = 0

    def put_object(self, **arguments: Any) -> None:
        key = arguments["Key"]
        self.calls.append(("put", key))
        assert arguments["IfNoneMatch"] == "*"
        if key in self.objects:
            raise FakeSdkError("conditional conflict")
        target = self.root / str(len(self.objects))
        with target.open("wb") as handle:
            shutil.copyfileobj(arguments["Body"], handle, CHUNK)
        assert target.stat().st_size == arguments["ContentLength"]
        self.objects[key] = target
        if any("/" + kind + "/" in key for kind in self.lose):
            raise FakeSdkError("reply lost after immutable creation")

    def get_object(self, **arguments: Any) -> dict[str, Any]:
        key = arguments["Key"]
        self.calls.append(("get", key))
        if key not in self.objects or any("/" + kind + "/" in key for kind in self.deny_get):
            raise FakeSdkError("object unavailable")
        path = self.objects[key]
        return {"Body": path.open("rb"), "ContentLength": path.stat().st_size}

    def close(self) -> None:
        self.closed += 1


@pytest.fixture
def storage(tmp_path, monkeypatch):
    remote = Store(tmp_path / "remote")
    settings = Storage(
        bucket="test-checkpoints",
        prefix="test-only",
        endpoint="https://s3.example.invalid",
        addressing_style="path",
    )
    monkeypatch.setattr(
        checkpoint_s3, "connect", lambda _settings, _timeout: (remote, (FakeSdkError,))
    )
    return settings, remote


@pytest.fixture
def checkpoint(tmp_path):
    root, directory = tmp_path / "source", tmp_path / "local"
    root.mkdir()
    directory.mkdir()
    (root / ".openshell-initialized").write_bytes(b"initialized\n")
    (root / "result").write_text("test research bytes")
    source = CheckpointSource(
        project_id="p",
        environment_id="e",
        sandbox="ss-test",
        sandbox_id="sandbox-original",
        profile="crypto-base",
        image="example.invalid/test@sha256:" + "a" * 64,
        policy="sha256:" + "b" * 64,
        launch_settings=None,
        architecture="arm64",
        require_workspace_prefix=False,
        prefix_identity=None,
        package_fingerprint=None,
    )
    with (directory / "archive.bin").open("wb") as handle:
        summary = encode(root, source, handle)
    receipt = CheckpointReceipt(
        source=source,
        created_at=datetime.now(UTC),
        archive_sha256=summary.sha256,
        archive_size=summary.size,
        members=summary.members,
        content_bytes=summary.content_bytes,
        metadata_bytes=summary.metadata_bytes,
    )
    install(directory / "receipt.json", receipt.model_dump_json().encode())
    return directory, source, receipt


def test_upload_fetch_preserves_exact_receipt_and_source_without_rebinding(
    tmp_path, checkpoint, storage
):
    directory, source, receipt = checkpoint
    settings, remote = storage
    identity = upload(directory, source, settings)
    assert (
        identity
        == hashlib.sha256(
            remote.objects[settings.key("manifests", identity)].read_bytes()
        ).hexdigest()
    )
    target = tmp_path / "fetched"
    assert fetch(target, source, settings, identity) == receipt
    assert verify(target, source) == receipt
    assert (target / "archive.bin").read_bytes() == (directory / "archive.bin").read_bytes()
    assert (target / "receipt.json").read_bytes() == (directory / "receipt.json").read_bytes()
    assert (directory / "archive.bin").is_file()
    assert remote.closed == 2


@pytest.mark.parametrize("kind", ["chunks", "manifests"])
def test_lost_put_reply_reconciles_exact_immutable_bytes_and_same_retry_identity(
    checkpoint, storage, kind
):
    directory, source, _ = checkpoint
    settings, remote = storage
    remote.lose.add(kind)
    identity = upload(directory, source, settings)
    originals = {key: path.read_bytes() for key, path in remote.objects.items()}
    assert upload(directory, source, settings) == identity
    assert {key: path.read_bytes() for key, path in remote.objects.items()} == originals
    assert len([key for key in remote.objects if "/manifests/" in key]) == 1


def test_unconfirmed_chunk_write_never_publishes_final_manifest_and_can_retry(checkpoint, storage):
    directory, source, _ = checkpoint
    settings, remote = storage
    remote.lose.add("chunks")
    remote.deny_get.add("chunks")
    with pytest.raises(OSError, match="object-store request failed"):
        upload(directory, source, settings)
    assert not any("/manifests/" in key for key in remote.objects)
    assert len(remote.objects) == 1
    remote.deny_get.clear()
    assert upload(directory, source, settings)
    assert len(remote.objects) == 2


def test_existing_hash_object_is_verified_and_never_overwritten(checkpoint, storage):
    directory, source, _ = checkpoint
    settings, remote = storage
    upload(directory, source, settings)
    key = next(key for key in remote.objects if "/chunks/" in key)
    path = remote.objects[key]
    raw = path.read_bytes()
    path.write_bytes(b"X" + raw[1:])
    with pytest.raises(ValueError, match="bytes differ"):
        upload(directory, source, settings)
    assert path.read_bytes() == b"X" + raw[1:]


@pytest.mark.parametrize("change", ["corrupt", "truncate", "extend"])
def test_fetch_rejects_changed_chunk_and_keeps_only_incomplete_staging(
    tmp_path, checkpoint, storage, change
):
    directory, source, _ = checkpoint
    settings, remote = storage
    identity = upload(directory, source, settings)
    path = remote.objects[next(key for key in remote.objects if "/chunks/" in key)]
    raw = path.read_bytes()
    path.write_bytes(
        {"corrupt": b"X" + raw[1:], "truncate": raw[:-1], "extend": raw + b"X"}[change]
    )
    target = tmp_path / "fetched"
    with pytest.raises(ValueError, match=r"differ|exceeds"):
        fetch(target, source, settings, identity)
    assert not target.exists()
    assert (tmp_path / "fetched.checkpoint-fetch/intent.json").is_file()
    assert (tmp_path / "fetched.checkpoint-fetch/failure.json").is_file()
    assert not (tmp_path / "fetched.checkpoint-fetch/receipt.json").exists()


def test_manifest_digest_is_required_before_any_chunk_fetch(tmp_path, checkpoint, storage):
    directory, source, _ = checkpoint
    settings, remote = storage
    identity = upload(directory, source, settings)
    path = remote.objects[settings.key("manifests", identity)]
    path.write_bytes(path.read_bytes() + b"garbage")
    remote.calls.clear()
    with pytest.raises(ValueError, match="bytes differ"):
        fetch(tmp_path / "fetched", source, settings, identity)
    assert all("/chunks/" not in key for _, key in remote.calls)
    with pytest.raises(ValueError, match="string_pattern_mismatch"):
        fetch(tmp_path / "other", source, settings, "../manifest")


def test_fetch_refuses_wrong_source_and_existing_destinations(tmp_path, checkpoint, storage):
    directory, source, _ = checkpoint
    settings, remote = storage
    identity = upload(directory, source, settings)
    remote.calls.clear()
    with pytest.raises(FileExistsError, match="new destination"):
        fetch(directory, source, settings, identity)
    assert remote.calls == []
    wrong = source.model_copy(update={"project_id": "other-project"})
    with pytest.raises(ValueError, match="retained original source"):
        fetch(tmp_path / "other", wrong, settings, identity)
    assert not (tmp_path / "other").exists()
    assert all("/chunks/" not in key for _, key in remote.calls)


@pytest.mark.parametrize("change", ["footer", "trailing", "chunk-size", "header-field"])
def test_well_hashed_but_invalid_manifest_is_not_a_completed_checkpoint(
    tmp_path, checkpoint, storage, change
):
    directory, source, _ = checkpoint
    settings, remote = storage
    identity = upload(directory, source, settings)
    path = remote.objects[settings.key("manifests", identity)]
    lines = path.read_bytes().splitlines()
    if change == "footer":
        body = json.loads(lines[-1])
        body["chunks"] += 1
        lines[-1] = json.dumps(body).encode()
    elif change == "trailing":
        lines.append(lines[-1])
    elif change == "chunk-size":
        body = json.loads(lines[1])
        body["size"] -= 1
        lines[1] = json.dumps(body).encode()
    else:
        body = json.loads(lines[0])
        body["destination"] = "unexpected"
        lines[0] = json.dumps(body).encode()
    data = b"\n".join(lines) + b"\n"
    forged = hashlib.sha256(data).hexdigest()
    other = remote.root / "forged-manifest"
    other.write_bytes(data)
    remote.objects[settings.key("manifests", forged)] = other
    with pytest.raises(ValueError, match=r"checkpoint|Header"):
        fetch(tmp_path / "fetched", source, settings, forged)
    assert not (tmp_path / "fetched").exists()


def test_operation_deadline_does_not_commit_a_manifest(checkpoint, storage):
    directory, source, _ = checkpoint
    settings, remote = storage
    clock = FakeClock()
    objects = Objects(settings, timeout=1, clock=clock)
    clock.advance(2)
    with pytest.raises(TimeoutError, match="operation deadline"):
        objects.retain("chunks", "a" * 64, 1, io.BytesIO(b"x"))
    assert remote.calls == []
    for timeout in (0, float("nan"), 86401):
        with pytest.raises(ValueError, match="timeout"):
            upload(directory, source, settings, timeout=timeout)


def test_transport_deadline_is_checked_during_local_archive_validation(checkpoint, storage):
    directory, source, _ = checkpoint
    settings, remote = storage

    class AdvancingClock(FakeClock):
        def monotonic(self) -> float:
            self.advance(0.25)
            return super().monotonic()

    with pytest.raises(TimeoutError, match="operation deadline"):
        upload(directory, source, settings, timeout=1, clock=AdvancingClock())
    assert remote.calls == []


def test_transport_size_and_manifest_bounds_are_separate_from_local_artifacts(
    checkpoint, storage, monkeypatch
):
    directory, source, receipt = checkpoint
    settings, remote = storage
    monkeypatch.setattr(checkpoint_transport, "MAX_TRANSPORT_BYTES", receipt.archive_size - 1)
    with pytest.raises(ValueError, match="transport size bound"):
        upload(directory, source, settings)
    assert remote.calls == []
    monkeypatch.setattr(checkpoint_transport, "MAX_TRANSPORT_BYTES", receipt.archive_size)
    monkeypatch.setattr(checkpoint_transport, "MAX_MANIFEST_BYTES", 1)
    with pytest.raises(ValueError, match="manifest exceeds"):
        upload(directory, source, settings)


def test_upload_requires_valid_local_receipt_before_any_remote_call(checkpoint, storage):
    directory, source, _ = checkpoint
    settings, remote = storage
    with (directory / "archive.bin").open("ab") as handle:
        handle.write(b"uncommitted")
    with pytest.raises(ValueError, match="trailing"):
        upload(directory, source, settings)
    assert remote.calls == []


def test_private_storage_settings_refuse_credentials_and_unsafe_endpoints(tmp_path):
    path = tmp_path / "storage.toml"
    path.write_text('bucket = "test-checkpoints"\nendpoint = "https://s3.example.invalid"\n')
    path.chmod(0o600)
    assert checkpoint_s3.load(path).bucket == "test-checkpoints"
    path.chmod(0o644)
    with pytest.raises(ValueError, match="private and bounded"):
        checkpoint_s3.load(path)
    path.chmod(0o600)
    path.write_text('bucket = "test-checkpoints"\nsecret_access_key = "test-secret-do-not-echo"\n')
    with pytest.raises(ValueError, match="settings are invalid") as caught:
        checkpoint_s3.load(path)
    assert "test-secret" not in str(caught.value)
    for endpoint in (
        "https://user:password@example.invalid",
        "https://example.invalid/path",
        "file:///tmp/no",
        "https://example.invalid?query=yes",
    ):
        with pytest.raises(ValueError, match="invalid checkpoint endpoint"):
            Storage(bucket="test-checkpoints", endpoint=endpoint)


def test_multi_chunk_transfer_uses_fixed_parts_and_keeps_final_receipt(
    tmp_path, checkpoint, storage
):
    directory, source, _ = checkpoint
    settings, remote = storage
    root = tmp_path / "large-source"
    root.mkdir()
    with (root / "large").open("wb") as handle:
        handle.truncate(CHUNK_BYTES + 123)
    with (directory / "archive.bin").open("wb") as handle:
        summary = encode(root, source, handle)
    receipt = CheckpointReceipt(
        source=source,
        created_at=datetime.now(UTC),
        archive_sha256=summary.sha256,
        archive_size=summary.size,
        members=summary.members,
        content_bytes=summary.content_bytes,
        metadata_bytes=summary.metadata_bytes,
    )
    (directory / "receipt.json").write_bytes(receipt.model_dump_json().encode())
    tracemalloc.start()
    try:
        identity = upload(directory, source, settings)
        chunks = [path.stat().st_size for key, path in remote.objects.items() if "/chunks/" in key]
        assert len(chunks) == 2
        assert max(chunks) == CHUNK_BYTES
        assert fetch(tmp_path / "fetched", source, settings, identity) == receipt
        assert tracemalloc.get_traced_memory()[1] < 4 * 1024**2
    finally:
        tracemalloc.stop()


def test_lost_manifest_reply_and_receipt_reconcile_the_same_final_identity(checkpoint, storage):
    directory, source, _ = checkpoint
    settings, remote = storage
    remote.lose.add("manifests")
    remote.deny_get.add("manifests")
    with pytest.raises(OSError, match="object-store request failed"):
        upload(directory, source, settings)
    manifest = next(key for key in remote.objects if "/manifests/" in key)
    original = {key: path.read_bytes() for key, path in remote.objects.items()}
    remote.deny_get.clear()
    assert upload(directory, source, settings) == manifest.rpartition("/")[2]
    assert {key: path.read_bytes() for key, path in remote.objects.items()} == original


def test_fetch_never_replaces_a_destination_created_during_download(
    tmp_path, checkpoint, storage, monkeypatch
):
    directory, source, _ = checkpoint
    settings, _ = storage
    identity = upload(directory, source, settings)
    publish = publish_directory

    def competing(stage, target):
        target.mkdir()
        (target / "preserve").write_text("other operator output")
        publish(stage, target)

    monkeypatch.setattr(checkpoint_transport, "publish_directory", competing)
    target = tmp_path / "fetched"
    with pytest.raises(FileExistsError, match="already exists"):
        fetch(target, source, settings, identity)
    assert (target / "preserve").read_text() == "other operator output"
    assert not (tmp_path / "fetched.checkpoint-fetch/receipt.json").exists()


def test_upload_detects_local_archive_changes_before_final_manifest(
    checkpoint, storage, monkeypatch
):
    directory, source, _ = checkpoint
    settings, remote = storage
    put = remote.put_object

    def mutate(**arguments):
        put(**arguments)
        with (directory / "archive.bin").open("ab") as handle:
            handle.write(b"changed after the chunk was read")

    monkeypatch.setattr(remote, "put_object", mutate)
    with pytest.raises(ValueError, match="source archive changed"):
        upload(directory, source, settings)
    assert not any("/manifests/" in key for key in remote.objects)


def test_oversize_manifest_response_is_rejected_before_reading(
    tmp_path, checkpoint, storage, monkeypatch
):
    directory, source, _ = checkpoint
    settings, remote = storage
    identity = upload(directory, source, settings)
    body = io.BytesIO(b"small")
    monkeypatch.setattr(
        remote,
        "get_object",
        lambda **_: {"Body": body, "ContentLength": checkpoint_transport.MAX_MANIFEST_BYTES + 1},
    )
    with pytest.raises(ValueError, match="exceeds its bound"):
        fetch(tmp_path / "fetched", source, settings, identity)
    assert body.closed
    assert not (tmp_path / "fetched").exists()


def test_production_transport_cli_uses_original_binding_and_private_destination(
    tmp_path, checkpoint, storage, capsys
):
    directory, source, receipt = checkpoint
    settings, _ = storage
    state = tmp_path / "state"
    state.mkdir()
    records = EnvironmentRecords(state / "environments.sqlite3")
    records.save(
        EnvironmentInfo(
            project_id=source.project_id,
            environment_id=source.environment_id,
            sandbox=source.sandbox,
            sandbox_id=source.sandbox_id,
            profile=source.profile,
            image=source.image,
            policy=source.policy,
            state=EnvironmentState.STOPPED,
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
    )
    records.close()
    WorkerStore(state / "worker.sqlite3").close()
    config = tmp_path / "worker.toml"
    config.write_text(
        f'state_dir = "{state}"\ntoken_file = "unused-test-token"\n'
        "[openshell]\nisolation_tier = 3\n"
    )
    destination = tmp_path / "storage.toml"
    destination.write_text(
        'bucket = "test-checkpoints"\nprefix = "test-only"\n'
        'endpoint = "https://s3.example.invalid"\naddressing_style = "path"\n'
    )
    destination.chmod(0o600)
    assert checkpoint_s3.load(destination) == settings
    common = [
        "--settings",
        str(config),
        "--environment",
        "e",
        "--architecture",
        "arm64",
        "--storage",
        str(destination),
    ]
    assert main(["workspace-checkpoint", "upload", *common, "--checkpoint", str(directory)]) == 0
    output = capsys.readouterr().out
    identity = output.split("remote_checkpoint_manifest=", 1)[1].split()[0]
    target = tmp_path / "fetched"
    assert (
        main(
            [
                "workspace-checkpoint",
                "fetch",
                *common,
                "--checkpoint",
                str(target),
                "--manifest",
                identity,
            ]
        )
        == 0
    )
    assert verify(target, source) == receipt
    assert "recovery_set=incomplete" in capsys.readouterr().out
    assert (
        main(["workspace-checkpoint", "fetch", *common, "--checkpoint", str(tmp_path / "other")])
        == 1
    )
    assert "fetch requires --manifest" in capsys.readouterr().err


def test_sdk_setup_keeps_standard_credential_chain_and_disables_hidden_retries(
    tmp_path, monkeypatch
):
    remote = Store(tmp_path / "fake-sdk")
    captured = {}

    def configuration(**arguments):
        captured["configuration"] = arguments
        return "test-config"

    def client(service, **arguments):
        captured["service"] = service
        captured["client"] = arguments
        return remote

    modules = {
        "boto3": SimpleNamespace(client=client),
        "botocore.config": SimpleNamespace(Config=configuration),
        "botocore.exceptions": SimpleNamespace(
            BotoCoreError=FakeSdkError, ClientError=FakeSdkError
        ),
    }
    monkeypatch.setattr(importlib, "import_module", lambda name: modules[name])
    settings = Storage(
        bucket="test-checkpoints", endpoint="https://s3.example.invalid", addressing_style="path"
    )
    actual, errors = checkpoint_s3.connect(settings, 3)
    assert actual is remote
    assert errors == (FakeSdkError, FakeSdkError)
    assert captured["service"] == "s3"
    assert captured["client"] == {
        "region_name": "us-east-1",
        "endpoint_url": "https://s3.example.invalid",
        "config": "test-config",
    }
    assert captured["configuration"]["retries"] == {"total_max_attempts": 1}
    assert captured["configuration"]["connect_timeout"] == 3
    assert captured["configuration"]["read_timeout"] == 3
