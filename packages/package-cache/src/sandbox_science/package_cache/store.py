# SPDX-License-Identifier: MIT
"""The cache's on-disk store: content-addressed blobs and an SQLite index of entries.

Layout below the cache directory:

- ``blobs/<first two hex digits>/<sha256>`` holds bodies named by their
  SHA-256, so identical files are stored once;
- ``index.sqlite3`` maps each cache key (upstream resource and response
  variant) to its blob, with the times and validators the cache needs;
- ``tmp/`` holds bodies being written; they are renamed into ``blobs/`` once
  complete and fsynced, so a blob is always whole;
- ``lock`` is held by the serving process.

A blob is renamed into place before its entry is committed, and an entry is
deleted before its blob, so a crash leaves at worst a blob that no entry
refers to. Recovery removes those and stray temp files. An entry whose blob
has gone missing is ignored and dropped.

The size cap counts the blobs that entries refer to (a shared blob once) plus
a fixed ``entry_overhead`` per entry, about what an index row costs, so that
entries sharing one small blob (negative answers) cannot grow the index
without limit. Eviction first purges negative entries older than
``negative_expiry``, which the cache no longer serves, then removes the least
recently accessed entries, skipping those whose blob an environment's
lockfile pins (``pins``). Pinned blobs are protected in rank order up to
``protect_share`` of the cap; when they hold more, the store warns and evicts
the excess, oldest pinned first, before anything else.

The store is synchronous and thread-safe; the web application calls it from
worker threads.
"""

from __future__ import annotations

import fcntl
import hashlib
import logging
import os
import re
import shutil
import sqlite3
import stat
import tempfile
import threading
import time
from collections.abc import Callable, Generator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import BinaryIO, Final, Self

from .adapter import Kind
from .errors import StoreError

ACCESS_INTERVAL: Final = 60.0
"""Seconds between access-time updates of an entry; hits in between write nothing."""

EVICT_TO: Final = 0.9
"""Eviction removes entries until the store holds at most this fraction of its cap."""

ORPHAN_GRACE: Final = 3600.0
"""How old a blob without an entry must be before ``prune`` deletes it, since a
running cache may be about to commit an entry for a blob it has just stored."""

PROTECT_SHARE: Final = 0.5
"""The share of the cap that pinned blobs may hold protected."""

ENTRY_OVERHEAD: Final = 1024
"""Bytes each entry counts toward the size cap besides its blob: about what its index row,
with its key and the indexes over it, takes in ``index.sqlite3``."""

