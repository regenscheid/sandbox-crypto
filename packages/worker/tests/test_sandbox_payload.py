# SPDX-License-Identifier: MIT
"""The files the worker ships into a sandbox (``SANDBOX_FILES``) run there on their own:
the sandbox's Python has the standard library and, in some images, jupyter_client and
pdf_oxide, but not the worker package. So each file imports only the standard library,
its siblings (by path, never by package), and those two, lazily, where it needs them."""

from __future__ import annotations

import ast
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from sandbox_science.worker.backends import SANDBOX_FILES

LAZY: dict[str, frozenset[str]] = {
    "executor.py": frozenset({"jupyter_client"}),
    "pdfhelper.py": frozenset({"pdf_oxide", "PIL", "cairosvg"}),
}
"""Packages a file may import inside a function, for a feature the image may lack."""
LOADS: dict[str, frozenset[str]] = {
    "executor.py": frozenset(
        {
            "binding_inventory",
            "capture",
            "executor_lifecycle",
            "executor_admission",
            "provisioning",
            "sandbox_files",
            "sandbox_processes",
            "sandbox_setup",
            "pdfhelper",
        }
    ),
    "daemon.py": frozenset({"executor", "request_frames"}),
}
"""The siblings each file loads or runs by path; the others load none."""
NAMES = {path.name for path in SANDBOX_FILES}
SIBLINGS = {name.removesuffix(".py") for name in NAMES}
REPOSITORY = Path(__file__).resolve().parents[3]


def imports(tree: ast.Module) -> list[tuple[str, bool, bool]]:
    """Each import as (top-level module, run when the module is imported, only where the
    worker package is: under ``TYPE_CHECKING`` or ``if __package__``, never in a sandbox).

    A function's or lambda's body runs only when called; a class body runs at import.
    ``importlib.import_module`` and ``__import__`` of a constant name count as imports.
    """
    found: list[tuple[str, bool, bool]] = []

    def add(module: str, depth: int, typing_only: bool) -> None:
        found.append((module.split(".")[0] or module, depth == 0, typing_only))

    def visit(nodes: list[ast.stmt] | list[ast.AST], depth: int, typing_only: bool) -> None:
        for child in nodes:
            if isinstance(child, ast.Import):
                for alias in child.names:
                    add(alias.name, depth, typing_only)
            elif isinstance(child, ast.ImportFrom):
                add("." * child.level + (child.module or ""), depth, typing_only)
            elif isinstance(child, ast.Call) and (named := dynamic_import(child)) is not None:
                add(named, depth, typing_only)
            elif isinstance(child, ast.If) and package_only(child.test):
                visit(child.body, depth, True)
                visit(child.orelse, depth, typing_only)
                continue
            deferred = isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda)
            visit(list(ast.iter_child_nodes(child)), depth + deferred, typing_only)

    visit(tree.body, 0, False)
    return found


def package_only(test: ast.expr) -> bool:
    """Whether a branch on ``test`` runs only where the worker package is."""
    match test:
        case ast.Name(id="TYPE_CHECKING" | "__package__"):
            return True
        case ast.Attribute(value=ast.Name(id="typing"), attr="TYPE_CHECKING"):
            return True
        case _:
            return False


def dynamic_import(call: ast.Call) -> str | None:
    """The constant module name an ``importlib.import_module`` or ``__import__`` call
    imports, or None."""
    match call.func:
        case ast.Name(id="__import__") | ast.Attribute(attr="import_module"):
            pass
        case _:
            return None
    match call.args:
        case [ast.Constant(value=str() as name), *_]:
            return name
        case _:
            return None


