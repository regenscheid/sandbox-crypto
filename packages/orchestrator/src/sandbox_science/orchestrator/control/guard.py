# SPDX-License-Identifier: MIT
"""A task-local admission check executed inside each authoritative write transaction."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Generator
from contextlib import contextmanager
from contextvars import ContextVar

Check = Callable[[sqlite3.Connection], None]
_CHECK: ContextVar[Check | None] = ContextVar("control_write_guard", default=None)


def check(db: sqlite3.Connection) -> None:
    """Check the calling task's authority after BEGIN IMMEDIATE, before state effects."""
    if verify := _CHECK.get():
        verify(db)


@contextmanager
def guarded(verify: Check) -> Generator[None, None, None]:
    """Retain authority across awaits and copied thread contexts without global mutation."""
    token = _CHECK.set(verify)
    try:
        yield
    finally:
        _CHECK.reset(token)
