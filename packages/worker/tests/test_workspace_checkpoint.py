# SPDX-License-Identifier: MIT
"""Offline complete checkpoint provenance, safe extraction and failure visibility."""

from __future__ import annotations

import io
import json
import os
import sqlite3
import stat
import struct
import tracemalloc
from datetime import UTC, datetime
from pathlib import Path
from typing import BinaryIO, cast

import pytest

from sandbox_science.contracts import EnvironmentInfo, EnvironmentState
from sandbox_science.contracts.workspace_checkpoint import (
    MAX_CHECKPOINT_HEADER,
    CheckpointSource,
)
from sandbox_science.worker import checkpoint_stream
from sandbox_science.worker.checkpoint_operator import operate, receipt, retained_source, verify
from sandbox_science.worker.checkpoint_stream import MAGIC, decode, encode
from sandbox_science.worker.cli import main
from sandbox_science.worker.environments import EnvironmentRecords, OpenShellCli
from sandbox_science.worker.settings import OpenShellBackendSettings, Settings, SettingsError
from sandbox_science.worker.state_lock import StateLock
from sandbox_science.worker.store import RevisionRecord, Store

IDENTITY = "prefix-" + "a" * 32
FINGERPRINT = "b" * 32
LAUNCH = json.dumps({"kubernetes": {"pod": {"node_selector": {"kubernetes.io/arch": "arm64"}}}})


def source() -> CheckpointSource:
    return CheckpointSource(
        project_id="p",
        environment_id="e",
        sandbox="ss-checkpoint-test",
        sandbox_id="sandbox-original",
        profile="crypto-base",
        image="example.invalid/research@sha256:" + "1" * 64,
        policy="sha256:" + "2" * 64,
        launch_settings=LAUNCH,
        architecture="arm64",
        require_workspace_prefix=True,
        prefix_identity=IDENTITY,
        package_fingerprint=FINGERPRINT,
    )


def tree(root: Path) -> None:
    root.mkdir()
    (root / ".openshell-initialized").write_bytes(b"initialized\n")
    prefix = root / ".sandbox-science-prefix"
    prefix.mkdir()
    (prefix / ".sandbox-science-prefix-id").write_text(IDENTITY)
    (prefix / "bin").mkdir()
    (prefix / "bin" / "tool").write_bytes(b"native package bytes")
    (prefix / "bin" / "tool").chmod(0o751)
    # A frozen tree's link, as crypto-base's prefix has (images/common/prefix_layout.py):
    # stored as a link, never followed into the image's read-only copy.
    (prefix / "share").mkdir()
    (prefix / "share" / "gap").symlink_to("/opt/sandbox-science-frozen/share/gap")
    (root / "research").mkdir()
    (root / "research" / "result").write_text("retained result")
    os.link(root / "research" / "result", root / "hardlink")
    (root / "absolute").symlink_to("/do-not-follow-this-checkpoint-link")
    (root / "relative").symlink_to("research/result")
    (root / "empty").mkdir(mode=0o750)
    root.chmod(0o751)


