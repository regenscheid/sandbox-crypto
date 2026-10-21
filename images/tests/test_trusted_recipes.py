# SPDX-License-Identifier: MIT
"""Unpublished service recipe inputs and actual local-only wrapper invocations."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from .test_recipes import IMAGES, _containerfile_args, _recipe_inputs, _recipe_packages, _stages

PROFILES = ("trusted-app", "trusted-worker")


@pytest.mark.parametrize("profile", PROFILES)
@pytest.mark.parametrize(
    "option",
    [
        "--push",
        "--push=true",
        "--output=type=registry",
        "--cache-to=type=registry,ref=any",
        "--tag=registry.example/image",
    ],
)
def test_trusted_build_rejects_export_options_before_invoking_tools(
    tmp_path: Path, profile: str, option: str
) -> None:
    done = subprocess.run(
        ["bash", str(IMAGES / "build_trusted.sh"), profile, option],
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin:/bin"},
    )
    assert done.returncode == 2
    assert "local load only" in done.stderr


@pytest.mark.parametrize("profile", PROFILES)
def test_publication_entrypoint_refuses_unpublished_service_profiles(profile: str) -> None:
    done = subprocess.run(
        ["bash", str(IMAGES / "publish_arch.sh"), profile, "registry.example/image", "1"],
        capture_output=True,
        text=True,
    )
    assert done.returncode == 2
    assert "unknown profile" in done.stderr


@pytest.mark.parametrize("profile", PROFILES)
def test_trusted_build_only_loads_local_images_and_exports_locked_scoped_dependencies(
    tmp_path: Path, profile: str
) -> None:
    calls = tmp_path / "calls.jsonl"
    commands = tmp_path / "bin"
    commands.mkdir()
    for name in ("uv", "docker"):
        script = commands / name
        script.write_text(
            f"#!{sys.executable}\nimport json,sys\nfrom pathlib import Path\n"
            f"with Path({str(calls)!r}).open('a') as output: "
            f"output.write(json.dumps([{name!r},*sys.argv[1:]])+'\\n')\n"
            "if '--output-file' in sys.argv:\n"
            " Path(sys.argv[sys.argv.index('--output-file')+1]).write_text('synthetic-lock')\n"
        )
        script.chmod(0o700)
    done = subprocess.run(
        ["bash", str(IMAGES / "build_trusted.sh"), profile],
        capture_output=True,
        text=True,
        env={**os.environ, "PATH": f"{commands}:{os.environ['PATH']}", "PLATFORM": "linux/amd64"},
    )
    assert done.returncode == 0, done.stderr
    actual = [json.loads(row) for row in calls.read_text().splitlines()]
    export = next(call for call in actual if call[0] == "uv" and "export" in call)
    assert {"--locked", "--no-dev", "--no-emit-workspace", "--extra", "s3"} <= set(export)
    assert "--no-hashes" not in export
    packages = {export[n + 1] for n, part in enumerate(export) if part == "--package"}
    assert packages == {f"sandbox-science-{package}" for package in _recipe_packages(profile)}
    builds = [call for call in actual if call[:3] == ["docker", "buildx", "build"]]
    assert len(builds) == 1
    build = builds[0]
    assert "--load" in build
    assert not any("registry" in part or part.startswith("--push") for part in build)
    for context in ("wheels=", "trusted-inputs=", "workspace-web=", "repository-tools="):
        assert any(part.startswith(context) for part in build)
    assert f"sandbox-science/{profile}:" in done.stdout


@pytest.mark.parametrize("profile", PROFILES)
def test_revision_covers_locks_packages_and_all_runtime_inputs(profile: str) -> None:
    inputs = set(_recipe_inputs(profile))
    assert {
        "trusted",
        "services",
        "build_trusted.sh",
        "test_trusted.sh",
        "orchestrator/conda-lock.yml",
        "../uv.lock",
        "../pyproject.toml",
        "../scripts/check_licenses.py",
    } <= inputs
    packages = _recipe_packages(profile)
    assert "contracts" in packages
    for package in packages:
        assert f"../packages/{package}/src" in inputs
        assert f"../packages/{package}/pyproject.toml" in inputs
    if profile == "trusted-app":
        assert {
            "../skills/builtin",
            "../web/workspace/src",
            "../web/workspace/server",
            "../web/workspace/licenses",
            "../web/workspace/stubs",
            "../web/workspace/package-lock.json",
            "../web/workspace/vite.figurecheck.config.ts",
        } <= inputs
        assert "orchestrator" in packages
        assert "workspace" in packages
    else:
        assert {"worker", "policy-middleware", "package-cache", "policy", "event-store"} <= set(
            packages
        )
        assert "orchestrator" not in packages
        assert "workspace" not in packages


def test_trusted_native_pins_and_build_contexts_are_explicit() -> None:
    arguments = _containerfile_args()
    assert arguments["NODE_VERSION"].startswith("24.")
    assert arguments["OPENSHELL_VERSION"] == "0.1.3"
    for product in ("NODE", "OPENSHELL"):
        for architecture in ("AMD64", "ARM64"):
            assert re.fullmatch(r"[0-9a-f]{64}", arguments[f"{product}_SHA256_{architecture}"])
    stages = _stages()
    for context in ("trusted-inputs", "workspace-web", "repository-tools"):
        assert "exit 1" in stages[context]
    for profile in PROFILES:
        stage = stages[profile]
        assert "--only-binary=:all: --require-hashes" in stage
        assert "python -m pip check" in stage
        assert "services/copy_credentials.py" in stage
        assert "services/initialize_state.py" in stage
        assert "io.github.regenscheid.sandbox-science.profile" in stage
    assert "npm ci --ignore-scripts" in stages["trusted-web-build"]
    assert "--installed" in stages["trusted-web-build"]
    assert "npm run build:math" in stages["trusted-web-build"]
    assert "/usr/share/sandbox-science/math" in stages["trusted-app"]
    assert "/usr/share/sandbox-science/workspace" in stages["trusted-app"]
    assert "apt_install.sh" not in stages["trusted-python"]
    assert "apt_install.sh" in stages["trusted-worker"]
    assert "openssh-client" in stages["trusted-worker"]
    assert "git" in stages["trusted-worker"]
    assert "xz-utils" not in stages["trusted-worker"]
    assert "unpack_node.py" in stages["trusted-node-unpack"]
    assert "tar -xJ" not in stages["trusted-node-unpack"]


def test_trusted_smoke_uses_only_a_local_hardened_container() -> None:
    script = (IMAGES / "test_trusted.sh").read_text()
    assert "--pull=never" in script
    assert "--network=none" in script
    assert "--read-only" in script
    assert "--cap-drop=ALL" in script
    for name in ("build_trusted.sh", "test_trusted.sh"):
        subprocess.run(["bash", "-n", str(IMAGES / name)], check=True)
