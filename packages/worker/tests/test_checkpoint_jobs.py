# SPDX-License-Identifier: MIT
"""Checkpoint Jobs offline: streaming capture with no local archive, the worker's own
verification (a tampered object is refused), promotion and deletion with the keeper key,
holds, the kept count and its rotation, the deletion journal and its refusals."""

from __future__ import annotations

import hashlib
import http.server
import io
import json
import os
import sqlite3
import ssl
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from sandbox_science.contracts import EnvironmentInfo, EnvironmentState
from sandbox_science.contracts.workspace_checkpoint import CheckpointInventory, CheckpointPlan
from sandbox_science.testing import FakeClock
from sandbox_science.worker import (
    checkpoint_capture,
    checkpoint_jobs,
    checkpoint_jobs_cli,
    checkpoint_remote,
    checkpoint_s3,
    checkpoint_transport,
)
from sandbox_science.worker.checkpoint_capture import capture_upload
from sandbox_science.worker.checkpoint_records import CheckpointRecords, RecordError, held_until
from sandbox_science.worker.checkpoint_s3 import Storage
from sandbox_science.worker.cli import main
from sandbox_science.worker.environments import EnvironmentRecords, OpenShellCli
from sandbox_science.worker.settings import OpenShellBackendSettings, Settings
from sandbox_science.worker.store import Store
from sandbox_science.worker.tls import ServerTLS

SMALL = 4096
LAUNCH = json.dumps({"kubernetes": {"pod": {"node_selector": {"kubernetes.io/arch": "amd64"}}}})
IMAGE = "example.invalid/research@sha256:" + "1" * 64
POLICY = "sha256:" + "2" * 64


