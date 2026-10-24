# SPDX-License-Identifier: MIT
"""``sandbox-science-unit serve`` with stand-ins for the three services (fake_part.py).

The stand-ins listen where the real ones would and write each step to a journal, so the
tests see the order of starts and stops, and what each was given.
"""

from __future__ import annotations

import json
import os
import signal
import time
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from .conftest import free_port
from .unit_support import Unit

pytestmark = pytest.mark.slow

WORKER = "sandbox-science-worker"
MIDDLEWARE = "sandbox-science-policy-middleware"
CACHE = "sandbox-science-package-cache"


class Site:
    def __init__(self, tmp_path: Path, fake_parts: Path) -> None:
        self.directory = tmp_path / "unit"
        self.directory.mkdir()
        self.journal = tmp_path / "journal"
        self.journal.touch()
        taken: set[int] = set()
        addresses: dict[str, str] = {}
        for name in ("worker", "middleware", "cache"):
            port = free_port(taken)
            taken.add(port)
            addresses[name] = f"127.0.0.1:{port}"
        self.addresses = addresses
        self.worker, self.middleware, self.cache = (
            addresses["worker"],
            addresses["middleware"],
            addresses["cache"],
        )
        self.fake_parts = fake_parts
        self.started: list[Unit] = []

    def write(
        self,
        *,
        unit: str = "",
        stop_timeout: int = 5,
        pass_worker: tuple[str, ...] = (),
        sections: tuple[str, ...] = ("worker", "middleware", "cache"),
        extra: dict[str, str] | None = None,
    ) -> Path:
        passed = ["FAKE_JOURNAL", "FAKE_PARTS"]
        names = ", ".join(f'"{name}"' for name in passed)
        worker = ", ".join(f'"{name}"' for name in (*passed, *pass_worker))
        text = (
            f'[unit]\nbin_dir = "{self.fake_parts}"\nstop_timeout = {stop_timeout}\n'
            "start_timeout = 20\n"
            f"pass_env = {{ worker = [{worker}], middleware = [{names}], cache = [{names}] }}\n"
            f"{unit}\n"
        )
        for name in sections:
            text += f'[{name}]\nlisten = "{self.addresses[name]}"\n{(extra or {}).get(name, "")}\n'
        path = self.directory / "unit.toml"
        path.write_text(text)
        return path

    def start(
        self,
        config: Path,
        behaviours: dict[str, str] | None = None,
        *options: str,
        env: dict[str, str] | None = None,
    ) -> Unit:
        environment = {
            **os.environ,
            "FAKE_JOURNAL": str(self.journal),
            "FAKE_PARTS": json.dumps(behaviours or {}),
            **(env or {}),
        }
        unit = Unit(config, environment, *options)
        self.started.append(unit)
        return unit

    def steps(self) -> list[str]:
        return self.journal.read_text().splitlines()


@pytest.fixture
def site(tmp_path: Path, fake_parts: Path) -> Iterator[Site]:
    made = Site(tmp_path, fake_parts)
    yield made
    for unit in made.started:
        if unit.process.poll() is None:
            unit.process.kill()
            unit.process.wait(30)


def wait_for(condition: Callable[[], bool], what: str, timeout: float = 20) -> None:
    deadline = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < deadline, what
        time.sleep(0.05)


def test_the_unit_starts_cache_middleware_worker_and_stops_them_in_reverse(site: Site) -> None:
    unit = site.start(site.write())
    unit.expect("[unit] the unit is ready: cache, middleware, worker")
    assert site.steps() == [f"{CACHE} start", f"{MIDDLEWARE} start", f"{WORKER} start"]
    # One stream, each service's lines under its name.
    assert f"[cache] {CACHE} serving on {site.cache}" in unit.seen
    assert f"[middleware] {MIDDLEWARE} serving on {site.middleware}" in unit.seen
    assert f"[worker] worker API on {site.worker}" in unit.seen
    # Healthy is all three answering.
    shown = unit.status()
    assert shown.returncode == 0, shown.stdout
    assert shown.stdout.count("answers") == 3
    assert unit.stop() == 0
    assert site.steps()[3:] == [f"{WORKER} stop", f"{MIDDLEWARE} stop", f"{CACHE} stop"]
    assert unit.output.rstrip().endswith("[unit] the unit stopped")
    # Nothing answers once it has stopped: the unit is unhealthy.
    assert unit.status().returncode == 1


def test_a_service_stopping_by_itself_stops_the_others_and_fails_the_unit(site: Site) -> None:
    unit = site.start(site.write(), {WORKER: "exit-later"})
    unit.expect("the unit is ready")
    assert unit.finish() == 1
    assert site.steps() == [
        f"{CACHE} start",
        f"{MIDDLEWARE} start",
        f"{WORKER} start",
        f"{WORKER} exits",
        f"{MIDDLEWARE} stop",
        f"{CACHE} stop",
    ]
    assert "the worker stopped by itself, with status 5" in unit.output
    assert "stopping the unit" in unit.output
    assert unit.output.rstrip().endswith("[unit] the unit stopped")


