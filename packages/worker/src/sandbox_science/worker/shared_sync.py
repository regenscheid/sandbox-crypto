# SPDX-License-Identifier: MIT
"""Shared project data across a project's environments (D79).

Each environment is its own sandbox (a VM with OpenShell's VM driver), so the ``shared/``
area a project's sessions write is a separate directory in each. The worker keeps the
project's copy of every shared file, under its SHA-256 in the blob store, and brings each
environment's ``shared/`` in step with it:

- ``up``: after a tool call in an environment, the files that changed there since it last
  synced are copied into the project's copy;
- ``down``: before a tool call, the files whose project version is newer than the
  environment's are copied into it. Its own unsynced changes go up first.

Each file in the project's copy has a version, a number that grows with every change in the
project. An environment's record says, for each file, the stamp (modification time, size,
inode) and SHA-256 its copy had when last synced, and the project version it matched. A
change an environment makes to a file whose project version moved on meanwhile, because
another environment changed it, is a conflict: the project keeps the version it has, the
environment's version is kept beside it as a conflict copy, and the agent is told. A
deletion that meets a change loses to it.

The stamps decide which files to hash, so a call that changes nothing in ``shared/`` costs
one scan. Files larger than ``max_file_bytes`` are not copied, and the agent is told once.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import logging
import posixpath
import secrets
import sqlite3
import threading
from collections.abc import AsyncGenerator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, Protocol, cast

from .artifacts import Blobs
from .errors import WorkerError

_log = logging.getLogger(__name__)

MAX_FILE_BYTES: Final = 32 * 1024 * 1024
"""The largest file copied between environments: the executor takes a write in one line."""
MAX_PROJECT_BYTES: Final = 2 * 1024 * 1024 * 1024
"""The most the project's copy of its shared files may hold, so a sandbox cannot fill the
worker's disk with files under the per-file limit."""
READ_BYTES: Final = 8 * 1024 * 1024
SHARED: Final = "shared/"

Stamp = tuple[int, int, int]
"""A file's modification time in nanoseconds, size and inode, as the executor scans them."""


class Channel(Protocol):
    async def request(self, op: str, **fields: Any) -> dict[str, Any]: ...


@dataclass(frozen=True, slots=True)
class ProjectFile:
    """The project's copy of a shared file; ``sha256`` is None once it is deleted."""

    path: str
    version: int
    sha256: str | None
    size: int


@dataclass(frozen=True, slots=True)
class Copy:
    """An environment's copy of a shared file as of its last sync."""

    sha256: str
    version: int
    """The project version the copy matched; lower than the project's when it is out of date."""
    stamp: Stamp


