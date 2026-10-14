# SPDX-License-Identifier: MIT
"""The control store's connection and transactions.

``<state-dir>/control.sqlite3`` holds the documents, comments, reviews, plans, artifacts
and math checks. Each of those has a repository that keeps its own SQL and runs it in a
transaction it gets from the one ``ControlStore`` it was given: ``write()`` for a change
and ``read()`` for a consistent view. More than one process writes the file (each harness
run, and the workspace server), so a change is one ``BEGIN IMMEDIATE`` transaction, which
takes SQLite's write lock before it reads anything. The database uses write-ahead
logging, so readers never wait for a writer, and ``synchronous=FULL``, so a change
reported done survives a crash.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from . import migrations
from .errors import BUSY, INTERNAL, NOT_FOUND, ControlError

FILE_NAME: Final = "control.sqlite3"
DEFAULT_TIMEOUT: Final = 10.0
"""Seconds a change waits for another process's write to finish."""


@dataclass(frozen=True, slots=True)
class Tx:
    """A transaction in progress: repositories run their SQL on ``sql``."""

    sql: sqlite3.Connection


def _busy(error: sqlite3.OperationalError) -> bool:
    return any(word in str(error).lower() for word in ("locked", "busy"))


class ControlStore:
    """One process's connection to the control store at ``path``.

    A writer creates the database when it first opens it, or brings it to the current
    schema version, in that first transaction (migrations.py); before it opens the file
    to write, it checks the file through a read-only connection and refuses one it
    cannot use, without changing it. A read-only store opens the database only to read,
    refuses one in another version, and is ``missing`` until a writer has made it. One
    lock serializes this object's transactions across threads.
    """

    def __init__(
        self,
        path: Path,
        *,
        readonly: bool = False,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        self.path = path
        self.readonly = readonly
        self.timeout = timeout
        self._lock = threading.RLock()
        self._db: sqlite3.Connection | None = None

    @property
    def missing(self) -> bool:
        """Whether this read-only store has no database yet. ``read()`` on it raises
        ``not_found``: a lookup fails with that kind, and repositories that list records
        check ``missing`` first and return an empty listing."""
        with self._lock:
            if not self.readonly or self._db is not None:
                return False
            try:
                self._connection()
            except ControlError as error:
                # A file the first writer has not filled yet is missing too; any other
                # refusal is for read() to report.
                return error.kind == NOT_FOUND
            return False

    @property
    def in_transaction(self) -> bool:
        """Whether the calling thread has a transaction open on this store. The lock
        makes another thread's transaction finish first."""
        with self._lock:
            return self._db is not None and self._db.in_transaction

    def initialize(self) -> None:
        """Open the database, creating it for a writer, before anything else uses it."""
        with self._lock:
            self._connection()

    def close(self) -> None:
        with self._lock:
            if self._db is not None:
                self._db.close()
                self._db = None

    @contextmanager
    def write(self) -> Generator[Tx]:
        """One change: SQLite's write lock is taken before anything is read."""
        if self.readonly:
            raise ControlError(INTERNAL, "this control store is read-only")
        with self._lock:
            try:
                db = self._connection()
                self._unnested(db)
                db.execute("BEGIN IMMEDIATE")
            except sqlite3.OperationalError as error:
                if not _busy(error):
                    raise
                raise self._held() from error
            try:
                yield Tx(db)
                db.execute("COMMIT")
            except BaseException:
                if db.in_transaction:
                    db.execute("ROLLBACK")
                raise

    @contextmanager
    def read(self) -> Generator[Tx]:
        """A consistent view for reads that take several queries.

        A ``missing`` store raises ``not_found``: callers that read it as empty check
        ``missing`` first.
        """
        with self._lock:
            db = self._connection()
            self._unnested(db)
            db.execute("BEGIN")
            try:
                yield Tx(db)
            finally:
                if db.in_transaction:
                    db.execute("COMMIT")

    @staticmethod
    def _unnested(db: sqlite3.Connection) -> None:
        # The lock is held, so an open transaction is this thread's own.
        if db.in_transaction:
            raise ControlError(
                INTERNAL, "a control store transaction is already open in this thread"
            )

    def _connection(self) -> sqlite3.Connection:
        if self._db is not None:
            return self._db
        if self.readonly:
            if not self.path.is_file():
                raise ControlError(NOT_FOUND, "nothing has been stored yet")
            db = self._readonly()
            try:
                migrations.check(self.path, db, readonly=True)
            except BaseException:
                db.close()
                raise
        else:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            if self.path.is_file():
                # Refuse a file this code cannot use before anything could change it:
                # opening it to write could switch its journal or replay a crashed WAL.
                probe = self._readonly()
                try:
                    migrations.check(self.path, probe, readonly=False)
                finally:
                    probe.close()
            db = sqlite3.connect(
                self.path, timeout=self.timeout, isolation_level=None, check_same_thread=False
            )
            try:
                self._open(db)
            except sqlite3.OperationalError as error:
                db.close()
                if not _busy(error):
                    raise
                raise self._held() from error
            except BaseException:
                db.close()
                raise
        self._db = db
        return db

    def _readonly(self) -> sqlite3.Connection:
        return sqlite3.connect(
            self.path.resolve().as_uri() + "?mode=ro",
            uri=True,
            timeout=self.timeout,
            isolation_level=None,
            check_same_thread=False,
        )

    def _held(self) -> ControlError:
        return ControlError(
            BUSY,
            f"another change held the control store for over {self.timeout:g} "
            "seconds: try again (with the same operation ID, a retry is safe)",
        )

    def _open(self, db: sqlite3.Connection) -> None:
        # Switching a new file to WAL fails at once, without the busy timeout, while
        # another process is creating it: wait for that as for any other lock.
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                (mode,) = db.execute("PRAGMA journal_mode=WAL").fetchone()
                break
            except sqlite3.OperationalError as error:
                if not _busy(error) or time.monotonic() >= deadline:
                    raise
                time.sleep(0.01)
        if str(mode).lower() != "wal":
            raise ControlError(INTERNAL, f"{self.path} cannot use write-ahead logging")
        db.execute("PRAGMA synchronous=FULL")
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("BEGIN IMMEDIATE")
        try:
            migrations.migrate(db, self.path)
            db.execute("COMMIT")
        finally:
            if db.in_transaction:
                db.execute("ROLLBACK")


def store_path(state_dir: Path) -> Path:
    """The control store's database in a harness state directory."""
    return state_dir / FILE_NAME
