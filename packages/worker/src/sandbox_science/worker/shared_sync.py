# SPDX-License-Identifier: MIT
"""Shared project data across a project's environments (D79).

Each environment is its own sandbox (a VM with OpenShell's VM driver), so the ``shared/``
area a project's sessions write is a separate directory in each. The worker keeps the
project's copy of every shared file, in a blob store of the project's own, and brings
each environment's ``shared/`` in step with it:

- ``up``: after a tool call in an environment, the files that changed there since its
  last sync are copied into the project's copy, and the files deleted there are deleted;
- ``down``: before a tool call, the environment's own changes go up first, then the
  files whose project version is newer than the environment's copy are written into it.

Every change to a project's copy takes the next value of the project's counter as its
version. For each file in an environment, the worker records the stamp (modification
time, size and inode) and SHA-256 its copy had when last synced, and the version it
matched. A change an environment made to a file whose project version moved on
meanwhile is a conflict: the project keeps its version, the environment's is kept
beside it as a conflict copy named after the environment, and the agent is told. A
deletion that meets a change loses to it.

Research code controls ``shared/``, so the worker trusts nothing an executor reports.
Paths, stamps and digests are checked; content is read from the environment and kept
only if the bytes received hash to the digest, unless the same project already holds
that content; and a scan with unreadable parts, or one cut short, proves no file
missing. A mark the worker writes beside ``shared/`` identifies the directory it
synced, so a new sandbox under an old ID starts afresh rather than looking as if
everything in it was deleted. An environment's ``shared/`` is synced for the first
project that uses it, and no other.

Stamps decide which files to hash, so a call that changes nothing in ``shared/`` costs a
scan before it and one after. Files modified within two seconds of a scan are hashed
again at the next, since a clock coarser than a rewrite could hide one. Files over
``max_file_bytes``, and changes that would take the project's copy past
``max_project_bytes`` or ``max_project_files``, stay where they are, and the agent is
told once. Blobs no shared file uses any more are deleted.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import logging
import posixpath
import re
import secrets
import sqlite3
import threading
import time
import weakref
from collections import OrderedDict, deque
from collections.abc import AsyncGenerator, Collection, Generator, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final, Protocol, cast

from sandbox_science.contracts import ErrorKind

from .artifacts import Blobs
from .errors import WorkerError

_log = logging.getLogger(__name__)

MAX_FILE_BYTES: Final = 32 * 1024 * 1024
"""The largest file copied between environments: a write to the executor is one line."""
MAX_PROJECT_BYTES: Final = 2 * 1024 * 1024 * 1024
"""The most a project's copy of its shared files may hold."""
MAX_PROJECT_FILES: Final = 10_000
"""The most files a project's copy may hold; an environment's scan reports up to 20,000."""
HASH_BUDGET: Final = 256 * 1024 * 1024
"""The bytes one scan hashes; files past it are hashed at a later scan."""
READ_BYTES: Final = 8 * 1024 * 1024
RACY_NS: Final = 2_000_000_000
"""Files modified this close to a scan are hashed again at the next one."""
FAILURE_BACKOFF: Final = 600.0
"""Seconds before a file that could not be written into an environment is tried again."""
MAX_NOTICES: Final = 10
"""The most notices one sync step adds to a result."""
MAX_REMEMBERED: Final = 10_000
"""Bounds the notices told once and the files remembered as failing or too large."""
MAX_WROTE: Final = 1000
SHARED: Final = "shared/"
_DIGEST: Final = re.compile(r"[0-9a-f]{64}")
_MAX_PATH_BYTES: Final = 4096
_MAX_INT: Final = 2**63 - 1
_FILE_ERRORS: Final = frozenset(
    {ErrorKind.CONFLICT, ErrorKind.NOT_FOUND, ErrorKind.INVALID_REQUEST, ErrorKind.POLICY_DENIED}
)
"""Errors about one file. Any other means the executor failed, and the sync stops."""

Stamp = tuple[int, int, int]
"""A file's modification time in nanoseconds, size and inode, as the executor scans them."""
_STALE: Final[Stamp] = (-1, -1, -1)
"""The stamp of a copy whose content must be hashed again before it is trusted."""


class Channel(Protocol):
    async def request(self, op: str, **fields: Any) -> dict[str, Any]: ...


@dataclass(frozen=True, slots=True)
class ProjectFile:
    """A file in the project's copy of ``shared/``."""

    path: str
    version: int
    sha256: str
    size: int
    executable: bool = False


@dataclass(frozen=True, slots=True)
class Copy:
    """An environment's copy of a shared file as of its last sync."""

    sha256: str
    version: int
    """The project version it matched; lower than the project's when it is out of date."""
    stamp: Stamp


