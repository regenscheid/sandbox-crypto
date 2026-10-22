# SPDX-License-Identifier: MIT
"""The executor's file operations, confined to the session workspace and the shared area.

The executor (executor.py) loads this file beside itself in a sandbox, so it depends only
on the standard library. Paths are opened relative to a directory descriptor, without
following symbolic links; the file a reply names is the file read or written.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import errno
import hashlib
import json
import os
import re
import secrets
import shutil
import stat
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Final, cast

Message = dict[str, Any]

MAX_SNAPSHOT = 20000
"""Files scanned before and after an attempt to find the ones it wrote."""
MAX_DEPTH = 128
"""Directory levels scanned below the session workspace and the shared area."""
MAX_PATH_BYTES = 4096
MAX_OBSERVATION_ENTRIES = 64
MAX_OBSERVATION_BYTES = 16 * 1024
MAX_OBSERVATION_SCAN_ENTRIES = 256
MAX_OBSERVATION_SCAN_BYTES = 64 * 1024
SHARED = "shared"
DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC


class ToolError(Exception):
    """A failure reported to the worker service with one of the spec's error kinds."""

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind
        self.message = message


def encode_line(message: Message) -> bytes:
    """One protocol line: JSON in UTF-8. An unpaired surrogate becomes '?'."""
    text = json.dumps(message, ensure_ascii=False, separators=(",", ":"))
    return (text + "\n").encode("utf-8", "replace")


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


def path_refusal(parent: int, name: str, path: str, exc: OSError) -> ToolError:
    """Why ``name`` in the directory ``parent`` cannot be used on the way to ``path``."""
    with contextlib.suppress(OSError):
        if stat.S_ISLNK(os.lstat(name, dir_fd=parent).st_mode):
            return ToolError("invalid_request", f"path {path!r} goes through a symlink")
    if exc.errno == errno.ENOTDIR:
        return ToolError("invalid_request", f"path {path!r}: {name!r} is not a directory")
    return ToolError("invalid_request", f"cannot use {path!r}: {exc.strerror}")


def _open_directory(parent: int, name: str, path: str, *, create: bool) -> int:
    try:
        return os.open(name, DIRECTORY_FLAGS, dir_fd=parent)
    except FileNotFoundError:
        if not create:
            raise ToolError("not_found", f"{path!r} does not exist") from None
    except OSError as exc:
        raise path_refusal(parent, name, path, exc) from exc
    with contextlib.suppress(FileExistsError):
        os.mkdir(name, dir_fd=parent)
    try:
        return os.open(name, DIRECTORY_FLAGS, dir_fd=parent)
    except OSError as exc:
        raise path_refusal(parent, name, path, exc) from exc


def walk(directory: int, names: Sequence[str], path: str, *, create: bool) -> int:
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


class SnapshotFiles(dict[str, tuple[int, int, int]]):
    """Compatibility map plus a separately bounded, lossless metadata sidecar.

    This adds no filesystem operations. The original walk's regular-file/depth/
    directory limitations remain; its result is never mutation-complete.
    """

    def __init__(self) -> None:
        super().__init__()
        self.raw: dict[tuple[str, bytes], tuple[int, int, int]] = {}
        self.raw_bytes = 0
        self.limited = False
        self.error = False
        self.path_omitted = False
        self.started_ns = time.monotonic_ns()
        self.finished_ns: int | None = None

    def observe(self, area: str, path: str, stamp: tuple[int, int, int]) -> None:
        if len(self.raw) >= MAX_OBSERVATION_SCAN_ENTRIES:
            self.limited = True
            return
        raw = os.fsencode(path)
        if (
            len(raw) > MAX_PATH_BYTES
            or b"\0" in raw
            or any(part in (b"", b".", b"..") for part in raw.split(b"/"))
        ):
            self.path_omitted = True
            return
        size = len(raw) + 128
        if self.raw_bytes + size > MAX_OBSERVATION_SCAN_BYTES:
            self.limited = True
            return
        self.raw[(area, raw)] = stamp
        self.raw_bytes += size


