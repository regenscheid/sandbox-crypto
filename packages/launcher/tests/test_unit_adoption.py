# SPDX-License-Identifier: MIT
"""``sandbox-science up`` with the worker in a unit that runs apart (``[worker] unit``).

The gateway checks the policy middleware when it starts, so on babbage the unit starts
before the gateway and so before ``up``: ``up`` waits for it, uses its worker, watches
it, and never stops it.
"""

from __future__ import annotations

import json
import os
import signal
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from sandbox_science.launcher.config import load_config

from .conftest import free_port
from .unit_support import Unit

pytestmark = pytest.mark.slow

WORKER = "sandbox-science-worker"
OURS = {"environment_id": "env-crypto-1", "project_id": "crypto", "sandbox": "ss-1111"}


def journal(path: Path) -> list[str]:
    return [line for line in path.read_text().splitlines() if not line.startswith("openshell ")]


class Adopted:
    def __init__(self, site: dict[str, Any], tmp_path: Path, fake_parts: Path) -> None:
        self.site = site
        self.tmp_path = tmp_path
        self.config_dir = tmp_path / "site"
        self.path = self.config_dir / "unit.toml"
        self.middleware = free_port(site["taken"])
        self.cache = free_port({*site["taken"], self.middleware})
        passed = '["FAKE_JOURNAL", "FAKE_PARTS", "FAKE_OPENSHELL"]'
        self.path.write_text(
            f'[unit]\nbin_dir = "{fake_parts}"\nstop_timeout = 5\nstart_timeout = 20\n'
            f"pass_env = {{ worker = {passed}, middleware = {passed}, cache = {passed} }}\n"
            '[worker]\nsettings_file = "state/worker/worker.toml"\n'
            f'[middleware]\nlisten = "127.0.0.1:{self.middleware}"\n'
            f'[cache]\nlisten = "127.0.0.1:{self.cache}"\n'
        )
        self.units: list[Unit] = []
        (tmp_path / "openshell.json").write_text(
            json.dumps(
                {
                    "providers": [],
                    "environments": [{**OURS, "state": "ready"}],
                    "sandboxes": {"ss-1111": {"id": "a", "phase": "Ready"}},
                }
            )
        )

    def prepare(self) -> str:
        prepared = self.site["launch"](unit=self.path, command="prepare")
        assert prepared.finish() == 0, prepared.output
        return prepared.output

    def start_unit(self) -> Unit:
        env = {
            **os.environ,
            "FAKE_JOURNAL": str(self.site["journal"]),
            "FAKE_PARTS": "{}",
            "FAKE_OPENSHELL": str(self.tmp_path / "openshell.json"),
        }
        unit = Unit(self.path, env)
        self.units.append(unit)
        unit.expect("the unit is ready")
        return unit

    def launch(self, **changes: Any) -> Any:
        return self.site["launch"](unit=self.path, **changes)


@pytest.fixture
def adopted(
    site: dict[str, Any], fake_parts: Path, fake_openshell: Path, tmp_path: Path
) -> Iterator[Adopted]:
    made = Adopted(site, tmp_path, fake_parts)
    yield made
    for unit in made.units:
        if unit.process.poll() is None:
            unit.process.kill()
            unit.process.wait(30)


def wait_for(condition: Callable[[], bool], what: str, timeout: float = 30) -> None:
    deadline = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < deadline, what
        time.sleep(0.1)


def test_prepare_writes_the_workers_settings_and_names_the_unit(adopted: Adopted) -> None:
    said = adopted.prepare()
    layout_root = load_config(adopted.config_dir / "sandbox-science.toml").state_dir
    assert f"worker_settings={layout_root}/worker/worker.toml" in said
    assert f"unit={adopted.path}" in said
    settings = layout_root / "worker/worker.toml"
    assert settings.stat().st_mode & 0o777 == 0o600
    assert 'pool_id = "local"' in settings.read_text()
    # Nothing started: no part, no journal entry.
    assert journal(adopted.site["journal"]) == []


