# SPDX-License-Identifier: MIT
"""The research prefix's image layout (docs/IMAGES.md, "Prefix layout").

  prefix_layout.py trim PREFIX                    remove what a sandbox never reads
  prefix_layout.py freeze PREFIX FROZEN           move the frozen trees to their read-only path
  prefix_layout.py layer BASE LAYER               the workspace's layer over the base (B11)
  prefix_layout.py check PREFIX FROZEN [--layer]  check the layout an image build left

The research images keep their Conda environment, the base, in the image at
``/opt/sandbox-science-base``, read-only. OpenShell copies the image's workspace into every new
environment's volume, file by file, within a fixed grace period; what it copies of the
environment is only ``layer``'s output: a small writable environment at the workspace prefix's
path that runtime installs go into, a Python virtual environment over the base's interpreter that
is also a Conda prefix (docs/IMAGES.md, "The environment outside the workspace"). ``trim`` removes
documentation, conda's package test files and static libraries that sit beside a shared one
(the compiler toolchain's own archives stay). ``freeze`` moves large trees nobody installs into
at runtime (GAP, Giac, Maxima and ECL, the compiler toolchain) to a read-only image path with the
same relative layout, leaves an absolute symlink at each original place, pins the Conda packages
that own them (``conda-meta/pinned``) and writes a manifest the worker checks the symlinks
against. It runs as root, in the same build step as the Conda install, so neither the moved nor
the removed files stay in a lower image layer. The image's own Python runs it: standard library
only.
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import os
import shutil
import stat
import subprocess
import sys
from collections.abc import Iterable, Iterator
from pathlib import Path

PREFIX = Path("/sandbox/.sandbox-science-prefix")
"""The workspace prefix: the layer in an image of this layout, the whole environment in a
workspace seeded from an earlier one."""
FROZEN = Path("/opt/sandbox-science-frozen")
BASE = Path("/opt/sandbox-science-base")
"""The research images' Conda environment, read-only (workspace_prefix.BASE)."""
ALIAS = Path("/opt/sandbox-science")
"""The stable path of the interpreter, kernelspecs and launchers: a link to ``PREFIX``."""
LAYER_MARKER = ".sandbox-science-layer"
"""The layer's marker, which tells it from a whole prefix (workspace_prefix.LAYER_MARKER)."""
LAYER_VERSION = 1
MANIFEST = "manifest.json"
MANIFEST_VERSION = 2
"""2 since the layer: an older worker refuses the manifest, and with it these images, rather
than taking the layer for a whole environment (docs/COMPATIBILITY.md). Every research image
has one, minimal's with no trees."""
SLIM = ("files", "paths_data")
"""What a base package's record in the layer replaces: its file list becomes the layer's own
entries for the package (its programs and shared libraries), which micromamba removes before
it links a replacement, and the rest is what it solves with."""

DOCUMENTATION = ("share/doc", "share/man", "share/gtk-doc", "etc/conda/test-files")
"""Read by people, never by a program in the sandbox. conda's test files are its packages'
build-time tests (Sage's ship 4,000 source files). ``share/info`` stays: Maxima reads its help
index there as it starts, and it is 65 files."""
NOTICES = ("*licen[cs]e*", "*copying*", "*copyright*", "*notice*")
"""License texts among the documentation, which stay with the software they cover."""
TOOLCHAIN = ("lib/gcc", "libexec/gcc", "*-conda-linux-gnu")
"""The compilers' own trees, whose static archives (libgcc.a, libc_nonshared.a, crt objects)
the compilers link with; ``trim`` leaves them alone."""
FROZEN_TREES = ("share/gap", "share/giac", "share/maxima", "lib/ecl-*", *TOOLCHAIN)
"""Relative to the prefix; a pattern matches entries of its parent directory.

Data trees and the compilers, whose programs find their libraries without ``$ORIGIN``. The
dynamic linker resolves ``$ORIGIN`` from a program's real place, so a program moved here
would look for its prefix's ``lib`` under this path: Maxima's (``lib/maxima``, one file)
stays in the prefix for that. ``check`` runs ``ldd`` on every program here."""
LDD_EXEMPT = ("*.fas", "ecl_min")
"""ECL's compiled modules, loaded into a running ECL that has its library already, and the
bootstrap compiler ECL's own build used."""


