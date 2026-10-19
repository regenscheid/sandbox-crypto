# SPDX-License-Identifier: MIT
"""Fixed gates through multiple real folds, reviews, finalization and reopened evidence."""

from __future__ import annotations

import json
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
        completed = subprocess.run(
            [sys.executable, str(fixture), str(tmp_path / str(count)), str(count)],
            text=True,
            capture_output=True,
            timeout=240,
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
