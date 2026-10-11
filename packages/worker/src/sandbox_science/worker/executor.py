# SPDX-License-Identifier: MIT
"""The executor: runs shell commands, Jupyter kernels and file operations in a sandbox.

The trusted worker service starts it inside a research sandbox and speaks
newline-delimited JSON with it over stdin and stdout. It depends only on the
standard library and, once a kernel starts, jupyter_client, so the worker
service can copy this one file into any sandbox that has Python.
docs/specs/tools.md defines the behavior and docs/WORKER.md the protocol.

Requests carry an ``id`` and an ``op``; each gets one reply
(``{"type": "reply", "id": …, "ok": …}``). Shell commands and cells reply as
soon as they are accepted, then send events
(``{"type": "event", "execution": …, "event": …}``) ending with ``end``.
Every line it sends is shorter than MAX_LINE: output goes in chunks, and a
result too large for one line keeps less of its output. When stdin closes,
the executor stops every command and kernel, and what they left running.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import codecs
import contextlib
import errno
import hashlib
import importlib.util
import json
import os
import platform
import re
import secrets
import signal
import site
import socket
import stat
import sys
import sysconfig
import tempfile
import time
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final, cast

PROTOCOL = 1
MAX_LINE = 16 * 1024 * 1024
"""The longest line the executor sends; the worker drops longer ones."""
MAX_REQUEST = 64 * 1024 * 1024
"""The longest request line it reads (a file write carries the file)."""
MAX_READ = 8 * 1024 * 1024
MAX_LIST = 5000
MAX_SNAPSHOT = 20000
"""Files scanned before and after an attempt to find the ones it wrote."""
MAX_DEPTH = 128
"""Directory levels scanned below the session workspace and the shared area."""
MAX_WRITTEN = 1000
MAX_PATH_BYTES = 4096
MAX_OUTPUTS = 10_000
"""Rich outputs a cell returns; one note stands for any beyond them or the limit."""
STREAM_CHUNK = 64 * 1024
"""Characters of output per stream event."""
MAX_ERROR_TEXT = 64 * 1024
MAX_TRACEBACK_LINES = 100
MAX_TRACEBACK_LINE = 8 * 1024
TERM_GRACE = 2.0
INTERRUPT_GRACE = 5.0
KERNEL_START_TIMEOUT = 120.0
KILL_ROUNDS = 5
KERNEL_NAMES = {"python": "python3", "sage": "sagemath"}
PDF_HELPER = Path(__file__).with_name("pdfhelper.py")
"""The PDF helper the worker copies beside this file (docs/specs/pdf-sources.md)."""
"""Each language's Jupyter kernelspec; SageMath installs ``sagemath`` beside its Python."""
PROBE_TEXT = 256
"""The longest value the isolation probe reports."""
MAX_SENTINELS = 8
SHARED = "shared"
CONFIG_RECORD = "configured.json"
"""In the executor's own directory: what ``configure`` wrote, by path and SHA-256."""
OWNER = "SANDBOX_SCIENCE_OWNER"
"""Names the call or kernel that started a process, in its environment. Processes
keep it unless they replace their whole environment."""
PR_SET_DUMPABLE = 4
PR_SET_CHILD_SUBREAPER = 36
_DIRECTORY = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
_SESSION_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
_ENV_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_spawned: set[int] = set()
"""Processes asyncio or jupyter_client started, and will reap: never reaped here."""

Message = dict[str, Any]
Send = Callable[[Message], Awaitable[None]]
Leader = tuple[int, int]
"""A session leader's process ID and start time."""


class ToolError(Exception):
    """A failure reported to the worker service with one of the spec's error kinds."""

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind
        self.message = message


class _NotStartedError(Exception):
    """A cell cancelled before it reached its kernel."""


# Output capture.


class Capture:
    """One output stream: live text up to half the limit, then only a rolling tail."""

    def __init__(self, name: str, limit: int) -> None:
        self.name = name
        self.head_limit = limit // 2
        self.tail_limit = limit - self.head_limit
        self.head = bytearray()
        self.tail = bytearray()
        self.total = 0
        self._decoder = codecs.getincrementaldecoder("utf-8")("replace")

    def feed(self, data: bytes) -> str:
        """Record ``data`` and return the text to stream now (empty once the head is full)."""
        self.total += len(data)
        room = self.head_limit - len(self.head)
        live = b""
        if room > 0:
            live = data[:room]
            self.head += live
            data = data[room:]
        if data:
            self.tail += data
            if len(self.tail) > self.tail_limit:
                del self.tail[: len(self.tail) - self.tail_limit]
        return self._decoder.decode(live) if live else ""

    def summary(self) -> Message:
        dropped = self.total - len(self.head) - len(self.tail)
        head = bytes(self.head).decode("utf-8", "replace")
        tail = bytes(self.tail).decode("utf-8", "replace")
        if dropped > 0:
            text = f"{head}\n[… {dropped} bytes omitted …]\n{tail}"
        else:
            text = (bytes(self.head) + bytes(self.tail)).decode("utf-8", "replace")
        return {
            "name": self.name,
            "text": text,
            "total_bytes": self.total,
            "truncated": dropped > 0,
        }


def bounded(text: str, limit: int) -> str:
    """``text``, keeping its head and tail if it is longer than ``limit`` characters."""
    if len(text) <= limit:
        return text
    half = limit // 2
    return f"{text[:half]}\n[… {len(text) - 2 * half} characters omitted …]\n{text[-half:]}"


def printable_path(name: str) -> str:
    """A file name as text: bytes that are not UTF-8 become U+FFFD."""
    return name.encode("utf-8", "surrogateescape").decode("utf-8", "replace")


# Paths. File operations open one directory at a time, relative to the last, and
# never follow a symlink, so a symlink swapped in on the way cannot redirect them.


def check_relative(path: str) -> str:
    """The same rules as the contracts' validate_relative_path."""
    if path == ".":
        return path
    if not path or "\x00" in path or len(path.encode()) > 4096 or path.startswith("/"):
        raise ToolError("invalid_request", f"invalid path {path!r}")
    if any(segment in ("", ".", "..") for segment in path.split("/")):
        raise ToolError("invalid_request", f"path {path!r} has an empty, '.' or '..' segment")
    return path


def _refused(parent: int, name: str, path: str, exc: OSError) -> ToolError:
    """Why ``name`` in the directory ``parent`` cannot be used on the way to ``path``."""
    with contextlib.suppress(OSError):
        if stat.S_ISLNK(os.lstat(name, dir_fd=parent).st_mode):
            return ToolError("invalid_request", f"path {path!r} goes through a symlink")
    if exc.errno == errno.ENOTDIR:
        return ToolError("invalid_request", f"path {path!r}: {name!r} is not a directory")
    return ToolError("invalid_request", f"cannot use {path!r}: {exc.strerror}")


def _open_directory(parent: int, name: str, path: str, *, create: bool) -> int:
    try:
        return os.open(name, _DIRECTORY, dir_fd=parent)
    except FileNotFoundError:
        if not create:
            raise ToolError("not_found", f"{path!r} does not exist") from None
    except OSError as exc:
        raise _refused(parent, name, path, exc) from exc
    with contextlib.suppress(FileExistsError):
        os.mkdir(name, dir_fd=parent)
    try:
        return os.open(name, _DIRECTORY, dir_fd=parent)
    except OSError as exc:
        raise _refused(parent, name, path, exc) from exc


def _walk(directory: int, names: Sequence[str], path: str, *, create: bool) -> int:
    """Open ``names`` one directory at a time below ``directory``, which is consumed."""
    try:
        for name in names:
            child = _open_directory(directory, name, path, create=create)
            os.close(directory)
            directory = child
    except BaseException:
        os.close(directory)
        raise
    return directory


def scan_files(directory: int, prefix: str, files: dict[str, tuple[int, int, int]]) -> bool:
    """Add the regular files below ``directory`` (which is consumed) to ``files``.

    Depth first, holding open only the directories on the current path. Returns
    True when MAX_SNAPSHOT files stopped the scan.
    """
    path = [(directory, prefix, _subdirectories(directory, prefix, files))]
    try:
        while path:
            if len(files) >= MAX_SNAPSHOT:
                return True
            current, relative, pending = path[-1]
            if not pending or len(path) > MAX_DEPTH:
                os.close(current)
                path.pop()
                continue
            name = pending.pop()
            try:
                child = os.open(name, _DIRECTORY, dir_fd=current)
            except OSError:
                continue
            below = f"{relative}{name}/"
            path.append((child, below, _subdirectories(child, below, files)))
        return False
    finally:
        for current, _, _ in path:
            os.close(current)


def _subdirectories(
    directory: int, relative: str, files: dict[str, tuple[int, int, int]]
) -> list[str]:
    """Record a directory's regular files in ``files``; return its subdirectories' names."""
    names: list[str] = []
    try:
        with os.scandir(directory) as listing:
            for entry in listing:
                if len(files) >= MAX_SNAPSHOT:
                    break
                try:
                    if entry.is_dir(follow_symlinks=False):
                        names.append(entry.name)
                        continue
                    if not entry.is_file(follow_symlinks=False):
                        continue
                    info = entry.stat(follow_symlinks=False)
                except OSError:
                    continue
                files[relative + entry.name] = (info.st_mtime_ns, info.st_size, info.st_ino)
    except OSError:
        pass
    return names


SHARED_REPLY_BYTES = 4 * 1024 * 1024
"""The most path bytes a scan of the shared area reports; the rest is a truncated scan.

A reply names a path at most twice, in ``files`` and in ``sha256`` or ``too_large``, so
it stays under MAX_LINE."""
SYNC_MARK = ".sandbox-science-sync"
"""Beside sessions/ and shared/: the worker's mark of this environment's sync (D79)."""


