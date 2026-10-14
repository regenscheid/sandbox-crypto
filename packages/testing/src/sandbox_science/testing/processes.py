# SPDX-License-Identifier: MIT
"""Processes a test run leaves behind: executors, sandbox daemons, kernels, commands.

A test puts the worker's state under the run's temporary directory, so every process
it starts through the worker either works in a directory there (kernels, commands) or
names one there as its ``--root`` (executors, sandbox daemons). Other agents' test runs
share the machine, so a process counts only when all of these hold:

- it belongs to this user and started after this test process did;
- it is not this process or one of its ancestors (``uv run pytest --basetemp=…``);
- its working directory is a live directory under the base, the same directory (device
  and inode) that the path names now, not a deleted one whose path came round again;
  or its ``--root`` argument is a path under the base. Other arguments that merely
  name a path there, such as ``tail -F <base>/worker.log``, do not count.

Before a kill, the process's start time is read again, so a reused process ID is left
alone. Linux only: it reads ``/proc``.
"""

from __future__ import annotations

import contextlib
import os
import signal
import time
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class _Found:
    start: int
    """Start time in clock ticks since boot, as ``/proc/<pid>/stat`` gives it."""
    description: str


def _stat(pid: int) -> tuple[str, int, int] | None:
    """The state, parent and start time of ``pid``, or None when it is gone."""
    try:
        with open(f"/proc/{pid}/stat", "rb") as handle:
            fields = handle.read().rsplit(b")", 1)[1].split()
    except (OSError, IndexError):
        return None
    return fields[0].decode(), int(fields[1]), int(fields[19])


def _ancestors(pid: int) -> set[int]:
    """``pid`` and every process above it."""
    found: set[int] = set()
    while pid > 0 and pid not in found:
        found.add(pid)
        stat = _stat(pid)
        if stat is None:
            break
        pid = stat[1]
    return found


def _under(path: str, root: str) -> bool:
    return path == root or path.startswith(root + os.sep)


def _root_arguments(argv: list[str]) -> list[str]:
    """The values of ``--root`` in a command line, as an executor or daemon takes it."""
    values: list[str] = []
    for index, argument in enumerate(argv):
        if argument == "--root" and index + 1 < len(argv):
            values.append(argv[index + 1])
        elif argument.startswith("--root="):
            values.append(argument.removeprefix("--root="))
    return values


def _in_live_directory(pid: int, root: str) -> bool:
    """Whether ``pid`` works in a directory under ``root`` that still exists there."""
    try:
        cwd = os.readlink(f"/proc/{pid}/cwd")
        if cwd.endswith(" (deleted)") or not _under(cwd, root):
            return False
        actual, named = os.stat(f"/proc/{pid}/cwd"), os.stat(cwd)
    except OSError:
        return False
    return (actual.st_dev, actual.st_ino) == (named.st_dev, named.st_ino)


def _scan(base: Path, since: int | None) -> dict[int, _Found]:
    root = str(base.resolve())
    own = os.getpid()
    if since is None:
        stat = _stat(own)
        since = stat[2] if stat is not None else 0
    skip = _ancestors(own)
    uid = os.getuid()
    found: dict[int, _Found] = {}
    for entry in os.listdir("/proc"):
        if not entry.isdigit() or int(entry) in skip:
            continue
        pid = int(entry)
        stat = _stat(pid)
        if stat is None or stat[0] == "Z" or stat[2] < since:
            continue
        try:
            if os.stat(f"/proc/{pid}").st_uid != uid:
                continue
            with open(f"/proc/{pid}/cmdline", "rb") as handle:
                argv = [part.decode(errors="replace") for part in handle.read().split(b"\0")]
        except OSError:
            continue
        roots = [value for value in _root_arguments(argv) if _under(value.rstrip(os.sep), root)]
        if roots or _in_live_directory(pid, root):
            line = " ".join(argv).strip()[:300]
            with contextlib.suppress(OSError):
                line += f" (in {os.readlink(f'/proc/{pid}/cwd')})"
            found[pid] = _Found(stat[2], line)
    return found


def left_behind(base: Path, since: int | None = None) -> dict[int, str]:
    """This run's live processes under ``base`` (see the module), by process ID.

    ``since`` is a start time in clock ticks since boot; processes that started before
    it are not this run's. It defaults to this process's own start.
    """
    return {pid: found.description for pid, found in _scan(base, since).items()}


def end_left_behind(base: Path, grace: float, since: int | None = None) -> dict[int, str]:
    """Wait up to ``grace`` seconds for ``base``'s processes to end, then kill the rest.

    Returns the processes that were still running and were killed.
    """
    deadline = time.monotonic() + grace
    while (left := _scan(base, since)) and time.monotonic() < deadline:
        time.sleep(0.1)
    killed: dict[int, str] = {}
    for pid, found in left.items():
        stat = _stat(pid)
        if stat is None or stat[2] != found.start:  # ended, or its ID was reused since
            continue
        with contextlib.suppress(OSError):
            os.kill(pid, signal.SIGKILL)
            killed[pid] = found.description
    return killed
