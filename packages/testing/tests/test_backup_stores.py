# SPDX-License-Identifier: MIT
"""The backup and restore helper (docs/BACKUP-RESTORE.md): consistent copies of stores in
use, hashes checked, private files, the gateway's state never read, nothing deleted."""

from __future__ import annotations

import fcntl
import hashlib
import importlib.util
import os
import sqlite3
import stat
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[3]
NOW = datetime(2026, 10, 23, 12, 0, 0, tzinfo=UTC)


@pytest.fixture(scope="module")
def tool() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "backup_stores", ROOT / "scripts" / "backup_stores.py"
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["backup_stores"] = module
    spec.loader.exec_module(module)
    return module


def database(path: Path, rows: int, version: int = 7) -> sqlite3.Connection:
    """A WAL-mode store with a version, left open so its -wal file holds recent changes."""
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, isolation_level=None)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("CREATE TABLE IF NOT EXISTS t (n INTEGER)")
    for n in range(rows):
        db.execute("INSERT INTO t VALUES (?)", (n,))
    db.execute(f"PRAGMA user_version = {version}")
    return db


def blob(directory: Path, data: bytes, *, name: str | None = None) -> str:
    digest = name or hashlib.sha256(data).hexdigest()
    path = directory / "blobs" / digest[:2] / digest
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return digest


