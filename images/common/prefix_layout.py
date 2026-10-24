# SPDX-License-Identifier: MIT
"""The research prefix's image layout (docs/IMAGES.md, "Prefix layout").

  prefix_layout.py trim PREFIX           remove what a sandbox never reads
  prefix_layout.py freeze PREFIX FROZEN  move the frozen trees out of the workspace seed
  prefix_layout.py check PREFIX FROZEN   check the layout an image build left

The research prefix lives in the workspace, and OpenShell copies the image's workspace into
every new environment's volume, file by file, within a fixed grace period. ``trim`` removes
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
FROZEN = Path("/opt/sandbox-science-frozen")
MANIFEST = "manifest.json"
MANIFEST_VERSION = 1

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
    pinned = (prefix / "conda-meta" / "pinned").read_text().splitlines()
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
    args = parser.parse_args(argv)
    if args.command == "trim":
        print(json.dumps({"trimmed": trim(args.prefix)}))
        return 0
    if args.command == "freeze":
        print(json.dumps({"frozen": freeze(args.prefix, args.frozen)}))
        return 0
    problems = check(args.prefix, args.frozen)
    for problem in problems:
        print(f"prefix layout: {problem}", file=sys.stderr)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