def scan_shared(directory: int) -> tuple[dict[str, list[int]], bool, bool, list[str]]:
    """The regular files below ``shared/`` (``directory``, which is consumed), for its sync.

    Maps each path to its modification time, size, inode and executable bit. Returns
    the files, whether the scan stopped early, whether anything could not be read (so
    that missing files prove nothing), and names that are not text.
    """
    files: dict[str, list[int]] = {}
    odd: list[str] = []
    partial = False
    truncated = False
    reported = 0
    stack: list[tuple[int, str, int]] = [(directory, SHARED + "/", 0)]
    try:
        while stack:
            current, relative, depth = stack.pop()
            try:
                if depth > MAX_DEPTH:
                    partial = True
                    continue
                try:
                    listing = list(os.scandir(current))
                except OSError:
                    partial = True
                    continue
                for entry in listing:
                    name = entry.name
                    if not name.isprintable():
                        # Never a synced path, so its absence proves nothing: no partial scan.
                        odd.append(relative + name.encode("utf-8", "replace").decode())
                        continue
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            child = os.open(name, _DIRECTORY, dir_fd=current)
                            stack.append((child, f"{relative}{name}/", depth + 1))
                            continue
                        if not entry.is_file(follow_symlinks=False):
                            continue
                        if name.startswith(".write-"):
                            continue  # a write of this executor's under way
                        info = entry.stat(follow_symlinks=False)
                    except OSError:
                        partial = True
                        continue
                    path = relative + name
                    reported += len(json.dumps(path, ensure_ascii=False).encode())
                    if len(files) >= MAX_SNAPSHOT or reported > SHARED_REPLY_BYTES:
                        truncated = True
                        return files, truncated, partial, odd[:10]
                    executable = 1 if info.st_mode & 0o111 else 0
                    files[path] = [info.st_mtime_ns, info.st_size, info.st_ino, executable]
            finally:
                os.close(current)
    finally:
        for current, _, _ in stack:
            os.close(current)
    return files, truncated, partial, odd[:10]


def read_range(descriptor: int, offset: int, length: int, hashed: bool, path: str) -> Message:
    """A range of an open file and, when ``hashed``, the whole file's SHA-256.

    One pass over the file gives both. The descriptor is consumed.
    """
    with os.fdopen(descriptor, "rb") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise ToolError("invalid_request", f"{path!r} is not a regular file")
        content = bytearray()
        reply: Message = {"modified": info.st_mtime, "offset": offset}
        if hashed:
            digest = hashlib.sha256()
            position = 0
            while block := handle.read(1024 * 1024):
                digest.update(block)
                end = position + len(block)
                if end > offset and position < offset + length:
                    content += block[max(0, offset - position) : offset + length - position]
                position = end
            reply.update(sha256=digest.hexdigest(), size=position)
        else:
            handle.seek(offset)
            content += handle.read(length)
            reply["size"] = info.st_size
    reply.update(
        content_b64=base64.b64encode(content).decode(),
        eof=offset + len(content) >= reply["size"],
    )
    return reply


def write_file(
    parent: int,
    name: str,
    content: bytes,
    expected: object,
    create_only: bool,
    path: str,
    executable: bool | None = None,
) -> Message:
    """Create or atomically replace ``name`` in the directory ``parent``, which is consumed.

    The reply's ``stamp`` is the new file's modification time, size and inode as written,
    taken before the file takes its name, so a later change shows as a different stamp.
    """
    try:
        try:
            info = os.stat(name, dir_fd=parent, follow_symlinks=False)
        except FileNotFoundError:
            info = None
        if info is not None and stat.S_ISLNK(info.st_mode):
            raise ToolError("invalid_request", f"path {path!r} goes through a symlink")
        if info is not None and stat.S_ISDIR(info.st_mode):
            raise ToolError("invalid_request", f"{path!r} is a directory")
        if create_only and info is not None:
            raise ToolError("conflict", f"{path!r} already exists")
        checked: os.stat_result | None = None
        if expected is not None:
            if info is None:
                raise ToolError("conflict", f"{path!r} does not exist")
            current, checked = _hashed(parent, name, path)
            if current != expected:
                raise ToolError("conflict", f"{path!r} changed: its SHA-256 is {current}")
        temporary = f".write-{secrets.token_hex(8)}"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
        descriptor = os.open(temporary, flags, 0o600, dir_fd=parent)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(content)
                if executable is not None:
                    os.fchmod(handle.fileno(), 0o755 if executable else 0o644)
                handle.flush()
                written = os.fstat(handle.fileno())
            if checked is not None and _exchange(parent, temporary, name):
                # Swapped at once: the file that was there is at the temporary name now,
                # and if it changed since it was checked, it goes back.
                if _changed_since(parent, temporary, checked):
                    _exchange(parent, temporary, name)
                    raise ToolError("conflict", f"{path!r} changed while it was written")
                os.unlink(temporary, dir_fd=parent)
            else:
                if checked is not None and _changed_since(parent, name, checked):
                    raise ToolError("conflict", f"{path!r} changed while it was written")
                os.replace(temporary, name, src_dir_fd=parent, dst_dir_fd=parent)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(temporary, dir_fd=parent)
            raise
    finally:
        os.close(parent)
    return {
        "sha256": hashlib.sha256(content).hexdigest(),
        "size": len(content),
        "stamp": [written.st_mtime_ns, written.st_size, written.st_ino],
    }


_RENAME_EXCHANGE: Final = 2


def _exchange(parent: int, first: str, second: str) -> bool:
    """Swap two names in ``parent`` at once (renameat2); False where that is unavailable."""
    try:
        import ctypes

        libc = ctypes.CDLL(None, use_errno=True)
        renameat2 = libc.renameat2
    except (ImportError, OSError, AttributeError):
        return False
    renameat2.argtypes = [
        ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint,
    ]  # fmt: skip
    if renameat2(parent, os.fsencode(first), parent, os.fsencode(second), _RENAME_EXCHANGE) == 0:
        return True
    code = ctypes.get_errno()
    if code in (errno.EINVAL, errno.ENOSYS, errno.EPERM, errno.EOPNOTSUPP):
        return False  # not on this file system, or refused by the sandbox
    raise OSError(code, os.strerror(code), second)


def _changed_since(parent: int, name: str, before: os.stat_result) -> bool:
    """Whether ``name`` is no longer the file ``before`` described."""
    try:
        now = os.stat(name, dir_fd=parent, follow_symlinks=False)
    except FileNotFoundError:
        return True
    return (now.st_ino, now.st_mtime_ns, now.st_size) != (
        before.st_ino,
        before.st_mtime_ns,
        before.st_size,
    )


def _move_file(source: int, name: str, destination: int, target: str, path: str) -> list[int]:
    """Give the regular file ``name`` the free name ``target``; return its new stamp."""
    info = os.stat(name, dir_fd=source, follow_symlinks=False)
    if not stat.S_ISREG(info.st_mode):
        raise ToolError("invalid_request", f"{path!r} is not a regular file")
    try:
        # A hard link never replaces an existing file, and the inode stays the same.
        os.link(name, target, src_dir_fd=source, dst_dir_fd=destination, follow_symlinks=False)
        os.unlink(name, dir_fd=source)
    except FileExistsError:
        raise ToolError("conflict", f"the new name for {path!r} is taken") from None
    except OSError as exc:
        if exc.errno not in (errno.EPERM, errno.EXDEV, errno.EMLINK, errno.EOPNOTSUPP):
            raise
        # No hard links here: a rename, after checking the name is free.
        try:
            os.stat(target, dir_fd=destination, follow_symlinks=False)
        except FileNotFoundError:
            os.rename(name, target, src_dir_fd=source, dst_dir_fd=destination)
        else:
            raise ToolError("conflict", f"the new name for {path!r} is taken") from None
    moved = os.stat(target, dir_fd=destination, follow_symlinks=False)
    return [moved.st_mtime_ns, moved.st_size, moved.st_ino]


def _file_error(exc: OSError, path: str) -> ToolError:
    """A failure on one file, by kind; a full or failing disk stops the sync instead."""
    if exc.errno in (errno.ENOSPC, errno.EDQUOT, errno.EIO, errno.EROFS):
        return ToolError("unavailable", f"cannot change {path!r}: {exc.strerror}")
    if exc.errno in (errno.EACCES, errno.EPERM):
        return ToolError("policy_denied", f"cannot change {path!r}: {exc.strerror}")
    return ToolError("invalid_request", f"cannot change {path!r}: {exc.strerror}")


def _read_mark(root: Path) -> str | None:
    """The sync's mark, read without following a link or waiting on a pipe."""
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
    try:
        descriptor = os.open(root / SYNC_MARK, flags)
    except OSError:
        return None
    with os.fdopen(descriptor, "rb") as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            return None
        try:
            return handle.read(257).decode()[:256]
        except UnicodeDecodeError:
            return None


def _write_mark(root: Path, mark: str) -> None:
    temporary = root / f".write-{secrets.token_hex(8)}"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
    with os.fdopen(os.open(temporary, flags, 0o600), "w") as handle:
        handle.write(mark)
    os.replace(temporary, root / SYNC_MARK)


def open_shared_files(shared: Path, limit: int = 10_000) -> list[str]:
    """The files in the shared area that some other process has open, as ``shared/`` paths.

    Read from ``/proc``. Processes it may not inspect are left out, so a file missing
    from the list can still be open.
    """
    prefix = os.path.realpath(shared) + os.sep
    found: set[str] = set()
    own = str(os.getpid())
    try:
        processes = [entry for entry in os.listdir("/proc") if entry.isdigit() and entry != own]
    except OSError:
        return []
    for process in processes:
        try:
            descriptors = os.listdir(f"/proc/{process}/fd")
        except OSError:
            continue
        for descriptor in descriptors:
            try:
                target = os.readlink(f"/proc/{process}/fd/{descriptor}")
            except OSError:
                continue
            if target.startswith(prefix):
                found.add(SHARED + "/" + target[len(prefix) :])
                if len(found) >= limit:
                    return sorted(found)
    return sorted(found)


def _remove_checked(parent: int, name: str, expected: str, path: str) -> None:
    """Unlink ``name`` if it is the regular file whose content hashes to ``expected``."""
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
    try:
        descriptor = os.open(name, flags, dir_fd=parent)
    except FileNotFoundError:
        raise ToolError("not_found", f"no file {path!r}") from None
    with os.fdopen(descriptor, "rb") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise ToolError("invalid_request", f"{path!r} is not a regular file")
        digest = hashlib.sha256()
        while block := handle.read(1024 * 1024):
            digest.update(block)
        if digest.hexdigest() != expected:
            raise ToolError("conflict", f"{path!r} changed: its SHA-256 is {digest.hexdigest()}")
        # The name must still be this file, not one put in its place meanwhile.
        if os.stat(name, dir_fd=parent, follow_symlinks=False).st_ino != info.st_ino:
            raise ToolError("conflict", f"{path!r} changed while it was checked")
        os.unlink(name, dir_fd=parent)


