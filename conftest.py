# SPDX-License-Identifier: MIT
"""Rules for every test in the repository.

Tests marked ``integration`` need OpenShell, KVM, a cluster or the public registries.
They run only when ``SS_INTEGRATION=1``, so CI and ordinary local runs skip them.
"""

import os

import pytest


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if os.environ.get("SS_INTEGRATION") == "1":
        return
    skip = pytest.mark.skip(reason="integration test: set SS_INTEGRATION=1 to run it")
    for item in items:
        if item.get_closest_marker("integration") is not None:
            item.add_marker(skip)
