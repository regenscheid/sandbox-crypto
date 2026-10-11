# SPDX-License-Identifier: MIT
"""The base profile recipes, checked without Docker (docs/IMAGES.md).

The pins are exact, the lockfiles cover both architectures with a SHA-256 for every package,
the declared OpenShell filesystem paths are a valid targets section, the Landlock emulation in
test.sh matches the policy compiler, and the license helpers that gate the minimal image behave.
"""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import yaml

from sandbox_science.policy.openshell import (
    SANDBOX_DEVICES_READ_ONLY,
    SANDBOX_DEVICES_READ_WRITE,
    SandboxFilesystem,
)

IMAGES = Path(__file__).resolve().parents[1]
PROFILES = ("minimal", "crypto-base")
PLATFORMS = ["linux-64", "linux-aarch64"]


def _load(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


report = _load("image_report", IMAGES / "common" / "image_report.py")
checks = _load("check_image", IMAGES / "checks" / "check_image.py")


def _containerfile_args() -> dict[str, str]:
    text = (IMAGES / "Containerfile").read_text()
    return dict(re.findall(r"^ARG (\w+)=(\S+)$", text, flags=re.MULTILINE))


def _lockfile(profile: str) -> dict[str, Any]:
    return yaml.safe_load((IMAGES / profile / "conda-lock.yml").read_text())


# Pins.


def test_every_download_in_the_containerfile_is_pinned() -> None:
    args = _containerfile_args()
    assert re.fullmatch(
        r"docker\.io/library/ubuntu:24\.04@sha256:[0-9a-f]{64}", args["UBUNTU_IMAGE"]
    )
    assert re.fullmatch(r"\d{8}T\d{6}Z", args["UBUNTU_SNAPSHOT"])
    for name in ("MICROMAMBA_SHA256_AMD64", "MICROMAMBA_SHA256_ARM64"):
        assert re.fullmatch(r"[0-9a-f]{64}", args[name])
    for name in ("G6K_COMMIT", "LATTICE_ESTIMATOR_COMMIT"):
        assert re.fullmatch(r"[0-9a-f]{40}", args[name])


def test_python_requirements_are_hashed() -> None:
    text = (IMAGES / "crypto-base" / "requirements.txt").read_text()
    requirements = [line for line in text.splitlines() if re.match(r"^[A-Za-z]", line)]
    assert requirements
    for requirement in requirements:
        assert re.fullmatch(r"[A-Za-z0-9_.-]+==\S+ \\", requirement)
    assert len(re.findall(r"--hash=sha256:[0-9a-f]{64}", text)) == 2 * len(requirements)


@pytest.mark.parametrize("profile", PROFILES)
def test_lockfiles_pin_every_package_for_both_architectures(profile: str) -> None:
    lock = _lockfile(profile)
    assert lock["metadata"]["platforms"] == PLATFORMS
    packages = lock["package"]
    for package in packages:
        assert package["url"].startswith("https://conda.anaconda.org/conda-forge/")
        assert re.fullmatch(r"[0-9a-f]{64}", package["hash"]["sha256"])
    environment = yaml.safe_load((IMAGES / profile / "environment.yml").read_text())
    wanted = {spec.split()[0] for spec in environment["dependencies"]}
    for platform in PLATFORMS:
        names = {package["name"] for package in packages if package["platform"] == platform}
        assert wanted <= names, f"{platform} lacks {wanted - names}"


@pytest.mark.parametrize("profile", PROFILES)
def test_each_profile_locks_the_kernel_packages(profile: str) -> None:
    names = {package["name"] for package in _lockfile(profile)["package"]}
    assert {"python", "ipykernel", "jupyter_client", "pip"} <= names
    if profile == "crypto-base":
        assert {"sage", "sagelib", "fpylll", "fplll", "pari", "libflint", "cython"} <= names


# The OpenShell view.


def test_the_declared_filesystem_paths_are_a_valid_targets_section() -> None:
    section = yaml.safe_load((IMAGES / "common" / "sandbox-filesystem.yml").read_text())
    filesystem = SandboxFilesystem.model_validate(section)
    assert "/opt/sandbox-science" in filesystem.read_write


def test_the_landlock_emulation_adds_the_compilers_devices() -> None:
    text = (IMAGES / "test.sh").read_text()

    def array(name: str) -> tuple[str, ...]:
        found = re.search(rf"^{name}=\((.*)\)$", text, flags=re.MULTILINE)
        assert found is not None, name
        return tuple(found.group(1).split())

    assert array("devices_ro") == SANDBOX_DEVICES_READ_ONLY
    assert array("devices_rw") == SANDBOX_DEVICES_READ_WRITE


def test_the_check_plan_names_methods_of_the_checks() -> None:
    for profile in PROFILES:
        for name in checks.planned(profile, landlock=True, network=True):
            assert callable(getattr(checks.Checks, name.replace("-", "_")))
    assert "landlock" not in checks.planned("minimal", landlock=False, network=False)
    assert "sage-kernel" not in checks.planned("minimal", landlock=True, network=True)


# Licenses.


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        ("MIT", "MIT"),
        ("MIT and PSF-2.0", "MIT AND PSF-2.0"),
        ("LGPL-2.1-only or MPL-1.1", "LGPL-2.1-only OR MPL-1.1"),
        ("(MIT OR Apache-2.0) AND BSD-3-Clause", "(MIT OR Apache-2.0) AND BSD-3-Clause"),
        ("GPL-3.0-only WITH GCC-exception-3.1", "GPL-3.0-only WITH GCC-exception-3.1"),
        ("GPLv2+", "GPL-2.0-or-later"),
        ("BSD", "LicenseRef-BSD"),
        ("Public Domain", "LicenseRef-Public-Domain"),
        ("LGPL 3", "LicenseRef-LGPL-3"),
        ("MIT OR", "LicenseRef-MIT-OR"),
        ("", "NOASSERTION"),
        (None, "NOASSERTION"),
    ],
)
def test_licenses_are_normalized_to_spdx(given: str | None, expected: str) -> None:
    assert report.normalize(given) == expected


