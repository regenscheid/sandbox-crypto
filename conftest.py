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

import os
from collections.abc import Iterator

import pytest

from sandbox_science.testing.processes import end_left_behind

LEFTOVER_GRACE = 10.0
"""Seconds a module's processes get to end after its last test, before they count."""


GIT_QUIET = {"maintenance.auto": "false", "gc.auto": "0"}
"""Git settings for every git the tests run: no automatic maintenance or garbage
collection, which git starts in the background after a commit or a fetch and which could
still be writing in a test's repository as its temporary directory is removed."""


def pytest_configure(config: pytest.Config) -> None:
    if "GIT_CONFIG_COUNT" in os.environ:
        return  # the runner's own settings stand; the skill imports quiet theirs anyway
    os.environ["GIT_CONFIG_COUNT"] = str(len(GIT_QUIET))
    for index, (key, value) in enumerate(GIT_QUIET.items()):
        os.environ[f"GIT_CONFIG_KEY_{index}"] = key
        os.environ[f"GIT_CONFIG_VALUE_{index}"] = value


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


@pytest.fixture(scope="module", autouse=True)
def no_processes_left_behind(tmp_path_factory: pytest.TempPathFactory) -> Iterator[None]:
    yield
    if not os.path.isdir("/proc"):
        return
    left = end_left_behind(tmp_path_factory.getbasetemp(), LEFTOVER_GRACE)
    if left:
        listed = "\n".join(f"  {pid}: {line}" for pid, line in sorted(left.items()))
        pytest.fail(f"processes this module left running (now killed):\n{listed}", pytrace=False)