def _matches(prefix: Path, patterns: Iterable[str]) -> list[str]:
    """The real directories under ``prefix`` that ``patterns`` name, relative, sorted."""
    found: set[str] = set()
    for pattern in patterns:
        parent, _, name = pattern.rpartition("/")
        directory = prefix / parent if parent else prefix
        try:
            entries = os.listdir(directory)
        except FileNotFoundError:
            continue
        for entry in entries:
            path = directory / entry
            if fnmatch.fnmatchcase(entry, name) and path.is_dir() and not path.is_symlink():
                found.add(f"{parent}/{entry}" if parent else entry)
    return sorted(found)


def _within(relative: str, trees: Iterable[str]) -> bool:
    return any(relative == tree or relative.startswith(f"{tree}/") for tree in trees)


def _walk(root: Path) -> Iterator[tuple[Path, list[str], list[str]]]:
    for directory, dirs, files in os.walk(root):
        yield Path(directory), dirs, files


# Trimming.


def _notice(name: str) -> bool:
    return any(fnmatch.fnmatchcase(name.lower(), pattern) for pattern in NOTICES)


def _remove(path: Path) -> tuple[int, int]:
    """Remove ``path`` but the license texts under it; returns the files and bytes removed."""
    if path.is_symlink() or not path.is_dir():
        size = path.lstat().st_size
        path.unlink()
        return 1, size
    files = size = 0
    for directory, dirs, names in os.walk(path, topdown=False):
        for name in [*names, *(d for d in dirs if Path(directory, d).is_symlink())]:
            if not _notice(name):
                files += 1
                size += Path(directory, name).lstat().st_size
                Path(directory, name).unlink()
        for name in dirs:
            if not Path(directory, name).is_symlink() and not os.listdir(Path(directory, name)):
                Path(directory, name).rmdir()
    if not os.listdir(path):
        path.rmdir()
    return files, size


def static_libraries(prefix: Path) -> list[Path]:
    """Static archives with a shared library of the same name beside them, outside the
    toolchain: ``lib/libpari.a`` beside ``lib/libpari.so``. An archive alone, such as NumPy's
    ``libnpymath.a``, is all there is to link with and stays."""
    toolchain = _matches(prefix, TOOLCHAIN)
    found: list[Path] = []
    for directory, dirs, names in _walk(prefix):
        relative = directory.relative_to(prefix).as_posix()
        if relative != "." and _within(relative, toolchain):
            dirs[:] = []
            continue
        dirs[:] = [d for d in dirs if not (directory / d).is_symlink()]
        entries = set(names)
        for name in names:
            if not name.endswith(".a"):
                continue
            stem = name[:-2]
            if f"{stem}.so" in entries or any(e.startswith(f"{stem}.so.") for e in entries):
                found.append(directory / name)
    return sorted(found)


def trim(prefix: Path) -> dict[str, list[int]]:
    """Remove documentation and redundant static libraries; returns files and bytes removed."""
    removed: dict[str, list[int]] = {}
    for relative in DOCUMENTATION:
        path = prefix / relative
        if path.exists() or path.is_symlink():
            removed[relative] = list(_remove(path))
    archives = [0, 0]
    for path in static_libraries(prefix):
        archives[0] += 1
        archives[1] += path.lstat().st_size
        path.unlink()
    removed["static libraries"] = archives
    return removed


# Freezing.


def _rewrite_links(prefix: Path, frozen: Path, tree: str) -> int:
    """Make the moved tree's relative links that leave it absolute, through the prefix.

    ``lib/gcc/…/libgomp.so -> ../../../libgomp.so`` meant the prefix's ``lib``; moved, the
    same text would mean the frozen copy's. A link that stays within its own tree keeps its
    relative text, which the mirrored layout resolves alike."""
    rewritten = 0
    for directory, dirs, names in _walk(frozen / tree):
        for name in [*names, *(d for d in dirs if (directory / d).is_symlink())]:
            link = directory / name
            if not link.is_symlink():
                continue
            target = os.readlink(link)
            if os.path.isabs(target):
                continue
            logical = os.path.normpath(
                os.path.join(directory.relative_to(frozen).as_posix(), target)
            )
            if logical.startswith("../") or logical == "..":
                raise SystemExit(f"{link} -> {target} leaves the prefix")
            if _within(logical, [tree]):
                continue
            link.unlink()
            link.symlink_to(prefix / logical)
            rewritten += 1
    return rewritten


