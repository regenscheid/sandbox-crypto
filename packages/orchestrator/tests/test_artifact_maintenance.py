# SPDX-License-Identifier: MIT
"""Bounded cache policy passes, resumable cursors and no unverified deletion."""

from __future__ import annotations

import json
import os
from contextlib import closing

import pytest
from pydantic import ValidationError

from sandbox_science.orchestrator import artifact_s3
from sandbox_science.orchestrator.artifact_blobs import Blob
from sandbox_science.orchestrator.artifact_config import install as configure
from sandbox_science.orchestrator.artifact_maintenance import (
    MaintenancePolicy,
    load_policy,
    maintain,
)
from sandbox_science.orchestrator.artifact_references import reference_page
from sandbox_science.orchestrator.artifacts import ArtifactInventory
from sandbox_science.orchestrator.cli_artifacts import main
from sandbox_science.orchestrator.documents import DocumentStore
from sandbox_science.orchestrator.maintenance_cursor import progress, save, scope
from sandbox_science.testing import FakeClock

from .test_artifact_blobs import install
from .test_artifact_inventory import record
from .test_artifact_s3 import SETTINGS, FakeS3, SDKError, write


@pytest.fixture
def state(tmp_path, monkeypatch):
    remote = FakeS3()
    monkeypatch.setattr(artifact_s3, "connect", lambda *_: (remote, (SDKError,)))
    with closing(DocumentStore(tmp_path / "control.sqlite3")) as documents:
        inventory = ArtifactInventory(documents)
        configure(inventory.blobs.root, SETTINGS)
        yield inventory, remote


def retained(inventory, data, operation):
    found = install(inventory, data, operation).revision
    assert found is not None
    blob = Blob(found.sha256, found.size)
    os.utime(inventory.blobs.path(blob.sha256), (0, 0))
    return blob


def policy(**values):
    return MaintenancePolicy(min_age_seconds=0, **values)


def test_preview_and_apply_preserve_pinned_reads_metadata_orphans_and_transfers(state):
    inventory, remote = state
    blob = retained(inventory, b"durable", "one")
    inline = record(inventory, "inline", b"old inline").revision
    assert inline is not None
    orphan = write(inventory.blobs, b"unreferenced")
    staging = inventory.blobs.root / "staging" / "unsettled"
    staging.write_bytes(b"keep")
    held = inventory.reserve_transfer("p1", 1, "a" * 64)
    database = inventory.documents.path
    with inventory.documents.control.read() as tx:
        before = tx.sql.execute("SELECT * FROM artifact_usage ORDER BY project").fetchall()
    preview = maintain(database, policy())
    assert [entry.state for entry in preview.entries] == ["would_evict"]
    assert inventory.blobs.path(blob.sha256).exists()
    with inventory.blobs.open(blob) as pinned:
        result = maintain(database, policy(), apply=True)
        assert result.evicted_bytes == blob.size
        assert pinned.read() == b"durable"
    assert not inventory.blobs.path(blob.sha256).exists()
    assert inventory.blobs.path(orphan.sha256).read_bytes() == b"unreferenced"
    assert staging.read_bytes() == b"keep"
    assert inventory.content("p1", inline) == b"old inline"
    assert remote.objects[SETTINGS.key(blob.sha256)] == b"durable"
    with inventory.documents.control.read() as tx:
        assert tx.sql.execute("SELECT * FROM artifact_usage ORDER BY project").fetchall() == before
        assert tx.sql.execute("SELECT identity FROM artifact_transfers").fetchone() == (held,)


def test_absent_cache_is_not_downloaded_or_reported_as_verified_remote(state):
    inventory, remote = state
    blob = retained(inventory, b"absent", "one")
    inventory.blobs.path(blob.sha256).unlink()
    remote.objects.clear()
    remote.requests.clear()
    result = maintain(inventory.documents.path, policy(), apply=True)
    assert result.entries[0].state == "not_cached"
    assert remote.requests == []
    assert result.evicted_bytes == result.selected_bytes == 0


@pytest.mark.parametrize("failure", ["corrupt", "get_failure", "size_delta"])
def test_remote_failure_never_deletes_the_local_copy(state, failure):
    inventory, remote = state
    blob = retained(inventory, b"keep local", "one")
    setattr(remote, failure, True)
    result = maintain(inventory.documents.path, policy(), apply=True)
    assert result.failed == 1
    assert result.entries[0].state == "unavailable"
    assert inventory.blobs.path(blob.sha256).read_bytes() == b"keep local"


def test_archive_policy_only_writes_in_apply_and_recovers_historical_local_content(state):
    inventory, remote = state
    blob = retained(inventory, b"historical", "one")
    remote.objects.clear()
    selected = policy(archive_before_evict=True)
    assert maintain(inventory.documents.path, selected).failed == 1
    assert remote.objects == {}
    assert inventory.blobs.path(blob.sha256).exists()
    result = maintain(inventory.documents.path, selected, apply=True)
    assert result.failed == 0
    assert result.evicted_bytes == blob.size
    assert remote.objects[SETTINGS.key(blob.sha256)] == b"historical"


