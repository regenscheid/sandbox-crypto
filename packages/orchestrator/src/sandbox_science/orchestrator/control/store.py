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
from collections.abc import Callable, Generator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from .errors import BUSY, INTERNAL, NOT_FOUND, ControlError

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

    A writer creates the database when it first opens it and runs ``prepare`` in that
    first transaction, to create or check the schema. A read-only store opens the
    database only to read, refuses one whose ``user_version`` is not ``version``, and
    is ``missing`` until a writer has made it. One lock serializes this object's
    transactions across threads.
    """

    def __init__(
        self,
        path: Path,
        *,
        version: int,
        prepare: Callable[[sqlite3.Connection], None],
        readonly: bool = False,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        self.path = path
        self.readonly = readonly
        self.timeout = timeout
        self._version = version
        self._prepare = prepare
        self._lock = threading.RLock()
        self._db: sqlite3.Connection | None = None

    @property
    def missing(self) -> bool:
        """Whether this read-only store has no database yet: it reads as empty."""
        with self._lock:
            return self.readonly and self._db is None and not self.path.is_file()

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
                raise ControlError(
                    BUSY,
                    f"another change held the control store for over {self.timeout:g} "
                    "seconds: try again (with the same operation ID, a retry is safe)",
                ) from error
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
            db = sqlite3.connect(
                self.path.resolve().as_uri() + "?mode=ro",
                uri=True,
                timeout=self.timeout,
                isolation_level=None,
                check_same_thread=False,
            )
            (version,) = db.execute("PRAGMA user_version").fetchone()
            if version != self._version:
                db.close()
                raise ControlError(
                    INTERNAL, f"{self.path} is in store format {version}, not {self._version}"
                )
        else:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            db = sqlite3.connect(
                self.path, timeout=self.timeout, isolation_level=None, check_same_thread=False
            )
            try:
                self._open(db)
            except BaseException:
                db.close()
                raise
        self._db = db
        return db

    def _open(self, db: sqlite3.Connection) -> None:
        (mode,) = db.execute("PRAGMA journal_mode=WAL").fetchone()
        if str(mode).lower() != "wal":
            raise ControlError(INTERNAL, f"{self.path} cannot use write-ahead logging")
        db.execute("PRAGMA synchronous=FULL")
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("BEGIN IMMEDIATE")
        try:
            self._prepare(db)
            db.execute("COMMIT")
        finally:
            if db.in_transaction:
                db.execute("ROLLBACK")
