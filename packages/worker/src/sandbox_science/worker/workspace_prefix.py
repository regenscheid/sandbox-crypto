# SPDX-License-Identifier: MIT
"""The opt-in research prefix identity, using only the sandbox's standard library."""

from __future__ import annotations

import contextlib
import os
import re
import secrets
import stat
import tempfile
from pathlib import Path

PREFIX = Path("/sandbox/.sandbox-science-prefix")
ALIAS = Path("/opt/sandbox-science")
MARKER = ".sandbox-science-prefix-id"


def describe(python_prefix: str) -> dict[str, str | int]:
    """Validate the fixed layout before creating its nonsecret, per-workspace identity.

    Never call this for ordinary/local executors: their Python prefix belongs to the
    operator. The image contains no marker, so first workspace seeding copies only
    packages, and each independent retained workspace receives its own identity.
    """
    if (
        PREFIX.is_symlink()
        or not PREFIX.is_dir()
        or PREFIX.resolve() != PREFIX
        or not ALIAS.is_symlink()
        or ALIAS.resolve() != PREFIX
        or Path(python_prefix).resolve() != PREFIX
        or not (PREFIX / "conda-meta").is_dir()
        or not (PREFIX / "bin/python").is_file()
    ):
        raise OSError("required workspace prefix layout is missing or incompatible")
    marker = PREFIX / MARKER
    if not marker.exists() and not marker.is_symlink():
        # Link a complete, synced identity atomically. Concurrent bridge startups
        # cannot observe a half-written marker or replace an existing identity.
        fd, temporary = tempfile.mkstemp(prefix=".prefix-id-", dir=PREFIX)
        try:
            with os.fdopen(fd, "w", encoding="ascii") as stream:
                stream.write("prefix-" + secrets.token_hex(16))
                stream.flush()
                os.fsync(stream.fileno())
            with contextlib.suppress(FileExistsError):
                os.link(temporary, marker)
        finally:
            os.unlink(temporary)
        directory = os.open(PREFIX, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    fd = os.open(marker, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError("workspace prefix identity is not a regular file")
        identity = os.read(fd, 128).decode("ascii")
    finally:
        os.close(fd)
    if re.fullmatch(r"prefix-[0-9a-f]{32}", identity) is None:
        raise OSError("workspace prefix identity is malformed")
    return {"protocol": 1, "path": str(PREFIX), "identity": identity}


def report(python_prefix: str) -> dict[str, str | int]:
    """Report a bounded failure through hello so the worker can retain a recovery hold."""
    try:
        return describe(python_prefix)
    except (OSError, UnicodeError):
        return {"protocol": 1, "error": "required workspace prefix identity/layout unavailable"}
