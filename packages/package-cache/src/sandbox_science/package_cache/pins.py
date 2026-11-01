# SPDX-License-Identifier: MIT
"""Package files that environments' lockfiles pin, which eviction protects up to a ceiling.

The worker records each environment's current revision (``environment_revisions``), the
last revision known good of one that needs package recovery (``package_status``, CP-13)
and every kept revision with its lockfile (``revision_entries``) in
``<worker state_dir>/worker.sqlite3``, and keeps the lockfiles themselves, by the SHA-256
of their JSON, in ``<worker state_dir>/lockfiles/blobs/<ab>/<sha256>`` (docs/WORKER.md,
"Package commands"). The cache runs in the worker's Pod, on the same volume (D101); it
opens that database read-only and never writes it.

A lockfile pins while it is an environment's current or known-good revision's, and for
``protect_days`` after it stopped being current (when the environment's next revision was
recorded). A pinned lockfile pins the SHA-256 of every Conda package and pip archive it
lists: the digests of the files themselves, so they name the cache's blobs directly. A
pip distribution installed from an index records none and is not pinned.

Each pinned digest carries a rank, ``(tier, recency)``: tier 1 for a current or
known-good lockfile, ranked by when that revision was recorded, tier 0 for one in its
grace period, ranked by when it stopped being current. The store protects pinned blobs
in rank order up to its ceiling (``protect_share`` of ``max_size``) and evicts the rest
oldest-pinned first, since the lockfiles are written from what an untrusted sandbox
reports and must not be able to hold the volume.

Reading is bounded (``MAX_LOCKFILES`` lockfiles, the best ranked first, of at most
``MAX_LOCKFILE_BYTES`` each) and reused for ``REFRESH`` seconds. A lockfile that cannot be
read or parsed pins nothing, and records that cannot be read pin nothing (each logged), so
that eviction can still keep the disk bounded.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import stat
import threading
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Final, cast

from sandbox_science.contracts.revisions import MAX_LOCKFILE_BYTES

from .settings import Settings
from .store import PinRank

MAX_LOCKFILES: Final = 10_000
"""Lockfiles read at most per refresh, the best ranked first; the worker keeps 50 per
environment."""
REFRESH: Final = 60.0
"""Seconds a reading of the worker's records is reused."""
FORMAT: Final = "sandbox-science-lockfile"
CURRENT: Final = 1
GRACE: Final = 0
_SECTIONS: Final = ("conda", "pip")
_HEX64: Final = re.compile(r"[0-9a-f]{64}")
# Every kept revision with a lockfile; current and known-good ones have tier 1 and their
# own recording time, the others tier 0 and the time their environment's next revision
# was recorded (when they stopped being current), or NULL when there is none.
_LOCKFILES: Final = """
SELECT e.lockfile,
       CASE WHEN e.revision = r.revision OR e.revision = s.revision THEN 1 ELSE 0 END,
       CASE WHEN e.revision = r.revision OR e.revision = s.revision THEN e.recorded
            ELSE (SELECT MIN(n.recorded) FROM revision_entries AS n
                  WHERE n.environment_id = e.environment_id AND n.revision > e.revision)
       END
FROM revision_entries AS e
LEFT JOIN environment_revisions AS r ON r.environment_id = e.environment_id
LEFT JOIN package_status AS s ON s.environment_id = e.environment_id
WHERE e.lockfile IS NOT NULL
"""

_log = logging.getLogger(__name__)


class LockfilePins:
    """The ranked digests that one worker's environments' lockfiles pin, read on demand."""

    def __init__(
        self,
        state_dir: Path,
        *,
        protect_days: float,
        clock: Callable[[], float] = time.time,
        refresh: float = REFRESH,
        max_lockfiles: int = MAX_LOCKFILES,
    ) -> None:
        if protect_days < 0:
            raise ValueError("protect_days must not be negative")
        self._database = state_dir / "worker.sqlite3"
        self._blobs = state_dir / "lockfiles" / "blobs"
        self._grace = protect_days * 86400.0
        self._clock = clock
        self._refresh = refresh
        self._max_lockfiles = max_lockfiles
        self._lock = threading.Lock()
        self._read_at: float | None = None
        self._pinned: Mapping[str, PinRank] = {}

    def __call__(self) -> Mapping[str, PinRank]:
        """The pinned digests and their ranks, read afresh at most every ``refresh`` s."""
        with self._lock:
            now = self._clock()
            if self._read_at is None or not 0 <= now - self._read_at < self._refresh:
                self._pinned = self._read(now)
                self._read_at = now
            return self._pinned

    def _read(self, now: float) -> dict[str, PinRank]:
        lockfiles = self._lockfiles(now)
        if not lockfiles:
            return {}
        if not self._blobs.is_dir():
            _log.warning(
                "the worker's lockfile store %s is missing or unreadable: nothing is pinned",
                self._blobs,
            )
            return {}
        ranked = sorted(lockfiles.items(), key=lambda item: item[1], reverse=True)
        if len(ranked) > self._max_lockfiles:
            _log.warning(
                "%d lockfiles pin files; only the %d best ranked are read",
                len(ranked),
                self._max_lockfiles,
            )
        pinned: dict[str, PinRank] = {}
        failed = 0
        for sha256, rank in ranked[: self._max_lockfiles]:
            digests = _digests(self._blobs / sha256[:2] / sha256)
            if digests is None:
                failed += 1
                continue
            for digest in digests:
                if digest not in pinned or pinned[digest] < rank:
                    pinned[digest] = rank
        if failed:
            _log.warning(
                "%d lockfiles in %s could not be read and pin nothing", failed, self._blobs
            )
        return pinned

    def _lockfiles(self, now: float) -> dict[str, PinRank] | None:
        """Each pinning lockfile and its best rank; None when the records cannot be read."""
        try:
            if not self._database.is_file():
                raise sqlite3.OperationalError("no such file")
            database = sqlite3.connect(f"{self._database.as_uri()}?mode=ro", uri=True, timeout=5.0)
        except (OSError, sqlite3.Error) as exc:
            _log.warning(
                "cannot read the worker's records %s, so nothing is pinned: %s",
                self._database,
                exc,
            )
            return None
        try:
            rows = database.execute(_LOCKFILES).fetchall()
        except sqlite3.Error as exc:
            _log.warning(
                "cannot read the worker's records %s, so nothing is pinned: %s",
                self._database,
                exc,
            )
            return None
        finally:
            database.close()
        lockfiles: dict[str, PinRank] = {}
        for sha256, tier, recency in rows:
            if not isinstance(sha256, str) or not _HEX64.fullmatch(sha256):
                continue
            if not isinstance(recency, int | float):
                continue
            rank = (CURRENT if tier == CURRENT else GRACE, float(recency))
            if rank[0] == GRACE and now - rank[1] > self._grace:
                continue
            if sha256 not in lockfiles or lockfiles[sha256] < rank:
                lockfiles[sha256] = rank
        return lockfiles


def _digests(path: Path) -> set[str] | None:
    """The file digests one lockfile lists; None when it cannot be read as a lockfile."""
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except OSError:
        return None
    try:
        found = os.fstat(descriptor)
        if not stat.S_ISREG(found.st_mode) or found.st_size > MAX_LOCKFILE_BYTES:
            return None
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
    """The settings' lockfile pins, or None when no worker state directory is configured."""
    if settings.worker_state_dir is None:
        return None
    return LockfilePins(settings.worker_state_dir, protect_days=settings.protect_days)
