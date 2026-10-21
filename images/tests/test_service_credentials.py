# SPDX-License-Identifier: MIT
"""Projected Secret files become private regular files without widening scope."""

import importlib.util
from pathlib import Path

import pytest

PATH = Path(__file__).resolve().parents[1] / "services" / "copy_credentials.py"
SPEC = importlib.util.spec_from_file_location("service_credentials", PATH)
assert SPEC is not None
assert SPEC.loader is not None
copy = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(copy)


@pytest.fixture
def roots(tmp_path):
    source, target = tmp_path / "source", tmp_path / "private"
    source.mkdir()
    target.mkdir()
    return source, target


def test_projected_files_copy_to_private_regular_files_and_repeat_identically(roots):
    source, target = roots
    data = source / "..generation"
    data.mkdir()
    (data / "runtime.token").write_text("test-secret-runtime-token-000000")
    (source / "..data").symlink_to("..generation")
    (source / "runtime.token").symlink_to("..data/runtime.token")
    mapping = [["runtime.token", "control/runtime.token"]]
    copy.copy_files(source, target, mapping)
    private = target / "control/runtime.token"
    before = private.stat()
    assert not private.is_symlink()
    assert before.st_mode & 0o777 == 0o600
    assert private.read_text() == "test-secret-runtime-token-000000"
    copy.copy_files(source, target, mapping)
    assert private.stat().st_ino == before.st_ino


def test_changed_projection_never_overwrites_a_completed_credential(roots):
    source, target = roots
    path = source / "token"
    path.write_text("test-secret-original-000000")
    mapping = [["token", "runtime.token"]]
    copy.copy_files(source, target, mapping)
    path.write_text("test-secret-rotated-000000")
    with pytest.raises(ValueError, match="changed"):
        copy.copy_files(source, target, mapping)
    assert (target / "runtime.token").read_text() == "test-secret-original-000000"


@pytest.mark.parametrize("relative", ["../token", "/token", "a//b", "a/./b", "a\0b"])
def test_mapping_rejects_nonliteral_relative_paths_before_any_copy(roots, relative):
    source, target = roots
    (source / "token").write_text("test-secret-keep-000000")
    with pytest.raises(ValueError, match="invalid service-file"):
        copy.copy_files(source, target, [["token", "okay"], ["token", relative]])
    assert list(target.iterdir()) == []


def test_symlink_source_escape_and_destination_parent_escape_are_refused(roots, tmp_path):
    source, target = roots
    outside = tmp_path / "outside"
    outside.mkdir()
    value = outside / "token"
    value.write_text("test-secret-outside-000000")
    (source / "token").symlink_to(value)
    with pytest.raises(ValueError, match="source root"):
        copy.copy_files(source, target, [["token", "out"]])
    (source / "token").unlink()
    (source / "token").write_text("test-secret-inside-000000")
    (target / "escape").symlink_to(outside)
    with pytest.raises(ValueError, match="parent"):
        copy.copy_files(source, target, [["token", "escape/token"]])
    assert value.read_text() == "test-secret-outside-000000"


def test_destination_symlink_nonprivate_or_oversize_file_is_refused(roots):
    source, target = roots
    (source / "token").write_text("test-secret-value-000000")
    (target / "other").write_text("test-secret-other-000000")
    path = target / "token"
    path.symlink_to("other")
    with pytest.raises(OSError, match="symbolic links"):
        copy.copy_files(source, target, [["token", "token"]])
    path.unlink()
    path.write_text("test-secret-value-000000")
    path.chmod(0o644)
    with pytest.raises(ValueError, match="private"):
        copy.copy_files(source, target, [["token", "token"]])
    path.unlink()
    (source / "token").write_bytes(b"x" * (copy.MAX_FILE_BYTES + 1))
    with pytest.raises(ValueError, match="size"):
        copy.copy_files(source, target, [["token", "token"]])
    assert not path.exists()


def test_duplicate_destinations_and_excess_mapping_are_refused(roots):
    source, target = roots
    for mapping in ([["a", "b"], ["c", "b"]], [["a", str(i)] for i in range(33)]):
        with pytest.raises(ValueError, match="service-file"):
            copy.copy_files(source, target, mapping)
