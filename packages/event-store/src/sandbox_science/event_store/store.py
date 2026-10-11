# SPDX-License-Identifier: MIT
"""Records in one SQLite database, each chained to the one before.

Durability: write-ahead logging with ``synchronous=FULL`` syncs the log at
every commit, so ``append`` returns only once its record would survive a crash
or a power loss. ``fullfsync`` makes macOS flush the drive's cache as well; it
has no effect elsewhere.

Concurrency: a commit reads the newest record and inserts the next ones inside
one write transaction (``BEGIN IMMEDIATE``), so SQLite's write lock serializes
commits and the chain cannot fork, whether appends come from threads, asyncio
tasks or other processes on the same host. The newest record is read again for
every commit rather than cached, because another process may have appended in
between.

Group commit: within one ``EventStore``, appends that arrive while a commit is
under way wait for it to finish, and the next of them commits them all in one
transaction. A sync to disk takes milliseconds, so this keeps concurrent
appends from queueing behind one sync each. Every append still returns only
once its own record is committed.
"""

from __future__ import annotations

import asyncio
import sqlite3
import threading
import time
from collections.abc import Callable, Generator, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import astuple, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType
from typing import Final, Self

from .canonical import canonical_payload
from .errors import CheckpointError, EventStoreError
from .records import (
    GENESIS,
    MAX_SEQ,
    Checkpoint,
    Record,
    check_name,
    format_time,
    record_from_row,
    record_hash,
)

DEFAULT_TIMEOUT: Final = 5.0
"""Seconds an append waits for the store's locks before it fails."""

SCHEMA_VERSION: Final = 1
"""``PRAGMA user_version`` of a store in this format."""

_SCHEMA: Final = (
    """CREATE TABLE events (
    seq INTEGER PRIMARY KEY,
    time TEXT NOT NULL,
    kind TEXT NOT NULL,
    source TEXT NOT NULL,
    payload TEXT NOT NULL,
    prev_hash TEXT NOT NULL,
    hash TEXT NOT NULL
) STRICT""",
    # These guard against mistakes, not against anyone who can write the file,
    # who can drop them. Verification is what detects changes.
    """CREATE TRIGGER events_no_update BEFORE UPDATE ON events
BEGIN SELECT RAISE(ABORT, 'the event store is append-only'); END""",
    """CREATE TRIGGER events_no_delete BEFORE DELETE ON events
BEGIN SELECT RAISE(ABORT, 'the event store is append-only'); END""",
)
_TRIGGERS: Final = ("events_no_delete", "events_no_update")
_SELECT: Final = "SELECT seq, time, kind, source, payload, prev_hash, hash FROM events"
_HEAD: Final = "SELECT seq, hash FROM events ORDER BY seq DESC LIMIT 1"
_INSERT: Final = (
    "INSERT INTO events (seq, time, kind, source, payload, prev_hash, hash)"
    " VALUES (?, ?, ?, ?, ?, ?, ?)"
)


def _now() -> datetime:
    return datetime.now(UTC)


@dataclass(eq=False, slots=True)
class _Append:
    """An append waiting for a commit, and its outcome once ``done`` is set."""

    kind: str
    source: str
    payload: str
    deadline: float  # time.monotonic() by which it must have the locks
    done: threading.Event = field(default_factory=threading.Event)
    record: Record | None = None
    error: EventStoreError | None = None


