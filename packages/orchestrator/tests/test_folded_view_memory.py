# SPDX-License-Identifier: MIT
"""Historical payloads do not accumulate when rebuilding an already-folded view."""

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
FIXTURE = Path(__file__).parent / "fixtures" / "folded_view_memory.py"


def run(mode: str, root: Path, count: int) -> dict[str, int]:
    result = subprocess.run(
        [sys.executable, str(FIXTURE), mode, str(root), str(count)],
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    assert result.returncode == 0, f"{mode}: {result.stderr}"
    return json.loads(result.stdout) if mode != "prepare" else {}


def test_rebuild_streams_folded_payloads_and_keeps_exact_evidence(tmp_path: Path) -> None:
    measured = []
    # Before the experiment: <=8 MiB Python / <=16 MiB RSS added per rebuild;
    # fixed output geometry. The positive control must exceed both budgets.
    for count in (4, 40):
        root = tmp_path / str(count)
        run("prepare", root, count)
        found = run("measure", root, count)
        assert found["python_peak"] < 8 * 1024**2, found
        assert found["rss_growth"] < 16 * 1024**2, found
        assert found["view_bytes"] < 10_000, found
        measured.append(found)
    control = run("inline", tmp_path / "40", 40)
    assert control["python_peak"] > 16 * 1024**2, control
    assert control["rss_growth"] > 16 * 1024**2, control
    print(json.dumps({"streamed": measured, "inline": control}, sort_keys=True))
