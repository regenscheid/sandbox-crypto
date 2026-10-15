# SPDX-License-Identifier: MIT
"""A skill pack's files, read from git or from an archive on the user's machine
(docs/specs/skills.md, SK-1, SK-2, SK-13; docs/SKILLS.md).

A git source is fetched at one commit into a temporary bare repository with the user's
git, without blobs over 1 MiB where the server allows it, no submodules, a cap on the
files written and on the repository's size, only the transports ``ALLOWED`` names, and
none of the caller's ``GIT_*`` location variables. Its tree is listed as a stream, only
under the skills' directory, and its files are read from git's objects, so nothing in
the pack is written out as a file. An archive is read in memory, decompressed once
within a budget. Both stop after their time limit.

Only the skills' directory is kept: ``skills/`` when the pack has skills there, else its
top, or the directory the user names. Entries that are not regular files, paths that
are not plain relative ones and files over ``MAX_FILE_BYTES`` are left out and listed;
paths that differ only by case are reported; a pack that lists a path twice, has over
``MAX_FILES`` files or ``MAX_PACK_BYTES`` bytes, or over four times as many entries, is
refused.
"""

from __future__ import annotations

import bz2
import contextlib
import gzip
import hashlib
import io
import lzma
import os
import re
import resource
import stat
import subprocess
import tarfile
import tempfile
import time
import zipfile
import zlib
from collections.abc import Callable
from dataclasses import dataclass, replace
from functools import partial
from pathlib import Path
from typing import Final
from urllib.parse import urlsplit, urlunsplit

from sandbox_science.orchestrator.control import ControlError
from sandbox_science.orchestrator.control.errors import INVALID_REQUEST, NOT_FOUND, UNAVAILABLE
from sandbox_science.orchestrator.documents.agent_text import hidden_characters

MAX_FILE_BYTES: Final = 1024 * 1024
MAX_PACK_BYTES: Final = 32 * 1024 * 1024
MAX_FILES: Final = 4096
GIT_SECONDS: Final = 300.0
ARCHIVE_SECONDS: Final = 120.0
ALLOWED: Final = "file:git:http:https:ssh"
"""The transports git may use (``GIT_ALLOW_PROTOCOL``)."""
_LOCATION: Final = frozenset(
    {"GIT_DIR", "GIT_WORK_TREE", "GIT_OBJECT_DIRECTORY", "GIT_INDEX_FILE", "GIT_COMMON_DIR"}
    | {"GIT_NAMESPACE", "GIT_ALTERNATE_OBJECT_DIRECTORIES"}
)
_REF: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,199}")
_SHORT_HASH: Final = re.compile(r"[0-9a-f]{4,39}")
_SKILLS: Final = re.compile(r"skills/[^/]+/SKILL\.md")
_CONTROL: Final = re.compile(r"[\x00-\x1f\x7f-\x9f\\]")

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
    """The git URL without credentials or the repository's path, or the archive's name."""
    root: str
    """The directory the skills are in, ``""`` for the pack's top."""
    files: tuple[PackFile, ...]
    skipped: tuple[Skipped, ...]
    ref: str | None = None
    commit: str | None = None
    default_branch: str | None = None
    """The repository's default branch, fetched when no ref was given."""
    archive_sha256: str | None = None
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class _Entry:
    path: str
    kind: str
    """``file``, ``executable``, or what else the entry is (``symbolic link``...)."""
    size: int
    read: Callable[[], bytes]


def shown_origin(origin: str) -> str:
    """``origin`` as an import records it: a URL without its password, and without its
    user name unless it is an SSH login."""
    parts = urlsplit(origin)
    if not parts.scheme or "@" not in parts.netloc:
        return origin
    host = parts.netloc.rpartition("@")[2]
    user = parts.username if parts.scheme == "ssh" and parts.password is None else None
    return urlunsplit(parts._replace(netloc=f"{user}@{host}" if user else host))


