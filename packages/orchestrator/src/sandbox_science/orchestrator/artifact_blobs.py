# SPDX-License-Identifier: MIT
"""Durable control-owned artifact bytes, independent of research containers."""

from __future__ import annotations

import contextlib
import hashlib
import os
import re
import stat
import tempfile
from collections.abc import Generator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, BinaryIO, Final

from sandbox_science.contracts.artifact_inventory import MAX_ARTIFACT_BYTES

from .artifact_config import load
from .documents.errors import INTERNAL, DocumentError

if TYPE_CHECKING:
    from .artifact_s3 import S3Backing

CHUNK_BYTES: Final = 512 * 1024
INLINE_BYTES: Final = 8 * 1024 * 1024
_SHA = re.compile(r"[0-9a-f]{64}")


def _sync(directory: Path) -> None:
    descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _directory(directory: Path) -> None:
    if not directory.exists():
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        _sync(directory.parent)


@dataclass(frozen=True)
class Blob:
    sha256: str
    size: int

    def __post_init__(self) -> None:
        if _SHA.fullmatch(self.sha256) is None or (
            type(self.size) is not int or not 0 <= self.size <= MAX_ARTIFACT_BYTES
        ):
            raise ValueError("invalid artifact hash or size")


class ArtifactBlobs:
    """Hash-named files adjacent to a control DB; reads never create directories."""

    def __init__(self, database: Path) -> None:
        self.root = database.with_name(database.name + ".artifacts")

    @property
    def backing(self) -> S3Backing | None:
        """Re-read settings so a long-lived publisher sees newly installed backing."""
        from .artifact_s3 import S3Backing

        try:
            settings = load(self.root)
        except (OSError, ValueError):
            # Validation errors can quote a mistakenly supplied credential field.
            raise DocumentError(INTERNAL, "artifact backing configuration is invalid") from None
        return S3Backing(settings) if settings is not None else None

    def path(self, sha256: str) -> Path:
        if _SHA.fullmatch(sha256) is None:
            raise ValueError("invalid artifact hash")
        return self.root / "blobs" / sha256[:2] / sha256

    def writer(self, expected: Blob) -> BlobWriter:
        return BlobWriter(self, expected)

    def prefix(self, blob: Blob, limit: int) -> bytes:
        """A bounded provenance preview of verified-ingest bytes, not a full read/hash.

        Ledger comparisons already use a prefix; they must not scan a GiB while
        holding their SQL transaction. Full-content consumers use ``open``.
        """
        if type(limit) is not int or not 0 <= limit <= INLINE_BYTES:
            raise ValueError("invalid artifact prefix bound")
        try:
            backing = self.backing
            try:
                descriptor = os.open(self.path(blob.sha256), os.O_RDONLY | os.O_NOFOLLOW)
            except FileNotFoundError:
                if backing is None:
                    raise
                return backing.prefix(blob, limit)
            with os.fdopen(descriptor, "rb") as stream:
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_size != blob.size:
                    raise ValueError("artifact blob size/type differs")
                return stream.read(limit)
        except (OSError, ValueError) as error:
            raise DocumentError(INTERNAL, "artifact provenance prefix unavailable") from error

    @contextmanager
    def open(self, blob: Blob) -> Generator[BinaryIO]:
        """Verify exact size/hash before yielding a pinned descriptor at offset zero."""
        with ExitStack() as stack:
            try:
                backing = self.backing
                try:
                    descriptor = os.open(self.path(blob.sha256), os.O_RDONLY | os.O_NOFOLLOW)
                except FileNotFoundError:
                    if backing is None:
                        raise
                    stream = stack.enter_context(tempfile.TemporaryFile(mode="w+b"))
                    backing.copy(blob, stream)
                    stream.seek(0)
                else:
                    stream = stack.enter_context(os.fdopen(descriptor, "rb"))
                    self._verify_local(stream, blob)
            except (OSError, ValueError) as error:
                raise DocumentError(
                    INTERNAL, "retained artifact blob is unavailable or corrupt"
                ) from error
            yield stream

    @staticmethod
    def _verify_local(stream: BinaryIO, blob: Blob) -> None:
        """Verify the original descriptor before yielding any bytes."""
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_size != blob.size:
            raise ValueError("artifact blob size/type differs")
        digest = hashlib.sha256()
        while chunk := stream.read(CHUNK_BYTES):
            digest.update(chunk)
        after = os.fstat(stream.fileno())
        if digest.hexdigest() != blob.sha256 or (
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
        ) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns):
            raise ValueError("artifact blob hash changed")
        stream.seek(0)

    def archive(self, blob: Blob) -> None:
        """Make a verified remote copy; never remove the local source."""
        backing = self.backing
        if backing is None:
            raise ValueError("artifact S3 backing is not configured")
        try:
            with self.open(blob) as stream:
                backing.retain(blob, stream)
        except (OSError, ValueError) as error:
            raise DocumentError(
                INTERNAL, "artifact archival failed; local content retained"
            ) from error

    def evict(self, blob: Blob, *, apply: bool = False) -> bool:
        """Verify both copies, then optionally remove only this local hash-named file."""
        backing = self.backing
        if backing is None:
            raise ValueError("artifact S3 backing is not configured")
        path = self.path(blob.sha256)
        if not path.exists():
            backing.copy(blob)
            return False
        with self.open(blob) as stream:
            backing.copy(blob)
            if apply:
                original, current = os.fstat(stream.fileno()), path.lstat()
                if (original.st_dev, original.st_ino) != (current.st_dev, current.st_ino):
                    raise ValueError("local artifact changed during eviction")
                path.unlink()
                _sync(path.parent)
        return True


class BlobWriter:
    """Bounded staged ingestion; installing content never overwrites an existing blob."""

    def __init__(self, store: ArtifactBlobs, expected: Blob) -> None:
        self.store, self.expected = store, expected
        store.path(expected.sha256)  # validate before creating anything
        _directory(store.root)
        _directory(store.root / "staging")
        descriptor, name = tempfile.mkstemp(dir=store.root / "staging")
        self.path = Path(name)
        self.stream = os.fdopen(descriptor, "wb")
        self.size = 0
        self.digest = hashlib.sha256()
        self.installed = False

    def write(self, data: bytes) -> None:
        if self.size + len(data) > self.expected.size:
            raise ValueError("artifact exceeds pinned size")
        self.stream.write(data)
        self.digest.update(data)
        self.size += len(data)

    def commit(self) -> Blob:
        if (self.size, self.digest.hexdigest()) != (self.expected.size, self.expected.sha256):
            raise ValueError("artifact size/hash differs")
        self.stream.flush()
        os.fchmod(self.stream.fileno(), 0o400)
        os.fsync(self.stream.fileno())
        self.stream.close()
        target = self.store.path(self.expected.sha256)
        _directory(target.parent.parent)
        _directory(target.parent)
        try:
            os.link(self.path, target)
            self.installed = True
        except FileExistsError:
            with self.store.open(self.expected):
                pass  # deduplication never trusts only the existing filename
            self.installed = True
        _sync(target.parent)
        self.discard()
        # Installation precedes remote I/O: uncertainty must keep the publisher's
        # durable reservation and the installed local source.
        if self.store.backing is not None:
            self.store.archive(self.expected)
        return self.expected

    def discard(self) -> None:
        with contextlib.suppress(OSError):
            self.stream.close()
        with contextlib.suppress(FileNotFoundError):
            self.path.unlink()
