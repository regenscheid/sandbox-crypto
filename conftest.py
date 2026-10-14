# SPDX-License-Identifier: MIT
"""Rules for every test in the repository.

Tests marked ``integration`` need OpenShell, KVM, a cluster or the public registries.
They run only when ``SS_INTEGRATION=1``, so CI and ordinary local runs skip them.

After each test module, no process may still run from the run's temporary directory:
an executor, a sandbox daemon, a kernel or a command they started. A module that
leaves one fails at its last test's teardown, and the processes are killed.
"""

import os
from collections.abc import Iterator

import pytest

from sandbox_science.testing.processes import end_left_behind

LEFTOVER_GRACE = 10.0
"""Seconds a module's processes get to end after its last test, before they count."""


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if os.environ.get("SS_INTEGRATION") == "1":
        return
    skip = pytest.mark.skip(reason="integration test: set SS_INTEGRATION=1 to run it")
    for item in items:
        if item.get_closest_marker("integration") is not None:
            item.add_marker(skip)


@pytest.fixture(scope="module", autouse=True)
def no_processes_left_behind(tmp_path_factory: pytest.TempPathFactory) -> Iterator[None]:
    yield
    if not os.path.isdir("/proc"):
        return
    left = end_left_behind(tmp_path_factory.getbasetemp(), LEFTOVER_GRACE)
    if left:
        listed = "\n".join(f"  {pid}: {line}" for pid, line in sorted(left.items()))
        pytest.fail(f"processes this module left running (now killed):\n{listed}", pytrace=False)
