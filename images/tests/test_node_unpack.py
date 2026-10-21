# SPDX-License-Identifier: MIT
"""Pinned Node extraction requires no external xz and never follows unsafe members."""

import importlib.util
import io
import tarfile
from pathlib import Path

import pytest

PATH = Path(__file__).resolve().parents[1] / "trusted" / "unpack_node.py"
SPEC = importlib.util.spec_from_file_location("unpack_node", PATH)
assert SPEC is not None
assert SPEC.loader is not None
node = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(node)


def archive(path: Path, members: list[tarfile.TarInfo]) -> None:
    with tarfile.open(path, "w:xz") as stream:
        for member in members:
            stream.addfile(member, io.BytesIO(b"x" * member.size))


def test_python_extracts_single_node_prefix_and_preserves_executable_symlinks(tmp_path: Path):
    member = tarfile.TarInfo("node-v24-linux/bin/node")
    member.mode = 0o755
    member.size = 8
    link = tarfile.TarInfo("node-v24-linux/bin/npm")
    link.type = tarfile.SYMTYPE
    link.linkname = "node"
    source = tmp_path / "node.tar.xz"
    archive(source, [member, link])
    destination = tmp_path / "node"
    node.unpack(source, destination)
    assert (destination / "bin/node").read_bytes() == b"x" * 8
    assert (destination / "bin/node").stat().st_mode & 0o111
    assert (destination / "bin/npm").is_symlink()
    with pytest.raises(ValueError, match="must not exist"):
        node.unpack(source, destination)


@pytest.mark.parametrize("kind", ["traversal", "absolute_link", "fifo", "multiple_roots"])
def test_incomplete_or_unsafe_node_archives_never_expose_destination(tmp_path: Path, kind: str):
    member = tarfile.TarInfo("node/file")
    if kind == "traversal":
        member.name = "../outside"
    elif kind == "absolute_link":
        member.type = tarfile.SYMTYPE
        member.linkname = "/outside"
    elif kind == "fifo":
        member.type = tarfile.FIFOTYPE
    source = tmp_path / "node.tar.xz"
    archive(source, [member, tarfile.TarInfo("another")] if kind == "multiple_roots" else [member])
    destination = tmp_path / "node"
    with pytest.raises((ValueError, tarfile.FilterError)):
        node.unpack(source, destination)
    assert not destination.exists()
    assert not (tmp_path / "outside").exists()
    assert list(tmp_path.glob(".node-unpack-*")) == []