class SharedRecords:
    """The project's shared files and each environment's copies, in their own SQLite file."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript(
            "CREATE TABLE IF NOT EXISTS files ("
            " project_id TEXT NOT NULL, path TEXT NOT NULL, version INTEGER NOT NULL,"
            " sha256 TEXT, size INTEGER NOT NULL, changed_by TEXT NOT NULL,"
            " PRIMARY KEY (project_id, path));"
            "CREATE TABLE IF NOT EXISTS copies ("
            " project_id TEXT NOT NULL, environment_id TEXT NOT NULL, path TEXT NOT NULL,"
            " sha256 TEXT NOT NULL, version INTEGER NOT NULL, mtime INTEGER NOT NULL,"
            " size INTEGER NOT NULL, inode INTEGER NOT NULL,"
            " PRIMARY KEY (project_id, environment_id, path));"
            "CREATE TABLE IF NOT EXISTS seen ("
            " project_id TEXT NOT NULL, environment_id TEXT NOT NULL, version INTEGER NOT NULL,"
            " PRIMARY KEY (project_id, environment_id));"
        )
        self._lock = threading.Lock()

    def close(self) -> None:
        self._db.close()

    def files(self, project_id: str) -> dict[str, ProjectFile]:
        with self._lock:
            rows = self._db.execute(
                "SELECT path, version, sha256, size FROM files WHERE project_id = ?",
                (project_id,),
            ).fetchall()
        return {str(r[0]): ProjectFile(str(r[0]), int(r[1]), r[2], int(r[3])) for r in rows}

    def total(self, project_id: str) -> int:
        """The bytes of the project's current shared files."""
        with self._lock:
            row = self._db.execute(
                "SELECT SUM(size) FROM files WHERE project_id = ? AND sha256 IS NOT NULL",
                (project_id,),
            ).fetchone()
        return int(row[0]) if row and row[0] is not None else 0

    def latest(self, project_id: str) -> int:
        with self._lock:
            row = self._db.execute(
                "SELECT MAX(version) FROM files WHERE project_id = ?", (project_id,)
            ).fetchone()
        return int(row[0]) if row and row[0] is not None else 0

    def record(
        self, project_id: str, path: str, sha256: str | None, size: int, environment_id: str
    ) -> int:
        """Store a new version of a file, or its deletion; return the version."""
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                row = self._db.execute(
                    "SELECT MAX(version) FROM files WHERE project_id = ?", (project_id,)
                ).fetchone()
                version = (int(row[0]) if row and row[0] is not None else 0) + 1
                self._db.execute(
                    "INSERT INTO files VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT (project_id, path)"
                    " DO UPDATE SET version = excluded.version, sha256 = excluded.sha256,"
                    " size = excluded.size, changed_by = excluded.changed_by",
                    (project_id, path, version, sha256, size, environment_id),
                )
            except BaseException:
                self._db.execute("ROLLBACK")
                raise
            self._db.execute("COMMIT")
        return version

    def copies(self, project_id: str, environment_id: str) -> dict[str, Copy]:
        with self._lock:
            rows = self._db.execute(
                "SELECT path, sha256, version, mtime, size, inode FROM copies"
                " WHERE project_id = ? AND environment_id = ?",
                (project_id, environment_id),
            ).fetchall()
        return {
            str(r[0]): Copy(str(r[1]), int(r[2]), (int(r[3]), int(r[4]), int(r[5]))) for r in rows
        }

    def set_copy(self, project_id: str, environment_id: str, path: str, copy: Copy) -> None:
        with self._lock:
            self._db.execute(
                "INSERT INTO copies VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT (project_id, environment_id, path) DO UPDATE SET"
                " sha256 = excluded.sha256, version = excluded.version, mtime = excluded.mtime,"
                " size = excluded.size, inode = excluded.inode",
                (project_id, environment_id, path, copy.sha256, copy.version, *copy.stamp),
            )

    def drop_copy(self, project_id: str, environment_id: str, path: str) -> None:
        with self._lock:
            self._db.execute(
                "DELETE FROM copies WHERE project_id = ? AND environment_id = ? AND path = ?",
                (project_id, environment_id, path),
            )

    def seen(self, project_id: str, environment_id: str) -> int:
        with self._lock:
            row = self._db.execute(
                "SELECT version FROM seen WHERE project_id = ? AND environment_id = ?",
                (project_id, environment_id),
            ).fetchone()
        return int(row[0]) if row else -1

    def set_seen(self, project_id: str, environment_id: str, version: int) -> None:
        with self._lock:
            self._db.execute(
                "INSERT INTO seen VALUES (?, ?, ?) ON CONFLICT (project_id, environment_id)"
                " DO UPDATE SET version = excluded.version",
                (project_id, environment_id, version),
            )


@dataclass(frozen=True, slots=True)
class _Scan:
    files: dict[str, Stamp]
    sha256: dict[str, str]
    truncated: bool


