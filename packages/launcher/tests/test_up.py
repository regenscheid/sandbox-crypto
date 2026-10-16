# SPDX-License-Identifier: MIT
"""``sandbox-science up``, ``down`` and ``status``, with stand-ins for the parts.

The stand-ins (fake_part.py) listen where the real parts would and write each step to a
journal, so the tests see the order of starts and stops. No OpenShell is involved.
"""

from __future__ import annotations

import json
import os
import signal
import socket
import stat
import subprocess
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from sandbox_science.launcher.config import load_config
from sandbox_science.launcher.instance import alive, boot_id, read
from sandbox_science.launcher.state import Layout
from sandbox_science.launcher.supervise import start_time

pytestmark = pytest.mark.slow

LOGIN_TOKEN = "fake-login-token-0123456789abcdef"
STARTED = [
    "sandbox-science-worker start",
    "sandbox-science-worker policy add crypto",
    "sandbox-science-harness project show crypto",
    "sandbox-science-harness project create crypto",
    "sandbox-science-harness project revise crypto",
    "sandbox-science-harness session open crypto main",
    "sandbox-science-harness start",
    "sandbox-science-workspace start",
]


def journal(path: Path) -> list[str]:
    return path.read_text().splitlines()


def wait_for(condition: Callable[[], bool], what: str, timeout: float = 15) -> None:
    deadline = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < deadline, what
        time.sleep(0.1)


def test_up_imports_and_pins_the_skill_packs_before_the_run_service(
    site: dict[str, Any],
) -> None:
    """SK-28: the packs a project names are imported and pinned once the projects are
    made; the embedding model reaches the import, with the key by its path, and the run
    service."""
    launcher = site["launch"](
        projects=[{"id": "crypto", "profile": "minimal", "skills": ["ss"]}],
        skills={
            "embedding_model": "embed",
            "packs": {"ss": {"git": "https://git.example/ss.git"}},
        },
    )
    launcher.expect("Sandbox Science is running.")
    steps = journal(site["journal"])
    opened = steps.index("sandbox-science-harness session open crypto main")
    assert steps[opened + 1 : opened + 7] == [
        "sandbox-science-harness skills list crypto",
        "sandbox-science-harness skills list ss",
        "sandbox-science-harness skills import crypto ss",  # its calls count for crypto
        "sandbox-science-harness skills embed with embed",
        "sandbox-science-harness skills pin crypto ss",
        "sandbox-science-harness embeds with embed",
    ]
    assert steps[opened + 7] == "sandbox-science-harness start"
    assert f"skills: ss imported at {'c' * 12}, 1 skill" in launcher.output
    assert f"skills: crypto pins ss at {'c' * 12}" in launcher.output
    launcher.process.send_signal(signal.SIGINT)
    assert launcher.finish() == 0


STOPPED = [
    "sandbox-science-workspace stop",
    "sandbox-science-harness stop",
    "sandbox-science-worker stop",
]


def test_up_runs_until_ctrl_c_and_then_stops_the_parts_in_reverse(site: dict[str, Any]) -> None:
    launcher = site["launch"]()
    launcher.expect("Sandbox Science is running.")
    launcher.expect("Stop:")
    assert journal(site["journal"]) == STARTED
    layout = Layout(load_config(launcher.config).state_dir)
    # The login address is in a private file, and nowhere else the launcher writes.
    assert LOGIN_TOKEN in layout.login.read_text()
    assert stat.S_IMODE(layout.login.stat().st_mode) == 0o600
    assert str(layout.login) in launcher.output
    assert LOGIN_TOKEN not in launcher.output
    assert not any(LOGIN_TOKEN in log.read_text() for log in layout.logs.iterdir())
    shown = launcher.command("status")
    assert shown.returncode == 0, shown.stdout + shown.stderr
    for part in ("worker", "run-service", "workspace"):
        assert f"  {part}: process" in shown.stdout
    assert shown.stdout.count("answers") == 2
    launcher.process.send_signal(signal.SIGINT)
    assert launcher.finish() == 0
    assert "Sandbox Science stopped" in launcher.output
    assert journal(site["journal"]) == [*STARTED, *STOPPED]
    assert not layout.login.exists()
    assert not layout.record.exists()
    assert launcher.command("status").returncode == 3


def test_closing_the_terminal_stops_the_parts_in_order(site: dict[str, Any]) -> None:
    launcher = site["launch"]()
    launcher.expect("Stop:")
    launcher.process.send_signal(signal.SIGHUP)
    assert launcher.finish() == 0
    assert journal(site["journal"]) == [*STARTED, *STOPPED]


