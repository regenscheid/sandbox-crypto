# SPDX-License-Identifier: MIT
"""The research sandboxes: suspended on an explicit stop, through the worker, and shown.

The worker stand-in (fake_part.py) lists and suspends the environments in the OpenShell
stand-in's file, for its clients' projects only, as the worker does.
"""

from __future__ import annotations

import json
import signal
import time
from pathlib import Path
from typing import Any

import pytest

from sandbox_science.launcher.config import load_config
from sandbox_science.launcher.state import Layout

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

    return {"launch": launch, "state": state, "journal": site["journal"], "spawn": site["spawn"]}


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


def set_state(state: Path, change: Any) -> None:
    found = json.loads(state.read_text())
    change(found)
    state.write_text(json.dumps(found))


def wait_for(condition: Any, what: str, timeout: float = 15) -> None:
    deadline = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < deadline, what
        time.sleep(0.1)


def test_a_sandbox_listed_as_suspended_but_running_is_suspended_again(
    research: dict[str, Any],
) -> None:
    # An earlier stop that failed: the worker's record says stopped, the sandbox runs.
    set_state(research["state"], lambda found: found["environments"][0].update(state="stopped"))
    launcher = research["launch"]()
    launcher.expect("Stop:")
    launcher.process.send_signal(signal.SIGINT)
    assert launcher.finish() == 0
    assert "sandbox-science-worker suspend env-crypto-1" in journal(research["journal"])
    assert phases(research["state"])["ss-1111"] == "Stopped"


def test_down_after_a_killed_launcher_lists_them_with_the_worker(
    research: dict[str, Any],
) -> None:
    launcher = research["launch"]()
    launcher.expect("Stop:")
    # The one the launcher listed was suspended since (idle suspension), and a new one
    # was made after its last list; then the launcher is killed.
    second = {"environment_id": "env-crypto-2", "project_id": "crypto", "sandbox": "ss-3333"}

    def add(found: dict[str, Any]) -> None:
        found["environments"][0]["state"] = "stopped"
        found["sandboxes"]["ss-1111"]["phase"] = "Stopped"
        found["environments"].append({**second, "state": "ready"})
        found["sandboxes"]["ss-3333"] = {"id": "d", "phase": "Ready"}

    set_state(research["state"], add)
    launcher.process.kill()
    launcher.finish()
    wait_for(lambda: "sandbox-science-worker stop" in journal(research["journal"]), "worker")
    stopped = launcher.command("down")
    assert stopped.returncode == 0, stopped.stdout + stopped.stderr
    assert "starting the worker to list and suspend the research sandboxes" in stopped.stdout
    # The list the launcher kept says nothing of env-crypto-2: only the worker does.
    assert "sandbox-science-worker suspend env-crypto-2" in journal(research["journal"])
    assert phases(research["state"]) == {
        "ss-1111": "Stopped",
        "ss-2222": "Ready",
        "by-hand": "Ready",
        "ss-3333": "Stopped",
    }


def test_sandboxes_not_checked_are_said_to_be_unknown(research: dict[str, Any]) -> None:
    launcher = research["launch"]({"sandbox-science-worker": "exit-at-start"})
    assert launcher.finish() == 1
    assert "the research sandboxes were not checked" in launcher.output


def test_status_writes_nothing(research: dict[str, Any]) -> None:
    launcher = research["launch"]()
    launcher.expect("Stop:")
    layout = Layout(load_config(launcher.config).state_dir)
    wait_for(layout.sandboxes.exists, "the first list")
    before = layout.sandboxes.stat().st_mtime_ns
    assert launcher.command("status").returncode == 0
    assert layout.sandboxes.stat().st_mtime_ns == before


def test_a_second_ctrl_c_stops_waiting_for_the_suspension(research: dict[str, Any]) -> None:
    launcher = research["launch"]({"sandbox-science-worker": "suspend-slow"})
    launcher.expect("Stop:")
    launcher.process.send_signal(signal.SIGINT)
    launcher.expect("suspending the research sandboxes")
    started = time.monotonic()
    launcher.process.send_signal(signal.SIGINT)
    assert launcher.finish() == 1
    assert time.monotonic() - started < 15  # not the suspension's 20 s
    assert "the launcher stopped waiting for the suspension" in launcher.output
    assert journal(research["journal"])[-1] == "sandbox-science-worker stop"


def test_down_running_the_worker_alone_is_shown_and_stops_on_a_signal(
    research: dict[str, Any],
) -> None:
    launcher = research["launch"]()
    launcher.expect("Stop:")
    assert launcher.command("down", "--keep-sandboxes").returncode == 0
    assert launcher.finish() == 0
    env = {**launcher.env, "FAKE_PARTS": json.dumps({"sandbox-science-worker": "suspend-slow"})}
    downing = research["spawn"](launcher.config, env, "down")
    downing.expect("starting the worker to suspend the research sandboxes")
    wait_for(
        lambda: journal(research["journal"]).count("sandbox-science-worker start") == 2,
        "the worker alone",
    )
    shown = launcher.command("status")
    assert shown.returncode == 4
    assert "sandbox-science down (process" in shown.stdout
    waiting = research["spawn"](launcher.config, env, "down")  # waits for the first
    waiting.expect("is suspending the research sandboxes: waiting for it")
    downing.process.send_signal(signal.SIGHUP)
    assert downing.finish() == 1
    assert "no longer waiting for the suspension" in downing.output
    waiting.finish()
    assert journal(research["journal"])[-1] == "sandbox-science-worker stop"


def test_a_worker_of_another_protocol_is_refused(research: dict[str, Any]) -> None:
    launcher = research["launch"]({"sandbox-science-worker": "old-protocol"})
    launcher.expect("Stop:")
    stopped = launcher.command("down")
    assert stopped.returncode == 1
    assert "version mismatch: the worker reports protocol '4'" in stopped.stdout
    assert launcher.finish() == 1
