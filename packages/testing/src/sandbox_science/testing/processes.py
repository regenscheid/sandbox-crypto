# SPDX-License-Identifier: MIT
"""Processes a test run leaves behind: executors, sandbox daemons, kernels, commands.

Every process a test starts through the worker has its command line or working
directory under the run's temporary directory, since that is where the test puts the
worker's state. Linux only: it reads ``/proc``.
"""

from __future__ import annotations

import contextlib
import os
import signal
import time
from pathlib import Path


def left_behind(base: Path) -> dict[int, str]:
    """This user's live processes whose command line or working directory is under ``base``.

    Maps each process ID to its command line and working directory, for a report.
    """
    root = str(base.resolve())
    found: dict[int, str] = {}
    own = os.getpid()
    for entry in os.listdir("/proc"):
        if not entry.isdigit() or int(entry) == own:
            continue
        try:
            with open(f"/proc/{entry}/stat", "rb") as handle:
                if handle.read().rsplit(b")", 1)[1].split()[0] == b"Z":
                    continue
            with open(f"/proc/{entry}/cmdline", "rb") as handle:
                argv = handle.read().replace(b"\0", b" ").decode(errors="replace").strip()
            cwd = os.readlink(f"/proc/{entry}/cwd")
        except (OSError, IndexError):  # gone, or another user's
            continue
        if root + os.sep in argv or cwd == root or cwd.startswith(root + os.sep):
            found[int(entry)] = f"{argv[:300]} (in {cwd})"
    return found


def end_left_behind(base: Path, grace: float) -> dict[int, str]:
    """Wait up to ``grace`` seconds for ``base``'s processes to end, then kill the rest.

    Returns the processes that were still running and were killed.
    """
    deadline = time.monotonic() + grace
    while (left := left_behind(base)) and time.monotonic() < deadline:
        time.sleep(0.1)
    for pid in left:
        with contextlib.suppress(OSError):
            os.kill(pid, signal.SIGKILL)
    return left