def _scan_error(files: dict[str, tuple[int, int, int]]) -> None:
    if isinstance(files, SnapshotFiles):
        files.error = True


def scan_files(
    directory: int, prefix: str, files: dict[str, tuple[int, int, int]], *, area: str = "session"
) -> bool:
    """Add the regular files below ``directory`` (which is consumed) to ``files``.

    Depth first, holding open only the directories on the current path. Returns
    True when MAX_SNAPSHOT files stopped the scan.
    """
    path = [(directory, prefix, _subdirectories(directory, prefix, files, area, prefix))]
    try:
        while path:
            if len(files) >= MAX_SNAPSHOT:
                if isinstance(files, SnapshotFiles):
                    files.limited = True
                return True
            current, relative, pending = path[-1]
            if not pending or len(path) > MAX_DEPTH:
                if pending and isinstance(files, SnapshotFiles):
                    files.limited = True
                os.close(current)
                path.pop()
                continue
            name = pending.pop()
            try:
                child = os.open(name, DIRECTORY_FLAGS, dir_fd=current)
            except OSError:
                _scan_error(files)
                continue
            below = f"{relative}{name}/"
            path.append((child, below, _subdirectories(child, below, files, area, prefix)))
        return False
    finally:
        for current, _, _ in path:
            os.close(current)