@pytest.fixture
def retained(tmp_path, monkeypatch):
    settings = Settings(
        token_file=tmp_path / "unused-token",
        state_dir=tmp_path / "state",
        openshell=OpenShellBackendSettings(isolation_tier=3),
    )
    settings.state_dir.mkdir()
    info = EnvironmentInfo(
        environment_id="e",
        project_id="p",
        profile="crypto-base",
        image=source().image,
        sandbox=source().sandbox,
        sandbox_id=source().sandbox_id,
        policy=source().policy,
        state=EnvironmentState.STOPPED,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    records = EnvironmentRecords(settings.state_dir / "environments.sqlite3")
    records.save(info, driver_config=LAUNCH, require_workspace_prefix=True)
    records.close()
    store = Store(settings.state_dir / "worker.sqlite3")
    store.remember_workspace_prefix("e", IDENTITY, "inc-" + "c" * 24)
    store.save_revision("e", RevisionRecord(3, FINGERPRINT, "original-attempt"))
    store.set_package_status("e", "needs_recovery", "interrupted-installer", 3)
    store.close()
    observations = []

    async def stopped(self, name):
        observations.append(name)
        return {"id": "sandbox-original", "phase": "Stopped"}

    monkeypatch.setattr(OpenShellCli, "sandbox", stopped)
    return settings, observations


def encoded(root: Path) -> bytes:
    stream = io.BytesIO()
    encode(root, source(), stream)
    return stream.getvalue()


def framed(members: list[dict[str, object]]) -> bytes:
    def frame(data):
        return struct.pack(">I", len(data)) + data

    return (
        MAGIC
        + frame(source().model_dump_json().encode())
        + b"".join(
            frame(json.dumps(member).encode()) + b"\0" * int(str(member.get("size", 0)))
            for member in members
        )
        + frame(b"")
    )


def directory(path: str = ".") -> dict[str, object]:
    return {"path": path, "kind": "directory", "mode": 0o751}


async def test_complete_roundtrip_keeps_prefix_links_modes_and_all_recovery_holds(
    tmp_path, retained
):
    settings, observations = retained
    root, checkpoint, restored = (
        tmp_path / "sandbox",
        tmp_path / "checkpoint",
        tmp_path / "restored",
    )
    tree(root)
    expected = await operate(settings, "create", "e", "arm64", checkpoint, root, True)
    assert observations == ["ss-checkpoint-test"] * 2
    assert verify(checkpoint) == expected
    assert await operate(settings, "verify", "e", "arm64", checkpoint, None, False) == expected
    assert await operate(settings, "restore", "e", "arm64", checkpoint, restored, True) == expected
    assert observations == ["ss-checkpoint-test"] * 4
    assert (restored / ".openshell-initialized").read_bytes() == b"initialized\n"
    assert (restored / ".sandbox-science-prefix/bin/tool").read_bytes() == b"native package bytes"
    assert stat.S_IMODE((restored / ".sandbox-science-prefix/bin/tool").stat().st_mode) == 0o751
    assert stat.S_IMODE((restored / "empty").stat().st_mode) == 0o750
    assert stat.S_IMODE(restored.stat().st_mode) == 0o751
    assert os.readlink(restored / "absolute") == "/do-not-follow-this-checkpoint-link"
    frozen = restored / ".sandbox-science-prefix/share/gap"
    assert os.readlink(frozen) == "/opt/sandbox-science-frozen/share/gap"
    assert os.readlink(restored / "relative") == "research/result"
    assert (restored / "hardlink").stat().st_ino == (restored / "research/result").stat().st_ino
    assert (root / "research/result").read_text() == "retained result"
    store = Store(settings.state_dir / "worker.sqlite3")
    try:
        assert store.package_status("e") == ("needs_recovery", "interrupted-installer", 3)
        assert store.revision("e") == RevisionRecord(3, FINGERPRINT, "original-attempt")
        assert store.workspace_prefix("e") == (IDENTITY, "inc-" + "c" * 24)
    finally:
        store.close()
    assert receipt(restored.with_name("restored.checkpoint-restore")) == expected


@pytest.mark.parametrize(
    ("phase", "identity"),
    [("Ready", "sandbox-original"), ("Stopped", "replacement"), ("Unknown", "sandbox-original")],
)
async def test_create_refuses_unconfirmed_or_replaced_original_before_writing(
    tmp_path, retained, monkeypatch, phase, identity
):
    settings, _ = retained
    root = tmp_path / "sandbox"
    tree(root)

    async def observe(self, name):
        return {"id": identity, "phase": phase}

    monkeypatch.setattr(OpenShellCli, "sandbox", observe)
    with pytest.raises(ValueError, match="not confirmed Stopped"):
        await operate(settings, "create", "e", "arm64", tmp_path / "out", root, True)
    assert not (tmp_path / "out").exists()


async def test_post_observation_failure_preserves_incomplete_bytes_without_receipt(
    tmp_path, retained, monkeypatch
):
    settings, _ = retained
    root, checkpoint = tmp_path / "sandbox", tmp_path / "checkpoint"
    tree(root)
    calls = 0

    async def observe(self, name):
        nonlocal calls
        calls += 1
        return {"id": "sandbox-original", "phase": "Stopped" if calls == 1 else "Ready"}

    monkeypatch.setattr(OpenShellCli, "sandbox", observe)
    with pytest.raises(ValueError, match="not confirmed Stopped"):
        await operate(settings, "create", "e", "arm64", checkpoint, root, True)
    assert (checkpoint / "archive.bin").stat().st_size > 0
    assert (checkpoint / "intent.json").is_file()
    assert (checkpoint / "failure.json").is_file()
    assert not (checkpoint / "receipt.json").exists()
    with pytest.raises(FileNotFoundError):
        verify(checkpoint)


async def test_explicit_exclusivity_lease_and_architecture_are_required(tmp_path, retained):
    settings, _ = retained
    root = tmp_path / "sandbox"
    tree(root)
    with pytest.raises(ValueError, match="exclusive-offline-mount"):
        await operate(settings, "create", "e", "arm64", tmp_path / "out", root, False)
    with StateLock(settings.state_dir), pytest.raises(SettingsError, match="another worker"):
        await operate(settings, "create", "e", "arm64", tmp_path / "out", root, True)
    with pytest.raises(ValueError, match="architecture differs"):
        await operate(settings, "create", "e", "amd64", tmp_path / "out", root, True)


@pytest.mark.parametrize(
    "change", ["missing-sentinel", "wrong-prefix", "symlink-prefix", "unreadable-file"]
)
async def test_incomplete_tree_never_commits_a_receipt(tmp_path, retained, change):
    settings, _ = retained
    root, checkpoint = tmp_path / "sandbox", tmp_path / "checkpoint"
    tree(root)
    if change == "missing-sentinel":
        (root / ".openshell-initialized").unlink()
    elif change == "wrong-prefix":
        (root / ".sandbox-science-prefix/.sandbox-science-prefix-id").write_text(
            "prefix-" + "f" * 32
        )
    elif change == "symlink-prefix":
        (root / ".sandbox-science-prefix").rename(root / "elsewhere")
        (root / ".sandbox-science-prefix").symlink_to("elsewhere")
    else:
        (root / "research" / "secret").write_text("the sandbox made this unreadable")
        (root / "research" / "secret").chmod(0)
    with pytest.raises((ValueError, OSError)) as raised:
        await operate(settings, "create", "e", "arm64", checkpoint, root, True)
    if change == "unreadable-file":
        assert "research/secret" in str(raised.value)
    assert not (checkpoint / "receipt.json").exists()
    assert (checkpoint / "failure.json").is_file()


async def test_special_files_are_skipped_and_listed_not_fatal(tmp_path, retained):
    """C4: a socket or FIFO in the workspace is left out and named in the receipt, which
    says how the copy was made; the rest restores intact."""
    import socket

    settings, _ = retained
    root, checkpoint, target = tmp_path / "sandbox", tmp_path / "checkpoint", tmp_path / "target"
    tree(root)
    os.mkfifo(root / "research" / "pipe")
    listener = socket.socket(socket.AF_UNIX)
    # A short relative name: socket paths are limited to about 100 bytes.
    cwd = Path.cwd()
    os.chdir(root)
    try:
        listener.bind("kernel.sock")
    finally:
        os.chdir(cwd)
    try:
        expected = await operate(settings, "create", "e", "arm64", checkpoint, root, True)
    finally:
        listener.close()
    assert expected.format == "sandbox-workspace-checkpoint-v2"
    assert expected.capture == "offline_mount"
    assert expected.exclusive_offline_mount_declared is None
    assert {(entry.path, entry.kind) for entry in expected.skipped} == {
        ("research/pipe", "fifo"),
        ("kernel.sock", "socket"),
    }
    assert verify(checkpoint) == expected
    await operate(settings, "restore", "e", "arm64", checkpoint, target, True)
    assert (target / "research" / "result").read_text() == "retained result"
    assert not (target / "research" / "pipe").exists()
    assert not (target / "kernel.sock").exists()


def test_special_files_still_fail_a_strict_encoding(tmp_path):
    root = tmp_path / "sandbox"
    tree(root)
    os.mkfifo(root / "pipe")
    with pytest.raises(ValueError, match="special file"):
        encode(root, source(), io.BytesIO())


async def test_checkpoint_and_restore_never_overwrite_existing_outputs(tmp_path, retained):
    settings, _ = retained
    root, checkpoint, target = tmp_path / "sandbox", tmp_path / "checkpoint", tmp_path / "target"
    tree(root)
    expected = await operate(settings, "create", "e", "arm64", checkpoint, root, True)
    with pytest.raises(FileExistsError):
        await operate(settings, "create", "e", "arm64", checkpoint, root, True)
    assert verify(checkpoint) == expected
    target.mkdir()
    (target / "important").write_text("preserve")
    with pytest.raises(FileExistsError, match="new destination"):
        await operate(settings, "restore", "e", "arm64", checkpoint, target, True)
    assert (target / "important").read_text() == "preserve"
    with pytest.raises(ValueError, match="outside the source"):
        await operate(settings, "create", "e", "arm64", root / "recursive", root, True)


@pytest.mark.parametrize(
    "bad",
    [
        {"path": "../escape", "kind": "file", "mode": 0o600},
        {"path": "/absolute", "kind": "file", "mode": 0o600},
        {"path": "a/../escape", "kind": "file", "mode": 0o600},
        {"path": "missing/child", "kind": "file", "mode": 0o600},
        {"path": "hard", "kind": "hardlink", "mode": 0o600, "target": "../outside"},
        {"path": "pipe", "kind": "fifo", "mode": 0o600},
        {"path": "link", "kind": "symlink", "mode": 0o600, "target": "outside"},
        {"path": "link", "kind": "symlink", "mode": 0o777, "target": "x\0y"},
        {"path": "file", "kind": "file", "mode": 0o600, "target": "unexpected"},
        {"path": "file", "kind": "file", "mode": 0o600, "executable_metadata": True},
        {"path": "directory", "kind": "directory", "mode": 0o700, "size": 1},
    ],
)
def test_archive_metadata_cannot_select_unsafe_extraction(tmp_path, bad):
    target = tmp_path / "target"
    target.mkdir()
    with pytest.raises(ValueError, match=r"checkpoint|CheckpointMember"):
        decode(io.BytesIO(framed([directory(), bad])), source(), target)
    assert not (tmp_path / "escape").exists()


def test_symlink_parent_duplicate_and_mismatched_hardlink_are_rejected(tmp_path):
    root = directory()
    symlink = {"path": "link", "kind": "symlink", "mode": 0o777, "target": "/tmp"}
    file = {"path": "file", "kind": "file", "mode": 0o600}
    for members in (
        [root, root],
        [root, file, file],
        [root, symlink, {**file, "path": "link/escape"}],
        [root, file, {"path": "hard", "kind": "hardlink", "mode": 0o777, "target": "file"}],
    ):
        with pytest.raises(ValueError, match="checkpoint"):
            decode(io.BytesIO(framed(members)), source())


def test_truncation_extra_bytes_corrupt_provenance_and_header_bounds(tmp_path):
    root = tmp_path / "sandbox"
    tree(root)
    data = encoded(root)
    for damaged in (
        data[:-1],
        data + b"extra",
        data.replace(b"sandbox-original", b"sandbox-replaced"),
    ):
        with pytest.raises(ValueError, match=r"checkpoint|CheckpointSource"):
            decode(io.BytesIO(damaged), source())
    source_data = source().model_dump_json().encode()
    too_large = (
        MAGIC
        + struct.pack(">I", len(source_data))
        + source_data
        + struct.pack(">I", MAX_CHECKPOINT_HEADER + 1)
    )
    with pytest.raises(ValueError, match="header exceeds"):
        decode(io.BytesIO(too_large), source())


@pytest.mark.parametrize(
    ("limit", "value"),
    [("MAX_CHECKPOINT_MEMBERS", 1), ("MAX_CHECKPOINT_METADATA", 1), ("MAX_CHECKPOINT_CONTENT", 0)],
)
def test_explicit_whole_archive_bounds(tmp_path, monkeypatch, limit, value):
    root = tmp_path / "sandbox"
    tree(root)
    monkeypatch.setattr(checkpoint_stream, limit, value)
    with pytest.raises(ValueError, match="bound"):
        encoded(root)


def test_encoder_detects_source_mutation_during_content_read(tmp_path):
    root = tmp_path / "sandbox"
    tree(root)
    victim = root / "research/result"

    class MutatingWriter(io.BytesIO):
        changed = False

        def write(self, data):
            if data == b"retained result" and not self.changed:
                self.changed = True
                victim.write_text("changed behind the capture")
            return super().write(data)

    with pytest.raises(ValueError, match="source file changed"):
        encode(root, source(), MutatingWriter())


def test_encoder_postscan_detects_an_already_read_file_changed_by_a_later_member(tmp_path):
    root = tmp_path / "sandbox"
    tree(root)
    originals = {
        b"native package bytes": root / ".sandbox-science-prefix/bin/tool",
        b"retained result": root / "research/result",
        b"initialized\n": root / ".openshell-initialized",
        IDENTITY.encode(): root / ".sandbox-science-prefix/.sandbox-science-prefix-id",
    }

    class LaterMutation(io.BytesIO):
        victim: tuple[Path, bytes] | None = None

        def write(self, data):
            if data in originals:
                if self.victim is None:
                    self.victim = originals[data], data
                else:
                    self.victim[0].write_bytes(self.victim[1][::-1])
            return super().write(data)

    with pytest.raises(ValueError, match="source file changed after capture"):
        encode(root, source(), LaterMutation())


async def test_receipt_hash_and_current_binding_are_required_for_restore(tmp_path, retained):
    settings, _ = retained
    root, checkpoint = tmp_path / "sandbox", tmp_path / "checkpoint"
    tree(root)
    await operate(settings, "create", "e", "arm64", checkpoint, root, True)
    with (checkpoint / "archive.bin").open("r+b") as handle:
        offset = handle.read().index(b"native package bytes")
        handle.seek(offset)
        handle.write(b"X")
    with pytest.raises(ValueError, match="differs"):
        await operate(settings, "restore", "e", "arm64", checkpoint, tmp_path / "restore", True)
    assert not (tmp_path / "restore").exists()
    assert (tmp_path / "restore.checkpoint-restore/failure.json").is_file()


def test_production_cli_reports_local_only_and_does_not_read_tokens(tmp_path, retained, capsys):
    settings, _ = retained
    root = tmp_path / "sandbox"
    tree(root)
    config = tmp_path / "worker.toml"
    config.write_text(
        f'state_dir = "{settings.state_dir}"\ntoken_file = "does-not-exist"\n'
        "[openshell]\nisolation_tier = 3\n"
    )
    arguments = [
        "workspace-checkpoint",
        "create",
        "--settings",
        str(config),
        "--environment",
        "e",
        "--architecture",
        "arm64",
        "--checkpoint",
        str(tmp_path / "checkpoint"),
        "--workspace",
        str(root),
        "--exclusive-offline-mount",
    ]
    assert main(arguments) == 0
    assert "remote_retention=not_established recovery_set=incomplete" in capsys.readouterr().out


def test_large_file_stream_buffers_are_bounded(tmp_path):
    root = tmp_path / "sandbox"
    tree(root)
    with (root / "large").open("wb") as handle:
        handle.truncate(32 * 1024**2)
    with (tmp_path / "archive").open("w+b") as handle:
        tracemalloc.start()
        try:
            summary = encode(root, source(), handle)
            handle.seek(0)
            assert decode(handle, source()) == summary
            assert tracemalloc.get_traced_memory()[1] < 4 * 1024**2
        finally:
            tracemalloc.stop()


def test_content_larger_than_artifact_limit_streams_without_whole_file_buffers(tmp_path):
    root = tmp_path / "sandbox"
    tree(root)
    amount = 1024**3 + 8192
    with (root / "large").open("wb") as handle:
        handle.truncate(amount)

    class Sink:
        def write(self, data: bytes) -> int:
            return len(data)

    tracemalloc.start()
    try:
        summary = encode(root, source(), cast(BinaryIO, Sink()))
        assert summary.content_bytes > amount
        assert summary.size > 1024**3
        assert tracemalloc.get_traced_memory()[1] < 4 * 1024**2
    finally:
        tracemalloc.stop()


async def test_restore_postcheck_never_exposes_a_completed_directory(
    tmp_path, retained, monkeypatch
):
    settings, _ = retained
    root, checkpoint, target = tmp_path / "sandbox", tmp_path / "checkpoint", tmp_path / "target"
    tree(root)
    await operate(settings, "create", "e", "arm64", checkpoint, root, True)
    calls = 0

    async def observe(self, name):
        nonlocal calls
        calls += 1
        return {"id": "sandbox-original", "phase": "Stopped" if calls == 1 else "Ready"}

    monkeypatch.setattr(OpenShellCli, "sandbox", observe)
    with pytest.raises(ValueError, match="not confirmed Stopped"):
        await operate(settings, "restore", "e", "arm64", checkpoint, target, True)
    journal = tmp_path / "target.checkpoint-restore"
    assert not target.exists()
    assert (journal / "tree/research/result").read_text() == "retained result"
    assert (journal / "failure.json").is_file()
    assert not (journal / "receipt.json").exists()


async def test_restore_publication_does_not_replace_a_racing_target(
    tmp_path, retained, monkeypatch
):
    settings, _ = retained
    root, checkpoint, target = tmp_path / "sandbox", tmp_path / "checkpoint", tmp_path / "target"
    tree(root)
    await operate(settings, "create", "e", "arm64", checkpoint, root, True)
    calls = 0

    async def observe(self, name):
        nonlocal calls
        calls += 1
        if calls == 2:
            target.mkdir()
        return {"id": "sandbox-original", "phase": "Stopped"}

    monkeypatch.setattr(OpenShellCli, "sandbox", observe)
    with pytest.raises(FileExistsError, match="already exists"):
        await operate(settings, "restore", "e", "arm64", checkpoint, target, True)
    assert list(target.iterdir()) == []
    assert (tmp_path / "target.checkpoint-restore/tree").is_dir()
    assert not (tmp_path / "target.checkpoint-restore/receipt.json").exists()


def test_duplicate_json_fields_are_not_silently_reinterpreted():
    frame = b'{"path":".","path":"escape","kind":"directory","mode":448}'
    prefix = (
        MAGIC
        + struct.pack(">I", len(source().model_dump_json().encode()))
        + source().model_dump_json().encode()
    )
    raw = prefix + struct.pack(">I", len(frame)) + frame + struct.pack(">I", 0)
    with pytest.raises(ValueError, match="repeated JSON key"):
        decode(io.BytesIO(raw), source())


def test_retained_source_does_not_rewrite_unknown_recovery_state(retained):
    settings, _ = retained
    assert retained_source(settings, "e", "arm64") == source()
    with sqlite3.connect(settings.state_dir / "worker.sqlite3") as db:
        assert db.execute(
            "SELECT status,provision_id,revision FROM package_status WHERE environment_id='e'"
        ).fetchone() == ("needs_recovery", "interrupted-installer", 3)


async def test_receipt_serialization_bound_is_checked_before_commit(tmp_path, retained):
    settings, _ = retained
    root, checkpoint = tmp_path / "sandbox", tmp_path / "checkpoint"
    tree(root)
    launch = json.dumps(
        {
            "kubernetes": {"pod": {"node_selector": {"kubernetes.io/arch": "arm64"}}},
            "annotation": "\\" * 32550,
        }
    )
    with sqlite3.connect(settings.state_dir / "environments.sqlite3") as db:
        db.execute("UPDATE sandbox_launch_settings SET driver_config=?", (launch,))
    with pytest.raises(ValueError, match="receipt exceeds its metadata bound"):
        await operate(settings, "create", "e", "arm64", checkpoint, root, True)
    assert not (checkpoint / "receipt.json").exists()
    assert (checkpoint / "failure.json").is_file()