def test_the_middleware_stopping_stops_the_worker_before_the_cache(site: Site) -> None:
    unit = site.start(site.write(), {MIDDLEWARE: "exit-later"})
    assert unit.finish() == 1
    assert site.steps()[-3:] == [f"{MIDDLEWARE} exits", f"{WORKER} stop", f"{CACHE} stop"]
    assert "the middleware stopped by itself, with status 5" in unit.output


def test_a_service_that_cannot_start_leaves_the_later_ones_unstarted(site: Site) -> None:
    unit = site.start(site.write(), {MIDDLEWARE: "exit-at-start"})
    assert unit.finish() == 1
    assert site.steps() == [f"{CACHE} start", f"{MIDDLEWARE} exits", f"{CACHE} stop"]
    assert "stopped as it started, with status 4" in unit.output
    # What it said is shown with the error, and was in the stream under its name.
    assert f"[middleware] {MIDDLEWARE}: cannot start, as asked" in unit.seen
    assert "its last lines:" in unit.output
    assert "[unit] the unit is ready" not in unit.output


def test_a_service_that_ignores_the_stop_is_killed_after_the_stop_timeout(site: Site) -> None:
    unit = site.start(site.write(stop_timeout=1), {WORKER: "ignore-term"})
    unit.expect("the unit is ready")
    assert unit.stop() == 0
    assert "the worker did not stop in time; killing it" in unit.output
    assert site.steps()[3:] == [f"{MIDDLEWARE} stop", f"{CACHE} stop"]


def test_inline_sections_become_private_files_that_are_removed_at_stop(site: Site) -> None:
    run = site.directory / "run"
    config = site.write(unit=f'runtime_dir = "{run}"')
    unit = site.start(config)
    unit.expect("the unit is ready")
    files = sorted(path.name for path in run.iterdir())
    assert files == [".unit-cache.toml", ".unit-middleware.toml", ".unit-worker.toml"]
    for name in files:
        assert (run / name).stat().st_mode & 0o777 == 0o600
    assert f'listen = "{site.worker}"' in (run / ".unit-worker.toml").read_text()
    assert unit.stop() == 0
    assert list(run.iterdir()) == []


def test_a_settings_file_runs_as_it_is_and_no_copy_is_made(site: Site) -> None:
    settings = site.directory / "worker.toml"
    settings.write_text(f'listen = "{site.worker}"\n')
    config = site.directory / "unit.toml"
    config.write_text(
        site.write()
        .read_text()
        .replace(
            f'[worker]\nlisten = "{site.worker}"\n', '[worker]\nsettings_file = "worker.toml"\n'
        )
    )
    unit = site.start(config)
    unit.expect("the unit is ready")
    assert not (site.directory / ".unit-worker.toml").exists()
    assert (site.directory / ".unit-middleware.toml").exists()
    assert unit.stop() == 0
    assert site.steps()[:3] == [f"{CACHE} start", f"{MIDDLEWARE} start", f"{WORKER} start"]


def test_only_runs_one_service_of_the_file(site: Site) -> None:
    unit = site.start(site.write(), None, "--only", "middleware")
    unit.expect("[unit] the unit is ready: middleware")
    assert site.steps() == [f"{MIDDLEWARE} start"]
    assert unit.status("--only", "middleware").returncode == 0
    assert unit.status().returncode == 1  # the other two do not answer
    assert unit.stop() == 0
    assert site.steps() == [f"{MIDDLEWARE} start", f"{MIDDLEWARE} stop"]


def test_each_service_gets_only_the_variables_the_config_passes(site: Site) -> None:
    path = site.write(pass_worker=("PASSED_ON",))
    unit = site.start(path, None, env={"LEAK_CHECK": "1", "PASSED_ON": "1"})
    unit.expect("the unit is ready")
    assert unit.stop() == 0
    steps = site.steps()
    assert f"{WORKER} saw PASSED_ON" in steps
    assert not any("LEAK_CHECK" in step for step in steps)
    assert f"{MIDDLEWARE} saw PASSED_ON" not in steps


def test_restart_true_starts_a_service_that_stopped_again_instead_of_stopping_the_unit(
    site: Site,
) -> None:
    unit = site.start(site.write(unit="restart = true"), {WORKER: "exit-later"})
    unit.expect("the unit is ready")
    wait_for(lambda: site.steps().count(f"{WORKER} start") == 2, "the worker started again")
    assert unit.process.poll() is None
    unit.expect("the worker stopped, with status 5; starting it again")
    unit.process.send_signal(signal.SIGTERM)
    assert unit.finish() == 0
    assert site.steps().count(f"{MIDDLEWARE} start") == 1


def test_a_second_unit_on_the_same_addresses_does_not_start(site: Site) -> None:
    config = site.write()
    first = site.start(config)
    first.expect("the unit is ready")
    second = site.start(config)
    assert second.finish() == 1
    assert "something else listens on" in second.output
    assert first.stop() == 0