class FakeSdkError(Exception):
    """botocore's ClientError carries the store's code in ``response``."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.response = {"Error": {"Code": code}}


def etag(data: bytes) -> str:
    return '"' + hashlib.md5(data, usedforsecurity=False).hexdigest() + '"'


class Garage:
    """Buckets as one store: like Garage, it ignores ``If-None-Match`` and copies inside."""

    def __init__(self) -> None:
        self.objects: dict[tuple[str, str], bytes] = {}
        self.calls: list[tuple[str, str, str]] = []
        self.before_copy: Any = None
        self.unavailable: set[str] = set()
        self.fail_delete: set[str] = set()

    def put_object(self, **arguments: Any) -> None:
        data = arguments["Body"].read()
        assert len(data) == arguments["ContentLength"]
        self.calls.append(("put", arguments["Bucket"], arguments["Key"]))
        self.objects[(arguments["Bucket"], arguments["Key"])] = data

    def get_object(self, **arguments: Any) -> dict[str, Any]:
        location = (arguments["Bucket"], arguments["Key"])
        self.calls.append(("get", *location))
        if any(location[1].endswith(name) for name in self.unavailable):
            raise FakeSdkError("SlowDown")
        if location not in self.objects:
            raise FakeSdkError("NoSuchKey")
        data = self.objects[location]
        return {"Body": io.BytesIO(data), "ContentLength": len(data), "ETag": etag(data)}

    def copy_object(self, **arguments: Any) -> None:
        source = arguments["CopySource"]
        self.calls.append(("copy", arguments["Bucket"], arguments["Key"]))
        if self.before_copy is not None:
            self.before_copy(source["Key"])
        data = self.objects[(source["Bucket"], source["Key"])]
        if arguments["CopySourceIfMatch"] != etag(data):
            raise FakeSdkError("PreconditionFailed")
        self.objects[(arguments["Bucket"], arguments["Key"])] = data

    def delete_object(self, **arguments: Any) -> None:
        self.calls.append(("delete", arguments["Bucket"], arguments["Key"]))
        if any(arguments["Key"].endswith(name) for name in self.fail_delete):
            raise FakeSdkError("ServiceUnavailable")
        self.objects.pop((arguments["Bucket"], arguments["Key"]), None)  # missing: a no-op

    def list_objects_v2(self, **arguments: Any) -> dict[str, object]:
        # Two keys a page, so that continuation is exercised.
        keys = sorted(
            key
            for bucket, key in self.objects
            if bucket == arguments["Bucket"] and key.startswith(arguments["Prefix"])
        )
        start = int(arguments.get("ContinuationToken", "0"))
        page = keys[start : start + 2]
        more = start + 2 < len(keys)
        return {
            "Contents": [{"Key": key} for key in page],
            "IsTruncated": more,
            **({"NextContinuationToken": str(start + 2)} if more else {}),
        }

    def close(self) -> None:
        pass

    def keys(self, bucket: str, kind: str) -> set[str]:
        return {
            key.rsplit("/", 1)[1] for b, key in self.objects if b == bucket and f"/{kind}/" in key
        }


@pytest.fixture
def garage(monkeypatch) -> Garage:
    store = Garage()
    monkeypatch.setattr(checkpoint_s3, "connect", lambda _s, _t: (store, (FakeSdkError,)))
    # Small chunks, so that a small workspace spans several.
    for module in (checkpoint_capture, checkpoint_transport, checkpoint_remote):
        monkeypatch.setattr(module, "CHUNK_BYTES", SMALL)
    return store


def storage(bucket: str) -> Storage:
    return Storage(
        bucket=bucket,
        prefix="test-only",
        endpoint="http://garage.test.invalid:3900",
        addressing_style="path",
        region="garage",
    )


UPLOAD, KEPT = storage("test-checkpoint-upload"), storage("test-checkpoints")


def workspace(root: Path) -> Path:
    root.mkdir()
    (root / ".openshell-initialized").write_bytes(b"initialized\n")
    (root / "research").mkdir()
    # About five small chunks, none alike.
    big = b"".join(hashlib.sha256(n.to_bytes(2)).digest() for n in range(600))
    (root / "research" / "big").write_bytes(big)
    (root / "research" / "notes").write_text("retained result")
    (root / "link").symlink_to("research/notes")
    return root


@pytest.fixture
def retained(tmp_path) -> Settings:
    """A worker state directory with one stopped session environment."""
    token = tmp_path / "pool-token"
    token.write_text("test-secret-" + "t" * 40)
    settings = Settings(
        token_file=token,
        state_dir=tmp_path / "state",
        openshell=OpenShellBackendSettings(isolation_tier=3),
    )
    settings.state_dir.mkdir()
    save_environment(settings, "e", "ss-checkpoint-test", EnvironmentState.STOPPED)
    Store(settings.state_dir / "worker.sqlite3").close()
    return settings


def save_environment(
    settings: Settings, environment_id: str, sandbox: str, state: EnvironmentState, **fields: Any
) -> None:
    records = EnvironmentRecords(settings.state_dir / "environments.sqlite3")
    records.save(
        EnvironmentInfo(
            environment_id=environment_id,
            project_id="p",
            profile="crypto-base",
            image=IMAGE,
            sandbox=sandbox,
            sandbox_id=f"{sandbox}-id",
            policy=POLICY,
            state=state,
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
            **fields,
        ),
        driver_config=LAUNCH,
        require_workspace_prefix=False,
    )
    records.close()


@pytest.fixture
def stopped(monkeypatch) -> list[str]:
    """OpenShell's phases, one per look; the last repeats."""
    phases = ["Stopped"]

    async def sandbox(self, name):
        phase = phases.pop(0) if len(phases) > 1 else phases[0]
        return {"id": f"{name}-id", "phase": phase}

    monkeypatch.setattr(OpenShellCli, "sandbox", sandbox)
    return phases


def planned(settings: Settings, **options: Any) -> CheckpointPlan:
    return checkpoint_jobs.plan(
        settings, "e", "amd64", "test-operator", suspend=options.pop("suspend", refuse), **options
    )


def refuse(_settings: Settings, _environment: str) -> bool:
    raise AssertionError("a stopped environment is not suspended again")


# Streaming capture and the worker's verification


def test_capture_streams_chunks_and_the_worker_verifies_every_byte(
    tmp_path, garage, retained, stopped
):
    plan = planned(retained)
    root = workspace(tmp_path / "volume")
    identity, receipt, written = capture_upload(root, plan.source, UPLOAD, plan.capture)
    assert receipt.capture == "suspended_pvc"
    assert receipt.format.endswith("-v2")
    assert len(garage.keys(UPLOAD.bucket, "chunks")) == written > 3
    assert garage.keys(UPLOAD.bucket, "manifests") == {identity}
    assert checkpoint_remote.verify(UPLOAD, identity, plan.source) == receipt
    # A retry of an unchanged tree re-sends no chunk: each one already reads back exactly.
    again, _, rewritten = capture_upload(root, plan.source, UPLOAD, plan.capture)
    assert rewritten == 0
    assert again != identity  # a new receipt time, so a new manifest


