# SPDX-License-Identifier: MIT
"""The documents, the consistency review and the math checks depend one way (B-05):
math checks on both, the consistency review on the documents, the documents on neither.
Every import counts, including those inside functions."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1] / "src" / "sandbox_science" / "orchestrator"
PACKAGE = "sandbox_science.orchestrator"


def _imports(package: str) -> set[tuple[str, str]]:
    """(module file, imported module) for every import in ``package``."""
    found: set[tuple[str, str]] = set()
    for path in sorted((ROOT / package).glob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                found.update((path.name, alias.name) for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                if node.level == 0:
                    found.add((path.name, node.module or ""))
                elif node.level == 1:
                    found.add((path.name, f"{PACKAGE}.{package}.{node.module or ''}"))
                else:
                    found.add((path.name, f"{PACKAGE}.{node.module or ''}"))
    return found


@pytest.mark.parametrize(
    ("package", "forbidden"),
    [
        ("documents", ("consistency", "mathchecks")),
        ("consistency", ("mathchecks",)),
    ],
)
def test_a_package_imports_nothing_that_depends_on_it(
    package: str, forbidden: tuple[str, ...]
) -> None:
    prefixes = tuple(f"{PACKAGE}.{name}" for name in forbidden)
    found = sorted(
        f"{module}: {target}" for module, target in _imports(package) if target.startswith(prefixes)
    )
    assert found == []
