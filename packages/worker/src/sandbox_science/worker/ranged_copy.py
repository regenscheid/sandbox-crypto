# SPDX-License-Identifier: MIT
"""Copying a file between two environments of the worker's, whatever its size.

A file within 100 MiB is read whole and written by one file write, as before. A larger
one goes in parts of at most 8 MiB (``PART_BYTES``, one read): each part is read
without hashing the whole file again, and written into a part file the target's
executor keeps outside the workspace; the last part's write names the whole file's
SHA-256 and size, which the executor checks before it gives the file its name, so a
file that changed while it was copied is never written, and its parts are discarded.
An upload's ID is derived from what is copied, so a copy a worker restart cut short
goes on from the bytes its part holds. A part's read or write is tried once more after
a transient failure; a copy that then gives up, or that its caller's deadline ends, has
its parts discarded by the caller, and a part nothing has written for a day is removed
by the target's executor (child environments' hand-back, CE-6, and inputs, CE-7).

The digest is checked in sandboxes: the source's executor reports the file's SHA-256 as
its first read hashes it, and the target's executor hashes the parts against it before
it names the file. Both run beside the research code of their sandboxes, so the digest
is the source executor's claim, checked by the target's. The file takes its name by a
rename in its directory, which the executor opened following no symlink, after checking
the name holds no directory or symlink: code in the target sandbox could put another
file at that name between the check and the rename, and may change the file afterwards
as it may change any file there. A file within 100 MiB is read by the worker, which
checks its digest itself.
"""

from __future__ import annotations

import base64
import hashlib
from collections.abc import Awaitable, Callable
from functools import partial
from typing import Final

from sandbox_science.contracts import (
    ErrorKind,
    ExecutionContext,
    ReadFileRequest,
    WriteFileRequest,
)
from sandbox_science.contracts.worker import MAX_READ_BYTES, ManifestFile

from .errors import WorkerError
from .job_sandboxes import EnvironmentPort, TooLargeError, read_whole

PART_BYTES: Final = MAX_READ_BYTES
"""The bytes of one part: one read of the source, one ranged write of the target."""
_REFUSED_PARTS: Final = 3
"""Parts in a row the target may refuse for their offset before the copy gives up: only
another writer of the same upload makes it refuse one twice."""


async def _again[T](call: Callable[[], Awaitable[T]]) -> T:
    """``call``, tried once more after a transient failure (``unavailable``): a part's read
    or write is not given up for one dropped request."""
    try:
        return await call()
    except WorkerError as error:
        if error.kind is not ErrorKind.UNAVAILABLE:
            raise
    return await call()


def upload_id(*names: str) -> str:
    """A ranged write's upload ID, the same each time the same copy is made."""
    return hashlib.sha256("\x00".join(names).encode()).hexdigest()[:32]


async def copy_file(
    port: EnvironmentPort,
    source: ExecutionContext,
    path: str,
    target: ExecutionContext,
    target_path: str,
    *,
    budget: int,
    upload: str,
) -> ManifestFile:
    """Copy ``path`` of ``source``'s environment to ``target_path`` of ``target``'s, within
    ``budget`` bytes. Raises ``TooLargeError`` for a file over the budget, and
    ``WorkerError`` when it cannot be read or written, or changed while it was copied."""
    try:
        data, digest = await read_whole(port, source, path, budget)
    except TooLargeError as error:
        if not error.per_file:
            raise
        return await copy_ranged(
            port,
            source,
            path,
            target,
            target_path,
            size=error.size,
            sha256=error.sha256,
            budget=budget,
            upload=upload,
        )
    await port.write_file(
        WriteFileRequest(
            context=target, path=target_path, content_b64=base64.b64encode(data).decode()
        )
    )
    return ManifestFile(path=target_path, size=len(data), sha256=digest)


async def copy_ranged(
    port: EnvironmentPort,
    source: ExecutionContext,
    path: str,
    target: ExecutionContext,
    target_path: str,
    *,
    size: int,
    sha256: str | None,
    budget: int,
    upload: str,
) -> ManifestFile:
    """Copy a file in parts, resuming the upload's part where it ends, and once more from
    the start when a resumed part turns out to hold another version of the file."""
    if sha256 is None:
        found = await port.read_file(ReadFileRequest(context=source, path=path, length=0))
        size, sha256 = found.size, found.sha256
    if size > budget:
        raise TooLargeError(
            f"{size} bytes is over the remaining byte limit",
            size=size,
            limit=max(0, budget),
            per_file=False,
        )
    written = await port.write_range(target, target_path, upload, 0, b"")
    resumed = written.size > 0
    try:
        return await _send(
            port, source, path, target, target_path, size, sha256, upload, written.size
        )
    except WorkerError as error:
        if not resumed or error.kind is not ErrorKind.CONFLICT:
            raise
    # The part held bytes of an earlier version: the target discarded them; once more.
    await port.write_range(target, target_path, upload, 0, b"", discard=True)
    await port.write_range(target, target_path, upload, 0, b"")
    return await _send(port, source, path, target, target_path, size, sha256, upload, 0)


async def _send(
    port: EnvironmentPort,
    source: ExecutionContext,
    path: str,
    target: ExecutionContext,
    target_path: str,
    size: int,
    sha256: str,
    upload: str,
    offset: int,
) -> ManifestFile:
    refused = 0
    while True:
        if offset > size:
            # More than the file holds now: another version's part. Start again.
            await port.write_range(target, target_path, upload, 0, b"", discard=True)
            offset = (await port.write_range(target, target_path, upload, 0, b"")).size
        data = b""
        if offset < size:
            length = min(PART_BYTES, size - offset)
            data, now = await _again(partial(port.read_part, source, path, offset, length))
            if now != size or not data:
                await port.write_range(target, target_path, upload, 0, b"", discard=True)
                raise WorkerError(ErrorKind.CONFLICT, f"{path} changed while it was copied")
        last = offset + len(data) >= size
        written = await _again(
            partial(
                port.write_range,
                target,
                target_path,
                upload,
                offset,
                data,
                sha256=sha256 if last else None,
                size=size if last else None,
            )
        )
        if written.committed:
            return ManifestFile(path=target_path, size=size, sha256=sha256)
        if written.accepted:
            if written.size != offset + len(data):
                # A target that took the part must hold exactly what was sent.
                raise WorkerError(
                    ErrorKind.CONFLICT,
                    f"the target holds {written.size} bytes of {target_path} after a part"
                    f" that ended at {offset + len(data)}",
                )
            refused = 0
        else:
            refused += 1
            if refused > _REFUSED_PARTS:
                raise WorkerError(
                    ErrorKind.CONFLICT, f"another copy into {target_path} is under way"
                )
        offset = written.size


__all__ = ["PART_BYTES", "copy_file", "copy_ranged", "upload_id"]