def test_capture_skips_and_lists_special_files(tmp_path, garage, retained, stopped):
    plan = planned(retained)
    root = workspace(tmp_path / "volume")
    os.mkfifo(root / "research" / "kernel.fifo")
    identity, receipt, _ = capture_upload(root, plan.source, UPLOAD, plan.capture)
    assert [(e.path, e.kind) for e in receipt.skipped] == [("research/kernel.fifo", "fifo")]
    assert checkpoint_remote.verify(UPLOAD, identity, plan.source).skipped == receipt.skipped


@pytest.mark.parametrize("target", ["chunk", "manifest", "missing-chunk"])
def test_a_tampered_or_missing_object_is_refused(tmp_path, garage, retained, stopped, target):
    plan = planned(retained)
    identity, _, _ = capture_upload(
        workspace(tmp_path / "volume"), plan.source, UPLOAD, plan.capture
    )
    chunk = sorted(k for b, k in garage.objects if "/chunks/" in k)[1]
    manifest = next(k for b, k in garage.objects if "/manifests/" in k)
    key = (UPLOAD.bucket, chunk if target != "manifest" else manifest)
    if target == "missing-chunk":
        del garage.objects[key]
    else:
        original = garage.objects[key]
        garage.objects[key] = bytes([original[0] ^ 1]) + original[1:]  # same length
    with pytest.raises((ValueError, OSError)):
        checkpoint_remote.verify(UPLOAD, identity, plan.source)


def test_verification_refuses_another_binding(tmp_path, garage, retained, stopped):
    plan = planned(retained)
    identity, _, _ = capture_upload(
        workspace(tmp_path / "volume"), plan.source, UPLOAD, plan.capture
    )
    other = plan.source.model_copy(update={"sandbox_id": "another-sandbox"})
    with pytest.raises(ValueError, match="another binding"):
        checkpoint_remote.verify(UPLOAD, identity, other)


# The worker's records: plan and hold, verify, keep, rotate


def test_plan_holds_the_environment_and_a_second_plan_is_refused(retained, stopped):
    clock = FakeClock(datetime.now(UTC))
    plan = planned(retained, clock=clock)
    assert plan.source.environment_id == "e"
    assert plan.capture == "suspended_pvc"
    assert held_until(retained.state_dir, "e", clock.now()) == plan.hold_expires_at
    with pytest.raises(RecordError, match="holds this environment"):
        planned(retained, clock=clock)
    clock.advance(checkpoint_jobs.HOLD_SECONDS + 1)
    assert held_until(retained.state_dir, "e", clock.now()) is None
    assert planned(retained, clock=clock).checkpoint_id != plan.checkpoint_id  # expired


def test_a_running_environment_is_suspended_first_and_one_in_use_is_left_running(retained, stopped):
    save_environment(retained, "e", "ss-checkpoint-test", EnvironmentState.READY)
    asked: list[str] = []

    def suspend(_settings: Settings, environment: str) -> bool:
        asked.append(environment)
        return True

    stopped[:] = ["Running", "Stopped"]
    planned(retained, suspend=suspend)
    assert asked == ["e"]
    records = CheckpointRecords(retained.state_dir)
    records.release(records.holds(datetime.now(UTC))[0].checkpoint_id, datetime.now(UTC), "test")
    records.close()
    stopped[:] = ["Running"]
    with pytest.raises(RecordError, match="in use"):
        planned(retained, suspend=lambda _s, _e: False)
    # The failed plan lifted its own hold.
    assert held_until(retained.state_dir, "e", datetime.now(UTC)) is None


def checkpointed(tmp_path: Path, settings: Settings, name: str) -> tuple[CheckpointPlan, str]:
    plan = planned(settings)
    root = workspace(tmp_path / name)
    (root / "research" / "notes").write_text(f"result of {name}")
    identity, _, _ = capture_upload(root, plan.source, UPLOAD, plan.capture)
    checkpoint_jobs.verify(
        settings, plan.checkpoint_id, identity, UPLOAD, "upload", "test-operator"
    )
    checkpoint_remote.promote(UPLOAD, KEPT, identity)
    checkpoint_jobs.verify(settings, plan.checkpoint_id, identity, KEPT, "kept", "test-operator")
    return plan, identity


