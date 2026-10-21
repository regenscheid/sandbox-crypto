# SPDX-License-Identifier: MIT
"""Bounded framed workspace bytes, with disk-backed path and inode indexes."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import stat
import struct
import tempfile
from collections.abc import Callable, Generator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from sandbox_science.contracts.jsonio import strict_loads
from sandbox_science.contracts.workspace_checkpoint import (
    MAX_CHECKPOINT_CONTENT,
    MAX_CHECKPOINT_HEADER,
    MAX_CHECKPOINT_MEMBERS,
    MAX_CHECKPOINT_METADATA,
    MAX_CHECKPOINT_RECEIPT,
    CheckpointMember,
    CheckpointSource,
)

MAGIC = b"SS-WORKSPACE-CHECKPOINT-1\n"
CHUNK = 512 * 1024


@dataclass(frozen=True)
class StreamSummary:
    sha256: str
    size: int
    members: int
    content_bytes: int
    metadata_bytes: int


class Stream:
    """Hash every byte while enforcing bounded individual reads and writes."""

    def __init__(self, handle: BinaryIO, check: Callable[[], None] | None = None) -> None:
        self.handle, self.digest, self.size = handle, hashlib.sha256(), 0
        self.members = self.content = self.metadata = 0
        self.check = check

    def write(self, data: bytes) -> None:
        if self.handle.write(data) != len(data):
            raise OSError("short checkpoint write")
        self.digest.update(data)
        self.size += len(data)

    def read(self, amount: int) -> bytes:
        if self.check is not None:
            self.check()
        data = self.handle.read(amount)
        self.digest.update(data)
        self.size += len(data)
        if len(data) != amount:
            raise ValueError("checkpoint archive is truncated")
        return data

    def frame(self, data: bytes) -> None:
        self.write(struct.pack(">I", len(data)))
        self.write(data)

    def header(self, limit: int) -> bytes:
        size = struct.unpack(">I", self.read(4))[0]
        if size > limit:
            raise ValueError("checkpoint header exceeds its bound")
        return self.read(size)

    def charge(self, data: bytes, member: CheckpointMember) -> None:
        self.members += 1
        self.metadata += len(data)
        self.content += member.size
        if (
            self.members > MAX_CHECKPOINT_MEMBERS
            or self.metadata > MAX_CHECKPOINT_METADATA
            or self.content > MAX_CHECKPOINT_CONTENT
        ):
            raise ValueError("checkpoint exceeds its member, metadata or content bound")

    def summary(self) -> StreamSummary:
        return StreamSummary(
            self.digest.hexdigest(), self.size, self.members, self.content, self.metadata
        )


@contextmanager
def index() -> Generator[sqlite3.Connection]:
    """Never retain a complete tree's names or hardlinks in process memory."""
    with tempfile.TemporaryDirectory(prefix="ss-checkpoint-index-") as directory:
        db = sqlite3.connect(Path(directory) / "members.sqlite3")
        try:
            db.execute("PRAGMA cache_size=-1024")
            db.execute("CREATE TABLE members(path TEXT PRIMARY KEY,kind TEXT,mode INTEGER)")
            db.execute(
                "CREATE TABLE inodes(device INTEGER,inode INTEGER,path TEXT,"
                "PRIMARY KEY(device,inode))"
            )
            db.execute("CREATE TABLE observed(path TEXT PRIMARY KEY,stamp TEXT,target TEXT)")
            yield db
        finally:
            db.close()


def _stamp(info: os.stat_result) -> tuple[int, ...]:
    return (
        info.st_dev,
        info.st_ino,
        info.st_mode,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
        info.st_nlink,
    )


def _member(
    path: str, info: os.stat_result, kind: str, target: str | None = None
) -> CheckpointMember:
    # Validation names every supported kind; no archive-selected executable callback.
    return CheckpointMember.model_validate(
        {
            "path": path,
            "kind": kind,
            "mode": stat.S_IMODE(info.st_mode),
            "size": info.st_size if kind == "file" else 0,
            "target": target,
        }
    )


def _emit(stream: Stream, member: CheckpointMember) -> None:
    data = member.model_dump_json().encode()
    if len(data) > MAX_CHECKPOINT_HEADER:
        raise ValueError("checkpoint member header exceeds its bound")
    stream.charge(data, member)
    stream.frame(data)