def _owners(prefix: Path, trees: list[str]) -> list[dict[str, str]]:
    """The Conda packages with a file in one of ``trees``: name, version and build."""
    owners: dict[str, dict[str, str]] = {}
    for record in sorted((prefix / "conda-meta").glob("*.json")):
        data = json.loads(record.read_text())
        if any(_within(path, trees) for path in data.get("files", [])):
            owners[data["name"]] = {
                "name": data["name"],
                "version": data["version"],
                "build": data["build"],
            }
    return [owners[name] for name in sorted(owners)]


def pin(package: dict[str, str]) -> str:
    """A ``conda-meta/pinned`` line holding ``package`` at its installed build."""
    return f"{package['name']}=={package['version']}={package['build']}"


def _snapshot(prefix: Path) -> list[tuple[str, int, int]]:
    """What the worker's package fingerprint reads (provisioning.fingerprint): the
    ``conda-meta`` records and the site-packages metadata directories, by size and mtime."""
    places = [prefix / "conda-meta", *sorted(prefix.glob("lib/python3*/site-packages"))]
    found: list[tuple[str, int, int]] = []
    for place in places:
        for entry in sorted(os.scandir(place), key=lambda e: e.name):
            if entry.name.endswith((".json", ".dist-info", ".egg-info")):
                info = entry.stat(follow_symlinks=False)
                found.append((f"{place}/{entry.name}", info.st_size, info.st_mtime_ns))
    return found


def _read_only(frozen: Path) -> None:
    """Root's, and writable by nobody else, links included. (Run as another user, as the
    tests do, only the write permissions are taken away.)"""
    for directory, dirs, names in _walk(frozen):
        for name in [".", *dirs, *names]:
            path = directory / name
            if os.geteuid() == 0:
                os.chown(path, 0, 0, follow_symlinks=False)
            info = path.lstat()
            if not stat.S_ISLNK(info.st_mode):
                path.chmod(stat.S_IMODE(info.st_mode) & ~0o022)


def freeze(prefix: Path, frozen: Path) -> dict[str, object]:
    trees = _matches(prefix, FROZEN_TREES)
    if not trees:
        return {"trees": [], "packages": []}
    before = _snapshot(prefix)
    packages = _owners(prefix, trees)
    resolved = {}
    for tree in trees:
        for directory, dirs, names in _walk(prefix / tree):
            for name in [*names, *dirs]:
                if (directory / name).is_symlink():
                    resolved[(directory / name).relative_to(prefix)] = (directory / name).exists()
    rewritten = 0
    for tree in trees:
        target = frozen / tree
        target.parent.mkdir(parents=True, exist_ok=True)
        # A rename, or a copy where the tree is in a lower layer of the build's filesystem.
        shutil.move(prefix / tree, target)
        (prefix / tree).symlink_to(target)
        os.chown(prefix / tree, *_owner((prefix / tree).parent), follow_symlinks=False)
    for tree in trees:
        rewritten += _rewrite_links(prefix, frozen, tree)
    broken = [str(path) for path, ok in resolved.items() if ok and not (prefix / path).exists()]
    if broken:
        raise SystemExit(f"links that resolved before the move no longer do: {broken[:10]}")
    pinned = prefix / "conda-meta" / "pinned"
    existing = pinned.read_text().splitlines() if pinned.exists() else []
    lines = [*existing, *(pin(package) for package in packages if pin(package) not in existing)]
    pinned.write_text("".join(f"{line}\n" for line in lines))
    os.chown(pinned, *_owner(prefix / "conda-meta"))
    manifest = {
        "version": MANIFEST_VERSION,
        "prefix": str(prefix),
        "frozen": str(frozen),
        "trees": trees,
        "packages": packages,
    }
    (frozen / MANIFEST).write_text(json.dumps(manifest, indent=1) + "\n")
    _read_only(frozen)
    if _snapshot(prefix) != before:
        raise SystemExit("freezing changed what the package fingerprint reads")
    return {**manifest, "links_rewritten": rewritten}


