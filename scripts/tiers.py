# SPDX-License-Identifier: MIT
"""The test tiers CI runs, and a check that every test belongs to exactly one of them.

    uv run python scripts/tiers.py run fast -n auto   # pytest on one tier
    uv run python scripts/tiers.py check              # every test in exactly one tier

A tier is a marker expression. Pull requests run ``fast`` and ``slow``, each in a job
of its own; pushes to main and the nightly run also run the whole suite serially, with
no marker filter, on every supported Python. ``check`` also fails when a ``test_*.py``
file (tracked, or new and not ignored) yields no test at all, so a file outside
``testpaths`` is not silently left out.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TIERS = {"fast": "not slow", "slow": "slow"}
"""Each tier's ``-m`` expression; together they must select every test exactly once."""


class _Recorder:
    """A pytest plugin that keeps the node IDs a collection selected."""

    def __init__(self) -> None:
        self.ids: list[str] = []

    def pytest_collection_finish(self, session: object) -> None:
        self.ids = [item.nodeid for item in getattr(session, "items", [])]


def _collect_here(expression: str | None, output: Path) -> int:
    """Collect in this process and write the selected node IDs to ``output`` as JSON."""
    import pytest

    recorder = _Recorder()
    arguments = ["--collect-only", "-q", "-p", "no:cacheprovider"]
    if expression is not None:
        arguments += ["-m", expression]
    with open(os.devnull, "w") as quiet:
        stdout, sys.stdout = sys.stdout, quiet
        try:
            code = pytest.main(arguments, plugins=[recorder])
        finally:
            sys.stdout = stdout
    output.write_text(json.dumps(recorder.ids))
    # 5: nothing selected, which an empty tier may be.
    return 0 if code in (0, 5) else int(code)


def _collect(expression: str | None, output: Path) -> list[str]:
    """The node IDs a collection with ``-m expression`` selects (all tests for None)."""
    command = [sys.executable, __file__, "_collect", str(output)]
    if expression is not None:
        command += ["--expression", expression]
    done = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, check=False)  # noqa: S603
    if done.returncode != 0:
        raise SystemExit(f"collection failed ({expression or 'all tests'}):\n{done.stderr}")
    return json.loads(output.read_text())


def _test_files() -> set[str]:
    """Files (tracked, or new and not ignored) that pytest would take for test modules."""
    git = shutil.which("git")
    if git is None:
        raise SystemExit("git is not on PATH")
    patterns = ["*test_*.py", "*_test.py"]
    listed = subprocess.run(  # noqa: S603 -- fixed arguments, git resolved on PATH
        [git, "ls-files", "-z", "--cached", "--others", "--exclude-standard", "--", *patterns],
        cwd=ROOT,
        capture_output=True,
        check=True,
    ).stdout.decode()
    names = (Path(name) for name in listed.split("\0") if name)
    return {
        name.as_posix()
        for name in names
        if name.name.startswith("test_") or name.name.endswith("_test.py")
    }


def check() -> int:
    """Fail when a test is in no tier or in two, or a test file yields no test."""
    with tempfile.TemporaryDirectory() as directory:
        scratch = Path(directory)
        expressions: list[str | None] = [None, *TIERS.values()]
        with ThreadPoolExecutor(len(expressions)) as pool:
            outputs = [scratch / f"{index}.json" for index in range(len(expressions))]
            results = list(pool.map(_collect, expressions, outputs))
    everything, *tiers = (set(ids) for ids in results)
    problems: list[str] = []
    for test in sorted(everything):
        homes = [name for name, ids in zip(TIERS, tiers, strict=True) if test in ids]
        if len(homes) != 1:
            problems.append(f"{test}: in {', '.join(homes) or 'no tier'}")
    collected = {test.split("::", 1)[0] for test in everything}
    for name in sorted(_test_files() - collected):
        problems.append(f"{name}: no test collected from this file")
    if problems:
        print("Tests outside exactly one tier:", *problems, sep="\n  ")
        return 1
    sizes = ", ".join(f"{name} {len(ids)}" for name, ids in zip(TIERS, tiers, strict=True))
    print(f"{len(everything)} tests, each in one tier: {sizes}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="The test tiers CI runs.")
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run", help="run pytest on one tier; other arguments go to pytest")
    run.add_argument("tier", choices=sorted(TIERS))
    commands.add_parser("check", help="check that every test is in exactly one tier")
    collect = commands.add_parser("_collect")
    collect.add_argument("output", type=Path)
    collect.add_argument("--expression")
    arguments, rest = parser.parse_known_args(argv)
    if arguments.command == "run":
        command = [sys.executable, "-m", "pytest", "-m", TIERS[arguments.tier], *rest]
        os.chdir(ROOT)
        os.execv(sys.executable, command)  # noqa: S606
    if rest:
        parser.error(f"unrecognized arguments: {' '.join(rest)}")
    if arguments.command == "check":
        return check()
    return _collect_here(arguments.expression, arguments.output)


if __name__ == "__main__":
    sys.exit(main())