@dataclass(frozen=True, slots=True)
class Claim:
    """The project an environment's ``shared/`` is synced for, and the mark it carries."""

    project_id: str
    mark: str


@dataclass(frozen=True, slots=True)
class _State:
    files: dict[str, ProjectFile]
    version: int
    """The project's counter: the version of its latest change."""
    copies: dict[str, Copy]
    seen: int
    """The counter as of the environment's last complete ``down``; -1 before the first."""


@dataclass
class _Batch:
    """What one sync step changes, recorded in one transaction."""

    project_id: str
    environment_id: str
    counter: int | None = None
    files: dict[str, ProjectFile] = field(default_factory=dict[str, ProjectFile])
    deleted: set[str] = field(default_factory=set[str])
    copies: dict[str, Copy] = field(default_factory=dict[str, Copy])
    dropped: set[str] = field(default_factory=set[str])
    released: set[str] = field(default_factory=set[str])
    """Digests a change stopped using: their blobs go if no file of the project uses them."""
    seen: int | None = None

    def put(self, file: ProjectFile) -> None:
        self.files[file.path] = file
        self.deleted.discard(file.path)

    def delete(self, path: str) -> None:
        self.files.pop(path, None)
        self.deleted.add(path)

    def keep(self, path: str, copy: Copy) -> None:
        self.copies[path] = copy
        self.dropped.discard(path)

    def drop(self, path: str) -> None:
        self.copies.pop(path, None)
        self.dropped.add(path)

    @property
    def empty(self) -> bool:
        return not (
            self.counter is not None
            or self.files
            or self.deleted
            or self.copies
            or self.dropped
            or self.seen is not None
        )


