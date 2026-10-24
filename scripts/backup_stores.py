# SPDX-License-Identifier: MIT
"""Snapshot and restore the stores of one Sandbox Science instance (docs/BACKUP-RESTORE.md).

    backup_stores.py backup  --dest DIR --launcher-state DIR [--middleware-state DIR] ...
    backup_stores.py verify  BACKUP
    backup_stores.py restore BACKUP --launcher-state DIR [--middleware-state DIR] [--apply]
    backup_stores.py versions --launcher-state DIR

``backup`` writes a new dated directory under ``--dest``: every SQLite store through
SQLite's backup API (a consistent copy of a database in use, WAL included), the
content-addressed blob directories hash-checked as they are copied, the policy store's
small files, and a ``MANIFEST.json`` with each file's SHA-256 and each database's schema
version. A database is checked with ``PRAGMA integrity_check`` before the directory is
published, and the directory appears under its final name only when everything is in it.

It never reads the OpenShell gateway's database (its state directory is
``.../state/openshell``; it holds the registered providers' credentials and their key),
the instance's tokens or TLS keys, the package cache or the sandboxes' disks. Directories
are mode 700 and files mode 600.

``restore`` checks a backup the same way, then, with ``--apply``, puts each file where it
came from. A file it replaces is renamed ``<name>.pre-restore-<time>``, never deleted.
Nothing here deletes anything.
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import itertools
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
from collections.abc import Iterator, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final, NamedTuple

MANIFEST: Final = "MANIFEST.json"
FORMAT: Final = 1
CHUNK: Final = 1024 * 1024
DIGEST: Final = re.compile(r"[0-9a-f]{64}")
LABEL: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
WORKER_DATABASES: Final = (
    "worker.sqlite3",
    "environments.sqlite3",
    "environment-claims.sqlite3",
    "shared.sqlite3",
    "documents-catalog.sqlite3",
)
"""The worker's SQLite stores in its state directory. Its other directories are not
backed up: ``environments/`` and ``shared/`` are rebuilt from the sandboxes, and the
sandboxes' disks are the OpenShell VM driver's (docs/BACKUP-RESTORE.md)."""
LOCKS: Final = ("launcher.lock", "worker/state/worker.lock")
"""Files, under the launcher's state directory, that `up` and the worker hold locked."""


class BackupError(Exception):
    """Something the operator must read and act on."""


class Item(NamedTuple):
    """One thing to back up: ``kind`` is sqlite, blobs, tree or file."""

    kind: str
    source: Path
    rel: str


def refuse_gateway(path: Path) -> None:
    """Refuse a path in OpenShell's gateway state, ``<XDG_STATE_HOME>/openshell``."""
    parts = path.resolve().parts
    for before, after in itertools.pairwise(parts):
        if before == "state" and after == "openshell":
            raise BackupError(
                f"{path} is the OpenShell gateway's state: its database holds the "
                "providers' credentials and their key, and is never copied"
            )


def items(
    launcher_state: Path, middleware_state: Path | None, config_files: Sequence[Path]
) -> list[Item]:
    """What exists to back up, and where each goes in the backup."""
    control = launcher_state / "control"
    artifacts = control / "control.sqlite3.artifacts"
    worker = launcher_state / "worker" / "state"
    wanted = [
        Item("sqlite", control / "control.sqlite3", "control/control.sqlite3"),
        Item("blobs", artifacts / "blobs", "control/artifacts/blobs"),
        Item("file", artifacts / "storage.toml", "control/artifacts/storage.toml"),
        *(Item("sqlite", worker / name, f"worker/{name}") for name in WORKER_DATABASES),
        Item("blobs", worker / "artifacts" / "blobs", "worker/artifacts/blobs"),
        Item("blobs", worker / "lockfiles" / "blobs", "worker/lockfiles/blobs"),
    ]
    if middleware_state is not None:
        wanted.append(Item("sqlite", middleware_state / "events.db", "middleware/events.db"))
        wanted += [
            Item("tree", middleware_state / name, f"middleware/{name}")
            for name in ("policies", "sandboxes", "projects")
        ]
    wanted += [Item("file", path, f"config/{path.name}") for path in config_files]
    for item in wanted:
        refuse_gateway(item.source)
    return [item for item in wanted if item.source.exists()]


