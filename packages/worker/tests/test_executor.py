# SPDX-License-Identifier: MIT
"""Executor helpers that the sandbox's restrictions shape."""

from __future__ import annotations

import os
import signal
import subprocess
import time
from pathlib import Path

import pytest

from sandbox_science.worker import executor


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    # A zombie still answers kill(0): check its state.
    with open(f"/proc/{pid}/stat", "rb") as handle:
        state = handle.read().rsplit(b")", 1)[1].split()[0]
    return state != b"Z"


def test_a_refused_group_signal_falls_back_to_each_member(monkeypatch: pytest.MonkeyPatch) -> None:
    # OpenShell's seccomp filter refuses kill(-pgid) but allows kill(pid) (lab 4).
    leader = subprocess.Popen(
        ["/bin/bash", "-c", "sleep 60 & echo $!; wait"],
        stdout=subprocess.PIPE,
        start_new_session=True,
        text=True,
    )
    assert leader.stdout is not None
    child = int(leader.stdout.readline())
    assert set(executor.group_members(leader.pid)) >= {leader.pid, child}

    def refuse(pgid: int, signum: int) -> None:
        raise PermissionError(1, "Operation not permitted")

    monkeypatch.setattr(os, "killpg", refuse)
    executor.signal_group(leader.pid, signal.SIGKILL)
    leader.wait(timeout=10)
    for _ in range(50):
        if not _alive(child):
            break
        time.sleep(0.1)
    assert not _alive(child)


def test_signalling_a_finished_group_is_harmless() -> None:
    process = subprocess.Popen(["true"], start_new_session=True)  # noqa: S607
    process.wait()
    executor.signal_group(process.pid, signal.SIGTERM)


def test_capture_keeps_head_and_tail() -> None:
    capture = executor.Capture("stdout", 10)
    live = capture.feed(b"abcdefghij0123456789")
    assert live == "abcde"
    summary = capture.summary()
    assert summary["truncated"] is True
    assert summary["total_bytes"] == 20
    assert summary["text"].startswith("abcde\n[… 10 bytes omitted …]\n")
    assert summary["text"].endswith("56789")


def test_the_environments_tools_come_first_on_path(tmp_path: Path) -> None:
    # Lab 5: Sage's gp and GAP interfaces look their programs up on PATH.
    prefix = tmp_path / "envs" / "sage"
    (prefix / "conda-meta").mkdir(parents=True)
    tools = str(prefix / "bin")
    env = executor.tool_environment(
        {"PATH": f"/usr/bin:{tools}:/bin", "HOME": "/sandbox"}, f"{tools}/python", str(prefix)
    )
    assert env["PATH"] == f"{tools}:/usr/bin:/bin"
    assert env["CONDA_PREFIX"] == str(prefix)
    assert env["HOME"] == "/sandbox"


def test_a_plain_interpreter_gets_no_conda_prefix(tmp_path: Path) -> None:
    env = executor.tool_environment({}, str(tmp_path / "bin" / "python3"), str(tmp_path))
    assert env["PATH"] == str(tmp_path / "bin")
    assert "CONDA_PREFIX" not in env
