# SPDX-License-Identifier: MIT
"""``scripts/check_licenses.py --image``: a trusted image's licenses.txt against the Python
distributions the image installs (docs/IMAGES.md). The locked wheels are replaced by fixtures,
since reading them needs the environment and sometimes the network; CI runs the real check."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest

from .test_recipes import ROOT, _recipe_packages

PROFILES = ("trusted-app", "trusted-worker")


def _script() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "check_licenses_image", ROOT / "scripts" / "check_licenses.py"
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


check = _script()

FILE = """\
# The licenses in a fixture image. Comments, even with GPL-3.0-only in them, are not entries.
Apache-2.0
Apache-2.0 AND MIT
BSD-3-Clause
GPL-3.0-only   # from the Conda bootstrap
LicenseRef-Ubuntu-archive
MIT
"""


@pytest.fixture
def repository(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A repository holding only the files ``--image`` reads besides the lock."""
    root = tmp_path / "repository"
    (root / "images" / "common").mkdir(parents=True)
    (root / "images" / "orchestrator").mkdir()
    (root / "images" / "trusted-app").mkdir()
    (root / "images" / "common" / "image_report.py").write_bytes(
        (ROOT / "images" / "common" / "image_report.py").read_bytes()
    )
    (root / "images" / "revision.sh").write_text(
        "recipe_packages() {\n  case $1 in\n"
        "    trusted-app) packages=(contracts policy) ;;\n  esac\n}\n"
    )
    (root / "images" / "orchestrator" / "environment.yml").write_text(
        "dependencies:\n  - python 3.14.*\n  - pyyaml 6.0.*\n"
    )
    (root / "images" / "orchestrator" / "conda-lock.yml").write_text(
        "package:\n- name: python\n  version: '3.14.0'\n- name: PyYAML\n  version: '6.0'\n"
    )
    monkeypatch.setattr(check, "_repository_root", lambda: root)
    monkeypatch.setattr(
        check,
        "image_requirements",
        lambda _root, _profile: (
            "attrs==26.1.0 \\\n    --hash=sha256:aa\nidna==3.20 \\\n    --hash=sha256:bb\n"
        ),
    )
    return root


def wheels(monkeypatch: pytest.MonkeyPatch, **licenses: str | None) -> None:
    monkeypatch.setattr(check, "_locked_licenses", lambda _requirements, _workspace: licenses)


def write_file(root: Path, text: str) -> None:
    (root / "images" / "trusted-app" / "licenses.txt").write_text(text)


