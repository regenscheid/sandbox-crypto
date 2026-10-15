# SPDX-License-Identifier: MIT
"""Finding and ending the processes a test leaves behind, and nothing else."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

from sandbox_science.testing.processes import end_left_behind, left_behind

pytestmark = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="reads /proc")

SLEEPER = [sys.executable, "-c", "import time; time.sleep(60)"]


@contextmanager
def started(
    argv: list[str], cwd: Path, *, own_session: bool = False
) -> Iterator[subprocess.Popen[bytes]]:
    """``argv`` running in ``cwd``; with ``own_session``, as if from another terminal."""
    process = subprocess.Popen(
        argv,
        cwd=cwd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=own_session,
    )
    try:
        yield process
    finally:
        process.kill()
        process.wait()


def start_ticks(pid: int) -> int:
    with open(f"/proc/{pid}/stat", "rb") as handle:
        return int(handle.read().rsplit(b")", 1)[1].split()[19])


def test_a_process_working_in_the_directory_or_rooted_there_is_found_and_killed(
    tmp_path: Path,
) -> None:
    base, other = tmp_path / "base", tmp_path / "base-sibling"
    (base / "inner").mkdir(parents=True)
    other.mkdir()
    with (
        started(["sleep", "60"], base / "inner") as by_cwd,
        started([*SLEEPER, "--root", str(base / "inner")], other) as by_root,
        started([*SLEEPER, f"--root={base}"], other) as by_root_option,
        started(["sleep", "60"], other) as elsewhere,
    ):
        expected = {by_cwd.pid, by_root.pid, by_root_option.pid}
        assert set(left_behind(base)) == expected
        assert set(end_left_behind(base, grace=0.2)) == expected
        for process in (by_cwd, by_root, by_root_option):
            assert process.wait(timeout=10) < 0
        assert elsewhere.poll() is None  # a sibling directory's process is left alone
        assert left_behind(base) == {}


def test_a_process_that_ends_within_the_grace_is_not_reported(tmp_path: Path) -> None:
    process = subprocess.Popen(["sleep", "0.3"], cwd=tmp_path)
    try:
        assert end_left_behind(tmp_path, grace=10) == {}
    finally:
        process.wait()


def test_a_process_in_a_removed_directory_of_the_same_path_is_not_the_runs(
    tmp_path: Path,
) -> None:
    # An earlier run's directory, removed and made again under the same path.
    base = tmp_path / "base"
    (base / "old").mkdir(parents=True)
    with started(["sleep", "60"], base / "old") as stranger:
        shutil.rmtree(base)
        (base / "old").mkdir(parents=True)
        assert stranger.pid not in left_behind(base)


def test_a_command_this_session_started_is_found_by_the_paths_it_names(tmp_path: Path) -> None:
    # A command a test starts, such as `sandbox-science-harness serve --state-dir ...`,
    # works elsewhere and has no --root, but names the test's directory.
    base, elsewhere = tmp_path / "base", tmp_path / "elsewhere"
    base.mkdir()
    elsewhere.mkdir()
    with (
        started([*SLEEPER, "--state-dir", str(base / "state")], elsewhere) as bare,
        started([*SLEEPER, f"--token-file={base}/token"], elsewhere) as option,
    ):
        assert set(left_behind(base)) == {bare.pid, option.pid}


def test_other_path_arguments_do_not_count(tmp_path: Path) -> None:
    # Someone watching a log in the run's directory from another terminal, and a --root
    # that only contains the base's path as text.
    base, elsewhere = tmp_path / "base", tmp_path / "elsewhere"
    base.mkdir()
    elsewhere.mkdir()
    copy = Path(f"{tmp_path}-copy{base}")
    copy.mkdir(parents=True)
    try:
        with (
            started(
                ["tail", "-F", str(base / "worker.log")], elsewhere, own_session=True
            ) as watcher,
            started([*SLEEPER, f"--root={copy}/x"], elsewhere) as lookalike,
        ):
            found = left_behind(base)
            assert watcher.pid not in found
            assert lookalike.pid not in found
    finally:
        shutil.rmtree(Path(f"{tmp_path}-copy"))


def test_a_process_started_before_the_run_is_not_the_runs(tmp_path: Path) -> None:
    with started(["sleep", "60"], tmp_path) as earlier:
        assert earlier.pid in left_behind(tmp_path)
        assert earlier.pid not in left_behind(tmp_path, since=start_ticks(earlier.pid) + 1)


def test_the_checking_process_and_its_ancestors_are_not_reported(tmp_path: Path) -> None:
    # A launcher such as `uv run pytest --basetemp=<base>/` names the base in its own
    # command line, and may work in it: the check runs below it.
    code = (
        "import json, sys\n"
        "from pathlib import Path\n"
        "from sandbox_science.testing.processes import left_behind\n"
        "print(json.dumps(list(left_behind(Path(sys.argv[1]), since=0))))\n"
    )
    check = [sys.executable, "-c", code, str(tmp_path)]
    launcher = [
        sys.executable,
        "-c",
        f"import subprocess, sys; sys.exit(subprocess.call({check!r}))",
    ]
    done = subprocess.run(
        [*launcher, f"--root={tmp_path}"], cwd=tmp_path, capture_output=True, check=True
    )
    assert json.loads(done.stdout) == []
