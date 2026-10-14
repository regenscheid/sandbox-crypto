# SPDX-License-Identifier: MIT
"""Finding and ending the processes a test leaves behind."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from sandbox_science.testing.processes import end_left_behind, left_behind

pytestmark = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="reads /proc")


def test_a_process_running_in_the_directory_is_found_and_killed(tmp_path: Path) -> None:
    base, other = tmp_path / "base", tmp_path / "base-sibling"
    (base / "inner").mkdir(parents=True)
    other.mkdir()
    by_cwd = subprocess.Popen(["sleep", "60"], cwd=base / "inner")
    sleeper = [sys.executable, "-c", "import time; time.sleep(60)"]
    by_argv = subprocess.Popen([*sleeper, str(base / "inner" / "root")], cwd=other)
    elsewhere = subprocess.Popen(["sleep", "60"], cwd=other)
    try:
        assert set(left_behind(base)) == {by_cwd.pid, by_argv.pid}
        killed = end_left_behind(base, grace=0.2)
        assert set(killed) == {by_cwd.pid, by_argv.pid}
        assert by_cwd.wait(timeout=10) < 0
        assert by_argv.wait(timeout=10) < 0
        assert elsewhere.poll() is None  # a sibling directory's process is left alone
        assert left_behind(base) == {}
    finally:
        for process in (by_cwd, by_argv, elsewhere):
            process.kill()
            process.wait()


def test_a_process_that_ends_within_the_grace_is_not_reported(tmp_path: Path) -> None:
    process = subprocess.Popen(["sleep", "0.3"], cwd=tmp_path)
    try:
        assert end_left_behind(tmp_path, grace=10) == {}
    finally:
        process.wait()