def _stamped_sha256(parent: int, name: str, stamp: Sequence[int]) -> str | None:
    """The SHA-256 of ``name`` if it is still the file scanned as ``stamp``, else None.

    A digest then always belongs to the stamp reported with it, so the worker never
    takes a later version for the one it scanned.
    """
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
    with os.fdopen(os.open(name, flags, dir_fd=parent), "rb") as handle:
        before = os.fstat(handle.fileno())
        scanned = (before.st_mtime_ns, before.st_size, before.st_ino)
        if not stat.S_ISREG(before.st_mode) or scanned != tuple(stamp[:3]):
            return None
        digest = hashlib.sha256()
        while block := handle.read(1024 * 1024):
            digest.update(block)
        after = os.fstat(handle.fileno())
    if (after.st_mtime_ns, after.st_size) != scanned[:2]:
        return None  # changed while it was read
    return digest.hexdigest()


def _hashed(parent: int, name: str, path: str) -> tuple[str, os.stat_result]:
    """A file's SHA-256, and its status as it was before it was read."""
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
    with os.fdopen(os.open(name, flags, dir_fd=parent), "rb") as handle:
        before = os.fstat(handle.fileno())
        if not stat.S_ISREG(before.st_mode):
            raise ToolError("invalid_request", f"{path!r} is not a regular file")
        digest = hashlib.sha256()
        while block := handle.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest(), before


def list_directory(directory: int, limit: int) -> tuple[list[Message], bool]:
    """Up to ``limit`` entries of a directory, by name; symlinks are not followed.

    The descriptor is consumed.
    """
    try:
        with os.scandir(directory) as listing:
            names = sorted(entry.name for entry in listing)
        entries: list[Message] = []
        for name in names[:limit]:
            try:
                info = os.stat(name, dir_fd=directory, follow_symlinks=False)
            except OSError:
                continue
            if stat.S_ISLNK(info.st_mode):
                kind = "symlink"
            elif stat.S_ISDIR(info.st_mode):
                kind = "directory"
            elif stat.S_ISREG(info.st_mode):
                kind = "file"
            else:
                kind = "other"
            entries.append(
                {
                    "name": printable_path(name),
                    "type": kind,
                    "size": info.st_size,
                    "modified": info.st_mtime,
                }
            )
        return entries, len(names) > limit
    finally:
        os.close(directory)


# Processes. Every command and kernel runs with its own OWNER mark, and the
# executor adopts orphaned processes where the kernel lets it (a subreaper), so
# what a call started is found when it ends, even outside its process group or
# session. Signals go to one process at a time: OpenShell's seccomp filter
# refuses signals to process groups (lab 4).


@dataclass(frozen=True)
class Process:
    """A process as /proc showed it. Its pid and start time together name it."""

    pid: int
    ppid: int
    session: int
    start: int
    zombie: bool


def process_info(pid: int) -> Process | None:
    try:
        with open(f"/proc/{pid}/stat", "rb") as handle:
            data = handle.read()
    except OSError:
        return None
    # The command name is in parentheses and may contain anything: parse after the
    # last ')'. Then come state, ppid, pgrp and session; start time is the 20th.
    fields = data[data.rfind(b")") + 2 :].split()
    try:
        return Process(pid, int(fields[1]), int(fields[3]), int(fields[19]), fields[0] == b"Z")
    except (IndexError, ValueError):
        return None


def process_table() -> dict[int, Process]:
    table: dict[int, Process] = {}
    with contextlib.suppress(OSError):
        for entry in os.listdir("/proc"):
            if entry.isdigit() and (info := process_info(int(entry))) is not None:
                table[info.pid] = info
    return table


def environment_fingerprint() -> str:
    """A digest of the packages installed for this interpreter (W3).

    Conda records each package of the prefix in ``conda-meta``, and pip each
    distribution in a ``.dist-info`` or ``.egg-info`` directory of the prefix's or the
    user's site-packages. Installing, removing or upgrading one changes the digest.
    The files the packages own do not count, which keeps the scan cheap.
    """
    paths = sysconfig.get_paths()
    places = [os.path.join(sys.prefix, "conda-meta"), paths["purelib"], paths["platlib"]]
    with contextlib.suppress(Exception):
        places.append(site.getusersitepackages())
    digest = hashlib.sha256()
    for place in dict.fromkeys(places):
        try:
            with os.scandir(place) as scan:
                entries = sorted((entry.name, entry) for entry in scan)
        except OSError:
            continue
        for name, entry in entries:
            if not (name.endswith((".json", ".dist-info", ".egg-info")) or name == "history"):
                continue
            try:
                info = entry.stat(follow_symlinks=False)
            except OSError:
                continue
            line = f"{place}\0{name}\0{info.st_size}\0{info.st_mtime_ns}\n"
            digest.update(line.encode("utf-8", "surrogateescape"))
    return digest.hexdigest()[:32]


def cpu_seconds_below(root: int) -> float:
    """CPU time of every process below ``root``, and of the children ``root`` reaped.

    A process's ``cutime`` and ``cstime`` hold what its reaped children used, so the
    sum stays put when a process ends, wherever below ``root`` it is reaped.
    """
    ticks: dict[int, tuple[int, int, int]] = {}  # pid -> (ppid, own, reaped children)
    with contextlib.suppress(OSError):
        for entry in os.listdir("/proc"):
            if not entry.isdigit():
                continue
            try:
                with open(f"/proc/{entry}/stat", "rb") as handle:
                    data = handle.read()
                fields = data[data.rfind(b")") + 2 :].split()
                ppid, own = int(fields[1]), int(fields[11]) + int(fields[12])
                ticks[int(entry)] = (ppid, own, int(fields[13]) + int(fields[14]))
            except (OSError, IndexError, ValueError):
                continue
    children: dict[int, list[int]] = {}
    for pid, (ppid, _, _) in ticks.items():
        children.setdefault(ppid, []).append(pid)
    total = ticks[root][2] if root in ticks else 0
    stack = list(children.get(root, []))
    while stack:
        pid = stack.pop()
        _, own, reaped = ticks[pid]
        total += own + reaped
        stack.extend(children.get(pid, []))
    return total / os.sysconf("SC_CLK_TCK")


def process_owner(pid: int) -> str | None:
    """The OWNER a process started with: ``""`` without one, None when unreadable.

    Unreadable are zombies, other users' processes, and children of this executor
    between fork and exec, since the executor is not dumpable.
    """
    try:
        with open(f"/proc/{pid}/environ", "rb") as handle:
            environ = handle.read()
    except OSError:
        return None
    if not environ:
        return None
    prefix = OWNER.encode() + b"="
    for item in environ.split(b"\0"):
        if item.startswith(prefix):
            return item[len(prefix) :].decode("utf-8", "replace")
    return ""


def owned_processes(owner: str, leader: Leader | None) -> list[Process]:
    """The live processes that belong to ``owner``, a call or a kernel.

    A process belongs to it when it is in the session ``leader`` began, carries
    the owner's mark, or descends from such a process without a mark of its
    own. Below this executor, a process with no mark at all has replaced its
    environment to get away, and belongs to whichever owner ends first. A
    process whose environment cannot be read is left alone.
    """
    table = process_table()
    me = os.getpid()
    session = leader[0] if leader is not None else None
    if (
        leader is not None
        and (head := table.get(leader[0])) is not None
        and head.start != leader[1]
    ):
        session = None  # the leader's pid was reused: the session number means nothing now
    children: dict[int, list[int]] = {}
    for info in table.values():
        children.setdefault(info.ppid, []).append(info.pid)
    found: list[Process] = []
    seen = {me}
    stack: list[tuple[int, str]] = [(pid, "") for pid in children.get(me, ())]
    while stack:
        pid, inherited = stack.pop()
        info = table.get(pid)
        if info is None or pid in seen:
            continue
        seen.add(pid)
        mark = process_owner(pid)
        if mark is None and not info.zombie:
            continue
        whose = owner if session is not None and info.session == session else mark or inherited
        if whose in (owner, "") and not info.zombie:
            found.append(info)
        stack.extend((child, whose) for child in children.get(pid, ()))
    # Without a subreaper, init adopts orphans: find them by session and mark.
    for info in table.values():
        if info.pid in seen or info.zombie:
            continue
        if (session is not None and info.session == session) or process_owner(info.pid) == owner:
            found.append(info)
    return found


def signal_processes(processes: Iterable[Process], signum: int) -> list[Process]:
    """Signal each process that is still the one scanned; return those signalled."""
    sent: list[Process] = []
    for info in processes:
        current = process_info(info.pid)
        if current is None or current.start != info.start:
            continue  # gone, or its pid reused since the scan
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.kill(info.pid, signum)
            sent.append(info)
    return sent


def end_owned(owner: str, leader: Leader | None) -> int:
    """SIGKILL everything ``owner`` left running; return how many processes that was.

    A few rounds catch processes forked meanwhile. Processes this executor
    adopted are reaped, so they do not stay as zombies.
    """
    killed: dict[int, Process] = {}
    for _ in range(KILL_ROUNDS):
        targets = owned_processes(owner, leader)
        if not targets:
            break
        for info in signal_processes(targets, signal.SIGKILL):
            killed[info.pid] = info
        time.sleep(0.01)
    _reap(killed)
    return len(killed)


def end_descendants() -> None:
    """SIGKILL every process below this executor, as it shuts down."""
    for _ in range(KILL_ROUNDS):
        table = process_table()
        children: dict[int, list[int]] = {}
        for info in table.values():
            children.setdefault(info.ppid, []).append(info.pid)
        targets: list[Process] = []
        stack = list(children.get(os.getpid(), ()))
        while stack:
            pid = stack.pop()
            if not table[pid].zombie:
                targets.append(table[pid])
            stack.extend(children.get(pid, ()))
        if not signal_processes(targets, signal.SIGKILL):
            break
        time.sleep(0.01)


def _reap(pids: Iterable[int]) -> None:
    """Collect the status of killed processes this executor adopted, so none stays a zombie."""
    waiting = [pid for pid in pids if pid not in _spawned]
    for _ in range(20):
        if not waiting:
            return
        still: list[int] = []
        for pid in waiting:
            try:
                done, _ = os.waitpid(pid, os.WNOHANG)
            except ChildProcessError:  # not adopted (yet): another parent reaps it
                continue
            if not done:
                still.append(pid)
        waiting = still
        time.sleep(0.01)


