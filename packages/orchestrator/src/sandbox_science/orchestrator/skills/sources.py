# SPDX-License-Identifier: MIT
"""A skill pack's files, read from git or from an archive on the user's machine
(docs/specs/skills.md, SK-1, SK-2, SK-13; docs/SKILLS.md).

A git source is fetched at one commit into a temporary bare repository with the user's
git, and its files are read from git's objects, so nothing in the pack is written out as
a file. An archive is read in memory. Only the skills' directory is kept: ``skills/``
when the pack has skills there, else its top, or the directory the user names. Entries
that are not regular files, paths that are not plain relative ones and files over
``MAX_FILE_BYTES`` are left out and listed; a pack over ``MAX_FILES`` files or
``MAX_PACK_BYTES`` bytes is refused.
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import os
import re
import stat
import subprocess
import tarfile
import tempfile
import zipfile
from collections.abc import Callable
from dataclasses import dataclass, replace
from functools import partial
from pathlib import Path
from typing import Final

from sandbox_science.orchestrator.control import ControlError
from sandbox_science.orchestrator.control.errors import INVALID_REQUEST, NOT_FOUND, UNAVAILABLE

MAX_FILE_BYTES: Final = 1024 * 1024
MAX_PACK_BYTES: Final = 32 * 1024 * 1024
MAX_FILES: Final = 4096
GIT_SECONDS: Final = 300.0
_REF: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,199}")
_SHORT_HASH: Final = re.compile(r"[0-9a-f]{4,39}")
_SKILLS: Final = re.compile(r"skills/[^/]+/SKILL\.md")
_CONTROL: Final = re.compile(r"[\x00-\x1f\x7f]")

Skipped = dict[str, str]
"""A path left out, and why: ``{"path": ..., "reason": ...}``."""


@dataclass(frozen=True)
class PackFile:
    path: str
    """Relative to the skills' directory."""
    content: bytes
    executable: bool = False


@dataclass(frozen=True)
class Fetched:
    """A pack's files at one revision, and where they came from."""

    kind: str
    """``git`` or ``archive``."""
    origin: str
    """The git URL or the repository's absolute path, or the archive's file name."""
    root: str
    """The directory the skills are in, ``""`` for the pack's top."""
    files: tuple[PackFile, ...]
    skipped: tuple[Skipped, ...]
    ref: str | None = None
    commit: str | None = None
    default_branch: str | None = None
    """The repository's default branch, when no ref was given."""
    archive_sha256: str | None = None


@dataclass(frozen=True)
class _Entry:
    path: str
    kind: str
    """``file``, ``executable``, or what else the entry is (``symbolic link``...)."""
    size: int
    read: Callable[[], bytes]


def from_git(origin: str, ref: str | None = None, root: str | None = None) -> Fetched:
    """The pack at ``ref`` of the repository ``origin``, a URL or a local path."""
    if not origin or origin.startswith("-") or _CONTROL.search(origin):
        raise ControlError(INVALID_REQUEST, "give a git URL or a repository's path")
    if ref is not None and (not _REF.fullmatch(ref) or ".." in ref):
        raise ControlError(INVALID_REQUEST, f"{ref!r} is not a branch, a tag or a commit hash")
    local = Path(origin).expanduser()
    if local.is_dir():
        origin = str(local.resolve())
    with tempfile.TemporaryDirectory(prefix="sandbox-skills-") as repository:
        _git(repository, "init", "-q", "--bare")
        try:
            _git(repository, "fetch", "-q", "--depth", "1", "--no-tags", origin, ref or "HEAD")
        except ControlError as error:
            if ref is not None and _SHORT_HASH.fullmatch(ref):
                raise ControlError(
                    NOT_FOUND, "give a commit's full hash, or a branch or a tag"
                ) from error
            raise
        commit = _git(repository, "rev-parse", "--verify", "FETCH_HEAD^{commit}").decode().strip()
        default = None
        if ref is None:
            with contextlib.suppress(ControlError):
                said = _git(repository, "ls-remote", "--symref", origin, "HEAD").decode()
                found = re.match(r"ref: refs/heads/(\S+)\tHEAD", said)
                default = found[1] if found else None
        listing = _git(repository, "ls-tree", "-r", "-l", "-z", "--full-tree", commit)
        entries = _tree(listing)
        chosen, skipped, top = _select(entries, root)
        blobs = _blobs(repository, [entry.read() for entry in chosen])
    files = tuple(
        PackFile(e.path, content, e.kind == "executable")
        for e, content in zip(chosen, blobs, strict=True)
    )
    return Fetched("git", origin, top, files, skipped, ref, commit, default)


