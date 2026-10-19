# SPDX-License-Identifier: MIT
"""Host process identity checks without pretending a Linux runner is macOS."""

from __future__ import annotations

import os

import psutil
import pytest

from sandbox_science.launcher import processes


def test_darwin_path_checks_creation_session_and_command(monkeypatch: pytest.MonkeyPatch) -> None:
    # psutil is imported before patching the dispatch flag; its Linux backend
    # exercises our Darwin branch's API contract on this CI host.
    monkeypatch.setattr(processes.sys, "platform", "darwin")
    pid = os.getpid()
    assert processes.boot_id().startswith("darwin-")
    assert processes.start_time(pid) is not None
    assert processes.start_time(99_999_999) is None
    assert pid in processes.members(os.getsid(pid))
    command = psutil.Process(pid).cmdline()[0]
    assert processes.command_has(pid, command)
    assert not processes.command_has(pid, "definitely-not-this-command")