class SharedRecords:
    """Projects' shared files and environments' copies, in their own SQLite file."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._db.execute("PRAGMA journal_mode=WAL")
        # A power loss may undo the last syncs; the next ones find what they missed.
        self._db.execute("PRAGMA synchronous=NORMAL")
        self._db.executescript(
            "CREATE TABLE IF NOT EXISTS projects ("
            " project_id TEXT PRIMARY KEY, version INTEGER NOT NULL);"
            "CREATE TABLE IF NOT EXISTS files ("
            " project_id TEXT NOT NULL, path TEXT NOT NULL, version INTEGER NOT NULL,"
            " sha256 TEXT NOT NULL, size INTEGER NOT NULL, executable INTEGER NOT NULL,"
            " changed_by TEXT NOT NULL, PRIMARY KEY (project_id, path));"
            "CREATE INDEX IF NOT EXISTS files_by_sha256 ON files (project_id, sha256);"
            "CREATE TABLE IF NOT EXISTS environments ("
            " environment_id TEXT PRIMARY KEY, project_id TEXT NOT NULL, mark TEXT NOT NULL,"
            " seen INTEGER NOT NULL);"
            "CREATE TABLE IF NOT EXISTS copies ("
            " environment_id TEXT NOT NULL, path TEXT NOT NULL, sha256 TEXT NOT NULL,"
            " version INTEGER NOT NULL, mtime INTEGER NOT NULL, size INTEGER NOT NULL,"
            " inode INTEGER NOT NULL, PRIMARY KEY (environment_id, path));"
        )
        self._lock = threading.Lock()

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def claim(self, environment_id: str) -> Claim | None:
        with self._lock:
            row = self._db.execute(
                "SELECT project_id, mark FROM environments WHERE environment_id = ?",
                (environment_id,),
            ).fetchone()
        return None if row is None else Claim(str(row[0]), str(row[1]))

    def reclaim(self, environment_id: str, project_id: str, mark: str) -> None:
        """Sync an environment afresh for ``project_id``: its old copies prove nothing now."""
        with self._lock, self._transaction():
            self._db.execute("DELETE FROM copies WHERE environment_id = ?", (environment_id,))
            self._db.execute(
                "INSERT INTO environments VALUES (?, ?, ?, -1) ON CONFLICT (environment_id)"
                " DO UPDATE SET project_id = excluded.project_id, mark = excluded.mark,"
                " seen = -1",
                (environment_id, project_id, mark),
            )

    def forget(self, environment_id: str) -> None:
        """Drop a deleted environment's records."""
        with self._lock, self._transaction():
            self._db.execute("DELETE FROM copies WHERE environment_id = ?", (environment_id,))
            self._db.execute("DELETE FROM environments WHERE environment_id = ?", (environment_id,))

    def state(self, project_id: str, environment_id: str) -> _State:
        """The project's files and counter, and the environment's copies, read together."""
        with self._lock:
            files = {
                str(r[0]): ProjectFile(str(r[0]), int(r[1]), str(r[2]), int(r[3]), bool(r[4]))
                for r in self._db.execute(
                    "SELECT path, version, sha256, size, executable FROM files"
                    " WHERE project_id = ?",
                    (project_id,),
                )
            }
            row = self._db.execute(
                "SELECT version FROM projects WHERE project_id = ?", (project_id,)
            ).fetchone()
            copies = {
                str(r[0]): Copy(str(r[1]), int(r[2]), (int(r[3]), int(r[4]), int(r[5])))
                for r in self._db.execute(
                    "SELECT path, sha256, version, mtime, size, inode FROM copies"
                    " WHERE environment_id = ?",
                    (environment_id,),
                )
            }
            seen = self._db.execute(
                "SELECT seen FROM environments WHERE environment_id = ?", (environment_id,)
            ).fetchone()
        return _State(files, int(row[0]) if row else 0, copies, int(seen[0]) if seen else -1)

    def apply(self, batch: _Batch) -> list[str]:
        """Record a sync step; return its released digests that no file of the project uses."""
        project, environment = batch.project_id, batch.environment_id
        with self._lock, self._transaction():
            if batch.counter is not None:
                self._db.execute(
                    "INSERT INTO projects VALUES (?, ?) ON CONFLICT (project_id)"
                    " DO UPDATE SET version = excluded.version",
                    (project, batch.counter),
                )
            self._db.executemany(
                "INSERT INTO files VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT (project_id, path)"
                " DO UPDATE SET version = excluded.version, sha256 = excluded.sha256,"
                " size = excluded.size, executable = excluded.executable,"
                " changed_by = excluded.changed_by",
                [
                    (project, f.path, f.version, f.sha256, f.size, int(f.executable), environment)
                    for f in batch.files.values()
                ],
            )
            self._db.executemany(
                "DELETE FROM files WHERE project_id = ? AND path = ?",
                [(project, path) for path in batch.deleted],
            )
            self._db.executemany(
                "INSERT INTO copies VALUES (?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT (environment_id, path) DO UPDATE SET sha256 = excluded.sha256,"
                " version = excluded.version, mtime = excluded.mtime, size = excluded.size,"
                " inode = excluded.inode",
                [
                    (environment, path, c.sha256, c.version, *c.stamp)
                    for path, c in batch.copies.items()
                ],
            )
            self._db.executemany(
                "DELETE FROM copies WHERE environment_id = ? AND path = ?",
                [(environment, path) for path in batch.dropped],
            )
            if batch.seen is not None:
                self._db.execute(
                    "UPDATE environments SET seen = ? WHERE environment_id = ?",
                    (batch.seen, environment),
                )
            return [digest for digest in batch.released if not self._uses(project, digest)]

    def used(self, project_id: str, digests: Collection[str]) -> set[str]:
        """Which of ``digests`` a file of the project uses."""
        with self._lock:
            return {digest for digest in digests if self._uses(project_id, digest)}

    def digests(self) -> dict[str, set[str]]:
        """Every project's digests, for the blob stores' sweep."""
        with self._lock:
            rows = self._db.execute("SELECT DISTINCT project_id, sha256 FROM files").fetchall()
        used: dict[str, set[str]] = {}
        for project_id, digest in rows:
            used.setdefault(str(project_id), set()).add(str(digest))
        return used

    def _uses(self, project_id: str, digest: str) -> bool:
        return (
            self._db.execute(
                "SELECT 1 FROM files WHERE project_id = ? AND sha256 = ? LIMIT 1",
                (project_id, digest),
            ).fetchone()
            is not None
        )

    @contextlib.contextmanager
    def _transaction(self) -> Generator[None]:
        self._db.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self._db.execute("ROLLBACK")
            raise
        self._db.execute("COMMIT")


@dataclass(frozen=True, slots=True)
class _Scan:
    files: dict[str, tuple[Stamp, bool]]
    """Each path's stamp and executable bit."""
    sha256: dict[str, str]
    too_large: frozenset[str]
    incomplete: bool
    """Cut short, or with parts that could not be read: a missing file proves nothing."""
    odd: tuple[str, ...]
    """Names that cannot be synced: not text, or not a plain path below ``shared/``."""
    mark: str | None
    now: int


