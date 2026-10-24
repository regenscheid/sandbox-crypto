# SPDX-License-Identifier: MIT
"""The research prefix's image layout (images/common/prefix_layout.py, docs/IMAGES.md), on a
small stand-in prefix: what trimming removes and keeps, and what freezing moves, links, pins
and records. The images' builds run it on the real prefix and check the result."""

from __future__ import annotations

import importlib.util
import json
import os
import re
import sys
from pathlib import Path
from types import ModuleType

import pytest

IMAGES = Path(__file__).resolve().parents[1]


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "prefix_layout", IMAGES / "common" / "prefix_layout.py"
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["prefix_layout"] = module
    spec.loader.exec_module(module)
    return module


layout = _load()


def _write(path: Path, text: str = "x") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def _record(prefix: Path, name: str, version: str, build: str, files: list[str]) -> None:
    record = {"name": name, "version": version, "build": build, "files": files}
    _write(prefix / "conda-meta" / f"{name}-{version}-{build}.json", json.dumps(record))


@pytest.fixture
def prefix(tmp_path: Path) -> Path:
    """A prefix shaped like crypto-base's, with a few files in each place that matters."""
    root = tmp_path / "prefix"
    for path in (
        "share/doc/mpfr/COPYING",
        "share/doc/mpfr/mpfr.html",
        "share/doc/NTL/copying.txt",
        "share/doc/gc/README",
        "share/man/man1/gp.1",
        "share/info/maxima-index.lisp",
        "etc/conda/test-files/sage/1/test.py",
        "lib/libpari.a",
        "lib/libpari.so.9",
        "lib/libgmp.a",
        "lib/libgmp.so",
        "lib/python3.14/site-packages/numpy/_core/lib/libnpymath.a",
        "lib/python3.14/site-packages/x-1.0.dist-info/METADATA",
        "lib/gcc/x86_64-conda-linux-gnu/15.3.0/libgcc.a",
        "lib/gcc/x86_64-conda-linux-gnu/15.3.0/libgcc_s.so",
        "libexec/gcc/x86_64-conda-linux-gnu/15.3.0/cc1",
        "x86_64-conda-linux-gnu/sysroot/usr/lib64/libc_nonshared.a",
        "x86_64-conda-linux-gnu/sysroot/usr/lib64/libc.so",
        "share/gap/lib/init.g",
        "share/gap/doc/ref/manual.six",
        "share/giac/doc/aide_cas",
        "lib/libgomp.so",
        "bin/x86_64-conda-linux-gnu-ld",
        "lib/maxima/5.49.0/binary-ecl/maxima",
    ):
        _write(root / path)
    gcc = root / "lib/gcc/x86_64-conda-linux-gnu/15.3.0"
    (gcc / "libgomp.so").symlink_to("../../../libgomp.so")  # leaves its tree
    (gcc / "libgcc_s.so.1").symlink_to("libgcc_s.so")  # stays within it
    (root / "x86_64-conda-linux-gnu/bin").mkdir()
    (root / "x86_64-conda-linux-gnu/bin/ld").symlink_to("../../bin/x86_64-conda-linux-gnu-ld")
    (root / "share/giac/doc/fr").mkdir()
    (root / "share/giac/doc/fr/aide_cas").symlink_to("../aide_cas")
    _record(root, "gap-core", "4.15.1", "h60ec763_2", ["share/gap/lib/init.g", "bin/gap"])
    _record(root, "gcc_impl_linux-64", "15.3.0", "h6ad849d_7", ["lib/gcc/x/15.3.0/libgcc.a"])
    _record(root, "pari", "2.17.3", "h0_0", ["lib/libpari.so.9"])
    return root


def test_trimming_removes_documentation_but_license_texts(prefix: Path) -> None:
    removed = layout.trim(prefix)
    assert sorted(path.name for path in (prefix / "share/doc").rglob("*") if path.is_file()) == [
        "COPYING",
        "copying.txt",
    ]
    assert not (prefix / "share/doc/gc").exists()
    assert not (prefix / "share/man").exists()
    assert not (prefix / "etc/conda/test-files").exists()
    # Maxima reads its help index from share/info as it starts.
    assert (prefix / "share/info/maxima-index.lisp").exists()
    assert removed["share/doc"][0] == 2


def test_trimming_removes_only_static_libraries_beside_a_shared_one(prefix: Path) -> None:
    removed = layout.trim(prefix)
    assert not (prefix / "lib/libpari.a").exists()  # beside libpari.so.9
    assert not (prefix / "lib/libgmp.a").exists()
    assert removed["static libraries"][0] == 2
    # All there is to link with, and the compilers' own archives, stay.
    assert (prefix / "lib/python3.14/site-packages/numpy/_core/lib/libnpymath.a").exists()
    assert (prefix / "lib/gcc/x86_64-conda-linux-gnu/15.3.0/libgcc.a").exists()
    assert (prefix / "x86_64-conda-linux-gnu/sysroot/usr/lib64/libc_nonshared.a").exists()
    assert layout.check(prefix, prefix.parent / "no-frozen-trees") == []


