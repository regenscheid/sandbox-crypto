# SPDX-License-Identifier: MIT
"""The research prefix's image layout (images/common/prefix_layout.py, docs/IMAGES.md), on a
small stand-in prefix: what trimming removes and keeps, and what freezing moves, links, pins
and records. The images' builds run it on the real prefix and check the result."""

from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
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
        install = stage.index(
            "micromamba install -y -r /opt/sandbox-science-base -p /opt/sandbox-science-base"
        )
        trim = stage.index("prefix_layout.py trim /opt/sandbox-science-base")
        layer = stage.index(
            "prefix_layout.py layer \\\n"
            "        /opt/sandbox-science-base /sandbox/.sandbox-science-prefix"
        )
        check = stage.index(
            "prefix_layout.py check /opt/sandbox-science-base /opt/sandbox-science-frozen \\\n"
            "        --layer /sandbox/.sandbox-science-prefix"
        )
        assert install < trim < layer < check
        assert "chmod -R go-w /opt/sandbox-science-base" in stage[install:trim]
        freeze = (
            "prefix_layout.py freeze \\\n"
            "        /opt/sandbox-science-base /opt/sandbox-science-frozen"
        )
        # minimal freezes nothing; crypto-base freezes in the install's RUN, so no layer keeps
        # the files it moves.
        if profile == "minimal":
            assert freeze not in stage
            assert "\nRUN " not in stage[install:trim]
        else:
            assert trim < stage.index(freeze) < layer
            assert "\nRUN " not in stage[install : stage.index(freeze)]
    assert Path("/opt/sandbox-science-frozen") == layout.FROZEN
    assert Path("/sandbox/.sandbox-science-prefix") == layout.PREFIX
    assert Path("/opt/sandbox-science-base") == layout.BASE
    assert Path("/opt/sandbox-science") == layout.ALIAS


def test_the_worker_checks_the_layer_the_build_leaves() -> None:
    """The worker's layout check (workspace_prefix.py) reads what ``layer`` writes."""
    from sandbox_science.worker import workspace_prefix

    assert workspace_prefix.BASE == layout.BASE
    assert workspace_prefix.PREFIX == layout.PREFIX
    assert workspace_prefix.ALIAS == layout.ALIAS
    assert workspace_prefix.LAYER_MARKER == layout.LAYER_MARKER
    assert workspace_prefix.LAYER_VERSION == layout.LAYER_VERSION


@pytest.fixture
def base(tmp_path: Path) -> Path:
    """A base shaped like minimal's: an interpreter, Python scripts, a perl script, a
    library with its links, kernelspecs and Conda records."""
    root = tmp_path / "base"
    _write(root / "bin/python3.14")
    (root / "bin/python").symlink_to("python3.14")
    _write(root / "bin/pip", f"#!{root}/bin/python3.14\nimport pip\n")
    _write(root / "bin/sage", f"#!/bin/sh\n'''exec' {root}/bin/python \"$0\" \"$@\"\n'''\n")
    _write(root / "bin/tool.pl", f"#!{root}/bin/perl\nprint 1;\n")
    _write(root / "bin/elf", "\x7fELF python")
    for name in ("pip", "sage", "tool.pl", "elf"):
        (root / "bin" / name).chmod(0o755)
    _write(root / "lib/libgmp.so.10.5.0")
    (root / "lib/libgmp.so.10").symlink_to("libgmp.so.10.5.0")
    (root / "lib/libgmp.so").symlink_to("libgmp.so.10.5.0")
    _write(root / "lib/libgmp.a")
    _write(root / "lib/python3.14/site-packages/x-1.0.dist-info/METADATA")
    (root / "lib/pkgconfig").mkdir()
    spec = {"argv": [f"{root}/bin/python", "-m", "ipykernel_launcher"], "language": "python"}
    _write(root / "share/jupyter/kernels/python3/kernel.json", json.dumps(spec))
    sage = {"argv": ["python", "-m", "sage.repl.ipython_kernel"], "language": "sage"}
    _write(root / "share/jupyter/kernels/sagemath/kernel.json", json.dumps(sage))
    _record(root, "python", "3.14.8", "h0", ["bin/python3.14"])
    _record(root, "gmp", "6.3.0", "h1", ["lib/libgmp.so.10.5.0", "lib/libgmp.so"])
    _write(root / "conda-meta/history", "==> 2026-10-26 <==\n")
    _write(root / "conda-meta/pinned", "gmp==6.3.0=h1\n")
    return root


