# SPDX-License-Identifier: MIT
"""The profile recipes, checked without Docker (docs/IMAGES.md).

The pins are exact, the lockfiles cover both architectures with a SHA-256 for every package,
each image's tag covers what its build reads, the services' images install the packages they
need, the declared OpenShell filesystem paths are valid targets sections, the Landlock emulation
in test.sh matches the policy compiler, and the license helpers that gate the permissive
images behave.
"""

from __future__ import annotations

import importlib.util
import json
import re
import subprocess
import sys
import tomllib
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import yaml

from sandbox_science.policy.openshell import (
    BASELINE_READ_ONLY,
    BASELINE_READ_WRITE,
    RESEARCH_IMAGE_READ_ONLY,
    SANDBOX_DEVICES_READ_ONLY,
    SANDBOX_DEVICES_READ_WRITE,
    FilesystemPolicy,
    SandboxFilesystem,
    compile_orchestrator,
    parse_targets,
)

IMAGES = Path(__file__).resolve().parents[1]
ROOT = IMAGES.parent
RESEARCH_PROFILES = ("minimal", "crypto-base")
SERVICE_PROFILES = ("orchestrator",)
"""The platform services' images, which install the repository's packages from wheels. The
MCP proxy's went with the proxy (D98, 2026-10-26)."""
PROFILES = (*RESEARCH_PROFILES, *SERVICE_PROFILES)
PLATFORMS = ["linux-64", "linux-aarch64"]
PREFIX = "/opt/sandbox-science"
SERVICE_FILESYSTEM = FilesystemPolicy(
    include_workdir=True,
    read_only=(*BASELINE_READ_ONLY, PREFIX),
    read_write=BASELINE_READ_WRITE,
)
"""A service sandbox's compiled filesystem section: OpenShell's baseline, which the compiler
lists since its supervisor adds it only where the paths exist on its own host (OpenShell
v0.1.5), and the prefix to read. No research devices."""
KERNEL_PACKAGES = {"ipykernel", "jupyter_client", "pyzmq", "zeromq"}


