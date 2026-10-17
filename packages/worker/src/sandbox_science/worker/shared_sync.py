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
meanwhile is a conflict: the project keeps its version, and the environment's file is
renamed there to a conflict copy named after the environment, so a call still writing
to it goes on writing to the copy. The project's version then takes the name, and the
agent is told. A deletion that meets a change loses to it.

Research code controls ``shared/``, so the worker trusts nothing an executor reports.
Paths, stamps and digests are checked; content is read from the environment and kept
only if the bytes received hash to the digest, unless the same project already holds
that content; and a scan with unreadable parts, or one cut short, proves no file
missing. A mark the worker writes beside ``shared/`` identifies the directory it
synced: a new sandbox under an old ID, or a mark removed, makes the environment's
records unverified, so nothing in it reads as deleted while the project's own
deletions still apply. An environment's ``shared/`` is synced for the first project
that uses it, and no other.

Stamps decide which files to hash, so a call that changes nothing in ``shared/`` costs a
scan before it and one after. A file recorded within two seconds of its last change is
hashed again at the next scan, since a clock coarser than a rewrite could hide one.
Each step has a time and a byte budget and records its progress as it goes, so a large
change arrives over several calls rather than never. A file that a process has open is
not replaced or removed until nothing has it open. A failure on one file is retried
later and the agent told once; only a failing executor stops a step. Files over
``max_file_bytes``, and changes that would take the project's copy past
``max_project_bytes`` or ``max_project_files``, stay where they are, and the agent is
told once. Blobs are written to disk before they are recorded and checked when read,
and blobs no shared file uses any more are deleted.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import dataclasses
import hashlib
import logging
import posixpath
import re
import secrets
import sqlite3
import threading
import weakref
from collections import OrderedDict, deque
from collections.abc import AsyncGenerator, Collection, Generator, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final, Protocol, cast

from sandbox_science.contracts import SYSTEM_CLOCK, Clock, ErrorKind

from .artifacts import Blobs
from .errors import WorkerError
from .recovery_fence import check_dispatch

_log = logging.getLogger(__name__)