def _walk(stream: Stream, db: sqlite3.Connection, directory: int, path: str) -> None:
    before = os.fstat(directory)
    _emit(stream, _member(path, before, "directory"))
    db.execute("INSERT INTO observed VALUES (?,?,NULL)", (path, json.dumps(_stamp(before))))
    with os.scandir(directory) as entries:
        for entry in entries:
            relative = entry.name if path == "." else path + "/" + entry.name
            info = os.stat(entry.name, dir_fd=directory, follow_symlinks=False)
            if not stat.S_ISDIR(info.st_mode):
                db.execute(
                    "INSERT INTO observed VALUES (?,?,?)",
                    (
                        relative,
                        json.dumps(_stamp(info)),
                        os.readlink(entry.name, dir_fd=directory)
                        if stat.S_ISLNK(info.st_mode)
                        else None,
                    ),
                )
            if stat.S_ISDIR(info.st_mode):
                descriptor = os.open(
                    entry.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory
                )
                try:
                    if _stamp(os.fstat(descriptor)) != _stamp(info):
                        raise ValueError("checkpoint source directory changed")
                    _walk(stream, db, descriptor, relative)
                finally:
                    os.close(descriptor)
            elif stat.S_ISLNK(info.st_mode):
                target = os.readlink(entry.name, dir_fd=directory)
                _emit(stream, _member(relative, info, "symlink", target))
                if _stamp(os.stat(entry.name, dir_fd=directory, follow_symlinks=False)) != _stamp(
                    info
                ):
                    raise ValueError("checkpoint source symlink changed")
            elif stat.S_ISREG(info.st_mode):
                prior = db.execute(
                    "SELECT path FROM inodes WHERE device=? AND inode=?", (info.st_dev, info.st_ino)
                ).fetchone()
                if prior is not None:
                    _emit(stream, _member(relative, info, "hardlink", str(prior[0])))
                    continue
                descriptor = os.open(
                    entry.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory
                )
                with os.fdopen(descriptor, "rb") as handle:
                    if _stamp(os.fstat(descriptor)) != _stamp(info):
                        raise ValueError("checkpoint source file changed")
                    _emit(stream, _member(relative, info, "file"))
                    left = info.st_size
                    while left:
                        chunk = handle.read(min(CHUNK, left))
                        if not chunk:
                            raise ValueError("checkpoint source file shrank")
                        stream.write(chunk)
                        left -= len(chunk)
                    if handle.read(1) or _stamp(os.fstat(descriptor)) != _stamp(info):
                        raise ValueError("checkpoint source file changed")
                if info.st_nlink > 1:
                    db.execute(
                        "INSERT INTO inodes VALUES (?,?,?)", (info.st_dev, info.st_ino, relative)
                    )
            else:
                raise ValueError(f"checkpoint source has an unsupported special file: {relative!r}")
    if _stamp(os.fstat(directory)) != _stamp(before):
        raise ValueError("checkpoint source directory changed")


def _unchanged(db: sqlite3.Connection, directory: int, path: str) -> int:
    """Detect changes to previously read files too, without following replaced parents."""
    before = os.fstat(directory)
    if db.execute("SELECT stamp FROM observed WHERE path=?", (path,)).fetchone() != (
        json.dumps(_stamp(before)),
    ):
        raise ValueError("checkpoint source directory changed after capture")
    count = 1
    with os.scandir(directory) as entries:
        for entry in entries:
            relative = entry.name if path == "." else path + "/" + entry.name
            info = os.stat(entry.name, dir_fd=directory, follow_symlinks=False)
            if stat.S_ISDIR(info.st_mode):
                descriptor = os.open(
                    entry.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory
                )
                try:
                    count += _unchanged(db, descriptor, relative)
                finally:
                    os.close(descriptor)
            else:
                target = (
                    os.readlink(entry.name, dir_fd=directory)
                    if stat.S_ISLNK(info.st_mode)
                    else None
                )
                if db.execute(
                    "SELECT stamp,target FROM observed WHERE path=?", (relative,)
                ).fetchone() != (json.dumps(_stamp(info)), target):
                    raise ValueError("checkpoint source file changed after capture")
                count += 1
    if _stamp(os.fstat(directory)) != _stamp(before):
        raise ValueError("checkpoint source directory changed during validation")
    return count


