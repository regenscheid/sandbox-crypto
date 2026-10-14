# SPDX-License-Identifier: MIT
"""The repository check that keeps pyright's private-usage check on in product code."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[3]
PRODUCT = "packages/orchestrator/src/sandbox_science/orchestrator/example.py"
USE = "from sandbox_science.orchestrator.loop import _sections"


def hygiene() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "check_repo_hygiene", ROOT / "scripts" / "check_repo_hygiene.py"
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# Each of these would hide a private use from pyright in a product module.
HIDDEN = {
    "file reportPrivateUsage=false": f"# pyright: reportPrivateUsage=false\n{USE}",
    "file reportPrivateUsage=none": f"# pyright: reportPrivateUsage=none\n{USE}",
    "file reportPrivateUsage=warning": f"# pyright: reportPrivateUsage=warning\n{USE}",
    "file reportPrivateUsage=information": f"# pyright: reportPrivateUsage=information\n{USE}",
    "file pyright: basic": f"# pyright: basic\n{USE}",
    "file pyright: standard": f"# pyright: standard\n{USE}",
    "file type: ignore": f"# type: ignore\n{USE}",
    "line type: ignore": f"{USE}  # type: ignore",
    "line type: ignore[arg-type]": f"{USE}  # type: ignore[arg-type]",
    "line bare pyright: ignore": f"{USE}  # pyright: ignore",
    "line ignore[rule] without a reason": f"{USE}  # pyright: ignore[reportPrivateUsage]",
    "line ignore[rule] with only a noqa after it": (
        f"{USE}  # pyright: ignore[reportPrivateUsage]  # noqa: E501"
    ),
    "line ignore[rule] with only punctuation after it": (
        f"{USE}  # pyright: ignore[reportPrivateUsage] - :"
    ),
}


@pytest.mark.parametrize("name", sorted(HIDDEN))
def test_every_way_pyright_accepts_to_hide_a_private_use_is_refused(name: str) -> None:
    assert hygiene().pragma_problems({PRODUCT: HIDDEN[name]}), name


def test_strict_named_rules_and_a_reasoned_private_use_are_accepted() -> None:
    allowed = "\n".join(
        [
            "# pyright: strict",
            "x = int(y)  # pyright: ignore[reportUnknownArgumentType]",
            f"{USE}  # pyright: ignore[reportPrivateUsage] - the loop's own test seam",
        ]
    )
    assert hygiene().pragma_problems({PRODUCT: allowed}) == []
    tests = "packages/orchestrator/tests/test_example.py"
    assert hygiene().pragma_problems({tests: "# pyright: reportPrivateUsage=false"}) == []


def test_the_pyright_settings_the_rule_relies_on_are_kept() -> None:
    check = hygiene()
    assert check.settings_problems((ROOT / "pyproject.toml").read_text()) == []
    assert check.settings_problems("[tool.pyright]\n") == [
        'pyproject.toml: pyright needs strict = ["packages/*/src"]',
        "pyproject.toml: pyright needs enableTypeIgnoreComments = false",
    ]