@pytest.mark.parametrize(
    ("expression", "allowed"),
    [
        ("MIT", True),
        ("Apache-2.0 AND MIT", True),
        ("MIT OR Apache-2.0", True),
        ("GPL-2.0-or-later OR MIT", True),
        ("(GPL-2.0-only OR MIT) AND BSD-3-Clause", True),
        ("GPL-3.0-only", False),
        ("MIT AND GPL-3.0-only", False),
        ("GPL-3.0-only WITH GCC-exception-3.1", False),
        ("LGPL-2.1-or-later", False),
        ("AGPL-3.0-only", False),
        ("LicenseRef-BSD", False),
        ("NOASSERTION", False),
    ],
)
def test_the_permissive_policy(expression: str, allowed: bool) -> None:
    assert report.permissive(expression) is allowed


def test_only_named_runtime_libraries_escape_the_permissive_policy() -> None:
    packages = [
        {"name": "readline", "version": "8.3", "license": "GPL-3.0-only"},
        {"name": "libgcc", "version": "16.2.0", "license": "GPL-3.0-only WITH GCC-exception-3.1"},
        {"name": "pari", "version": "2.17.3", "license": "GPL-2.0-or-later"},
        {"name": "zeromq", "version": "4.3.5", "license": "MPL-2.0"},
    ]
    assert report.check_policy(packages, []) == ["pari 2.17.3: GPL-2.0-or-later"]
    assert report.check_policy([], [{"name": "g6k", "version": "1", "license": "GPL-2.0"}])


@pytest.mark.parametrize("profile", PROFILES)
def test_the_license_labels_are_spdx_expressions(profile: str) -> None:
    lines = report.read_licenses(IMAGES / profile / "licenses.txt")
    assert report.UBUNTU in lines
    expression = report.label(lines)
    assert report.is_spdx(report.tokens(expression))