def encode(root: Path, source: CheckpointSource, handle: BinaryIO) -> StreamSummary:
    """Capture complete bytes without following source links or buffering files."""
    stream = Stream(handle)
    stream.write(MAGIC)
    stream.frame(source.model_dump_json().encode())
    descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        with index() as db:
            _walk(stream, db, descriptor, ".")
            if _unchanged(db, descriptor, ".") != stream.members:
                raise ValueError("checkpoint source member count changed")
    finally:
        os.close(descriptor)
    stream.frame(b"")
    return stream.summary()


def _validate(db: sqlite3.Connection, member: CheckpointMember, number: int) -> None:
    path, kind = member.path, member.kind
    if (number == 1) != (path == ".") or (path == "." and kind != "directory"):
        raise ValueError("checkpoint must begin with its root directory")
    parent = path.rpartition("/")[0] or "."
    if path != "." and db.execute(
        "SELECT kind FROM members WHERE path=?", (parent,)
    ).fetchone() != ("directory",):
        raise ValueError("checkpoint parent is missing or is not a directory")
    if (kind in ("symlink", "hardlink")) != (member.target is not None):
        raise ValueError("checkpoint link target is missing or unexpected")
    if kind != "file" and member.size:
        raise ValueError("checkpoint non-file contains bytes")
    if member.target is not None and "\0" in member.target:
        raise ValueError("checkpoint link target contains NUL")
    if kind == "symlink" and member.mode != 0o777:
        raise ValueError("checkpoint symlink mode cannot be preserved")
    if kind == "hardlink" and db.execute(
        "SELECT kind,mode FROM members WHERE path=?", (member.target,)
    ).fetchone() != ("file", member.mode):
        raise ValueError("checkpoint hardlink does not name an earlier matching regular file")
    try:
        db.execute("INSERT INTO members VALUES (?,?,?)", (path, kind, member.mode))
    except sqlite3.IntegrityError:
        raise ValueError("checkpoint has a duplicate member path") from None


def decode(
    handle: BinaryIO,
    source: CheckpointSource,
    target: Path | None = None,
    *,
    check: Callable[[], None] | None = None,
) -> StreamSummary:
    """Verify the entire stream; optionally extract into a caller-owned empty directory."""
    stream = Stream(handle, check)
    if stream.read(len(MAGIC)) != MAGIC:
        raise ValueError("unsupported checkpoint archive format")
    source_bytes = stream.header(MAX_CHECKPOINT_RECEIPT)
    strict_loads(source_bytes)
    if CheckpointSource.model_validate_json(source_bytes) != source:
        raise ValueError("checkpoint source identity differs")
    with index() as db:
        while data := stream.header(MAX_CHECKPOINT_HEADER):
            strict_loads(data)
            member = CheckpointMember.model_validate_json(data)
            stream.charge(data, member)
            _validate(db, member, stream.members)
            path = None if target is None else target / member.path
            if path is not None and member.kind == "directory" and member.path != ".":
                path.mkdir(mode=0o700)
            elif path is not None and member.kind == "symlink":
                path.symlink_to(member.target or "")
            elif path is not None and target is not None and member.kind == "hardlink":
                os.link(target / (member.target or ""), path, follow_symlinks=False)
            if member.kind == "file":
                writer = None if path is None else path.open("xb")
                try:
                    left = member.size
                    while left:
                        chunk = stream.read(min(CHUNK, left))
                        if writer is not None:
                            writer.write(chunk)
                        left -= len(chunk)
                    if writer is not None:
                        writer.flush()
                        os.fchmod(writer.fileno(), member.mode)
                        os.fsync(writer.fileno())
                finally:
                    if writer is not None:
                        writer.close()
        if not stream.members or handle.read(1):
            raise ValueError("checkpoint has no members or has trailing bytes")
        if target is not None:
            # Children first: restrictive original modes must not prevent extraction.
            for path, mode in db.execute(
                "SELECT path,mode FROM members WHERE kind='directory' ORDER BY length(path) DESC"
            ):
                directory = target / str(path)
                descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                try:
                    os.fchmod(descriptor, int(mode))
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
    return stream.summary()