def test_a_checkpoint_counts_only_once_verified_again_in_the_kept_bucket(
    tmp_path, garage, retained, stopped
):
    plan = planned(retained)
    identity, _, _ = capture_upload(workspace(tmp_path / "v"), plan.source, UPLOAD, plan.capture)
    with pytest.raises(RecordError, match="upload bucket first"):
        checkpoint_jobs.verify(retained, plan.checkpoint_id, identity, KEPT, "kept", "op")
    checkpoint_jobs.verify(retained, plan.checkpoint_id, identity, UPLOAD, "upload", "op")
    states = {c.manifest_sha256: c.state for c in checkpoint_jobs.inventory(retained).checkpoints}
    assert states == {identity: "verified"}
    assert held_until(retained.state_dir, "e", datetime.now(UTC)) is None  # hold lifted
    checkpoint_remote.promote(UPLOAD, KEPT, identity)
    assert any(call[0] == "copy" for call in garage.calls)  # inside the store
    # A kept copy that differs is not counted.
    chunk = next(k for b, k in garage.objects if b == KEPT.bucket and "/chunks/" in k)
    good = garage.objects[(KEPT.bucket, chunk)]
    garage.objects[(KEPT.bucket, chunk)] = b"x" * len(good)
    with pytest.raises(ValueError, match="differ"):
        checkpoint_jobs.verify(retained, plan.checkpoint_id, identity, KEPT, "kept", "op")
    garage.objects[(KEPT.bucket, chunk)] = good
    checkpoint_jobs.verify(retained, plan.checkpoint_id, identity, KEPT, "kept", "op")
    assert [c.state for c in checkpoint_jobs.inventory(retained).checkpoints] == ["kept"]


def test_a_capture_outside_its_hold_is_not_recorded(tmp_path, garage, retained, stopped):
    plan = planned(retained)
    late = FakeClock(plan.hold_expires_at + timedelta(hours=1))
    identity, _, _ = capture_upload(
        workspace(tmp_path / "v"), plan.source, UPLOAD, plan.capture, clock=late
    )
    with pytest.raises(RecordError, match="while its hold held"):
        checkpoint_jobs.verify(retained, plan.checkpoint_id, identity, UPLOAD, "upload", "op")


def test_the_latest_two_are_kept_and_older_ones_become_candidates(
    tmp_path, garage, retained, stopped
):
    identities = [checkpointed(tmp_path, retained, f"v{n}")[1] for n in range(3)]
    records = CheckpointRecords(retained.state_dir)
    try:
        keep, prune = records.rotation("ss-checkpoint-test", 2)
    finally:
        records.close()
    assert keep == identities[:0:-1]
    assert prune == identities[:1]


def test_deleting_a_checkpoint_keeps_chunks_another_one_uses(tmp_path, garage, retained, stopped):
    first = checkpointed(tmp_path, retained, "one")[1]
    second = checkpointed(tmp_path, retained, "two")[1]
    shared = garage.keys(KEPT.bucket, "chunks")
    removed = checkpoint_remote.delete(KEPT, first)
    assert first not in garage.keys(KEPT.bucket, "manifests")
    assert 0 < removed < len(shared)  # the large file's chunks are the same in both
    plan = (
        CheckpointRecords(retained.state_dir)
        .hold(
            next(
                c.checkpoint_id
                for c in checkpoint_jobs.inventory(retained).checkpoints
                if c.manifest_sha256 == second
            )
        )
        .plan
    )
    assert checkpoint_remote.verify(KEPT, second, plan.source).members > 1


def test_the_last_kept_checkpoint_of_an_existing_volume_is_never_forgotten(
    tmp_path, garage, retained, stopped
):
    first = checkpointed(tmp_path, retained, "one")[1]
    records = CheckpointRecords(retained.state_dir)
    now = datetime.now(UTC)
    try:
        with pytest.raises(RecordError, match="last verified checkpoint"):
            records.forget(first, now, "test-operator", volume_gone=False)
        second = checkpointed(tmp_path, retained, "two")[1]
        assert records.forget(first, now, "test-operator", volume_gone=False).state == "deleting"
        with pytest.raises(RecordError, match="last verified checkpoint"):
            records.forget(second, now, "test-operator", volume_gone=False)
        records.deleted(first, now, "test-operator", {"chunks_removed": 1})
        journal = records.deletions()
        assert [(d.kind, d.subject, d.phase, d.actor) for d in journal] == [
            ("checkpoint", first, "intent", "test-operator"),
            ("checkpoint", first, "done", "test-operator"),
        ]
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            records.db.execute("UPDATE deletions SET actor='someone else'")
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            records.db.execute("DELETE FROM deletions")
    finally:
        records.close()