def from_archive(path: Path, root: str | None = None) -> Fetched:
    """The pack in a ``.tar``, ``.tar.gz`` or ``.zip`` file."""
    if path.stat().st_size > MAX_PACK_BYTES:
        raise ControlError(INVALID_REQUEST, "the archive is larger than 32 MiB")
    data = path.read_bytes()
    entries: list[_Entry] = []
    if zipfile.is_zipfile(io.BytesIO(data)):
        archive = zipfile.ZipFile(io.BytesIO(data))
        for info in archive.infolist():
            mode = info.external_attr >> 16
            kind = "symbolic link" if stat.S_ISLNK(mode) else _regular(mode)
            if not info.is_dir():
                entries.append(_Entry(info.filename, kind, info.file_size, _zipped(archive, info)))
    else:
        try:
            tar = tarfile.open(fileobj=io.BytesIO(data), mode="r:*")  # noqa: SIM115 - in memory
        except tarfile.TarError:
            raise ControlError(INVALID_REQUEST, "the file is not a tar or zip archive") from None
        for count, member in enumerate(tar, 1):
            if count > 4 * MAX_FILES:
                raise ControlError(INVALID_REQUEST, "the archive has too many entries")
            link = "symbolic link" if member.issym() or member.islnk() else "special file"
            kind = _regular(member.mode) if member.isreg() else link
            if not member.isdir():
                read = partial(_tarred, tar, member)
                entries.append(_Entry(member.name, kind, member.size, read))
    entries = _unwrapped(entries)
    chosen, skipped, top = _select(entries, root)
    files = tuple(PackFile(e.path, e.read(), e.kind == "executable") for e in chosen)
    digest = hashlib.sha256(data).hexdigest()
    return Fetched("archive", path.name, top, files, skipped, archive_sha256=digest)


def _git(repository: str, *args: str, data: bytes | None = None) -> bytes:
    try:
        done = subprocess.run(  # noqa: S603 - the user's own git, on the user's own request
            ["git", *args],  # noqa: S607 - git on the user's PATH
            cwd=repository,
            input=data,
            capture_output=True,
            timeout=GIT_SECONDS,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
            check=False,
        )
    except FileNotFoundError:
        raise ControlError(UNAVAILABLE, "git is not installed") from None
    except subprocess.TimeoutExpired:
        raise ControlError(UNAVAILABLE, f"git {args[0]} took over {GIT_SECONDS:g} s") from None
    if done.returncode != 0:
        lines = done.stderr.decode(errors="replace").strip().splitlines()
        raise ControlError(NOT_FOUND, f"git {args[0]}: {lines[-1] if lines else 'failed'}"[:500])
    return done.stdout


def _tree(listing: bytes) -> list[_Entry]:
    """``ls-tree -r -l -z`` as entries whose ``read`` gives the blob's object ID."""
    kinds = {"100644": "file", "100755": "executable", "120000": "symbolic link"}
    entries: list[_Entry] = []
    for record in listing.split(b"\0"):
        if record:
            meta, _, name = record.partition(b"\t")
            mode, _, oid, size = meta.decode().split()
            kind = kinds.get(mode, "submodule" if mode == "160000" else "special file")
            sized = int(size) if size.isdigit() else 0
            entries.append(_Entry(name.decode(errors="replace"), kind, sized, oid.encode))
    return entries


