# SPDX-License-Identifier: MIT
"""A scheduler's private checkpoint; it grants no retention or deletion authority."""

from __future__ import annotations

import fcntl
import hashlib
import os
import stat
import tempfile
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

from pydantic import Field

from sandbox_science.contracts.canonical import canonical_json
from sandbox_science.contracts.model import Sha256, StrictContract

from .artifact_maintenance import MaintenancePolicy


class Cursor(StrictContract):
    version: int = Field(default=1, ge=1, le=1)
    scope: Sha256
    after: str = Field(default="", pattern=r"^(?:[0-9a-f]{64})?$")


def scope(database: Path, policy: MaintenancePolicy, apply: bool) -> str:
    """Bind a progress file to its private database path, policy and execution mode."""
    return hashlib.sha256(
        canonical_json(
            {"database": str(database.resolve()), "policy": policy.model_dump(), "apply": apply}
        )
    ).hexdigest()


def _private(fd: int) -> None:
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError("maintenance progress must be a private regular file")


@contextmanager
def progress(path: Path, expected: str) -> Generator[Cursor]:
    """Serialize a scheduled pass; never follow a lock or cursor symlink."""
    descriptor = os.open(
        path.with_name(path.name + ".lock"),
        os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC,
        0o600,
    )
    try:
        _private(descriptor)
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            source = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
        except FileNotFoundError:
            current = Cursor(scope=expected)
        else:
            with os.fdopen(source, "rb") as stream:
                _private(stream.fileno())
                data = stream.read(1025)
                if len(data) > 1024:
                    raise ValueError("maintenance progress exceeds its bound")
                current = Cursor.model_validate_json(data)
                if current.scope != expected:
                    raise ValueError("maintenance progress belongs to another policy or mode")
        yield current
    finally:
        os.close(descriptor)


def save(path: Path, current: Cursor, after: str) -> None:
    """Fsync a new checkpoint before atomically publishing it under the held lock."""
    data = Cursor(scope=current.scope, after=after).model_dump_json().encode()
    descriptor, temporary = tempfile.mkstemp(prefix=".maintenance-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        Path(temporary).unlink(missing_ok=True)
