# SPDX-License-Identifier: MIT
"""Create-only cold-volume state layout, independent of image builds and clusters."""

from __future__ import annotations

import runpy
import stat
from pathlib import Path

import pytest

HELPER = Path(__file__).resolve().parents[1] / "services/initialize_state.py"


def initialize(root, component, *, package_cache=False):
    runpy.run_path(str(HELPER))["initialize"](root, component, package_cache=package_cache)


@pytest.mark.parametrize(
    ("component", "cache", "expected"),
    [
        ("app", False, {"control"}),
        ("pool", False, {"worker", "middleware"}),
        ("pool", True, {"worker", "middleware", "package-cache"}),
    ],
)
def test_fixed_directories_on_empty_volume_are_private_and_idempotent(
    tmp_path, component, cache, expected
):
    initialize(tmp_path, component, package_cache=cache)
    assert {path.name for path in tmp_path.iterdir()} == expected
    before = {
        path.name: (path.stat().st_ino, path.stat().st_mtime_ns) for path in tmp_path.iterdir()
    }
    for path in tmp_path.iterdir():
        assert stat.S_IMODE(path.stat().st_mode) == 0o700
        (path / "existing-state").write_bytes(b"preserve original state")
    initialize(tmp_path, component, package_cache=cache)
    assert {path.name: path.stat().st_ino for path in tmp_path.iterdir()} == {
        name: value[0] for name, value in before.items()
    }
    assert all(
        (path / "existing-state").read_bytes() == b"preserve original state"
        for path in tmp_path.iterdir()
    )


def test_existing_state_modes_and_contents_are_not_changed(tmp_path):
    control = tmp_path / "control"
    control.mkdir(mode=0o750)
    state = control / "original.sqlite3"
    state.write_bytes(b"existing database")
    state.chmod(0o640)
    original = (control.stat(), state.stat())
    initialize(tmp_path, "app")
    assert control.stat() == original[0]
    assert state.stat() == original[1]
    assert state.read_bytes() == b"existing database"


@pytest.mark.parametrize("collision", ["symlink", "file"])
def test_collisions_are_refused_without_following_or_replacing(tmp_path, collision):
    outside = tmp_path / "outside"
    outside.mkdir()
    root = tmp_path / "volume"
    root.mkdir()
    control = root / "control"
    if collision == "symlink":
        control.symlink_to(outside, target_is_directory=True)
    else:
        control.write_bytes(b"keep file")
    with pytest.raises(NotADirectoryError):
        initialize(root, "app")
    assert not list(outside.iterdir())
    assert control.is_symlink() if collision == "symlink" else control.read_bytes() == b"keep file"


def test_symlink_root_is_refused(tmp_path):
    root = tmp_path / "real-volume"
    root.mkdir()
    linked = tmp_path / "volume-link"
    linked.symlink_to(root, target_is_directory=True)
    with pytest.raises(NotADirectoryError):
        initialize(linked, "app")
    assert not list(root.iterdir())


@pytest.mark.parametrize(("component", "cache"), [("../other", False), ("app", True)])
def test_unknown_layout_is_refused_before_any_state_change(tmp_path, component, cache):
    with pytest.raises(ValueError, match="unsupported service state layout"):
        initialize(tmp_path, component, package_cache=cache)
    assert not list(tmp_path.iterdir())
