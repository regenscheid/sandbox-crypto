# SPDX-License-Identifier: MIT
"""Turn a repeated pytest run's output into the job summary's flake rate.

    python scripts/flake_report.py <pytest-output-file> [--title TEXT]

The nightly runs repeat the slow tier with pytest-repeat (``--count N``), so each test runs
N times and a test that fails once in N runs is a flake. This reads the file
``scripts/ci_pytest.sh`` leaves, counts the runs that passed and failed, and prints a
Markdown summary: the observed flake rate, and each test that failed with how often.
Under GitHub Actions the summary is also appended to ``$GITHUB_STEP_SUMMARY``. The exit
status is always 0; the test step's own status says whether the run passed.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Final

OUTCOMES: Final = re.compile(r"(\d+) (passed|failed|errors?|skipped|xfailed|xpassed)")
FINAL_LINE: Final = re.compile(r"^=+ (.*\d+ (?:passed|failed|errors?|skipped).*) =+$")
FAILURE: Final = re.compile(r"^(?:FAILED|ERROR) (\S+)")
REPEAT_SUFFIX: Final = re.compile(r"\[(?:(.*)-)?\d+-\d+\]$")
"""pytest-repeat ends each test ID with ``[run-count]``, after any parameters."""
INTERRUPTED: Final = re.compile(r"^!+ (.*?) !+$")


def test_name(node_id: str) -> str:
    """The node ID without pytest-repeat's ``[run-count]`` suffix, keeping any parameters."""
    found = REPEAT_SUFFIX.search(node_id)
    if found is None:
        return node_id
    parameters = found.group(1)
    return node_id[: found.start()] + (f"[{parameters}]" if parameters else "")


def read_output(text: str) -> tuple[Counter[str], Counter[str], str | None]:
    """The outcome counts, the failed tests with their failure counts, and why it stopped."""
    outcomes: Counter[str] = Counter()
    failed: Counter[str] = Counter()
    stopped: str | None = None
    for line in text.splitlines():
        if found := FAILURE.match(line):
            failed[test_name(found.group(1))] += 1
        elif (final := FINAL_LINE.match(line)) is not None:
            outcomes = Counter()
            for number, outcome in OUTCOMES.findall(final.group(1)):
                outcomes[outcome.rstrip("s") if outcome.startswith("error") else outcome] += int(
                    number
                )
        elif (cut := INTERRUPTED.match(line)) is not None and "Interrupted" in cut.group(1):
            stopped = cut.group(1)
    return outcomes, failed, stopped


def summary(text: str | None, title: str) -> str:
    """The Markdown summary of one repeated run's output (None when there is none)."""
    lines = [f"### {title}", ""]
    if text is None:
        return "\n".join([*lines, "No pytest output: the run did not get as far as the tests.", ""])
    outcomes, failed, stopped = read_output(text)
    passed = outcomes["passed"]
    broken = outcomes["failed"] + outcomes["error"]
    runs = passed + broken
    if runs == 0:
        return "\n".join([*lines, "No test run finished.", ""])
    rate = 100 * broken / runs
    lines.append(
        f"{runs} test runs: {passed} passed, {broken} failed or errored. "
        f"**Observed flake rate {rate:.3f}%** ({broken}/{runs})."
    )
    if stopped is not None:
        lines += ["", f"The run stopped early ({stopped}); the rate covers the runs that finished."]
    if failed:
        lines += ["", "| failures | test |", "| ---: | --- |"]
        lines += [f"| {count} | `{name}` |" for name, count in failed.most_common()]
    return "\n".join([*lines, ""])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="The flake rate of a repeated pytest run.")
    parser.add_argument("output", type=Path, help="the pytest output file")
    parser.add_argument("--title", default="Flake rate")
    arguments = parser.parse_args(argv)
    try:
        text = arguments.output.read_text(errors="replace")
    except OSError:
        text = None
    report = summary(text, arguments.title)
    print(report)
    target = os.environ.get("GITHUB_STEP_SUMMARY")
    if target:
        with open(target, "a", encoding="utf-8") as handle:
            handle.write(report + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
