# SPDX-License-Identifier: MIT
"""Owned MAIN kernel notices: exact provenance, delivery and save boundaries."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import tracemalloc
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

import pytest

from sandbox_science.orchestrator import projects
from sandbox_science.orchestrator.kernel_notice_bindings import (
    BindingError,
    KernelBindings,
    WorkerConfiguration,
)
from sandbox_science.orchestrator.projects import ProjectError, ProjectStore

POLICY = "sha256:" + "a" * 64
AUTH = "Bearer test-secret-kernel-notice-configuration"
CONFIG = WorkerConfiguration("http://127.0.0.1:9999", AUTH)


@pytest.fixture
def project_store(tmp_path: Path) -> ProjectStore:
    store = ProjectStore(tmp_path / "state")
    store.create("project", "minimal", POLICY)
    store.open_session("project", "a", dedicated=False)
    return store


def test_lifetime_is_opaque_rotates_even_with_equal_timestamps_and_baseline_is_durable(
    tmp_path, monkeypatch
):
    class SameTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 10, 13, tzinfo=UTC)

    monkeypatch.setattr(projects, "datetime", SameTime)
    store = ProjectStore(tmp_path)
    store.create("project", "minimal", POLICY)
    first = store.open_session("project", "a", dedicated=False)
    assert first.kernel_notice_lifetime is not None
    assert store.kernel_notice_baseline("project", "a") == first
    store.close_session("project", "a")
    second = store.open_session("project", "a", dedicated=False)
    assert second.opened_at == first.opened_at
    assert second.kernel_notice_lifetime != first.kernel_notice_lifetime
    assert store.open_session("project", "a", dedicated=False) == second


def test_legacy_open_gets_present_baseline_once_and_closed_never_baselines(project_store):
    target = project_store.root / "project.json"
    data = json.loads(target.read_bytes())
    del data["sessions"]["a"]["kernel_notice_lifetime"]
    target.write_text(json.dumps(data))
    assert project_store.get("project").sessions["a"].kernel_notice_lifetime is None
    first = project_store.kernel_notice_baseline("project", "a")
    assert first.kernel_notice_lifetime is not None
    assert ProjectStore(project_store.root.parent).kernel_notice_baseline("project", "a") == first
    project_store.close_session("project", "a")
    with pytest.raises(ProjectError, match="no open session"):
        project_store.kernel_notice_baseline("project", "a")


@pytest.mark.parametrize("kind", ["symlink", "fifo", "directory", "oversize"])
def test_lifetime_baseline_bounded_registry_and_lock_special_files(project_store, kind, tmp_path):
    target = project_store.root / "project.json"
    if kind == "oversize":
        target.write_bytes(b" " * (1024 * 1024 + 1))
    else:
        target = project_store.root / "project.lock"
        target.unlink()
        if kind == "symlink":
            target.symlink_to(tmp_path / "untouched")
        elif kind == "fifo":
            os.mkfifo(target)
        else:
            target.mkdir()
    with pytest.raises(ProjectError):
        project_store.kernel_notice_baseline("project", "a")
    assert not (tmp_path / "untouched").exists()


def test_private_binding_stable_by_logical_session_and_not_an_auth_export(tmp_path):
    state = KernelBindings(tmp_path)
    first = state.ensure("project", "a", CONFIG)
    assert state.ensure("project", "a", CONFIG) == first
    assert KernelBindings(tmp_path).ensure("project", "a", CONFIG) == first
    assert state.ensure("project", "b", CONFIG) != first
    assert "test-secret" not in repr(CONFIG)
    for item in tmp_path.iterdir():
        if item.is_file() and item.name != "kernel-notice.key":
            raw = item.read_bytes()
            assert AUTH.encode() not in raw
            assert hashlib.sha256(AUTH.encode()).hexdigest().encode() not in raw
    assert (tmp_path / "kernel-notice.key").stat().st_mode & 0o777 == 0o600
    assert (tmp_path / "kernel-notice-bindings.sqlite3").stat().st_mode & 0o777 == 0o600
    with pytest.raises(BindingError, match="changed"):
        state.ensure("project", "a", WorkerConfiguration(CONFIG.endpoint, AUTH + "-changed"))
    assert state.ensure("project", "a", CONFIG) == first
    with sqlite3.connect(tmp_path / "kernel-notice-bindings.sqlite3") as db:
        assert db.execute("SELECT count(*) FROM bindings").fetchone() == (2,)
        assert db.execute("SELECT diagnostic FROM bindings WHERE session='a'").fetchone() == (
            "binding_changed",
        )


def test_concurrent_private_initializers_share_one_key_and_binding(tmp_path):
    with ThreadPoolExecutor(max_workers=2) as executor:
        tasks = [
            executor.submit(KernelBindings(tmp_path).ensure, "project", "a", CONFIG)
            for _ in range(2)
        ]
        assert tasks[0].result() == tasks[1].result()
    with sqlite3.connect(tmp_path / "kernel-notice-bindings.sqlite3") as db:
        assert db.execute("SELECT count(*) FROM bindings").fetchone() == (1,)


@pytest.mark.parametrize(
    "kind", ["missing", "wrong", "large", "mode", "symlink", "fifo", "directory", "hardlink"]
)
def test_existing_private_key_cannot_be_replaced_recreated_or_followed(tmp_path, kind):
    state = KernelBindings(tmp_path)
    state.ensure("project", "a", CONFIG)
    key = tmp_path / "kernel-notice.key"
    if kind == "mode":
        key.chmod(0o644)
    elif kind == "wrong":
        key.write_bytes(b"x" * 32)
    elif kind == "large":
        key.write_bytes(b"x" * (20 * 1024 * 1024))
    elif kind == "hardlink":
        os.link(key, tmp_path / "alias")
    else:
        key.unlink()
        if kind == "symlink":
            (tmp_path / "elsewhere").write_bytes(b"x" * 32)
            key.symlink_to(tmp_path / "elsewhere")
        elif kind == "fifo":
            os.mkfifo(key)
        elif kind == "directory":
            key.mkdir()
    with pytest.raises(BindingError) as caught:
        state.ensure("project", "a", CONFIG)
    assert AUTH not in str(caught.value)
    assert str(tmp_path) not in str(caught.value)
    if kind == "missing":
        assert not key.exists()


def test_binding_corruption_is_sql_bounded_and_substituted_scope_cannot_prove(tmp_path):
    state = KernelBindings(tmp_path)
    state.ensure("project", "a", CONFIG)
    state.ensure("project", "b", CONFIG)
    database = tmp_path / "kernel-notice-bindings.sqlite3"
    with sqlite3.connect(database) as db:
        db.execute("DROP TRIGGER bindings_immutable")
        other = db.execute("SELECT binding,pin FROM bindings WHERE session='b'").fetchone()
        db.execute("UPDATE bindings SET binding=?,pin=? WHERE session='a'", other)
    with pytest.raises(BindingError, match="changed"):
        state.ensure("project", "a", CONFIG)
    with sqlite3.connect(database) as db:
        db.execute("UPDATE bindings SET pin=? WHERE session='a'", ("x" * (20 * 1024 * 1024),))
    tracemalloc.start()
    try:
        with pytest.raises(BindingError):
            state.ensure("project", "a", CONFIG)
        assert tracemalloc.get_traced_memory()[1] < 256 * 1024
    finally:
        tracemalloc.stop()


def test_binding_quotas_count_empty_metadata_and_do_not_repin(tmp_path, monkeypatch):
    from sandbox_science.orchestrator import kernel_notice_bindings as module

    state = KernelBindings(tmp_path)
    original = state.ensure("project", "a", CONFIG)
    monkeypatch.setattr(module, "MAX_BINDINGS", 1)
    with pytest.raises(BindingError, match="unavailable"):
        state.ensure("project", "b", CONFIG)
    assert state.ensure("project", "a", CONFIG) == original
    monkeypatch.setattr(module, "MAX_BYTES", 256)
    with pytest.raises(BindingError, match="unavailable"):
        state.ensure("project", "a", CONFIG)