# Volume deletions: the worker's refusals and its journal


def test_volume_deletion_refusals_and_journal(tmp_path, garage, retained, stopped):
    record = checkpoint_jobs.record_volume
    with pytest.raises(RecordError, match="is stopped"):
        record(
            retained,
            "intent",
            "pv-1",
            "ss-checkpoint-test",
            "op",
            without_checkpoint=True,
            detail={},
        )
    save_environment(retained, "gone", "ss-gone", EnvironmentState.DELETED)
    with pytest.raises(RecordError, match="no verified checkpoint"):
        record(retained, "intent", "pv-2", "ss-gone", "op", without_checkpoint=False, detail={})
    record(retained, "intent", "pv-2", "ss-gone", "op", without_checkpoint=True, detail={"gib": 20})
    with pytest.raises(RecordError, match="--without-checkpoint"):
        record(retained, "intent", "pv-3", None, "op", without_checkpoint=False, detail={})
    # A child whose hand-back was incomplete keeps its volume, even once deleted.
    save_environment(retained, "child", "ss-child", EnvironmentState.DELETED, kind="child")
    db = sqlite3.connect(retained.state_dir / "worker.sqlite3")
    db.execute(
        "INSERT INTO child_environments VALUES ('child','d','p','s','e','crypto-base','retained',"
        "NULL,NULL,'t','t',NULL,NULL,'t','t')"
    )
    db.commit()
    db.close()
    with pytest.raises(RecordError, match="hand-back is retained"):
        record(retained, "intent", "pv-4", "ss-child", "op", without_checkpoint=True, detail={})
    # A hold refuses too.
    planned(retained)
    found = checkpoint_jobs.inventory(retained)
    assert checkpoint_jobs.volume_refusals(found, "ss-checkpoint-test", without_checkpoint=True)
    record(retained, "done", "pv-2", "ss-gone", "op", without_checkpoint=True, detail={})
    journal = CheckpointRecords(retained.state_dir).deletions()
    assert [(d.subject, d.phase, d.actor, d.detail["sandbox"]) for d in journal] == [
        ("pv-2", "intent", "op", "ss-gone"),
        ("pv-2", "done", "op", "ss-gone"),
    ]


def test_inventory_traces_volumes_to_projects_sessions_and_hand_backs(retained, stopped):
    save_environment(
        retained, "c1", "ss-child", EnvironmentState.DELETED, kind="child", session_id="s1"
    )
    found = CheckpointInventory.model_validate_json(
        checkpoint_jobs.inventory(retained, keep=3).model_dump_json()
    )
    assert found.keep == 3
    rows = {e.environment_id: e for e in found.environments}
    assert rows["c1"].session_id == "s1"
    assert rows["c1"].state == "deleted"
    assert rows["e"].state == "stopped"
    assert rows["e"].project_id == "p"


# The commands


def test_the_job_command_reads_its_plan_and_storage_from_its_environment(
    tmp_path, garage, retained, stopped, monkeypatch, capsys
):
    plan = planned(retained)
    root = workspace(tmp_path / "volume")
    log = tmp_path / "termination-log"
    monkeypatch.setattr(checkpoint_jobs_cli, "TERMINATION_LOG", log)
    monkeypatch.setenv(checkpoint_jobs_cli.PLAN_VARIABLE, plan.model_dump_json())
    monkeypatch.setenv(checkpoint_jobs_cli.STORAGE_VARIABLE, UPLOAD.model_dump_json())
    assert main(["checkpoint", "capture-upload", "--workspace", str(root)]) == 0
    line = capsys.readouterr().out.splitlines()[0]
    identity = line.split()[0].removeprefix("checkpoint_manifest=")
    assert log.read_text() == f"checkpoint_manifest={identity}\n"
    assert f"checkpoint_id={plan.checkpoint_id}" in line
    # An expired plan, or storage settings carrying a credential, are refused.
    expired = plan.model_copy(update={"hold_expires_at": datetime.now(UTC) - timedelta(1)})
    monkeypatch.setenv(checkpoint_jobs_cli.PLAN_VARIABLE, expired.model_dump_json())
    assert main(["checkpoint", "capture-upload", "--workspace", str(root)]) == 1
    monkeypatch.setenv(checkpoint_jobs_cli.PLAN_VARIABLE, plan.model_dump_json())
    leaked = json.dumps({**UPLOAD.model_dump(), "aws_secret_access_key": "test-secret-leak"})
    monkeypatch.setenv(checkpoint_jobs_cli.STORAGE_VARIABLE, leaked)
    assert main(["checkpoint", "capture-upload", "--workspace", str(root)]) == 1
    assert "test-secret-leak" not in capsys.readouterr().err