def lock_held(launcher_state: Path) -> str | None:
    """The lock file an instance still holds, if any: the launcher's or the worker's."""
    for name in LOCKS:
        path = launcher_state / name
        try:
            descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC)
        except OSError:
            continue
        try:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return name
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)
    return None


def _private_directory(path: Path) -> None:
    """Make ``path`` and any missing parent mode 700 (``mkdir(parents=True)`` would give a
    parent the umask's mode). A directory that is already there keeps its mode."""
    missing: list[Path] = []
    for candidate in (path, *path.parents):
        if candidate.exists():
            break
        missing.append(candidate)
    for directory in reversed(missing):
        directory.mkdir(mode=0o700, exist_ok=True)
        directory.chmod(0o700)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def _copy(source: Path, target: Path) -> str:
    """Copy a file to ``target`` (mode 600, renamed into place), returning its SHA-256."""
    _private_directory(target.parent)
    temporary = target.with_name(f".{target.name}.part")
    digest = hashlib.sha256()
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with source.open("rb") as reader, os.fdopen(descriptor, "wb") as writer:
            while chunk := reader.read(CHUNK):
                digest.update(chunk)
                writer.write(chunk)
            writer.flush()
            os.fsync(writer.fileno())
        temporary.replace(target)
    except BaseException:
        with contextlib.suppress(OSError):
            temporary.unlink()
        raise
    return digest.hexdigest()


def _versions(db: sqlite3.Connection) -> dict[str, int]:
    return {
        "user_version": int(db.execute("PRAGMA user_version").fetchone()[0]),
        "application_id": int(db.execute("PRAGMA application_id").fetchone()[0]),
    }


def _check_copy(path: Path) -> dict[str, int]:
    """Open a copy without changing it, run SQLite's integrity check and read its versions."""
    db = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro&immutable=1", uri=True)
    try:
        result = [row[0] for row in db.execute("PRAGMA integrity_check")]
        if result != ["ok"]:
            raise BackupError(
                f"{path.name} fails SQLite's integrity check: {'; '.join(result[:5])}"
            )
        return _versions(db)
    finally:
        db.close()


def snapshot_sqlite(source: Path, target: Path) -> dict[str, Any]:
    """A consistent copy of a SQLite database that may be in use, checked and mode 600."""
    _private_directory(target.parent)
    temporary = target.with_name(f".{target.name}.part")
    with contextlib.suppress(FileNotFoundError):
        temporary.unlink()
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(descriptor)
    reader = sqlite3.connect(f"{source.resolve().as_uri()}?mode=ro", uri=True, timeout=30)
    try:
        writer = sqlite3.connect(temporary)
        try:
            reader.backup(writer)
        finally:
            writer.close()
    except BaseException:
        with contextlib.suppress(OSError):
            temporary.unlink()
        raise
    finally:
        reader.close()
    try:
        versions = _check_copy(temporary)
        with temporary.open("rb") as stream:
            os.fsync(stream.fileno())
        temporary.chmod(0o600)
        temporary.replace(target)
    except BaseException:
        with contextlib.suppress(OSError):
            temporary.unlink()
        raise
    return {"sha256": _sha256(target), "bytes": target.stat().st_size, **versions}


def _files(root: Path) -> Iterator[Path]:
    """Regular files under ``root``, in a fixed order; a link is never followed."""
    for directory, names, files in os.walk(root, followlinks=False):
        names.sort()
        for name in sorted(files):
            path = Path(directory) / name
            if path.is_symlink() or not path.is_file():
                raise BackupError(f"{path} is not a regular file")
            yield path


def _previous_blob(previous: Path | None, rel: str, name: str, size: int) -> Path | None:
    if previous is None:
        return None
    candidate = previous / rel / name[:2] / name
    try:
        if candidate.is_file() and not candidate.is_symlink() and candidate.stat().st_size == size:
            return candidate
    except OSError:
        return None
    return None