def _owner(path: Path) -> tuple[int, int]:
    info = path.stat()
    return info.st_uid, info.st_gid


# The layer.


def _python_version(base: Path) -> str:
    """The base's Python version, from its Conda record."""
    for record in sorted((base / "conda-meta").glob("python-[0-9]*.json")):
        return str(json.loads(record.read_text())["version"])
    raise SystemExit(f"no python record in {base / 'conda-meta'}")


def _launcher_lines(data: bytes, old: bytes, new: bytes) -> bytes | None:
    """``data`` with ``old`` replaced by ``new`` in its first two lines, where a script names
    its interpreter (``#!`` and pip's ``'''exec'`` form); None when they do not name it."""
    if not data.startswith(b"#!"):
        return None
    lines = data.split(b"\n", 2)
    head = [line.replace(old, new) for line in lines[:2]]
    if head == lines[:2]:
        return None
    return b"\n".join([*head, *lines[2:]])


def launchers(base: Path, alias: Path = ALIAS) -> int:
    """Make the base's Python scripts and kernelspecs start ``alias``'s interpreter, the
    layer's, so that every Python program sees what runtime installs added; returns how many
    files changed. Other interpreters (perl, sh) keep their lines."""
    old, new = f"{base}/bin/python".encode(), f"{alias}/bin/python".encode()
    changed = 0
    for entry in sorted(os.scandir(base / "bin"), key=lambda e: e.name):
        if entry.is_symlink() or not entry.is_file():
            continue
        path = Path(entry.path)
        with path.open("rb") as stream:
            data = stream.read()
        rewritten = _launcher_lines(data, old, new)
        if rewritten is None:
            continue
        info = path.lstat()
        path.write_bytes(rewritten)
        os.chmod(path, stat.S_IMODE(info.st_mode))
        changed += 1
    for spec in sorted((base / "share/jupyter/kernels").glob("*/kernel.json")):
        document = json.loads(spec.read_text())
        argv = document.get("argv")
        if isinstance(argv, list) and argv and str(argv[0]).startswith(old.decode()):
            argv[0] = new.decode() + str(argv[0])[len(old) :]
            spec.write_text(json.dumps(document, indent=1) + "\n")
            changed += 1
    return changed


def _script(path: Path) -> bool:
    """A text file with a shebang: the layer copies it, so that an upgrade that rewrites it
    writes the layer's copy, never through a link into the read-only base."""
    if path.is_symlink() or not path.is_file():
        return False
    with path.open("rb") as stream:
        return stream.read(2) == b"#!"


def _entry(base: Path, relative: str) -> bool:
    """Whether the layer has its own entry for the base's file ``relative``: each program in
    ``bin``, and each shared library at the top of ``lib``."""
    folder, _, name = relative.partition("/")
    if not name or "/" in name:
        return False
    if folder == "bin":
        return True
    path = base / relative
    return folder == "lib" and ".so" in name and (path.is_symlink() or not path.is_dir())


def slim(base: Path, record: dict[str, object]) -> dict[str, object]:
    """A base package's record in the layer: its file list names only the layer's entries for
    it (provisioning.slim_record says the same, a test keeps them in step)."""
    files = record.get("files")
    listed = [str(item) for item in files] if isinstance(files, list) else []
    own = [relative for relative in listed if _entry(base, relative)]
    return {key: value for key, value in record.items() if key not in SLIM} | {"files": own}


def make_entry(base: Path, layer: Path, relative: str) -> None:
    """The layer's entry for the base's ``relative``: a copy of a script, else a link."""
    source, target = base / relative, layer / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    if _script(source):
        shutil.copyfile(source, target)
        os.chmod(target, stat.S_IMODE(source.stat().st_mode))
    else:
        target.symlink_to(source)