def test_the_local_suspend_accepts_only_the_workers_own_certificate(tmp_path, retained):
    from sandbox_science.testing.mcp import make_tls

    make_tls(tmp_path)
    seen: list[str] = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            seen.append(f"{self.path} {self.headers['Authorization'][:12]}")
            info = EnvironmentInfo(
                environment_id="e",
                project_id="p",
                profile="crypto-base",
                image=IMAGE,
                sandbox="ss-checkpoint-test",
                policy=POLICY,
                state=EnvironmentState.STOPPED,
                created_at=datetime.now(UTC),
                updated_at=datetime.now(UTC),
            )
            body = json.dumps({"environment": info.model_dump(mode="json"), "suspended": True})
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body.encode())

        def log_message(self, format: str, *args: object) -> None:
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(tmp_path / "server.pem", tmp_path / "server-key.pem")
    server.socket = context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        own = retained.model_copy(
            update={
                "listen": f"127.0.0.1:{port}",
                "tls": ServerTLS(
                    certificate=tmp_path / "server.pem", private_key=tmp_path / "server-key.pem"
                ),
            }
        )
        assert checkpoint_jobs.local_suspend(own, "e")
        assert seen == ["/v1/environments/e/suspend Bearer test-"]
        other = own.model_copy(
            update={"tls": ServerTLS(certificate=tmp_path / "ca.pem", private_key=tmp_path / "x")}
        )
        with pytest.raises(RecordError, match="another certificate"):
            checkpoint_jobs.local_suspend(other, "e")
        assert len(seen) == 1  # nothing, not even the token, went to the impostor
    finally:
        server.shutdown()
        server.server_close()


# Review findings (2026-10-28): each with the reproduction it came with


def second_upload(tmp_path: Path, settings: Settings) -> tuple[CheckpointPlan, str]:
    """A second checkpoint of the same workspace, verified in the upload bucket only."""
    plan = planned(settings)
    root = workspace(tmp_path / "two")
    (root / "research" / "notes").write_text("result of two")
    identity, _, _ = capture_upload(root, plan.source, UPLOAD, plan.capture)
    checkpoint_jobs.verify(settings, plan.checkpoint_id, identity, UPLOAD, "upload", "op")
    return plan, identity


def source_of(settings: Settings, manifest: str) -> Any:
    found = checkpoint_jobs.inventory(settings).checkpoints
    checkpoint_id = next(c.checkpoint_id for c in found if c.manifest_sha256 == manifest)
    records = CheckpointRecords(settings.state_dir)
    try:
        return records.hold(checkpoint_id).plan.source
    finally:
        records.close()


def test_promotion_never_overwrites_a_shared_kept_chunk(tmp_path, garage, retained, stopped):
    """Finding 1: a chunk swapped in the upload bucket after verification must not replace
    the kept copy an older checkpoint uses."""
    first = checkpointed(tmp_path, retained, "one")[1]
    plan, identity = second_upload(tmp_path, retained)
    shared = sorted(garage.keys(KEPT.bucket, "chunks") & garage.keys(UPLOAD.bucket, "chunks"))
    assert shared  # the large file's chunks are the same in both
    key = (UPLOAD.bucket, UPLOAD.key("chunks", shared[0]))
    garage.objects[key] = b"x" * len(garage.objects[key])
    checkpoint_remote.promote(UPLOAD, KEPT, identity)
    assert checkpoint_remote.verify(KEPT, first, source_of(retained, first)).members > 1
    # The second's kept copy holds the exact shared chunk too, so it verifies as well.
    checkpoint_jobs.verify(retained, plan.checkpoint_id, identity, KEPT, "kept", "op")