class SharedSync:
    """Brings an environment's ``shared/`` in step with the project's copy, and back."""

    def __init__(
        self,
        records: SharedRecords,
        blobs: Blobs,
        *,
        max_file_bytes: int = MAX_FILE_BYTES,
        max_project_bytes: int = MAX_PROJECT_BYTES,
    ) -> None:
        self.records = records
        self.blobs = blobs
        self.max_file_bytes = max_file_bytes
        self.max_project_bytes = max_project_bytes
        self._projects: dict[str, asyncio.Lock] = {}
        self._environments: dict[tuple[str, str], asyncio.Lock] = {}
        self._told: set[tuple[str, str, str]] = set()
        """Files too large to copy that the agent was told about, by project, path and hash."""

    async def up(
        self, project_id: str, environment_id: str, session_id: str, channel: Channel
    ) -> list[str]:
        """Copy the environment's changed shared files into the project's copy."""
        async with self._environment(project_id, environment_id):
            return await self._up(project_id, environment_id, session_id, channel)

    async def down(
        self, project_id: str, environment_id: str, session_id: str, channel: Channel
    ) -> list[str]:
        """Bring the environment's shared files up to the project's, its own changes first."""
        async with self._environment(project_id, environment_id):
            notices = await self._up(project_id, environment_id, session_id, channel)
            latest = self.records.latest(project_id)
            if self.records.seen(project_id, environment_id) == latest:
                return notices
            copies = self.records.copies(project_id, environment_id)
            scan = await self._scan(channel, ())
            written: list[tuple[str, str, int]] = []
            complete = True
            for path, current in sorted(self.records.files(project_id).items()):
                copy = copies.get(path)
                if copy is not None and copy.version >= current.version:
                    continue
                try:
                    if current.sha256 is None:
                        if copy is not None and path in scan.files:
                            await channel.request(
                                "remove", session=session_id, path=path, expected_sha256=copy.sha256
                            )
                        self.records.drop_copy(project_id, environment_id, path)
                        continue
                    if copy is not None and copy.sha256 == current.sha256:
                        stamp = scan.files.get(path, copy.stamp)
                        self.records.set_copy(
                            project_id,
                            environment_id,
                            path,
                            Copy(copy.sha256, current.version, stamp),
                        )
                        continue
                    if current.size > self.max_file_bytes:
                        notices += self._too_large(project_id, path, current.sha256, current.size)
                        continue
                    if copy is None and path in scan.files:
                        continue  # a file of its own the environment could not sync: left as is
                    data = await asyncio.to_thread(self.blobs.path(current.sha256).read_bytes)
                    present = copy is not None and path in scan.files
                    await channel.request(
                        "write",
                        session=session_id,
                        path=path,
                        content_b64=base64.b64encode(data).decode(),
                        **(
                            {"expected_sha256": copy.sha256}
                            if present and copy
                            else {"create_only": True}
                        ),
                    )
                    written.append((path, current.sha256, current.version))
                except (WorkerError, OSError) as error:
                    # Changed in the environment meanwhile, or unreadable: tried again next time.
                    complete = False
                    _log.info("shared file %s not brought to %s: %s", path, environment_id, error)
            if written:
                after = await self._scan(channel, ())
                for path, sha256, version in written:
                    stamp = after.files.get(path)
                    if stamp is not None:
                        self.records.set_copy(
                            project_id, environment_id, path, Copy(sha256, version, stamp)
                        )
            if complete:
                self.records.set_seen(project_id, environment_id, latest)
            return notices

    async def _up(
        self, project_id: str, environment_id: str, session_id: str, channel: Channel
    ) -> list[str]:
        copies = self.records.copies(project_id, environment_id)
        scan = await self._scan(channel, ())
        changed = [
            path
            for path, stamp in scan.files.items()
            if (copy := copies.get(path)) is None or copy.stamp != stamp
        ]
        removed = [path for path in copies if path not in scan.files and not scan.truncated]
        if not changed and not removed:
            return []
        hashed = await self._scan(channel, changed) if changed else scan
        notices: list[str] = []
        async with self._project(project_id):
            files = self.records.files(project_id)
            for path in changed:
                sha256, stamp = hashed.sha256.get(path), hashed.files.get(path)
                if sha256 is None or stamp is None:
                    continue  # gone or changing: next time
                copy, current = copies.get(path), files.get(path)
                if copy is not None and copy.sha256 == sha256:
                    self.records.set_copy(
                        project_id, environment_id, path, Copy(sha256, copy.version, stamp)
                    )
                    continue
                if current is not None and current.sha256 == sha256:
                    # The project already has this content, from another environment.
                    self.records.set_copy(
                        project_id, environment_id, path, Copy(sha256, current.version, stamp)
                    )
                    continue
                if stamp[1] > self.max_file_bytes:
                    notices += self._too_large(project_id, path, sha256, stamp[1])
                    continue
                replaced = current.size if current is not None and current.sha256 else 0
                if self.records.total(project_id) - replaced + stamp[1] > self.max_project_bytes:
                    notices += self._full(project_id, path, sha256)
                    continue
                if not await self._fetch(channel, session_id, path, sha256):
                    continue  # it changed while it was read: next time
                base = copy.version if copy is not None else 0
                if current is None or current.sha256 is None or current.version <= base:
                    version = self.records.record(
                        project_id, path, sha256, stamp[1], environment_id
                    )
                    self.records.set_copy(
                        project_id, environment_id, path, Copy(sha256, version, stamp)
                    )
                    continue
                # The project's file moved on since this environment's copy: a conflict.
                name = _conflict_name(path, session_id, set(files))
                version = self.records.record(project_id, name, sha256, stamp[1], environment_id)
                files[name] = ProjectFile(name, version, sha256, stamp[1])
                # The copy stays at its old version, so ``down`` replaces it with the project's.
                self.records.set_copy(project_id, environment_id, path, Copy(sha256, base, stamp))
                notices.append(
                    f"{_shown(path)} was also changed in another of the project's environments: "
                    f"that version stays as {_shown(path)}, and this one is kept as {_shown(name)}"
                )
            for path in removed:
                copy, current = copies[path], files.get(path)
                # If another environment changed the file meanwhile, the change wins, and
                # ``down`` brings it back here.
                if (
                    current is not None
                    and current.sha256 is not None
                    and current.version <= copy.version
                ):
                    self.records.record(project_id, path, None, 0, environment_id)
                self.records.drop_copy(project_id, environment_id, path)
        return notices

    async def _scan(self, channel: Channel, hashed: Sequence[str]) -> _Scan:
        reply = await channel.request("shared", hash=list(hashed))
        raw_files = reply.get("files")
        files: dict[str, Stamp] = {}
        if isinstance(raw_files, dict):
            for path, stamp in cast(dict[object, object], raw_files).items():
                if (
                    isinstance(path, str)
                    and path.startswith(SHARED)
                    and isinstance(stamp, list)
                    and len(cast(list[object], stamp)) == 3
                    and all(type(v) is int for v in cast(list[object], stamp))
                ):
                    values = cast(list[int], stamp)
                    files[path] = (values[0], values[1], values[2])
        raw_hashes = reply.get("sha256")
        digests: dict[str, str] = {}
        if isinstance(raw_hashes, dict):
            for path, digest in cast(dict[object, object], raw_hashes).items():
                if isinstance(path, str) and isinstance(digest, str) and len(digest) == 64:
                    digests[path] = digest
        return _Scan(files, digests, reply.get("truncated") is True)

    async def _fetch(self, channel: Channel, session_id: str, path: str, sha256: str) -> bool:
        """Read a file into the blob store; False if it is no longer ``sha256``."""
        if await asyncio.to_thread(self.blobs.path(sha256).is_file):
            return True
        writer = self.blobs.writer()
        offset = 0
        try:
            while True:
                reply = await channel.request(
                    "read", session=session_id, path=path, offset=offset, length=READ_BYTES,
                    hash=offset == 0,
                )  # fmt: skip
                if offset == 0 and reply.get("sha256") != sha256:
                    writer.discard()
                    return False
                data = base64.b64decode(str(reply.get("content_b64", "")), validate=True)
                writer.write(data)
                offset += len(data)
                if reply.get("eof") is True or not data:
                    break
                if offset > self.max_file_bytes:
                    writer.discard()
                    return False
        except BaseException:
            writer.discard()
            raise
        return await asyncio.to_thread(writer.commit, sha256)

    def _too_large(self, project_id: str, path: str, sha256: str, size: int) -> list[str]:
        key = (project_id, path, sha256)
        if key in self._told:
            return []
        self._told.add(key)
        limit = self.max_file_bytes // (1024 * 1024)
        return [
            f"{_shown(path)} ({size // (1024 * 1024)} MiB) is over {limit} MiB, so it is not "
            "copied to the project's other environments; publish it as an artifact instead"
        ]

    def _full(self, project_id: str, path: str, sha256: str) -> list[str]:
        key = (project_id, path, sha256)
        if key in self._told:
            return []
        self._told.add(key)
        limit = self.max_project_bytes // (1024 * 1024)
        return [
            f"the project's shared files hold {limit} MiB, their limit, so {_shown(path)} is not "
            "copied to the project's other environments; remove files from shared/ or publish "
            "large ones as artifacts"
        ]

    @contextlib.asynccontextmanager
    async def _environment(self, project_id: str, environment_id: str) -> AsyncGenerator[None]:
        lock = self._environments.setdefault((project_id, environment_id), asyncio.Lock())
        async with lock:
            yield

    @contextlib.asynccontextmanager
    async def _project(self, project_id: str) -> AsyncGenerator[None]:
        async with self._projects.setdefault(project_id, asyncio.Lock()):
            yield


def _conflict_name(path: str, session_id: str, taken: set[str]) -> str:
    """``shared/results.csv`` -> ``shared/results.conflict-s2.csv``, then ``…-s2-2.csv``."""
    directory, name = posixpath.split(path)
    stem, dot, suffix = name.rpartition(".")
    if not stem:  # no suffix, or a dot file
        stem, dot, suffix = name, "", ""
    label = "".join(c if c.isalnum() or c in "-_" else "-" for c in session_id)[:40] or "session"
    for count in range(1, 100):
        tail = label if count == 1 else f"{label}-{count}"
        candidate = posixpath.join(directory, f"{stem}.conflict-{tail}{dot}{suffix}")
        if candidate not in taken:
            return candidate
    return posixpath.join(directory, f"{stem}.conflict-{label}-{secrets.token_hex(4)}{dot}{suffix}")


def _shown(path: str) -> str:
    return "".join(c if c.isprintable() else "?" for c in path[:300])