def base_identity(base: Path) -> str:
    """What the base holds, by its Conda records' and distributions' names, which carry their
    versions and builds, and not by their files' times, so that an image rebuilt with the same
    packages has the same identity (provisioning.base_identity)."""
    import hashlib

    records = sorted(path.name for path in (base / "conda-meta").glob("*.json"))
    distributions = sorted(
        {
            path.name
            for site in base.glob("lib/python3*/site-packages")
            for path in site.iterdir()
            if path.name.endswith((".dist-info", ".egg-info"))
        }
    )
    text = "\n".join(records) + "\0" + "\n".join(distributions)
    return hashlib.sha256(text.encode()).hexdigest()


def _python_pin(base: Path) -> str:
    for record in sorted((base / "conda-meta").glob("python-[0-9]*.json")):
        data = json.loads(record.read_text())
        return pin({"name": "python", "version": data["version"], "build": data["build"]})
    raise SystemExit(f"no python record in {base / 'conda-meta'}")


def layer(
    base: Path,
    layer: Path,
    alias: Path = ALIAS,
    *,
    frozen: Path = FROZEN,
    revision: str = "unknown",
) -> dict[str, object]:
    """Write the workspace's layer over ``base`` at ``layer``, which must not exist.

    A virtual environment of the base's interpreter, so that its Python's ``sys.prefix`` is
    the layer, which pip, ``python -m pip``, uv and Jupyter install into unconfigured, and the
    layer's ``site-packages`` comes before the base's. It is a Conda prefix too: ``conda-meta``
    holds each base package's record with only the layer's entries for it as its files, so
    micromamba solves against the base, links only new packages here, and removes the layer's
    entries of a package it replaces before linking the new one. ``bin`` holds a copy of each
    base script and a link to each other program, ``lib`` a link to each shared library:
    ``/opt/sandbox-science/bin/<program>`` keeps working, and a package installed here finds
    the base's libraries through its ``$ORIGIN/../lib``. Each record keeps the time of the
    base's, so that one restored after a failed transaction reads as before. The layer pins
    Python at the base's build, since the layer's interpreter is the base's. The marker names
    the base, the image's revision and the base's identity; the frozen trees' manifest is
    written too, with no trees where nothing is frozen.
    """
    version = _python_version(base)
    layer.mkdir()
    (layer / "pyvenv.cfg").write_text(
        f"home = {base}/bin\ninclude-system-site-packages = true\nversion = {version}\n"
    )
    marker = {
        "version": LAYER_VERSION,
        "base": str(base),
        "revision": revision,
        "identity": base_identity(base),
    }
    (layer / LAYER_MARKER).write_text(json.dumps(marker) + "\n")
    counts = {"scripts": 0, "programs": 0, "libraries": 0, "records": 0}
    (layer / "bin").mkdir()
    for entry in sorted(os.scandir(base / "bin"), key=lambda e: e.name):
        make_entry(base, layer, f"bin/{entry.name}")
        counts["scripts" if _script(Path(entry.path)) else "programs"] += 1
    minor = ".".join(version.split(".")[:2])
    (layer / "lib" / f"python{minor}" / "site-packages").mkdir(parents=True)
    for entry in sorted(os.scandir(base / "lib"), key=lambda e: e.name):
        if _entry(base, f"lib/{entry.name}"):
            make_entry(base, layer, f"lib/{entry.name}")
            counts["libraries"] += 1
    (layer / "conda-meta").mkdir()
    for record in sorted((base / "conda-meta").iterdir()):
        if record.name.endswith(".json"):
            target = layer / "conda-meta" / record.name
            target.write_text(json.dumps(slim(base, json.loads(record.read_text()))) + "\n")
            info = record.stat()
            os.utime(target, ns=(info.st_atime_ns, info.st_mtime_ns))
            counts["records"] += 1
        elif record.name == "history":
            shutil.copyfile(record, layer / "conda-meta" / record.name)
    pinned = base / "conda-meta" / "pinned"
    lines = pinned.read_text().splitlines() if pinned.exists() else []
    (layer / "conda-meta" / "pinned").write_text(
        "".join(f"{line}\n" for line in [*lines, _python_pin(base)])
    )
    if not (frozen / MANIFEST).exists():
        frozen.mkdir(parents=True, exist_ok=True)
        manifest = {
            "version": MANIFEST_VERSION,
            "prefix": str(base),
            "frozen": str(frozen),
            "trees": [],
            "packages": [],
        }
        (frozen / MANIFEST).write_text(json.dumps(manifest, indent=1) + "\n")
        _read_only(frozen)
    entries = sum(len(dirs) + len(names) for _, dirs, names in _walk(layer))
    return {"base": str(base), "layer": str(layer), "python": version, **counts, "entries": entries}