def _sockets_work() -> bool:
    """Whether this process may bind a loopback socket, as a kernel's client must."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.bind(("127.0.0.1", 0))
    except OSError:
        return False
    return True


def protect_process() -> bool:
    """Make this process not dumpable, unless that takes its sockets away; say whether it is.

    Research code runs as this same user. Not dumpable, this process keeps its /proc
    files (fd, environ, mem) and ptrace closed to it, so research code cannot write into
    the protocol or read other sessions' requests. OpenShell's supervisor refuses socket
    calls from a process it cannot inspect, which stops every kernel starting (lab 8);
    there the executor stays dumpable.
    """
    sockets = _sockets_work()
    if not _prctl(PR_SET_DUMPABLE, 0):
        return False
    if sockets and not _sockets_work():
        _prctl(PR_SET_DUMPABLE, 1)
        return False
    return True


def _prctl(option: int, value: int) -> bool:
    """One prctl(2) call through libc; False where it is unavailable or refused."""
    try:
        import ctypes

        libc = ctypes.CDLL(None, use_errno=True)
        arguments = (ctypes.c_ulong(value), ctypes.c_ulong(0), ctypes.c_ulong(0), ctypes.c_ulong(0))
        return bool(libc.prctl(option, *arguments) == 0)
    except (ImportError, OSError, AttributeError):
        return False


@dataclass
class Running:
    """An accepted shell command or cell."""

    execution: str
    kind: str
    owner: str = field(default_factory=lambda: f"call-{secrets.token_hex(8)}")
    task: asyncio.Task[None] | None = None
    queued: bool = False
    """A cell waiting for its kernel; cancelling drops it from the queue."""
    started: bool = False
    cancel_requested: bool = False
    restarted: bool = False
    """Its kernel was restarted under it."""
    closing: bool = False
    """Its session was closed under it, which stops the kernel."""
    process: asyncio.subprocess.Process | None = None
    leader: Leader | None = None
    kernel: Kernel | None = None


@dataclass
class Kernel:
    """A session's kernel for one language; its generation grows on every restart."""

    session: str
    language: str
    cwd: Path
    env: dict[str, str]
    generation: int = 0
    state_lost: bool = False
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    """Held by the running cell: cells queue on it in submission order."""
    control: asyncio.Lock = field(default_factory=asyncio.Lock)
    """Held while the kernel starts, stops or restarts."""
    manager: Any = None
    client: Any = None
    owner: str = ""
    leader: Leader | None = None
    last_used: float = field(default_factory=time.monotonic)
    """When a cell last started or ended, for stopping idle kernels."""

    @property
    def kernel_id(self) -> str:
        name = f"krn-{self.session}-{self.language}"
        if len(name) <= 128:
            return name
        # A long session ID would make an ID longer than the worker can report.
        digest = hashlib.sha256(f"{self.session}\0{self.language}".encode()).hexdigest()[:24]
        return f"krn-{digest}-{self.language}"

    def info(self) -> Message:
        return {
            "kernel_id": self.kernel_id,
            "language": self.language,
            "generation": self.generation,
        }

    async def alive(self) -> bool:
        return self.manager is not None and await _alive(self.manager)

    async def ensure_started(self) -> None:
        if await self.alive():
            return
        if self.manager is not None:
            # It died since the last cell: whatever it held is gone.
            await self.stop()
            self.state_lost = True
        try:
            from jupyter_client.manager import AsyncKernelManager
        except ImportError as exc:
            raise ToolError("unsupported", "jupyter_client is not installed here") from exc
        owner = f"kernel-{secrets.token_hex(8)}"
        manager: Any = AsyncKernelManager(kernel_name=KERNEL_NAMES[self.language])
        leader: Leader | None = None
        try:
            await manager.start_kernel(cwd=str(self.cwd), env={**self.env, OWNER: owner})
            pid = getattr(manager.provisioner, "pid", None)
            if isinstance(pid, int) and (info := process_info(pid)) is not None:
                _spawned.add(pid)
                leader = (pid, info.start)
            client = manager.client()
            client.start_channels()
            await client.wait_for_ready(timeout=KERNEL_START_TIMEOUT)
        except Exception as exc:
            with contextlib.suppress(Exception):
                await manager.shutdown_kernel(now=True)
            await asyncio.to_thread(end_owned, owner, leader)
            if leader is not None:
                _spawned.discard(leader[0])
            raise ToolError("unsupported", f"cannot start a {self.language} kernel: {exc}") from exc
        self.manager, self.client, self.owner, self.leader = manager, client, owner, leader
        self.generation += 1

    async def stop(self) -> None:
        """Shut the kernel down, and end what it started."""
        client, manager, owner, leader = self.client, self.manager, self.owner, self.leader
        self.client = self.manager = self.leader = None
        self.owner = ""
        if client is not None:
            with contextlib.suppress(Exception):
                client.stop_channels()
        if manager is not None:
            with contextlib.suppress(Exception):
                await manager.shutdown_kernel(now=True)
        if owner:
            await asyncio.to_thread(end_owned, owner, leader)
        if leader is not None:
            _spawned.discard(leader[0])

    async def restart(self) -> None:
        await self.stop()
        self.state_lost = True
        await self.ensure_started()


async def _alive(manager: Any) -> bool:
    try:
        return bool(await manager.is_alive())
    except Exception:
        return False


async def _interrupt(manager: Any) -> None:
    with contextlib.suppress(Exception):
        await manager.interrupt_kernel()