class SharedSync:
    """Brings environments' ``shared/`` in step with their project's copy, and back."""

    def __init__(
        self,
        records: SharedRecords,
        root: Path,
        *,
        max_file_bytes: int = MAX_FILE_BYTES,
        max_project_bytes: int = MAX_PROJECT_BYTES,
        max_project_files: int = MAX_PROJECT_FILES,
        hash_budget: int = HASH_BUDGET,
        failure_backoff: float = FAILURE_BACKOFF,
    ) -> None:
        self.records = records
        self.root = root
        """Each project's blob store is a directory here, named by a hash of its ID."""
        self.max_file_bytes = max_file_bytes
        self.max_project_bytes = max_project_bytes
        self.max_project_files = max_project_files
        self.hash_budget = hash_budget
        self.failure_backoff = failure_backoff
        self._stores: dict[str, Blobs] = {}
        self._project_locks = weakref.WeakValueDictionary[str, asyncio.Lock]()
        self._environment_locks = weakref.WeakValueDictionary[str, asyncio.Lock]()
        self._told = OrderedDict[tuple[str, ...], None]()
        self._failing: dict[str, dict[str, tuple[int, float]]] = {}
        """By environment and path: the version that could not be written, and when to retry."""
        self._large: dict[str, dict[str, Stamp]] = {}
        """By environment and path: the stamp of a file too large to copy, not hashed again."""
        self._wrote: dict[str, deque[tuple[float, str]]] = {}
        """By environment: when ``down`` wrote which path, so calls are not credited with it."""

    def blobs(self, project_id: str) -> Blobs:
        """The project's blob store: no other project's sync reads or deletes its blobs."""
        store = self._stores.get(project_id)
        if store is None:
            store = self._stores[project_id] = Blobs(self.root / _store_name(project_id))
        return store

    async def sweep(self) -> int:
        """Delete blobs no shared file uses, which a crash or a cut-short sync can leave.

        Only when no sync runs, as at startup. Returns how many were deleted.
        """
        used = await asyncio.to_thread(self.records.digests)
        stores = {_store_name(project): digests for project, digests in used.items()}
        return await asyncio.to_thread(_sweep, self.root, stores)

    async def up(
        self, project_id: str, environment_id: str, session_id: str, channel: Channel
    ) -> list[str]:
        """Copy the environment's changes to its shared files into the project's copy."""
        async with self._locked(self._environment_locks, environment_id):
            scan, notices = await self._attach(project_id, environment_id, channel)
            if scan is not None:
                async with self._locked(self._project_locks, project_id):
                    told, _ = await self._up(project_id, environment_id, session_id, channel, scan)
                notices += told
        return _bounded(notices)

    async def down(
        self, project_id: str, environment_id: str, session_id: str, channel: Channel
    ) -> list[str]:
        """Bring the environment's shared files up to the project's, its own changes first."""
        async with self._locked(self._environment_locks, environment_id):
            scan, notices = await self._attach(project_id, environment_id, channel)
            if scan is not None:
                async with self._locked(self._project_locks, project_id):
                    told, scan = await self._up(
                        project_id, environment_id, session_id, channel, scan
                    )
                notices += told
                # Another environment's ``up`` may record changes meanwhile; the next
                # ``down`` brings them.
                notices += await self._down(project_id, environment_id, session_id, channel, scan)
        return _bounded(notices)

    async def write(
        self,
        project_id: str,
        environment_id: str,
        session_id: str,
        channel: Channel,
        fields: dict[str, Any],
        timeout: float,
    ) -> dict[str, Any]:
        """A file request's write in ``shared/``, its precondition held against the project.

        The environment is brought up to date first and the written file goes up at once,
        all under the project's lock: of two writers in different environments that expect
        the same version, the second gets ``conflict``, as in one environment. If the sync
        before the write fails, the write is not made (``unavailable``); if the one after
        fails, the next sync takes the file.
        """
        scan: _Scan | None = None
        async with contextlib.AsyncExitStack() as held:
            try:
                async with asyncio.timeout(timeout):
                    await held.enter_async_context(
                        self._locked(self._environment_locks, environment_id)
                    )
                    scan, _ = await self._attach(project_id, environment_id, channel)
                    if scan is not None:
                        await held.enter_async_context(
                            self._locked(self._project_locks, project_id)
                        )
                        _, scan = await self._up(
                            project_id, environment_id, session_id, channel, scan
                        )
                        await self._down(project_id, environment_id, session_id, channel, scan)
            except Exception as error:
                _log.warning(
                    "syncing shared/ in %s before a write failed: %r", environment_id, error
                )
                raise WorkerError(
                    ErrorKind.UNAVAILABLE,
                    "the project's shared files could not be synced with this environment, so "
                    "the write was not made; try again",
                ) from error
            reply = await channel.request("write", session=session_id, **fields)
            if scan is not None:
                try:
                    async with asyncio.timeout(timeout):
                        after = await self._scan(channel, ())
                        await self._up(project_id, environment_id, session_id, channel, after)
                except Exception as error:  # the write stands; the next sync takes it
                    _log.warning(
                        "syncing shared/ in %s after a write failed: %r", environment_id, error
                    )
        return reply

    def written_since(self, environment_id: str, since: float) -> set[str]:
        """The paths ``down`` wrote into the environment since ``since`` (monotonic clock)."""
        return {path for when, path in self._wrote.get(environment_id, ()) if when >= since}

    async def forget(self, environment_id: str) -> None:
        """Drop what is known of a deleted environment."""
        async with self._locked(self._environment_locks, environment_id):
            await asyncio.to_thread(self.records.forget, environment_id)
            self._reset(environment_id)

    # Steps.

    async def _attach(
        self, project_id: str, environment_id: str, channel: Channel
    ) -> tuple[_Scan | None, list[str]]:
        """Scan the environment's ``shared/``, syncing it afresh if it is not the one known.

        None if the environment's ``shared/`` is synced for another project.
        """
        scan = await self._scan(channel, ())
        claim = await asyncio.to_thread(self.records.claim, environment_id)
        if claim is not None and claim.project_id != project_id:
            return None, self._once(
                ("other", environment_id, project_id),
                "shared/ in this environment is synced with another project's environments, "
                "so not with this project's; use one of this project's own environments",
            )
        if claim is None or scan.mark != claim.mark:
            # A new sandbox under this ID, or one never synced: what it holds is compared
            # with the project's files afresh, so nothing in it reads as deleted.
            mark = f"{secrets.token_hex(16)} {project_id}"[:256]
            await asyncio.to_thread(self.records.reclaim, environment_id, project_id, mark)
            self._reset(environment_id)
            scan = await self._scan(channel, (), mark=mark)
            if scan.mark != mark:
                raise WorkerError(ErrorKind.INTERNAL, "the executor did not keep the sync's mark")
        notices: list[str] = []
        for name in scan.odd:
            notices += self._once(
                ("odd", project_id, name),
                f"{name} is not synced with the project's other environments: its name is not "
                f"text, or its path is over {_MAX_PATH_BYTES} bytes",
            )
        return scan, notices

    async def _up(
        self,
        project_id: str,
        environment_id: str,
        session_id: str,
        channel: Channel,
        scan: _Scan,
    ) -> tuple[list[str], _Scan]:
        """``up`` with both locks held; returns its notices and the latest scan."""
        state = await asyncio.to_thread(self.records.state, project_id, environment_id)
        copies = state.copies
        large = self._large.get(environment_id, {})
        candidates: list[str] = []
        for path, (stamp, _) in scan.files.items():
            if large.get(path) == stamp:
                continue  # too large, and unchanged
            copy = copies.get(path)
            if copy is None or copy.stamp != stamp or stamp[0] >= scan.now - RACY_NS:
                candidates.append(path)
        if not candidates and (scan.incomplete or copies.keys() <= scan.files.keys()):
            return [], scan
        if candidates:
            scan = await self._scan(channel, candidates)
        files = dict(state.files)
        counter = state.version
        held = {f.sha256: f.size for f in files.values()}
        total = sum(f.size for f in files.values())
        taken = set(files) | set(scan.files)
        batch = _Batch(project_id, environment_id)
        notices: list[str] = []
        fetched: set[str] = set()
        try:
            for path in candidates:
                entry = scan.files.get(path)
                if entry is None:
                    continue  # gone since
                stamp, executable = entry
                if path in scan.too_large:
                    self._remember(self._large, environment_id, path, stamp)
                    notices += self._too_large(project_id, path, stamp[1])
                    continue
                sha256 = scan.sha256.get(path)
                if sha256 is None:
                    continue  # changing, or past the hash budget: at a later scan
                copy, current = copies.get(path), files.get(path)
                if copy is not None and copy.sha256 == sha256:
                    batch.keep(path, Copy(sha256, copy.version, stamp))
                    continue
                if current is not None and current.sha256 == sha256:
                    # The project already has this content, from another environment.
                    batch.keep(path, Copy(sha256, current.version, stamp))
                    continue
                base = copy.version if copy is not None else 0
                conflict = current is not None and current.version > base
                target = _conflict_name(path, environment_id, taken) if conflict else path
                replaced = files.get(target)
                if replaced is None and len(files) >= self.max_project_files:
                    notices += self._full(project_id, path)
                    continue
                size = held.get(sha256)
                if size is None:
                    size = await self._fetch(project_id, channel, session_id, path, sha256)
                    if size is None:
                        continue  # it changed while it was read: at a later scan
                    fetched.add(sha256)
                if total - (replaced.size if replaced else 0) + size > self.max_project_bytes:
                    notices += self._full(project_id, path)
                    continue
                counter += 1
                new = ProjectFile(target, counter, sha256, size, executable)
                if replaced is not None:
                    batch.released.add(replaced.sha256)
                    total -= replaced.size
                files[target] = new
                taken.add(target)
                held[sha256] = size
                total += size
                batch.put(new)
                if conflict:
                    # The copy stays at the version it matched, so ``down`` replaces it with
                    # the project's.
                    batch.keep(path, Copy(sha256, base, stamp))
                    notices.append(
                        f"{path} was also changed in another of the project's environments: "
                        f"that version stays as {path}, and this one is kept as {target}"
                    )
                else:
                    batch.keep(path, Copy(sha256, counter, stamp))
            if not scan.incomplete:
                for path in copies.keys() - scan.files.keys():
                    current = files.get(path)
                    # A file another environment changed meanwhile stays: the change wins
                    # over the deletion, and ``down`` brings it back here.
                    if current is not None and current.version <= copies[path].version:
                        counter += 1
                        batch.delete(path)
                        batch.released.add(current.sha256)
                        del files[path]
                        total -= current.size
                    batch.drop(path)
            batch.counter = counter
            batch.released |= fetched
            await self._apply(project_id, batch)
        except BaseException:
            # Blobs read for changes that were not recorded.
            self._discard(project_id, fetched)
            raise
        return notices, scan

    async def _down(
        self,
        project_id: str,
        environment_id: str,
        session_id: str,
        channel: Channel,
        scan: _Scan,
    ) -> list[str]:
        """``down`` with the environment's lock held, once its own changes went up."""
        state = await asyncio.to_thread(self.records.state, project_id, environment_id)
        failing = self._failing.setdefault(environment_id, {})
        now = time.monotonic()
        for path in failing.keys() - state.files.keys() - state.copies.keys():
            del failing[path]  # nothing left to bring
        if state.seen == state.version and all(now < until for _, until in failing.values()):
            return []
        files, copies = state.files, state.copies
        store = self.blobs(project_id)
        batch = _Batch(project_id, environment_id)
        notices: list[str] = []
        complete = True
        try:
            # Deletions first: a directory they empty may be where a file is to be written.
            for path in sorted(files.keys() | copies.keys(), key=lambda p: (p in files, p)):
                current, copy = files.get(path), copies.get(path)
                if current is not None and copy is not None and copy.version >= current.version:
                    continue
                version = current.version if current is not None else -1
                failed = failing.get(path)
                if failed is not None and failed[0] == version and now < failed[1]:
                    continue
                try:
                    if current is None:
                        # Deleted in the project: the environment's copy goes if unchanged.
                        if copy is not None and (path in scan.files or scan.incomplete):
                            await channel.request(
                                "remove", session=session_id, path=path, expected_sha256=copy.sha256
                            )
                        batch.drop(path)
                    elif copy is not None and copy.sha256 == current.sha256:
                        # The same bytes: the version moves on, and the stamp stays as hashed.
                        batch.keep(path, Copy(copy.sha256, current.version, copy.stamp))
                    elif copy is None and path in scan.files:
                        continue  # the environment's own file there, which ``up`` decides
                    else:
                        data = await asyncio.to_thread(store.path(current.sha256).read_bytes)
                        precondition: dict[str, Any] = (
                            {"expected_sha256": copy.sha256}
                            if copy is not None
                            else {"create_only": True}
                        )
                        reply = await channel.request(
                            "write",
                            session=session_id,
                            path=path,
                            content_b64=base64.b64encode(data).decode(),
                            executable=current.executable,
                            **precondition,
                        )
                        stamp = _stamp(reply.get("stamp"))
                        batch.keep(path, Copy(current.sha256, current.version, stamp))
                        self._note_written(environment_id, path)
                    failing.pop(path, None)
                except WorkerError as error:
                    if error.kind not in _FILE_ERRORS:
                        raise
                    if error.kind is ErrorKind.NOT_FOUND and current is None:
                        batch.drop(path)  # already gone
                    elif error.kind is ErrorKind.CONFLICT:
                        # Changed in the environment since its last sync: ``up`` hashes it
                        # again and decides. Until it does, this version is not sent again.
                        if copy is not None:
                            batch.keep(path, Copy(copy.sha256, copy.version, _STALE))
                        self._fail(environment_id, path, version)
                        complete = False
                    else:
                        self._fail(environment_id, path, version)
                        notices += self._once(
                            ("failed", environment_id, path, str(version)),
                            f"{path} could not be brought into this environment: "
                            f"{_shown(error.message)}",
                        )
                except (OSError, ValueError) as error:  # a blob that cannot be read
                    self._fail(environment_id, path, version)
                    complete = False
                    _log.warning("shared blob of %s for %s unreadable: %s", project_id, path, error)
            if complete:
                batch.seen = state.version
        finally:
            # What was written stays recorded, even if the step was cut short.
            if not batch.empty:
                await asyncio.shield(self._apply(project_id, batch))
        return notices

    async def _apply(self, project_id: str, batch: _Batch) -> None:
        unused = await asyncio.to_thread(self.records.apply, batch)
        if unused:
            await asyncio.to_thread(_unlink, self.blobs(project_id), unused)

    def _discard(self, project_id: str, digests: Collection[str]) -> None:
        if digests:
            unused = set(digests) - self.records.used(project_id, digests)
            _unlink(self.blobs(project_id), unused)

    # The executor's replies.

    async def _scan(
        self, channel: Channel, hashed: Sequence[str], mark: str | None = None
    ) -> _Scan:
        fields: dict[str, Any] = {
            "hash": list(hashed),
            "hash_limit": self.max_file_bytes,
            "hash_budget": self.hash_budget,
        }
        if mark is not None:
            fields["mark"] = mark
        reply = await channel.request("shared", **fields)
        files: dict[str, tuple[Stamp, bool]] = {}
        refused: list[str] = []
        incomplete = reply.get("truncated") is not False or reply.get("partial") is not False
        raw_files = reply.get("files")
        if isinstance(raw_files, dict):
            for path, raw in cast(dict[object, object], raw_files).items():
                values = cast(list[object], raw) if isinstance(raw, list) else []
                if (
                    isinstance(path, str)
                    and _valid_path(path)
                    and len(values) == 4
                    and all(type(v) is int and 0 <= v <= _MAX_INT for v in values)
                    and values[3] in (0, 1)
                ):
                    numbers = cast(list[int], values)
                    files[path] = ((numbers[0], numbers[1], numbers[2]), numbers[3] == 1)
                else:
                    incomplete = True  # something there that cannot be accounted for
                    if isinstance(path, str) and len(refused) < 10:
                        refused.append(path)
        else:
            incomplete = True
        digests: dict[str, str] = {}
        raw_digests = reply.get("sha256")
        if isinstance(raw_digests, dict):
            for path, digest in cast(dict[object, object], raw_digests).items():
                if (
                    isinstance(path, str)
                    and path in files
                    and isinstance(digest, str)
                    and _DIGEST.fullmatch(digest)
                ):
                    digests[path] = digest
        raw_large = reply.get("too_large")
        too_large = frozenset(
            path
            for path in (cast(list[object], raw_large) if isinstance(raw_large, list) else [])
            if isinstance(path, str) and path in files
        )
        raw_odd = reply.get("odd")
        odd = tuple(
            _shown(name)
            for name in [
                *(cast(list[object], raw_odd) if isinstance(raw_odd, list) else [])[:10],
                *refused,
            ]
            if isinstance(name, str)
        )
        mark, now = reply.get("mark"), reply.get("now")
        return _Scan(
            files=files,
            sha256=digests,
            too_large=too_large,
            incomplete=incomplete,
            odd=odd,
            mark=mark if isinstance(mark, str) else None,
            now=now if type(now) is int and 0 <= now <= _MAX_INT else 0,
        )

    async def _fetch(
        self, project_id: str, channel: Channel, session_id: str, path: str, sha256: str
    ) -> int | None:
        """Read a file into the project's blob store; its size, or None if it is not ``sha256``."""
        writer = await asyncio.to_thread(self.blobs(project_id).writer)
        stored = False
        try:
            offset = 0
            while True:
                try:
                    reply = await channel.request(
                        "read", session=session_id, path=path, offset=offset,
                        length=READ_BYTES, hash=offset == 0,
                    )  # fmt: skip
                except WorkerError as error:
                    if error.kind not in _FILE_ERRORS:
                        raise
                    return None
                raw = reply.get("content_b64")
                if not isinstance(raw, str):
                    return None
                try:
                    data = base64.b64decode(raw, validate=True)
                except ValueError:
                    return None
                if offset == 0 and reply.get("sha256") != sha256:
                    return None
                if offset + len(data) > self.max_file_bytes:
                    return None
                await asyncio.to_thread(writer.write, data)
                offset += len(data)
                if reply.get("eof") is True or not data:
                    break
            # Kept only if the bytes received hash to ``sha256``, whatever the executor said.
            stored = await asyncio.to_thread(writer.commit, sha256)
            return writer.size if stored else None
        finally:
            if not stored:
                writer.discard()

    # Bookkeeping.

    def _once(self, key: tuple[str, ...], text: str) -> list[str]:
        if key in self._told:
            return []
        self._told[key] = None
        while len(self._told) > MAX_REMEMBERED:
            self._told.popitem(last=False)
        return [text]

    def _too_large(self, project_id: str, path: str, size: int) -> list[str]:
        mib = 1024 * 1024
        return self._once(
            ("large", project_id, path, str(size)),
            f"{path} ({size // mib} MiB) is over {self.max_file_bytes // mib} MiB, so it is not "
            "copied to the project's other environments; publish it as an artifact instead",
        )

    def _full(self, project_id: str, path: str) -> list[str]:
        mib = 1024 * 1024
        return self._once(
            ("full", project_id, path),
            f"the project's shared files are at their limit ({self.max_project_files} files, "
            f"{self.max_project_bytes // mib} MiB), so {path} is not copied to the project's "
            "other environments; remove files from shared/, or publish large ones as artifacts",
        )

    def _fail(self, environment_id: str, path: str, version: int) -> None:
        until = time.monotonic() + self.failure_backoff
        self._remember(self._failing, environment_id, path, (version, until))

    @staticmethod
    def _remember[T](
        table: dict[str, dict[str, T]], environment_id: str, path: str, value: T
    ) -> None:
        entries = table.setdefault(environment_id, {})
        entries[path] = value
        while len(entries) > MAX_REMEMBERED:
            del entries[next(iter(entries))]

    def _reset(self, environment_id: str) -> None:
        self._failing.pop(environment_id, None)
        self._large.pop(environment_id, None)
        self._wrote.pop(environment_id, None)

    def _note_written(self, environment_id: str, path: str) -> None:
        wrote = self._wrote.get(environment_id)
        if wrote is None:
            wrote = self._wrote[environment_id] = deque(maxlen=MAX_WROTE)
        wrote.append((time.monotonic(), path))

    @staticmethod
    @contextlib.asynccontextmanager
    async def _locked(
        locks: weakref.WeakValueDictionary[str, asyncio.Lock], key: str
    ) -> AsyncGenerator[None]:
        # The holder's reference keeps the lock; once unused, its entry goes with it.
        lock = locks.get(key)
        if lock is None:
            lock = locks[key] = asyncio.Lock()
        async with lock:
            yield