def from_git(origin: str, ref: str | None = None, root: str | None = None) -> Fetched:
    """The pack at ``ref`` of the repository ``origin``, a URL or a local path."""
    if not origin or origin.startswith("-") or _CONTROL.search(origin):
        raise ControlError(INVALID_REQUEST, "give a git URL or a repository's path")
    if ref is not None and (not _REF.fullmatch(ref) or ".." in ref):
        raise ControlError(INVALID_REQUEST, f"{ref!r} is not a branch, a tag or a commit hash")
    local = Path(origin).expanduser()
    if local.is_dir():
        origin = str(local.resolve())
    with tempfile.TemporaryDirectory(prefix="sandbox-skills-") as scratch:
        repo = os.path.join(scratch, "pack.git")
        _git(repo, "init", "-q", "--bare")
        _git(repo, "remote", "add", "origin", origin)
        default, target = None, ref or "HEAD"
        if ref is None:
            # The default branch, and then that branch's commit, rather than HEAD again.
            with contextlib.suppress(ControlError):
                said = _git(repo, "ls-remote", "--symref", "origin", "HEAD").decode()
                found = re.match(r"ref: refs/heads/(\S+)\tHEAD", said)
                default = found[1] if found else None
            target = f"refs/heads/{default}" if default else "HEAD"
        fetch = ("fetch", "-q", "--depth", "1", "--no-tags", "--no-recurse-submodules")
        try:
            _git(repo, *fetch, "--filter=blob:limit=1m", "origin", target, limit=_budget())
        except ControlError as error:
            if ref is not None and _SHORT_HASH.fullmatch(ref):
                raise ControlError(
                    NOT_FOUND, "give a commit's full hash, a branch or a tag"
                ) from None
            raise error from None
        held = sum(path.stat().st_size for path in Path(repo).rglob("*") if path.is_file())
        if held > _budget():
            raise ControlError(INVALID_REQUEST, "the fetched commit is larger than 64 MiB")
        commit = _git(repo, "rev-parse", "--verify", "FETCH_HEAD^{commit}").decode().strip()
        if root is None:
            entries = _tree(repo, commit, "skills")
            root = "skills" if any(_SKILLS.fullmatch(e.path) for e in entries) else ""
            entries = entries if root else _tree(repo, commit, None)
        else:
            root = _root(root)
            entries = _tree(repo, commit, root or None)
        entries = _sized(repo, entries)
        chosen, skipped, top, warnings = _select(entries, root)
        blobs = _blobs(repo, [entry.read() for entry in chosen])
    files = tuple(
        PackFile(e.path, content, e.kind == "executable")
        for e, content in zip(chosen, blobs, strict=True)
    )
    shown = shown_origin(origin)
    return Fetched("git", shown, top, files, skipped, ref, commit, default, None, warnings)


def from_archive(path: Path, root: str | None = None) -> Fetched:
    """The pack in a ``.tar`` (possibly gzip, bzip2 or xz compressed) or ``.zip`` file."""
    if path.stat().st_size > MAX_PACK_BYTES:
        raise ControlError(INVALID_REQUEST, "the archive is larger than 32 MiB")
    data = path.read_bytes()
    deadline = time.monotonic() + ARCHIVE_SECONDS
    try:
        if zipfile.is_zipfile(io.BytesIO(data)):
            entries = _zipped(data)
        else:
            entries = _tarred(_unpacked(data, deadline), deadline)
        chosen, skipped, top, warnings = _select(_unwrapped(entries), root)
        files: list[PackFile] = []
        for entry in chosen:
            _in_time(deadline)
            files.append(PackFile(entry.path, entry.read(), entry.kind == "executable"))
    except (tarfile.TarError, zipfile.BadZipFile, zlib.error, lzma.LZMAError, EOFError) as error:
        raise ControlError(INVALID_REQUEST, f"the archive cannot be read: {error}"[:300]) from None
    except (OSError, RuntimeError, NotImplementedError, ValueError) as error:
        raise ControlError(INVALID_REQUEST, f"the archive cannot be read: {error}"[:300]) from None
    digest = hashlib.sha256(data).hexdigest()
    return Fetched(
        "archive", path.name, top, tuple(files), skipped, archive_sha256=digest, warnings=warnings
    )


def _budget() -> int:
    """Bytes a fetch may write, and an archive may unpack to."""
    return 2 * MAX_PACK_BYTES


def _environment() -> dict[str, str]:
    kept = {
        name: value
        for name, value in os.environ.items()
        if name not in _LOCATION and not name.startswith("GIT_ALTERNATE_")
    }
    allowed = {
        "GIT_ALLOW_PROTOCOL": ALLOWED,
        "GIT_NO_LAZY_FETCH": "1",
        "GIT_LITERAL_PATHSPECS": "1",
    }
    return {**kept, "GIT_TERMINAL_PROMPT": "0", **allowed}


