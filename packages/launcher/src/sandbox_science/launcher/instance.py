# SPDX-License-Identifier: MIT
"""The instance running from a state directory: its lock and its record.

``up`` holds ``launcher.lock`` for as long as it runs, so a second ``up`` on the same
state directory is refused, and ``down`` and ``status`` know whether one runs. The
record, ``launcher.json``, says which process that is and which parts it started, so
``down`` can stop it and ``status`` can show it. A launcher killed outright leaves its
parts running in their own sessions; the next ``up`` or ``down`` finds them by the
record and stops them.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, cast

from .config import LauncherError
from .state import Layout, write_private
from .supervise import end_session


@dataclass(frozen=True)
class RecordedPart:
    name: str
    pid: int
    command: str
    """The part's command, the first of its arguments, to know it by in ``/proc``."""
    address: str | None


@dataclass(frozen=True)
class Record:
    pid: int
    started_at: str
    config: str
    gui: str | None
    parts: tuple[RecordedPart, ...]

    def dumps(self) -> str:
        return json.dumps(
            {
                "pid": self.pid,
                "started_at": self.started_at,
                "config": self.config,
                "gui": self.gui,
                "parts": [asdict(part) for part in self.parts],
            },
            indent=2,
        )


def lock(layout: Layout) -> int:
    """Take the state directory's lock for this ``up``; refused while another holds it."""
    descriptor = os.open(layout.lock, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(descriptor)
        found = read(layout)
        who = f" (process {found.pid})" if found is not None else ""
        raise LauncherError(
            f"Sandbox Science already runs from {layout.root}{who}: stop it with "
            "sandbox-science down"
        ) from None
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
            started_at=str(raw["started_at"]),
            config=str(raw["config"]),
            gui=None if raw["gui"] is None else str(raw["gui"]),
            parts=tuple(
                RecordedPart(
                    name=str(part["name"]),
                    pid=int(part["pid"]),
                    command=str(part["command"]),
                    address=None if part["address"] is None else str(part["address"]),
                )
                for part in cast(list[dict[str, Any]], raw["parts"])
            ),
        )
    except (OSError, ValueError, KeyError, TypeError):
        return None


def alive(part: RecordedPart) -> bool:
    """Whether the part's process still runs its command (a reused process ID does not)."""
    try:
        arguments = Path(f"/proc/{part.pid}/cmdline").read_bytes().split(b"\0")
    except OSError:
        return False
    return part.command.encode() in arguments


def left_behind(layout: Layout, *, locked: bool = False) -> list[RecordedPart]:
    """Parts a launcher that did not stop cleanly left running; ``locked`` when this
    process holds the lock, so the record is an earlier instance's."""
    record = read(layout)
    if record is None or (not locked and running(layout)):
        return []
    return [part for part in record.parts if alive(part)]


def stop_left_behind(layout: Layout, *, locked: bool = False) -> list[RecordedPart]:
    """Stop the parts a launcher killed outright left running, the last started first,
    and forget its record."""
    if not locked and running(layout):
        return []
    parts = left_behind(layout, locked=True)
    for part in reversed(parts):
        end_session(part.pid)
    with contextlib.suppress(FileNotFoundError):
        layout.record.unlink()
    return parts