def _valid_path(path: str) -> bool:
    """A path the executor may report: below ``shared/``, plain, printable and not too long."""
    if not path.startswith(SHARED) or path.endswith("/") or not path.isprintable():
        return False
    if len(path.encode("utf-8", "surrogateescape")) > _MAX_PATH_BYTES:
        return False
    return all(part not in ("", ".", "..") for part in path[len(SHARED) :].split("/"))


def _stamp(raw: object) -> Stamp:
    values = cast(list[object], raw) if isinstance(raw, list) else []
    if len(values) == 3 and all(type(v) is int and 0 <= v <= _MAX_INT for v in values):
        numbers = cast(list[int], values)
        return (numbers[0], numbers[1], numbers[2])
    return _STALE


def _conflict_name(path: str, environment_id: str, taken: Collection[str]) -> str:
    """``shared/results.csv`` -> ``shared/results.conflict-<environment>.csv``, then ``-2``…"""
    directory, name = posixpath.split(path)
    stem, dot, suffix = name.rpartition(".")
    if not stem:  # no suffix, or a dot file
        stem, dot, suffix = name, "", ""
    label = "".join(c if c.isalnum() or c in "-_" else "-" for c in environment_id)[:40] or "env"
    for count in range(1, 100):
        tail = label if count == 1 else f"{label}-{count}"
        candidate = posixpath.join(directory, f"{stem}.conflict-{tail}{dot}{suffix}")
        if candidate not in taken:
            return candidate
    tail = f"{label}-{secrets.token_hex(4)}"
    return posixpath.join(directory, f"{stem}.conflict-{tail}{dot}{suffix}")