class Instance:
    """A state directory laid out as the launcher and the personal script make it."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.launcher = root / "launcher"
        self.middleware = root / "openshell" / "middleware" / "state"
        self.connections = [
            database(self.launcher / "control" / "control.sqlite3", 50, 78),
            database(self.launcher / "worker" / "state" / "worker.sqlite3", 5, 9),
            database(self.middleware / "events.db", 3, 1),
        ]
        control_blobs = self.launcher / "control" / "control.sqlite3.artifacts"
        self.control_blob = blob(control_blobs, b"artifact bytes")
        (control_blobs / "storage.toml").write_text('bucket = "b"\n')
        self.worker_blob = blob(self.launcher / "worker" / "state" / "artifacts", b"capture")
        (self.middleware / "policies").mkdir()
        (self.middleware / "policies" / "ab.yaml").write_text("kind: Policy\n")
        (self.middleware / "sandboxes").mkdir()
        (self.middleware / "sandboxes" / "sbx-1.json").write_text('{"policy": "sha256:ab"}\n')
        # What must never be copied.
        gateway = root / "openshell" / "state" / "openshell"
        gateway.mkdir(parents=True)
        (gateway / "gateway.db").write_text("providers and the key that encrypts them")
        (self.launcher / "tokens").mkdir()
        (self.launcher / "tokens" / "worker.token").write_text("secret")
        (root / "openshell" / "middleware" / "tls").mkdir()
        (root / "openshell" / "middleware" / "tls" / "ca-key.pem").write_text("key")
        (root / "openshell" / "cache").mkdir()
        (root / "openshell" / "cache" / "index.sqlite3").write_text("cache")

    def close(self) -> None:
        for connection in self.connections:
            connection.close()

    def make(self, tool: ModuleType, dest: Path, **extra: object) -> Path:
        return tool.backup(
            dest=dest,
            launcher_state=self.launcher,
            middleware_state=self.middleware,
            live=True,
            now=NOW,
            **extra,
        )


@pytest.fixture
def instance(tmp_path: Path):
    made = Instance(tmp_path / "instance")
    yield made
    made.close()


def modes(directory: Path) -> set[int]:
    found: set[int] = set()
    for current, names, files in os.walk(directory):
        found |= {stat.S_IMODE(os.stat(Path(current) / name).st_mode) for name in [*names, *files]}
    return found


def test_a_backup_of_stores_in_use_is_complete_checked_and_private(
    tool: ModuleType, instance: Instance, tmp_path: Path
) -> None:
    made = instance.make(tool, tmp_path / "backups", label="pre-upgrade")
    assert made.name == "20261023T120000Z-pre-upgrade"
    assert modes(made) <= {0o600, 0o700}
    manifest = tool.verify(made)
    assert manifest["live"] is False  # nothing held the instance's locks
    files = manifest["files"]
    assert files["control/control.sqlite3"]["user_version"] == 78
    assert files["worker/worker.sqlite3"]["user_version"] == 9
    assert f"control/artifacts/blobs/{instance.control_blob[:2]}/{instance.control_blob}" in files
    assert f"worker/artifacts/blobs/{instance.worker_blob[:2]}/{instance.worker_blob}" in files
    assert "middleware/policies/ab.yaml" in files
    assert "middleware/sandboxes/sbx-1.json" in files
    # The copy holds what was committed while the source's writer was still open.
    copy = sqlite3.connect(made / "control" / "control.sqlite3")
    assert copy.execute("SELECT count(*) FROM t").fetchone() == (50,)
    copy.close()
    # No -wal or -shm beside a copy, and no leftover temporary file.
    assert not [p for p in made.rglob("*") if p.name.endswith(("-wal", "-shm", ".part"))]


def test_gateway_tokens_keys_and_cache_are_never_copied(
    tool: ModuleType, instance: Instance, tmp_path: Path
) -> None:
    made = instance.make(tool, tmp_path / "backups")
    names = {p.name for p in made.rglob("*")}
    assert not names & {"gateway.db", "worker.token", "ca-key.pem", "index.sqlite3"}
    with pytest.raises(tool.BackupError, match="gateway"):
        tool.backup(
            dest=tmp_path / "b",
            launcher_state=instance.launcher,
            middleware_state=instance.root / "openshell" / "state" / "openshell",
            live=True,
        )
    with pytest.raises(tool.BackupError, match="gateway"):
        instance.make(tool, instance.root / "openshell" / "state" / "openshell" / "backups")


def test_a_running_instance_is_refused_unless_asked(
    tool: ModuleType, instance: Instance, tmp_path: Path
) -> None:
    lock = instance.launcher / "launcher.lock"
    with lock.open("w") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        with pytest.raises(tool.BackupError, match="running"):
            tool.backup(
                dest=tmp_path / "b", launcher_state=instance.launcher, middleware_state=None
            )
        made = tool.backup(
            dest=tmp_path / "b", launcher_state=instance.launcher, middleware_state=None, live=True
        )
        assert tool.read_manifest(made)["live"] is True
    # Released: a plain backup works, and says it was not live.
    plain = tool.backup(
        dest=tmp_path / "b2", launcher_state=instance.launcher, middleware_state=None, now=NOW
    )
    assert tool.read_manifest(plain)["live"] is False


def test_a_blob_that_is_not_its_name_stops_the_backup_and_leaves_no_backup(
    tool: ModuleType, instance: Instance, tmp_path: Path
) -> None:
    wrong = hashlib.sha256(b"something else").hexdigest()
    blob(instance.launcher / "control" / "control.sqlite3.artifacts", b"damaged", name=wrong)
    with pytest.raises(tool.BackupError, match="does not hold the content"):
        instance.make(tool, tmp_path / "backups")
    assert [p.name for p in (tmp_path / "backups").iterdir()] == ["20261023T120000Z.partial"]


def test_a_damaged_backup_is_found(tool: ModuleType, instance: Instance, tmp_path: Path) -> None:
    made = instance.make(tool, tmp_path / "backups")
    (made / "middleware" / "policies" / "ab.yaml").write_text("changed")
    with pytest.raises(tool.BackupError, match=r"policies/ab.yaml: content differs"):
        tool.verify(made)


def test_blobs_the_previous_backup_holds_are_linked_not_copied(
    tool: ModuleType, instance: Instance, tmp_path: Path
) -> None:
    first = instance.make(tool, tmp_path / "backups")
    second = tool.backup(
        dest=tmp_path / "backups",
        launcher_state=instance.launcher,
        middleware_state=instance.middleware,
        live=True,
        link_from=first,
        now=datetime(2026, 10, 24, tzinfo=UTC),
    )
    rel = f"control/artifacts/blobs/{instance.control_blob[:2]}/{instance.control_blob}"
    assert (first / rel).stat().st_ino == (second / rel).stat().st_ino
    tool.verify(second)


def test_restore_plans_first_then_replaces_without_deleting(
    tool: ModuleType, instance: Instance, tmp_path: Path
) -> None:
    made = instance.make(tool, tmp_path / "backups")
    # The instance moves on, then is stopped; a damaged blob and a lost one are in the way.
    instance.connections[0].execute("INSERT INTO t VALUES (999)")
    instance.close()
    store = instance.launcher / "control" / "control.sqlite3"
    blobs = instance.launcher / "control" / "control.sqlite3.artifacts" / "blobs"
    damaged = blobs / instance.control_blob[:2] / instance.control_blob
    damaged.write_bytes(b"damaged")
    arguments = {"launcher_state": instance.launcher, "middleware_state": instance.middleware}
    plan = tool.restore(made, now=NOW, **arguments)
    assert f"replace {store}" in plan
    assert plan[-1].startswith("nothing changed")
    assert damaged.read_bytes() == b"damaged"

    done = tool.restore(made, apply=True, now=NOW, **arguments)
    assert f"replace {store}" in done
    restored = sqlite3.connect(store)
    assert restored.execute("SELECT count(*) FROM t").fetchone() == (50,)
    restored.close()
    assert damaged.read_bytes() == b"artifact bytes"
    assert stat.S_IMODE(store.stat().st_mode) == 0o600
    # What was replaced is kept beside it, the store's -wal with it.
    aside = sorted(p.name for p in store.parent.iterdir() if "pre-restore" in p.name)
    assert aside[0] == "control.sqlite3.pre-restore-20261023T120000Z"
    assert len(list(damaged.parent.glob("*.pre-restore-*"))) == 1
    # A second restore finds nothing to change in the blobs.
    again = tool.restore(made, now=NOW, **arguments)
    assert not any("content-addressed" in line for line in again)


def test_restore_refuses_a_running_instance_and_a_damaged_backup(
    tool: ModuleType, instance: Instance, tmp_path: Path
) -> None:
    made = instance.make(tool, tmp_path / "backups")
    arguments = {"launcher_state": instance.launcher, "middleware_state": None, "apply": True}
    with (instance.launcher / "worker" / "state" / "worker.lock").open("w") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        with pytest.raises(tool.BackupError, match=r"worker.lock is held"):
            tool.restore(made, **arguments)
    (made / "worker" / "worker.sqlite3").write_bytes(b"not a database")
    with pytest.raises(tool.BackupError, match="damaged"):
        tool.restore(made, **arguments)


def test_a_database_that_fails_its_integrity_check_is_not_backed_up(
    tool: ModuleType, tmp_path: Path
) -> None:
    path = tmp_path / "bad.sqlite3"
    db = sqlite3.connect(path, isolation_level=None)
    db.execute("PRAGMA journal_mode=DELETE")
    db.execute("CREATE TABLE t (n INTEGER PRIMARY KEY, v TEXT)")
    db.execute("CREATE INDEX i ON t(v)")
    for n in range(2000):
        db.execute("INSERT INTO t VALUES (?, ?)", (n, f"value {n}"))
    db.close()
    # Damage an index page: the file is still a database, and the check says what is wrong.
    data = bytearray(path.read_bytes())
    for offset in range(4096 * 2, len(data) - 4096, 4096):
        data[offset + 100 : offset + 140] = b"\xff" * 40
    path.write_bytes(bytes(data))
    with pytest.raises((tool.BackupError, sqlite3.Error)):
        tool.snapshot_sqlite(path, tmp_path / "out" / "bad.sqlite3")
    assert not (tmp_path / "out" / "bad.sqlite3").exists()


def test_versions_say_whether_a_start_would_migrate_a_store(
    tool: ModuleType, instance: Instance, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(tool, "expected_versions", lambda: {"control/control.sqlite3": 79})
    lines, status = tool.pending_migrations(instance.launcher)
    assert status == 3
    assert lines[0] == "control/control.sqlite3: version 78, this checkout migrates it to 79"
    monkeypatch.setattr(
        tool,
        "expected_versions",
        lambda: {"control/control.sqlite3": 78, "worker/worker.sqlite3": 9},
    )
    assert tool.pending_migrations(instance.launcher)[1] == 0
    monkeypatch.setattr(tool, "expected_versions", lambda: {"control/control.sqlite3": 77})
    lines, status = tool.pending_migrations(instance.launcher)
    assert status == 1
    assert "NEWER than the 77" in lines[0]