def _make(base: Path, tmp_path: Path) -> Path:
    layer, alias, frozen = tmp_path / "layer", tmp_path / "alias", tmp_path / "frozen"
    layout.launchers(base, alias)
    layout.layer(base, layer, alias, frozen=frozen, revision="abc1234")
    return layer


def test_the_layer_is_a_small_environment_over_the_base(base: Path, tmp_path: Path) -> None:
    layer, alias, frozen = tmp_path / "layer", tmp_path / "alias", tmp_path / "frozen"
    assert layout.launchers(base, alias) == 3
    made = layout.layer(base, layer, alias, frozen=frozen, revision="abc1234")
    counts = (made["scripts"], made["programs"], made["libraries"], made["records"])
    assert counts == (3, 3, 3, 2)
    assert made["entries"] == sum(len(d) + len(n) for _, d, n in os.walk(layer))
    # Python scripts and kernelspecs start the layer's interpreter; other interpreters and
    # programs keep theirs.
    assert (base / "bin/pip").read_text().startswith(f"#!{alias}/bin/python3.14\n")
    assert f"'''exec' {alias}/bin/python " in (base / "bin/sage").read_text()
    assert (base / "bin/tool.pl").read_text().startswith(f"#!{base}/bin/perl")
    assert (base / "bin/elf").read_text() == "\x7fELF python"
    spec = json.loads((base / "share/jupyter/kernels/python3/kernel.json").read_text())
    assert spec["argv"][0] == f"{alias}/bin/python"
    sage = json.loads((base / "share/jupyter/kernels/sagemath/kernel.json").read_text())
    assert sage["argv"][0] == "python"
    # A virtual environment of the base's interpreter that sees the base's packages.
    config = (layer / "pyvenv.cfg").read_text().splitlines()
    assert config == [
        f"home = {base}/bin",
        "include-system-site-packages = true",
        "version = 3.14.8",
    ]
    marker = json.loads((layer / layout.LAYER_MARKER).read_text())
    assert marker == {
        "version": 1,
        "base": str(base),
        "revision": "abc1234",
        "identity": layout.base_identity(base),
    }
    # Scripts are copies, so that an upgrade rewrites the layer's; the rest are links.
    assert not (layer / "bin/pip").is_symlink()
    assert (layer / "bin/pip").read_bytes() == (base / "bin/pip").read_bytes()
    assert os.access(layer / "bin/pip", os.X_OK)
    assert os.readlink(layer / "bin/python") == str(base / "bin/python")
    assert os.readlink(layer / "bin/elf") == str(base / "bin/elf")
    assert sorted(path.name for path in (layer / "lib").iterdir()) == [
        "libgmp.so",
        "libgmp.so.10",
        "libgmp.so.10.5.0",
        "python3.14",
    ]
    assert os.readlink(layer / "lib/libgmp.so.10") == str(base / "lib/libgmp.so.10")
    assert list((layer / "lib/python3.14/site-packages").iterdir()) == []
    # Each base record names the layer's entries for its package as its files, and keeps
    # the base's time; the layer pins Python at the base's build.
    gmp = json.loads((layer / "conda-meta/gmp-6.3.0-h1.json").read_text())
    assert gmp == {
        "name": "gmp",
        "version": "6.3.0",
        "build": "h1",
        "files": ["lib/libgmp.so.10.5.0", "lib/libgmp.so"],
    }
    python = json.loads((layer / "conda-meta/python-3.14.8-h0.json").read_text())
    assert python["files"] == ["bin/python3.14"]
    for name in ("gmp-6.3.0-h1.json", "python-3.14.8-h0.json"):
        base_time = (base / "conda-meta" / name).stat().st_mtime_ns
        assert (layer / "conda-meta" / name).stat().st_mtime_ns == base_time
    pins = (layer / "conda-meta/pinned").read_text()
    assert pins == "gmp==6.3.0=h1\npython==3.14.8=h0\n"
    assert (layer / "conda-meta/history").exists()
    # The frozen trees' manifest, at the version an older worker refuses, with no trees.
    manifest = json.loads((frozen / layout.MANIFEST).read_text())
    assert (manifest["version"], manifest["trees"]) == (2, [])
    owner = os.getuid()
    problems = layout.check_layer(base, layer, alias)
    # Run as another user than root, the base's ownership is all the check refuses.
    assert problems == ([] if owner == 0 else [f"{base} is writable by others than root"])


