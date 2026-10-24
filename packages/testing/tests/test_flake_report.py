# SPDX-License-Identifier: MIT
"""The nightly's flake-rate summary reads pytest-repeat's output."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

ROOT = Path(__file__).resolve().parents[3]


def report() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "flake_report", ROOT / "scripts" / "flake_report.py"
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


OUTPUT = """\
=========================== short test summary info ============================
FAILED packages/a/tests/test_x.py::test_one[1-2] - assert 0
FAILED packages/a/tests/test_x.py::test_one[2-2] - assert 0
ERROR packages/a/tests/test_y.py::test_two[case-a-1-2] - boom
========= 2 failed, 1 error, 1947 passed, 3 skipped, 4 warnings in 900.00s =========
"""


def test_repeat_suffixes_are_dropped_and_parameters_kept() -> None:
    name = report().test_name
    assert name("t.py::test_one[1-2]") == "t.py::test_one"
    assert name("t.py::test_two[case-a-1-2]") == "t.py::test_two[case-a]"
    assert name("t.py::test_plain") == "t.py::test_plain"


def test_the_summary_gives_the_rate_and_each_failing_test() -> None:
    text = report().summary(OUTPUT, "slow tier")
    assert "1950 test runs: 1947 passed, 3 failed or errored" in text
    assert "0.154%" in text
    assert "| 2 | `packages/a/tests/test_x.py::test_one` |" in text
    assert "| 1 | `packages/a/tests/test_y.py::test_two[case-a]` |" in text


def test_a_run_stopped_by_the_session_timeout_says_so() -> None:
    output = (
        "!!!!!!! xdist.dsession.Interrupted: session-timeout: 4320.0 sec exceeded !!!!!!!\n"
        "========= 9038 passed, 260 skipped in 4344.40s (1:12:24) =========\n"
    )
    text = report().summary(output, "slow tier")
    assert "9038 test runs: 9038 passed, 0 failed or errored" in text
    assert "stopped early" in text


def test_missing_or_empty_output_is_not_an_error() -> None:
    module = report()
    assert "did not get as far" in module.summary(None, "x")
    assert "No test run finished" in module.summary("collected 0 items\n", "x")


def test_the_summary_is_appended_to_the_step_summary(tmp_path: Path, monkeypatch, capsys) -> None:
    output = tmp_path / "pytest.txt"
    output.write_text(OUTPUT)
    target = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(target))
    assert report().main([str(output), "--title", "shard 1"]) == 0
    assert "### shard 1" in target.read_text()
    assert "flake rate" in capsys.readouterr().out
