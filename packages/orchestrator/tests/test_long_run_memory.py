# SPDX-License-Identifier: MIT
"""Fixed gates through multiple real folds, reviews, finalization and reopened evidence."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(sys.platform != "linux", reason="Linux RSS gate"),
]


def test_ten_times_longer_loop_stays_bounded_across_consumers(tmp_path: Path) -> None:
    fixture = Path(__file__).parent / "fixtures" / "long_run_memory.py"
    reports = []
    for count in (40, 400):
        # glibc gives threads arenas of their own, up to eight a core, and on Python 3.12
        # their fragmentation alone grew RSS by 140 MiB in the longer loop where two
        # arenas grow it by 35 MiB (babbage, 2026-10-22): the gate measures this code's
        # memory, not the allocator's spread across threads.
        completed = subprocess.run(
            [sys.executable, str(fixture), str(tmp_path / str(count)), str(count)],
            text=True,
            capture_output=True,
            timeout=240,
            env={**os.environ, "MALLOC_ARENA_MAX": "2"},
        )
        assert completed.returncode == 0, completed.stderr
        report = json.loads(completed.stdout)
        assert report["python_peak"] < 32 * 1024**2, report
        assert report["rss_growth"] < 64 * 1024**2, report
        assert report["final_bytes"] < 1024**2, report
        assert report["folds"] >= 2
        assert report["reviews"] >= 1
        reports.append(report)
    assert reports[-1]["control_peak"] > 64 * 1024**2, reports
    print(json.dumps(reports))
