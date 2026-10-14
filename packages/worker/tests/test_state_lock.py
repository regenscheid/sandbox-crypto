# SPDX-License-Identifier: MIT
"""One worker for each state directory, and what that lets the worker settle (decision 9)."""

from __future__ import annotations

import asyncio
import contextlib
import os
import secrets
import signal
import socket
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from sandbox_science.contracts import ExecutionContext, PublishRequest, ShellRequest, new_id
from sandbox_science.worker.artifacts import Blobs
from sandbox_science.worker.backends import LocalBackend
from sandbox_science.worker.build import build_worker
from sandbox_science.worker.cli import main
from sandbox_science.worker.errors import WorkerError
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
    assert f"cleared CaptureLease(publication_id='{lease.publication_id}'" in printed
    assert "no capture leases" in printed
    # Clearing is idempotent, and the clear is recorded in the store.
    assert main(["captures", "--settings", str(path), "--clear", lease.publication_id]) == 0
    assert "already cleared" in capsys.readouterr().out
    with contextlib.closing(sqlite3.connect(state / "worker.sqlite3")) as db:
        assert db.execute("SELECT publication_id, phase FROM capture_clears").fetchall() == [
            (lease.publication_id, "held")
        ]


def test_captures_refuses_a_state_directory_without_a_store(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = settings_file(tmp_path)
    assert main(["captures", "--settings", str(path)]) == 1
    assert "no worker store" in capsys.readouterr().err
    assert not (tmp_path / "state" / "worker.sqlite3").exists()


async def test_a_publication_retrying_cleanup_never_reopens_a_released_executor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Admission is checked again after the retry: a release that ran meanwhile stands."""
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
        original = channel.request
        acked, resume = asyncio.Event(), asyncio.Event()

        async def slow(op: str, **fields: Any) -> dict[str, Any]:
            reply = await original(op, **fields)
            if op == "capture_cancel" and not acked.is_set():
                acked.set()
                await resume.wait()
            return reply

        monkeypatch.setattr(channel, "request", slow)
        publishing = asyncio.create_task(
            worker.publish(PublishRequest(context=CONTEXT, path="notes.txt"))
        )
        await acked.wait()
        await worker.release("env1")
        resume.set()
        with pytest.raises(WorkerError, match="admission"):
            await publishing
        assert view(worker).channel_or_none("env1") is None
    finally:
        await worker.close()
        store.close()


async def test_a_lease_never_dispatched_neither_holds_admission_nor_survives_a_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = Store(tmp_path / "worker.sqlite3")
    worker = manager(store, tmp_path)
    try:
        await worker.start()
        await worker.capabilities("env1")

        def refused(*args: object, **kwargs: object) -> object:
            raise ValueError("capture endpoint incarnation changed before dispatch")

        with monkeypatch.context() as patch:
            patch.setattr(store, "dispatch_capture", refused)
            for _ in range(3):
                with pytest.raises(WorkerError, match="changed before dispatch"):
                    await worker.publish(PublishRequest(context=CONTEXT, path="notes.txt"))
        assert store.capture_leases() == ()  # nothing was sent, so nothing is held
    finally:
        await worker.close()
    # One a stopped worker left held, never dispatched, is settled at the next start.
    held = store.reserve_capture(new_id("pub"), "env1")
    store.finish_capture(held, certain=False)
    assert [lease.phase for lease in store.capture_leases()] == ["held"]
    worker = manager(store, tmp_path)
    try:
        await worker.start()
        assert store.capture_leases() == ()
    finally:
        await worker.close()
        store.close()


HOLD = """
import subprocess, sys, time
from pathlib import Path
from sandbox_science.worker.state_lock import StateLock
lock = StateLock(Path(sys.argv[1]))
children = []
if sys.argv[2] == "spawn":
    # The worst case a worker could write: a child that keeps every inheritable descriptor.
    children.append(subprocess.Popen(["sleep", "60"], close_fds=False))
    children.append(subprocess.Popen(["sleep", "60"], start_new_session=True))
print("ready", *(child.pid for child in children), flush=True)
time.sleep(60)
"""


def lock_descriptors(pid: int, lock: Path) -> list[str]:
    found: list[str] = []
    with contextlib.suppress(OSError):
        for descriptor in os.listdir(f"/proc/{pid}/fd"):
            with contextlib.suppress(OSError):
                if os.readlink(f"/proc/{pid}/fd/{descriptor}") == str(lock):
                    found.append(descriptor)
    return found


def test_a_second_process_is_refused_and_a_killed_holder_frees_the_lock(tmp_path: Path) -> None:
    state = tmp_path / "state"
    process = subprocess.Popen(
        [sys.executable, "-c", HOLD, str(state), "none"], stdout=subprocess.PIPE, text=True
    )
    try:
        assert process.stdout is not None
        assert process.stdout.readline().startswith("ready")
        with pytest.raises(SettingsError, match="another worker uses the state directory"):
            StateLock(state)
        process.kill()
        process.wait()
        with StateLock(state):
            pass
        assert (state / "worker.lock").exists()  # left in place, as it must be
    finally:
        process.kill()
        process.wait()


def test_children_do_not_inherit_the_lock(tmp_path: Path) -> None:
    state = tmp_path / "state"
    process = subprocess.Popen(
        [sys.executable, "-c", HOLD, str(state), "spawn"], stdout=subprocess.PIPE, text=True
    )
    children: list[int] = []
    try:
        assert process.stdout is not None
        children = [int(pid) for pid in process.stdout.readline().split()[1:]]
        assert lock_descriptors(process.pid, state / "worker.lock")
        for child in children:
            assert lock_descriptors(child, state / "worker.lock") == []
        process.kill()
        process.wait()
        with StateLock(state):  # the children, still running, hold nothing
            pass
    finally:
        process.kill()
        process.wait()
        for child in children:
            with contextlib.suppress(ProcessLookupError):
                os.kill(child, signal.SIGKILL)


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def test_serve_twice_and_captures_against_a_running_serve(tmp_path: Path) -> None:
    worker = [str(Path(sys.executable).parent / "sandbox-science-worker")]
    (tmp_path / "worker.token").write_text(secrets.token_hex(32) + "\n")
    settings = tmp_path / "worker.toml"
    settings.write_text(
        'token_file = "worker.token"\nstate_dir = "state"\n'
        f'listen = "127.0.0.1:{free_port()}"\n\n[local]\n'
    )
    first = subprocess.Popen(
        [*worker, "serve", "--settings", str(settings)], stderr=subprocess.PIPE, text=True
    )
    try:
        assert first.stderr is not None
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and "worker API on" not in first.stderr.readline():
            pass
        second = subprocess.run(
            [*worker, "serve", "--settings", str(settings)],
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert second.returncode == 1
        assert "another worker uses the state directory" in second.stderr
        assert second.stderr.count("\n") == 1, second.stderr  # one line, no traceback
        listed = subprocess.run(
            [*worker, "captures", "--settings", str(settings)],
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert listed.returncode == 1
        assert "stop it first" in listed.stderr
        first.send_signal(signal.SIGTERM)
        assert first.wait(timeout=60) == 0
        listed = subprocess.run(
            [*worker, "captures", "--settings", str(settings)],
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert listed.returncode == 0, listed.stderr
        assert "no capture leases" in listed.stdout
    finally:
        first.kill()
        first.wait()