def test_a_file_that_matches_passes(
    repository: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    write_file(repository, FILE)
    wheels(monkeypatch, boto3="Apache-2.0", yarl="Apache-2.0", attrs="MIT", idna="BSD-3-Clause")
    assert check.main(["--image", "trusted-app"]) == 0
    out = capsys.readouterr()
    assert out.err == ""
    assert "not checked here" in out.out
    assert "Conda bootstrap" in out.out
    assert "Ubuntu" in out.out


def test_a_license_missing_from_the_file_fails_naming_it_and_its_packages(
    repository: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    write_file(repository, FILE)
    wheels(
        monkeypatch,
        cryptography="Apache-2.0 OR BSD-3-Clause",
        boto3="Apache-2.0",
        attrs="MIT",
        sniffio="MIT-0",
    )
    assert check.main(["--image", "trusted-app"]) == 1
    err = capsys.readouterr().err
    assert "'Apache-2.0 OR BSD-3-Clause' (carried by cryptography) is missing" in err
    assert "'MIT-0' (carried by sniffio) is missing" in err
    assert "'MIT'" not in err


def test_a_stale_entry_warns_and_does_not_fail(
    repository: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    write_file(repository, FILE + "Zlib\nApache-2.0 AND MIT\n")
    wheels(monkeypatch, boto3="Apache-2.0", attrs="MIT", idna="BSD-3-Clause")
    assert check.main(["--image", "trusted-app"]) == 0
    out = capsys.readouterr()
    assert out.err == ""
    assert "warning: 3 entries" in out.out
    assert "'Zlib'" in out.out
    assert "'Apache-2.0 AND MIT'" in out.out
    assert "'GPL-3.0-only'" in out.out
    assert "LicenseRef-Ubuntu-archive'" not in out.out  # the label always carries it


def test_the_licenses_are_normalized_as_the_build_does(
    repository: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_file(repository, FILE)
    # The build uppercases the operators and joins the words with single spaces.
    wheels(monkeypatch, boto3="Apache-2.0", attrs="mit", idna="BSD-3-Clause")
    assert check.main(["--image", "trusted-app"]) == 1  # 'mit' is not 'MIT'
    wheels(monkeypatch, a="Apache-2.0 and  MIT", b="MIT", c="BSD-3-Clause")
    assert check.main(["--image", "trusted-app"]) == 0


def test_a_license_only_a_bootstrap_package_carries_is_a_warning(
    repository: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    write_file(repository, FILE)
    # PyYAML is also a Conda package, whose record may give the image's license for it.
    wheels(monkeypatch, pyyaml="ISC", boto3="Apache-2.0", attrs="MIT", idna="BSD-3-Clause")
    assert check.main(["--image", "trusted-app"]) == 0
    assert "carried only by pyyaml" in capsys.readouterr().out
    wheels(monkeypatch, pyyaml="ISC", other="ISC", boto3="Apache-2.0")
    assert check.main(["--image", "trusted-app"]) == 1


@pytest.mark.parametrize("found", [None, "GPL-3.0-only", "not a license ("])
def test_a_wheel_the_build_would_refuse_fails(
    repository: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    found: str,
) -> None:
    write_file(repository, FILE)
    wheels(monkeypatch, boto3="Apache-2.0", attrs="MIT", idna="BSD-3-Clause", odd=found)
    assert check.main(["--image", "trusted-app"]) == 1
    assert "odd:" in capsys.readouterr().err


def test_a_locked_set_that_cannot_be_read_fails(
    repository: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    write_file(repository, FILE)

    def refuse(_root: Path, _profile: str) -> str:
        raise check.ImageLicenseError("uv export failed")

    monkeypatch.setattr(check, "image_requirements", refuse)
    assert check.main(["--image", "trusted-app"]) == 1
    assert "trusted-app: uv export failed" in capsys.readouterr().err


def test_image_cannot_be_combined_with_npm(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        check.main(["--image", "trusted-app", "--npm", "package-lock.json"])
    assert "cannot be combined" in capsys.readouterr().err


def test_requirements_keep_each_entrys_hashes_and_drop_what_does_not_apply() -> None:
    environment = {"python_version": "3.14", "sys_platform": "linux", "os_name": "posix"}
    text = (
        "attrs==26.1.0 \\\n    --hash=sha256:aa \\\n    --hash=sha256:ab\n"
        "colorama==0.4.6 ; sys_platform == 'win32' \\\n    --hash=sha256:cc\n"
        "Typing_Extensions==4.16.0 ; python_full_version < '3.20' \\\n    --hash=sha256:dd\n"
    )
    found = check.parse_requirements(text, {**environment, "python_full_version": "3.14.0"})
    assert sorted(found) == ["attrs", "typing-extensions"]
    assert found["attrs"] == (
        "26.1.0",
        "attrs==26.1.0 \\\n    --hash=sha256:aa \\\n    --hash=sha256:ab\n",
    )


def test_unexpected_requirement_lines_are_refused() -> None:
    with pytest.raises(check.ImageLicenseError):
        check.parse_requirements("-e ./local\n", {})


@pytest.mark.parametrize("profile", PROFILES)
def test_the_profiles_packages_are_the_ones_the_build_reads(profile: str) -> None:
    assert check.image_workspace_packages(ROOT, profile) == _recipe_packages(profile)


@pytest.mark.parametrize("profile", PROFILES)
def test_the_committed_files_read_as_the_build_reads_them(profile: str) -> None:
    report = check._image_report(ROOT)
    entries = report.read_licenses(ROOT / "images" / profile / "licenses.txt")
    assert check.UBUNTU in entries
    assert report.UBUNTU == check.UBUNTU
    assert "MIT-0" in entries  # cffi's license, once missing from the file
    assert check.image_python_environment(ROOT)["python_version"].startswith("3.")
    assert "python" in check.image_bootstrap_names(ROOT)