_SCHEMA_VERSION: Final = 1
PinRank = tuple[int, float]
"""A pinned blob's rank, ``(tier, recency)``: higher is protected first (``pins``)."""
_HEX64: Final = re.compile(r"[0-9a-f]{64}")
_HEX2: Final = re.compile(r"[0-9a-f]{2}")
_EVICT_BATCH: Final = 256
_SCHEMA: Final = """
CREATE TABLE IF NOT EXISTS entries (
    key TEXT PRIMARY KEY NOT NULL,
    sha256 TEXT NOT NULL,
    size INTEGER NOT NULL,
    kind TEXT NOT NULL,
    content_type TEXT NOT NULL,
    fetched REAL NOT NULL,
    accessed REAL NOT NULL,
    etag TEXT,
    last_modified TEXT,
    status INTEGER NOT NULL,
    immutable INTEGER NOT NULL,
    digest TEXT
);
CREATE INDEX IF NOT EXISTS entries_by_access ON entries (accessed);
CREATE INDEX IF NOT EXISTS entries_by_blob ON entries (sha256);
CREATE INDEX IF NOT EXISTS entries_negative ON entries (fetched) WHERE status != 200;
"""
_SELECT_BY_KEY: Final = """
SELECT key, sha256, size, kind, content_type, fetched, accessed, etag, last_modified,
       status, immutable, digest
FROM entries WHERE key = ?
"""
_UPSERT: Final = """
INSERT OR REPLACE INTO entries (
    key, sha256, size, kind, content_type, fetched, accessed, etag, last_modified,
    status, immutable, digest
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""
_TOTAL: Final = """
SELECT COALESCE(SUM(size), 0) FROM (SELECT MAX(size) AS size FROM entries GROUP BY sha256)
"""
_COUNT: Final = "SELECT COUNT(*) FROM entries"
# The term "status != 200" lets SQLite use the partial index entries_negative.
_EXPIRED_NEGATIVE: Final = """
SELECT key, sha256, size FROM entries WHERE status != 200 AND fetched < ? LIMIT ?
"""
# temp.pinned holds the digests the pins named, with their ranks, at the start of an
# eviction or count.
_PINNED_TABLE: Final = (
    "CREATE TEMP TABLE IF NOT EXISTS pinned "
    "(sha256 TEXT PRIMARY KEY, tier INTEGER NOT NULL, recency REAL NOT NULL)"
)
# The stored pinned blobs, best ranked first: by rank, then the most recently accessed.
_PINNED_BLOBS: Final = """
SELECT p.sha256, MAX(e.size), MAX(e.accessed) AS accessed
FROM temp.pinned AS p JOIN entries AS e ON e.sha256 = p.sha256
GROUP BY p.sha256 ORDER BY p.tier DESC, p.recency DESC, accessed DESC, p.sha256
"""
_LEAST_RECENT_UNPINNED: Final = """
SELECT key, sha256, size FROM entries WHERE sha256 NOT IN (SELECT sha256 FROM temp.pinned)
ORDER BY accessed, key LIMIT ?
"""

_log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class Entry:
    """One cache key's stored response."""

    key: str
    sha256: str
    """The blob holding the body."""
    size: int
    kind: Kind
    content_type: str
    """The content type upstream sent."""
    fetched: float
    """When upstream last sent or confirmed the body (Unix time)."""
    accessed: float
    """When the entry was last served, to within ``ACCESS_INTERVAL``."""
    etag: str | None = None
    last_modified: str | None = None
    status: int = 200
    """Upstream's status: 200, or 404 or 410 for a negative entry."""
    immutable: bool = False
    digest: str | None = None
    """The digest the upstream body was verified against, as ``<algorithm>:<hex>``."""


@dataclass(frozen=True, slots=True)
class TempBlob:
    """A complete, fsynced body in the store's temp directory, ready for ``Store.put``."""

    path: Path
    sha256: str
    size: int


@dataclass(frozen=True, slots=True)
class StoreStats:
    """How much the store holds."""

    entries: int
    bytes: int
    """Bytes in the blobs that entries refer to; a shared blob counts once."""
    pinned_bytes: int = 0
    """Of those, bytes in blobs that environments' lockfiles pin."""
    protected_bytes: int = 0
    """Of the pinned bytes, those within ``protect_limit``, which eviction keeps."""


@dataclass(frozen=True, slots=True)
class Removal:
    """What an eviction or cleanup removed."""

    entries: int = 0
    blobs: int = 0
    bytes: int = 0

    def __add__(self, other: Removal) -> Removal:
        return Removal(
            self.entries + other.entries, self.blobs + other.blobs, self.bytes + other.bytes
        )


