# SPDX-License-Identifier: MIT
"""Host process identities and session membership for launcher recovery.

Linux uses procfs clock ticks and its boot UUID. Darwin uses psutil's kernel
process creation time and boot time. Neither platform trusts a recycled PID.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def boot_id() -> str:
    """A value that changes across host boots."""
    if sys.platform == "darwin":
        import psutil

        return f"darwin-{int(psutil.boot_time())}"
    return Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip()


def start_time(pid: int) -> int | None:
    """A process's creation identity in platform units, or None if it is gone."""
    if sys.platform == "darwin":
        import psutil

        try:
            return int(psutil.Process(pid).create_time() * 1_000_000)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            return None
    try:
        with open(f"/proc/{pid}/stat", "rb") as stat:
            return int(stat.read().rsplit(b")", 1)[1].split()[19])
    except (OSError, IndexError, ValueError):
        return None


def members(session: int) -> list[int]:
    """Live processes in a session, including children in other process groups."""
    if sys.platform == "darwin":
        import psutil

        found: list[int] = []
        for process in psutil.process_iter(attrs=["pid", "status"]):
            try:
                if (
                    process.info["status"] != psutil.STATUS_ZOMBIE
                    and os.getsid(process.pid) == session
                ):
                    found.append(process.pid)
            except (ProcessLookupError, PermissionError, psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        return found
    found: list[int] = []
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            with open(f"/proc/{entry}/stat", "rb") as stat:
                fields = stat.read().rsplit(b")", 1)[1].split()
            if fields[0] != b"Z" and int(fields[3]) == session:
                found.append(int(entry))
        except (OSError, IndexError, ValueError):
            continue
    return found


def command_has(pid: int, command: str) -> bool:
    """Whether the live process command line includes the recorded executable."""
    if sys.platform == "darwin":
        import psutil

        try:
            return command in psutil.Process(pid).cmdline()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            return False
    try:
        return command.encode() in Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
    except OSError:
        return False
