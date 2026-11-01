# SPDX-License-Identifier: MIT
"""Rules for every test in the repository.

Tests marked ``integration`` need OpenShell, KVM, a cluster or the public registries.
They run only when ``SS_INTEGRATION=1``, so CI and ordinary local runs skip them. Tests
marked ``live`` call the deployment's real model gateway, for the labs; they run only
when ``SS_LIVE_GATEWAY`` names it.

After each test module, no process this run started may still run from the run's
temporary directory: an executor, a sandbox daemon, a kernel or a command they started
(``sandbox_science.testing.processes`` says how one is recognized). A module that leaves
one fails at its last test's teardown, and the processes are killed.
"""

import logging
import os
from collections.abc import Iterator

import pytest

from sandbox_science.contracts.logs import reset_logging
from sandbox_science.testing.processes import end_left_behind

LEFTOVER_GRACE = 10.0
"""Seconds a module's processes get to end after its last test, before they count."""


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    integration = os.environ.get("SS_INTEGRATION") == "1"
    live = bool(os.environ.get("SS_LIVE_GATEWAY"))
    skip = pytest.mark.skip(reason="integration test: set SS_INTEGRATION=1 to run it")
    unlive = pytest.mark.skip(reason="live gateway test: set SS_LIVE_GATEWAY to run it")
    for item in items:
        if not integration and item.get_closest_marker("integration") is not None:
            item.add_marker(skip)
        if not live and item.get_closest_marker("live") is not None:
            item.add_marker(unlive)


@pytest.fixture(autouse=True)
def logging_as_found() -> Iterator[None]:
    """A test that runs an entry point's ``main`` sets up logging for the whole process
    (``configure_logging``); the next test finds the root logger as it was."""
    root = logging.getLogger()
    level = root.level
    yield
    reset_logging()
    root.setLevel(level)


@pytest.fixture(scope="module", autouse=True)
def no_processes_left_behind(tmp_path_factory: pytest.TempPathFactory) -> Iterator[None]:
    yield
    if not os.path.isdir("/proc"):
        return
    left = end_left_behind(tmp_path_factory.getbasetemp(), LEFTOVER_GRACE)
    if left:
        listed = "\n".join(f"  {pid}: {line}" for pid, line in sorted(left.items()))
        pytest.fail(f"processes this module left running (now killed):\n{listed}", pytrace=False)