class EventStore:
    """Append records to the store at ``path``, creating it if it does not exist.

    An append fails if it cannot get this object's lock and SQLite's write lock
    within ``timeout`` seconds, so it returns within that time plus the time
    its commit takes.
    """

    def __init__(
        self,
        path: Path,
        *,
        timeout: float = DEFAULT_TIMEOUT,
        clock: Callable[[], datetime] = _now,
    ) -> None:
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        self._path = path
        self._timeout = timeout
        self._clock = clock
        self._lock = threading.Lock()  # held while using the connection
        self._waiting: list[_Append] = []  # appends for the next commit
        self._waiting_lock = threading.Lock()
        self._closed = False
        self._db: sqlite3.Connection | None = _open_writer(path, timeout)

    @property
    def path(self) -> Path:
        """The database file."""
        return self._path

    def append(self, kind: str, source: str, payload: Mapping[str, object]) -> Record:
        """Append a record and return it once it is committed to disk.

        ``kind`` and ``source`` are lowercase names (``alert``,
        ``policy-middleware``); ``payload`` is a JSON object without floats.
        Raises RecordError for invalid values and EventStoreError if the record
        could not be stored.
        """
        return self._append(
            check_name(kind, "kind"), check_name(source, "source"), canonical_payload(payload)
        )

    async def append_async(self, kind: str, source: str, payload: Mapping[str, object]) -> Record:
        """``append`` for asyncio code: the write runs in a thread, not on the event loop.

        The payload is copied before the first await, so later changes to it do
        not matter. A cancelled append may still be stored.
        """
        text = canonical_payload(payload)
        kind = check_name(kind, "kind")
        source = check_name(source, "source")
        return await asyncio.to_thread(self._append, kind, source, text)

    def head(self) -> Checkpoint:
        """The newest record's sequence number and hash."""
        with self._locked() as db:
            try:
                row = db.execute(_HEAD).fetchone()
            except sqlite3.Error as exc:
                raise EventStoreError(f"cannot read the event store {self._path}: {exc}") from exc
        return _head(row, self._path)

    def close(self) -> None:
        """Close the database; later appends fail."""
        with self._lock:
            self._closed = True
            if self._db is not None:
                self._db.close()
                self._db = None

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    def _append(self, kind: str, source: str, payload: str) -> Record:
        """Queue one append and return once a commit has stored it (group commit)."""
        append = _Append(kind, source, payload, time.monotonic() + self._timeout)
        with self._waiting_lock:
            self._waiting.append(append)
        # Whoever holds the lock commits everything waiting. Holding it, an
        # append is therefore either done or still waiting, never in between.
        while not append.done.is_set():
            if self._lock.acquire(timeout=max(0.0, append.deadline - time.monotonic())):
                try:
                    if not append.done.is_set():
                        with self._waiting_lock:
                            batch, self._waiting = self._waiting, []
                        self._commit(batch)
                finally:
                    self._lock.release()
                continue
            with self._waiting_lock:
                if append in self._waiting:
                    self._waiting.remove(append)
                    raise EventStoreError(
                        f"timed out after {self._timeout} s waiting for the event store "
                        f"{self._path}"
                    )
            append.done.wait()  # a commit under way has taken it
        if append.error is not None:
            raise append.error
        if append.record is None:  # pragma: no cover - done means one or the other
            raise EventStoreError("an append finished without a record")
        return append.record

    def _commit(self, batch: list[_Append]) -> None:
        """Store a batch in one transaction, and give each append its outcome."""
        try:
            records = self._insert(self._connection(), batch)
        except BaseException as exc:
            message = f"cannot append to the event store {self._path}: {exc}"
            if isinstance(exc, EventStoreError):
                message = str(exc)
            for append in batch:
                # One exception each: they are raised in different threads.
                error = EventStoreError(message)
                error.__cause__ = exc
                append.error = error
                append.done.set()
            if not isinstance(exc, Exception):
                raise
            return
        for append, record in zip(batch, records, strict=True):
            append.record = record
            append.done.set()

    def _insert(self, db: sqlite3.Connection, batch: list[_Append]) -> list[Record]:
        # Wait for SQLite's lock only until the first deadline in the batch,
        # so that no append in it waits past its own.
        wait = min(append.deadline for append in batch) - time.monotonic()
        db.execute(f"PRAGMA busy_timeout = {max(1, int(wait * 1000))}")
        try:
            db.execute("BEGIN IMMEDIATE")
            head = _head(db.execute(_HEAD).fetchone(), self._path)
            records: list[Record] = []
            for append in batch:
                if head.seq >= MAX_SEQ:
                    raise EventStoreError(f"the event store {self._path} is full")
                seq = head.seq + 1
                # Taken under the write lock, so times follow the sequence
                # unless the system clock is set back.
                moment = format_time(self._clock())
                hash_ = record_hash(
                    prev_hash=head.hash,
                    seq=seq,
                    time=moment,
                    kind=append.kind,
                    source=append.source,
                    payload=append.payload,
                )
                record = Record(
                    seq, moment, append.kind, append.source, append.payload, head.hash, hash_
                )
                db.execute(_INSERT, astuple(record))
                records.append(record)
                head = Checkpoint(seq, hash_)
            db.execute("COMMIT")
        except BaseException:
            self._abandon(db)
            raise
        return records

    def _connection(self) -> sqlite3.Connection:
        """The open connection; call with the lock held."""
        if self._closed:
            raise EventStoreError(f"the event store {self._path} is closed")
        if self._db is None:
            self._db = _open_writer(self._path, self._timeout)
        return self._db

    def _abandon(self, db: sqlite3.Connection) -> None:
        """Roll back a failed append; if even that fails, reopen the database next time."""
        if not db.in_transaction:
            return
        try:
            db.execute("ROLLBACK")
        except sqlite3.Error:
            # Closing discards what is left of the transaction.
            db.close()
            self._db = None

    @contextmanager
    def _locked(self) -> Generator[sqlite3.Connection, None, None]:
        """Hold this store's lock, waiting at most the timeout, and yield its connection."""
        if not self._lock.acquire(timeout=self._timeout):
            raise EventStoreError(
                f"timed out after {self._timeout} s waiting for the event store {self._path}"
            )
        try:
            yield self._connection()
        finally:
            self._lock.release()