class Store:
    """Blobs and their index below one cache directory."""

    def __init__(
        self,
        root: Path,
        *,
        max_size: int,
        clock: Callable[[], float] = time.time,
        access_interval: float = ACCESS_INTERVAL,
        entry_overhead: int = ENTRY_OVERHEAD,
        negative_expiry: float | None = None,
        pins: Callable[[], Mapping[str, PinRank]] | None = None,
        protect_share: float = PROTECT_SHARE,
    ) -> None:
        """Open (creating if need be) the store in ``root``.

        ``entry_overhead`` is what each entry counts toward ``max_size``
        besides its blob. ``negative_expiry`` is the age in seconds after which
        eviction purges a negative (404/410) entry; None leaves them to the
        least-recently-used order. ``pins`` returns the SHA-256 digests of the
        blobs that eviction protects, with their ranks (``pins.LockfilePins``);
        None pins nothing. Pinned blobs are protected, best ranked first, up to
        ``protect_share`` of ``max_size``.
        """
        if max_size <= 0:
            raise ValueError("max_size must be positive")
        if entry_overhead < 0:
            raise ValueError("entry_overhead must not be negative")
        if negative_expiry is not None and negative_expiry <= 0:
            raise ValueError("negative_expiry must be positive")
        if not 0 < protect_share <= 1:
            raise ValueError("protect_share must be above 0 and at most 1")
        self._root = root
        self._blobs = root / "blobs"
        self._temp = root / "tmp"
        self._max_size = max_size
        self._clock = clock
        self._access_interval = access_interval
        self._entry_overhead = entry_overhead
        self._negative_expiry = negative_expiry
        self._pins = pins
        self._protect_limit = int(max_size * protect_share)
        self._bytes = 0
        self._entries = 0
        self._lock = threading.RLock()
        self._lock_fd: int | None = None
        try:
            for directory in (root, self._blobs, self._temp):
                directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            self._db = sqlite3.connect(
                root / "index.sqlite3",
                timeout=30.0,
                isolation_level=None,
                check_same_thread=False,
            )
        except (OSError, sqlite3.Error) as exc:
            raise StoreError(f"cannot open the store in {root}: {exc}") from exc
        try:
            with self._errors("open the index"):
                self._initialize()
                self._recount()
        except BaseException:
            self._db.close()
            raise

    def _initialize(self) -> None:
        self._db.execute("PRAGMA journal_mode = WAL")
        self._db.execute("PRAGMA synchronous = NORMAL")
        (version,) = self._db.execute("PRAGMA user_version").fetchone()
        if version not in (0, _SCHEMA_VERSION):
            raise StoreError(
                f"{self._root} has index version {version}, expected {_SCHEMA_VERSION}"
            )
        self._db.executescript(_SCHEMA)
        self._db.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")

    @property
    def root(self) -> Path:
        """The cache directory."""
        return self._root

    @property
    def temp_dir(self) -> Path:
        """Where bodies are written before ``put`` moves them into the store."""
        return self._temp

    @property
    def max_size(self) -> int:
        """The size cap in bytes."""
        return self._max_size

    @property
    def total_size(self) -> int:
        """Bytes in referenced blobs, as tracked by this process since the last recount."""
        return self._bytes

    @property
    def charged_size(self) -> int:
        """What counts toward ``max_size``: ``total_size`` plus the overhead of every entry."""
        return self._bytes + self._entries * self._entry_overhead

    def close(self) -> None:
        """Close the index and release the directory lock, if held."""
        with self._lock:
            self._db.close()
            if self._lock_fd is not None:
                os.close(self._lock_fd)
                self._lock_fd = None

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def blob_path(self, sha256: str) -> Path:
        """Where the blob with this SHA-256 is stored."""
        if not _HEX64.fullmatch(sha256):
            raise ValueError(f"{sha256!r} is not a hex SHA-256")
        return self._blobs / sha256[:2] / sha256

    def recover(self) -> Removal:
        """Claim the cache directory and clean up after an unclean shutdown.

        Takes an exclusive lock held until ``close`` (StoreError if another
        process holds it), removes stray temp files and removes blobs no entry
        refers to.
        """
        with self._lock:
            self._claim()
            try:
                for path in self._temp.iterdir():
                    if path.is_dir() and not path.is_symlink():
                        shutil.rmtree(path)
                    else:
                        path.unlink(missing_ok=True)
            except OSError as exc:
                raise StoreError(f"cannot clear {self._temp}: {exc}") from exc
            return self.remove_orphans()

    def _claim(self) -> None:
        if self._lock_fd is not None:
            return
        path = self._root / "lock"
        try:
            fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
        except OSError as exc:
            raise StoreError(f"cannot open {path}: {exc}") from exc
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            os.close(fd)
            if isinstance(exc, BlockingIOError):
                raise StoreError(f"{self._root} is in use by another package cache") from exc
            raise StoreError(f"cannot lock {path}: {exc}") from exc
        self._lock_fd = fd

    def get(self, key: str) -> Entry | None:
        """Return a key's entry as stored, without checking its blob or recording an access."""
        with self._lock, self._errors("read an entry"):
            row = self._db.execute(_SELECT_BY_KEY, (key,)).fetchone()
        return None if row is None else _entry(row)

    def keys(self) -> list[str]:
        """All keys, least recently accessed first."""
        with self._lock, self._errors("list entries"):
            rows = self._db.execute("SELECT key FROM entries ORDER BY accessed, key").fetchall()
        return [str(row[0]) for row in rows]

    def lookup(self, key: str) -> tuple[Entry, BinaryIO] | None:
        """Return a key's entry and its open blob, and record the access.

        Returns None when there is no entry, or when its blob is missing or does
        not have the recorded size; such an entry is dropped. The caller closes
        the file.
        """
        with self._lock, self._errors("look up an entry"):
            row = self._db.execute(_SELECT_BY_KEY, (key,)).fetchone()
            if row is None:
                return None
            entry = _entry(row)
            if entry is None:
                _log.warning("dropping malformed index entry %r", key)
                cursor = self._db.execute("DELETE FROM entries WHERE key = ?", (key,))
                self._entries = max(self._entries - cursor.rowcount, 0)
                return None
            file = self._open(entry)
            if file is None:
                _log.warning("dropping entry %r: its blob is missing or damaged", key)
                self._delete_entries([(entry.key, entry.sha256, entry.size)])
                return None
            try:
                now = self._clock()
                if not 0 <= now - entry.accessed < self._access_interval:
                    self._db.execute("UPDATE entries SET accessed = ? WHERE key = ?", (now, key))
                    entry = replace(entry, accessed=now)
            except BaseException:
                file.close()
                raise
            return entry, file

    def open_blob(self, entry: Entry) -> BinaryIO | None:
        """Open an entry's blob, or return None (dropping the entry) if it is gone."""
        with self._lock, self._errors("open a blob"):
            file = self._open(entry)
            if file is None:
                self._delete_entries([(entry.key, entry.sha256, entry.size)])
            return file

    def _open(self, entry: Entry) -> BinaryIO | None:
        try:
            file = self.blob_path(entry.sha256).open("rb")
        except FileNotFoundError:
            return None
        try:
            size = os.fstat(file.fileno()).st_size
        except BaseException:
            file.close()
            raise
        if size != entry.size:
            file.close()
            return None
        return file

    def write_temp(self, data: bytes) -> TempBlob:
        """Write a body to a new temp file and fsync it, ready for ``put``."""
        try:
            fd, name = tempfile.mkstemp(dir=self._temp, prefix="body-")
            path = Path(name)
            try:
                with os.fdopen(fd, "wb") as handle:
                    handle.write(data)
                    handle.flush()
                    os.fsync(handle.fileno())
            except BaseException:
                path.unlink(missing_ok=True)
                raise
        except OSError as exc:
            raise StoreError(f"cannot write a temp file in {self._temp}: {exc}") from exc
        return TempBlob(path, hashlib.sha256(data).hexdigest(), len(data))

    def put(
        self,
        key: str,
        blob: TempBlob,
        *,
        kind: Kind,
        content_type: str,
        status: int = 200,
        immutable: bool = False,
        etag: str | None = None,
        last_modified: str | None = None,
        digest: str | None = None,
    ) -> Entry:
        """Move a complete body into the store and point ``key`` at it.

        The temp file is consumed: moved into ``blobs/``, or deleted when an
        identical blob is already stored or the put fails. The key's previous
        blob is deleted if no other entry refers to it.
        """
        target = self.blob_path(blob.sha256)
        if blob.path.parent != self._temp:
            raise ValueError(f"{blob.path} is not in the store's temp directory")
        now = self._clock()
        entry = Entry(
            key=key,
            sha256=blob.sha256,
            size=blob.size,
            kind=kind,
            content_type=content_type,
            fetched=now,
            accessed=now,
            etag=etag,
            last_modified=last_modified,
            status=status,
            immutable=immutable,
            digest=digest,
        )
        referenced = True
        added = False
        freed: tuple[str, int] | None = None
        with self._lock:
            try:
                with self._errors("store a body"):
                    self._place(blob, target)
                    with self._transaction() as db:
                        referenced = self._referenced(blob.sha256)
                        old = db.execute(
                            "SELECT sha256, size FROM entries WHERE key = ?", (key,)
                        ).fetchone()
                        db.execute(_UPSERT, _row(entry))
                        added = old is None
                        if (
                            old is not None
                            and old[0] != blob.sha256
                            and not self._referenced(old[0])
                        ):
                            freed = (str(old[0]), int(old[1]))
            finally:
                # A no-op once the body has been moved into blobs/.
                blob.path.unlink(missing_ok=True)
            if not referenced:
                self._bytes += blob.size
            if added:
                self._entries += 1
            if freed is not None:
                self._unlink_blob(freed[0])
                self._bytes = max(self._bytes - freed[1], 0)
        return entry

    def _place(self, blob: TempBlob, target: Path) -> None:
        try:
            existing = target.stat().st_size
        except FileNotFoundError:
            existing = None
        if existing == blob.size:
            # Same name and size: the same body. Refresh its age so that a
            # concurrent prune does not take it for an orphan.
            os.utime(target)
            return
        target.parent.mkdir(mode=0o700, exist_ok=True)
        os.replace(blob.path, target)

    def refresh(self, entry: Entry, *, etag: str | None, last_modified: str | None) -> Entry | None:
        """Record that upstream confirmed an entry's body (a 304), with its validators.

        Returns the updated entry, or None if the key no longer holds that body.
        """
        now = self._clock()
        with self._lock, self._errors("refresh an entry"):
            cursor = self._db.execute(
                "UPDATE entries SET fetched = ?, accessed = ?, etag = ?, last_modified = ? "
                "WHERE key = ? AND sha256 = ?",
                (now, now, etag, last_modified, entry.key, entry.sha256),
            )
            if cursor.rowcount != 1:
                return None
        return replace(entry, fetched=now, accessed=now, etag=etag, last_modified=last_modified)

    def forget(self, key: str) -> Removal:
        """Delete a key's entry, and its blob if no other entry refers to it.

        The next request for the key fetches it again. Removes nothing when
        there is no such entry.
        """
        with self._lock, self._errors("forget an entry"):
            row = self._db.execute(
                "SELECT key, sha256, size FROM entries WHERE key = ?", (key,)
            ).fetchone()
            if row is None:
                return Removal()
            return self._delete_entries([(str(row[0]), str(row[1]), int(row[2]))])

    def over_capacity(self) -> bool:
        """Whether the store counts more than ``max_size`` bytes, as tracked by this process."""
        return self.charged_size > self._max_size

    @property
    def protect_limit(self) -> int:
        """The bytes of pinned blobs eviction protects at most: ``protect_share`` of the cap."""
        return self._protect_limit

    def evict(self, *, include_pinned: bool = False) -> Removal:
        """Purge expired negative entries, then bring the store under its cap.

        Negative entries older than ``negative_expiry`` go first: the cache no
        longer serves them, not even as a stale fallback. Then, if the store
        counts more than ``max_size`` bytes (``charged_size``), pinned blobs
        beyond ``protect_limit`` go, oldest pinned first, with a warning; then the
        least recently accessed unpinned entries until the store counts at most
        ``EVICT_TO`` of the cap. ``include_pinned`` evicts by access alone. The
        pins are read once per call. A blob is deleted once no entry refers to it.
        """
        target = int(self._max_size * EVICT_TO)
        with self._lock, self._errors("evict entries"):
            self._recount()
        removed = self._purge_negative()
        with self._lock:
            if self.charged_size <= self._max_size:
                return removed
        pinned: Mapping[str, PinRank] = {} if include_pinned else self._pinned()
        with self._lock, self._errors("evict entries"):
            self._load_pins(pinned)
            protected, overflow = self._split_pins()
            if overflow:
                excess = sum(size for _, size in overflow)
                _log.warning(
                    "pinned files hold %d bytes, more than the protection ceiling of %d "
                    "(protect_share of max_size): evicting the %d bytes pinned longest ago",
                    protected + excess,
                    self._protect_limit,
                    excess,
                )
            for sha256, _ in overflow:
                rows = self._db.execute(
                    "SELECT key, sha256, size FROM entries WHERE sha256 = ?", (sha256,)
                ).fetchall()
                removed += self._delete_entries(rows)
        while True:
            with self._lock, self._errors("evict entries"):
                if self.charged_size <= target:
                    return removed
                rows = self._db.execute(_LEAST_RECENT_UNPINNED, (_EVICT_BATCH,)).fetchall()
                if not rows:
                    return removed
                removed += self._delete_entries(rows, stop_at=target)

    def _pinned(self) -> Mapping[str, PinRank]:
        """The digests the pins name now; outside the store's lock, since reading them
        reads files."""
        return {} if self._pins is None else self._pins()

    def _load_pins(self, pinned: Mapping[str, PinRank]) -> None:
        """Put ``pinned`` in ``temp.pinned``. Call with the lock held."""
        self._db.execute(_PINNED_TABLE)
        # A deferred transaction: it writes the connection's temp table only, so it takes
        # no lock on the index that a serving cache or another command would wait for.
        self._db.execute("BEGIN")
        try:
            self._db.execute("DELETE FROM temp.pinned")
            self._db.executemany(
                "INSERT OR IGNORE INTO temp.pinned (sha256, tier, recency) VALUES (?, ?, ?)",
                ((digest, tier, recency) for digest, (tier, recency) in pinned.items()),
            )
        except BaseException:
            self._db.execute("ROLLBACK")
            raise
        self._db.execute("COMMIT")

    def _split_pins(self) -> tuple[int, list[tuple[str, int]]]:
        """The bytes of the stored pinned blobs within ``protect_limit``, best ranked
        first, and the blobs past it, oldest pinned first. Call with the lock held."""
        protected = 0
        overflow: list[tuple[str, int]] = []
        for sha256, size, _ in self._db.execute(_PINNED_BLOBS).fetchall():
            if not overflow and protected + int(size) <= self._protect_limit:
                protected += int(size)
            else:
                overflow.append((str(sha256), int(size)))
        overflow.reverse()
        return protected, overflow

    def _purge_negative(self) -> Removal:
        """Delete the negative entries older than ``negative_expiry``, a batch at a time."""
        removed = Removal()
        if self._negative_expiry is None:
            return removed
        cutoff = self._clock() - self._negative_expiry
        while True:
            with self._lock, self._errors("purge negative entries"):
                rows = self._db.execute(_EXPIRED_NEGATIVE, (cutoff, _EVICT_BATCH)).fetchall()
                if not rows:
                    return removed
                removed += self._delete_entries(rows)

    def remove_orphans(self, *, min_age: float = 0.0) -> Removal:
        """Delete blobs that no entry refers to and that are at least ``min_age`` seconds old.

        Files in ``blobs/`` that the store would not have written are left alone.
        """
        blobs = 0
        freed = 0
        with self._lock, self._errors("remove unreferenced blobs"):
            referenced = {
                str(row[0]) for row in self._db.execute("SELECT DISTINCT sha256 FROM entries")
            }
            now = time.time()
            for directory in self._blobs.iterdir():
                if not _HEX2.fullmatch(directory.name) or not directory.is_dir():
                    continue
                for path in directory.iterdir():
                    name = path.name
                    if (
                        not _HEX64.fullmatch(name)
                        or not name.startswith(directory.name)
                        or name in referenced
                    ):
                        continue
                    try:
                        info = path.lstat()
                    except FileNotFoundError:
                        continue
                    if not stat.S_ISREG(info.st_mode) or now - info.st_mtime < min_age:
                        continue
                    path.unlink(missing_ok=True)
                    blobs += 1
                    freed += info.st_size
        return Removal(blobs=blobs, bytes=freed)

    def stats(self) -> StoreStats:
        """Count the entries, the bytes their blobs hold and the bytes pins name and protect
        (at most ``protect_limit``)."""
        pinned = self._pinned()
        with self._lock, self._errors("count entries"):
            self._recount()
            protected = overflow = 0
            if pinned:
                self._load_pins(pinned)
                protected, excess = self._split_pins()
                overflow = sum(size for _, size in excess)
            return StoreStats(
                entries=self._entries,
                bytes=self._bytes,
                pinned_bytes=protected + overflow,
                protected_bytes=protected,
            )

    def _recount(self) -> None:
        """Count the entries and blob bytes afresh. Call with the lock held."""
        (total,) = self._db.execute(_TOTAL).fetchone()
        (entries,) = self._db.execute(_COUNT).fetchone()
        self._bytes = int(total)
        self._entries = int(entries)

    def _referenced(self, sha256: str) -> bool:
        row = self._db.execute("SELECT 1 FROM entries WHERE sha256 = ? LIMIT 1", (sha256,))
        return row.fetchone() is not None

    def _delete_entries(
        self, rows: Sequence[tuple[str, str, int]], *, stop_at: int | None = None
    ) -> Removal:
        """Delete entries, then the blobs they leave unreferenced. Call with the lock held.

        With ``stop_at``, stop as soon as the store counts at most that many bytes.
        """
        freed: dict[str, int] = {}
        deleted = 0
        charged = self.charged_size
        with self._transaction() as db:
            for key, sha256, size in rows:
                if stop_at is not None and charged <= stop_at:
                    break
                cursor = db.execute(
                    "DELETE FROM entries WHERE key = ? AND sha256 = ?", (key, sha256)
                )
                deleted += cursor.rowcount
                charged -= cursor.rowcount * self._entry_overhead
                if sha256 not in freed and not self._referenced(sha256):
                    freed[sha256] = size
                    charged -= size
        self._entries = max(self._entries - deleted, 0)
        self._bytes = max(self._bytes - sum(freed.values()), 0)
        for sha256 in freed:
            self._unlink_blob(sha256)
        return Removal(entries=deleted, blobs=len(freed), bytes=sum(freed.values()))

    def _unlink_blob(self, sha256: str) -> None:
        if not _HEX64.fullmatch(sha256):
            return  # a malformed index row: there is no blob of that name
        try:
            self.blob_path(sha256).unlink(missing_ok=True)
        except OSError as exc:
            # The entry is already gone; the blob is an orphan that recovery or prune removes.
            _log.warning("cannot delete blob %s: %s", sha256, exc)

    @contextmanager
    def _transaction(self) -> Generator[sqlite3.Connection, None, None]:
        self._db.execute("BEGIN IMMEDIATE")
        try:
            yield self._db
        except BaseException:
            self._db.execute("ROLLBACK")
            raise
        self._db.execute("COMMIT")

    @contextmanager
    def _errors(self, action: str) -> Generator[None, None, None]:
        try:
            yield
        except (OSError, sqlite3.Error) as exc:
            raise StoreError(f"cannot {action} in {self._root}: {exc}") from exc