class Executor:
    """Handles one connection's requests. ``root`` holds sessions/ and shared/."""

    def __init__(
        self,
        root: Path,
        send: Send,
        *,
        subreaper: bool = False,
        protected: bool = False,
        kernel_idle: float | None = None,
    ) -> None:
        self.root = root
        self.kernel_idle = kernel_idle
        """Stop a kernel nobody has used for this many seconds; None keeps them."""
        self.reaper: asyncio.Task[None] | None = None
        self.send = send
        self.subreaper = subreaper
        self.protected = protected
        """Whether this process adopts orphans, so leftovers are found below it."""
        self.running: dict[str, Running] = {}
        self.kernels: dict[tuple[str, str], Kernel] = {}
        self.tasks: set[asyncio.Task[None]] = set()
        self.configured: dict[str, str] = {}
        """Variables from ``configure``, added to every command's and kernel's environment."""
        self.closing = False
        """Shutting down: attempts it stops are not reported as ended. The worker
        reports them lost when this connection closes, since it stopped them."""

    # Plumbing.

    async def reply(self, request: Message, **payload: Any) -> None:
        await self.send({"type": "reply", "id": request.get("id"), "ok": True, **payload})

    async def fail(self, request: Message, error: ToolError) -> None:
        await self.send(
            {
                "type": "reply",
                "id": request.get("id"),
                "ok": False,
                "error": {"kind": error.kind, "message": error.message},
            }
        )

    async def event(self, execution: str, event: str, **payload: Any) -> None:
        await self.send({"type": "event", "execution": execution, "event": event, **payload})

    async def stream(self, execution: str, name: str, text: str) -> None:
        """Send live output, in events of at most STREAM_CHUNK characters."""
        for start in range(0, len(text), STREAM_CHUNK):
            chunk = text[start : start + STREAM_CHUNK]
            await self.event(execution, "stream", name=name, text=chunk)

    def spawn(self, coroutine: Awaitable[None]) -> asyncio.Task[None]:
        task = asyncio.ensure_future(coroutine)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return task

    async def handle(self, request: Message) -> None:
        handlers: dict[str, Callable[[Message], Awaitable[None]]] = {
            "hello": self.op_hello,
            "probe": self.op_probe,
            "configure": self.op_configure,
            "shell": self.op_shell,
            "cell": self.op_cell,
            "pdf": self.op_pdf,
            "cancel": self.op_cancel,
            "restart": self.op_restart,
            "close_session": self.op_close_session,
            "usage": self.op_usage,
            "read": self.op_read,
            "write": self.op_write,
            "list": self.op_list,
            "shared": self.op_shared,
            "remove": self.op_remove,
            "move": self.op_move,
        }
        handler = handlers.get(str(request.get("op")))
        try:
            if handler is None:
                raise ToolError("invalid_request", f"unknown operation {request.get('op')!r}")
            await handler(request)
        except ToolError as error:
            await self.fail(request, error)
        except Exception as exc:  # every request gets a reply
            message = bounded(f"{type(exc).__name__}: {exc}", 2000)
            await self.fail(request, ToolError("internal", message))

    async def shutdown(self) -> None:
        """Stop every command and kernel, then anything still running below this process."""
        self.closing = True
        if self.reaper is not None:
            self.reaper.cancel()
        running = list(self.running.values())
        await asyncio.gather(
            *(self.terminate(item) for item in running if item.process is not None),
            return_exceptions=True,
        )
        for item in running:
            if item.task is not None:
                item.task.cancel()
        await asyncio.gather(
            *(kernel.stop() for kernel in self.kernels.values()), return_exceptions=True
        )
        await asyncio.to_thread(end_descendants)

    # Paths and environment.

    def session(self, request: Message) -> str:
        return self.session_name(request.get("session"))

    def session_name(self, session: object) -> str:
        if not isinstance(session, str) or not _SESSION_RE.fullmatch(session):
            raise ToolError("invalid_request", f"invalid session {session!r}")
        return session

    def session_dir(self, session: object) -> Path:
        return self.root / "sessions" / self.session_name(session)

    def split(self, session: object, path: object) -> tuple[list[str], list[str]]:
        """A workspace path's base (its directories below the root) and the rest of it."""
        if not isinstance(path, str):
            raise ToolError("invalid_request", "path must be a string")
        check_relative(path)
        if path == SHARED or path.startswith(SHARED + "/"):
            return [SHARED], path.split("/")[1:]
        return ["sessions", self.session_name(session)], [] if path == "." else path.split("/")

    def open_base(self, base: Sequence[str]) -> int:
        """Open the session workspace or the shared area, creating it, through no symlink."""
        root = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        return _walk(root, base, "/".join(base), create=True)

    def directory(self, session: object, path: object) -> Path:
        """A workspace directory's path, once it is known to exist with no symlink on the way."""
        base, rest = self.split(session, path)
        os.close(_walk(self.open_base(base), rest, str(path), create=False))
        return self.root.joinpath(*base, *rest)

    def snapshot(self, session: str) -> tuple[dict[str, tuple[int, int, int]], bool]:
        """The regular files of the session workspace and the shared area.

        Maps each path to its modification time, size and inode number. No
        symlink is followed, to the areas themselves included.
        """
        files: dict[str, tuple[int, int, int]] = {}
        for prefix, base in (("", ["sessions", session]), (SHARED + "/", [SHARED])):
            try:
                directory = self.open_base(base)
            except (OSError, ToolError):
                continue
            if scan_files(directory, prefix, files):
                return files, True
        return files, False

    async def written(
        self, session: str, before: tuple[dict[str, tuple[int, int, int]], bool]
    ) -> Message:
        """The files created or changed since ``before``, for the attempt's result."""
        after = await asyncio.to_thread(self.snapshot, session)
        changed = sorted(path for path, stamp in after[0].items() if before[0].get(path) != stamp)
        names = [printable_path(path) for path in changed[:MAX_WRITTEN]]
        return {
            "written_files": [name for name in names if len(name.encode()) <= MAX_PATH_BYTES],
            "written_files_truncated": before[1] or after[1] or len(changed) > MAX_WRITTEN,
            # Installed packages, after the attempt: the worker counts their changes.
            "environment": await asyncio.to_thread(environment_fingerprint),
        }

    def environment(self, session: str) -> dict[str, str]:
        env = tool_environment(os.environ, sys.executable, sys.prefix)
        env.update(self.configured)
        env["SESSION_WORKSPACE"] = str(self.session_dir(session))
        env["PROJECT_SHARED"] = str(self.root / SHARED)
        return env

    # Operations.

    async def op_hello(self, request: Message) -> None:
        languages = available_languages()
        await self.reply(
            request,
            protocol=PROTOCOL,
            languages=languages,
            location={
                "hostname": socket.gethostname(),
                "system": platform.platform(),
                "python": sys.version.split()[0],
                "pid": os.getpid(),
                "uid": os.getuid(),
                "root": str(self.root),
            },
            subreaper=self.subreaper,
            not_dumpable=self.protected,
            features=available_features(),
            environment=await asyncio.to_thread(environment_fingerprint),
        )

    async def op_probe(self, request: Message) -> None:
        """Describe this sandbox, and say whether each of the worker's sentinel paths shows here."""
        raw: object = request.get("sentinels", [])
        items = cast(list[object], raw) if isinstance(raw, list) else []
        paths = [item for item in items if isinstance(item, str)]
        if not isinstance(raw, list) or len(paths) != len(items) or len(paths) > MAX_SENTINELS:
            raise ToolError("invalid_request", "sentinels must be a short list of paths")
        facts = await asyncio.to_thread(probe_facts)
        seen = {path: await asyncio.to_thread(sentinel_state, path) for path in paths}
        await self.reply(request, facts=facts, sentinels=seen)

    async def op_configure(self, request: Message) -> None:
        """Write configuration files, and add variables to every command's and kernel's environment.

        The worker sends the package clients' configuration this way. Files go
        in ``.executor/config`` or the home directory, and a file's ``variable``
        names it. A file that differs from what this op last wrote there was
        changed by someone else, or was never written by it: it is kept, and
        the reply says so.
        """
        raw_files, raw_env = request.get("files", []), request.get("env", {})
        if not isinstance(raw_files, list) or not isinstance(raw_env, dict):
            raise ToolError("invalid_request", "files must be a list and env an object")
        env: dict[str, str] = {}
        for name, value in cast(dict[object, object], raw_env).items():
            if not (isinstance(name, str) and _ENV_NAME_RE.fullmatch(name)):
                raise ToolError("invalid_request", f"invalid variable name {name!r}")
            if not isinstance(value, str) or "\x00" in value:
                raise ToolError("invalid_request", f"invalid value for {name}")
            env[name] = value
        state = self.root / ".executor"
        files: list[tuple[Path, str, str | None]] = []
        for item in cast(list[object], raw_files):
            fields = cast(dict[str, object], item) if isinstance(item, dict) else {}
            base, path, content = fields.get("base"), fields.get("path"), fields.get("content")
            variable = fields.get("variable")
            if base not in ("config", "home") or not isinstance(content, str):
                raise ToolError("invalid_request", "a file needs a base and string content")
            if variable is not None and not (
                isinstance(variable, str) and _ENV_NAME_RE.fullmatch(variable)
            ):
                raise ToolError("invalid_request", f"invalid variable name {variable!r}")
            if not isinstance(path, str) or path == ".":
                raise ToolError("invalid_request", f"invalid path {path!r}")
            directory = state / "config" if base == "config" else Path.home()
            files.append((directory / check_relative(path), content, variable))
        written, skipped, pointers = await asyncio.to_thread(write_config_files, state, files)
        self.configured = {**env, **pointers}
        await self.reply(request, written=written, skipped=skipped)

    def accept(self, request: Message, kind: str) -> Running:
        execution = request.get("execution")
        if not isinstance(execution, str) or not execution:
            raise ToolError("invalid_request", "missing execution id")
        if execution in self.running:
            raise ToolError("conflict", f"execution {execution} is already running")
        running = Running(execution=execution, kind=kind)
        self.running[execution] = running
        return running

    async def op_pdf(self, request: Message) -> None:
        """Run the PDF helper as its own process, like a shell call's argv, in the session."""
        if "pdf" not in available_features():
            raise ToolError("unsupported", "pdf_oxide is not installed in this environment")
        await self.op_shell({**request, "argv": pdf_argv(request), "cwd": "."})

    async def op_shell(self, request: Message) -> None:
        session = self.session(request)
        try:
            cwd = self.directory(session, request.get("cwd", "."))
        except ToolError as error:
            if error.kind != "not_found":
                raise
            raise ToolError(
                "invalid_request", f"working directory {request.get('cwd')!r} does not exist"
            ) from None
        raw_argv: object = request.get("argv")
        if raw_argv is None:
            raw_argv = ["/bin/bash", "-c", str(request.get("script", ""))]
        if not isinstance(raw_argv, list):
            raise ToolError("invalid_request", "argv must be a list of strings")
        argv = [item for item in cast(list[object], raw_argv) if isinstance(item, str)]
        if not argv or len(argv) != len(cast(list[object], raw_argv)):
            raise ToolError("invalid_request", "argv must be a non-empty list of strings")
        running = self.accept(request, "shell")
        await self.reply(request)
        running.task = self.spawn(
            self.run_shell(
                running,
                argv,
                cwd,
                self.environment(session),
                float(request.get("timeout", 120)),
                int(request.get("limit", 1024 * 1024)),
            )
        )

    async def run_shell(
        self,
        running: Running,
        argv: list[str],
        cwd: Path,
        env: dict[str, str],
        timeout: float,
        limit: int,
    ) -> None:
        execution = running.execution
        started = time.monotonic()
        wall_started = time.time()
        captures = {name: Capture(name, limit) for name in ("stdout", "stderr")}
        state, error, exit_code, signal_number = "failed", None, None, None
        written: Message = {}
        session = env["SESSION_WORKSPACE"].rsplit("/", 1)[-1]
        not_started = {"kind": "cancelled", "message": "cancelled before it started"}
        try:
            if running.cancel_requested:
                state, error = "cancelled", not_started
                return
            before = await asyncio.to_thread(self.snapshot, session)
            if running.cancel_requested:
                state, error = "cancelled", not_started
                return
            try:
                process = await asyncio.create_subprocess_exec(
                    *argv,
                    cwd=cwd,
                    env={**env, OWNER: running.owner},
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    start_new_session=True,
                )
            except OSError as exc:
                error = {"kind": "invalid_request", "message": f"cannot run {argv[0]!r}: {exc}"}
                return
            _spawned.add(process.pid)
            info = process_info(process.pid)
            running.leader = (process.pid, info.start) if info is not None else None
            running.process = process
            running.started = True
            await self.event(execution, "state", state="running")
            readers = [
                asyncio.ensure_future(self.pump(execution, process.stdout, captures["stdout"])),
                asyncio.ensure_future(self.pump(execution, process.stderr, captures["stderr"])),
            ]
            if running.cancel_requested:  # asked for while the command was starting
                self.spawn(self.terminate(running))
            timed_out = False
            try:
                await asyncio.wait_for(process.wait(), timeout)
            except TimeoutError:
                timed_out = True
                await self.terminate(running)
            # What the command left running may not outlive it, and would hold its pipes.
            await asyncio.to_thread(end_owned, running.owner, running.leader)
            _, pending = await asyncio.wait(readers, timeout=5)
            for reader in pending:
                reader.cancel()
            written = await self.written(session, before)
            code = process.returncode
            exit_code = code if code is not None and code >= 0 else None
            signal_number = -code if code is not None and code < 0 else None
            if running.cancel_requested:
                state = "cancelled"
                error = {"kind": "cancelled", "message": "cancelled on request"}
            elif timed_out:
                state = "timed_out"
                error = {"kind": "timed_out", "message": f"exceeded the {timeout:g} s limit"}
            else:
                state = "succeeded" if code == 0 else "failed"
        finally:
            if running.process is not None:
                _spawned.discard(running.process.pid)
            self.running.pop(execution, None)
            result = {
                "exit_code": exit_code,
                "signal": signal_number,
                "stdout": captures["stdout"].summary(),
                "stderr": captures["stderr"].summary(),
                "error": error,
                "elapsed_seconds": time.monotonic() - started,
                "sandbox_started": wall_started,
                "sandbox_ended": time.time(),
                **written,
            }
            if not self.closing:
                await self.event(execution, "end", state=state, result=result)

    async def pump(
        self, execution: str, stream: asyncio.StreamReader | None, capture: Capture
    ) -> None:
        if stream is None:
            return
        while chunk := await stream.read(65536):
            if text := capture.feed(chunk):
                await self.stream(execution, capture.name, text)

    async def terminate(self, running: Running) -> None:
        """SIGTERM to the command and what it started, then SIGKILL after a grace period."""
        process = running.process
        if process is None:
            return
        owner, leader = running.owner, running.leader
        await asyncio.to_thread(
            lambda: signal_processes(owned_processes(owner, leader), signal.SIGTERM)
        )
        try:
            await asyncio.wait_for(process.wait(), TERM_GRACE)
        except TimeoutError:
            await asyncio.to_thread(end_owned, owner, leader)
            await process.wait()

    def kernel(self, session: str, language: str) -> Kernel:
        if language not in KERNEL_NAMES:
            raise ToolError("unsupported", f"no {language!r} kernel here")
        key = (session, language)
        if key not in self.kernels:
            cwd = self.directory(session, ".")
            self.kernels[key] = Kernel(session, language, cwd, self.environment(session))
        return self.kernels[key]

    async def op_cell(self, request: Message) -> None:
        session = self.session(request)
        kernel = self.kernel(session, str(request.get("language", "python")))
        code = request.get("code")
        if not isinstance(code, str):
            raise ToolError("invalid_request", "code must be a string")
        running = self.accept(request, "cell")
        running.kernel = kernel
        await self.reply(request)
        running.task = self.spawn(
            self.run_cell(
                running,
                kernel,
                code,
                float(request.get("timeout", 120)),
                int(request.get("limit", 1024 * 1024)),
            )
        )

    async def run_cell(
        self, running: Running, kernel: Kernel, code: str, timeout: float, limit: int
    ) -> None:
        execution = running.execution
        started = time.monotonic()
        wall_started = time.time()
        captures = {name: Capture(name, limit) for name in ("stdout", "stderr")}
        outputs: list[Message] = []
        result: Message = {}
        state = "failed"
        ran_on: Message | None = None
        try:
            if running.cancel_requested:
                raise _NotStartedError
            running.queued = True
            async with kernel.lock:
                running.queued = False
                async with kernel.control:
                    await kernel.ensure_started()
                if running.cancel_requested:  # asked for while the kernel started
                    raise _NotStartedError
                running.started = True
                ran_on = kernel.info()
                state_lost, kernel.state_lost = kernel.state_lost, False
                before = await asyncio.to_thread(self.snapshot, kernel.session)
                await self.event(execution, "state", state="running")
                state, result = await self.execute(
                    running, kernel, code, timeout, limit, captures, outputs
                )
                result["state_lost"] = state_lost or result.get("state_lost", False)
                result.update(await self.written(kernel.session, before))
        except (asyncio.CancelledError, _NotStartedError):
            state = "cancelled"
            result = {"error": {"kind": "cancelled", "message": "cancelled before it started"}}
        except ToolError as error:
            state = "failed"
            result = {"error": {"kind": error.kind, "message": error.message}}
        except Exception as exc:
            state = "failed"
            message = bounded(f"{type(exc).__name__}: {exc}", 2000)
            result = {"error": {"kind": "internal", "message": message}}
        finally:
            running.queued = False
            self.running.pop(execution, None)
            kernel.last_used = time.monotonic()
            if ran_on is None and kernel.generation:
                ran_on = kernel.info()
            result.update(
                stdout=captures["stdout"].summary(),
                stderr=captures["stderr"].summary(),
                outputs=outputs,
                kernel=ran_on,
                elapsed_seconds=time.monotonic() - started,
                sandbox_started=wall_started,
                sandbox_ended=time.time(),
            )
            if not self.closing:
                await self.event(execution, "end", state=state, result=result)

    async def execute(
        self,
        running: Running,
        kernel: Kernel,
        code: str,
        timeout: float,
        limit: int,
        captures: dict[str, Capture],
        outputs: list[Message],
    ) -> tuple[str, Message]:
        from queue import Empty

        loop = asyncio.get_running_loop()
        # This generation's client and manager, even once a restart replaces the kernel's.
        client, manager = kernel.client, kernel.manager
        msg_id = client.execute(code, store_history=True, allow_stdin=False)
        deadline = loop.time() + timeout
        interrupted_at: float | None = None
        timed_out = False
        error: Message | None = None
        execution_count: int | None = None
        spent = omitted = 0
        died: Message = {
            "status": "error",
            "error": {"kind": "kernel_died", "message": "the kernel process ended"},
        }
        while True:
            if running.restarted or running.closing:
                message = (
                    "the session was closed" if running.closing else "the kernel was restarted"
                )
                return "cancelled", {
                    "status": "aborted",
                    "error": {"kind": "cancelled", "message": message},
                    "state_lost": True,
                }
            now = loop.time()
            if not timed_out and now >= deadline:
                timed_out = True
                interrupted_at = now
                await _interrupt(manager)
            if interrupted_at is not None and now - interrupted_at > INTERRUPT_GRACE:
                # The kernel ignored the interrupt: restart it, losing its state.
                async with kernel.control:
                    if not (running.restarted or running.closing):
                        await kernel.restart()
                kind = "timed_out" if timed_out else "cancelled"
                return kind, {
                    "status": "aborted",
                    "error": {"kind": kind, "message": "the kernel did not stop and was restarted"},
                    "state_lost": True,
                }
            if running.cancel_requested and interrupted_at is None:
                interrupted_at = now
                await _interrupt(manager)
            try:
                message = await client.get_iopub_msg(timeout=0.5)
            except Empty:
                if running.restarted or running.closing:
                    continue
                if not await _alive(manager):
                    kernel.state_lost = True
                    return "failed", died
                continue
            except (Exception, asyncio.CancelledError):
                # Its channels closed under it, which cancels a pending receive: a
                # restart, or a kernel gone away. Unless this task itself is cancelled.
                task = asyncio.current_task()
                if task is not None and task.cancelling():
                    raise
                if running.restarted or running.closing:
                    continue
                kernel.state_lost = True
                return "failed", died
            # The kernel runs research code: its messages are checked like anything else.
            if not isinstance(message, dict):
                continue
            message = cast(Message, message)
            header = message.get("parent_header")
            if not isinstance(header, dict) or cast(Message, header).get("msg_id") != msg_id:
                continue
            kind, content = message.get("msg_type"), message.get("content")
            if not isinstance(content, dict):
                continue
            content = cast(Message, content)
            if kind == "status" and content.get("execution_state") == "idle":
                break
            if kind == "stream":
                capture = captures.get(str(content.get("name", "stdout")), captures["stdout"])
                data = str(content.get("text", "")).encode("utf-8", "replace")
                if text := capture.feed(data):
                    await self.stream(running.execution, capture.name, text)
            elif kind == "execute_input":
                execution_count = _count(content.get("execution_count"))
            elif kind in ("display_data", "execute_result"):
                data = _mime_bundle(content.get("data", {}))
                size = sum(len(value.encode("utf-8", "replace")) for value in data.values())
                spent += size
                if spent > limit or len(outputs) >= MAX_OUTPUTS:
                    omitted += 1
                    if omitted > 1:
                        continue  # one note at the end stands for the rest
                    data = {"text/plain": f"[output of {size} bytes omitted: over the limit]"}
                output: Message = {
                    "type": "result" if kind == "execute_result" else "display",
                    "data": data,
                    "execution_count": _count(content.get("execution_count")),
                }
                outputs.append(output)
                await self.event(running.execution, "output", output=output)
            elif kind == "error":
                name = bounded(str(content.get("ename") or ""), 256)
                value = bounded(str(content.get("evalue") or ""), MAX_ERROR_TEXT)
                lines = content.get("traceback")
                lines = (
                    cast(list[object], lines)[-MAX_TRACEBACK_LINES:]
                    if isinstance(lines, list)
                    else []
                )
                error = {
                    "kind": "exception",
                    "message": f"{name}: {value}",
                    "name": name,
                    "value": value,
                    "traceback": [
                        bounded(_ANSI_RE.sub("", str(line)), MAX_TRACEBACK_LINE) for line in lines
                    ],
                }
        if omitted > 1:
            note: Message = {
                "type": "display",
                "data": {"text/plain": f"[{omitted - 1} more outputs omitted: over the limit]"},
                "execution_count": None,
            }
            outputs.append(note)
            await self.event(running.execution, "output", output=note)
        status = "error" if error else "ok"
        with contextlib.suppress(Empty, Exception):
            reply = await client.get_shell_msg(timeout=5)
            if reply.get("parent_header", {}).get("msg_id") == msg_id:
                reported = reply["content"].get("status")
                status = reported if reported in ("ok", "error", "aborted") else status
        result: Message = {"status": status, "execution_count": execution_count}
        if running.cancel_requested or timed_out:
            kind = "cancelled" if running.cancel_requested else "timed_out"
            reason = (
                "interrupted on request"
                if running.cancel_requested
                else f"exceeded the {timeout:g} s limit"
            )
            interruption: Message = {"kind": kind, "message": reason}
            if error is not None:
                # The kernel's own account of the interrupt: KeyboardInterrupt in Python.
                interruption.update(
                    message=f"{reason}: {error['name']}",
                    name=error["name"],
                    value=error["value"],
                    traceback=error["traceback"],
                )
            result["error"] = interruption
            return kind, result
        if error is not None:
            # A cell that raised: its exception, as the kernel reported it.
            result["error"] = error
            return "failed", result
        return "succeeded", result

    async def op_cancel(self, request: Message) -> None:
        running = self.running.get(str(request.get("execution")))
        if running is None:
            await self.reply(request, found=False)
            return
        running.cancel_requested = True
        await self.reply(request, found=True)
        if running.kind == "shell":
            if running.process is not None:
                self.spawn(self.terminate(running))
            # Before then, run_shell sees cancel_requested and never starts the command.
        elif running.queued and running.task is not None:
            running.task.cancel()  # waiting for its kernel: it leaves the queue
        # A cell starting or running sees cancel_requested itself: one starting never
        # runs, and a running one is interrupted by its loop.

    async def op_restart(self, request: Message) -> None:
        session = self.session(request)
        kernel = self.kernel(session, str(request.get("language", "python")))
        async with kernel.control:
            # The running cell does not hold up a restart: it ends, cancelled.
            for running in self.running.values():
                if running.kernel is kernel and running.started:
                    running.restarted = True
            if kernel.manager is None:
                await kernel.ensure_started()
            else:
                await kernel.restart()
        await self.reply(request, kernel=kernel.info())

    async def reap_idle_kernels(self, idle: float) -> None:
        """Stop kernels unused for ``idle`` seconds; the session's next cell reports lost state."""
        interval = max(0.2, min(30.0, idle / 2))
        while True:
            await asyncio.sleep(interval)
            for kernel in list(self.kernels.values()):
                if kernel.manager is None or time.monotonic() - kernel.last_used < idle:
                    continue
                async with kernel.control:
                    busy = any(running.kernel is kernel for running in self.running.values())
                    if kernel.manager is None or busy:
                        continue
                    if time.monotonic() - kernel.last_used < idle:
                        continue
                    await kernel.stop()
                    kernel.state_lost = True
                print(
                    f"executor: stopped kernel {kernel.kernel_id} after {idle:.0f} s unused",
                    file=sys.stderr,
                )

    async def op_close_session(self, request: Message) -> None:
        """Stop a session's kernels; reply with the ones that were running.

        Its running cells end cancelled and its queued ones never start. Each kernel
        keeps its generation, so the session's next cell starts a new one and reports
        that earlier state is gone. Shell calls are not affected.
        """
        session = self.session(request)
        stopped: list[Message] = []
        for (owner, _), kernel in list(self.kernels.items()):
            if owner != session:
                continue
            for running in self.running.values():
                if running.kernel is kernel:
                    running.cancel_requested = True
                    if running.started:
                        running.closing = True
            async with kernel.control:
                if kernel.manager is not None:
                    stopped.append(kernel.info())
                    await kernel.stop()
                    kernel.state_lost = True
        await self.reply(request, kernels=stopped)

    async def op_usage(self, request: Message) -> None:
        """The CPU time used below this executor, and its running kernels and calls.

        The worker samples it to tell an environment whose kernels still compute,
        say in a background thread, from an idle one.
        """
        cpu = await asyncio.to_thread(cpu_seconds_below, os.getpid())
        kernels = sum(1 for kernel in self.kernels.values() if kernel.manager is not None)
        await self.reply(request, cpu_seconds=cpu, kernels=kernels, running=len(self.running))

    async def op_read(self, request: Message) -> None:
        path = request.get("path")
        offset = int(request.get("offset", 0))
        length = min(int(request.get("length", MAX_READ)), MAX_READ)
        if offset < 0 or length < 0:
            raise ToolError("invalid_request", "offset and length must not be negative")
        base, rest = self.split(request.get("session"), path)
        if not rest:
            raise ToolError("invalid_request", f"{path!r} is not a regular file")
        parent = _walk(self.open_base(base), rest[:-1], str(path), create=False)
        try:
            # Without O_NONBLOCK, opening a FIFO would wait for a writer.
            flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
            descriptor = os.open(rest[-1], flags, dir_fd=parent)
        except FileNotFoundError:
            raise ToolError("not_found", f"no file {path!r}") from None
        except OSError as exc:
            raise _refused(parent, rest[-1], str(path), exc) from exc
        finally:
            os.close(parent)
        # In a thread: hashing a large file takes a while, and must not stall other work.
        hashed = request.get("hash", True) is not False
        reply = await asyncio.to_thread(read_range, descriptor, offset, length, hashed, str(path))
        await self.reply(request, **reply)

    async def op_write(self, request: Message) -> None:
        path = request.get("path")
        base, rest = self.split(request.get("session"), path)
        if not rest:
            raise ToolError("invalid_request", f"{path!r} is a directory")
        try:
            content = base64.b64decode(str(request.get("content_b64", "")), validate=True)
        except ValueError as exc:
            raise ToolError("invalid_request", "content_b64 is not base64") from exc
        expected = request.get("expected_sha256")
        create_only = bool(request.get("create_only", False))
        if base == [SHARED] and expected is None and not create_only:
            raise ToolError(
                "invalid_request", "writes to the shared area need expected_sha256 or create_only"
            )
        executable = request.get("executable")
        try:
            parent = _walk(self.open_base(base), rest[:-1], str(path), create=True)
            reply = await asyncio.to_thread(
                write_file,
                parent,
                rest[-1],
                content,
                expected,
                create_only,
                str(path),
                executable if isinstance(executable, bool) else None,
            )
        except OSError as exc:
            raise _file_error(exc, str(path)) from exc
        await self.reply(request, **reply)

    async def op_shared(self, request: Message) -> None:
        """The shared area's files with their stamps, and the SHA-256 of some (D79).

        ``hash`` names the files whose contents the worker needs, hashed up to
        ``hash_limit`` bytes each and ``hash_budget`` bytes in all; larger ones are
        listed in ``too_large``, and the rest wait for the next scan. ``mark`` replaces
        the worker's mark of this environment, which the reply carries.
        """
        raw: object = request.get("hash", [])
        wanted = [path for path in cast(list[object], raw) if isinstance(path, str)][:MAX_SNAPSHOT]
        limit = max(0, int(request.get("hash_limit", 0)))
        budget = max(0, int(request.get("hash_budget", 0)))
        mark = request.get("mark")
        reply = await asyncio.to_thread(
            self.shared_state,
            wanted,
            limit,
            budget,
            mark if isinstance(mark, str) else None,
            request.get("open") is True,
        )
        await self.reply(request, **reply)

    def shared_state(
        self,
        wanted: Sequence[str],
        limit: int,
        budget: int,
        mark: str | None,
        open_files: bool = False,
    ) -> Message:
        if mark is not None:
            _write_mark(self.root, mark[:256])
        current = _read_mark(self.root)
        files, truncated, partial, odd = scan_shared(self.open_base([SHARED]))
        digests: dict[str, str] = {}
        too_large: list[str] = []
        for path in wanted:
            stamp = files.get(path)
            if stamp is None:
                continue
            if stamp[1] > limit:
                too_large.append(path)
                continue
            if stamp[1] > budget:
                continue  # hashed at a later scan
            try:
                _, rest = self.split(None, path)
                parent = _walk(self.open_base([SHARED]), rest[:-1], path, create=False)
            except (OSError, ToolError):
                continue
            try:
                digest = _stamped_sha256(parent, rest[-1], stamp)
            except (OSError, ToolError):
                continue  # gone or replaced meanwhile: compared again next time
            finally:
                os.close(parent)
            if digest is not None:
                digests[path] = digest
                budget -= stamp[1]
        reply: Message = {
            "files": files,
            "truncated": truncated,
            "partial": partial,
            "odd": odd,
            "sha256": digests,
            "too_large": too_large,
            "mark": current,
            "now": time.time_ns(),
        }
        if open_files:
            reply["open"] = open_shared_files(self.root / SHARED)
        return reply

    async def op_remove(self, request: Message) -> None:
        """Remove a regular file in the shared area if it still has the expected SHA-256.

        Directories the removal leaves empty are removed too, up to the shared area.
        """
        path = request.get("path")
        base, rest = self.split(request.get("session"), path)
        if base != [SHARED] or not rest:
            raise ToolError("invalid_request", "only files in the shared area are removed")
        expected = request.get("expected_sha256")
        if not isinstance(expected, str):
            raise ToolError("invalid_request", "removing a shared file needs expected_sha256")
        directories = [self.open_base(base)]
        try:
            for name in rest[:-1]:
                directories.append(_walk(os.dup(directories[-1]), [name], str(path), create=False))
            await asyncio.to_thread(_remove_checked, directories[-1], rest[-1], expected, str(path))
            for depth in range(len(rest) - 1, 0, -1):
                try:
                    os.rmdir(rest[depth - 1], dir_fd=directories[depth - 1])
                except OSError:
                    break  # not empty, or not ours to remove
        except OSError as exc:
            raise _file_error(exc, str(path)) from exc
        finally:
            for directory in directories:
                os.close(directory)
        await self.reply(request, removed=True)

    async def op_move(self, request: Message) -> None:
        """Give a file in the shared area another name there, which must be free (D79).

        The file keeps its inode, so a process still writing to it writes under the new
        name: the sync moves an environment's version of a file to its conflict copy.
        """
        path, target = request.get("path"), request.get("target")
        base, rest = self.split(None, path)
        target_base, target_rest = self.split(None, target)
        if base != [SHARED] or target_base != [SHARED] or not rest or not target_rest:
            raise ToolError("invalid_request", "only files in the shared area are moved")
        source = _walk(self.open_base(base), rest[:-1], str(path), create=False)
        try:
            destination = _walk(
                self.open_base(target_base), target_rest[:-1], str(target), create=True
            )
            try:
                stamp = await asyncio.to_thread(
                    _move_file, source, rest[-1], destination, target_rest[-1], str(path)
                )
            except OSError as exc:
                raise _file_error(exc, str(path)) from exc
            finally:
                os.close(destination)
        finally:
            os.close(source)
        await self.reply(request, stamp=stamp)

    async def op_list(self, request: Message) -> None:
        path = request.get("path", ".")
        base, rest = self.split(request.get("session"), path)
        try:
            directory = _walk(self.open_base(base), rest, str(path), create=False)
        except ToolError as error:
            if error.kind == "not_found":
                raise ToolError("not_found", f"no directory {path!r}") from None
            raise
        limit = min(int(request.get("limit", MAX_LIST)), MAX_LIST)
        entries, truncated = await asyncio.to_thread(list_directory, directory, limit)
        await self.reply(request, entries=entries, truncated=truncated)