def test_a_swapped_chunk_of_its_own_is_never_promoted(tmp_path, garage, retained, stopped):
    checkpointed(tmp_path, retained, "one")
    _, identity = second_upload(tmp_path, retained)
    own = sorted(garage.keys(UPLOAD.bucket, "chunks") - garage.keys(KEPT.bucket, "chunks"))
    key = (UPLOAD.bucket, UPLOAD.key("chunks", own[0]))
    garage.objects[key] = b"x" * len(garage.objects[key])
    before = dict(garage.objects)
    with pytest.raises(ValueError, match="differ"):
        checkpoint_remote.promote(UPLOAD, KEPT, identity)
    assert {k: v for k, v in garage.objects.items() if k[0] == KEPT.bucket} == {
        k: v for k, v in before.items() if k[0] == KEPT.bucket
    }


def test_the_copy_is_pinned_to_the_bytes_that_were_hashed(tmp_path, garage, retained, stopped):
    """A swap between the hashed read and the copy fails the copy's ETag precondition."""
    _, identity = second_upload(tmp_path, retained)

    def swap(key: str) -> None:
        garage.objects[(UPLOAD.bucket, key)] = b"y" * len(garage.objects[(UPLOAD.bucket, key)])

    garage.before_copy = swap
    with pytest.raises(OSError, match="request failed"):
        checkpoint_remote.promote(UPLOAD, KEPT, identity)
    assert not garage.keys(KEPT.bucket, "chunks")


def test_a_deletion_resumes_after_its_manifest_is_gone(tmp_path, garage, retained, stopped):
    """Finding 2: the worker's recorded chunk list finishes a deletion whose manifest an
    earlier run already removed; the manifest itself goes last."""
    first = checkpointed(tmp_path, retained, "one")[1]
    records = CheckpointRecords(retained.state_dir)
    try:
        names = records.chunk_names(first)
    finally:
        records.close()
    assert len(names) > 3
    garage.fail_delete = {names[-1]}
    with pytest.raises(OSError, match="request failed"):
        checkpoint_remote.delete(KEPT, first, chunk_names=names)
    assert first in garage.keys(KEPT.bucket, "manifests")  # still there: deleted last
    garage.objects.pop((KEPT.bucket, KEPT.key("manifests", first)))  # as an older run left it
    garage.fail_delete = set()
    checkpoint_remote.delete(KEPT, first, chunk_names=names)
    assert not garage.keys(KEPT.bucket, "chunks")
    assert checkpoint_remote.delete(KEPT, first, chunk_names=names) == len(set(names))  # again


def test_the_delete_command_reads_the_recorded_list(tmp_path, garage, retained, stopped, capsys):
    first = checkpointed(tmp_path, retained, "one")[1]
    settings = ["--settings", str(tmp_path / "worker.toml")]
    (tmp_path / "worker.toml").write_text(
        f'token_file = "{retained.token_file}"\nstate_dir = "{retained.state_dir}"\n'
        "[openshell]\nisolation_tier = 3\n"
    )
    assert main(["checkpoint", "chunks", "--manifest", first, *settings]) == 0
    listing = tmp_path / "chunks.txt"
    listing.write_text(capsys.readouterr().out)
    garage.objects.pop((KEPT.bucket, KEPT.key("manifests", first)))
    storage = tmp_path / "kept.toml"
    storage.write_text(
        "\n".join(f'{key} = "{value}"' for key, value in KEPT.model_dump().items()) + "\n"
    )
    storage.chmod(0o600)
    command = ["checkpoint", "delete-objects", "--storage", str(storage), "--manifest", first]
    assert main([*command, "--chunks-file", str(listing)]) == 0
    assert not garage.keys(KEPT.bucket, "chunks")
    listing.write_text("not-a-digest\n")
    assert main([*command, "--chunks-file", str(listing)]) == 1