def _bounded(notices: list[str]) -> list[str]:
    if len(notices) <= MAX_NOTICES:
        return notices
    rest = len(notices) - MAX_NOTICES + 1
    return [*notices[: MAX_NOTICES - 1], f"and {rest} more notices about shared/"]


def _shown(text: str) -> str:
    return "".join(c if c.isprintable() else "?" for c in text[:300])


def _store_name(project_id: str) -> str:
    return hashlib.sha256(project_id.encode("utf-8", "surrogateescape")).hexdigest()[:32]


def _unlink(store: Blobs, digests: Collection[str]) -> None:
    for digest in digests:
        with contextlib.suppress(OSError, ValueError):
            store.path(digest).unlink()


def _sweep(root: Path, used: dict[str, set[str]]) -> int:
    """Delete the blobs in each store under ``root`` that its project's files do not use."""
    removed = 0
    if not root.is_dir():
        return 0
    for store in root.iterdir():
        if store.is_symlink() or not store.is_dir():
            continue
        for leftover in (store / "tmp").glob("*"):
            with contextlib.suppress(OSError):
                leftover.unlink()  # a blob being written when the worker stopped
        keep = used.get(store.name, set())
        blobs = store / "blobs"
        if blobs.is_symlink() or not blobs.is_dir():
            continue
        for directory in blobs.iterdir():
            if directory.is_symlink() or not directory.is_dir():
                continue
            for blob in directory.iterdir():
                if blob.name not in keep:
                    with contextlib.suppress(OSError):
                        blob.unlink()
                        removed += 1
    return removed
