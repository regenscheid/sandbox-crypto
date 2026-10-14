# SPDX-License-Identifier: MIT
"""The base profile checks (images/test.sh) against built images, one test per check and mode.

Skipped unless ``SS_IMAGE_TESTS`` names images that Docker can see, for example
``SS_IMAGE_TESTS="sandbox-science/minimal:abc1234 sandbox-science/crypto-base:abc1234"``. CI
has neither Docker images nor the variable, so these tests skip there. Each image's profile
comes from its label. ``NETWORK=0`` skips the runtime installs, as for test.sh.
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
TEST_SH = ROOT / "images" / "test.sh"
PROFILE_LABEL = "io.github.regenscheid.sandbox-science.profile"
MODES = ("container", "landlock")

pytestmark = pytest.mark.images

Results = dict[tuple[str, str], dict[str, Any]]


def _check_image() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "check_image", ROOT / "images" / "checks" / "check_image.py"
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _profile(image: str) -> str | None:
    """The image's profile, or None when Docker cannot see the image."""
    docker = shutil.which("docker")
    if docker is None:
        return None
    label = f'{{{{index .Config.Labels "{PROFILE_LABEL}"}}}}'
    found = subprocess.run(
        [docker, "image", "inspect", "--format", label, image],
        capture_output=True,
        text=True,
        check=False,
    )
    if found.returncode != 0:
        return None
    return found.stdout.strip() or None


def _cases() -> list[Any]:
    images = os.environ.get("SS_IMAGE_TESTS", "").replace(",", " ").split()
    if not images:
        reason = "SS_IMAGE_TESTS names no images"
        return [pytest.param("", "", "", marks=pytest.mark.skip(reason=reason), id="no-images")]
    planned = _check_image().planned
    network = os.environ.get("NETWORK", "1") != "0"
    cases: list[Any] = []
    for image in images:
        profile = _profile(image)
        if profile is None:
            reason = f"Docker cannot see {image}"
            cases.append(
                pytest.param(image, "", "", marks=pytest.mark.skip(reason=reason), id=image)
            )
            continue
        for mode in MODES:
            for check in planned(profile, landlock=mode == "landlock", network=network):
                cases.append(pytest.param(image, mode, check, id=f"{image}-{mode}-{check}"))
    return cases


@pytest.fixture(scope="session")
def image_results() -> Callable[[str], Results]:
    """Run test.sh once per image and keep each check's result by mode and name."""
    cache: dict[str, Results] = {}

    def results(image: str) -> Results:
        if image not in cache:
            profile = _profile(image)
            assert profile is not None
            run = subprocess.run(
                ["bash", str(TEST_SH), profile, image],
                capture_output=True,
                text=True,
                check=False,
                timeout=3600,
            )
            found: Results = {}
            for line in run.stdout.splitlines():
                if line.startswith("{"):
                    record = json.loads(line)
                    found[(record["mode"], record["check"])] = record
            if not found:
                pytest.fail(f"test.sh reported nothing:\n{run.stderr[-3000:]}")
            cache[image] = found
        return cache[image]

    return results


@pytest.mark.parametrize(("image", "mode", "check"), _cases())
def test_image_check(
    image: str, mode: str, check: str, image_results: Callable[[str], Results]
) -> None:
    record = image_results(image).get((mode, check))
    assert record is not None, f"{check} did not run in {mode} mode"
    assert record["ok"], record["detail"]