def _git(repo: str, *args: str, data: bytes | None = None, limit: int | None = None) -> bytes:
    """Run git on the temporary repository ``repo``; ``limit`` caps each file it writes."""

    def capped() -> None:
        if limit is not None:
            resource.setrlimit(resource.RLIMIT_FSIZE, (limit, limit))

    try:
        done = subprocess.run(  # noqa: S603 - the user's own git, on the user's own request
            ["git", f"--git-dir={repo}", *args],  # noqa: S607 - git on the user's PATH
            cwd=os.path.dirname(repo),
            input=data,
            capture_output=True,
            timeout=GIT_SECONDS,
            env=_environment(),
            check=False,
            preexec_fn=capped if limit is not None else None,
        )
    except FileNotFoundError:
        raise ControlError(UNAVAILABLE, "git is not installed") from None
    except subprocess.TimeoutExpired:
        raise ControlError(UNAVAILABLE, f"git {args[0]} took over {GIT_SECONDS:g} s") from None
    if done.returncode != 0:
        lines = done.stderr.decode(errors="replace").strip().splitlines()
        said = lines[-1] if lines else f"stopped by signal {-done.returncode}"
        raise ControlError(NOT_FOUND, f"git {args[0]}: {said}"[:500])
    return done.stdout


def _tree(repo: str, commit: str, path: str | None) -> list[_Entry]:
    """``ls-tree -r`` under ``path``, read as a stream and stopped past four times
    ``MAX_FILES`` entries; each entry's ``read`` gives its object ID."""
    kinds = {"100644": "file", "100755": "executable", "120000": "symbolic link"}
    command = ["git", f"--git-dir={repo}", "ls-tree", "-r", "-z", "--full-tree", commit]
    command += ["--", path] if path else []
    entries: list[_Entry] = []
    deadline = time.monotonic() + GIT_SECONDS
    with subprocess.Popen(  # noqa: S603 - the user's own git
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        cwd=os.path.dirname(repo),
        env=_environment(),
    ) as listing:
        assert listing.stdout is not None  # noqa: S101 - asked for above
        rest = b""
        while chunk := listing.stdout.read(1 << 16):
            *records, rest = (rest + chunk).split(b"\0")
            for record in records:
                meta, _, name = record.partition(b"\t")
                mode, _, oid = meta.decode().split()
                kind = kinds.get(mode, "submodule" if mode == "160000" else "special file")
                entries.append(_Entry(name.decode(errors="replace"), kind, 0, oid.encode))
            if len(entries) > 4 * MAX_FILES or time.monotonic() > deadline:
                listing.kill()
                raise ControlError(INVALID_REQUEST, "the pack's tree is too large to list")
        if listing.wait(timeout=GIT_SECONDS) != 0:
            raise ControlError(NOT_FOUND, "git ls-tree failed")
    return entries


def _sized(repo: str, entries: list[_Entry]) -> list[_Entry]:
    """The entries with their blobs' sizes; a blob the server left out (over 1 MiB) is
    sized past ``MAX_FILE_BYTES``."""
    files = [entry for entry in entries if entry.kind in ("file", "executable")]
    if not files:
        return entries
    said = _git(repo, "cat-file", "--batch-check", data=b"\n".join(e.read() for e in files) + b"\n")
    sizes: dict[bytes, int] = {}
    for line in said.splitlines():
        oid, _, rest = line.partition(b" ")
        size = rest.rpartition(b" ")[2]
        sizes[oid] = int(size) if size.isdigit() else MAX_FILE_BYTES + 1
    return [replace(e, size=sizes.get(e.read(), e.size)) for e in entries]


def _blobs(repo: str, oids: list[bytes]) -> list[bytes]:
    """The blobs' contents, read in one ``cat-file --batch``."""
    if not oids:
        return []
    out = _git(repo, "cat-file", "--batch", data=b"\n".join(oids) + b"\n")
    contents: list[bytes] = []
    at = 0
    for _ in oids:
        end = out.index(b"\n", at)
        size = int(out[at:end].split()[2])
        contents.append(out[end + 1 : end + 1 + size])
        at = end + 2 + size
    return contents


def _in_time(deadline: float) -> None:
    if time.monotonic() > deadline:
        raise ControlError(UNAVAILABLE, f"reading the archive took over {ARCHIVE_SECONDS:g} s")


def _unpacked(data: bytes, deadline: float) -> bytes:
    """A tar's bytes, decompressed once, within ``_budget()``."""
    opened: dict[bytes, Callable[[io.BytesIO], io.BufferedIOBase]] = {
        b"\x1f\x8b": lambda raw: gzip.GzipFile(fileobj=raw, mode="rb"),
        b"BZ": bz2.BZ2File,
        b"\xfd7": lzma.LZMAFile,
    }
    open_ = opened.get(data[:2])
    if open_ is None:
        return data
    stream, out = open_(io.BytesIO(data)), bytearray()
    while chunk := stream.read(1 << 20):
        out += chunk
        if len(out) > _budget():
            raise ControlError(INVALID_REQUEST, "the archive unpacks to more than 64 MiB")
        _in_time(deadline)
    return bytes(out)


