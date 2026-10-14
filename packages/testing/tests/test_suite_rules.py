# SPDX-License-Identifier: MIT
"""The repository's test configuration: strict marks, and the tier check's reports."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]


def test_a_misspelt_mark_fails_collection(tmp_path: Path) -> None:
    # Without --strict-markers, a module marked `slwo` would land in the fast tier.
    module = tmp_path / "test_misspelt.py"
    module.write_text("import pytest\npytestmark = pytest.mark.slwo\n\ndef test_x():\n    pass\n")
    collect = [sys.executable, "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider"]
    config = ["-c", str(ROOT / "pyproject.toml"), "--rootdir", str(tmp_path)]
    done = subprocess.run(
        [*collect, *config, str(module)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert done.returncode != 0
    assert "'slwo' not found in `markers`" in done.stdout


@pytest.mark.slow
def test_the_tier_check_shows_why_a_collection_failed(tmp_path: Path) -> None:
    # A module that raises ImportError on import breaks the PDF helper's test module
    # (importorskip skips only a missing module), without touching the repository.
    shadow = tmp_path / "pdf_oxide"
    shadow.mkdir()
    (shadow / "__init__.py").write_text('raise ImportError("tier-check probe: broken import")\n')
    done = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "tiers.py"), "check"],
        cwd=ROOT,
        env={**os.environ, "PYTHONPATH": str(tmp_path)},
        capture_output=True,
        text=True,
        check=False,
        timeout=600,
    )
    assert done.returncode != 0
    report = done.stdout + done.stderr
    assert "collection failed" in report
    assert "tier-check probe: broken import" in report
    assert "test_pdfhelper.py" in report