def check_layer(base: Path, layer: Path, alias: Path = ALIAS) -> list[str]:
    """What is wrong with the layer and the base an image build left; empty when nothing is."""
    problems: list[str] = []
    if layer.is_symlink() or not layer.is_dir():
        return [f"{layer} is not a directory"]
    try:
        marker = json.loads((layer / LAYER_MARKER).read_text())
    except (OSError, ValueError):
        marker = {}
    if (
        not isinstance(marker, dict)
        or marker.get("version") != LAYER_VERSION
        or marker.get("base") != str(base)
        or marker.get("identity") != base_identity(base)
        or not isinstance(marker.get("revision"), str)
    ):
        problems.append(f"{layer / LAYER_MARKER} does not name {base} and its identity")
    config = (layer / "pyvenv.cfg").read_text() if (layer / "pyvenv.cfg").is_file() else ""
    for line in (f"home = {base}/bin", "include-system-site-packages = true"):
        if line not in config.splitlines():
            problems.append(f"{layer / 'pyvenv.cfg'} lacks {line!r}")
    for folder in ("bin", "lib"):
        for entry in os.scandir(base / folder):
            relative = f"{folder}/{entry.name}"
            if not _entry(base, relative):
                continue
            path = layer / relative
            if _script(Path(entry.path)):
                good = _script(path) and path.read_bytes() == Path(entry.path).read_bytes()
            else:
                good = path.is_symlink() and os.readlink(path) == entry.path
            if not good:
                kind = "copy of the base's script" if _script(Path(entry.path)) else "link"
                problems.append(f"{path} is not a {kind} to the base's {folder} entry")
    records = {record.name for record in (base / "conda-meta").glob("*.json")}
    slims = {record.name for record in (layer / "conda-meta").glob("*.json")}
    if records != slims:
        problems.append(f"{layer / 'conda-meta'} differs from the base's records")
    for name in sorted(records & slims):
        expected = slim(base, json.loads((base / "conda-meta" / name).read_text()))
        found = layer / "conda-meta" / name
        if json.loads(found.read_text()) != expected or (
            found.stat().st_mtime_ns != (base / "conda-meta" / name).stat().st_mtime_ns
        ):
            problems.append(f"{found} is not the base's record")
    pinned = base / "conda-meta" / "pinned"
    expected_pins = [*(pinned.read_text().splitlines() if pinned.exists() else [])]
    expected_pins.append(_python_pin(base))
    layer_pins = layer / "conda-meta" / "pinned"
    if not layer_pins.exists() or layer_pins.read_text().splitlines() != expected_pins:
        problems.append(f"{layer_pins} differs from the base's pins and Python's")
    if any(layer.glob("lib/python3*/site-packages/*")):
        problems.append(f"{layer} has packages of its own")
    old = f"{base}/bin/python".encode()
    for entry in os.scandir(base / "bin"):
        if entry.is_file() and not entry.is_symlink():
            with open(entry.path, "rb") as stream:
                head = stream.read(4096)
            if _launcher_lines(head, old, b"") is not None:
                problems.append(f"{entry.path} starts the base's interpreter, not the layer's")
    for spec in (base / "share/jupyter/kernels").glob("*/kernel.json"):
        argv = json.loads(spec.read_text()).get("argv") or [""]
        if str(argv[0]).startswith(old.decode()):
            problems.append(f"{spec} starts the base's interpreter, not the layer's")
    if (base / "pkgs").is_dir() and any((base / "pkgs").iterdir()):
        problems.append(f"{base / 'pkgs'} is not empty")
    for directory, dirs, names in _walk(base):
        for name in [".", *dirs, *names]:
            info = (directory / name).lstat()
            if info.st_uid != 0 or (not stat.S_ISLNK(info.st_mode) and info.st_mode & 0o022):
                problems.append(f"{directory / name} is writable by others than root")
                return problems
    return problems


