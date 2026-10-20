# SPDX-License-Identifier: MIT
"""Admitted original-upload scratch and process-safe ownership of exact retries."""

from __future__ import annotations

import fcntl
import hashlib
import os
import sqlite3
import tempfile
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import TYPE_CHECKING, BinaryIO, Self

from .artifact_blobs import CHUNK_BYTES, Blob
from .documents import DocumentError

if TYPE_CHECKING:
    from .uploads import UserUpload, UserUploads


@contextmanager
def storage_errors() -> Generator[None]:
    """Storage failures are retryable; scope, identity and quota refusals keep their kind."""
    message = "original upload storage is unavailable; retry the same upload ID"
    try:
        yield
    except DocumentError as error:
        if error.kind != "internal":
            raise
        raise DocumentError("unavailable", message) from None
    except (OSError, sqlite3.DatabaseError):
        raise DocumentError("unavailable", message) from None


@dataclass(frozen=True)
class UploadAdmission:
    """The durable reservation identity; its lease outlives actual owned I/O."""

    project: str
    session: str
    upload_id: str
    identity: str
    capacity: int
    sha256: str | None


def acquire(root: Path, project: str, session: str, upload_id: str) -> BinaryIO:
    """Never unlink these stable lock files; use a local/block-backed filesystem."""
    directory = root / "upload-locks"
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    # A bounded set of stable inodes also covers discarded/cancelled upload IDs.
    # Collisions refuse another live owner; no lock inode is ever unlinked.
    name = hashlib.sha256(f"{project}\0{session}\0{upload_id}".encode()).hexdigest()[:3]
    path = directory / name
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        current, held = path.stat(follow_symlinks=False), os.fstat(descriptor)
        if (current.st_dev, current.st_ino) != (held.st_dev, held.st_ino):
            raise DocumentError("unavailable", "upload ownership changed; retry the file")
        return os.fdopen(descriptor, "rb")
    except BlockingIOError:
        os.close(descriptor)
        raise DocumentError("unavailable", "another request still owns this upload") from None
    except BaseException:
        os.close(descriptor)
        raise


class UploadWriter:
    """One caller's bounded scratch, exact digest pin, blob install and receipt."""

    def __init__(
        self,
        uploads: UserUploads,
        admission: UploadAdmission,
        lease: BinaryIO,
        filename: str,
        media: str,
        expected_size: int | None,
    ) -> None:
        self.uploads, self.admission, self.lease = uploads, admission, lease
        self.filename, self.media, self.expected_size = filename, media, expected_size
        self.stream = tempfile.TemporaryFile(mode="w+b")  # noqa: SIM115 - discard owns the admitted writer's lifetime
        self.size, self.digest = 0, hashlib.sha256()
        self.installed = False
        self.result: UserUpload | None = None

    def write(self, data: bytes) -> None:
        """Append one bounded HTTP chunk; oversized/closed writes fail before I/O."""
        if self.stream.closed or self.result is not None:
            raise DocumentError("conflict", "the upload writer has already finished")
        bound = self.expected_size if self.expected_size is not None else self.admission.capacity
        if self.size + len(data) > bound:
            raise DocumentError("invalid_request", "upload exceeds its admitted byte limit")
        with storage_errors():
            self.stream.write(data)
        self.digest.update(data)
        self.size += len(data)

    def commit(self) -> UserUpload:
        """Pin before installation; only verified durable bytes can receive metadata."""
        with storage_errors():
            return self._commit()

    def _commit(self) -> UserUpload:
        if self.result is not None:
            return self.result
        if self.stream.closed:
            raise DocumentError("conflict", "the upload writer has already finished")
        if self.expected_size is not None and self.size != self.expected_size:
            raise DocumentError("invalid_request", "upload length differs from its declaration")
        blob = Blob(self.digest.hexdigest(), self.size)
        previous = self.uploads.pin(self.admission, self.filename, self.media, blob)
        if previous is not None:
            self.stream.close()
            self.uploads.release(self.admission)
            self.result = previous
            return previous
        writer = self.uploads.blobs.writer(blob)
        try:
            self.stream.seek(0)
            while chunk := self.stream.read(CHUNK_BYTES):
                writer.write(chunk)
            self.stream.close()  # no duplicate receive scratch during remote I/O
            try:
                writer.commit()
            finally:
                # A failed/unknown remote request is not proof no bytes were installed.
                self.installed = writer.installed
            self.result = self.uploads.finish(self.admission, self.filename, self.media, blob)
            return self.result
        finally:
            writer.discard()

    def discard(self) -> None:
        """Drain callers first; retain prior unknown or newly installed reservations."""
        with storage_errors():
            try:
                self.stream.close()
            finally:
                if not self.lease.closed:
                    try:
                        if (
                            self.result is None
                            and not self.installed
                            and self.admission.sha256 is None
                        ):
                            self.uploads.release(self.admission)
                    finally:
                        self.lease.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        kind: type[BaseException] | None,
        error: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.discard()