def tool_environment(base: Mapping[str, str], python: str, prefix: str) -> dict[str, str]:
    """``base`` with the environment's tools first on PATH, as the tools spec requires.

    The executor runs with the environment's interpreter, so its directory holds the
    environment's commands; without it, Sage's interfaces could not find ``gp`` or ``gap``
    (lab 5). A Conda environment is also named in ``CONDA_PREFIX``.
    """
    env = dict(base)
    tools = os.path.dirname(python)
    rest = [entry for entry in env.get("PATH", "").split(os.pathsep) if entry and entry != tools]
    env["PATH"] = os.pathsep.join([tools, *rest])
    if os.path.isdir(os.path.join(prefix, "conda-meta")):
        env["CONDA_PREFIX"] = prefix
    return env


def available_features() -> list[str]:
    """What this environment offers beyond kernels: ``pdf`` with the helper and pdf_oxide."""
    features: list[str] = []
    if PDF_HELPER.is_file() and importlib.util.find_spec("pdf_oxide") is not None:
        features.append("pdf")
    return features


def pdf_argv(request: Message) -> list[str]:
    """The PDF helper's command line for a ``pdf`` request, run with this interpreter."""
    operation, path = str(request.get("operation", "")), str(request.get("path", ""))
    if operation not in ("info", "pages", "search", "render"):
        raise ToolError("invalid_request", f"no PDF operation {operation!r}")
    argv = [sys.executable, "-I", str(PDF_HELPER), operation, path]
    match operation:
        case "pages":
            argv += ["--pages", str(request.get("pages", ""))]
            if request.get("include_artifacts") is True:
                argv.append("--include-artifacts")
        case "search":
            argv += ["--query", str(request.get("query", ""))]
        case "render":
            argv += ["--page", str(request.get("page", "")), "--dpi", str(request.get("dpi", 150))]
            region: object = request.get("region")
            if isinstance(region, list):
                values = cast(list[object], region)
                argv += ["--region", ",".join(str(value) for value in values)]
            out: object = request.get("out")
            if isinstance(out, str):
                argv += ["--out", out]
        case _:
            pass
    return argv