def problems(name: str, source: str) -> list[str]:
    """What a sandbox could not import from file ``name``."""
    found: list[str] = []
    for module, top_level, typing_only in imports(ast.parse(source)):
        if typing_only:
            continue  # never runs in a sandbox
        if module.startswith("."):
            # The package is not there in a sandbox: siblings load by path at run time.
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
        ("executor.py", "class Kernel:\n    import jupyter_client\n"),  # runs at import
        ("executor.py", "import importlib\nimportlib.import_module('aiohttp')\n"),
        ("executor.py", "__import__('aiohttp')\n"),
        ("executor.py", "def f():\n    __import__('sandbox_science.contracts')\n"),
        ("executor.py", "if not __package__:\n    from . import capture\n"),
        ("executor.py", "if TYPE_CHECKING:\n    pass\nelse:\n    from . import capture\n"),
        ("executor.py", "try:\n    import jupyter_client\nexcept ImportError:\n    pass\n"),
    ],
)
def test_the_check_refuses_what_a_sandbox_cannot_import(name: str, source: str) -> None:
    assert problems(name, source)


@pytest.mark.parametrize(
    ("name", "source"),
    [
        ("executor.py", "def f():\n    from jupyter_client.manager import KernelManager\n"),
        ("executor.py", "start = lambda: __import__('jupyter_client')\n"),
        ("executor.py", "import typing\nif typing.TYPE_CHECKING:\n    from . import capture\n"),
        ("executor.py", "if __package__:\n    importlib.import_module('sandbox_science.x')\n"),
        ("executor.py", "import tomllib\n"),
    ],
)
def test_the_check_accepts_what_a_sandbox_can_import(name: str, source: str) -> None:
    assert problems(name, source) == []


@pytest.mark.parametrize("path", SANDBOX_FILES, ids=lambda path: path.name)
def test_every_sibling_a_sandbox_file_loads_is_shipped(path: Path) -> None:
    text = path.read_text()
    loaded = set(re.findall(r'_sibling\("([a-z_]+)"\)', text))
    named = set(re.findall(r'with_name\("([a-z_]+)\.py"\)', text))
    # The patterns must find what each file is known to load, or they check nothing.
    assert loaded | named == LOADS.get(path.name, frozenset())
    assert loaded | named <= SIBLINGS, f"{path.name} loads {(loaded | named) - SIBLINGS}"


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


LOAD_EXECUTOR = """
import importlib.util, sys, types
planted = types.ModuleType("sandbox_science_sandbox_files")
sys.modules[planted.__name__] = planted
spec = importlib.util.spec_from_file_location("sandbox_science_executor", "executor.py")
executor = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = executor
try:
    spec.loader.exec_module(executor)
except OSError as exc:
    print("failed", exc, sys.modules.get(planted.__name__) is None)
else:
    print("loaded", executor.sandbox_files is planted, executor.sandbox_files.__file__)
"""


def run_shipped(directory: Path) -> str:
    """Load executor.py by path from ``directory``, as a sandbox does, after something
    else put a module under its sibling's name."""
    result = subprocess.run(
        [sys.executable, "-I", "-c", LOAD_EXECUTOR],
        cwd=directory,
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )
    return result.stdout.strip()


def test_a_sibling_is_loaded_from_its_file_and_never_left_half_built(tmp_path: Path) -> None:
    for path in SANDBOX_FILES:
        shutil.copy(path, tmp_path)
    # A module already registered under the sibling's name is not adopted.
    assert run_shipped(tmp_path) == f"loaded False {tmp_path / 'sandbox_files.py'}"
    # A sibling that fails part way is not left registered for a later load to find.
    broken = tmp_path / "sandbox_files.py"
    broken.write_text(broken.read_text() + "\nraise OSError('disk hiccup')\n")
    assert run_shipped(tmp_path) == "failed disk hiccup True"


@pytest.mark.slow
def test_standalone_payload_preserves_file_error_identity(tmp_path: Path) -> None:
    """Collaborators share the loaded file module's ToolError, even without the package."""
    payload = tmp_path / "payload"
    payload.mkdir()
    root = tmp_path / "workspace"
    root.mkdir()
    for path in SANDBOX_FILES:
        shutil.copy(path, payload)
    request = {"id": 1, "op": "read", "session": "s1", "path": "../outside"}
    completed = subprocess.run(
        [sys.executable, "-I", str(payload / "executor.py"), "--stdio", "--root", str(root)],
        input=json.dumps(request) + "\n",
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )
    reply = json.loads(completed.stdout)
    assert reply["ok"] is False
    assert reply["error"]["kind"] == "invalid_request"
