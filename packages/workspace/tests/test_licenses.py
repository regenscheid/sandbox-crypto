# SPDX-License-Identifier: MIT
"""The web app's npm packages: the committed lockfile passes scripts/check_licenses.py
--npm, and packages that should not are refused."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[3]
LOCKFILE = ROOT / "web" / "workspace" / "package-lock.json"


def _script() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "check_licenses", ROOT / "scripts" / "check_licenses.py"
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


check = _script()


def package(**fields: Any) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "version": "1.0.0",
        "resolved": "https://registry.npmjs.org/a/-/a-1.0.0.tgz",
        "integrity": "sha512-AAAA",
        "license": "MIT",
    }
    entry.update(fields)
    return {k: v for k, v in entry.items() if v is not None}


def lockfile(**entries: dict[str, Any]) -> dict[str, Any]:
    return {
        "lockfileVersion": 3,
        "packages": {"": {"name": "web"}, **{f"node_modules/{k}": v for k, v in entries.items()}},
    }


def test_the_committed_lockfile_passes() -> None:
    app = json.loads((LOCKFILE.parent / "package.json").read_text())
    count, problems = check.npm_problems(json.loads(LOCKFILE.read_text()), app)
    assert problems == []
    assert count > 0
    assert check.main(["--npm", str(LOCKFILE)]) == 0


def test_allowed_packages_pass() -> None:
    count, problems = check.npm_problems(
        lockfile(
            a=package(),
            b=package(license="(MIT OR Apache-2.0)"),
            c=package(license="MPL-2.0"),
            fsevents=package(hasInstallScript=True, optional=True),
        )
    )
    assert (count, problems) == (4, [])


@pytest.mark.parametrize(
    ("entry", "reason"),
    [
        (package(license="GPL-3.0-only"), "GPL-3.0-only is not allowed"),
        (package(license="MIT AND GPL-2.0-or-later"), "is not allowed"),
        (package(license="SEE LICENSE IN LICENSE.md"), "is not allowed"),
        (package(license="BUSL-1.1 OR SSPL-1.0"), "is not allowed"),
        (package(license=None), "license not declared"),
        (package(resolved="https://evil.example/a-1.0.0.tgz"), "not the npm registry"),
        (package(resolved="git+ssh://git@github.com/a/a.git"), "not the npm registry"),
        (package(resolved=None, link=True), "not the npm registry"),
        (package(integrity="sha1-AAAA"), "no SHA-512 integrity"),
        (package(hasInstallScript=True), "needs an install script"),
    ],
)
def test_packages_that_break_a_rule_are_refused(entry: dict[str, Any], reason: str) -> None:
    _, problems = check.npm_problems(lockfile(a=entry))
    assert len(problems) == 1, problems
    assert reason in problems[0]
    assert problems[0].startswith("a@1.0.0: ")


@pytest.mark.parametrize(
    "lockfile_data",
    [
        {"lockfileVersion": 1, "dependencies": {}},
        {"lockfileVersion": 2, "packages": {"": {}}, "dependencies": {}},
        # npm reads `dependencies` when `packages` is missing, even in version 3.
        {
            "lockfileVersion": 3,
            "dependencies": {
                "left-pad": {"version": "1.3.0", "resolved": "https://evil.example/left-pad.tgz"}
            },
        },
        {"lockfileVersion": 3, "packages": {}},
        {"lockfileVersion": 3, "packages": {"": {}}, "dependencies": {}},
    ],
)
def test_lockfiles_npm_could_read_otherwise_are_refused(lockfile_data: dict[str, Any]) -> None:
    count, problems = check.npm_problems(lockfile_data)
    assert count == 0
    assert problems


@pytest.mark.parametrize(
    ("expression", "allowed"),
    [
        ("MIT", True),
        ("mit", True),
        ("(MIT OR Apache-2.0)", True),
        ("MIT AND (Apache-2.0 OR BSD-3-Clause)", True),
        ("Apache-2.0 AND MIT", True),
        ("MIT and Apache-2.0", True),
        ("Apache-2.0 WITH LLVM-exception", True),
        ("(MIT OR Apache-2.0) AND GPL-3.0-only", False),
        ("GPL-3.0-only AND (MIT OR Apache-2.0)", False),
        ("SSPL-1.0 AND (MIT OR ISC)", False),
        ("GPL-2.0-only WITH Classpath-exception-2.0", False),
        ("GPL-2.0+", False),
        ("MIT Apache-2.0", False),
        ("MIT OR", False),
        ("((MIT)", False),
        ("MIT)", False),
        ("", False),
        ("SEE LICENSE IN LICENSE.md", False),
        ("MIT/GPL", False),
    ],
)
def test_license_expressions_are_read_with_their_precedence(expression: str, allowed: bool) -> None:
    assert check.is_allowed(expression) is allowed


def test_installed_packages_must_name_the_lockfiles_license(tmp_path: Path) -> None:
    data = lockfile(a=package(), b=package(), c=package(optional=True))
    for name, license_ in (("a", "MIT"), ("b", "GPL-3.0-only")):
        directory = tmp_path / "node_modules" / name
        directory.mkdir(parents=True)
        (directory / "package.json").write_text(
            json.dumps({"name": name, "version": "1.0.0", "license": license_})
        )
    count, problems = check.installed_problems(data, tmp_path)
    assert count == 2
    assert problems == [
        "b@1.0.0: its package.json gives license 'GPL-3.0-only', the lockfile 'MIT'"
    ]
    (tmp_path / "node_modules" / "a" / "package.json").unlink()
    _, problems = check.installed_problems(data, tmp_path)
    assert "a@1.0.0: is not installed; run npm ci first" in problems


APP = {
    "name": "web",
    "dependencies": {"canvas": "file:stubs/no-canvas"},
    "overrides": {"canvas": "$canvas"},
}


def stubbed(**stub: Any) -> dict[str, Any]:
    """A lockfile whose `canvas` is the app's own stub, as an npm override links it."""
    data = lockfile(canvas={"resolved": "stubs/no-canvas", "link": True})
    data["packages"]["stubs/no-canvas"] = {"name": "canvas", "version": "0.0.0", **stub}
    return data


