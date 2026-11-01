# SPDX-License-Identifier: MIT
"""Snapshot outages retry and fail before install rather than using unrelated indexes."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "common" / "apt_install.sh"


@pytest.mark.parametrize("failed_update", [False, True])
def test_apt_install_keeps_snapshot_and_requires_successful_indexes(
    tmp_path: Path, failed_update: bool
) -> None:
    calls = tmp_path / "calls.jsonl"
    executable = tmp_path / "apt-get"
    executable.write_text(
        f"#!{sys.executable}\nimport json,sys\nfrom pathlib import Path\n"
        f"with Path({str(calls)!r}).open('a') as output: "
        "output.write(json.dumps(sys.argv[1:])+'\\n')\n"
        f"sys.exit(100 if {failed_update!r} and sys.argv[-1]=='update' else 0)\n"
    )
    executable.chmod(0o700)
    done = subprocess.run(
        ["bash", str(SCRIPT), "20261010T000000Z", "git", "openssh-client", "xz-utils"],
        capture_output=True,
        text=True,
        env={**os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}"},
    )
    actual = [json.loads(row) for row in calls.read_text().splitlines()]
    assert done.returncode == (100 if failed_update else 0)
    assert actual[0][-1] == "update"
    assert "APT::Update::Error-Mode=any" in actual[0]
    assert all("APT::Snapshot=20261010T000000Z" in call for call in actual)
    assert all("Acquire::Retries=5" in call for call in actual)
    if failed_update:
        assert len(actual) == 1  # no install after even one failed index
    else:
        assert actual[1][-3:] == ["git", "openssh-client", "xz-utils"]
        assert "install" in actual[1]
    assert "--allow-unauthenticated" not in actual[0]


@pytest.mark.parametrize("snapshot", ["", "latest", "--allow-unauthenticated"])
def test_apt_install_refuses_an_unpinned_snapshot(snapshot: str) -> None:
    done = subprocess.run(["bash", str(SCRIPT), snapshot, "git"], capture_output=True, text=True)
    assert done.returncode == 2