def _load(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


report = _load("image_report", IMAGES / "common" / "image_report.py")
layout = _load("prefix_layout", IMAGES / "common" / "prefix_layout.py")
checks = _load("check_image", IMAGES / "checks" / "check_image.py")


def _containerfile_args() -> dict[str, str]:
    text = (IMAGES / "Containerfile").read_text()
    return dict(re.findall(r"^ARG (\w+)=(\S+)$", text, flags=re.MULTILINE))


def _lockfile(profile: str) -> dict[str, Any]:
    return yaml.safe_load((IMAGES / profile / "conda-lock.yml").read_text())


def _stages() -> dict[str, str]:
    """The Containerfile's stages by name, with their instructions."""
    text = (IMAGES / "Containerfile").read_text()
    parts = re.split(r"^FROM \S+ AS (\S+)$", text, flags=re.MULTILINE)
    return dict(zip(parts[1::2], parts[2::2], strict=True))


def _context_paths(stage: str) -> set[str]:
    """The build-context paths a stage reads: bind mounts and copies not from another stage."""
    mounts = re.findall(r"--mount=type=bind,source=([^,\s]+)", stage)
    copies = re.findall(r"^COPY (?:--chmod=\S+ )?([a-z]\S*) ", stage, flags=re.MULTILINE)
    return {*mounts, *copies}


def _revision_sh(function: str, variable: str, profile: str) -> list[str]:
    """An array that a revision.sh function sets for the profile."""
    script = f'. "$1" && {function} {profile} && printf "%s\\n" "${{{variable}[@]}}"'
    done = subprocess.run(
        ["bash", "-c", script, "bash", str(IMAGES / "revision.sh")],
        capture_output=True,
        text=True,
        check=True,
    )
    return done.stdout.split()


def _recipe_inputs(profile: str) -> list[str]:
    """What revision.sh counts as the profile's build inputs, relative to images/."""
    return _revision_sh("recipe_inputs", "inputs", profile)


def _recipe_packages(profile: str) -> list[str]:
    """The repository's packages build.sh builds wheels of for the profile, as directories."""
    return _revision_sh("recipe_packages", "packages", profile)


def _requirements(package: str) -> list[str]:
    """A repository package's runtime requirements, by name (PEP 503 normalized)."""
    project = tomllib.loads((ROOT / "packages" / package / "pyproject.toml").read_text())
    names = []
    for requirement in project["project"]["dependencies"]:
        found = re.match(r"[A-Za-z0-9._-]+", requirement)
        assert found is not None, requirement
        names.append(re.sub(r"[._-]+", "-", found.group(0)).lower())
    return names


def _service_dependencies(profile: str) -> list[str]:
    """The packages a service's environment.yml asks for, besides Python and pip."""
    environment = yaml.safe_load((IMAGES / profile / "environment.yml").read_text())
    names = [spec.split()[0] for spec in environment["dependencies"]]
    return [name for name in names if name not in ("python", "pip")]


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


def test_arm64_apt_uses_the_pinned_archive_snapshot() -> None:
    recipe = (IMAGES / "Containerfile").read_text()
    assert 'if [ "${TARGETARCH}" = arm64 ]; then' in recipe
    assert (
        "s|http://ports.ubuntu.com/ubuntu-ports|"
        "https://snapshot.ubuntu.com/ubuntu/${UBUNTU_SNAPSHOT}|g"
    ) in recipe
    assert "Snapshot: no" in recipe
    assert 'APT::Snapshot="${UBUNTU_SNAPSHOT}"' in recipe


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


@pytest.mark.parametrize("profile", RESEARCH_PROFILES)
def test_each_research_profile_locks_the_kernel_packages(profile: str) -> None:
    names = {package["name"] for package in _lockfile(profile)["package"]}
    assert {"python", "ipykernel", "jupyter_client", "pip"} <= names
    if profile == "crypto-base":
        assert {"sagelib", "fpylll", "fplll", "pari", "libflint", "cython"} <= names
        # Not the `sage` metapackage, whose only addition is Jmol and its Java runtime.
        assert not {"sage", "jmol", "openjdk"} & names


@pytest.mark.parametrize("profile", SERVICE_PROFILES)
def test_the_services_lock_what_their_packages_require_and_no_kernels(profile: str) -> None:
    # pip installs the repository's packages without dependencies: the lockfile has the rest.
    names = {package["name"] for package in _lockfile(profile)["package"]}
    required = {
        name
        for package in _recipe_packages(profile)
        for name in _requirements(package)
        if not name.startswith("sandbox-science-")
    }
    assert required
    assert {"python", "pip", "pydantic-core", *required} <= names
    assert not KERNEL_PACKAGES & names


@pytest.mark.parametrize(
    ("profile", "name"),
    [(profile, name) for profile in SERVICE_PROFILES for name in _service_dependencies(profile)],
)
def test_the_services_lock_the_minor_versions_uv_lock_pins(profile: str, name: str) -> None:
    # The services' tests run with uv.lock's versions; relock the image when it moves on.
    uv_lock = tomllib.loads((ROOT / "uv.lock").read_text())
    tested = next(package["version"] for package in uv_lock["package"] if package["name"] == name)
    minor = ".".join(tested.split(".")[:2])
    locked = {p["version"] for p in _lockfile(profile)["package"] if p["name"] == name}
    assert locked
    assert all(version.startswith(f"{minor}.") for version in locked), (locked, tested)


def test_the_services_lock_the_same_python() -> None:
    # One interpreter for the platform services' images, as targets files name it.
    versions = {
        profile: {p["version"] for p in _lockfile(profile)["package"] if p["name"] == "python"}
        for profile in SERVICE_PROFILES
    }
    assert len({frozenset(found) for found in versions.values()}) == 1, versions


# Tags and build contexts.


@pytest.mark.parametrize("profile", PROFILES)
def test_the_recipe_revision_covers_what_the_build_reads(profile: str) -> None:
    inputs = _recipe_inputs(profile)
    assert {"Containerfile", ".dockerignore", "common", profile} <= set(inputs)
    assert not set(PROFILES) - {profile} & set(inputs), "another profile changes the tag"
    for path in inputs:
        assert (IMAGES / path).exists(), path
    stages = _stages()
    read = _context_paths(stages["base"]) | _context_paths(stages[profile])
    assert read
    for path in read:
        assert any(path == item or path.startswith(f"{item}/") for item in inputs), path
    # build.sh builds the wheels the image installs from these.
    packages = _recipe_packages(profile)
    assert bool(packages) is (profile in SERVICE_PROFILES)
    for package in packages:
        sources = {f"../packages/{package}/pyproject.toml", f"../packages/{package}/src"}
        assert sources <= set(inputs)


@pytest.mark.parametrize("profile", SERVICE_PROFILES)
def test_the_services_install_the_repository_packages_they_import(profile: str) -> None:
    # pip installs exactly the wheels build.sh builds, and those include every repository
    # package they depend on, so that `pip check` in the build holds.
    packages = _recipe_packages(profile)
    for package in packages:
        for name in _requirements(package):
            if name.startswith("sandbox-science-"):
                assert name.removeprefix("sandbox-science-") in packages, (package, name)
    install = re.search(r"pip install (.*?)&&", _stages()[profile], flags=re.DOTALL)
    assert install is not None
    installed = re.findall(r"\bsandbox-science-[a-z-]+", install.group(1))
    assert sorted(installed) == sorted(f"sandbox-science-{package}" for package in packages)


def test_the_service_builds_stop_without_the_wheels() -> None:
    # build.sh passes the wheels as the build context `wheels`. Without it, a stage of that name
    # stops the build; with no such stage, BuildKit would pull an image called wheels.
    stages = _stages()
    assert re.search(r"\bexit 1\s*$", stages["wheels"])
    for profile in SERVICE_PROFILES:
        assert list(stages).index("wheels") < list(stages).index(profile)
        assert "--mount=type=bind,from=wheels," in stages[profile]
    build = (IMAGES / "build.sh").read_text()
    shared = (IMAGES / "common/build_recipe.sh").read_text()
    assert "common/build_recipe.sh" in build
    assert '--build-context "wheels=' in shared


# The OpenShell view.


def test_the_declared_filesystem_paths_are_a_valid_targets_section() -> None:
    section = yaml.safe_load((IMAGES / "common" / "sandbox-filesystem.yml").read_text())
    filesystem = SandboxFilesystem.model_validate(section)
    assert PREFIX in filesystem.read_write


def test_the_orchestrators_filesystem_section_reads_the_prefix_only() -> None:
    section = yaml.safe_load((IMAGES / "orchestrator" / "filesystem.yml").read_text())
    filesystem = SandboxFilesystem.model_validate(section)
    assert filesystem == SandboxFilesystem(read_only=(PREFIX,))
    # The example targets file lists it, and the compiler adds the baseline but no devices.
    targets = parse_targets((ROOT / "examples" / "openshell" / "targets.yaml").read_text())
    assert targets.orchestrator is not None
    assert targets.orchestrator.filesystem == filesystem
    assert compile_orchestrator(targets.orchestrator).filesystem_policy == SERVICE_FILESYSTEM


def test_the_landlock_emulation_adds_the_compilers_devices() -> None:
    text = (IMAGES / "test.sh").read_text()

    def array(name: str) -> tuple[str, ...]:
        found = re.search(rf"^{name}=\((.*)\)$", text, flags=re.MULTILINE)
        assert found is not None, name
        return tuple(found.group(1).split())

    assert array("devices_ro") == SANDBOX_DEVICES_READ_ONLY
    assert array("devices_rw") == SANDBOX_DEVICES_READ_WRITE
    assert array("frozen_ro") == RESEARCH_IMAGE_READ_ONLY == (str(layout.FROZEN), str(layout.BASE))
    # compile_orchestrator() adds none to the service's sandbox.
    branch = re.search(
        r"^  orchestrator\)$(.*?)^    ;;$",
        text,
        flags=re.MULTILINE | re.DOTALL,
    )
    assert branch is not None
    assert re.search(r"^    devices_ro=\(\)$", branch.group(1), flags=re.MULTILINE)
    assert re.search(r"^    devices_rw=\(\)$", branch.group(1), flags=re.MULTILINE)
    assert re.search(r"^    frozen_ro=\(\)$", branch.group(1), flags=re.MULTILINE)
    assert "$here/$profile/filesystem.yml" in branch.group(1)
    for profile in SERVICE_PROFILES:
        assert (IMAGES / profile / "filesystem.yml").is_file()


def test_the_check_plan_names_methods_of_the_checks() -> None:
    for profile in PROFILES:
        for name in checks.planned(profile, landlock=True, network=True):
            assert callable(getattr(checks.Checks, name.replace("-", "_")))
    assert "landlock" not in checks.planned("minimal", landlock=False, network=False)
    assert "sage-kernel" not in checks.planned("minimal", landlock=True, network=True)
    orchestrator = checks.planned("orchestrator", landlock=True, network=True)
    assert {"landlock", "harness-cli", "harness-run", "no-kernels"} <= set(orchestrator)
    assert not {"executor", "python-kernel", "runtime-install"} & set(orchestrator)
    assert set(checks.SERVICE_PACKAGES) == set(SERVICE_PROFILES)
    for profile in SERVICE_PROFILES:
        expected = sorted(f"sandbox-science-{package}" for package in _recipe_packages(profile))
        assert sorted(checks.SERVICE_PACKAGES[profile]) == expected


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


def test_the_image_checks_client_runs_work_on_this_executor(tmp_path: Path) -> None:
    """The image check drives the worker's executor itself, outside CI's Docker-free runs:
    run here on this checkout's executor, its client opens package admission, which the
    executor holds from its start until the worker opens it, and its limits reach the
    executor. Without either, every shell and cell waited out 120 s for "a package command"
    (lab of 2026-10-16), and only images/test.sh showed it."""
    script = ROOT / "packages" / "worker" / "src" / "sandbox_science" / "worker" / "executor.py"
    held = checks.Executor(script, tmp_path / "project", tmp_path / "held.log")
    try:
        held.request("hello", wait=60)
        with pytest.raises(checks.CheckError, match=r"waited \d+ s for a package command"):
            held.shell("echo held", limit=1)
    finally:
        held.close()
    client = checks.Executor(script, tmp_path / "project", tmp_path / "executor.log")
    try:
        reply = client.open()
        assert set(checks.FEATURES["minimal"]) <= set(reply["features"])
        assert client.shell("echo ran", limit=30) == "ran\n"
    finally:
        client.close()


def test_the_service_checks_expect_each_packages_declared_license() -> None:
    """The record check compares each repository package the service images install with
    the license its ``pyproject.toml`` declares, which changed for the orchestrator."""
    for packages in checks.SERVICE_PACKAGES.values():
        for name in packages:
            folder = ROOT / "packages" / name.removeprefix("sandbox-science-")
            declared = tomllib.loads((folder / "pyproject.toml").read_text())["project"]
            assert (declared["name"], declared["license"]) == (name, checks.PACKAGE_LICENSES[name])


def test_the_harness_run_check_answers_on_this_checkouts_harness(tmp_path: Path) -> None:
    """The orchestrator image's harness-run check, run here on this checkout's harness: its
    stub worker speaks the worker protocol the contracts declare, which the harness refuses
    a worker without, and the scripted task is answered."""
    from sandbox_science.contracts.worker import PROTOCOL_HEADER, PROTOCOL_VERSION

    probe = subprocess.run(
        [sys.executable, "-I", "-c", checks.PROTOCOL_PROBE], capture_output=True, text=True
    )
    assert probe.stdout.split() == [PROTOCOL_HEADER, str(PROTOCOL_VERSION)]
    requests: list[str] = []
    server = checks.stub_worker((PROTOCOL_HEADER, PROTOCOL_VERSION), requests)
    transcript = tmp_path / "transcript.json"
    args = checks.harness_run_args(f"http://127.0.0.1:{server.server_port}", tmp_path, transcript)
    try:
        done = subprocess.run(
            [sys.executable, "-c", "import sys; from sandbox_science.orchestrator.cli import "
             "main; sys.exit(main())", "run", *args],
            capture_output=True, text=True, timeout=120,
            env={"HOME": str(tmp_path), "PATH": "/usr/local/bin:/usr/bin:/bin"},
        )  # fmt: skip
    finally:
        server.shutdown()
        server.server_close()
    assert done.returncode == 0, done.stderr[-1500:]
    assert done.stdout.rstrip().endswith(checks.MARKER)
    assert requests[:1] == [f"/v1/capabilities?environment_id={checks.SESSION}"]
    assert json.loads(transcript.read_text())["stopped"] == "answered"


def test_binding_inventory_generator_is_shared_by_build_and_executor() -> None:
    recipe = (IMAGES / "Containerfile").read_text()
    assert "from=binding-inventory,source=binding_inventory.py" in recipe
    assert "python -I -B /tmp/binding_inventory.py" in recipe
    build = (IMAGES / "build.sh").read_text()
    shared = (IMAGES / "common/build_recipe.sh").read_text()
    revision = (IMAGES / "revision.sh").read_text()
    assert "common/build_recipe.sh" in build
    assert "binding-inventory=$image_here/../packages/worker/src/sandbox_science/worker" in shared
    assert "../packages/worker/src/sandbox_science/worker/binding_inventory.py" in revision


def test_research_images_ship_a_target_native_versioned_scheduler_shim() -> None:
    stages = _stages()
    build = stages["scheduler-shim"]
    assert 'APT::Snapshot="${UBUNTU_SNAPSHOT}"' in build
    assert "from=binding-inventory,source=shim/sched_self.c" in build
    assert "sha256sum /tmp/sched_self.c" in build
    assert "SANDBOX_SCIENCE_SCHED_SOURCE_SHA256" in build
    assert "cc -shared -fPIC -O2" in build
    source = "../packages/worker/src/sandbox_science/worker/shim/sched_self.c"
    for profile in RESEARCH_PROFILES:
        assert source in _recipe_inputs(profile)
        assert (
            "COPY --from=scheduler-shim /output/ /usr/local/lib/sandbox-science/shim/"
            in stages[profile]
        )
        assert "scheduler-shim" in checks.planned(profile, landlock=True, network=False)
    for profile in SERVICE_PROFILES:
        assert "COPY --from=scheduler-shim" not in stages[profile]


def test_only_the_research_layer_is_seeded_into_workspace_without_changing_services() -> None:
    stages = _stages()
    research = stages["research-base"]
    assert "ln -s /sandbox/.sandbox-science-prefix /opt/sandbox-science" in research
    assert "install -d -o 0 -g 0 -m 0755 /opt/sandbox-science-base" in research
    assert "mv /opt/sandbox-science /sandbox" not in research
    path = re.search(r"ENV PATH=(\S+)", research)
    assert path is not None
    assert path.group(1).split(":")[:2] == [
        "/opt/sandbox-science/bin",
        "/opt/sandbox-science-base/bin",
    ]
    assert "JUPYTER_PATH=/opt/sandbox-science-base/share/jupyter" in research
    recipe = (IMAGES / "Containerfile").read_text()
    for profile in RESEARCH_PROFILES:
        stage = stages[profile]
        assert f"FROM research-base AS {profile}" in recipe
        assert "! -name .sandbox-science-prefix -exec rm -rf -- {} +" in stage
        assert "find /sandbox -mindepth 1 -delete" not in stage
        # The base is installed as root, never as the sandbox account, and the layer last.
        assert "setpriv" not in stage
        assert "-p /opt/sandbox-science-base" in stage
        layer = stage.index("prefix_layout.py layer")
        assert "pip install" not in stage[layer:]
        assert "chown -R -h 1000:1000 /sandbox/.sandbox-science-prefix" in stage[layer:]
    for profile in SERVICE_PROFILES:
        assert f"FROM base AS {profile}" in recipe
    script = (IMAGES / "test.sh").read_text()
    assert "type=volume,target=/sandbox" in script
    assert "type=volume,target=/opt/sandbox-science" not in script
