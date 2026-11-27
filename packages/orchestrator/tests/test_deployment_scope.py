# SPDX-License-Identifier: MIT
"""``project create`` says when the deployment's control service and worker will not act
for the project: the list control recorded at its start does not name it."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from sandbox_science.orchestrator import cli, deployment_scope

POLICY = "sha256:" + "a" * 64


def create(state: Path, project: str) -> int:
    return cli.main(
        ["project", "create", "--state-dir", str(state), "--project", project,
         "--profile", "minimal", "--policy", POLICY],
    )  # fmt: skip


def test_a_project_the_deployment_does_not_list_is_recorded_with_a_warning(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    state = tmp_path / "state"
    deployment_scope.record(state, ["crypto", "lattice"])
    assert create(state, "snova") == 0
    found = capsys.readouterr()
    assert found.out.startswith("created: ")
    assert json.loads(found.out.removeprefix("created: "))["project_id"] == "snova"
    assert found.err.startswith("warning: project 'snova' is recorded")
    assert "(crypto, lattice)" in found.err
    assert "[[projects]]" in found.err
    assert deployment_scope.GUIDE in found.err
    # A second create of the same project says so again: it still will not run.
    assert create(state, "snova") == 0
    found = capsys.readouterr()
    assert found.out.startswith("exists: ")
    assert "warning: project 'snova'" in found.err


def test_a_listed_project_or_no_recorded_list_gives_no_warning(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    listed = tmp_path / "listed"
    deployment_scope.record(listed, ["crypto"])
    assert create(listed, "crypto") == 0
    assert capsys.readouterr().err == ""
    # No control service has started with this state directory: nothing is known.
    assert create(tmp_path / "fresh", "crypto") == 0
    assert capsys.readouterr().err == ""


@pytest.mark.parametrize("content", [b"{not json", b'{"projects": []}', b"x" * 20000])
def test_a_damaged_list_is_never_read_as_a_list(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], content: bytes
) -> None:
    state = tmp_path / "state"
    state.mkdir()
    (state / deployment_scope.SCOPE_FILE).write_bytes(content)
    assert create(state, "crypto") == 0
    err = capsys.readouterr().err
    assert "whether this deployment runs project 'crypto' is unknown" in err


def test_the_list_is_replaced_whole(tmp_path: Path) -> None:
    deployment_scope.record(tmp_path, ["a1"])
    deployment_scope.record(tmp_path, ["b1", "c1"])
    assert deployment_scope.read(tmp_path) == (frozenset({"b1", "c1"}), None)
    assert [p.name for p in tmp_path.iterdir()] == [deployment_scope.SCOPE_FILE]
