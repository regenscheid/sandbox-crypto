# SPDX-License-Identifier: MIT
"""Published artifact bytes, stored once under their SHA-256."""

from __future__ import annotations

import contextlib
import hashlib
import os
import re
import tempfile
from pathlib import Path

_DIGEST = re.compile(r"[0-9a-f]{64}")


class Blobs:
    """A content-addressed directory: ``blobs/ab/abcd…``."""

    def __init__(self, root: Path) -> None:
        self.root = root
        (root / "tmp").mkdir(parents=True, exist_ok=True)

    def path(self, sha256: str) -> Path:
        # A digest names a file here, so it must be one: never a path of its own.
        if not _DIGEST.fullmatch(sha256):
            raise ValueError("not a SHA-256 digest")
        return self.root / "blobs" / sha256[:2] / sha256

    def writer(self) -> BlobWriter:
        return BlobWriter(self)


class BlobWriter:
    """Collects a blob in a temporary file and moves it into place once its hash checks."""

    def __init__(self, blobs: Blobs) -> None:
        self._blobs = blobs
        descriptor, name = tempfile.mkstemp(dir=blobs.root / "tmp")
        self._file = os.fdopen(descriptor, "wb")
        self._path = Path(name)
        self._digest = hashlib.sha256()
        self.size = 0

    def write(self, data: bytes) -> None:
        self._file.write(data)
        self._digest.update(data)
        self.size += len(data)

    def commit(self, expected_sha256: str) -> bool:
        """Store the blob if its hash is ``expected_sha256``; return whether it was.

        The blob is on disk before this returns, so a record made afterwards never names
        a blob that a crash left empty or torn.
        """
        self._file.flush()
        os.fsync(self._file.fileno())
        self._file.close()
        sha256 = self._digest.hexdigest()
        if sha256 != expected_sha256:
            self.discard()
            return False
        target = self._blobs.path(sha256)
        target.parent.mkdir(parents=True, exist_ok=True)
        os.replace(self._path, target)
        directory = os.open(target.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        return True

    def discard(self) -> None:
        with contextlib.suppress(OSError):
            self._file.close()
        with contextlib.suppress(OSError):
            self._path.unlink()
