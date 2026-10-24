# SPDX-License-Identifier: MIT
"""OpenShell's release is named in one place, and nothing else spells it in code."""

from __future__ import annotations

import ast
import re
import subprocess
from pathlib import Path

import pytest

from sandbox_science.contracts import openshell_version
from sandbox_science.contracts.openshell_version import (
    OPENSHELL_VERSION,
    OPENSHELL_VERSION_OUTPUT,
    pinned_version,
)

ROOT = Path(__file__).resolve().parents[3]
DEFINITION = "packages/contracts/src/sandbox_science/contracts/openshell_version.py"
STRAY = re.compile(r"(?<![\w.])0\.1\.[35](?![0-9])")
"""The two releases the code has known: a literal of either, in code, is a pin that the
constant does not move."""
ALLOWED = {
    DEFINITION: "defines it",
    "images/Containerfile": "the build argument, which cannot import; "
    "test_the_containerfile_and_the_example_config_agree checks it",
    "examples/sandbox-science.toml": "an example install path; the same test checks it",
}


def tracked() -> list[str]:
    done = subprocess.run(
        ["git", "-C", str(ROOT), "ls-files", "-z"],
        capture_output=True,
        text=True,
        check=True,
    )
    return [name for name in done.stdout.split("\0") if name]


def code_files(names: list[str]) -> tuple[list[str], list[str]]:
    """The tracked Python files that run (not tests, not generated code), and the other
    files whose non-comment lines are configuration or commands."""
    python: list[str] = []
    text: list[str] = []
    for name in names:
        parts = name.split("/")
        if "tests" in parts or "_proto" in parts or name == "conftest.py":
            continue
        if name.endswith(".py") and (
            (parts[0] == "packages" and "src" in parts) or parts[0] in ("scripts", "images")
        ):
            python.append(name)
        elif (
            (name.startswith("scripts/") and name.endswith(".sh"))
            or (
                parts[0] == "images"
                and (name.endswith(".sh") or parts[-1].startswith("Containerfile"))
            )
            or (parts[0] == "examples" and name.endswith((".toml", ".yaml", ".yml", ".json")))
            or (name.startswith(".github/workflows/") and name.endswith((".yml", ".yaml")))
        ):
            text.append(name)
    return python, text


def string_literals(source: str) -> list[tuple[int, str]]:
    """Each string constant of a Python module with its line, leaving out the strings that
    stand as statements: docstrings, and the attribute docstrings under a constant."""
    tree = ast.parse(source)
    documentation = {
        id(node.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)
    }
    return sorted(
        (node.lineno, node.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in documentation
    )


def test_the_constant_is_one_release_number() -> None:
    assert re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", OPENSHELL_VERSION)
    assert f"openshell {OPENSHELL_VERSION}" == OPENSHELL_VERSION_OUTPUT
    # The scripts that cannot import it read this very line (personal_up.sh with sed,
    # mac_local.py with a regular expression).
    line = f'OPENSHELL_VERSION: Final = "{OPENSHELL_VERSION}"'
    assert line in (ROOT / DEFINITION).read_text(encoding="utf-8").splitlines()
    assert openshell_version.__file__ == str(ROOT / DEFINITION)


@pytest.mark.parametrize(
    ("output", "pinned"),
    [
        (f"openshell {OPENSHELL_VERSION}", True),
        (f"openshell {OPENSHELL_VERSION}\n", True),
        (f"openshell-gateway v{OPENSHELL_VERSION}", True),
        (f"openshell {OPENSHELL_VERSION}0", False),
        (f"openshell {OPENSHELL_VERSION}-pre.1", False),
        (f"openshell {OPENSHELL_VERSION}.1", False),
        (f"openshell 1{OPENSHELL_VERSION}", False),
        ("openshell 0.1.3", False),
        ("openshell", False),
        ("", False),
    ],
)
def test_only_the_pinned_release_matches(output: str, pinned: bool) -> None:
    assert pinned_version(output) is pinned


def test_no_code_spells_the_release_out() -> None:
    python, text = code_files(tracked())
    assert len(python) > 100  # the scan finds the repository's code
    assert "scripts/personal_up.sh" in text
    found: list[str] = []
    for name in python:
        if name in ALLOWED:
            continue
        source = (ROOT / name).read_text(encoding="utf-8")
        found += [
            f"{name}:{line}: {value[:60]!r}"
            for line, value in string_literals(source)
            if STRAY.search(value)
        ]
    for name in text:
        if name in ALLOWED:
            continue
        lines = (ROOT / name).read_text(encoding="utf-8").splitlines()
        found += [
            f"{name}:{number}: {line.strip()[:60]!r}"
            for number, line in enumerate(lines, 1)
            if not line.lstrip().startswith("#") and STRAY.search(line)
        ]
    assert not found, (
        "OpenShell's release is OPENSHELL_VERSION in contracts/openshell_version.py; "
        "use it instead of a literal in:\n" + "\n".join(found)
    )


def test_the_scan_catches_a_literal() -> None:
    found = string_literals('x = "openshell-0.1.5/bin"\ny = f"v{x}0.1.3"\n')
    assert (1, "openshell-0.1.5/bin") in found
    assert (2, "0.1.3") in found
    docs = '"""v0.1.5 docs"""\ndef f():\n    """0.1.3"""\nX = 1\n"""about 0.1.3"""\n'
    assert string_literals(docs) == []
    assert STRAY.search("openshell-0.1.5/bin")
    assert not STRAY.search("0.1.50")
    assert not STRAY.search("10.1.5")


def test_the_containerfile_and_the_example_config_agree() -> None:
    containerfile = (ROOT / "images/Containerfile").read_text(encoding="utf-8")
    argument = re.search(r"^ARG OPENSHELL_VERSION=(\S+)$", containerfile, re.MULTILINE)
    assert argument is not None
    assert argument[1] == OPENSHELL_VERSION
    example = (ROOT / "examples/sandbox-science.toml").read_text(encoding="utf-8")
    installs = re.findall(r"openshell-([0-9.]+)/bin", example)
    assert installs
    assert set(installs) == {OPENSHELL_VERSION}