def available_languages() -> list[str]:
    """The languages whose kernelspecs this interpreter's Jupyter installation finds."""
    if not all(importlib.util.find_spec(name) for name in ("jupyter_client", "ipykernel")):
        return []
    try:
        from jupyter_client.kernelspec import KernelSpecManager

        specs: dict[str, str] = KernelSpecManager().find_kernel_specs()
    except Exception:  # a broken Jupyter installation offers no kernels
        return []
    return [language for language, name in KERNEL_NAMES.items() if name in specs]


def probe_facts() -> Message:
    """Facts about the machine and this process, for the worker's isolation check (D43).

    The worker reads the same facts on its own host and compares them. Only files
    are read, no tools run; a value that cannot be read is ``None``.
    """
    status = _process_status()
    return {
        "kernel_release": _clip(platform.release()),
        "boot_id": _first_line("/proc/sys/kernel/random/boot_id"),
        "hostname": _clip(socket.gethostname()),
        "machine_id": _first_line("/etc/machine-id"),
        "seccomp": {"0": "disabled", "1": "strict", "2": "filter"}.get(status.get("Seccomp", "")),
        "no_new_privs": {"0": False, "1": True}.get(status.get("NoNewPrivs", "")),
        "capabilities": _clip(status.get("CapEff", "")),
        "uid": os.getuid(),
        "hypervisor": _cpu_flag("hypervisor"),
        "sys_vendor": _first_line("/sys/class/dmi/id/sys_vendor"),
        "product_name": _first_line("/sys/class/dmi/id/product_name"),
    }


