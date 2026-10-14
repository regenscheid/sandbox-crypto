# SPDX-License-Identifier: MIT
"""Control-private, bounded immutable notification configuration identities.

No plaintext credential or unkeyed credential digest is persisted. All methods
are synchronous and must run in cancellation-owned work before their owner exits.
"""

from __future__ import annotations

import contextlib
import fcntl
import hmac
import json
import os
import secrets
import sqlite3
import stat
import time
from collections.abc import Generator
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from pydantic import TypeAdapter

from sandbox_science.contracts import Identifier, new_id

MAX_BINDINGS = 256
MAX_BYTES = 128 * 1024
_ID = TypeAdapter(Identifier)
_KEY = "kernel-notice.key"
_DATABASE = "kernel-notice-bindings.sqlite3"
_LOCK = "kernel-notice-bindings.lock"


class BindingError(ValueError):
    """Fixed local refusal without credentials, peer text or private paths."""


def _valid_endpoint(base_url: str) -> bool:
    """An http(s) worker URL with a host and no credentials, query or fragment."""
    if not 1 <= len(base_url) <= 2048 or any(not 32 < ord(c) < 127 for c in base_url):
        return False
    try:
        url = urlsplit(base_url)
        _ = url.port
    except ValueError:
        return False
    return (
        url.scheme in {"http", "https"}
        and bool(url.hostname)
        and url.username is None
        and url.password is None
        and not url.query
        and not url.fragment
    )