MAX_FILE_BYTES: Final = 32 * 1024 * 1024
"""The largest file copied between environments: a write to the executor is one line."""
MAX_PROJECT_BYTES: Final = 2 * 1024 * 1024 * 1024
"""The most a project's copy of its shared files may hold."""
MAX_PROJECT_FILES: Final = 10_000
"""The most files a project's copy may hold; an environment's scan reports up to 20,000."""
HASH_BUDGET: Final = 256 * 1024 * 1024
"""The bytes one scan hashes; files past it are hashed at a later scan."""
STEP_SECONDS: Final = 40.0
"""A step starts no transfer this long after it got its locks; the next goes on from there."""
CALL_SECONDS: Final = 100.0
"""All of one sync's steps start no transfer this long after it began, so that it ends
within the execution manager's limit of 120 seconds, waits for locks included."""
STEP_BYTES: Final = 1024 * 1024 * 1024
"""The most one step reads from an environment."""
COMMIT_FILES: Final = 100
COMMIT_BYTES: Final = 8 * 1024 * 1024
"""``up`` records what it read in batches this large, so a step cut short keeps them."""
READ_BYTES: Final = 8 * 1024 * 1024
RACY_NS: Final = 2_000_000_000
"""A file recorded this soon after its last change is hashed again at the next scan."""
FAILURE_BACKOFF: Final = 600.0
"""Seconds before a file that could not be written into an environment is tried again."""
MAX_NOTICES: Final = 10
"""The most notices one sync step adds to a result."""
MAX_REMEMBERED: Final = 10_000
"""Bounds the notices told once and the files remembered as failing or too large."""
RECENT_BYTES: Final = 256 * 1024
"""Path bytes of the writes into one environment remembered for attributing files."""
SHARED: Final = "shared/"
NAME_MAX: Final = 255
"""The longest file name, in bytes, that Linux file systems take."""
_DIGEST: Final = re.compile(r"[0-9a-f]{64}")
_MAX_PATH_BYTES: Final = 4096
_MAX_INT: Final = 2**63 - 1
_STOP: Final = frozenset({ErrorKind.UNAVAILABLE, ErrorKind.TIMED_OUT, ErrorKind.CANCELLED})
"""Errors that mean the executor, its channel or its disk failed: the step stops. Any
other error concerns one file, which is tried again later."""

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
    verified: bool = True
    """False once the environment's directory was found replaced: the file may be gone."""


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
            " inode INTEGER NOT NULL, verified INTEGER NOT NULL DEFAULT 1,"
            " PRIMARY KEY (environment_id, path));"
            "CREATE INDEX IF NOT EXISTS copies_by_sha256 ON copies (sha256);"
        )
        columns = {str(row[1]) for row in self._db.execute("PRAGMA table_info(copies)")}
        if "verified" not in columns:  # records from before copies could be unverified
            self._db.execute("ALTER TABLE copies ADD COLUMN verified INTEGER NOT NULL DEFAULT 1")
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
        """Mark an environment's directory as new: its copies are kept, but unverified."""
        with self._lock, self._transaction():
            self._db.execute(
                "UPDATE copies SET verified = 0, mtime = -1, size = -1, inode = -1"
                " WHERE environment_id = ?",
                (environment_id,),
            )
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
                str(r[0]): Copy(str(r[1]), int(r[2]), (int(r[3]), int(r[4]), int(r[5])), bool(r[6]))
                for r in self._db.execute(
                    "SELECT path, sha256, version, mtime, size, inode, verified FROM copies"
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
                "INSERT INTO copies VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT (environment_id, path) DO UPDATE SET sha256 = excluded.sha256,"
                " version = excluded.version, mtime = excluded.mtime, size = excluded.size,"
                " inode = excluded.inode, verified = excluded.verified",
                [
                    (environment, path, c.sha256, c.version, *c.stamp, int(c.verified))
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

    def unverify(self, project_id: str, sha256: str) -> None:
        """Have the project's environments hash their copies of ``sha256`` again."""
        with self._lock, self._transaction():
            self._db.execute(
                "UPDATE copies SET mtime = -1, size = -1, inode = -1 WHERE sha256 = ?"
                " AND environment_id IN"
                " (SELECT environment_id FROM environments WHERE project_id = ?)",
                (sha256, project_id),
            )

    def used(self, project_id: str, digests: Collection[str]) -> set[str]:
        """Which of ``digests`` a file of the project uses."""
        with self._lock:
            return {digest for digest in digests if self._uses(project_id, digest)}

    def project_digests(self, project_id: str) -> set[str]:
        """Every digest the project's files use."""
        with self._lock:
            rows = self._db.execute(
                "SELECT DISTINCT sha256 FROM files WHERE project_id = ?", (project_id,)
            ).fetchall()
        return {str(row[0]) for row in rows}

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
    reason: str | None
    """Why the scan proves no file missing: ``truncated``, ``partial`` or ``malformed``."""
    odd: tuple[str, ...]
    """Names that cannot be synced: not text, or not a plain path below ``shared/``."""
    mark: str | None
    now: int

    @property
    def incomplete(self) -> bool:
        return self.reason is not None


class _Recent:
    """What the sync wrote into one environment lately, so calls are not credited with it."""

    def __init__(self) -> None:
        self.writes: deque[tuple[float, str, Stamp]] = deque()
        self.size = 0
        self.latest: dict[str, Stamp] = {}
        """Those paths' stamps at the environment's latest scan."""

    def add(self, when: float, path: str, stamp: Stamp) -> None:
        self.writes.append((when, path, stamp))
        self.size += len(path)
        while self.size > RECENT_BYTES:
            _, old, _ = self.writes.popleft()
            self.size -= len(old)


class _BlobError(Exception):
    """A blob that could not be read, with whether it is gone or damaged for good."""

    def __init__(self, message: str, *, lost: bool) -> None:
        super().__init__(message)
        self.lost = lost
        """Missing, or not matching its digest; otherwise unreadable for now (EMFILE, EIO)."""


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
        step_seconds: float = STEP_SECONDS,
        step_bytes: int = STEP_BYTES,
        call_seconds: float = CALL_SECONDS,
        clock: Clock = SYSTEM_CLOCK,
    ) -> None:
        self.records = records
        self.clock = clock
        """The execution manager's clock: it stamps calls with it, and asks which files the
        sync wrote between two of its readings."""
        self.root = root
        """Each project's blob store is a directory here, named by a hash of its ID."""
        self.max_file_bytes = max_file_bytes
        self.max_project_bytes = max_project_bytes
        self.max_project_files = max_project_files
        self.hash_budget = hash_budget
        self.failure_backoff = failure_backoff
        self.step_seconds = step_seconds
        self.step_bytes = step_bytes
        self.call_seconds = call_seconds
        self._stores: dict[str, Blobs] = {}
        self._project_locks = weakref.WeakValueDictionary[str, asyncio.Lock]()
        self._environment_locks = weakref.WeakValueDictionary[str, asyncio.Lock]()
        self._told = OrderedDict[bytes, None]()
        self._failing: dict[str, dict[bytes, tuple[int, float]]] = {}
        """By environment and path: the version that could not be written, and when to retry."""
        self._large: dict[str, dict[bytes, Stamp]] = {}
        """By environment and path: the stamp of a file not to be read again until it changes."""
        self._recent: dict[str, _Recent] = {}
        self._dirty: set[str] = set()
        """Projects whose stores may hold blobs a cut-short step read and never recorded."""
        self._missing: dict[str, set[str]] = {}
        """By project: digests whose blobs were lost, to read again from an environment."""
        self._held: dict[str, set[bytes]] = {}
        """By environment: files kept back because a process there is writing them."""

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
        ends = self.clock.monotonic() + self.call_seconds
        async with self._locked(self._environment_locks, environment_id):
            scan, notices = await self._attach(project_id, environment_id, channel)
            if scan is not None:
                async with self._locked(self._project_locks, project_id):
                    notices += await self._up(
                        project_id, environment_id, session_id, channel, scan, self._budget(ends)
                    )
        return _bounded(notices)

    async def down(
        self, project_id: str, environment_id: str, session_id: str, channel: Channel
    ) -> list[str]:
        """Bring the environment's shared files up to the project's, its own changes first."""
        ends = self.clock.monotonic() + self.call_seconds
        async with self._locked(self._environment_locks, environment_id):
            scan, notices = await self._attach(project_id, environment_id, channel)
            if scan is not None:
                async with self._locked(self._project_locks, project_id):
                    notices += await self._up(
                        project_id, environment_id, session_id, channel, scan, self._budget(ends)
                    )
                # Another environment's ``up`` may record changes meanwhile; the next
                # ``down`` brings them.
                notices += await self._down(
                    project_id, environment_id, session_id, channel, self._budget(ends)
                )
        return _bounded(notices)

    async def write(
        self,
        project_id: str,
        environment_id: str,
        session_id: str,
        channel: Channel,
        fields: dict[str, Any],
        timeout: float,
        notices: list[str],
    ) -> dict[str, Any]:
        """A file request's write in ``shared/``, its precondition held against the project.

        The environment is brought up to date first and the written file goes up at once,
        all under the project's lock: of two writers in different environments that expect
        the same version, the second gets ``conflict``, as in one environment. If the sync
        before the write fails, or leaves the file out of date, the write is not made
        (``unavailable``); if the one after fails, the next sync takes the file. What the
        syncs find goes into ``notices``, whether or not the write is made.
        """
        path = posixpath.normpath(str(fields.get("path", "")))
        synced = False
        ends = self.clock.monotonic() + min(self.call_seconds, timeout)
        async with contextlib.AsyncExitStack() as held:
            try:
                async with asyncio.timeout(timeout):
                    await held.enter_async_context(
                        self._locked(self._environment_locks, environment_id)
                    )
                    scan, told = await self._attach(project_id, environment_id, channel)
                    notices += told
                    if scan is not None:
                        await held.enter_async_context(
                            self._locked(self._project_locks, project_id)
                        )
                        notices += await self._up(
                            project_id,
                            environment_id,
                            session_id,
                            channel,
                            scan,
                            self._budget(ends),
                        )
                        notices += await self._down(
                            project_id, environment_id, session_id, channel, self._budget(ends)
                        )
                        synced = True
            except Exception as error:
                _log.warning(
                    "syncing shared/ in %s before a write failed: %r", environment_id, error
                )
                raise WorkerError(
                    ErrorKind.UNAVAILABLE,
                    "the project's shared files could not be synced with this environment, so "
                    "the write was not made; try again",
                ) from error
            if synced:
                await self._check_current(project_id, environment_id, path)
            reply = await channel.request("write", session=session_id, **fields)
            if synced:
                try:
                    async with asyncio.timeout(timeout):
                        after = await self._scan(channel, ())
                        notices += await self._up(
                            project_id,
                            environment_id,
                            session_id,
                            channel,
                            after,
                            self.clock.monotonic() + self.step_seconds,
                        )
                except Exception as error:  # the write stands; the next sync takes it
                    _log.warning(
                        "syncing shared/ in %s after a write failed: %r", environment_id, error
                    )
        return reply

    def unchanged_writes(
        self, environment_id: str, paths: Collection[str], since: float, until: float
    ) -> set[str]:
        """Which of ``paths`` the sync wrote into the environment between ``since`` and
        ``until`` (monotonic clock), unchanged at its latest scan: no call wrote them."""
        recent = self._recent.get(environment_id)
        if recent is None:
            return set()
        wrote = {path: stamp for when, path, stamp in recent.writes if since <= when <= until}
        return {path for path in paths if path in wrote and recent.latest.get(path) == wrote[path]}

    async def forget(self, environment_id: str) -> None:
        """Drop what is known of a deleted environment."""
        async with self._locked(self._environment_locks, environment_id):
            await asyncio.to_thread(self.records.forget, environment_id)
            self._reset(environment_id)

    # Steps.

    async def _attach(
        self, project_id: str, environment_id: str, channel: Channel
    ) -> tuple[_Scan | None, list[str]]:
        """Scan the environment's ``shared/``, marking it as new if it is not the one known.

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
            # A new sandbox under this ID, a mark removed, or one never synced: what the
            # records say it holds is checked again, and nothing reads as deleted.
            mark = f"{secrets.token_hex(16)} {project_id}"[:256]
            check_dispatch()
            self.records.reclaim(environment_id, project_id, mark)
            self._reset(environment_id)
            scan = await self._scan(channel, (), mark=mark)
            if scan.mark != mark:
                raise WorkerError(ErrorKind.INTERNAL, "the executor did not keep the sync's mark")
        notices: list[str] = []
        for name in scan.odd:
            notices += self._once(
                ("odd", project_id, name),
                f"{name} is not synced with the project's other environments: its name is not "
                f"text, a name in it is over {NAME_MAX} bytes, or the path is over "
                f"{_MAX_PATH_BYTES} bytes",
            )
        if scan.reason == "truncated":
            notices += self._once(
                ("truncated", environment_id),
                "shared/ in this environment holds more than one scan reports (20,000 files, "
                "or 4 MiB of paths), so files deleted there are not deleted in the project's "
                "other environments",
            )
        elif scan.reason == "partial":
            notices += self._once(
                ("partial", environment_id),
                "parts of shared/ in this environment could not be read, so files deleted "
                "there are not deleted in the project's other environments",
            )
        return scan, notices

    async def _up(
        self,
        project_id: str,
        environment_id: str,
        session_id: str,
        channel: Channel,
        scan: _Scan,
        deadline: float,
    ) -> list[str]:
        """``up`` with both locks held; returns its notices."""
        state = await asyncio.to_thread(self.records.state, project_id, environment_id)
        copies = state.copies
        large = self._large.get(environment_id, {})
        missing = self._missing.get(project_id, set())
        candidates: list[str] = []
        for path, (stamp, _) in scan.files.items():
            if large.get(_key(path)) == stamp:
                continue  # not to be read again until it changes
            copy = copies.get(path)
            if (
                copy is None
                or copy.stamp != stamp
                or stamp[0] >= scan.now - RACY_NS
                or copy.sha256 in missing
            ):
                candidates.append(path)
        if candidates:
            scan = await self._scan(channel, candidates)
        self._note_scan(environment_id, scan)
        removed = (
            []
            if scan.incomplete
            else [path for path, copy in copies.items() if copy.verified and path not in scan.files]
        )
        if not candidates and not removed:
            await self._settle_project(project_id)
            return []
        files = dict(state.files)
        counter = state.version
        held = {f.sha256: f.size for f in files.values() if f.sha256 not in missing}
        total = sum(f.size for f in files.values())
        taken = set(files) | set(scan.files)
        notices: list[str] = []
        batch = _Batch(project_id, environment_id)
        read = unrecorded = 0
        try:
            for path in removed:
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
            for path in candidates:
                if self.clock.monotonic() > deadline or read > self.step_bytes:
                    break  # the rest at the next step
                entry = scan.files.get(path)
                if entry is None:
                    continue  # gone since
                stamp, executable = entry
                # Changed within a clock tick of the scan, it may change again unseen.
                recorded = _STALE if stamp[0] >= scan.now - RACY_NS else stamp
                if path in scan.too_large:
                    self._remember(self._large, environment_id, path, stamp)
                    notices += self._too_large(project_id, path, stamp[1])
                    continue
                sha256 = scan.sha256.get(path)
                if sha256 is None:
                    continue  # changing, or past the hash budget: at a later scan
                if sha256 in missing:
                    # The project lost these bytes; this environment still has them.
                    size = await self._fetch(project_id, channel, session_id, path, sha256)
                    if size is None:
                        continue
                    read += size
                    unrecorded += size
                    missing.discard(sha256)
                    held[sha256] = size
                copy, current = copies.get(path), files.get(path)
                if copy is not None and copy.sha256 == sha256:
                    batch.keep(path, Copy(sha256, copy.version, recorded))
                    self._settle(environment_id, path)
                    continue
                if current is not None and current.sha256 == sha256:
                    # The project already has this content, from another environment.
                    batch.keep(path, Copy(sha256, current.version, recorded))
                    self._settle(environment_id, path)
                    continue
                base = copy.version if copy is not None else 0
                # A copy not known to be the environment's own file proves no base: when the
                # contents differ, neither may silently replace the other.
                rival = (
                    current
                    if current is not None
                    and (current.version > base or (copy is not None and not copy.verified))
                    else None
                )
                target = _conflict_name(path, environment_id, taken) if rival else path
                replaced = files.get(target)
                room = self.max_project_bytes - total + (replaced.size if replaced else 0)
                if replaced is None and len(files) >= self.max_project_files:
                    notices += self._full(project_id, path)
                    continue
                size = held.get(sha256)
                if size is None:
                    if stamp[1] > room:  # by the size the scan reports, before reading it
                        notices += self._full(project_id, path)
                        continue
                    size = await self._fetch(project_id, channel, session_id, path, sha256)
                    if size is None:
                        continue  # it changed while it was read: at a later scan
                    read += size
                    unrecorded += size
                    if size > room:  # larger than the scan said: not read again unchanged
                        self._remember(self._large, environment_id, path, stamp)
                        notices += self._full(project_id, path)
                        await asyncio.to_thread(self._discard, project_id, {sha256})
                        continue
                elif size > room:
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
                if rival is not None:
                    notices.append(
                        f"{_shown(path)} was also changed in another of the project's "
                        f"environments: that version stays as {_shown(path)}, and this one is "
                        f"kept as {_shown(target)}"
                    )
                    if await self._move(channel, path, target):
                        # The environment's file is the conflict copy now, so a call still
                        # writing to it goes on writing there; the project's version takes
                        # the path at once, so the name is never left empty.
                        batch.keep(target, Copy(sha256, counter, _STALE))
                        stamp = await self._put_back(project_id, channel, session_id, rival)
                        if stamp is None:
                            batch.drop(path)  # ``down`` writes it
                        else:
                            batch.keep(path, Copy(rival.sha256, rival.version, _STALE))
                            self._wrote(environment_id, path, stamp)
                    else:
                        # The copy stays at the version it matched, so ``down`` replaces it.
                        batch.keep(path, Copy(sha256, base, recorded))
                else:
                    batch.keep(path, Copy(sha256, counter, recorded))
                self._settle(environment_id, path)
                if len(batch.files) >= COMMIT_FILES or unrecorded >= COMMIT_BYTES:
                    batch.counter = counter
                    await self._commit(project_id, batch)
                    batch = _Batch(project_id, environment_id)
                    unrecorded = 0
            batch.counter = counter
            await self._commit(project_id, batch)
        except BaseException:
            if unrecorded:
                self._dirty.add(project_id)  # its blobs go at the project's next step
            raise
        await self._settle_project(project_id)
        return notices

    async def _down(
        self,
        project_id: str,
        environment_id: str,
        session_id: str,
        channel: Channel,
        deadline: float,
    ) -> list[str]:
        """``down`` with the environment's lock held, once its own changes went up."""
        state = await asyncio.to_thread(self.records.state, project_id, environment_id)
        files, copies = state.files, state.copies
        failing = self._failing.setdefault(environment_id, {})
        now = self.clock.monotonic()
        if failing:
            known = {_key(path) for path in files.keys() | copies.keys()}
            for key in failing.keys() - known:
                del failing[key]  # nothing left to bring
        if state.seen == state.version and all(now < until for _, until in failing.values()):
            return []
        paths = files.keys() | copies.keys()
        work = [path for path in paths if not _current(files.get(path), copies.get(path))]
        kept_back = self._held.setdefault(environment_id, set())
        batch = _Batch(project_id, environment_id)
        notices: list[str] = []
        complete = True
        try:
            if work:
                scan = await self._scan(channel, ())  # which files are there now
                store = self.blobs(project_id)
                # Deletions first: a directory they empty may be where a file is to go.
                for path in sorted(work, key=lambda p: (p in files, p)):
                    if self.clock.monotonic() > deadline:
                        complete = False
                        break
                    current, copy = files.get(path), copies.get(path)
                    if copy is not None and not copy.verified:
                        if path not in scan.files:
                            copy = None  # the environment's directory does not have it
                        elif current is not None:
                            complete = False
                            continue  # ``up`` has not hashed it yet
                    version = current.version if current is not None else -1
                    failed = failing.get(_key(path))
                    if failed is not None and failed[0] == version and now < failed[1]:
                        continue
                    try:
                        if current is None:
                            # Deleted in the project: the environment's copy goes if unchanged.
                            if copy is not None and (path in scan.files or scan.incomplete):
                                reply = await channel.request(
                                    "remove",
                                    session=session_id,
                                    path=path,
                                    expected_sha256=copy.sha256,
                                    if_unused=True,
                                )
                                if reply.get("busy") is True:
                                    complete = False
                                    notices += self._kept_back(environment_id, path, version)
                                    continue
                            batch.drop(path)
                        elif copy is not None and copy.sha256 == current.sha256:
                            # The same bytes: the version moves on, and the stamp stays.
                            batch.keep(path, dataclasses.replace(copy, version=current.version))
                        elif copy is None and path in scan.files:
                            continue  # the environment's own file there, which ``up`` decides
                        else:
                            content = await asyncio.to_thread(_encoded, store, current.sha256)
                            precondition: dict[str, Any] = (
                                {"expected_sha256": copy.sha256}
                                if copy is not None
                                else {"create_only": True}
                            )
                            reply = await channel.request(
                                "write",
                                session=session_id,
                                path=path,
                                content_b64=content,
                                executable=current.executable,
                                if_unused=True,
                                **precondition,
                            )
                            if reply.get("busy") is True:
                                complete = False
                                notices += self._kept_back(environment_id, path, version)
                                continue
                            # Hashed again at the next scan, in case it changes within the
                            # clock tick of this write.
                            batch.keep(path, Copy(current.sha256, current.version, _STALE))
                            self._wrote(environment_id, path, _stamp(reply.get("stamp")))
                        failing.pop(_key(path), None)
                        kept_back.discard(_key(path))
                    except WorkerError as error:
                        if error.kind in _STOP:
                            raise
                        if error.kind is ErrorKind.NOT_FOUND and current is None:
                            batch.drop(path)  # already gone
                        elif error.kind is ErrorKind.CONFLICT:
                            complete = False
                            if copy is not None and path in scan.files:
                                # Changed since its last sync: ``up`` hashes it and decides.
                                batch.keep(path, dataclasses.replace(copy, stamp=_STALE))
                            else:
                                # A file of the environment's own is there now, or the one
                                # synced is gone past a scan cut short: not sent again soon.
                                self._fail(environment_id, path, version)
                        else:
                            self._fail(environment_id, path, version)
                            notices += self._once(
                                ("failed", environment_id, path, str(version)),
                                f"{_shown(path)} could not be brought into this environment: "
                                f"{_shown(error.message)}",
                            )
                    except _BlobError as error:
                        complete = False
                        self._fail(environment_id, path, version)
                        if current is not None and error.lost:
                            await self._lost(project_id, current.sha256)
                        _log.warning("shared files of %s: %s", project_id, error)
            if complete:
                batch.seen = state.version
        finally:
            # What was written stays recorded, even if the step was cut short.
            if not batch.empty:
                await self._commit(project_id, batch)
        return notices

    async def _check_current(self, project_id: str, environment_id: str, path: str) -> None:
        """Refuse a write to a file the sync could not bring up to date in the environment."""
        if not path.startswith(SHARED):
            return
        state = await asyncio.to_thread(self.records.state, project_id, environment_id)
        if not _current(state.files.get(path), state.copies.get(path)):
            if _key(path) in self._held.get(environment_id, set()):
                why = "a process in this environment is writing it"
                when = "once it is closed"
            else:
                why, when = "it could not be brought up to date in this environment", "again"
            raise WorkerError(
                ErrorKind.UNAVAILABLE,
                f"{_shown(path)} was not written: {why}, so the project's newer version is not "
                f"there yet; try {when}",
            )

    async def _commit(self, project_id: str, batch: _Batch) -> None:
        """Record a batch. Cut short meanwhile, the step still waits for the record before
        its locks go, so the cleanup of the project's blobs never races it."""
        applying = asyncio.ensure_future(self._apply(project_id, batch))
        try:
            await asyncio.shield(applying)
        except asyncio.CancelledError:
            with contextlib.suppress(Exception):
                await asyncio.wait({applying})
            raise

    async def _put_back(
        self, project_id: str, channel: Channel, session_id: str, file: ProjectFile
    ) -> Stamp | None:
        """Write the project's version of a file just moved away; its stamp, or None."""
        try:
            content = await asyncio.to_thread(_encoded, self.blobs(project_id), file.sha256)
            reply = await channel.request(
                "write",
                session=session_id,
                path=file.path,
                content_b64=content,
                executable=file.executable,
                create_only=True,
            )
        except _BlobError:
            return None
        except WorkerError as error:
            if error.kind in _STOP:
                raise
            return None
        return _stamp(reply.get("stamp"))

    def _kept_back(self, environment_id: str, path: str, version: int) -> list[str]:
        self._held.setdefault(environment_id, set()).add(_key(path))
        return self._once(
            ("kept back", environment_id, path, str(version)),
            f"{_shown(path)} is not updated in this environment while a process here is "
            "writing it; the project's newer version arrives once it is closed",
        )

    async def _apply(self, project_id: str, batch: _Batch) -> None:
        check_dispatch()
        unused = self.records.apply(batch)
        if unused:
            await asyncio.to_thread(_unlink, self.blobs(project_id), unused)

    def _discard(self, project_id: str, digests: Collection[str]) -> None:
        if digests:
            unused = set(digests) - self.records.used(project_id, digests)
            _unlink(self.blobs(project_id), unused)

    async def _settle_project(self, project_id: str) -> None:
        """With the project's lock held: delete blobs a cut-short step left unrecorded."""
        if project_id in self._dirty:
            used = await asyncio.to_thread(self.records.project_digests, project_id)
            await asyncio.to_thread(_sweep_store, self.blobs(project_id).root, used)
            self._dirty.discard(project_id)

    async def _lost(self, project_id: str, sha256: str) -> None:
        """A blob is gone or damaged: read it again from an environment that has it.

        A damaged blob stays where it is until good content replaces it; deleting it
        would gain nothing, and its record keeps naming the content to look for.
        """
        self._missing.setdefault(project_id, set()).add(sha256)
        check_dispatch()
        self.records.unverify(project_id, sha256)

    # The executor's replies.

    async def _scan(
        self,
        channel: Channel,
        hashed: Sequence[str],
        mark: str | None = None,
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
        malformed = False
        raw_files = reply.get("files")
        if isinstance(raw_files, dict):
            for path, raw in cast(dict[object, object], raw_files).items():
                if not isinstance(path, str):
                    malformed = True
                    continue
                if not _valid_path(path):
                    # Never a synced path, so its presence proves nothing either way.
                    if len(refused) < 10:
                        refused.append(path)
                    continue
                values = cast(list[object], raw) if isinstance(raw, list) else []
                if (
                    len(values) == 4
                    and all(type(v) is int and 0 <= v <= _MAX_INT for v in values)
                    and values[3] in (0, 1)
                ):
                    numbers = cast(list[int], values)
                    files[path] = ((numbers[0], numbers[1], numbers[2]), numbers[3] == 1)
                else:
                    malformed = True  # something there that cannot be accounted for
        else:
            malformed = True
        reason = (
            "truncated"
            if reply.get("truncated") is not False
            else "partial"
            if reply.get("partial") is not False
            else "malformed"
            if malformed
            else None
        )
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
        too_large = frozenset(
            path for path in _strings(reply.get("too_large"), len(files)) if path in files
        )
        odd = tuple(_shown(name) for name in [*_strings(reply.get("odd"), 10), *refused])
        mark, now = reply.get("mark"), reply.get("now")
        return _Scan(
            files=files,
            sha256=digests,
            too_large=too_large,
            reason=reason,
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
                    if error.kind in _STOP:
                        raise
                    return None  # gone or unreadable: at a later scan
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

    async def _move(self, channel: Channel, path: str, target: str) -> bool:
        """Give the environment's file ``path`` the free name ``target``; whether it did."""
        try:
            await channel.request("move", path=path, target=target)
        except WorkerError as error:
            if error.kind in _STOP:
                raise
            return False  # the name is taken there, or the file went: as before moving
        return True

    # Bookkeeping.

    def _once(self, key: tuple[str, ...], text: str) -> list[str]:
        digest = hashlib.blake2b(repr(key).encode(), digest_size=16).digest()
        if digest in self._told:
            return []
        self._told[digest] = None
        while len(self._told) > MAX_REMEMBERED:
            self._told.popitem(last=False)
        return [text]

    def _too_large(self, project_id: str, path: str, size: int) -> list[str]:
        mib = 1024 * 1024
        return self._once(
            ("large", project_id, path, str(size)),
            f"{_shown(path)} ({size // mib} MiB) is over {self.max_file_bytes // mib} MiB, so it "
            "is not copied to the project's other environments; publish it as an artifact "
            "instead",
        )

    def _full(self, project_id: str, path: str) -> list[str]:
        mib = 1024 * 1024
        return self._once(
            ("full", project_id, path),
            f"the project's shared files are at their limit ({self.max_project_files} files, "
            f"{self.max_project_bytes // mib} MiB), so {_shown(path)} is not copied to the "
            "project's other environments; remove files from shared/, or publish large ones as "
            "artifacts",
        )

    def _fail(self, environment_id: str, path: str, version: int) -> None:
        until = self.clock.monotonic() + self.failure_backoff
        self._remember(self._failing, environment_id, path, (version, until))

    def _settle(self, environment_id: str, path: str) -> None:
        """The environment's file at ``path`` was recorded: nothing about it is failing."""
        failing = self._failing.get(environment_id)
        if failing:
            failing.pop(_key(path), None)

    @staticmethod
    def _remember[T](
        table: dict[str, dict[bytes, T]], environment_id: str, path: str, value: T
    ) -> None:
        entries = table.setdefault(environment_id, {})
        entries[_key(path)] = value
        while len(entries) > MAX_REMEMBERED:
            del entries[next(iter(entries))]

    def _wrote(self, environment_id: str, path: str, stamp: Stamp) -> None:
        recent = self._recent.get(environment_id)
        if recent is None:
            recent = self._recent[environment_id] = _Recent()
        recent.add(self.clock.monotonic(), path, stamp)

    def _note_scan(self, environment_id: str, scan: _Scan) -> None:
        recent = self._recent.get(environment_id)
        if recent is not None:
            recent.latest = {
                path: scan.files[path][0] for _, path, _ in recent.writes if path in scan.files
            }

    def _budget(self, ends: float) -> float:
        """The deadline of a step that starts now, within its sync's."""
        return min(self.clock.monotonic() + self.step_seconds, ends)

    def _reset(self, environment_id: str) -> None:
        self._held.pop(environment_id, None)
        self._failing.pop(environment_id, None)
        self._large.pop(environment_id, None)
        self._recent.pop(environment_id, None)

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


def _current(file: ProjectFile | None, copy: Copy | None) -> bool:
    """Whether an environment's copy is the project's version of a file, or both are absent."""
    if file is None:
        return copy is None
    return copy is not None and copy.verified and copy.version >= file.version


def _valid_path(path: str) -> bool:
    """A path the executor may report: below ``shared/``, plain, printable and not too long."""
    if not path.startswith(SHARED) or path.endswith("/") or not path.isprintable():
        return False
    if len(path.encode("utf-8", "surrogateescape")) > _MAX_PATH_BYTES:
        return False
    return all(
        part not in ("", ".", "..") and len(part.encode("utf-8", "surrogateescape")) <= NAME_MAX
        for part in path[len(SHARED) :].split("/")
    )


def _stamp(raw: object) -> Stamp:
    values = cast(list[object], raw) if isinstance(raw, list) else []
    if len(values) == 3 and all(type(v) is int and 0 <= v <= _MAX_INT for v in values):
        numbers = cast(list[int], values)
        return (numbers[0], numbers[1], numbers[2])
    return _STALE


def _strings(raw: object, limit: int) -> list[str]:
    items = cast(list[object], raw) if isinstance(raw, list) else []
    return [item for item in items[:limit] if isinstance(item, str)]


def _conflict_name(path: str, environment_id: str, taken: Collection[str]) -> str:
    """``shared/results.csv`` -> ``shared/results.conflict-<environment>.csv``, then ``-2``…

    The name stays within ``NAME_MAX`` bytes, by shortening the original stem.
    """
    directory, name = posixpath.split(path)
    stem, dot, suffix = name.rpartition(".")
    if not stem:  # no suffix, or a dot file
        stem, dot, suffix = name, "", ""
    label = "".join(c if c.isalnum() or c in "-_" else "-" for c in environment_id)[:40] or "env"
    tails = [label, *(f"{label}-{count}" for count in range(2, 100))]
    for tail in [*tails, f"{label}-{secrets.token_hex(4)}"]:
        candidate = posixpath.join(directory, _fitted(stem, f".conflict-{tail}{dot}{suffix}"))
        if len(candidate.encode()) > _MAX_PATH_BYTES:  # a deep directory: at the top instead
            candidate = SHARED + posixpath.basename(candidate)
        if candidate not in taken:
            return candidate
    raise AssertionError("unreachable: the random name is free")


def _fitted(stem: str, ending: str) -> str:
    """``stem + ending``, the stem shortened to keep the name within ``NAME_MAX`` bytes."""
    if len(ending.encode()) >= NAME_MAX:
        # A suffix too long to keep: the name ends with the conflict label alone.
        ending = ending[: ending.index(".", 1)]
    room = NAME_MAX - len(ending.encode())
    shortened = stem.encode()[:room].decode("utf-8", "ignore")
    return (shortened or "file") + ending


def _bounded(notices: list[str]) -> list[str]:
    if len(notices) <= MAX_NOTICES:
        return notices
    rest = len(notices) - MAX_NOTICES + 1
    return [*notices[: MAX_NOTICES - 1], f"and {rest} more notices about shared/"]


def _shown(text: str) -> str:
    """Text an environment controls, as a notice may quote it: printable, and at most 300
    characters, the middle of a long path left out."""
    if len(text) > 300:
        text = f"{text[:140]}…{text[-140:]}"
    return "".join(c if c.isprintable() else "?" for c in text)


def _key(path: str) -> bytes:
    """A path's fixed-size key for the memory tables, whatever the path's length."""
    return hashlib.blake2b(path.encode("utf-8", "surrogateescape"), digest_size=16).digest()


def _store_name(project_id: str) -> str:
    return hashlib.sha256(project_id.encode("utf-8", "surrogateescape")).hexdigest()[:32]


def _encoded(store: Blobs, sha256: str) -> str:
    """A blob's content in base64, once it is checked against its digest."""
    try:
        data = store.path(sha256).read_bytes()
    except FileNotFoundError as exc:
        raise _BlobError(f"blob {sha256} is missing", lost=True) from exc
    except OSError as exc:
        raise _BlobError(f"blob {sha256} cannot be read now: {exc}", lost=False) from exc
    if hashlib.sha256(data).hexdigest() != sha256:
        raise _BlobError(f"blob {sha256} does not match its digest", lost=True)
    return base64.b64encode(data).decode()


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
        removed += _sweep_store(store, used.get(store.name, set()))
    return removed


def _sweep_store(store: Path, keep: set[str]) -> int:
    """Delete the blobs in one project's store that ``keep`` does not name."""
    removed = 0
    blobs = store / "blobs"
    if blobs.is_symlink() or not blobs.is_dir():
        return 0
    for directory in blobs.iterdir():
        if directory.is_symlink() or not directory.is_dir():
            continue
        for blob in directory.iterdir():
            if blob.name not in keep:
                with contextlib.suppress(OSError):
                    blob.unlink()
                    removed += 1
    return removed