def test_a_capture_after_an_early_release_is_not_recorded(tmp_path, garage, retained, stopped):
    """Finding 4: once the hold was lifted the sandbox may have run again."""
    plan = planned(retained)
    records = CheckpointRecords(retained.state_dir)
    records.release(plan.checkpoint_id, datetime.now(UTC), "operator")
    records.close()
    later = FakeClock(datetime.now(UTC) + timedelta(minutes=1))
    identity, _, _ = capture_upload(
        workspace(tmp_path / "v"), plan.source, UPLOAD, plan.capture, clock=later
    )
    with pytest.raises(RecordError, match="while its hold held"):
        checkpoint_jobs.verify(retained, plan.checkpoint_id, identity, UPLOAD, "upload", "op")


def test_a_start_and_a_plan_cannot_interleave(retained, stopped):
    """Finding 8: a start records itself in the transaction that finds no hold, and a plan
    refuses while a start is under way; a start cut short long ago no longer blocks."""
    from sandbox_science.worker.checkpoint_records import HeldError, begin_start, end_start

    now = datetime.now(UTC)
    begin_start(retained.state_dir, "e", now)
    with pytest.raises(RecordError, match="starting this sandbox"):
        planned(retained)
    end_start(retained.state_dir, "e", now)
    plan = planned(retained)
    with pytest.raises(HeldError):
        begin_start(retained.state_dir, "e", datetime.now(UTC))
    records = CheckpointRecords(retained.state_dir)
    records.release(plan.checkpoint_id, datetime.now(UTC), "test")
    records.close()
    begin_start(retained.state_dir, "e", now - timedelta(hours=1))  # never ended
    assert planned(retained, clock=FakeClock(now)).source.environment_id == "e"


def test_the_hold_check_fails_closed(retained):
    """Finding 8: only a missing table means no hold; any other error refuses."""
    from sandbox_science.worker.checkpoint_records import begin_start

    path = retained.state_dir / "checkpoints.sqlite3"
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE unrelated (x)")  # a file whose tables do not exist yet
    db.commit()
    db.close()
    assert held_until(retained.state_dir, "e", datetime.now(UTC)) is None
    path.write_bytes(b"not a database, " * 64)
    with pytest.raises(sqlite3.DatabaseError):
        held_until(retained.state_dir, "e", datetime.now(UTC))
    with pytest.raises(sqlite3.DatabaseError):
        begin_start(retained.state_dir, "e", datetime.now(UTC))


def test_a_start_after_the_newest_checkpoint_needs_the_flag(tmp_path, garage, retained, stopped):
    """Finding 6: a checkpoint older than the workspace's last start does not cover it."""
    from sandbox_science.worker.checkpoint_records import begin_start, end_start

    checkpointed(tmp_path, retained, "one")
    save_environment(retained, "e", "ss-checkpoint-test", EnvironmentState.DELETED)
    found = checkpoint_jobs.inventory(retained)
    assert checkpoint_jobs.checkpoint_cover(found, "ss-checkpoint-test") is None
    later = datetime.now(UTC) + timedelta(minutes=10)
    begin_start(retained.state_dir, "e", later)
    end_start(retained.state_dir, "e", later)
    found = checkpoint_jobs.inventory(retained)
    assert found.environments[0].last_started_at == later
    reasons = checkpoint_jobs.volume_refusals(found, "ss-checkpoint-test", without_checkpoint=False)
    assert any("after its newest verified checkpoint" in reason for reason in reasons)
    assert not checkpoint_jobs.volume_refusals(found, "ss-checkpoint-test", without_checkpoint=True)


def test_a_kept_chunk_that_cannot_be_read_is_never_overwritten(tmp_path, garage, retained, stopped):
    """Only a missing object or one whose bytes differ counts as absent: a transient error
    reading a good kept chunk stops the promotion, retryably, and copies nothing over it."""
    first = checkpointed(tmp_path, retained, "one")[1]
    _, identity = second_upload(tmp_path, retained)
    shared = sorted(garage.keys(KEPT.bucket, "chunks") & garage.keys(UPLOAD.bucket, "chunks"))
    garage.unavailable = {shared[0]}
    seen = len(garage.calls)
    with pytest.raises(OSError, match="request failed"):
        checkpoint_remote.promote(UPLOAD, KEPT, identity)
    assert not any(
        call[0] == "copy" and call[2].endswith(shared[0]) for call in garage.calls[seen:]
    )
    garage.unavailable = set()
    checkpoint_remote.promote(UPLOAD, KEPT, identity)  # the retry finishes
    assert checkpoint_remote.verify(KEPT, first, source_of(retained, first)).members > 1
