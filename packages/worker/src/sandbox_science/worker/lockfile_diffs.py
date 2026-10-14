# SPDX-License-Identifier: MIT
"""Bounded selected-blob comparison and owned off-loop work; no guest operations."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import re
import stat
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, TypeVar, cast

from sandbox_science.contracts import ErrorKind
from sandbox_science.contracts.lockfile_diffs import (
    HoldReason,
    LicenseObservation,
    LockfileDifference,
    LockfileHeader,
    LockfileHold,
    LockfileSelection,
)
from sandbox_science.contracts.revision_packages import RevisionPackage
from sandbox_science.contracts.revisions import CondaPackage, LockfileRef, PipDistribution

from .errors import WorkerError
from .provisioning import canonical_channel, canonical_url
from .resolution import is_agpl, normalize_pypi

MAX_RAW = 8 * 1024 * 1024
MAX_INDEX = 16 * 1024 * 1024
MAX_ENTRY = 16 * 1024
MAX_SECTION = 20_000
DEADLINE = 8.0
Key = tuple[str, str, str]
Package = CondaPackage | PipDistribution
Pair = tuple[LockfileSelection, LockfileSelection]
Selection = TypeVar("Selection", Pair, LockfileSelection)


def _unavailable() -> WorkerError:
    return WorkerError(ErrorKind.UNAVAILABLE, "selected lockfile comparison is unavailable")


def _page_parameters(offset: int, limit: int) -> None:
    if (
        type(offset) is not int
        or not 0 <= offset <= 80_000
        or type(limit) is not int
        or not 1 <= limit <= 8
    ):
        raise WorkerError(ErrorKind.INVALID_REQUEST, "invalid diff page selection")


class _HeldError(Exception):
    def __init__(self, reason: HoldReason) -> None:
        self.reason: HoldReason = reason


def _unique(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise _unavailable()
        result[key] = value
    return result


def _integer(text: str) -> int:
    if len(text) > 20:
        raise _unavailable()
    return int(text)


def _noninteger(text: str) -> object:
    raise _unavailable()


def _decode(raw: bytes) -> object:
    return cast(
        object,
        json.loads(
            raw.decode("utf-8", "strict"),
            object_pairs_hook=_unique,
            parse_int=_integer,
            parse_float=_noninteger,
            parse_constant=_noninteger,
        ),
    )


def _read_chunk(fd: int) -> bytes:
    return os.read(fd, 8192)


class _Stream:
    """A bounded byte tokenizer: whole entry decoding occurs only after its byte cap."""

    def __init__(self, fd: int) -> None:
        self.fd = fd
        self.buffer = b""
        self.index = 0
        self.position = 0
        self.read = 0
        self.digest = hashlib.sha256()

    def peek(self) -> int | None:
        if self.index == len(self.buffer):
            self.buffer = _read_chunk(self.fd)
            self.index = 0
            self.read += len(self.buffer)
            if self.read > MAX_RAW:
                raise _HeldError("input_limit")
            self.digest.update(self.buffer)
        return self.buffer[self.index] if self.buffer else None

    def take(self) -> int:
        value = self.peek()
        if value is None:
            raise _unavailable()
        self.index += 1
        self.position += 1
        return value

    def whitespace(self) -> None:
        while self.peek() in (9, 10, 13, 32):
            self.take()

    def expect(self, value: int) -> None:
        self.whitespace()
        if self.take() != value:
            raise _unavailable()

    def value(self, maximum: int) -> tuple[int, bytes]:
        self.whitespace()
        start = self.position
        raw = bytearray()
        depth = 0
        quoted = escaped = False
        container = self.peek() in (ord("{"), ord("["))
        string = self.peek() == ord('"')
        while (value := self.peek()) is not None:
            if raw and not quoted and depth == 0 and value in (9, 10, 13, 32, 44, 93, 125):
                break
            if len(raw) >= maximum:
                raise _HeldError("input_limit")
            value = self.take()
            raw.append(value)
            if quoted:
                if escaped:
                    escaped = False
                elif value == 92:
                    escaped = True
                elif value == 34:
                    quoted = False
                    if string and depth == 0:
                        break
            elif value == 34:
                quoted = True
            elif value in (123, 91):
                depth += 1
                if depth > 4:
                    raise _unavailable()
            elif value in (125, 93):
                depth -= 1
                if depth < 0:
                    raise _unavailable()
                if container and depth == 0:
                    break
        if not raw or quoted or depth:
            raise _unavailable()
        return start, bytes(raw)


@dataclass(frozen=True)
class _Entry:
    offset: int
    size: int
    digest: bytes


@dataclass
class _Budget:
    used: int = 0

    def add(self, key: Key) -> None:
        # A serialized index accounting quota, not an exact Python heap/RSS bound.
        self.used += sum(len(part.encode("utf-8")) for part in key) + 128
        if key[0] == "conda":
            self.used += len(key[2].casefold().encode("utf-8"))
        if self.used > MAX_INDEX:
            raise _HeldError("input_limit")


def _identity(info: os.stat_result) -> tuple[int, int, int, int, int, int, int]:
    return (
        info.st_dev,
        info.st_ino,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
        info.st_uid,
        info.st_nlink,
    )


class _Blob:
    def __init__(self, root: Path, ref: LockfileRef, stack: contextlib.ExitStack) -> None:
        if (
            type(ref.sha256) is not str
            or len(ref.sha256) != 64
            or re.fullmatch(r"[0-9a-f]{64}", ref.sha256) is None
            or any(
                type(count) is not int or not 0 <= count <= 20_000
                for count in (ref.conda_packages, ref.pip_distributions)
            )
        ):
            raise _unavailable()
        if type(ref.size) is not int or not 0 < ref.size <= MAX_RAW:
            raise _HeldError("input_limit")
        self.root_path, self.sha = root, ref.sha256
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
        self.root = os.open(root, flags)
        stack.callback(os.close, self.root)
        self.directory = os.open("blobs", flags, dir_fd=self.root)
        stack.callback(os.close, self.directory)
        self.prefix = os.open(ref.sha256[:2], flags, dir_fd=self.directory)
        stack.callback(os.close, self.prefix)
        self.fd = os.open(
            ref.sha256,
            os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
            dir_fd=self.prefix,
        )
        stack.callback(os.close, self.fd)
        info = os.fstat(self.fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.geteuid()
            or info.st_nlink != 1
            or info.st_size != ref.size
        ):
            raise _unavailable()
        self.original = _identity(info)

    def check(self) -> None:
        if _identity(os.fstat(self.fd)) != self.original:
            raise _unavailable()
        for fd, name, parent in (
            (self.root, self.root_path, None),
            (self.directory, "blobs", self.root),
            (self.prefix, self.sha[:2], self.directory),
            (self.fd, self.sha, self.prefix),
        ):
            current = os.stat(name, dir_fd=parent, follow_symlinks=False)
            held = os.fstat(fd)
            if (current.st_dev, current.st_ino) != (held.st_dev, held.st_ino):
                raise _unavailable()
        if _identity(os.stat(self.sha, dir_fd=self.prefix, follow_symlinks=False)) != self.original:
            raise _unavailable()


def _package(raw: bytes, kind: str) -> Package:
    value = _decode(raw)
    if type(value) is not dict:
        raise _unavailable()
    fields = cast(dict[str, object], value)
    if any(type(v) is str and len(v.encode("utf-8")) > 2048 for v in fields.values()):
        raise _HeldError("input_limit")
    model = CondaPackage if kind == "conda" else PipDistribution
    package = model.model_validate_json(raw, strict=True)
    if (
        isinstance(package, CondaPackage)
        and package.channel is not None
        and canonical_channel(package.channel) != package.channel
    ):
        raise _unavailable()
    if isinstance(package, PipDistribution) and package.url is not None:
        safe = canonical_url(package.url)
        if safe is None or safe[0] != package.url:
            raise _unavailable()
    return package


def _canonical(package: Package) -> bytes:
    raw = package.model_dump_json().encode("utf-8")
    if len(raw) > MAX_ENTRY:
        raise _HeldError("input_limit")
    return raw


def _scan(
    blob: _Blob, ref: LockfileRef, budget: _Budget
) -> tuple[LockfileHeader, dict[Key, _Entry]]:
    stream = _Stream(blob.fd)
    head: dict[str, object] = {}
    keys: set[str] = set()
    index: dict[Key, _Entry] = {}
    counts = {"conda": 0, "pip": 0}
    conda_names: set[str] = set()
    stream.expect(123)
    while True:
        stream.whitespace()
        if stream.peek() == 125:
            stream.take()
            break
        _, key_raw = stream.value(64)
        key = _decode(key_raw)
        if (
            type(key) is not str
            or key in keys
            or key not in {"format", "version", "python", "platform", "skipped", "conda", "pip"}
        ):
            raise _unavailable()
        keys.add(key)
        stream.expect(58)
        if key in counts:
            stream.expect(91)
            stream.whitespace()
            if stream.peek() != 93:
                while True:
                    offset, raw = stream.value(MAX_ENTRY)
                    package = _package(raw, key)
                    counts[key] += 1
                    if counts[key] > MAX_SECTION:
                        raise _HeldError("input_limit")
                    name = package.name if key == "conda" else normalize_pypi(package.name)
                    location = package.location if isinstance(package, PipDistribution) else ""
                    identity = (key, location, name)
                    if identity in index or (key == "conda" and name.casefold() in conda_names):
                        raise _unavailable()
                    if key == "conda":
                        conda_names.add(name.casefold())
                    budget.add(identity)
                    index[identity] = _Entry(
                        offset, len(raw), hashlib.sha256(_canonical(package)).digest()
                    )
                    stream.whitespace()
                    if stream.peek() == 93:
                        break
                    stream.expect(44)
            stream.expect(93)
        else:
            _, raw = stream.value(2048)
            head[key] = _decode(raw)
        stream.whitespace()
        if stream.peek() == 125:
            stream.take()
            break
        stream.expect(44)
        stream.whitespace()
        if stream.peek() == 125:
            raise _unavailable()
    stream.whitespace()
    if (
        stream.peek() is not None
        or stream.read != ref.size
        or stream.digest.hexdigest() != ref.sha256
    ):
        raise _unavailable()
    blob.check()
    if keys != {"format", "version", "python", "platform", "skipped", "conda", "pip"}:
        raise _HeldError("unsupported_format")
    if type(head["version"]) is not int or type(head["format"]) is not str:
        raise _unavailable()
    if head["format"] != "sandbox-science-lockfile" or head["version"] != 1:
        raise _HeldError("unsupported_format")
    if type(head["skipped"]) is not int or not 0 <= head["skipped"] <= 2**62:
        raise _unavailable()
    if counts["conda"] != ref.conda_packages or counts["pip"] != ref.pip_distributions:
        raise _unavailable()
    if type(head["platform"]) is str and len(head["platform"]) > 128:
        raise _HeldError("input_limit")
    header = LockfileHeader.model_validate_json(
        json.dumps(
            {
                "sha256": ref.sha256,
                "size": ref.size,
                "python": head["python"],
                "platform": head["platform"],
                "conda_packages": counts["conda"],
                "pip_distributions": counts["pip"],
                "skipped": head["skipped"],
            }
        ),
        strict=True,
    )
    return header, index


def _entries(
    old: dict[Key, _Entry], new: dict[Key, _Entry]
) -> Iterator[tuple[Key, _Entry | None, _Entry | None]]:
    a, b = sorted(old), sorted(new)
    left = right = 0
    while left < len(a) or right < len(b):
        if right == len(b) or (left < len(a) and a[left] < b[right]):
            key = a[left]
            left += 1
        else:
            key = b[right]
            right += 1
            if left < len(a) and a[left] == key:
                left += 1
        before, after = old.get(key), new.get(key)
        if before is None or after is None or before.digest != after.digest:
            yield key, before, after


def _selected(blob: _Blob, entry: _Entry | None, kind: str) -> Package | None:
    if entry is None:
        return None
    raw = os.pread(blob.fd, entry.size, entry.offset)
    package = _package(raw, kind)
    if len(raw) != entry.size or hashlib.sha256(_canonical(package)).digest() != entry.digest:
        raise _unavailable()
    return package


def _license(package: Package | None) -> LicenseObservation | None:
    if package is None:
        return None
    text = package.license
    return LicenseObservation(declared=text, agpl=is_agpl(text) if text is not None else None)


def _source(
    root: Path,
    selection: LockfileSelection,
    stack: contextlib.ExitStack,
    budget: _Budget,
) -> tuple[_Blob | None, LockfileHeader | None, dict[Key, _Entry], HoldReason | None]:
    """The same bounded proof for one inventory input or either comparison input."""
    blob = header = None
    index: dict[Key, _Entry] = {}
    try:
        if selection.lockfile is None:
            raise _HeldError("lockfile_unrecorded")
        blob = _Blob(root, selection.lockfile, stack)
        header, index = _scan(blob, selection.lockfile, budget)
        if header.skipped:
            raise _HeldError("skipped_entries")
    except FileNotFoundError:
        return blob, header, index, "blob_missing"
    except _HeldError as held:
        return blob, header, index, held.reason
    return blob, header, index, None


def revision_packages(
    root: Path, selection: LockfileSelection, offset: int, limit: int
) -> dict[str, object]:
    """Scan one exact blob once; materialize only a bounded page of recorded entries."""
    _page_parameters(offset, limit)
    if offset > 40_000:
        raise WorkerError(ErrorKind.INVALID_REQUEST, "invalid package page selection")
    try:
        with contextlib.ExitStack() as stack:
            blob, header, index, hold = _source(root, selection, stack, _Budget())
            frame: dict[str, object] = {
                "selection": selection,
                "offset": offset,
                "limit": limit,
                "header": header,
            }
            if hold is not None:
                return {
                    **frame,
                    "status": "held",
                    "holds": (hold,),
                    "total": None,
                    "next_offset": None,
                    "packages": (),
                }
            if blob is None or header is None:
                raise _unavailable()
            total = len(index)
            if total != header.conda_packages + header.pip_distributions:
                raise _unavailable()
            if offset > total:
                raise WorkerError(
                    ErrorKind.INVALID_REQUEST, "package offset exceeds selected total"
                )
            rows: list[RevisionPackage] = []
            output_bytes = 8192  # Reserve the bounded selection/header/scope envelope.
            for key in sorted(index)[offset : offset + limit]:
                package = _selected(blob, index[key], key[0])
                license_info = _license(package)
                if package is None or license_info is None:
                    raise _unavailable()
                row = RevisionPackage(
                    ecosystem=cast(Literal["conda", "pip"], key[0]),
                    name=key[2],
                    location=cast(Literal["prefix", "user"], key[1]) if key[1] else None,
                    package=package,
                    license=license_info,
                )
                size = len(row.model_dump_json().encode("utf-8")) + 1
                if output_bytes + size > 64 * 1024:
                    break
                rows.append(row)
                output_bytes += size
            if offset < total and not rows:
                raise _unavailable()
            blob.check()
            end = offset + len(rows)
            return {
                **frame,
                "status": "complete",
                "holds": (),
                "total": total,
                "next_offset": end if end < total else None,
                "packages": tuple(rows),
            }
    except WorkerError:
        raise
    except (OSError, ValueError, TypeError, RecursionError):
        raise _unavailable() from None


def compare(root: Path, pair: Pair, offset: int, limit: int) -> dict[str, object]:
    """Read at most two selected regular blobs; return no full lockfile or full diff."""
    _page_parameters(offset, limit)
    try:
        with contextlib.ExitStack() as stack:
            blobs: list[_Blob | None] = []
            indexes: list[dict[Key, _Entry]] = []
            headers: list[LockfileHeader | None] = []
            holds: list[LockfileHold] = []
            budget = _Budget()
            for side, selection in zip(("before", "after"), pair, strict=True):
                blob, header, index, hold = _source(root, selection, stack, budget)
                if hold is not None:
                    holds.append(LockfileHold(side=side, reason=hold))
                blobs.append(blob)
                headers.append(header)
                indexes.append(index)
            frame: dict[str, object] = {
                "before": pair[0],
                "after": pair[1],
                "offset": offset,
                "limit": limit,
                "before_header": headers[0],
                "after_header": headers[1],
                "holds": tuple(holds),
            }
            if holds:
                return {
                    **frame,
                    "status": "held",
                    "header_changes": (),
                    "total": None,
                    "added": None,
                    "removed": None,
                    "changed": None,
                    "next_offset": None,
                    "differences": (),
                }
            before_blob, after_blob = blobs
            if before_blob is None or after_blob is None:
                raise _unavailable()
            rows: list[LockfileDifference] = []
            counts = {"added": 0, "removed": 0, "changed": 0}
            total = 0
            output_bytes = 8192
            page_full = False
            for key, before_entry, after_entry in _entries(*indexes):
                change = (
                    "added"
                    if before_entry is None
                    else "removed"
                    if after_entry is None
                    else "changed"
                )
                counts[change] += 1
                if total >= offset and len(rows) < limit and not page_full:
                    before = _selected(before_blob, before_entry, key[0])
                    after = _selected(after_blob, after_entry, key[0])
                    a = before.model_dump() if before else {}
                    b = after.model_dump() if after else {}
                    changed = tuple(sorted(k for k in a.keys() | b.keys() if a.get(k) != b.get(k)))
                    row = LockfileDifference(
                        ecosystem=cast(Literal["conda", "pip"], key[0]),
                        name=key[2],
                        location=cast(Literal["prefix", "user"], key[1]) if key[1] else None,
                        change=change,
                        before=before,
                        after=after,
                        changed_fields=changed,
                        before_license=_license(before),
                        after_license=_license(after),
                    )
                    size = len(row.model_dump_json().encode()) + 1
                    if output_bytes + size > 64 * 1024:
                        page_full = True
                    else:
                        rows.append(row)
                        output_bytes += size
                total += 1
            if offset > total:
                raise WorkerError(ErrorKind.INVALID_REQUEST, "diff offset exceeds selected total")
            before_blob.check()
            after_blob.check()
            h0, h1 = headers
            if h0 is None or h1 is None:
                raise _unavailable()
            end = offset + len(rows)
            return {
                **frame,
                "status": "complete",
                "header_changes": tuple(
                    k for k in ("python", "platform") if getattr(h0, k) != getattr(h1, k)
                ),
                "total": total,
                **counts,
                "next_offset": end if end < total else None,
                "differences": tuple(rows),
            }
    except WorkerError:
        raise
    except (OSError, ValueError, TypeError, RecursionError):
        raise _unavailable() from None


class LockfileDiffs:
    """Shared package/diff readers: global two/per-environment one actual work owners."""

    def __init__(self) -> None:
        self.pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="lockfile-diff")
        self.tasks: set[asyncio.Task[dict[str, object]]] = set()
        self.environments: set[str] = set()
        self.closed = False
        self.draining: asyncio.Task[None] | None = None

    async def read(
        self,
        environment: str,
        root: Path,
        prepare: Callable[[], Pair],
        verify: Callable[[Pair], None],
        offset: int,
        limit: int,
    ) -> dict[str, object]:
        """Admit before SQL selection; preserve lane/descriptor ownership until settled."""
        _page_parameters(offset, limit)
        return await self._read(
            environment, prepare, verify, lambda pair: compare(root, pair, offset, limit)
        )

    async def read_packages(
        self,
        environment: str,
        root: Path,
        prepare: Callable[[], LockfileSelection],
        verify: Callable[[LockfileSelection], None],
        offset: int,
        limit: int,
    ) -> dict[str, object]:
        """One selected blob, using the exact same physical owners as comparisons."""
        _page_parameters(offset, limit)
        if offset > 40_000:
            raise WorkerError(ErrorKind.INVALID_REQUEST, "invalid package page selection")
        return await self._read(
            environment,
            prepare,
            verify,
            lambda selection: revision_packages(root, selection, offset, limit),
        )

    async def _read(
        self,
        environment: str,
        prepare: Callable[[], Selection],
        verify: Callable[[Selection], None],
        read: Callable[[Selection], dict[str, object]],
    ) -> dict[str, object]:
        if self.closed or len(self.tasks) >= 2 or environment in self.environments:
            raise _unavailable()
        self.environments.add(environment)

        async def run() -> dict[str, object]:
            try:
                selection = prepare()
                result = await asyncio.get_running_loop().run_in_executor(
                    self.pool, read, selection
                )
                verify(selection)
                return result
            finally:
                self.environments.discard(environment)

        task = asyncio.create_task(run())
        self.tasks.add(task)

        def done(value: asyncio.Task[dict[str, object]]) -> None:
            self.tasks.discard(value)
            if not value.cancelled():
                value.exception()

        task.add_done_callback(done)
        try:
            async with asyncio.timeout(DEADLINE):
                return await asyncio.shield(task)
        except TimeoutError:
            raise _unavailable() from None

    async def _drain(self) -> None:
        while self.tasks:
            await asyncio.gather(*tuple(self.tasks), return_exceptions=True)
        self.pool.shutdown(wait=True)

    async def close(self) -> None:
        """Repeated cancellation cannot close the pool underneath an admitted reader."""
        self.closed = True
        if self.draining is None:
            self.draining = asyncio.create_task(self._drain())
        interrupted: asyncio.CancelledError | None = None
        while not self.draining.done():
            try:
                await asyncio.shield(self.draining)
            except asyncio.CancelledError as error:
                if interrupted is None:
                    interrupted = error
        await self.draining
        if interrupted is not None:
            raise interrupted
