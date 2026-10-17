# SPDX-License-Identifier: MIT
"""The instance running from a state directory: its lock and its record.

``up`` holds ``launcher.lock`` for as long as it runs, so a second ``up`` on the same
state directory is refused, and ``down`` and ``status`` know whether one runs. ``down``
takes the same lock before it cleans up after a launcher that is gone. The record,
``launcher.json``, says which process the launcher is and which parts it started, so
``down`` can stop it and ``status`` can show it.

A process is known by its ID, its start time and the boot it started in (Linux's
``boot_id``), never by its ID or command line alone: an ID comes round again, after a
reboot or in time, and two state directories run the same commands. A part also leads
its own session. ``down`` or ``up`` stops what a killed launcher left only when all of
that still holds. A part's children do not get its death signal, so they can outlive
it: they are found as the processes still in its session, in the same boot, started
since it did (Linux reuses no ID that a session still has).
"""

from __future__ import annotations

import fcntl
import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, cast

from .config import LauncherError
from .state import Layout, write_private
from .supervise import end_session, members, start_time


def boot_id() -> str:
    return Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip()


@dataclass(frozen=True)
class RecordedPart:
    name: str
    pid: int
    started: int
    """Its start time, in clock ticks since boot (``/proc/<pid>/stat``)."""
    command: str
    """The first of its arguments, which its command line names."""
    address: str | None


@dataclass(frozen=True)
class Record:
    pid: int
    started: int
    boot: str
    started_at: str
    config: str
    gui: str | None
    parts: tuple[RecordedPart, ...]
    purpose: str = "up"
    replacement: bool = False
    """Whether this launcher advertises the explicit runtime replacement control signal."""
    """``up``, or ``down`` while it runs the worker alone to suspend the sandboxes."""

    def dumps(self) -> str:
        fields = asdict(self)
        fields["parts"] = [asdict(part) for part in self.parts]
        return json.dumps(fields, indent=2)


def lock(layout: Layout) -> int:
    """Take the state directory's lock; refused while an ``up`` holds it."""
    descriptor = try_lock(layout)
    if descriptor is None:
        found = read(layout)
        who = f" (process {found.pid})" if found is not None else ""
        raise LauncherError(
            f"Sandbox Science already runs from {layout.root}{who}: stop it with "
            "sandbox-science down"
        )
    return descriptor


def try_lock(layout: Layout) -> int | None:
    """The state directory's lock, held; None while an ``up`` holds it."""
    descriptor = os.open(layout.lock, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(descriptor)
        return None
    return descriptor


def running(layout: Layout) -> bool:
    """Whether an ``up`` holds the state directory's lock."""
    try:
        descriptor = os.open(layout.lock, os.O_RDONLY | os.O_CLOEXEC)
    except FileNotFoundError:
        return False
    try:
        fcntl.flock(descriptor, fcntl.LOCK_SH | fcntl.LOCK_NB)
    except BlockingIOError:
        return True
    finally:
        os.close(descriptor)
    return False


def write(layout: Layout, record: Record) -> None:
    write_private(layout.record, record.dumps() + "\n")


def read(layout: Layout) -> Record | None:
    try:
        raw = cast(dict[str, Any], json.loads(layout.record.read_text(encoding="utf-8")))
        return Record(
            pid=int(raw["pid"]),
            started=int(raw["started"]),
            boot=str(raw["boot"]),
            started_at=str(raw["started_at"]),
            config=str(raw["config"]),
            gui=None if raw["gui"] is None else str(raw["gui"]),
            parts=tuple(
                RecordedPart(
                    name=str(part["name"]),
                    pid=int(part["pid"]),
                    started=int(part["started"]),
                    command=str(part["command"]),
                    address=None if part["address"] is None else str(part["address"]),
                )
                for part in cast(list[dict[str, Any]], raw["parts"])
            ),
            purpose=str(raw.get("purpose", "up")),
            replacement=raw.get("replacement") is True,
        )
    except (OSError, ValueError, KeyError, TypeError):
        return None


def launcher_alive(record: Record) -> bool:
    """Whether the recorded launcher is still the process with that ID."""
    return record.boot == boot_id() and start_time(record.pid) == record.started


def alive(part: RecordedPart, record: Record) -> bool:
    """Whether the part is still that process, leading its session and running its command."""
    if record.boot != boot_id() or start_time(part.pid) != part.started:
        return False
    try:
        if os.getsid(part.pid) != part.pid:
            return False
        arguments = Path(f"/proc/{part.pid}/cmdline").read_bytes().split(b"\0")
    except OSError:
        return False
    return part.command.encode() in arguments


def lingering(part: RecordedPart, record: Record) -> list[int]:
    """The processes still in the part's session, the part's own children that outlived
    it among them."""
    if record.boot != boot_id():
        return []
    return [
        pid
        for pid in members(part.pid)
        if (started := start_time(pid)) is not None and started >= part.started
    ]


def left_behind(record: Record | None) -> list[RecordedPart]:
    """The parts of a launcher that is gone of which something still runs: the part, or
    what it started in its session."""
    if record is None or launcher_alive(record):
        return []
    return [part for part in record.parts if alive(part, record) or lingering(part, record)]


def stop_left_behind(record: Record | None) -> list[RecordedPart]:
    """Stop the parts a launcher killed outright left running, the last started first.

    The caller holds the state directory's lock, and keeps ``record`` from before it
    wrote its own."""
    parts = left_behind(record)
    for part in reversed(parts):
        end_session(part.pid)
    return parts