class StoreReader:
    """Read a store without writing to it, for verification and export."""

    def __init__(self, path: Path, *, timeout: float = DEFAULT_TIMEOUT) -> None:
        if not path.is_file():
            raise EventStoreError(f"no event store at {path}")
        self._path = path
        try:
            self._db = sqlite3.connect(
                path.resolve().as_uri() + "?mode=ro",
                uri=True,
                timeout=timeout,
                isolation_level=None,
            )
        except sqlite3.Error as exc:
            raise EventStoreError(f"cannot open the event store {path}: {exc}") from exc
        try:
            (version,) = self._db.execute("PRAGMA user_version").fetchone()
        except sqlite3.Error as exc:
            self._db.close()
            raise EventStoreError(f"cannot read the event store {path}: {exc}") from exc
        if version != SCHEMA_VERSION:
            self._db.close()
            raise EventStoreError(f"{path} is not an event store in format {SCHEMA_VERSION}")

    def head(self) -> Checkpoint:
        """The newest record's sequence number and hash."""
        try:
            row = self._db.execute(_HEAD).fetchone()
        except sqlite3.Error as exc:
            raise EventStoreError(f"cannot read the event store {self._path}: {exc}") from exc
        return _head(row, self._path)

    def rows(self, after: int | None = None) -> Iterator[tuple[object, ...]]:
        """Stored rows as they are, in sequence order, from one snapshot of the store.

        Without ``after``, every row, including any a tamperer numbered 0 or below.
        """
        query = _SELECT + " ORDER BY seq"
        parameters: tuple[int, ...] = ()
        if after is not None:
            query = _SELECT + " WHERE seq > ? ORDER BY seq"
            parameters = (after,)
        try:
            yield from self._db.execute(query, parameters)
        except sqlite3.Error as exc:
            raise EventStoreError(f"cannot read the event store {self._path}: {exc}") from exc

    def records(self, after: int = 0) -> Iterator[Record]:
        """Records after sequence number ``after``; raises RecordError at a malformed one."""
        for row in self.rows(after):
            yield record_from_row(row)

    def schema_problems(self) -> list[str]:
        """Ways the database's schema differs from a store's, such as dropped triggers."""
        try:
            objects = set(self._db.execute("SELECT type, name FROM sqlite_master"))
        except sqlite3.Error as exc:
            raise EventStoreError(f"cannot read the event store {self._path}: {exc}") from exc
        problems: list[str] = []
        if ("table", "events") not in objects:
            problems.append("the events table has been replaced")
        missing = [name for name in _TRIGGERS if ("trigger", name) not in objects]
        if missing:
            problems.append(f"append-only triggers were dropped: {', '.join(missing)}")
        return problems

    def close(self) -> None:
        """Close the database."""
        self._db.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()


def _open_writer(path: Path, timeout: float) -> sqlite3.Connection:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(path, timeout=timeout, isolation_level=None, check_same_thread=False)
    except (OSError, sqlite3.Error) as exc:
        raise EventStoreError(f"cannot open the event store {path}: {exc}") from exc
    try:
        _prepare(db, path)
    except sqlite3.Error as exc:
        db.close()
        raise EventStoreError(f"cannot open the event store {path}: {exc}") from exc
    except BaseException:
        db.close()
        raise
    return db


def _prepare(db: sqlite3.Connection, path: Path) -> None:
    """Set durability, and create the schema in a new database."""
    (mode,) = db.execute("PRAGMA journal_mode=WAL").fetchone()
    if str(mode).lower() != "wal":
        raise EventStoreError(f"{path} cannot use write-ahead logging (journal mode {mode})")
    db.execute("PRAGMA synchronous=FULL")
    db.execute("PRAGMA fullfsync=ON")
    db.execute("BEGIN IMMEDIATE")
    try:
        (version,) = db.execute("PRAGMA user_version").fetchone()
        if version == 0:
            (objects,) = db.execute("SELECT count(*) FROM sqlite_master").fetchone()
            if objects:
                raise EventStoreError(f"{path} is an SQLite database but not an event store")
            for statement in _SCHEMA:
                db.execute(statement)
            db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        elif version != SCHEMA_VERSION:
            raise EventStoreError(
                f"{path} is in event store format {version}; this version reads {SCHEMA_VERSION}"
            )
        db.execute("COMMIT")
    finally:
        if db.in_transaction:
            db.execute("ROLLBACK")


def _head(row: tuple[object, ...] | None, path: Path) -> Checkpoint:
    """The checkpoint for the newest row, ``(seq, hash)``, or for an empty store."""
    if row is None:
        return Checkpoint(0, GENESIS)
    seq, hash_ = row
    try:
        if isinstance(seq, int) and seq < 1:
            raise CheckpointError(f"sequence number {seq} is out of range")
        return Checkpoint(seq, hash_)  # pyright: ignore[reportArgumentType]
    except CheckpointError as exc:
        raise EventStoreError(
            f"the newest record in {path} is malformed ({exc}); verify the store"
        ) from exc
