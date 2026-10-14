# SPDX-License-Identifier: MIT
"""The documents, the consistency review and the math checks depend one way (B-05):
math checks on both, the consistency review on the documents, the documents on neither.
Every import counts, including those inside functions."""

from __future__ import annotations

import ast
import sys
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
                parent = (
                    PACKAGE if node.level > 1 else f"{PACKAGE}.{package}" if node.level == 1 else ""
                )
                base = ".".join(part for part in (parent, node.module or "") if part)
                found.add((path.name, base))
                # "from .. import mathchecks" imports the package itself.
                found.update((path.name, f"{base}.{alias.name}") for alias in node.names)
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


@pytest.mark.parametrize(
    "line",
    [
        "from .. import mathchecks\n",
        "from sandbox_science.orchestrator import mathchecks\n",
        "from .. import consistency\n",
        "import sandbox_science.orchestrator.mathchecks as checks\n",
        "def later():\n    from ..mathchecks.jobs import MathJobs\n",
    ],
)
def test_the_layer_check_sees_every_form_of_import(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, line: str
) -> None:
    (tmp_path / "documents").mkdir()
    (tmp_path / "documents" / "store.py").write_text(line)
    monkeypatch.setattr(sys.modules[__name__], "ROOT", tmp_path)
    forbidden = ("consistency", "mathchecks")
    with pytest.raises(AssertionError):
        test_a_package_imports_nothing_that_depends_on_it("documents", forbidden)