def _tarred(data: bytes, deadline: float) -> list[_Entry]:
    tar = tarfile.open(fileobj=io.BytesIO(data), mode="r:")  # noqa: SIM115 - in memory
    entries: list[_Entry] = []
    for count, member in enumerate(tar, 1):
        if count > 4 * MAX_FILES:
            raise ControlError(INVALID_REQUEST, "the archive has too many entries")
        _in_time(deadline)
        if member.isdir():
            continue
        kind = (
            _regular(member.mode)
            if member.isreg()
            else "symbolic link"
            if member.issym()
            else "hard link"
            if member.islnk()
            else "special file"
        )
        entries.append(_Entry(member.name, kind, member.size, partial(_member, tar, member)))
    return entries


def _member(tar: tarfile.TarFile, member: tarfile.TarInfo) -> bytes:
    stream = tar.extractfile(member)
    return stream.read() if stream is not None else b""


def _zipped(data: bytes) -> list[_Entry]:
    at = data.rfind(b"PK\x05\x06")
    count = int.from_bytes(data[at + 10 : at + 12], "little") if at >= 0 else 0
    if count >= min(0xFFFF, 4 * MAX_FILES):
        raise ControlError(INVALID_REQUEST, "the archive has too many entries")
    archive = zipfile.ZipFile(io.BytesIO(data))
    entries: list[_Entry] = []
    for info in archive.infolist():
        mode = info.external_attr >> 16
        kind = "symbolic link" if stat.S_ISLNK(mode) else _regular(mode)
        if not info.is_dir():
            entries.append(
                _Entry(info.filename, kind, info.file_size, partial(_inflated, archive, info))
            )
    return entries


def _inflated(archive: zipfile.ZipFile, info: zipfile.ZipInfo) -> bytes:
    with archive.open(info) as stream:
        content = stream.read(MAX_FILE_BYTES + 1)
    if len(content) != info.file_size:
        raise ControlError(INVALID_REQUEST, f"{info.filename[:200]}: its size is not as stated")
    return content


def _regular(mode: int) -> str:
    return "executable" if mode & 0o111 else "file"


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
    """A relative path of plain names: no control, format, bidirectional or zero-width
    character, no backslash, no empty, dot, dot-dot or ``.git`` name, and bounded."""
    parts = path.split("/")
    return (
        len(path.encode(errors="replace")) <= 1024
        and not _CONTROL.search(path)
        and not hidden_characters(path)
        and all(part not in ("", ".", "..", ".git") for part in parts)
        and all(len(part.encode(errors="replace")) <= 255 for part in parts)
    )


def _root(root: str) -> str:
    """The skills' directory the user named, refused unless it is a plain path."""
    root = root.strip("/")
    if root and (not _plain(root) or root.startswith(":")):
        raise ControlError(INVALID_REQUEST, f"{root[:200]!r} is not a plain relative path")
    return root


def _select(
    entries: list[_Entry], root: str | None
) -> tuple[list[_Entry], tuple[Skipped, ...], str, tuple[str, ...]]:
    """The entries to keep, under the skills' directory with paths relative to it; those
    left out; the directory; and warnings about paths that differ only by case."""
    if root is None:
        root = "skills" if any(_SKILLS.fullmatch(entry.path) for entry in entries) else ""
    root = _root(root)
    prefix = f"{root}/" if root else ""
    chosen: list[_Entry] = []
    skipped: list[Skipped] = []
    seen: set[str] = set()
    folded: dict[str, str] = {}
    warnings: list[str] = []
    for entry in sorted(entries, key=lambda e: e.path):
        if not entry.path.startswith(prefix):
            continue
        path = entry.path[len(prefix) :]
        if path in seen:
            raise ControlError(INVALID_REQUEST, f"the pack lists {path[:200]!r} twice")
        reason = None
        if not _plain(path):
            reason = "not a plain relative path"
        elif entry.kind not in ("file", "executable"):
            reason = f"a {entry.kind}, not imported"
        elif entry.size > MAX_FILE_BYTES:
            reason = "larger than 1 MiB"
        if reason is not None:
            skipped.append({"path": path or entry.path, "reason": reason})
            continue
        if (other := folded.get(path.casefold())) is not None:
            warnings.append(f"{other[:200]} and {path[:200]} differ only by case")
        seen.add(path)
        folded[path.casefold()] = path
        chosen.append(replace(entry, path=path))
    if len(chosen) > MAX_FILES or sum(entry.size for entry in chosen) > MAX_PACK_BYTES:
        raise ControlError(INVALID_REQUEST, "the pack has over 4,096 files or 32 MiB")
    if not chosen:
        raise ControlError(NOT_FOUND, f"no files in {root or 'the pack'}")
    return chosen, tuple(skipped), root, tuple(warnings)