def _subdirectories(
    directory: int,
    relative: str,
    files: dict[str, tuple[int, int, int]],
    area: str = "session",
    root_prefix: str = "",
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
                    _scan_error(files)
                    continue
                stamp = (info.st_mtime_ns, info.st_size, info.st_ino)
                files[relative + entry.name] = stamp
                if isinstance(files, SnapshotFiles):
                    # The namespace may have changed since is_file(). Preserve
                    # the compatibility map, but do not label special metadata
                    # as a regular-file observation.
                    if not stat.S_ISREG(info.st_mode):
                        files.error = True
                        continue
                    # The root is tagged by the caller before legacy textual
                    # prefixes or printable_path can collapse distinct paths.
                    files.observe(area, relative[len(root_prefix) :] + entry.name, stamp)
    except OSError:
        _scan_error(files)
    return names


def observation_report(before: object, after: object) -> Message | None:
    """Project only already-observed metadata; missing-after is not a deletion proof."""
    if not isinstance(before, SnapshotFiles) or not isinstance(after, SnapshotFiles):
        return None
    entries: list[Message] = []
    report: Message = {
        "protocol": 1,
        "source": "regular_stat_report_v1",
        "clock": "executor_monotonic",
        "before_started_ns": before.started_ns,
        "before_finished_ns": before.finished_ns,
        "after_started_ns": after.started_ns,
        "after_finished_ns": after.finished_ns,
        "entries": entries,
        "coverage": {
            "before_limited": before.limited,
            "after_limited": after.limited,
            "before_error": before.error,
            "after_error": after.error,
            "path_omitted": before.path_omitted or after.path_omitted,
            "report_truncated": False,
        },
    }

    def metadata(stamp: tuple[int, int, int] | None) -> Message | None:
        if stamp is None:
            return None
        return {"mtime_ns": stamp[0], "size": stamp[1], "inode": stamp[2]}

    for area, raw in sorted(before.raw.keys() | after.raw.keys()):
        previous, current = before.raw.get((area, raw)), after.raw.get((area, raw))
        if previous == current:
            continue
        kind = "not_seen_after" if current is None else "regular_stat_difference"
        if current is not None and previous is None and (before.limited or before.error):
            kind = "regular_observed"
        entry: Message = {
            "kind": kind,
            "area": area,
            "path_b64": base64.b64encode(raw).decode("ascii"),
            "before": metadata(previous),
            "after": metadata(current),
        }
        if len(entries) >= MAX_OBSERVATION_ENTRIES:
            report["coverage"]["report_truncated"] = True
            break
        entries.append(entry)
        # Reserve room for host-side explicit defaults in canonical serialization.
        if len(encode_line(report)) > MAX_OBSERVATION_BYTES - 512:
            entries.pop()
            report["coverage"]["report_truncated"] = True
            break
    return report


def bounded_observations(message: Message) -> Message:
    """Keep the independent sidecar bounded before end fitting or recovery compaction."""
    raw = message.get("output_observations")
    if raw is None:
        return message
    pending: list[tuple[object, int]] = [(raw, 0)]
    size = nodes = 0
    while pending:
        value, depth = pending.pop()
        nodes += 1
        if nodes > 2048 or depth > 6:
            break
        if isinstance(value, str):
            if len(value) > 8192:
                break
            size += len(value.encode("utf-8", "replace"))
        elif isinstance(value, dict):
            if len(cast(dict[object, object], value)) > 64:
                break
            pending.extend(
                (item, depth + 1)
                for pair in cast(dict[object, object], value).items()
                for item in pair
            )
        elif isinstance(value, (list, tuple)):
            if len(cast(list[object] | tuple[object, ...], value)) > 64:
                break
            pending.extend(
                (item, depth + 1) for item in cast(list[object] | tuple[object, ...], value)
            )
        elif value is None or type(value) in (bool, int):
            if type(value) is int and value.bit_length() > 64:
                break
            size += 24
        else:
            break
        if size > MAX_OBSERVATION_BYTES:
            break
    else:
        try:
            if len(encode_line({"report": raw})) <= MAX_OBSERVATION_BYTES:
                return message
        except (ValueError, TypeError, OverflowError, RecursionError):
            pass  # invalid derived data must not prevent the terminal result
    return {**message, "output_observations": None, "observation_omitted": "oversized"}


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
                            child = os.open(name, DIRECTORY_FLAGS, dir_fd=current)
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
    if_unused: bool = False,
) -> Message:
    """Create or atomically replace ``name`` in the directory ``parent``, which is consumed.

    The reply's ``stamp`` is the new file's modification time, size and inode as written,
    taken before the file takes its name, so a later change shows as a different stamp.
    With ``if_unused``, a file that another process is writing is left alone, and the
    reply says ``busy``.
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
        swapped = False
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(content)
                if executable is not None:
                    os.fchmod(handle.fileno(), 0o755 if executable else 0o644)
                handle.flush()
                written = os.fstat(handle.fileno())
            if info is not None and if_unused and being_written(_absolute(parent, name)):
                os.unlink(temporary, dir_fd=parent)
                return {"busy": True}
            try:
                swapped = checked is not None and _exchange(parent, temporary, name)
            except FileNotFoundError:
                raise ToolError("conflict", f"{path!r} changed while it was written") from None
            if swapped:
                # The file that was there is at the temporary name now; if it changed
                # after its check, it goes back, and so it does if a process opened it for
                # writing just before the swap, whose writes would otherwise be lost.
                if checked is not None and _changed_since(parent, temporary, checked):
                    _exchange(parent, temporary, name)
                    swapped = False
                    raise ToolError("conflict", f"{path!r} changed while it was written")
                if if_unused and being_written(_absolute(parent, temporary)):
                    _exchange(parent, temporary, name)
                    swapped = False
                    os.unlink(temporary, dir_fd=parent)
                    return {"busy": True}
                swapped = False
                os.unlink(temporary, dir_fd=parent)
            else:
                if checked is not None and _changed_since(parent, name, checked):
                    raise ToolError("conflict", f"{path!r} changed while it was written")
                os.replace(temporary, name, src_dir_fd=parent, dst_dir_fd=parent)
        except BaseException:
            if swapped:
                # The file that was there could not go back: it keeps a name of its own,
                # in sight, rather than being lost under the temporary one.
                with contextlib.suppress(OSError):
                    recovered = f"{name}.recovered-{secrets.token_hex(4)}"
                    os.rename(temporary, recovered, src_dir_fd=parent, dst_dir_fd=parent)
            else:
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


def _absolute(parent: int, name: str) -> str:
    """The path of ``name`` in the directory ``parent``, as ``/proc`` shows it."""
    return os.path.join(os.readlink(f"/proc/self/fd/{parent}"), name)


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


def move_file(source: int, name: str, destination: int, target: str, path: str) -> list[int]:
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


_WHOLE_EXECUTOR: Final = frozenset(
    {errno.ENOSPC, errno.EDQUOT, errno.EIO, errno.EROFS, errno.EMFILE, errno.ENFILE, errno.ENOMEM}
)
"""Errors that concern the executor or its disk rather than one file."""


def file_error(exc: OSError, path: str) -> ToolError:
    """A failure on one file, by kind; a full disk or an exhausted executor stops the sync."""
    if exc.errno in _WHOLE_EXECUTOR:
        return ToolError("unavailable", f"cannot change {path!r}: {exc.strerror}")
    if exc.errno in (errno.EACCES, errno.EPERM):
        return ToolError("policy_denied", f"cannot change {path!r}: {exc.strerror}")
    return ToolError("invalid_request", f"cannot change {path!r}: {exc.strerror}")


def read_mark(root: Path) -> str | None:
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


def write_mark(root: Path, mark: str) -> None:
    temporary = root / f".write-{secrets.token_hex(8)}"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
    with os.fdopen(os.open(temporary, flags, 0o600), "w") as handle:
        handle.write(mark)
    os.replace(temporary, root / SYNC_MARK)


def being_written(path: str) -> bool:
    """Whether another process has the file ``path`` open for writing, or mapped writable.

    Read from ``/proc``, at the moment it matters. Processes it may not inspect, such as
    non-dumpable ones, are left out, so False is not a proof.
    """
    own = str(os.getpid())
    try:
        processes = [entry for entry in os.listdir("/proc") if entry.isdigit() and entry != own]
    except OSError:
        return False
    for process in processes:
        try:
            descriptors = os.listdir(f"/proc/{process}/fd")
        except OSError:
            continue
        for descriptor in descriptors:
            try:
                if os.readlink(f"/proc/{process}/fd/{descriptor}") != path:
                    continue
                with open(f"/proc/{process}/fdinfo/{descriptor}") as info:
                    flags = next(
                        int(line.split()[1], 8) for line in info if line.startswith("flags:")
                    )
            except (OSError, StopIteration, ValueError, IndexError):
                continue
            if flags & (os.O_WRONLY | os.O_RDWR):
                return True
        try:
            with open(f"/proc/{process}/maps") as maps:
                for line in maps:
                    fields = line.split(maxsplit=5)
                    shared_write = len(fields) == 6 and "w" in fields[1] and "s" in fields[1]
                    if shared_write and fields[5].rstrip("\n") == path:
                        return True  # a shared, writable mapping writes to the file
        except OSError:
            continue
    return False


def remove_checked(
    parent: int, name: str, expected: str, path: str, *, if_unused: bool = False
) -> bool:
    """Unlink ``name`` if it is the regular file whose content hashes to ``expected``.

    With ``if_unused``, a file that another process is writing stays, and the result is
    False: the file is first moved aside, so a writer that opened it a moment before is
    still seen, and then put back.
    """
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
        if not if_unused:
            os.unlink(name, dir_fd=parent)
            return True
        if being_written(_absolute(parent, name)):
            return False
        aside = f".write-remove-{secrets.token_hex(8)}"  # a name scans leave out
        os.rename(name, aside, src_dir_fd=parent, dst_dir_fd=parent)
        if being_written(_absolute(parent, aside)):
            # Back under its name, unless a new file took the name meanwhile.
            try:
                os.link(aside, name, src_dir_fd=parent, dst_dir_fd=parent)
            except FileExistsError:
                recovered = f"{name}.recovered-{secrets.token_hex(4)}"
                os.rename(aside, recovered, src_dir_fd=parent, dst_dir_fd=parent)
                return False
            os.unlink(aside, dir_fd=parent)
            return False
        os.unlink(aside, dir_fd=parent)
        return True


def stamped_sha256(parent: int, name: str, stamp: Sequence[int]) -> str | None:
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


# Skill packs staged for helper code (docs/specs/skills.md, SK-16): each snapshot in its
# own folder below the environment's root, written by the harness, then made read-only.
# Sandbox code can still change them, so each later load checks every file's SHA-256.

STAGED = ".skills"
MAX_STAGED_ENTRIES = 8192
"""Entries, files and folders, a staged pack's check reads at most; past them, or past
``MAX_STAGED_DEPTH`` folders down, it is not as expected."""
MAX_STAGED_DEPTH = 32
FILE_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC


def _staged(
    directory: int,
    prefix: str,
    sizes: dict[str, int],
    found: dict[str, tuple[str, bool]],
    counted: list[int],
) -> None:
    """Each file below ``directory`` by its path, with its SHA-256 and whether it is
    executable; anything else (a symlink, a FIFO, a file of another size than ``sizes``
    expects, which is never hashed, or more entries or folders than allowed) as ``?``."""
    if prefix.count("/") >= MAX_STAGED_DEPTH:
        found[prefix] = ("?", False)
        return
    for entry in os.scandir(directory):
        path = prefix + entry.name
        counted[0] += 1
        if counted[0] > MAX_STAGED_ENTRIES:
            found["?"] = ("?", False)
            return
        if entry.is_dir(follow_symlinks=False):
            child = os.open(entry.name, DIRECTORY_FLAGS, dir_fd=directory)
            try:
                _staged(child, path + "/", sizes, found, counted)
            finally:
                os.close(child)
            continue
        found[path] = ("?", False)
        if not entry.is_file(follow_symlinks=False):
            continue
        descriptor = os.open(entry.name, FILE_FLAGS, dir_fd=directory)
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_size != sizes.get(path):
                continue
            digest = hashlib.sha256()
            while chunk := os.read(descriptor, 1 << 20):
                digest.update(chunk)
            found[path] = (digest.hexdigest(), bool(info.st_mode & stat.S_IXUSR))
        finally:
            os.close(descriptor)


def _modes(directory: int, sealed: bool) -> None:
    """Make the tree below ``directory`` read-only, or writable again to remove it. A file
    is opened as ``_staged`` opens one, never blocking, and left as it is when it is no
    longer a regular file: sandbox code can put a FIFO in its place between the check and
    the seal, whose open would wait for a writer (re-check of #493), and the next load's
    check finds it."""
    for entry in os.scandir(directory):
        if entry.is_dir(follow_symlinks=False):
            child = os.open(entry.name, DIRECTORY_FLAGS, dir_fd=directory)
            try:
                _modes(child, sealed)
            finally:
                os.close(child)
        elif entry.is_file(follow_symlinks=False):
            descriptor = os.open(entry.name, FILE_FLAGS, dir_fd=directory)
            try:
                info = os.fstat(descriptor)
                if not stat.S_ISREG(info.st_mode):
                    continue
                executable = info.st_mode & stat.S_IXUSR
                os.fchmod(
                    descriptor, (0o555 if executable else 0o444) if sealed else 0o644 | executable
                )
            finally:
                os.close(descriptor)
    os.fchmod(directory, 0o555 if sealed else 0o755)


def _remove(parent: int, name: str) -> None:
    """Remove ``name`` below ``parent``, whatever it is, through no symlink."""
    try:
        info = os.lstat(name, dir_fd=parent)
    except FileNotFoundError:
        return
    if not stat.S_ISDIR(info.st_mode):
        os.unlink(name, dir_fd=parent)
        return
    directory = os.open(name, DIRECTORY_FLAGS, dir_fd=parent)
    try:
        os.fchmod(directory, 0o700)
        for entry in list(os.scandir(directory)):
            _remove(directory, entry.name)
    finally:
        os.close(directory)
    os.rmdir(name, dir_fd=parent)


def stage_check(
    root: Path, source: str, snapshot: str, files: Sequence[Message], seal: bool
) -> Message:
    """Whether the snapshot is staged as ``files`` say, and nothing more; with ``seal``,
    once it is, it is made read-only."""
    base = [STAGED, source, snapshot]
    location = str(root.joinpath(*base))
    expected = {str(f["path"]): (str(f["sha256"]), bool(f.get("executable"))) for f in files}
    sizes = {str(f["path"]): f["size"] for f in files if isinstance(f.get("size"), int)}
    found: dict[str, tuple[str, bool]] = {}
    try:
        top = walk(os.open(root, DIRECTORY_FLAGS), base, location, create=False)
    except ToolError:  # missing, or a symlink or file in its place: staged again
        return {"location": location, "ready": False, "differing": sorted(expected)}
    try:
        _staged(top, "", sizes, found, [0])
        differing = sorted(p for p in {*found, *expected} if found.get(p) != expected.get(p))
        if not differing and seal:
            _modes(top, sealed=True)
    finally:
        os.close(top)
    return {
        "location": location,
        "ready": not differing,
        "sealed": seal and not differing,
        "differing": differing,
    }


def stage_put(
    root: Path, source: str, snapshot: str, reset: bool, files: Sequence[tuple[str, bytes, bool]]
) -> int:
    """Write ``files`` where the snapshot is staged, after removing it with ``reset``."""
    # A symlink or file sandbox code put at .skills or .skills/<source> goes first.
    for names in ([STAGED], [STAGED, source]):
        above = walk(os.open(root, DIRECTORY_FLAGS), names[:-1], STAGED, create=True)
        try:
            with contextlib.suppress(FileNotFoundError):
                if not stat.S_ISDIR(os.lstat(names[-1], dir_fd=above).st_mode):
                    os.unlink(names[-1], dir_fd=above)
        finally:
            os.close(above)
    parent = walk(
        os.open(root, DIRECTORY_FLAGS), [STAGED, source], f"{STAGED}/{source}", create=True
    )
    try:
        if reset:
            _remove(parent, snapshot)
        top = walk(parent, [snapshot], snapshot, create=True)
        parent = -1
    finally:
        if parent >= 0:
            os.close(parent)
    try:
        for path, content, executable in files:
            names = check_relative(path).split("/")
            directory = walk(os.dup(top), names[:-1], path, create=True)
            try:
                with contextlib.suppress(FileNotFoundError):
                    os.unlink(names[-1], dir_fd=directory)
                flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
                descriptor = os.open(names[-1], flags, 0o600, dir_fd=directory)
                try:
                    view = memoryview(content)
                    while view:
                        view = view[os.write(descriptor, view) :]
                    os.fchmod(descriptor, 0o755 if executable else 0o644)
                finally:
                    os.close(descriptor)
            finally:
                os.close(directory)
    finally:
        os.close(top)
    return len(files)


_SESSION_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
_SOURCE_RE = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?")
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_COMMAND_RE = re.compile(r"[a-z0-9][a-z0-9_.-]{0,63}")


class Requests:
    """Translate file and staged-skill request data into bounded file operations.

    These adapters return reply payloads. Protocol framing and authoritative binding
    admission stay with the executor, without access to its lifecycle state here.
    """

    def __init__(self, root: Path, *, max_read: int, max_list: int) -> None:
        self.root = root
        self.max_read = max_read
        self.max_list = max_list

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
        return walk(root, base, "/".join(base), create=True)

    def directory(self, session: object, path: object) -> Path:
        """A workspace directory's path, once it is known to exist with no symlink on the way."""
        base, rest = self.split(session, path)
        os.close(walk(self.open_base(base), rest, str(path), create=False))
        return self.root.joinpath(*base, *rest)

    async def read(self, request: Message) -> Message:
        path = request.get("path")
        offset = int(request.get("offset", 0))
        length = min(int(request.get("length", self.max_read)), self.max_read)
        if offset < 0 or length < 0:
            raise ToolError("invalid_request", "offset and length must not be negative")
        base, rest = self.split(request.get("session"), path)
        if not rest:
            raise ToolError("invalid_request", f"{path!r} is not a regular file")
        parent = walk(self.open_base(base), rest[:-1], str(path), create=False)
        try:
            # Without O_NONBLOCK, opening a FIFO would wait for a writer.
            flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
            descriptor = os.open(rest[-1], flags, dir_fd=parent)
        except FileNotFoundError:
            raise ToolError("not_found", f"no file {path!r}") from None
        except OSError as exc:
            raise path_refusal(parent, rest[-1], str(path), exc) from exc
        finally:
            os.close(parent)
        # In a thread: hashing a large file takes a while, and must not stall other work.
        hashed = request.get("hash", True) is not False
        return await asyncio.to_thread(read_range, descriptor, offset, length, hashed, str(path))

    def staged(self, request: Message) -> tuple[str, str]:
        source, snapshot = request.get("source"), request.get("snapshot")
        if not (isinstance(source, str) and _SOURCE_RE.fullmatch(source)):
            raise ToolError("invalid_request", f"invalid source {source!r}")
        if not (isinstance(snapshot, str) and _SHA256_RE.fullmatch(snapshot)):
            raise ToolError("invalid_request", "snapshot must be a SHA-256")
        return source, snapshot

    async def skills_check(self, request: Message, path: str) -> tuple[Message, list[str]]:
        """Whether a pack is staged as the harness expects it, and with ``seal``, made
        read-only once it is (docs/specs/skills.md, SK-16)."""
        source, snapshot = self.staged(request)
        raw = request.get("files", [])
        items = cast(list[object], raw) if isinstance(raw, list) else []
        files = [cast(Message, item) for item in items if isinstance(item, dict)]
        if not isinstance(raw, list) or len(files) != len(items):
            raise ToolError("invalid_request", "files must be a list of objects")
        for item in files:
            check_relative(str(item.get("path")))
        try:
            found = await asyncio.to_thread(
                stage_check,
                self.root,
                source,
                snapshot,
                files,
                request.get("seal") is True,
            )
        except OSError as exc:
            raise file_error(exc, f"{STAGED}/{source}") from exc
        raw = request.get("commands", [])
        names = (
            [n for n in cast(list[object], raw) if isinstance(n, str)][:256]
            if isinstance(raw, list)
            else []
        )
        found["commands"] = [
            n for n in names if _COMMAND_RE.fullmatch(n) and shutil.which(n, path=path)
        ]
        raw_bindings = request.get("binding_names", [])
        candidates = cast(list[object], raw_bindings) if isinstance(raw_bindings, list) else []
        names = [n for n in candidates if isinstance(n, str)]
        if (
            not isinstance(raw_bindings, list)
            or len(candidates) > 8192
            or len(names) != len(candidates)
            or any(len(n) > 128 for n in names)
        ):
            raise ToolError("invalid_request", "invalid binding inventory candidates")
        return found, names

    async def skills_put(self, request: Message) -> Message:
        """Write a batch of a pack's files where its snapshot is staged (SK-16)."""
        source, snapshot = self.staged(request)
        raw = request.get("files", [])
        files: list[tuple[str, bytes, bool]] = []
        for item in cast(list[object], raw) if isinstance(raw, list) else []:
            fields = cast(Message, item) if isinstance(item, dict) else {}
            path = check_relative(str(fields.get("path")))
            try:
                content = base64.b64decode(str(fields.get("content_b64", "")), validate=True)
            except ValueError as exc:
                raise ToolError("invalid_request", f"{path}: content_b64 is not base64") from exc
            files.append((path, content, fields.get("executable") is True))
        try:
            written = await asyncio.to_thread(
                stage_put,
                self.root,
                source,
                snapshot,
                request.get("reset") is True,
                files,
            )
        except OSError as exc:
            raise file_error(exc, f"{STAGED}/{source}") from exc
        return {"written": written}

    async def write(self, request: Message) -> Message:
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
            parent = walk(self.open_base(base), rest[:-1], str(path), create=True)
            reply = await asyncio.to_thread(
                write_file,
                parent,
                rest[-1],
                content,
                expected,
                create_only,
                str(path),
                executable if isinstance(executable, bool) else None,
                request.get("if_unused") is True,
            )
        except OSError as exc:
            raise file_error(exc, str(path)) from exc
        return reply

    async def shared(self, request: Message) -> Message:
        """The shared area's files with their stamps, and the SHA-256 of some (D79).

        ``hash`` names the files whose contents the worker needs, hashed up to
        ``hash_limit`` bytes each and ``hash_budget`` bytes in all; larger ones are
        listed in ``too_large``, and the rest wait for the next scan. ``mark`` replaces
        the worker's mark of this environment, which the reply carries.
        """
        raw: object = request.get("hash", [])
        named = [path for path in cast(list[object], raw) if isinstance(path, str)]
        wanted = named[:MAX_SNAPSHOT]
        limit = max(0, int(request.get("hash_limit", 0)))
        budget = max(0, int(request.get("hash_budget", 0)))
        mark = request.get("mark")
        return await asyncio.to_thread(
            self.shared_state,
            wanted,
            limit,
            budget,
            mark if isinstance(mark, str) else None,
        )

    def shared_state(
        self,
        wanted: Sequence[str],
        limit: int,
        budget: int,
        mark: str | None,
    ) -> Message:
        if mark is not None:
            write_mark(self.root, mark[:256])
        current = read_mark(self.root)
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
                parent = walk(self.open_base([SHARED]), rest[:-1], path, create=False)
            except (OSError, ToolError):
                continue
            try:
                digest = stamped_sha256(parent, rest[-1], stamp)
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
        return reply

    async def remove(self, request: Message) -> Message:
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
                directories.append(walk(os.dup(directories[-1]), [name], str(path), create=False))
            removed = await asyncio.to_thread(
                remove_checked,
                directories[-1],
                rest[-1],
                expected,
                str(path),
                if_unused=request.get("if_unused") is True,
            )
            if not removed:
                return {"removed": False, "busy": True}
            for depth in range(len(rest) - 1, 0, -1):
                try:
                    os.rmdir(rest[depth - 1], dir_fd=directories[depth - 1])
                except OSError:
                    break  # not empty, or not ours to remove
        except OSError as exc:
            raise file_error(exc, str(path)) from exc
        finally:
            for directory in directories:
                os.close(directory)
        return {"removed": True}

    async def move(self, request: Message) -> Message:
        """Give a file in the shared area another name there, which must be free (D79).

        The file keeps its inode, so a process still writing to it writes under the new
        name: the sync moves an environment's version of a file to its conflict copy.
        """
        path, target = request.get("path"), request.get("target")
        base, rest = self.split(None, path)
        target_base, target_rest = self.split(None, target)
        if base != [SHARED] or target_base != [SHARED] or not rest or not target_rest:
            raise ToolError("invalid_request", "only files in the shared area are moved")
        source = walk(self.open_base(base), rest[:-1], str(path), create=False)
        try:
            destination = walk(
                self.open_base(target_base), target_rest[:-1], str(target), create=True
            )
            try:
                stamp = await asyncio.to_thread(
                    move_file,
                    source,
                    rest[-1],
                    destination,
                    target_rest[-1],
                    str(path),
                )
            except OSError as exc:
                raise file_error(exc, str(path)) from exc
            finally:
                os.close(destination)
        finally:
            os.close(source)
        return {"stamp": stamp}

    async def list(self, request: Message) -> Message:
        path = request.get("path", ".")
        base, rest = self.split(request.get("session"), path)
        try:
            directory = walk(self.open_base(base), rest, str(path), create=False)
        except ToolError as error:
            if error.kind == "not_found":
                raise ToolError("not_found", f"no directory {path!r}") from None
            raise
        limit = min(int(request.get("limit", self.max_list)), self.max_list)
        entries, truncated = await asyncio.to_thread(list_directory, directory, limit)
        return {"entries": entries, "truncated": truncated}