def test_up_uses_the_units_worker_watches_it_and_leaves_it_running(adopted: Adopted) -> None:
    adopted.prepare()
    unit = adopted.start_unit()
    launcher = adopted.launch()
    launcher.expect("Sandbox Science is running.")
    steps = journal(adopted.site["journal"])
    assert steps.count(f"{WORKER} start") == 1  # the unit's: the launcher started none
    assert steps[:3] == [
        "sandbox-science-package-cache start",
        "sandbox-science-policy-middleware start",
        f"{WORKER} start",
    ]
    assert steps[3] == f"{WORKER} policy add crypto"
    assert "waiting for the unit" in launcher.output
    shown = launcher.command("status")
    assert shown.returncode == 0, shown.stdout
    assert "unit (runs apart" in shown.stdout
    for service in ("cache", "middleware", "worker"):
        assert f"    {service}: 127.0.0.1:" in shown.stdout
    assert "  worker: process" not in shown.stdout  # not one of the launcher's parts
    # A stop suspends the research sandboxes through the unit's worker, and stops the
    # launcher's parts, but not the unit.
    launcher.process.send_signal(signal.SIGINT)
    assert launcher.finish() == 0
    assert "suspended the research sandboxes ss-1111 (env-crypto-1)" in launcher.output
    steps = journal(adopted.site["journal"])
    assert f"{WORKER} suspend env-crypto-1" in steps
    assert f"{WORKER} stop" not in steps
    assert unit.process.poll() is None
    assert unit.stop() == 0
    assert journal(adopted.site["journal"])[-3:] == [
        f"{WORKER} stop",
        "sandbox-science-policy-middleware stop",
        "sandbox-science-package-cache stop",
    ]


def test_up_says_so_when_the_unit_is_not_running(adopted: Adopted) -> None:
    adopted.prepare()
    launcher = adopted.launch(start_timeout=2)
    assert launcher.finish() == 1
    assert "the unit is not healthy after 2 s (" in launcher.output
    assert "cache on 127.0.0.1" in launcher.output
    assert "is silent" in launcher.output
    assert journal(adopted.site["journal"]) == []


def test_up_does_not_start_while_one_service_of_the_unit_is_down(adopted: Adopted) -> None:
    adopted.prepare()
    env = {
        **os.environ,
        "FAKE_JOURNAL": str(adopted.site["journal"]),
        "FAKE_PARTS": "{}",
        "FAKE_OPENSHELL": str(adopted.tmp_path / "openshell.json"),
    }
    worker_only = Unit(adopted.path, env, "--only", "worker")
    adopted.units.append(worker_only)
    worker_only.expect("the unit is ready")
    launcher = adopted.launch(start_timeout=2)
    assert launcher.finish() == 1
    assert "the unit is not healthy after 2 s (" in launcher.output
    assert "cache on 127.0.0.1" in launcher.output
    assert "worker on 127.0.0.1" in launcher.output
    assert launcher.output.count("is silent") == 2  # the worker answers
    assert journal(adopted.site["journal"]) == [f"{WORKER} start"]


def test_the_launcher_stops_when_the_unit_stops_answering(adopted: Adopted) -> None:
    adopted.prepare()
    unit = adopted.start_unit()
    launcher = adopted.launch()
    launcher.expect("Sandbox Science is running.")
    assert unit.stop() == 0
    assert launcher.finish(timeout=60) == 1
    assert "the unit is no longer healthy" in launcher.output
    assert "Sandbox Science stopped" in launcher.output


def test_the_unit_must_run_the_launchers_worker_settings(adopted: Adopted) -> None:
    adopted.path.write_text(
        adopted.path.read_text().replace(
            '[worker]\nsettings_file = "state/worker/worker.toml"',
            '[worker]\nsettings_file = "elsewhere.toml"',
        )
    )
    launcher = adopted.launch()
    assert launcher.finish() == 1
    assert "[worker] must be settings_file = " in launcher.output
    assert "sandbox-science prepare writes" in launcher.output
    inline = adopted.path.read_text().replace(
        '[worker]\nsettings_file = "elsewhere.toml"',
        '[worker]\nlisten = "127.0.0.1:1"',
    )
    adopted.path.write_text(inline)
    again = adopted.launch()
    assert again.finish() == 1
    assert "[worker] must be settings_file = " in again.output


def test_down_after_a_killed_launcher_suspends_through_the_units_worker(
    adopted: Adopted,
) -> None:
    adopted.prepare()
    unit = adopted.start_unit()
    launcher = adopted.launch()
    launcher.expect("Sandbox Science is running.")
    launcher.process.kill()
    launcher.finish()
    stopped = launcher.command("down")
    assert stopped.returncode == 0, stopped.stdout + stopped.stderr
    steps = journal(adopted.site["journal"])
    assert f"{WORKER} suspend env-crypto-1" in steps
    # No second worker: the state directory's lock is the unit's worker's.
    assert steps.count(f"{WORKER} start") == 1
    assert "using the unit's worker to list and suspend the research sandboxes" in stopped.stdout
    assert "starting the worker" not in stopped.stdout
    assert unit.process.poll() is None
    assert unit.stop() == 0
