# SPDX-License-Identifier: MIT
"""``session open``: a session recorded without a run, so the workspace can chat in it."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from sandbox_science.orchestrator import cli

POLICY = "sha256:" + "a" * 64


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