def test_freezing_moves_the_trees_behind_absolute_links_and_pins_their_packages(
    prefix: Path, tmp_path: Path
) -> None:
    frozen = tmp_path / "frozen"
    layout.trim(prefix)
    before = layout._snapshot(prefix)
    result = layout.freeze(prefix, frozen)
    trees = ["lib/gcc", "libexec/gcc", "share/gap", "share/giac", "x86_64-conda-linux-gnu"]
    assert result["trees"] == trees
    for tree in trees:
        assert os.readlink(prefix / tree) == str(frozen / tree)
        assert (frozen / tree).is_dir()
        assert not (frozen / tree).is_symlink()
    # Maxima's binary finds its libraries through $ORIGIN, so it stays in the prefix.
    assert not (prefix / "lib/maxima").is_symlink()
    # Links that left their tree name the prefix; those within it keep their text.
    gcc = frozen / "lib/gcc/x86_64-conda-linux-gnu/15.3.0"
    assert os.readlink(gcc / "libgomp.so") == str(prefix / "lib/libgomp.so")
    assert os.readlink(gcc / "libgcc_s.so.1") == "libgcc_s.so"
    assert os.readlink(frozen / "x86_64-conda-linux-gnu/bin/ld") == str(
        prefix / "bin/x86_64-conda-linux-gnu-ld"
    )
    assert os.readlink(frozen / "share/giac/doc/fr/aide_cas") == "../aide_cas"
    assert (prefix / "x86_64-conda-linux-gnu/bin/ld").exists()
    assert (prefix / "share/gap/lib/init.g").exists()
    # The packages with files there, at their builds; the others are free to change.
    pinned = (prefix / "conda-meta/pinned").read_text().splitlines()
    assert pinned == ["gap-core==4.15.1=h60ec763_2", "gcc_impl_linux-64==15.3.0=h6ad849d_7"]
    manifest = json.loads((frozen / "manifest.json").read_text())
    assert manifest["trees"] == trees
    assert [package["name"] for package in manifest["packages"]] == [
        "gap-core",
        "gcc_impl_linux-64",
    ]
    # Nothing the worker's package fingerprint reads changed.
    assert layout._snapshot(prefix) == before
    # Nobody but the owner may write the frozen trees.
    assert not (frozen / "share/gap/lib/init.g").stat().st_mode & 0o022
    assert layout.check(prefix, frozen, owner=os.getuid()) == []


def test_the_check_finds_a_changed_link_or_a_lost_pin(prefix: Path, tmp_path: Path) -> None:
    frozen = tmp_path / "frozen"
    layout.trim(prefix)
    layout.freeze(prefix, frozen)
    (prefix / "share/gap").unlink()
    (prefix / "share/gap").symlink_to(tmp_path)
    (prefix / "conda-meta/pinned").write_text("")
    problems = layout.check(prefix, frozen, owner=os.getuid())
    assert f"{prefix / 'share/gap'} is not a link to {frozen / 'share/gap'}" in problems
    assert "gap-core is not pinned" in problems


def test_freezing_a_prefix_without_frozen_trees_changes_nothing(tmp_path: Path) -> None:
    root = tmp_path / "minimal"
    _write(root / "bin/python")
    (root / "conda-meta").mkdir()
    assert layout.freeze(root, tmp_path / "frozen") == {"trees": [], "packages": []}
    assert not (tmp_path / "frozen").exists()
    assert not (root / "conda-meta/pinned").exists()
    assert layout.check(root, tmp_path / "frozen") == []


def test_the_research_builds_trim_freeze_and_check_in_their_install_steps() -> None:
    parts = re.split(r"^FROM \S+ AS (\S+)$", (IMAGES / "Containerfile").read_text(), flags=re.M)
    stages = dict(zip(parts[1::2], parts[2::2], strict=True))
    for profile in ("minimal", "crypto-base"):
        stage = stages[profile]
        install = stage.index("micromamba install -y -n base -f /tmp/conda-lock.yml")
        trim = stage.index("prefix_layout.py trim /sandbox/.sandbox-science-prefix")
        check = stage.index(
            "prefix_layout.py check /sandbox/.sandbox-science-prefix /opt/sandbox-science-frozen"
        )
        assert install < trim < check
        assert "setpriv --reuid=1000 --regid=1000" in stage[:install]
        freeze = (
            "prefix_layout.py freeze /sandbox/.sandbox-science-prefix /opt/sandbox-science-frozen"
        )
        # minimal freezes nothing; crypto-base freezes in the install's RUN, so no layer keeps
        # the files it moves.
        if profile == "minimal":
            assert freeze not in stage
            assert "\nRUN " not in stage[install:trim]
        else:
            assert trim < stage.index(freeze) < check
            assert "\nRUN " not in stage[install : stage.index(freeze)]
    assert Path("/opt/sandbox-science-frozen") == layout.FROZEN
    assert Path("/sandbox/.sandbox-science-prefix") == layout.PREFIX