def test_a_stub_the_app_overrides_a_package_with_passes() -> None:
    assert check.npm_problems(stubbed(license="MIT"), APP) == (2, [])
    file_override = {**APP, "overrides": {"canvas": "file:stubs/no-canvas"}}
    assert check.npm_problems(stubbed(license="MIT"), file_override) == (2, [])


def _relinked(path: str) -> dict[str, Any]:
    data = lockfile(canvas={"resolved": path, "link": True})
    data["packages"][path] = {"name": "canvas", "version": "0.0.0", "license": "MIT"}
    return data


@pytest.mark.parametrize(
    ("data", "app", "reason"),
    [
        (stubbed(), APP, "license not declared"),
        (stubbed(license="GPL-3.0-only"), APP, "is not allowed"),
        # Without the app's package.json, or with a stub that replaces nothing, a local
        # package is just a package that is not from the registry.
        (stubbed(license="MIT"), None, "not the npm registry"),
        (stubbed(license="MIT"), {**APP, "overrides": {}}, "not the npm registry"),
        (
            stubbed(license="MIT"),
            {**APP, "overrides": {"canvas": "^1.0.0"}},
            "not the npm registry",
        ),
        (stubbed(license="MIT"), {**APP, "dependencies": {}}, "not the npm registry"),
        # A stub's entry gives a name, a version and a license, nothing more.
        (stubbed(license="MIT", hasInstallScript=True), APP, "not the npm registry"),
        (stubbed(license="MIT", dependencies={"a": "1.0.0"}), APP, "not the npm registry"),
        (stubbed(license="MIT", peerDependencies={"a": "1.0.0"}), APP, "not the npm registry"),
        (stubbed(license="MIT", bin={"a": "a.js"}), APP, "not the npm registry"),
        (
            stubbed(license="MIT", resolved="https://evil.example/x.tgz"),
            APP,
            "not the npm registry",
        ),
        (stubbed(license="MIT", name="other"), APP, "not the npm registry"),
        (lockfile(canvas={"resolved": "stubs/missing", "link": True}), APP, "not the npm registry"),
        (
            lockfile(canvas={"resolved": "stubs/no-canvas", "link": True, "integrity": "x"}),
            APP,
            "not the npm registry",
        ),
        *(
            (_relinked(path), APP, "not the npm registry")
            for path in ("stubs/../x", "stubs//x", "stubs/x/../../y", "/abs/stubs/x", "./stubs/x")
        ),
    ],
)
def test_stubs_that_break_a_rule_are_refused(
    data: dict[str, Any], app: dict[str, Any] | None, reason: str
) -> None:
    _, problems = check.npm_problems(data, app)
    assert any(reason in problem for problem in problems), problems


