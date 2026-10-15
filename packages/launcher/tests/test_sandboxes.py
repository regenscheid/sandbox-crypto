# SPDX-License-Identifier: MIT
"""The research sandboxes: suspended on an explicit stop, through the worker, and shown.

The worker stand-in (fake_part.py) lists and suspends the environments in the OpenShell
stand-in's file, for its clients' projects only, as the worker does.
"""

from __future__ import annotations

import json
import signal
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.slow

OURS = {"environment_id": "env-crypto-1", "project_id": "crypto", "sandbox": "ss-1111"}
OTHER_PROJECT = {"environment_id": "env-other-1", "project_id": "other", "sandbox": "ss-2222"}


@pytest.fixture
def research(site: dict[str, Any], fake_openshell: Path, tmp_path: Path) -> dict[str, Any]:
    state = tmp_path / "openshell.json"
    state.write_text(
        json.dumps(
            {
                "providers": [],
                "environments": [
                    {**OURS, "state": "ready"},
                    {**OTHER_PROJECT, "state": "ready"},
                ],
                "sandboxes": {
                    "ss-1111": {"id": "a", "phase": "Ready"},
                    "ss-2222": {"id": "b", "phase": "Ready"},
                    "by-hand": {"id": "c", "phase": "Ready"},
                },
            }
        )
    )

    def launch(parts: dict[str, str] | None = None) -> Any:
        return site["launch"](parts, openshell={"cli": str(fake_openshell)})

    return {"launch": launch, "state": state, "journal": site["journal"]}


def journal(path: Path) -> list[str]:
    return path.read_text().splitlines()


def phases(state: Path) -> dict[str, str]:
    return {
        name: item["phase"] for name, item in json.loads(state.read_text())["sandboxes"].items()
    }


def test_ctrl_c_suspends_the_projects_sandboxes_before_the_worker_stops(
    research: dict[str, Any],
) -> None:
    launcher = research["launch"]()
    launcher.expect("Stop:")
    shown = launcher.command("status")
    assert shown.returncode == 0
    assert "ss-1111 (env-crypto-1, project crypto): running" in shown.stdout
    assert "ss-2222" not in shown.stdout  # another project's: not this launcher's to see
    launcher.process.send_signal(signal.SIGINT)
    assert launcher.finish() == 0
    assert "suspended the research sandboxes ss-1111 (env-crypto-1)" in launcher.output
    assert journal(research["journal"])[-4:] == [
        "sandbox-science-workspace stop",
        "sandbox-science-harness stop",
        "sandbox-science-worker suspend env-crypto-1",
        "sandbox-science-worker stop",
    ]
    # Another project's sandbox, and one the worker did not make, run on.
    assert phases(research["state"]) == {
        "ss-1111": "Stopped",
        "ss-2222": "Ready",
        "by-hand": "Ready",
    }
    shown = launcher.command("status")
    assert shown.returncode == 3
    assert "ss-1111 (env-crypto-1, project crypto): suspended" in shown.stdout


def test_down_keeps_them_on_request_and_suspends_them_later(research: dict[str, Any]) -> None:
    launcher = research["launch"]()
    launcher.expect("Stop:")
    kept = launcher.command("down", "--keep-sandboxes")
    assert kept.returncode == 0, kept.stdout + kept.stderr
    assert "kept the research sandboxes ss-1111 (env-crypto-1) running" in kept.stdout
    assert launcher.finish() == 0
    assert not any("suspend" in step for step in journal(research["journal"]))
    # With everything else stopped, status still shows the sandbox that runs.
    shown = launcher.command("status")
    assert shown.returncode == 4
    assert "ss-1111 (env-crypto-1, project crypto): running" in shown.stdout
    stopped = launcher.command("down")
    assert stopped.returncode == 0, stopped.stdout + stopped.stderr
    assert "starting the worker to suspend the research sandboxes ss-1111" in stopped.stdout
    assert journal(research["journal"])[-3:] == [
        "sandbox-science-worker start",
        "sandbox-science-worker suspend env-crypto-1",
        "sandbox-science-worker stop",
    ]
    assert phases(research["state"])["ss-1111"] == "Stopped"
    assert phases(research["state"])["ss-2222"] == "Ready"
    assert launcher.command("status").returncode == 3


def test_a_sandbox_still_in_use_is_named_and_down_fails(research: dict[str, Any]) -> None:
    launcher = research["launch"]({"sandbox-science-worker": "suspend-busy"})
    launcher.expect("Stop:")
    stopped = launcher.command("down")
    assert stopped.returncode == 1
    assert "these research sandboxes may still be running: ss-1111 (env-crypto-1)" in stopped.stdout
    assert launcher.finish() == 1
    assert journal(research["journal"])[-1] == "sandbox-science-worker stop"  # stopped anyway


def test_a_part_that_dies_leaves_the_sandboxes_running_and_says_so(
    research: dict[str, Any],
) -> None:
    launcher = research["launch"]({"sandbox-science-harness": "exit-later"})
    assert launcher.finish() == 1
    assert (
        "the research sandboxes ss-1111 (env-crypto-1) are still running: "
        "sandbox-science down stops them"
    ) in launcher.output
    assert not any("suspend" in step for step in journal(research["journal"]))
    assert phases(research["state"])["ss-1111"] == "Ready"


def test_status_says_plainly_when_it_cannot_check_them(research: dict[str, Any]) -> None:
    launcher = research["launch"]()
    launcher.expect("Stop:")
    assert launcher.command("down", "--keep-sandboxes").returncode == 0
    assert launcher.finish() == 0
    launcher.env["FAKE_PARTS"] = json.dumps({"openshell": "get-fails"})
    shown = launcher.command("status")
    assert shown.returncode == 4
    assert "the research sandboxes could not be checked" in shown.stdout
    assert "the gateway is not reachable" in shown.stdout
