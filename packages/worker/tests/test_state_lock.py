# SPDX-License-Identifier: MIT
"""One worker for each state directory, and what that lets the worker settle (decision 9)."""

from __future__ import annotations

from pathlib import Path

import pytest

from sandbox_science.contracts import ExecutionContext, PublishRequest, ShellRequest, new_id
from sandbox_science.worker.artifacts import Blobs
from sandbox_science.worker.backends import LocalBackend
from sandbox_science.worker.build import build_worker
from sandbox_science.worker.cli import main
from sandbox_science.worker.manager import ExecutionManager
from sandbox_science.worker.settings import LocalBackendSettings, Settings, SettingsError
from sandbox_science.worker.state_lock import StateLock
from sandbox_science.worker.store import Store

from .harness import view

pytestmark = pytest.mark.slow

CONTEXT = ExecutionContext(project_id="p1", session_id="s1", environment_id="env1")
TOKEN = "test-secret-" + "s" * 40


def settings_file(tmp_path: Path) -> Path:
    path = tmp_path / "worker.toml"
    path.write_text('token_file = "worker.token"\nstate_dir = "state"\n\n[local]\n')
    return path


def local_settings(tmp_path: Path) -> Settings:
    return Settings.model_validate(
        {"token_file": tmp_path / "worker.token", "state_dir": tmp_path / "state", "local": {}}
    )


def manager(store: Store, root: Path) -> ExecutionManager:
    return ExecutionManager(
        store,
        LocalBackend(LocalBackendSettings(), root),
        Blobs(root / "artifacts"),
        pool_id="state-lock",
        max_artifact_size=1024,
        state_dir=root,
    )


async def test_a_second_worker_on_the_same_state_directory_is_refused(tmp_path: Path) -> None:
    settings = local_settings(tmp_path)
    first = build_worker(settings, TOKEN)
    try:
        with pytest.raises(SettingsError, match="another worker uses the state directory"):
            build_worker(settings, TOKEN)
    finally:
        await first.close()
    again = build_worker(settings, TOKEN)
    await again.close()


async def test_a_start_settles_the_leases_a_stopped_worker_left_active(tmp_path: Path) -> None:
    store = Store(tmp_path / "worker.sqlite3")
    reserved = store.reserve_capture(new_id("pub"), "env1")
    dispatched = store.dispatch_capture(
        store.reserve_capture(new_id("pub"), "env2"), "inc-" + "1" * 24, new_id("cap")
    )
    worker = manager(store, tmp_path)
    try:
        await worker.start()
        leases = store.capture_leases()
        # Never dispatched: nothing ran in the sandbox. Dispatched: held until cleanup.
        assert all(lease.publication_id != reserved.publication_id for lease in leases)
        assert [(lease.publication_id, lease.phase) for lease in leases] == [
            (dispatched.publication_id, "held")
        ]
    finally:
        await worker.close()
        store.close()


async def test_held_leases_are_retried_on_the_running_executor_before_admission_refuses(
    tmp_path: Path,
) -> None:
    """A cleanup acknowledgement that missed its deadline does not hold admission until the
    bridge reconnects: the next publication asks the executor again."""
    store = Store(tmp_path / "worker.sqlite3")
    worker = manager(store, tmp_path)
    try:
        await worker.start()
        await worker.capabilities("env1")
        channel = view(worker).channel("env1")
        for _ in range(2):
            lease = store.dispatch_capture(
                store.reserve_capture(new_id("pub"), "env1"), channel.incarnation, new_id("cap")
            )
            store.finish_capture(lease, certain=False)
        assert [lease.phase for lease in store.capture_leases()] == ["held", "held"]
        written, _ = await worker.submit(
            ShellRequest(operation_id="write", context=CONTEXT, script="printf notes > notes.txt")
        )
        await worker.wait(written.execution_id)
        artifact = await worker.publish(PublishRequest(context=CONTEXT, path="notes.txt"))
        assert artifact.size == 5
        assert store.capture_leases() == ()
    finally:
        await worker.close()
        store.close()


def test_captures_lists_and_clears_leases_only_with_the_worker_stopped(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = settings_file(tmp_path)
    state = tmp_path / "state"
    store = Store(state / "worker.sqlite3")
    lease = store.dispatch_capture(
        store.reserve_capture(new_id("pub"), "env1"), "inc-" + "1" * 24, new_id("cap")
    )
    store.finish_capture(lease, certain=False)
    store.close()
    with StateLock(state):
        assert main(["captures", "--settings", str(path)]) == 1
        assert "another worker uses the state directory" in capsys.readouterr().err
    assert main(["captures", "--settings", str(path)]) == 0
    assert f"{lease.publication_id} env1 held" in capsys.readouterr().out
    assert main(["captures", "--settings", str(path), "--clear", lease.publication_id]) == 0
    printed = capsys.readouterr().out
    assert f"cleared {lease.publication_id}" in printed
    assert "no capture leases" in printed
    assert main(["captures", "--settings", str(path), "--clear", lease.publication_id]) == 1