@dataclass(frozen=True)
class WorkerConfiguration:
    """Trusted in-memory client configuration, never a transcript/API value."""

    endpoint: str = field(repr=False)
    authorization: str = field(repr=False)

    def encoded(self) -> bytes:
        if (
            type(self.authorization) is not str
            or not 1 <= len(self.authorization) <= 16_384
            or type(self.endpoint) is not str
            or not 1 <= len(self.endpoint) <= 2048
            or any(character in self.authorization for character in ("\0", "\r", "\n"))
        ):
            raise BindingError("invalid trusted notification configuration")
        try:
            if not _valid_endpoint(self.endpoint):
                raise BindingError("invalid trusted notification configuration")
            if len(self.authorization.encode("utf-8", "strict")) > 16_384:
                raise BindingError("invalid trusted notification configuration")
        except (UnicodeError, TypeError, ValueError):
            raise BindingError("invalid trusted notification configuration") from None
        return json.dumps(
            {"version": 1, "endpoint": self.endpoint, "authorization": self.authorization},
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("ascii")


@dataclass(frozen=True)
class BindingPin:
    """Public opaque ID; private keyed pin stays in the control registry only."""

    binding_id: str


class KernelBindings:
    """One immutable worker-config binding per logical project/session, no reset."""

    def __init__(self, state_dir: Path) -> None:
        self.root = state_dir

    @staticmethod
    def _file(root: int, name: str, *, create: bool = False) -> int:
        flags = os.O_RDWR if create else os.O_RDONLY
        descriptor = os.open(
            name,
            flags | os.O_NOFOLLOW | os.O_NONBLOCK | (os.O_CREAT | os.O_EXCL if create else 0),
            0o600,
            dir_fd=root,
        )
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o600
        ):
            os.close(descriptor)
            raise BindingError("unsafe notification state file")
        return descriptor

    @contextlib.contextmanager
    def _locked(self) -> Generator[int]:
        root = lock = -1
        try:
            root = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                lock = self._file(root, _LOCK, create=True)
                os.fsync(lock)
                os.fsync(root)
            except FileExistsError:
                lock = self._file(root, _LOCK)
            deadline = time.monotonic() + 0.25
            while True:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise
                    time.sleep(0.005)
            yield root
        except (OSError, sqlite3.Error, UnicodeError, TypeError, ValueError):
            raise BindingError("notification configuration unavailable") from None
        finally:
            if lock >= 0:
                os.close(lock)
            if root >= 0:
                os.close(root)

    def _key(self, root: int, *, existing: bool) -> bytes:
        descriptor = -1
        try:
            try:
                descriptor = self._file(root, _KEY)
            except FileNotFoundError:
                if existing:
                    raise BindingError("notification key unavailable") from None
                descriptor = self._file(root, _KEY, create=True)
                value = secrets.token_bytes(32)
                os.write(descriptor, value)
                os.fsync(descriptor)
                os.fsync(root)
            if os.fstat(descriptor).st_size != 32:
                raise BindingError("notification key unavailable")
            value = os.pread(descriptor, 33, 0)
            if len(value) != 32:
                raise BindingError("notification key unavailable")
            return value
        finally:
            if descriptor >= 0:
                os.close(descriptor)

    @staticmethod
    def _budget(db: sqlite3.Connection) -> None:
        count, size, bad = db.execute(
            "SELECT count(*),coalesce(sum(length(CAST(project AS BLOB))+"
            "length(CAST(session AS BLOB))+length(CAST(binding AS BLOB))+"
            "length(CAST(pin AS BLOB))+160),0),"
            "coalesce(sum(CASE WHEN typeof(project)!='text' OR length(CAST(project AS BLOB))>128 "
            "OR typeof(session)!='text' OR length(CAST(session AS BLOB))>128 "
            "OR typeof(binding)!='text' OR length(CAST(binding AS BLOB))>128 "
            "OR typeof(pin)!='text' OR length(CAST(pin AS BLOB))!=64 "
            "OR (diagnostic IS NOT NULL AND (typeof(diagnostic)!='text' "
            "OR diagnostic NOT IN('unknown_lineage','unknown_revision','binding_changed',"
            "'identity_changed','capacity'))) THEN 1 ELSE 0 END),0) FROM bindings"
        ).fetchone()
        if bad or count > MAX_BINDINGS or size + 256 > MAX_BYTES:
            raise BindingError("notification configuration capacity unavailable")

    @contextlib.contextmanager
    def _db(self, root: int) -> Generator[tuple[sqlite3.Connection, bytes]]:
        descriptor = -1
        db: sqlite3.Connection | None = None
        created = False
        try:
            try:
                descriptor = self._file(root, _DATABASE)
            except FileNotFoundError:
                retained_names = (
                    _KEY,
                    "kernel-notices.sqlite3",
                    *(
                        name + suffix
                        for name in (_DATABASE, "kernel-notices.sqlite3")
                        for suffix in ("-wal", "-shm", "-journal")
                    ),
                )
                for retained in retained_names:
                    try:
                        os.stat(retained, dir_fd=root, follow_symlinks=False)
                    except FileNotFoundError:
                        continue
                    raise BindingError(
                        "notification registry lost retained initialization"
                    ) from None
                descriptor = self._file(root, _DATABASE, create=True)
                created = True
                os.fsync(root)
            key = self._key(root, existing=not created)
            for suffix in ("-wal", "-shm", "-journal"):
                try:
                    sidecar = self._file(root, _DATABASE + suffix)
                except FileNotFoundError:
                    continue
                os.close(sidecar)
            identity = os.fstat(descriptor)
            db = sqlite3.connect(self.root / _DATABASE, timeout=0, isolation_level=None)
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA synchronous=FULL")
            db.execute("BEGIN IMMEDIATE")
            if created:
                db.execute(
                    "CREATE TABLE meta(singleton INTEGER PRIMARY KEY CHECK(singleton=1),"
                    "version INTEGER NOT NULL,key_check TEXT NOT NULL,"
                    "inbox_phase TEXT NOT NULL,inbox_device INTEGER,inbox_inode INTEGER) STRICT"
                )
                db.execute(
                    "CREATE TABLE bindings(project TEXT NOT NULL,session TEXT NOT NULL,"
                    "binding TEXT NOT NULL,pin TEXT NOT NULL,diagnostic TEXT,"
                    "PRIMARY KEY(project,session)) STRICT"
                )
                db.execute(
                    "CREATE TRIGGER bindings_immutable BEFORE UPDATE OF "
                    "project,session,binding,pin "
                    "ON bindings BEGIN SELECT RAISE(ABORT,'notification binding is immutable'); END"
                )
                db.execute(
                    "CREATE TRIGGER bindings_no_delete BEFORE DELETE ON bindings "
                    "BEGIN SELECT RAISE(ABORT,'notification binding is immutable'); END"
                )
                db.execute(
                    "INSERT INTO meta VALUES(1,2,?,'new',NULL,NULL)",
                    (hmac.digest(key, b"kernel-notice-key-v1\0", "sha256").hex(),),
                )
            saved = db.execute(
                "SELECT CASE WHEN typeof(version)='integer' THEN version END,"
                "CASE WHEN typeof(key_check)='text' AND length(CAST(key_check AS BLOB))=64 "
                "THEN key_check END,CASE WHEN inbox_phase IN('new','initializing') "
                "AND inbox_device IS NULL AND inbox_inode IS NULL THEN inbox_phase "
                "WHEN inbox_phase='ready' AND typeof(inbox_device)='integer' "
                "AND inbox_device>=0 AND typeof(inbox_inode)='integer' AND inbox_inode>0 "
                "THEN inbox_phase END,"
                "CASE WHEN typeof(inbox_device)='integer' THEN inbox_device END,"
                "CASE WHEN typeof(inbox_inode)='integer' THEN inbox_inode END FROM meta LIMIT 2"
            ).fetchall()
            expected = hmac.digest(key, b"kernel-notice-key-v1\0", "sha256").hex()
            if (
                len(saved) != 1
                or saved[0][:2] != (2, expected)
                or saved[0][2] not in ("new", "initializing", "ready")
            ):
                raise BindingError("notification key or schema unavailable")
            self._budget(db)
            yield db, key
            self._budget(db)
            after = os.stat(_DATABASE, dir_fd=root, follow_symlinks=False)
            parent = os.stat(self.root, follow_symlinks=False)
            if (identity.st_dev, identity.st_ino) != (after.st_dev, after.st_ino) or (
                os.fstat(root).st_dev,
                os.fstat(root).st_ino,
            ) != (parent.st_dev, parent.st_ino):
                raise BindingError("notification state identity changed")
            db.execute("COMMIT")
            os.fsync(root)
        except BaseException:
            if db is not None and db.in_transaction:
                db.execute("ROLLBACK")
            raise
        finally:
            if db is not None:
                db.close()
            if descriptor >= 0:
                os.close(descriptor)

    @contextlib.contextmanager
    def inbox(self) -> Generator[tuple[int, bool]]:
        """Own first initialization or reopen its exact original private file.

        The in-progress phase commits before file/schema creation. Interrupted
        initialization stays held; it is never inferred from whatever survived.
        The caller performs first-only schema work while this lock is retained.
        """
        descriptor = -1
        try:
            with self._locked() as root:
                with self._db(root) as (db, _):
                    phase, device, inode = db.execute(
                        "SELECT inbox_phase,inbox_device,inbox_inode FROM meta"
                    ).fetchone()
                    if phase == "initializing":
                        raise BindingError("notification inbox initialization incomplete")
                    first = phase == "new"
                    if first:
                        for name in (
                            "kernel-notices.sqlite3",
                            *(
                                "kernel-notices.sqlite3" + suffix
                                for suffix in ("-wal", "-shm", "-journal")
                            ),
                        ):
                            try:
                                os.stat(name, dir_fd=root, follow_symlinks=False)
                            except FileNotFoundError:
                                continue
                            raise BindingError("notification inbox has unproved retained state")
                        db.execute("UPDATE meta SET inbox_phase='initializing'")
                # The first-init state above is now durable before any creation.
                descriptor = self._file(root, "kernel-notices.sqlite3", create=first)
                before = os.fstat(descriptor)
                if not first and (
                    (before.st_dev, before.st_ino) != (device, inode)
                    or before.st_size < 100
                    or os.pread(descriptor, 16, 0) != b"SQLite format 3\x00"
                ):
                    raise BindingError("notification original inbox unavailable")
                yield descriptor, first
                after = os.stat("kernel-notices.sqlite3", dir_fd=root, follow_symlinks=False)
                if (
                    (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino)
                    or os.fstat(descriptor).st_size < 100
                    or os.pread(descriptor, 16, 0) != b"SQLite format 3\x00"
                ):
                    raise BindingError("notification original inbox unavailable")
                if first:
                    os.fsync(descriptor)
                    os.fsync(root)
                    with self._db(root) as (db, _):
                        db.execute(
                            "UPDATE meta SET inbox_phase='ready',inbox_device=?,inbox_inode=? "
                            "WHERE inbox_phase='initializing'",
                            (before.st_dev, before.st_ino),
                        )
        finally:
            if descriptor >= 0:
                os.close(descriptor)

    def ensure(self, project: str, session: str, configuration: WorkerConfiguration) -> BindingPin:
        """Enroll once or prove the same logical binding; changed config stays held."""
        for value in (project, session):
            _ID.validate_python(value, strict=True)
        raw = configuration.encoded()
        mismatch = False
        with self._locked() as root, self._db(root) as (db, key):
            row = db.execute(
                "SELECT substr(CAST(binding AS BLOB),1,129),substr(CAST(pin AS BLOB),1,65) "
                "FROM bindings WHERE project=? AND session=?",
                (project, session),
            ).fetchone()
            if row is None:
                if db.execute("SELECT count(*) FROM bindings").fetchone()[0] >= MAX_BINDINGS:
                    raise BindingError("notification configuration capacity unavailable")
                identifier = new_id("knb")
                pin = self._pin(key, project, session, identifier, raw)
                db.execute(
                    "INSERT INTO bindings VALUES(?,?,?,?,NULL)", (project, session, identifier, pin)
                )
            else:
                identifier = _ID.validate_python(row[0].decode("ascii"), strict=True)
                retained = row[1].decode("ascii")
                if len(retained) != 64 or any(char not in "0123456789abcdef" for char in retained):
                    raise BindingError("notification configuration unavailable")
                pin = self._pin(key, project, session, identifier, raw)
                if not hmac.compare_digest(retained, pin):
                    mismatch = True
                    db.execute(
                        "UPDATE bindings SET diagnostic='binding_changed' "
                        "WHERE project=? AND session=?",
                        (project, session),
                    )
        if mismatch:
            raise BindingError("notification configuration changed")
        return BindingPin(identifier)

    @staticmethod
    def _pin(key: bytes, project: str, session: str, identifier: str, raw: bytes) -> str:
        scope = json.dumps([project, session, identifier], separators=(",", ":")).encode("ascii")
        return hmac.digest(key, b"kernel-notice-config-v1\0" + scope + b"\0" + raw, "sha256").hex()

    def diagnostic(self, project: str, session: str, reason: str) -> None:
        """One fixed diagnostic on an already enrolled logical binding, never a queue."""
        if reason not in {
            "unknown_lineage",
            "unknown_revision",
            "binding_changed",
            "identity_changed",
            "capacity",
        }:
            raise BindingError("invalid notification diagnostic")
        with self._locked() as root, self._db(root) as (db, _):
            db.execute(
                "UPDATE bindings SET diagnostic=? WHERE project=? AND session=?",
                (reason, project, session),
            )
