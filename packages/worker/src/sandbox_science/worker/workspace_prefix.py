# SPDX-License-Identifier: MIT
"""The opt-in research prefix identity, using only the sandbox's standard library."""

from __future__ import annotations

import contextlib
import json
import os
import re
import secrets
import stat
import tempfile
from pathlib import Path
from typing import cast

PREFIX = Path("/sandbox/.sandbox-science-prefix")
ALIAS = Path("/opt/sandbox-science")
MARKER = ".sandbox-science-prefix-id"
FROZEN = Path("/opt/sandbox-science-frozen")
"""The image's read-only copy of the prefix's frozen trees (images/common/prefix_layout.py)."""
FROZEN_MANIFEST = "manifest.json"
MAX_MANIFEST = 64 * 1024
_TREE = re.compile(
    r"[A-Za-z0-9_][A-Za-z0-9._+-]{0,127}(?:/[A-Za-z0-9_][A-Za-z0-9._+-]{0,127}){0,3}"
)


def _frozen_trees() -> list[str]:
    """The trees the image keeps outside the prefix, from its manifest; none without one."""
    try:
        fd = os.open(FROZEN / FROZEN_MANIFEST, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return []
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError("frozen tree manifest is not a regular file")
        data = os.read(fd, MAX_MANIFEST + 1)
    finally:
        os.close(fd)
    try:
        manifest: object = json.loads(data) if len(data) <= MAX_MANIFEST else None
    except ValueError:
        manifest = None
    fields = cast(dict[str, object], manifest) if isinstance(manifest, dict) else {}
    trees = fields.get("trees")
    entries = cast(list[object], trees) if isinstance(trees, list) else []
    found = [entry for entry in entries if isinstance(entry, str) and _TREE.fullmatch(entry)]
    if (
        fields.get("version") != 1
        or not isinstance(trees, list)
        or len(found) != len(entries)
        or len(found) > 64
    ):
        raise OSError("frozen tree manifest is malformed")
    return found


def check_frozen_links() -> None:
    """Each frozen tree is the image's read-only copy, through the exact link the image left,
    or, in a prefix seeded before the image moved it, a directory of the prefix's own.

    The links live in the workspace, where research code can replace them: one pointing
    anywhere else would put other files behind packages the lockfile names unchanged.
    Raises OSError for any other link, for a link through a linked parent directory, and
    for a link whose target the image lacks.
    """
    for tree in _frozen_trees():
        place = PREFIX
        for part in tree.split("/")[:-1]:
            place = place / part
            if place.is_symlink() or not place.is_dir():
                raise OSError(f"frozen tree {tree} sits under a link or a non-directory")
        link = PREFIX / tree
        if not link.is_symlink():
            if link.exists() and not link.is_dir():
                raise OSError(f"frozen tree {tree} is not a directory")
            continue
        expected = FROZEN / tree
        if os.readlink(link) != str(expected) or expected.is_symlink() or not expected.is_dir():
            raise OSError(f"frozen tree {tree} links elsewhere than {expected}")


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
    check_frozen_links()
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
