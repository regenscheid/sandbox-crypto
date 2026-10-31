# SPDX-License-Identifier: MIT
"""A workspace tree's order-independent digest, and emptying a restore's target.

An archive lists a tree in the order the capture walked it, and a restored tree on another
volume is walked in another order, so neither the archive's hash nor a second capture can
say that a restored tree is the checkpoint. The **tree digest** can: one line per path, in
byte order of the path, with its kind, mode, size, the content's SHA-256 or the link's
text, and for a file its hardlink group (the smallest path sharing its inode). The worker
computes it from the kept checkpoint while verifying it (``decode(tree=...)``); a restore
Job computes it from the volume it wrote (``describe``); the worker compares the two
(docs/KUBERNETES-BUNDLE.md#restoring-a-checkpoint). Ownership and times are outside the
checkpoint format and outside the digest. Rows live in a temporary SQLite file, so memory
stays bounded whatever the tree's size.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import stat
import tempfile
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

from sandbox_science.contracts.workspace_checkpoint import MAX_CHECKPOINT_DEPTH

READ = 512 * 1024
SYMLINK_MODE = 0o777
PRIVILEGE_BITS = 0o6000
"""Set-user-ID and set-group-ID: a restore never writes them (``decode``), so the digest
leaves them out on both sides."""


class TreeIndex:
    """The rows of one tree; ``digest`` once complete."""

    def __init__(self, db: sqlite3.Connection) -> None:
        self.db = db
        db.execute("PRAGMA cache_size=-1024")
        db.execute(
            "CREATE TABLE entries(path TEXT PRIMARY KEY, kind TEXT, mode INTEGER,"
            " size INTEGER, detail TEXT, first TEXT)"
        )
        db.execute("CREATE INDEX entries_first ON entries(first)")
        db.execute(
            "CREATE TABLE inodes(device INTEGER, inode INTEGER, path TEXT,"
            " PRIMARY KEY(device, inode))"
        )

    def add(
        self,
        path: str,
        kind: str,
        mode: int,
        *,
        size: int = 0,
        detail: str | None = None,
        first: str | None = None,
    ) -> None:
        try:
            self.db.execute(
                "INSERT INTO entries VALUES (?,?,?,?,?,?)",
                (path, kind, mode & ~PRIVILEGE_BITS, size, detail, first),
            )
        except sqlite3.IntegrityError:
            raise ValueError(f"the tree lists {path!r} twice") from None

    def link(self, path: str, target: str) -> None:
        """A hardlink to an earlier file: its content, in that file's group."""
        row = self.db.execute(
            "SELECT mode, size, detail, first FROM entries WHERE path=? AND kind='file'",
            (target,),
        ).fetchone()
        if row is None:
            raise ValueError(f"hardlink {path!r} names no earlier file")
        self.add(path, "file", int(row[0]), size=int(row[1]), detail=str(row[2]), first=row[3])

    def has(self, path: str) -> bool:
        return self.db.execute("SELECT 1 FROM entries WHERE path=?", (path,)).fetchone() is not None

    def seen(self, device: int, inode: int, path: str) -> str | None:
        """The first path of an inode with several links, recording this one if new."""
        row = self.db.execute(
            "SELECT path FROM inodes WHERE device=? AND inode=?", (device, inode)
        ).fetchone()
        if row is not None:
            return str(row[0])
        self.db.execute("INSERT INTO inodes VALUES (?,?,?)", (device, inode, path))
        return None

    def digest(self) -> str:
        checksum = hashlib.sha256(b"sandbox-workspace-tree-v1\n")
        for row in self.db.execute(
            "SELECT e.path, e.kind, e.mode, e.size, e.detail,"
            " (SELECT min(g.path) FROM entries g WHERE g.first = e.first)"
            " FROM entries e ORDER BY e.path"
        ):
            checksum.update(json.dumps(list(row), separators=(",", ":")).encode() + b"\n")
        return checksum.hexdigest()