def _row(entry: Entry) -> tuple[object, ...]:
    return (
        entry.key,
        entry.sha256,
        entry.size,
        entry.kind.value,
        entry.content_type,
        entry.fetched,
        entry.accessed,
        entry.etag,
        entry.last_modified,
        entry.status,
        int(entry.immutable),
        entry.digest,
    )


def _entry(row: Sequence[object]) -> Entry | None:
    """Build an entry from an index row, or return None if the row is malformed."""
    try:
        entry = Entry(
            key=_text(row[0]),
            sha256=_text(row[1]),
            size=_integer(row[2]),
            kind=Kind(_text(row[3])),
            content_type=_text(row[4]),
            fetched=_number(row[5]),
            accessed=_number(row[6]),
            etag=_optional_text(row[7]),
            last_modified=_optional_text(row[8]),
            status=_integer(row[9]),
            immutable=bool(_integer(row[10])),
            digest=_optional_text(row[11]),
        )
    except (ValueError, IndexError):
        return None
    if not _HEX64.fullmatch(entry.sha256) or entry.size < 0:
        return None
    return entry


def _text(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError(f"expected text, not {type(value).__name__}")
    return value


def _optional_text(value: object) -> str | None:
    return None if value is None else _text(value)


def _integer(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"expected an integer, not {type(value).__name__}")
    return value


def _number(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError(f"expected a number, not {type(value).__name__}")
    return float(value)
