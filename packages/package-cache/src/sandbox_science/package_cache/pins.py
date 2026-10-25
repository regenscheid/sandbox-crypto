# SPDX-License-Identifier: MIT
"""Package files that recent environment lockfiles pin, which eviction keeps.

The worker keeps each environment revision's lockfile in its lockfile store,
``<worker state_dir>/lockfiles/blobs/<ab>/<sha256>``, named by the SHA-256 of its JSON,
and deletes one once no revision of an environment it keeps names it (docs/WORKER.md,
"Package commands"). The cache runs in the worker's Pod, on the same volume (D101), and
reads that directory only, never the worker's database. A lockfile written there in the
last ``protect_days`` pins the SHA-256 of every Conda package and pip archive it lists.
Those are the digests of the files themselves, so they name the cache's blobs directly;
a pip distribution installed from an index records none, and is not pinned.

Reading is bounded (``MAX_LOCKFILES`` files of at most ``MAX_LOCKFILE_BYTES`` each) and
cached for ``REFRESH`` seconds, since eviction may run after every stored file while the
store is over its cap. A lockfile that cannot be read or parsed pins nothing; an
unreadable store pins nothing either, so that eviction can still keep the disk bounded.
"""

from __future__ import annotations

import json
import logging
import os
import re
import stat
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Final, cast

from sandbox_science.contracts.revisions import MAX_LOCKFILE_BYTES

from .settings import Settings

MAX_LOCKFILES: Final = 10_000
"""Lockfiles read at most per refresh; the worker keeps 50 per environment."""
REFRESH: Final = 60.0
"""Seconds a reading of the lockfile store is reused."""
FORMAT: Final = "sandbox-science-lockfile"
_SECTIONS: Final = ("conda", "pip")
_HEX64: Final = re.compile(r"[0-9a-f]{64}")
_HEX2: Final = re.compile(r"[0-9a-f]{2}")

_log = logging.getLogger(__name__)


class LockfilePins:
    """The SHA-256 digests pinned by the lockfiles of one lockfile store, read on demand."""

    def __init__(
        self,
        directory: Path,
        *,
        protect_days: int,
        clock: Callable[[], float] = time.time,
        refresh: float = REFRESH,
    ) -> None:
        if protect_days <= 0:
            raise ValueError("protect_days must be positive")
        self._directory = directory
        self._window = protect_days * 86400.0
        self._clock = clock
        self._refresh = refresh
        self._lock = threading.Lock()
        self._read_at: float | None = None
        self._pinned: frozenset[str] = frozenset()

    def __call__(self) -> frozenset[str]:
        """The pinned digests, read afresh at most every ``refresh`` seconds."""
        with self._lock:
            now = self._clock()
            if self._read_at is None or not 0 <= now - self._read_at < self._refresh:
                self._pinned = self._read(now - self._window)
                self._read_at = now
            return self._pinned

    def _read(self, since: float) -> frozenset[str]:
        blobs = self._directory / "blobs"
        pinned: set[str] = set()
        read = failed = 0
        try:
            directories = sorted(blobs.iterdir())
        except FileNotFoundError:
            return frozenset()  # the worker has stored no lockfile yet
        except OSError as exc:
            _log.warning("cannot read the lockfile store %s, so nothing is pinned: %s", blobs, exc)
            return frozenset()
        for directory in directories:
            if not _HEX2.fullmatch(directory.name):
                continue
            try:
                files = sorted(directory.iterdir())
            except OSError:
                failed += 1
                continue
            for path in files:
                if not _HEX64.fullmatch(path.name) or not path.name.startswith(directory.name):
                    continue
                if read >= MAX_LOCKFILES:
                    _log.warning(
                        "the lockfile store holds more than %d lockfiles; the rest pin nothing",
                        MAX_LOCKFILES,
                    )
                    return frozenset(pinned)
                digests = _digests(path, since)
                if digests is None:
                    failed += 1
                    continue
                read += 1
                pinned.update(digests)
        if failed:
            _log.warning("%d lockfiles in %s could not be read and pin nothing", failed, blobs)
        return frozenset(pinned)


def _digests(path: Path, since: float) -> set[str] | None:
    """The file digests one lockfile lists; empty when it is older than ``since``, None
    when it cannot be read as a lockfile."""
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except OSError:
        return None
    try:
        found = os.fstat(descriptor)
        if not stat.S_ISREG(found.st_mode) or found.st_size > MAX_LOCKFILE_BYTES:
            return None
        if found.st_mtime < since:
            return set()
        with os.fdopen(descriptor, "rb") as source:
            descriptor = -1
            content = source.read(MAX_LOCKFILE_BYTES + 1)
    except OSError:
        return None
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if len(content) > MAX_LOCKFILE_BYTES:
        return None
    try:
        document: object = json.loads(content)
    except (ValueError, RecursionError):
        return None
    if not isinstance(document, dict):
        return None
    lockfile = cast(dict[str, object], document)
    if lockfile.get("format") != FORMAT:
        return None
    digests: set[str] = set()
    for section in _SECTIONS:
        entries = lockfile.get(section, [])
        if not isinstance(entries, list):
            return None
        for entry in cast(list[object], entries):
            if isinstance(entry, dict):
                digest = cast(dict[str, object], entry).get("sha256")
                if isinstance(digest, str) and _HEX64.fullmatch(digest):
                    digests.add(digest)
    return digests


def lockfile_pins(settings: Settings) -> LockfilePins | None:
    """The settings' lockfile pins, or None when no lockfile store is configured."""
    if settings.lockfile_dir is None:
        return None
    return LockfilePins(settings.lockfile_dir, protect_days=settings.protect_days)
