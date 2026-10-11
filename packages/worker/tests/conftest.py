# SPDX-License-Identifier: MIT
"""Fixtures for every worker test module.

Test modules cannot import this file (``--import-mode=importlib``); they only
use its fixtures.
"""

import pytest


@pytest.fixture(autouse=True)
def isolated_jupyter(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Hide the host's own kernelspecs (a real SageMath, say) from local executors."""
    monkeypatch.setenv("JUPYTER_DATA_DIR", str(tmp_path_factory.mktemp("jupyter-data")))
    monkeypatch.delenv("JUPYTER_PATH", raising=False)