def backup_blobs(
    source: Path, target: Path, rel: str, previous: Path | None
) -> dict[str, dict[str, Any]]:
    """Copy a content-addressed directory (``ab/abcd…``), checking each file's hash against
    its name. A blob the previous backup already holds is linked to it, not copied again."""
    manifest: dict[str, dict[str, Any]] = {}
    for path in _files(source):
        relative = path.relative_to(source)
        name = path.name
        if len(relative.parts) != 2 or not DIGEST.fullmatch(name) or relative.parts[0] != name[:2]:
            # `tmp/` holds uploads in progress, and is not content.
            continue
        size = path.stat().st_size
        destination = target / relative
        linked = _previous_blob(previous, rel, name, size)
        if linked is not None:
            _private_directory(destination.parent)
            with contextlib.suppress(FileNotFoundError):
                destination.unlink()
            try:
                os.link(linked, destination)
            except OSError:
                linked = None
        if linked is None:
            found = _copy(path, destination)
            if found != name:
                destination.unlink()
                raise BackupError(f"{path} does not hold the content its name says: {found}")
        manifest[f"{rel}/{relative.as_posix()}"] = {"sha256": name, "bytes": size}
    return manifest


def backup_tree(source: Path, target: Path, rel: str) -> dict[str, dict[str, Any]]:
    manifest: dict[str, dict[str, Any]] = {}
    for path in _files(source):
        relative = path.relative_to(source).as_posix()
        destination = target / relative
        manifest[f"{rel}/{relative}"] = {
            "sha256": _copy(path, destination),
            "bytes": destination.stat().st_size,
        }
    return manifest


def _bytes_needed(chosen: Sequence[Item]) -> int:
    total = 0
    for item in chosen:
        if item.kind == "blobs" or item.kind == "tree":
            total += sum(path.stat().st_size for path in _files(item.source))
        else:
            total += item.source.stat().st_size
            total += sum(
                path.stat().st_size
                for path in (item.source.with_name(item.source.name + "-wal"),)
                if path.exists()
            )
    return total


