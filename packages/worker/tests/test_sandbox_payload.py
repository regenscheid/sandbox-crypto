# SPDX-License-Identifier: MIT
"""The files the worker ships into a sandbox (``SANDBOX_FILES``) run there on their own:
the sandbox's Python has the standard library and, in some images, jupyter_client and
pdf_oxide, but not the worker package. So each file imports only the standard library,
its siblings (by path, never by package), and those two, lazily, where it needs them."""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

import pytest

from sandbox_science.worker.backends import SANDBOX_FILES

LAZY: dict[str, frozenset[str]] = {
    "executor.py": frozenset({"jupyter_client"}),
    "pdfhelper.py": frozenset({"pdf_oxide"}),
}
"""Packages a file may import inside a function, for a feature the image may lack."""
NAMES = {path.name for path in SANDBOX_FILES}
SIBLINGS = {name.removesuffix(".py") for name in NAMES}
REPOSITORY = Path(__file__).resolve().parents[3]


def imports(tree: ast.Module) -> list[tuple[str, bool, bool]]:
    """Each import as (top-level module, at module level, only where the worker package
    is: under ``TYPE_CHECKING`` or ``if __package__``, never in a sandbox)."""
    found: list[tuple[str, bool, bool]] = []

    def visit(nodes: list[ast.stmt] | list[ast.AST], depth: int, typing_only: bool) -> None:
        for child in nodes:
            if isinstance(child, ast.Import):
                for alias in child.names:
                    found.append((alias.name.split(".")[0], depth == 0, typing_only))
            elif isinstance(child, ast.ImportFrom):
                module = "." * child.level + (child.module or "")
                found.append((module.split(".")[0] or module, depth == 0, typing_only))
            elif (
                isinstance(child, ast.If)
                and isinstance(child.test, ast.Name)
                and child.test.id in ("TYPE_CHECKING", "__package__")
            ):
                visit(child.body, depth, True)
                visit(child.orelse, depth, typing_only)
                continue
            nested = isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef)
            visit(list(ast.iter_child_nodes(child)), depth + nested, typing_only)

    visit(tree.body, 0, False)
    return found


def problems(name: str, source: str) -> list[str]:
    """What a sandbox could not import from file ``name``."""
    found: list[str] = []
    for module, top_level, typing_only in imports(ast.parse(source)):
        if module.startswith("."):
            # The package is not there in a sandbox: siblings load by path at run time.
            if not typing_only:
                found.append(f"{name} imports {module} where a sandbox runs it")
        elif module in sys.stdlib_module_names or module == "__future__":
            continue
        elif module not in LAZY.get(name, ()):
            found.append(f"{name} imports {module}")
        elif top_level:
            found.append(f"{name} imports {module} at module level")
    return found


@pytest.mark.parametrize("path", SANDBOX_FILES, ids=lambda path: path.name)
def test_a_sandbox_file_imports_only_the_standard_library(path: Path) -> None:
    assert problems(path.name, path.read_text()) == []


@pytest.mark.parametrize(
    ("name", "source"),
    [
        ("executor.py", "import aiohttp\n"),
        ("executor.py", "def f():\n    from sandbox_science.contracts import Event\n"),
        ("executor.py", "from . import capture\n"),
        ("executor.py", "import jupyter_client\n"),  # needed lazily, never at import
        ("capture.py", "def f():\n    import jupyter_client\n"),
    ],
)
def test_the_check_refuses_what_a_sandbox_cannot_import(name: str, source: str) -> None:
    assert problems(name, source)


@pytest.mark.parametrize("path", SANDBOX_FILES, ids=lambda path: path.name)
def test_every_sibling_a_sandbox_file_loads_is_shipped(path: Path) -> None:
    text = path.read_text()
    loaded = set(re.findall(r'_sibling\("([a-z_]+)"\)', text))
    named = set(re.findall(r'with_name\("([a-z_]+)\.py"\)', text))
    assert loaded <= SIBLINGS, f"{path.name} loads {loaded - SIBLINGS}"
    assert named <= SIBLINGS, f"{path.name} names {named - SIBLINGS}"


def test_the_image_checks_ship_the_same_files() -> None:
    check = ast.parse((REPOSITORY / "images/checks/check_image.py").read_text())
    listed = [
        ast.literal_eval(node.value)
        for node in check.body
        if isinstance(node, ast.Assign)
        and [ast.unparse(target) for target in node.targets] == ["WORKER_FILES"]
    ]
    assert len(listed) == 1
    assert set(listed[0]) == NAMES
    script = (REPOSITORY / "images/test.sh").read_text()
    copied = re.search(r"^for name in ([a-z_ ]+); do$", script, re.MULTILINE)
    assert copied is not None
    assert {f"{name}.py" for name in copied[1].split()} == NAMES