# Checking.


def unresolved(frozen: Path, trees: Iterable[str]) -> list[str]:
    """Programs and libraries in the frozen trees that ``ldd`` finds a library missing for."""
    problems: list[str] = []
    for tree in trees:
        for directory, _, names in _walk(frozen / tree):
            for name in names:
                path = directory / name
                if path.is_symlink() or any(fnmatch.fnmatchcase(name, e) for e in LDD_EXEMPT):
                    continue
                if not (os.access(path, os.X_OK) or ".so" in name):
                    continue
                with path.open("rb") as stream:
                    if stream.read(4) != b"\x7fELF":
                        continue
                # The image's own ldd, on the image's own files.
                done = subprocess.run(  # noqa: S603
                    ["/usr/bin/ldd", str(path)], capture_output=True, text=True, check=False
                )
                missing = [
                    line.split()[0] for line in done.stdout.splitlines() if "not found" in line
                ]
                if missing:
                    problems.append(f"{path} misses {', '.join(missing)}")
    return problems


def check(prefix: Path, frozen: Path, owner: int = 0) -> list[str]:
    """What is wrong with the layout an image build left; empty when nothing is. The frozen
    trees must belong to ``owner``, root in an image."""
    problems = [
        f"{directory / name} is documentation"
        for relative in DOCUMENTATION
        for directory, _, names in _walk(prefix / relative)
        for name in names
        if not _notice(name)
    ]
    problems += [f"{path} has a shared library beside it" for path in static_libraries(prefix)]
    manifest_path = frozen / MANIFEST
    if not manifest_path.exists():
        return problems  # an image that freezes nothing, such as minimal
    manifest = json.loads(manifest_path.read_text())
    if manifest.get("version") != MANIFEST_VERSION or manifest.get("prefix") != str(prefix):
        problems.append(f"{manifest_path} is for another layout")
    pinned_path = prefix / "conda-meta" / "pinned"
    pinned = pinned_path.read_text().splitlines() if pinned_path.exists() else []
    for tree in manifest["trees"]:
        link = prefix / tree
        if not link.is_symlink() or os.readlink(link) != str(frozen / tree):
            problems.append(f"{link} is not a link to {frozen / tree}")
        if (frozen / tree).is_symlink() or not (frozen / tree).is_dir():
            problems.append(f"{frozen / tree} is not a directory")
    for package in manifest["packages"]:
        if pin(package) not in pinned:
            problems.append(f"{package['name']} is not pinned")
    problems += unresolved(frozen, manifest["trees"])
    for directory, dirs, names in _walk(frozen):
        for name in [".", *dirs, *names]:
            info = (directory / name).lstat()
            if info.st_uid != owner or (not stat.S_ISLNK(info.st_mode) and info.st_mode & 0o022):
                problems.append(f"{directory / name} is writable by others than its owner")
                return problems
    return problems


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="The research prefix's image layout.")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("trim").add_argument("prefix", type=Path)
    for name in ("freeze", "check"):
        command = sub.add_parser(name)
        command.add_argument("prefix", type=Path)
        command.add_argument("frozen", type=Path)
    sub.choices["check"].add_argument(
        "--layer", type=Path, help="also check this layer over the prefix, the base"
    )
    command = sub.add_parser("layer")
    command.add_argument("base", type=Path)
    command.add_argument("layer", type=Path)
    command.add_argument("--revision", default="unknown", help="the image's recipe revision")
    args = parser.parse_args(argv)
    if args.command == "trim":
        print(json.dumps({"trimmed": trim(args.prefix)}))
        return 0
    if args.command == "freeze":
        print(json.dumps({"frozen": freeze(args.prefix, args.frozen)}))
        return 0
    if args.command == "layer":
        rewritten = launchers(args.base)
        made = layer(args.base, args.layer, revision=args.revision)
        print(json.dumps({"layer": made, "launchers": rewritten}))
        return 0
    problems = check(args.prefix, args.frozen)
    if args.layer is not None:
        problems += check_layer(args.prefix, args.layer)
    for problem in problems:
        print(f"prefix layout: {problem}", file=sys.stderr)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