def test_down_stops_it_and_a_second_up_is_refused(site: dict[str, Any]) -> None:
    launcher = site["launch"]()
    launcher.expect("Sandbox Science is running.")
    again = launcher.command("up")
    assert again.returncode == 1
    assert "already runs from" in again.stderr
    stopped = launcher.command("down")
    assert stopped.returncode == 0, stopped.stderr
    assert "Sandbox Science stopped" in stopped.stdout
    assert launcher.finish() == 0
    assert journal(site["journal"])[-1] == "sandbox-science-worker stop"
    assert "Sandbox Science is not running" in launcher.command("down").stdout


def test_a_second_up_moves_the_existing_project_to_the_policy(site: dict[str, Any]) -> None:
    """Lab 39: ``project create`` refuses a project that exists on another policy, so
    a project the control store has is only revised, as after a change to the policy."""
    first = site["launch"]()
    first.expect("Sandbox Science is running.")
    assert first.command("down").returncode == 0
    assert first.finish() == 0
    started = len(journal(site["journal"]))
    second = site["launch"]()
    second.expect("Sandbox Science is running.")
    projects = [line for line in journal(site["journal"])[started:] if " project " in line]
    assert projects == [
        "sandbox-science-harness project show crypto",
        "sandbox-science-harness project revise crypto",
    ]
    assert second.command("down").returncode == 0
    assert second.finish() == 0


def test_down_signals_only_the_launcher_its_record_names(site: dict[str, Any]) -> None:
    launcher = site["launch"]()
    launcher.expect("Stop:")
    layout = Layout(load_config(launcher.config).state_dir)
    kept = layout.record.read_text()
    forged = json.loads(kept)
    forged["started"] += 1  # the same process ID, started at another time: not the launcher
    layout.record.write_text(json.dumps(forged))
    refused = launcher.command("down")
    assert refused.returncode == 1
    assert "its record does not name it" in refused.stderr
    assert launcher.process.poll() is None
    layout.record.write_text(kept)
    assert launcher.command("down").returncode == 0
    assert launcher.finish() == 0


@pytest.mark.parametrize(
    ("parts", "message", "stops"),
    [
        (
            {"sandbox-science-workspace": "exit-at-start"},
            "the workspace stopped as it started, with status 4",
            ["sandbox-science-harness stop", "sandbox-science-worker stop"],
        ),
        (
            {"sandbox-science-harness": "exit-later"},
            "the run service stopped by itself, with status 5",
            ["sandbox-science-workspace stop", "sandbox-science-worker stop"],
        ),
        (
            {"sandbox-science-worker": "grant-fails"},
            "the policy grant failed, with status 2",
            ["sandbox-science-worker stop"],
        ),
    ],
)
def test_a_part_that_fails_stops_the_rest_and_is_reported(
    site: dict[str, Any], parts: dict[str, str], message: str, stops: list[str]
) -> None:
    launcher = site["launch"](parts)
    assert launcher.finish() == 1
    assert message in launcher.output
    if "exit-at-start" in parts.values():
        assert "cannot start, as asked" in launcher.output  # the end of its log
    if "grant-fails" in parts.values():
        assert "the policy store is not there" in launcher.output
    assert journal(site["journal"])[-len(stops) :] == stops


def test_an_address_something_else_holds_is_refused_before_starting(
    site: dict[str, Any],
) -> None:
    with socket.socket() as squatter:
        squatter.bind(("127.0.0.1", site["worker"]))
        squatter.listen()
        launcher = site["launch"]()
        assert launcher.finish() == 1
    assert f"something else listens on 127.0.0.1:{site['worker']}" in launcher.output
    assert journal(site["journal"]) == []


def test_each_part_gets_only_the_environment_its_table_names(site: dict[str, Any]) -> None:
    launcher = site["launch"](
        env={"LEAK_CHECK": "an exported key", "PASSED_ON": "1"},
        pass_env={"run_service": ["PASSED_ON"]},
    )
    launcher.expect("Stop:")
    seen = [line for line in journal(site["journal"]) if " saw " in line]
    assert seen == ["sandbox-science-harness saw PASSED_ON"]  # not the workspace


def test_a_state_directory_open_to_others_is_refused_before_it_is_read(
    site: dict[str, Any],
) -> None:
    launcher = site["launch"]()
    launcher.expect("Stop:")
    launcher.process.send_signal(signal.SIGINT)
    assert launcher.finish() == 0
    layout = Layout(load_config(launcher.config).state_dir)
    layout.root.chmod(0o755)
    try:
        for command in ("down", "status"):
            refused = launcher.command(command)
            assert refused.returncode == 1
            assert "is open to other users (mode 755)" in refused.stderr
    finally:
        layout.root.chmod(0o700)