def installed_stub(root: Path, manifest: dict[str, Any] | None = None) -> Path:
    stub = root / "stubs" / "no-canvas"
    stub.mkdir(parents=True)
    fields = {"name": "canvas", "version": "0.0.0", "license": "MIT", "private": True}
    (stub / "package.json").write_text(json.dumps(manifest or fields))
    (root / "node_modules").mkdir()
    (root / "node_modules" / "canvas").symlink_to(Path("..") / "stubs" / "no-canvas")
    return stub


def test_an_installed_stub_must_be_the_linked_directory(tmp_path: Path) -> None:
    data = stubbed(license="MIT")
    installed_stub(tmp_path)
    assert check.installed_problems(data, tmp_path) == (1, [])
    (tmp_path / "node_modules" / "canvas").unlink()
    (tmp_path / "node_modules" / "canvas").mkdir()
    _, problems = check.installed_problems(data, tmp_path)
    assert problems == ["canvas@None: is not linked to stubs/no-canvas"]


@pytest.mark.parametrize(
    "extra",
    [
        {"dependencies": {"y": "1.0.0"}},
        {"optionalDependencies": {"y": "1.0.0"}},
        {"scripts": {"install": "node -e 0"}},
        {"bin": {"y": "y.js"}},
        {"main": "index.js"},
        {"exports": "./index.js"},
    ],
)
def test_an_installed_stub_declares_nothing_to_run(tmp_path: Path, extra: dict[str, Any]) -> None:
    fields = {"name": "canvas", "version": "0.0.0", "license": "MIT", **extra}
    installed_stub(tmp_path, fields)
    _, problems = check.installed_problems(stubbed(license="MIT"), tmp_path)
    assert any("which a stub may not" in problem for problem in problems), problems


def test_an_installed_stub_holds_only_its_package_json(tmp_path: Path) -> None:
    stub = installed_stub(tmp_path)
    (stub / "index.js").write_text("fetch('https://evil.example/');\n")
    _, problems = check.installed_problems(stubbed(license="MIT"), tmp_path)
    assert problems == [
        "stubs/no-canvas@0.0.0: holds ['index.js', 'package.json'], not only a package.json"
    ]


def test_an_installed_stub_is_reached_through_no_symbolic_link(tmp_path: Path) -> None:
    outside = tmp_path / "outside" / "evil"
    outside.mkdir(parents=True)
    (outside / "package.json").write_text(
        json.dumps({"name": "canvas", "version": "0.0.0", "license": "MIT"})
    )
    app = tmp_path / "app"
    (app / "stubs").mkdir(parents=True)
    (app / "stubs" / "no-canvas").symlink_to(outside)
    (app / "node_modules").mkdir()
    (app / "node_modules" / "canvas").symlink_to(Path("..") / "stubs" / "no-canvas")
    _, problems = check.installed_problems(stubbed(license="MIT"), app)
    assert "stubs/no-canvas@0.0.0: is reached through a symbolic link" in problems
