# SPDX-License-Identifier: MIT
"""The repository check that keeps pyright's private-usage check on in product code."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

ROOT = Path(__file__).resolve().parents[3]


def hygiene() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "check_repo_hygiene", ROOT / "scripts" / "check_repo_hygiene.py"
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_a_file_wide_disable_or_an_unexplained_ignore_is_refused_in_product_code() -> None:
    check = hygiene()
    product = "packages/orchestrator/src/sandbox_science/orchestrator/example.py"
    sources = {
        product: "\n".join(
            [
                "# pyright: reportPrivateUsage=false",
                "x = store._read()  # pyright: ignore[reportPrivateUsage]",
                "y = store._read()  # pyright: ignore[reportPrivateUsage] - the parser's own cache",
            ]
        ),
        "packages/orchestrator/tests/test_example.py": "# pyright: reportPrivateUsage=false",
    }
    assert check.pragma_problems(sources) == [
        f"{product}:1: private usage is checked in every file",
        f"{product}:2: say why this line uses a private name",
    ]