def test_a_part_that_ignores_sigterm_is_killed_after_its_stop_timeout(
    site: dict[str, Any],
) -> None:
    launcher = site["launch"]({"sandbox-science-harness": "ignore-term"}, stop_timeout=1)
    launcher.expect("Sandbox Science is running.")
    launcher.process.send_signal(signal.SIGTERM)
    assert launcher.finish() == 0
    assert "the run service did not stop in time; killing it" in launcher.output
    assert "sandbox-science-harness stop" not in journal(site["journal"])
    assert journal(site["journal"])[-1] == "sandbox-science-worker stop"


def test_a_child_in_another_process_group_goes_with_its_part(site: dict[str, Any]) -> None:
    launcher = site["launch"]({"sandbox-science-workspace": "child-in-own-group"})
    launcher.expect("Stop:")
    (line,) = [line for line in journal(site["journal"]) if " child " in line]
    child = int(line.rsplit(" ", 1)[1])
    assert os.getpgid(child) == child  # not the part's group
    launcher.process.send_signal(signal.SIGINT)
    assert launcher.finish() == 0
    wait_for(lambda: start_time(child) is None, "the child outlived its part")


def test_parts_die_with_a_launcher_killed_outright(site: dict[str, Any]) -> None:
    launcher = site["launch"]()
    launcher.expect("Stop:")
    launcher.process.kill()
    launcher.finish()
    wait_for(
        lambda: all(line in journal(site["journal"]) for line in STOPPED),
        "a part outlived the launcher",
    )
    assert launcher.command("status").returncode == 3


def test_a_part_left_behind_is_shown_and_stopped_by_down(site: dict[str, Any]) -> None:
    launcher = site["launch"]({"sandbox-science-harness": "ignore-term"})
    launcher.expect("Stop:")
    layout = Layout(load_config(launcher.config).state_dir)
    record = read(layout)
    assert record is not None
    assert [part.name for part in record.parts] == ["worker", "run-service", "workspace"]
    launcher.process.kill()
    launcher.finish()
    (harness,) = [part for part in record.parts if part.name == "run-service"]
    wait_for(lambda: "sandbox-science-worker stop" in journal(site["journal"]), "worker")
    assert alive(harness, record)  # it ignored the SIGTERM it got when the launcher died
    shown = launcher.command("status")
    assert shown.returncode == 4
    assert "but the run-service an earlier launcher started still runs" in shown.stdout
    stopped = launcher.command("down")
    assert "stopped the run-service" in stopped.stdout
    assert not alive(harness, record)
    assert not layout.record.exists()


def test_a_child_that_outlived_its_part_is_found_and_stopped_by_down(
    site: dict[str, Any],
) -> None:
    launcher = site["launch"]({"sandbox-science-workspace": "child-stays"})
    launcher.expect("Stop:")
    (line,) = [line for line in journal(site["journal"]) if " child " in line]
    child = int(line.rsplit(" ", 1)[1])
    launcher.process.kill()
    launcher.finish()
    # The workspace dies with the launcher; its child, which has no death signal, does not.
    wait_for(lambda: "sandbox-science-workspace stop" in journal(site["journal"]), "workspace")
    assert start_time(child) is not None
    shown = launcher.command("status")
    assert shown.returncode == 4
    assert "but the workspace an earlier launcher started still runs" in shown.stdout
    stopped = launcher.command("down")
    assert "stopped the workspace" in stopped.stdout
    wait_for(lambda: start_time(child) is None, "the child outlived down")


def test_a_recorded_process_from_another_boot_or_time_is_left_alone(
    site: dict[str, Any],
) -> None:
    stranger = subprocess.Popen(["sleep", "60"], start_new_session=True)
    try:
        launcher = site["launch"]()  # for a config and a state directory
        launcher.expect("Stop:")
        launcher.process.send_signal(signal.SIGINT)
        assert launcher.finish() == 0
        layout = Layout(load_config(launcher.config).state_dir)
        started = start_time(stranger.pid)
        assert started is not None
        for boot, when in (("another-boot", started), (boot_id(), started + 1)):
            part = {"name": "worker", "pid": stranger.pid, "started": when, "command": "sleep",
                    "address": None}  # fmt: skip
            layout.record.write_text(
                json.dumps(
                    {
                        "pid": 1,
                        "started": 0,
                        "boot": boot,
                        "started_at": "then",
                        "config": "x",
                        "gui": None,
                        "parts": [part],
                    }
                )
            )
            assert launcher.command("down").returncode == 0
            assert stranger.poll() is None
    finally:
        stranger.kill()
        stranger.wait()
