# SPDX-License-Identifier: MIT
"""A part that is started again gives up after a bound, rather than forever."""

from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from sandbox_science.launcher.processes import members
from sandbox_science.launcher.state import Layout
from sandbox_science.launcher.supervise import Part, Supervisor
from sandbox_science.testing import eventually

pytestmark = pytest.mark.slow


def test_parent_pipe_watcher_ends_the_session_after_parent_loss(tmp_path: Path) -> None:
    """Exercise the Darwin watcher protocol on Linux without claiming a Mac run."""
    read_fd, write_fd = os.pipe()
    marker = tmp_path / "child.pid"
    process = subprocess.Popen(
        [
            sys.executable,
            "-I",
            "-m",
            "sandbox_science.launcher.parent_watch",
            str(read_fd),
            str(os.getpid()),
            sys.executable,
            "-c",
            "import os,time,pathlib; "
            f"pathlib.Path({str(marker)!r}).write_text(str(os.getpid())); "
            "time.sleep(60)",
        ],
        cwd=tmp_path,
        pass_fds=(read_fd,),
        start_new_session=True,
    )
    os.close(read_fd)
    try:
        deadline = time.monotonic() + 10
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert marker.exists()
        child_pid = int(marker.read_text())
        assert child_pid in members(process.pid)
        os.close(write_fd)
        write_fd = -1
        assert process.wait(timeout=10) == 70
        assert child_pid not in members(process.pid)
    finally:
        if write_fd >= 0:
            os.close(write_fd)
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=10)


async def test_a_part_that_keeps_stopping_is_given_up_after_its_bound(tmp_path: Path) -> None:
    layout = Layout(tmp_path / "state")
    layout.make()
    supervisor = Supervisor(
        layout, asyncio.Event(), stop_timeout=1, restart_delays=(0.01,), restarts=2
    )
    flaky = Part("flaky", ["/bin/sh", "-c", "sleep 0.2; exit 3"], tmp_path, restart=True)
    await supervisor.start(flaky, 5)
    try:
        reason = await asyncio.wait_for(supervisor.watch(), 20)
    finally:
        await supervisor.stop()
    assert reason is not None
    assert "the flaky kept stopping: it was started again 2 times in a row" in reason


async def test_optional_restart_exhaustion_preserves_other_components(tmp_path: Path) -> None:
    layout = Layout(tmp_path / "state")
    layout.make()
    stop = asyncio.Event()
    supervisor = Supervisor(layout, stop, stop_timeout=1, restart_delays=(0.01,), restarts=1)
    healthy = await supervisor.start(
        Part("healthy", [sys.executable, "-c", "import time; time.sleep(60)"], tmp_path), 5
    )
    await supervisor.start(
        Part(
            "flaky", ["/bin/sh", "-c", "sleep 0.2; exit 3"], tmp_path, restart=True, required=False
        ),
        5,
    )
    watching = asyncio.create_task(supervisor.watch())
    try:
        await eventually(
            lambda: any(p.part.name == "flaky" and p.reported for p in supervisor.running)
        )
        assert not watching.done()
        assert healthy.process.returncode is None
        stop.set()
        assert await watching is None
    finally:
        watching.cancel()
        await asyncio.gather(watching, return_exceptions=True)
        await supervisor.stop()


async def test_spawn_failure_consumes_retry_bound_without_tearing_down_other_parts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    layout = Layout(tmp_path / "state")
    layout.make()
    stop = asyncio.Event()
    supervisor = Supervisor(layout, stop, stop_timeout=1, restart_delays=(0.01,), restarts=2)
    healthy = await supervisor.start(
        Part("healthy", [sys.executable, "-c", "import time; time.sleep(60)"], tmp_path), 5
    )
    await supervisor.start(
        Part(
            "flaky", ["/bin/sh", "-c", "sleep 0.2; exit 3"], tmp_path, restart=True, required=False
        ),
        5,
    )
    calls = 0

    async def cannot_spawn(*args: object, **kwargs: object) -> asyncio.subprocess.Process:
        nonlocal calls
        calls += 1
        raise OSError("synthetic process admission failure")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", cannot_spawn)
    watching = asyncio.create_task(supervisor.watch())
    try:
        await eventually(
            lambda: any(p.part.name == "flaky" and p.reported for p in supervisor.running)
        )
        assert calls == 2
        assert not watching.done()
        assert healthy.process.returncode is None
        stop.set()
        assert await watching is None
    finally:
        watching.cancel()
        await asyncio.gather(watching, return_exceptions=True)
        await supervisor.stop()