def _revision(directory: Path | None) -> str | None:
    git = shutil.which("git")
    if git is None or directory is None:
        return None
    done = subprocess.run(  # noqa: S603 -- fixed arguments, git resolved on PATH
        [git, "-C", str(directory), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=False,
    )
    return done.stdout.strip() if done.returncode == 0 else None


def backup(
    *,
    dest: Path,
    launcher_state: Path,
    middleware_state: Path | None,
    config_files: Sequence[Path] = (),
    label: str | None = None,
    live: bool = False,
    link_from: Path | None = None,
    repository: Path | None = None,
    now: datetime | None = None,
) -> Path:
    """Write a dated backup under ``dest`` and return its directory."""
    refuse_gateway(dest)
    if label is not None and not LABEL.fullmatch(label):
        raise BackupError(
            "a label is letters, digits, '.', '_' and '-', and starts with a digit or letter"
        )
    held = lock_held(launcher_state)
    if held is not None and not live:
        raise BackupError(
            f"the instance is running ({held} is held): stop it first, so the stores agree "
            "with each other, or pass --live to take each store as it is"
        )
    chosen = items(launcher_state, middleware_state, config_files)
    if not any(item.kind == "sqlite" for item in chosen):
        raise BackupError(f"no store found under {launcher_state}")
    _private_directory(dest)
    free = shutil.disk_usage(dest).free
    needed = _bytes_needed(chosen)
    if free < needed + needed // 10:
        raise BackupError(f"{dest} has {free} bytes free, and the stores need about {needed}")
    stamp = (now or datetime.now(UTC)).strftime("%Y%m%dT%H%M%SZ")
    name = f"{stamp}-{label}" if label else stamp
    final = dest / name
    partial = dest / f"{name}.partial"
    if final.exists() or partial.exists():
        raise BackupError(f"{final} already exists; wait a second or choose another label")
    _private_directory(partial)
    files: dict[str, dict[str, Any]] = {}
    try:
        for item in chosen:
            target = partial / item.rel
            if item.kind == "sqlite":
                files[item.rel] = snapshot_sqlite(item.source, target)
            elif item.kind == "blobs":
                files.update(backup_blobs(item.source, target, item.rel, link_from))
            elif item.kind == "tree":
                files.update(backup_tree(item.source, target, item.rel))
            else:
                files[item.rel] = {
                    "sha256": _copy(item.source, target),
                    "bytes": target.stat().st_size,
                }
        document = {
            "format": FORMAT,
            "created": (now or datetime.now(UTC)).isoformat(timespec="seconds"),
            "label": label,
            "live": held is not None,
            "revision": _revision(repository),
            "files": files,
        }
        manifest = partial / MANIFEST
        descriptor = os.open(manifest, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(document, stream, indent=1, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        partial.rename(final)
    except BaseException:
        # The unfinished directory stays, named `.partial`, for the operator to look at.
        raise
    return final


def read_manifest(directory: Path) -> dict[str, Any]:
    try:
        document = json.loads((directory / MANIFEST).read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise BackupError(f"{directory} has no readable {MANIFEST}: {error}") from None
    if not isinstance(document, dict) or document.get("format") != FORMAT:
        raise BackupError(f"{directory / MANIFEST} is not a format {FORMAT} manifest")
    return document


def verify(directory: Path) -> dict[str, Any]:
    """Check every file against the manifest, and every database for integrity."""
    document = read_manifest(directory)
    files: dict[str, dict[str, Any]] = document["files"]
    problems: list[str] = []
    for rel, entry in sorted(files.items()):
        path = directory / rel
        if not path.is_file() or path.is_symlink():
            problems.append(f"{rel}: missing")
        elif _sha256(path) != entry["sha256"]:
            problems.append(f"{rel}: content differs from the manifest")
        elif "user_version" in entry:
            try:
                _check_copy(path)
            except (BackupError, sqlite3.Error) as error:
                problems.append(f"{rel}: {error}")
    if problems:
        raise BackupError("the backup is damaged:\n  " + "\n  ".join(problems))
    return document


def expected_versions() -> dict[str, int]:
    """The schema versions this checkout expects, when its packages can be imported."""
    found: dict[str, int] = {}
    with contextlib.suppress(Exception):
        from sandbox_science.orchestrator.control import migrations

        found["control/control.sqlite3"] = int(migrations.VERSION)
    with contextlib.suppress(Exception):
        from sandbox_science.worker import schema

        found["worker/worker.sqlite3"] = int(schema.latest())
    return found


def pending_migrations(launcher_state: Path) -> tuple[list[str], int]:
    """What each store's schema version is, against what this checkout expects, and a status:
    0 when none would migrate, 3 when a store is older than the checkout (a start would
    migrate it, and the migration cannot be undone), 1 when one is newer (a start refuses)."""
    expected = expected_versions()
    paths = {
        "control/control.sqlite3": launcher_state / "control" / "control.sqlite3",
        "worker/worker.sqlite3": launcher_state / "worker" / "state" / "worker.sqlite3",
    }
    lines: list[str] = []
    status = 0
    for rel, path in paths.items():
        if not path.is_file():
            lines.append(f"{rel}: not there")
            continue
        db = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=30)
        try:
            (have,) = db.execute("PRAGMA user_version").fetchone()
        finally:
            db.close()
        want = expected.get(rel)
        if want is None:
            lines.append(f"{rel}: version {have}; this checkout's packages are not importable")
            status = max(status, 1)
        elif have < want:
            lines.append(f"{rel}: version {have}, this checkout migrates it to {want}")
            status = 3 if status == 0 else status
        elif have > want:
            lines.append(f"{rel}: version {have}, NEWER than the {want} this checkout expects")
            status = 1
        else:
            lines.append(f"{rel}: version {have}, as this checkout expects")
    return lines, status


def _target(rel: str, launcher_state: Path, middleware_state: Path | None) -> Path | None:
    head, _, tail = rel.partition("/")
    if head == "control":
        if tail.startswith("artifacts/"):
            return (
                launcher_state / "control" / "control.sqlite3.artifacts" / tail[len("artifacts/") :]
            )
        return launcher_state / "control" / tail
    if head == "worker":
        return launcher_state / "worker" / "state" / tail
    if head == "middleware" and middleware_state is not None:
        return middleware_state / tail
    return None


def _same(a: Path, b: Path) -> bool:
    return a.is_file() and a.stat().st_size == b.stat().st_size and _sha256(a) == _sha256(b)


def _set_aside(path: Path, stamp: str) -> Path:
    aside = path.with_name(f"{path.name}.pre-restore-{stamp}")
    path.rename(aside)
    return aside


def restore(
    directory: Path,
    *,
    launcher_state: Path,
    middleware_state: Path | None,
    config_dir: Path | None = None,
    apply: bool = False,
    now: datetime | None = None,
) -> list[str]:
    """Plan (or, with ``apply``, carry out) putting a verified backup back. Returns the lines
    that say what was or would be done."""
    document = verify(directory)
    for path in (launcher_state, middleware_state, config_dir):
        if path is not None:
            refuse_gateway(path)
    held = lock_held(launcher_state)
    if held is not None:
        raise BackupError(f"the instance is running ({held} is held): stop it first")
    stamp = (now or datetime.now(UTC)).strftime("%Y%m%dT%H%M%SZ")
    lines: list[str] = []
    expected = expected_versions()
    for rel, entry in sorted(document["files"].items()):
        have, want = entry.get("user_version"), expected.get(rel)
        if have is not None and want is not None and have > want:
            lines.append(
                f"warning: {rel} is schema version {have}, newer than the {want} this checkout "
                "expects: check out the revision that made the backup "
                f"({document.get('revision') or 'not recorded'}) before starting"
            )
    blobs = {"create": 0, "replace": 0}
    for rel, entry in sorted(document["files"].items()):
        if rel.startswith("config/"):
            if config_dir is None:
                continue
            target = config_dir / rel[len("config/") :]
        else:
            target = _target(rel, launcher_state, middleware_state)
        if target is None:
            lines.append(f"skip {rel}: no place given for it")
            continue
        source = directory / rel
        sqlite_store = "user_version" in entry
        if target.exists() and not sqlite_store and _same(target, source):
            continue
        action = "replace" if target.exists() else "create"
        if "/blobs/" in rel:
            blobs[action] += 1
        else:
            lines.append(f"{action} {target}")
        if not apply:
            continue
        _private_directory(target.parent)
        _copy(source, target.with_name(f".{target.name}.restoring"))
        staged = target.with_name(f".{target.name}.restoring")
        try:
            if sqlite_store:
                for suffix in ("-wal", "-shm", "-journal"):
                    side = target.with_name(target.name + suffix)
                    if side.exists():
                        _set_aside(side, stamp)
            if target.exists():
                _set_aside(target, stamp)
            staged.replace(target)
        except BaseException:
            with contextlib.suppress(OSError):
                staged.unlink()
            raise
    for action, count in blobs.items():
        if count:
            lines.append(f"{action} {count} content-addressed files")
    if not apply:
        lines.append("nothing changed: add --apply to do this")
    return lines


def _paths(arguments: argparse.Namespace) -> tuple[Path, Path | None]:
    middleware = Path(arguments.middleware_state) if arguments.middleware_state else None
    return Path(arguments.launcher_state), middleware


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Snapshot and restore an instance's stores.")
    commands = parser.add_subparsers(dest="command", required=True)
    make = commands.add_parser("backup", help="write a dated backup")
    make.add_argument("--dest", required=True, type=Path)
    make.add_argument("--label")
    make.add_argument("--live", action="store_true", help="back up while the instance runs")
    make.add_argument("--link-from", type=Path, help="a previous backup whose blobs to link")
    make.add_argument("--repository", type=Path, help="the checkout, to record its revision")
    make.add_argument("--config-file", type=Path, action="append", default=[])
    check = commands.add_parser("verify", help="check a backup")
    check.add_argument("backup", type=Path)
    known = commands.add_parser("versions", help="schema versions against this checkout")
    known.add_argument("--launcher-state", required=True, type=Path)
    put = commands.add_parser("restore", help="put a backup back")
    put.add_argument("backup", type=Path)
    put.add_argument("--config-dir", type=Path, help="also restore the config files here")
    put.add_argument("--apply", action="store_true", help="do it; without, only say what")
    for command in (make, put):
        command.add_argument("--launcher-state", required=True, type=Path)
        command.add_argument("--middleware-state", type=Path)
    arguments = parser.parse_args(argv)
    try:
        if arguments.command == "verify":
            document = verify(arguments.backup)
            print(f"{arguments.backup}: {len(document['files'])} files, all as recorded")
            return 0
        if arguments.command == "versions":
            lines, status = pending_migrations(arguments.launcher_state)
            print(*lines, sep="\n")
            return status
        launcher, middleware = _paths(arguments)
        if arguments.command == "backup":
            made = backup(
                dest=arguments.dest,
                launcher_state=launcher,
                middleware_state=middleware,
                config_files=arguments.config_file,
                label=arguments.label,
                live=arguments.live,
                link_from=arguments.link_from,
                repository=arguments.repository,
            )
            print(made)
            return 0
        for line in restore(
            arguments.backup,
            launcher_state=launcher,
            middleware_state=middleware,
            config_dir=arguments.config_dir,
            apply=arguments.apply,
        ):
            print(line)
        return 0
    except BackupError as error:
        print(f"backup_stores: {error}", file=sys.stderr)
        return 1
    except (OSError, sqlite3.Error) as error:
        print(f"backup_stores: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
