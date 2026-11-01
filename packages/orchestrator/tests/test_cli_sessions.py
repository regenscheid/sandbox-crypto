# SPDX-License-Identifier: MIT
"""``session open``: a session recorded without a run, so the workspace can chat in it."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from sandbox_science.orchestrator import cli

POLICY = "sha256:" + "a" * 64
CA = ("--worker-ca-file", "ca.crt")


@pytest.fixture
def state(tmp_path: Path) -> Path:
    state = tmp_path / "state"
    created = cli.main(
        ["project", "create", "--state-dir", str(state), "--project", "p1",
         "--profile", "minimal", "--policy", POLICY],
    )  # fmt: skip
    assert created == 0
    return state


def sessions(state: Path, capsys: pytest.CaptureFixture[str]) -> dict[str, dict[str, object]]:
    capsys.readouterr()
    assert cli.main(["project", "show", "--state-dir", str(state), "--project", "p1"]) == 0
    return json.loads(capsys.readouterr().out)["sessions"]


def test_a_session_opens_without_a_run_and_stays_as_it_is(
    state: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    opening = ["session", "open", "--state-dir", str(state), "--project", "p1"]
    assert cli.main([*opening, "--session", "main"]) == 0
    assert "session main is open" in capsys.readouterr().out
    first = sessions(state, capsys)["main"]
    assert cli.main([*opening, "--session", "main"]) == 0
    assert sessions(state, capsys)["main"] == first
    assert cli.main([*opening, "--session", "own", "--dedicated"]) == 0
    found = sessions(state, capsys)
    assert re.fullmatch(r"p1-own-[0-9a-f]{12}", str(found["own"]["environment_id"]))
    assert found["main"]["environment_id"] != found["own"]["environment_id"]


def test_a_session_keeps_its_environment_and_needs_a_project(
    state: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    opening = ["session", "open", "--state-dir", str(state), "--session", "main"]
    assert cli.main([*opening, "--project", "p1"]) == 0
    assert cli.main([*opening, "--project", "p1", "--dedicated"]) == 1
    assert "a session keeps its environment" in capsys.readouterr().err
    assert cli.main([*opening, "--project", "p2"]) == 1


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        (["close"], "session close needs --worker and --token-file"),
        (["open", "--worker", "http://127.0.0.1:8100"], "session open needs no worker"),
        (
            ["close", "--worker", "http://127.0.0.1:8100", *("--token-file", "t"), *CA],
            "--worker-ca-file needs an https:// --worker",
        ),
    ],
)
def test_close_needs_the_worker_and_open_does_not(
    state: Path, capsys: pytest.CaptureFixture[str], arguments: list[str], message: str
) -> None:
    action, *rest = arguments
    with pytest.raises(SystemExit):
        cli.main(
            ["session", action, "--state-dir", str(state), "--project", "p1",
             "--session", "main", *rest],
        )  # fmt: skip
    assert message in capsys.readouterr().err


def test_close_waits_for_a_running_research_tree(
    state: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """``session close`` acts with the session's current authority, which is a live
    run's while its research tree runs: it refuses then, naming the work, instead of
    stopping that run's kernels and child environments under it."""
    from sandbox_science.orchestrator.continuity.records import WorkRecords
    from sandbox_science.orchestrator.projects import ProjectStore

    common = ["--state-dir", str(state), "--project", "p1", "--session", "main"]
    assert cli.main(["session", "open", *common]) == 0
    run = "20261031T120000000000Z-run-live"
    store = ProjectStore(state)
    try:
        owner = WorkRecords(store.control).admit("p1", "main", run)
        assert WorkRecords(store.control).get("p1", "main", owner.work).state == "running"
    finally:
        store.close()
    token = tmp_path / "token"
    token.write_text("test-secret-token\n")
    capsys.readouterr()
    closing = ["session", "close", *common, "--worker", "http://127.0.0.1:9", *("--token-file",
               str(token))]  # fmt: skip
    assert cli.main(closing) == 1
    assert f"research work {run} is running (run {run}); stop it" in capsys.readouterr().err