def sentinel_state(path: str) -> str:
    """``present`` or ``absent`` for ``path`` here, or ``unknown`` when that cannot be told.

    The file need not be readable: seeing it at all means sharing the worker's files.
    """
    if not os.path.isabs(path):
        return "unknown"
    try:
        os.lstat(path)
    except (FileNotFoundError, NotADirectoryError):
        return "absent"
    except OSError:  # such as a directory on the way that this user cannot search
        return "unknown"
    return "present"


def _clip(text: str) -> str | None:
    return text.strip()[:PROBE_TEXT] or None


def _first_line(path: str) -> str | None:
    try:
        with open(path, "rb") as handle:
            data = handle.read(PROBE_TEXT)
    except OSError:
        return None
    return _clip(data.decode("utf-8", "replace").split("\n", 1)[0])


def _process_status() -> dict[str, str]:
    """``/proc/self/status`` as name to value."""
    try:
        with open("/proc/self/status", encoding="utf-8", errors="replace") as handle:
            lines = handle.read(65536).splitlines()
    except OSError:
        return {}
    return {name: value.strip() for name, _, value in (line.partition(":") for line in lines)}


def _cpu_flag(flag: str) -> bool | None:
    """Whether the first CPU's flags include ``flag``; ``None`` if no flags are listed."""
    try:
        with open("/proc/cpuinfo", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                name, _, value = line.partition(":")
                if name.strip() == "flags":
                    return flag in value.split()
    except OSError:
        return None
    return None


def write_config_files(
    state: Path, files: list[tuple[Path, str, str | None]]
) -> tuple[list[str], list[Message], dict[str, str]]:
    """Write ``configure``'s files: (paths written, files kept with the reason, variables).

    ``state`` holds the record of what was written, by path and SHA-256, so a
    file changed since is recognized and kept. Each variable names its file,
    once the file is there.
    """
    record_path = state / CONFIG_RECORD
    try:
        loaded: object = json.loads(record_path.read_text())
    except (OSError, ValueError):
        loaded = {}
    record = cast(dict[str, object], loaded) if isinstance(loaded, dict) else {}
    written: list[str] = []
    skipped: list[Message] = []
    variables: dict[str, str] = {}
    for target, content, variable in files:
        data = content.encode()
        digest = hashlib.sha256(data).hexdigest()
        try:
            changed, reason = _write_config_file(target, data, digest, record.get(str(target)))
        except OSError as exc:
            changed, reason = False, f"cannot write it: {exc}"
        if reason is not None:
            skipped.append({"path": str(target), "reason": reason})
        else:
            record[str(target)] = digest
            if changed:
                written.append(str(target))
        if variable is not None and target.is_file():
            variables[variable] = str(target)
    try:
        _replace_file(record_path, json.dumps(record, indent=1).encode())
    except OSError as exc:
        skipped.append({"path": str(record_path), "reason": f"cannot write it: {exc}"})
    return written, skipped, variables


def _write_config_file(
    target: Path, data: bytes, digest: str, recorded: object
) -> tuple[bool, str | None]:
    """Write ``data`` unless another file is there: (whether it wrote, why it kept the file)."""
    if target.is_symlink() or (target.exists() and not target.is_file()):
        return False, "it is not a regular file"
    if target.exists():
        current = hashlib.sha256(target.read_bytes()).hexdigest()
        if current == digest:
            return False, None
        if current != recorded:
            if recorded is None:
                return False, "the executor did not write it"
            return False, "it changed after the executor wrote it"
    _replace_file(target, data)
    return True, None


def _replace_file(target: Path, data: bytes) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(dir=target.parent, prefix=".configure-")
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
        os.chmod(temporary, 0o644)
        os.replace(temporary, target)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(temporary)
        raise


def _count(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _mime_bundle(data: object) -> dict[str, str]:
    if not isinstance(data, dict):
        return {}
    return {
        str(mime): value if isinstance(value, str) else json.dumps(value)
        for mime, value in cast(dict[object, object], data).items()
    }


# Lines.


def encode_line(message: Message) -> bytes:
    """One protocol line: JSON in UTF-8. An unpaired surrogate becomes '?'."""
    text = json.dumps(message, ensure_ascii=False, separators=(",", ":"))
    return (text + "\n").encode("utf-8", "replace")


def fit_line(message: Message) -> bytes | None:
    """``message`` as a line no longer than MAX_LINE, or None if it cannot be one.

    An ``end`` event's result gives up output, its largest part first, until it
    fits. A reply that does not fit becomes an error reply.
    """
    line = encode_line(message)
    if len(line) <= MAX_LINE:
        return line
    if message.get("type") == "event" and message.get("event") == "end":
        result = dict(cast(Message, message.get("result") or {}))
        while _shrink(result):
            line = encode_line({**message, "result": result})
            if len(line) <= MAX_LINE:
                return line
        error = {"kind": "internal", "message": "the result was too large to send"}
        return encode_line({**message, "state": "failed", "result": {"error": error}})
    if message.get("type") == "reply":
        error = {"kind": "internal", "message": f"the reply was over {MAX_LINE} bytes"}
        return encode_line({"type": "reply", "id": message.get("id"), "ok": False, "error": error})
    return None


def _shrink(result: Message) -> bool:
    """Halve the largest part of a result over 1 KiB; False when no part is that large."""
    sizes: dict[str, int] = {}
    parts: dict[str, object] = {
        "stdout": _item(result.get("stdout"), "text"),
        "stderr": _item(result.get("stderr"), "text"),
        "outputs": result.get("outputs"),
        "traceback": _item(result.get("error"), "traceback"),
        "written_files": result.get("written_files"),
    }
    for name, part in parts.items():
        if part:
            sizes[name] = len(json.dumps(part, ensure_ascii=False))
    largest = max(sizes, key=sizes.__getitem__, default=None)
    if largest is None or sizes[largest] <= 1024:
        return False
    if largest in ("stdout", "stderr"):
        text = str(parts[largest])
        keep = len(text) // 4
        marker = f"\n[… {len(text) - 2 * keep} characters omitted to fit the result …]\n"
        shorter = text[:keep] + marker + text[len(text) - keep :]
        result[largest] = {**result[largest], "text": shorter, "truncated": True}
    elif largest == "outputs":
        outputs = cast(list[Message], parts[largest])
        kept = outputs[: len(outputs) // 2]
        note = f"[{len(outputs) - len(kept)} outputs omitted to fit the result]"
        result["outputs"] = [*kept, {"type": "display", "data": {"text/plain": note}}]
    elif largest == "traceback":
        lines = cast(list[str], parts[largest])
        shorter = (
            lines[len(lines) // 2 :] if len(lines) > 1 else [bounded(lines[0], len(lines[0]) // 2)]
        )
        result["error"] = {**result["error"], "traceback": shorter}
    else:
        files = cast(list[str], parts[largest])
        result["written_files"] = files[: len(files) // 2]
        result["written_files_truncated"] = True
    return True


def _item(container: object, key: str) -> object:
    return cast(Message, container).get(key) if isinstance(container, dict) else None


async def serve_stdio(
    root: Path,
    *,
    subreaper: bool = False,
    protected: bool = False,
    kernel_idle: float | None = None,
) -> None:
    """Serve one connection on stdin and stdout until stdin closes."""
    loop = asyncio.get_running_loop()
    # Keep the protocol on its own descriptor; anything else printing to stdout
    # (a library, a kernel's launcher) goes to stderr instead.
    protocol_fd = os.dup(1)
    os.dup2(2, 1)
    reader = asyncio.StreamReader(limit=MAX_REQUEST)
    await loop.connect_read_pipe(lambda: asyncio.StreamReaderProtocol(reader), sys.stdin)
    transport, protocol = await loop.connect_write_pipe(
        asyncio.streams.FlowControlMixin, os.fdopen(protocol_fd, "wb", buffering=0)
    )
    writer = asyncio.StreamWriter(transport, protocol, None, loop)
    lock = asyncio.Lock()

    async def send(message: Message) -> None:
        line = fit_line(message)
        if line is None:
            print("executor: dropped an event too large to send", file=sys.stderr)
            return
        async with lock:
            writer.write(line)
            await writer.drain()

    executor = Executor(
        root, send, subreaper=subreaper, protected=protected, kernel_idle=kernel_idle
    )
    if kernel_idle is not None:
        executor.reaper = executor.spawn(executor.reap_idle_kernels(kernel_idle))
    try:
        while True:
            try:
                line = await reader.readline()
            except ValueError:  # a request over MAX_REQUEST, already discarded
                continue
            if not line:
                break
            try:
                request = json.loads(line)
            except (ValueError, RecursionError):
                await send(
                    {
                        "type": "reply",
                        "id": None,
                        "ok": False,
                        "error": {"kind": "invalid_request", "message": "not JSON"},
                    }
                )
                continue
            if not isinstance(request, dict):
                continue
            executor.spawn(executor.handle(cast(Message, request)))
    finally:
        await executor.shutdown()
        with contextlib.suppress(Exception):
            writer.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Sandbox Science executor")
    parser.add_argument("--stdio", action="store_true", required=True, help="serve on stdin/stdout")
    parser.add_argument("--root", required=True, type=Path, help="project directory")
    parser.add_argument(
        "--kernel-idle", type=float, default=None, help="stop kernels unused this many seconds"
    )
    args = parser.parse_args(argv)
    root: Path = args.root
    root.mkdir(parents=True, exist_ok=True)
    state = root / ".executor"
    (state / "runtime").mkdir(parents=True, exist_ok=True)
    (root / SHARED).mkdir(exist_ok=True)
    (state / "ipython").mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("JUPYTER_RUNTIME_DIR", str(state / "runtime"))
    os.environ.setdefault("IPYTHONDIR", str(state / "ipython"))
    protected = protect_process()
    # A subreaper adopts its descendants' orphans, so leftovers stay findable below it.
    subreaper = _prctl(PR_SET_CHILD_SUBREAPER, 1)
    idle: float | None = args.kernel_idle
    if idle is not None and not idle > 0:
        parser.error("--kernel-idle must be a positive number of seconds")
    asyncio.run(
        serve_stdio(root.resolve(), subreaper=subreaper, protected=protected, kernel_idle=idle)
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