@contextmanager
def tree_index() -> Generator[TreeIndex]:
    with tempfile.TemporaryDirectory(prefix="ss-checkpoint-tree-") as directory:
        db = sqlite3.connect(Path(directory) / "tree.sqlite3")
        try:
            yield TreeIndex(db)
        finally:
            db.close()


def _content(directory: int, name: str, info: os.stat_result, path: str) -> str:
    try:
        descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    except PermissionError:
        raise ValueError(f"restored file is unreadable: {path!r}") from None
    checksum, left = hashlib.sha256(), info.st_size
    with os.fdopen(descriptor, "rb") as handle:
        while data := handle.read(READ):
            checksum.update(data)
            left -= len(data)
    if left:
        raise ValueError(f"restored file changed while it was read: {path!r}")
    return checksum.hexdigest()


def _walk(tree: TreeIndex, directory: int, path: str, exclude: frozenset[str], depth: int) -> None:
    if depth > MAX_CHECKPOINT_DEPTH:
        raise ValueError("the restored tree is deeper than a checkpoint")
    tree.add(path, "directory", stat.S_IMODE(os.fstat(directory).st_mode))
    for name in sorted(entry.name for entry in os.scandir(directory)):
        if path == "." and name in exclude:
            continue
        relative = name if path == "." else f"{path}/{name}"
        info = os.stat(name, dir_fd=directory, follow_symlinks=False)
        if stat.S_ISDIR(info.st_mode):
            child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            try:
                _walk(tree, child, relative, exclude, depth + 1)
            finally:
                os.close(child)
        elif stat.S_ISLNK(info.st_mode):
            tree.add(relative, "symlink", SYMLINK_MODE, detail=os.readlink(name, dir_fd=directory))
        elif stat.S_ISREG(info.st_mode):
            first = tree.seen(info.st_dev, info.st_ino, relative) if info.st_nlink > 1 else None
            tree.add(
                relative,
                "file",
                stat.S_IMODE(info.st_mode),
                size=info.st_size,
                detail=_content(directory, name, info, relative),
                first=first or relative,
            )
        else:
            tree.add(relative, "special", stat.S_IMODE(info.st_mode))


def describe(root: Path, *, exclude: frozenset[str] = frozenset()) -> str:
    """The tree digest of the tree at ``root``, read from disk, never through a link;
    ``exclude`` names entries of the root left out."""
    descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        with tree_index() as tree:
            _walk(tree, descriptor, ".", exclude, 1)
            return tree.digest()
    finally:
        os.close(descriptor)


def _remove(parent: int, name: str, device: int, path: str, depth: int) -> None:
    info = os.stat(name, dir_fd=parent, follow_symlinks=False)
    try:
        if not stat.S_ISDIR(info.st_mode):
            os.unlink(name, dir_fd=parent)
            return
        if info.st_dev != device:
            raise ValueError(f"the target workspace has another filesystem at {path!r}")
        if depth > MAX_CHECKPOINT_DEPTH + 64:
            raise ValueError("the target workspace is too deep to empty")
        child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
        try:
            mode = stat.S_IMODE(info.st_mode)
            if mode & 0o700 != 0o700:
                os.fchmod(child, mode | 0o700)
            for entry in [entry.name for entry in os.scandir(child)]:
                _remove(child, entry, device, f"{path}/{entry}", depth + 1)
        finally:
            os.close(child)
        os.rmdir(name, dir_fd=parent)
    except PermissionError:
        raise ValueError(f"cannot remove {path!r} from the target workspace") from None


def clear(root: Path, *, keep: frozenset[str]) -> None:
    """Empty ``root`` except the entries ``keep`` names, never following a link and never
    leaving its filesystem; a directory the restore's user owns but cannot write is made
    writable first. Any entry it cannot remove fails it, naming the entry."""
    descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        device = os.fstat(descriptor).st_dev
        for name in [entry.name for entry in os.scandir(descriptor)]:
            if name not in keep:
                _remove(descriptor, name, device, name, 1)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