def test_the_base_identity_follows_its_packages_not_their_times(base: Path) -> None:
    before = layout.base_identity(base)
    os.utime(base / "conda-meta/gmp-6.3.0-h1.json", ns=(1, 1))
    assert layout.base_identity(base) == before
    _record(base, "mpfr", "4.2.1", "h2", [])
    assert layout.base_identity(base) != before


@pytest.mark.parametrize(
    "damage",
    [
        "marker",
        "home",
        "program",
        "script",
        "library",
        "record",
        "time",
        "pins",
        "packages",
        "launcher",
        "pkgs",
    ],
)
def test_the_layer_check_finds_what_is_wrong(base: Path, tmp_path: Path, damage: str) -> None:
    layer = _make(base, tmp_path)
    alias = tmp_path / "alias"
    expected = {
        "marker": "does not name",
        "home": "lacks 'home",
        "program": "is not a link to the base's bin entry",
        "script": "is not a copy of the base's script",
        "library": "is not a link to the base's lib entry",
        "record": "is not the base's record",
        "time": "is not the base's record",
        "pins": "differs from the base's pins",
        "packages": "has packages of its own",
        "launcher": "starts the base's interpreter",
        "pkgs": "is not empty",
    }[damage]
    if damage == "marker":
        marker = json.loads((layer / layout.LAYER_MARKER).read_text())
        (layer / layout.LAYER_MARKER).write_text(json.dumps({**marker, "identity": "0" * 64}))
    elif damage == "home":
        (layer / "pyvenv.cfg").write_text("home = /elsewhere/bin\n")
    elif damage == "program":
        (layer / "bin/elf").unlink()
    elif damage == "script":
        (layer / "bin/pip").unlink()
        (layer / "bin/pip").symlink_to(base / "bin/pip")
    elif damage == "library":
        (layer / "lib/libgmp.so.10").unlink()
        (layer / "lib/libgmp.so.10").symlink_to("/elsewhere/libgmp.so.10")
    elif damage == "record":
        (layer / "conda-meta/gmp-6.3.0-h1.json").write_text('{"name": "gmp"}')
    elif damage == "time":
        os.utime(layer / "conda-meta/gmp-6.3.0-h1.json", ns=(1, 1))
    elif damage == "pins":
        (layer / "conda-meta/pinned").write_text("gmp==6.3.0=h1\n")
    elif damage == "packages":
        _write(layer / "lib/python3.14/site-packages/y.py")
    elif damage == "launcher":
        _write(base / "bin/late", f"#!{base}/bin/python3.14\n")
        shutil.copyfile(base / "bin/late", layer / "bin/late")
    else:
        _write(base / "pkgs/x.conda")
    problems = layout.check_layer(base, layer, alias)
    assert any(expected in problem for problem in problems), problems


def test_the_worker_restores_the_layer_as_the_build_wrote_it(base: Path, tmp_path: Path) -> None:
    """provisioning.py's slim records, layer entries, repair and base identity are the
    build's: a record and entries the worker writes back read as the build wrote them, so
    the package fingerprint does not move."""
    from sandbox_science.worker import provisioning

    layer = _make(base, tmp_path)
    for name in ("python-3.14.8-h0.json", "gmp-6.3.0-h1.json"):
        record = json.loads((base / "conda-meta" / name).read_text())
        assert provisioning.slim_record(base, record) == layout.slim(base, record)
    site = str(base / "lib/python3.14/site-packages")
    places = provisioning.Places(
        str(layer / "conda-meta"), (), None, str(base / "conda-meta"), (site,)
    )
    assert provisioning.base_identity(places) == layout.base_identity(base)
    before = provisioning.fingerprint(places)

    def records() -> dict[str, tuple[bytes, int]]:
        found = (layer / "conda-meta").glob("*.json")
        return {path.name: (path.read_bytes(), path.stat().st_mtime_ns) for path in found}

    written = records()
    # A failed transaction took gmp's record and its entries.
    (layer / "conda-meta/gmp-6.3.0-h1.json").unlink()
    for name in ("libgmp.so", "libgmp.so.10.5.0"):
        (layer / "lib" / name).unlink()
    assert provisioning.repair_layer(layer, base) == ["gmp"]
    assert records() == written
    assert os.readlink(layer / "lib/libgmp.so") == str(base / "lib/libgmp.so")
    assert provisioning.fingerprint(places) == before
    assert layout.check_layer(base, layer, tmp_path / "alias") in (
        [],
        [f"{base} is writable by others than root"],
    )
