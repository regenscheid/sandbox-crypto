# SPDX-License-Identifier: MIT
"""Init-only projected-file copies; credentials are never rendered or logged."""

from __future__ import annotations

import argparse
import hmac
import json
import os
import stat
import sys
import tempfile
from pathlib import Path, PurePosixPath

MAX_FILE_BYTES = 1024**2
MAX_TOTAL_BYTES = 8 * 1024**2
MAX_FILES = 32


def _relative(value: object) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 256
        or PurePosixPath(value).is_absolute()
        or any(part in ("", ".", "..") for part in value.split("/"))
        or any(ord(char) < 32 for char in value)
    ):
        raise ValueError("invalid service-file path")
    return value


def _read(path: Path, *, private: bool = False) -> bytes:
    flags = os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC
    descriptor = os.open(path, flags | (os.O_NOFOLLOW if private else 0))
    with os.fdopen(descriptor, "rb") as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_size > MAX_FILE_BYTES:
            raise ValueError("invalid service-file type or size")
        if private and (before.st_uid != os.getuid() or before.st_mode & 0o077):
            raise ValueError("existing service file is not private")
        data = stream.read(MAX_FILE_BYTES + 1)
        after = os.fstat(stream.fileno())
        if len(data) > MAX_FILE_BYTES or (
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
        ) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns):
            raise ValueError("service file changed while copying")
        return data


def _sync(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _parents(root: Path, relative: str) -> Path:
    parent = root
    for name in PurePosixPath(relative).parts[:-1]:
        child = parent / name
        try:
            child.mkdir(mode=0o700)
        except FileExistsError:
            if not stat.S_ISDIR(child.lstat().st_mode):
                raise ValueError("service-file parent is not a literal directory") from None
        else:
            _sync(parent)
        parent = child
    return root / relative


def copy_files(source: Path, destination: Path, pairs: object) -> None:
    """Copy an explicit bounded projection into idempotent no-overwrite private files.

    Secret projections may be symlinks within the source root. Destination files
    and parents cannot be symlinks. Interrupted init restarts accept completed
    identical files; partial temporary files never become configured credentials.
    """
    if not isinstance(pairs, list) or not 1 <= len(pairs) <= MAX_FILES:
        raise ValueError("invalid service-file mapping")
    paths: list[tuple[str, str]] = []
    for pair in pairs:
        if not isinstance(pair, list) or len(pair) != 2:
            raise ValueError("invalid service-file mapping")
        paths.append((_relative(pair[0]), _relative(pair[1])))
    if len({target for _, target in paths}) != len(paths):
        raise ValueError("duplicate service-file destination")
    original = source.resolve(strict=True)
    if not original.is_dir() or not stat.S_ISDIR(destination.lstat().st_mode):
        raise ValueError("service-file roots must be directories")
    total = 0
    for name, target in paths:
        projected = (source / name).resolve(strict=True)
        if not projected.is_relative_to(original):
            raise ValueError("service-file projection leaves its source root")
        data = _read(projected)
        total += len(data)
        if total > MAX_TOTAL_BYTES:
            raise ValueError("service-file mapping exceeds total bytes")
        path = _parents(destination, target)
        descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=".service-file-")
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.link(temporary, path)
            except FileExistsError:
                if not hmac.compare_digest(_read(path, private=True), data):
                    raise ValueError(
                        "service credential changed; recreate the init volume"
                    ) from None
            _sync(path.parent)
        finally:
            Path(temporary).unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--destination-root", type=Path, required=True)
    parser.add_argument("--files", required=True)
    args = parser.parse_args()
    try:
        if len(args.files) > 8192:
            raise ValueError("service-file mapping exceeds its bound")
        copy_files(args.source_root, args.destination_root, json.loads(args.files))
    except (OSError, ValueError, RecursionError):
        print(
            "private service-file initialization failed; check projection and private volumes",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