def _blobs(repository: str, oids: list[bytes]) -> list[bytes]:
    """The blobs' contents, read in one ``cat-file --batch``."""
    if not oids:
        return []
    out = _git(repository, "cat-file", "--batch", data=b"\n".join(oids) + b"\n")
    contents: list[bytes] = []
    at = 0
    for _ in oids:
        end = out.index(b"\n", at)
        size = int(out[at:end].split()[2])
        contents.append(out[end + 1 : end + 1 + size])
        at = end + 2 + size
    return contents


def _regular(mode: int) -> str:
    return "executable" if mode & 0o111 else "file"


def _zipped(archive: zipfile.ZipFile, info: zipfile.ZipInfo) -> Callable[[], bytes]:
    def read() -> bytes:
        with archive.open(info) as stream:
            content = stream.read(MAX_FILE_BYTES + 1)
        if len(content) != info.file_size:
            raise ControlError(INVALID_REQUEST, f"{info.filename}: its size is not as stated")
        return content

    return read


def _tarred(tar: tarfile.TarFile, member: tarfile.TarInfo) -> bytes:
    stream = tar.extractfile(member)
    return stream.read() if stream is not None else b""


def _unwrapped(entries: list[_Entry]) -> list[_Entry]:
    """Without the single top directory every entry is in, if there is one and it is not
    a skill's own directory."""
    paths = [entry.path.removeprefix("./") for entry in entries]
    tops = {path.split("/", 1)[0] for path in paths}
    wrapped = len(tops) == 1 and all("/" in path for path in paths)
    if wrapped and f"{next(iter(tops))}/SKILL.md" not in paths:
        return [replace(e, path=p.split("/", 1)[1]) for e, p in zip(entries, paths, strict=True)]
    return [replace(e, path=p) for e, p in zip(entries, paths, strict=True)]


def _plain(path: str) -> bool:
    parts = path.split("/")
    return (
        len(path.encode(errors="replace")) <= 1024
        and not _CONTROL.search(path)
        and all(part not in ("", ".", "..", ".git") for part in parts)
        and all(len(part.encode(errors="replace")) <= 255 for part in parts)
    )


def _select(
    entries: list[_Entry], root: str | None
) -> tuple[list[_Entry], tuple[Skipped, ...], str]:
    """The entries to keep, under the skills' directory with paths relative to it; those
    left out; and the directory."""
    if root is None:
        root = "skills" if any(_SKILLS.fullmatch(entry.path) for entry in entries) else ""
    root = root.strip("/")
    if root and not _plain(root):
        raise ControlError(INVALID_REQUEST, f"{root!r} is not a plain relative path")
    prefix = f"{root}/" if root else ""
    chosen: list[_Entry] = []
    skipped: list[Skipped] = []
    seen: set[str] = set()
    for entry in sorted(entries, key=lambda e: e.path):
        if not entry.path.startswith(prefix):
            continue
        path = entry.path[len(prefix) :]
        reason = None
        if not _plain(path):
            reason = "not a plain relative path"
        elif path in seen:
            reason = "listed twice"
        elif entry.kind not in ("file", "executable"):
            reason = f"a {entry.kind}, not imported"
        elif entry.size > MAX_FILE_BYTES:
            reason = "larger than 1 MiB"
        if reason is not None:
            skipped.append({"path": path or entry.path, "reason": reason})
            continue
        seen.add(path)
        chosen.append(replace(entry, path=path))
    if len(chosen) > MAX_FILES or sum(entry.size for entry in chosen) > MAX_PACK_BYTES:
        raise ControlError(INVALID_REQUEST, "the pack has over 4,096 files or 32 MiB")
    if not chosen:
        raise ControlError(NOT_FOUND, f"no files in {root or 'the pack'}")
    return chosen, tuple(skipped), root