def test_age_uses_injected_clock_and_future_times_stay_local(state):
    inventory, remote = state
    clock = FakeClock()
    blob = retained(inventory, b"fresh", "one")
    path = inventory.blobs.path(blob.sha256)
    now = clock.now().timestamp()
    os.utime(path, (now, now))
    selected = MaintenancePolicy(min_age_seconds=60)
    remote.requests.clear()
    assert (
        maintain(inventory.documents.path, selected, apply=True, clock=clock).entries[0].state
        == "young"
    )
    assert remote.requests == []
    clock.advance(60)
    assert maintain(inventory.documents.path, selected, apply=True, clock=clock).evicted_bytes == 5


def test_reference_and_byte_bounds_progress_past_large_objects(state):
    inventory, _ = state
    blobs = sorted(
        (retained(inventory, data, str(i)) for i, data in enumerate((b"123", b"4567", b"89"))),
        key=lambda blob: blob.sha256,
    )
    database = inventory.documents.path
    selected = policy(max_references=2, max_bytes=4)
    first = maintain(database, selected, apply=True)
    assert len(first.entries) == 2
    assert first.next_after == blobs[1].sha256
    assert first.selected_bytes <= 4
    last = maintain(database, selected, apply=True, after=first.next_after)
    assert len(last.entries) == 1
    assert last.entries[0].sha256 == blobs[2].sha256
    assert last.next_after == ""
    assert len(reference_page(database, limit=1)) == 1
    assert first.evicted_bytes + last.evicted_bytes <= 8


def test_cursor_cycles_back_to_failures_and_new_lower_hashes(state, tmp_path, capsys):
    inventory, remote = state
    blobs = sorted(
        [retained(inventory, b"one", "one"), retained(inventory, b"two", "two")],
        key=lambda blob: blob.sha256,
    )
    config = tmp_path / "maintenance.toml"
    config.write_text("max_references = 1\nmin_age_seconds = 0\n")
    cursor = tmp_path / "cursor.json"
    args = [
        "maintain",
        "--database",
        str(inventory.documents.path),
        "--policy",
        str(config),
        "--cursor-file",
        str(cursor),
        "--apply",
    ]
    remote.get_failure = True
    assert main(args) == 1
    first = json.loads(capsys.readouterr().out)
    assert first["next_after"] == blobs[0].sha256
    assert first["failed"] == 1
    assert cursor.stat().st_mode & 0o777 == 0o600
    remote.get_failure = False
    assert main(args) == 0
    assert json.loads(capsys.readouterr().out)["next_after"] == ""
    assert main(args) == 0
    assert json.loads(capsys.readouterr().out)["evicted_bytes"] == blobs[0].size
    assert all(not inventory.blobs.path(blob.sha256).exists() for blob in blobs)


def test_cursor_scope_and_live_owner_prevent_overlapping_or_misbound_passes(tmp_path):
    database, path = tmp_path / "db.sqlite3", tmp_path / "cursor.json"
    expected = scope(database, policy(), False)
    with progress(path, expected) as current:
        save(path, current, "a" * 64)
        with pytest.raises(BlockingIOError), progress(path, expected):
            pass
    with progress(path, expected) as current:
        assert current.after == "a" * 64
    for changed in (scope(database, policy(), True), scope(database, policy(max_bytes=1), False)):
        with pytest.raises(ValueError, match="another"), progress(path, changed):
            pass


@pytest.mark.parametrize("kind", ["symlink", "large", "nonprivate", "fifo"])
def test_cursor_rejects_invalid_progress_without_touching_other_files(tmp_path, kind):
    path = tmp_path / "cursor.json"
    other = tmp_path / "other"
    other.write_text("keep")
    if kind == "symlink":
        path.symlink_to(other)
    elif kind == "fifo":
        os.mkfifo(path, 0o600)
    else:
        path.write_text("x" * 1025 if kind == "large" else "{}")
        path.chmod(0o644 if kind == "nonprivate" else 0o600)
    with pytest.raises((OSError, ValueError)), progress(path, "a" * 64):
        pass
    assert other.read_text() == "keep"


@pytest.mark.parametrize(
    "content",
    [
        'max_references = "1"',
        "max_references = 0",
        "max_references = 101",
        "max_bytes = 0",
        "min_age_seconds = -1",
        'archive_before_evict = "true"',
        'endpoint = "https://test-secret-key.invalid"',
        "x" * 8193,
    ],
)
def test_policy_rejects_unknown_fields_coercion_and_unbounded_inputs(tmp_path, content):
    path = tmp_path / "policy.toml"
    path.write_text(content)
    with pytest.raises((ValueError, ValidationError)):
        load_policy(path)


def test_scheduling_preview_does_not_grant_apply_or_disclose_bad_configuration(
    state, tmp_path, capsys
):
    inventory, _ = state
    blob = retained(inventory, b"preview", "one")
    config = tmp_path / "policy.toml"
    config.write_text("min_age_seconds = 0\n")
    args = ["maintain", "--database", str(inventory.documents.path), "--policy", str(config)]
    assert main(args) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["apply"] is False
    assert result["entries"][0]["state"] == "would_evict"
    assert inventory.blobs.path(blob.sha256).exists()
    config.write_text('test-secret-key = "never print"')
    assert main(args) == 1
    assert "test-secret" not in capsys.readouterr().err


def test_mismatched_cache_size_fails_before_any_eviction(state):
    inventory, _ = state
    blob = retained(inventory, b"one", "one")
    path = inventory.blobs.path(blob.sha256)
    path.chmod(0o600)
    path.write_bytes(b"changed local bytes")
    result = maintain(inventory.documents.path, policy(), apply=True)
    assert result.failed == 1
    assert path.read_bytes() == b"changed local bytes"
