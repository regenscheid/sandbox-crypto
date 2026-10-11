# SPDX-License-Identifier: MIT
"""Environment revisions: how each change came, and its lockfile (Conda provisioning, W3).

An environment's installed packages have a revision: ``0`` when the worker first sees
them, one more for each change. The executor reports each change as a ``revision``
event, before any result that shows it: the packages' fingerprint, whether package
commands made the change through the environment's wrappers (CP-4), which attempt, and
at an attempt's end the lockfile (CP-16, CP-18). ``hello`` and results report
fingerprints too; one seen only there is a change nobody can account for.

The executor is untrusted: research code shares its sandbox, and can change only its
own environment's records. Every lockfile field must match its pattern; channels and
URLs are named again as CP-18 asks, by the package cache's own URL, whatever the
executor wrote; and a lockfile that still names the cache is refused. That work runs in
a thread (``prepare``), never on the event loop. An environment keeps its newest 500
revisions, and lockfiles for its newest 50, in 64 MiB at most; a lockfile no revision
names any more is deleted.
"""

from __future__ import annotations

import contextlib
import hashlib
import logging
import math
import re
import urllib.parse
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final, cast

from pydantic import ValidationError

from sandbox_science.contracts import (
    CondaPackage,
    EnvironmentRevision,
    ErrorKind,
    Lockfile,
    LockfileRef,
    PackageCommand,
    PipDistribution,
    RevisionHistory,
)
from sandbox_science.contracts.revisions import (
    MAX_COMMANDS,
    MAX_LOCKFILE_BYTES,
    MAX_LOCKFILE_ENTRIES,
    MAX_REVISION_PAGE,
    MAX_REVISION_PAGE_BYTES,
)

from . import provisioning
from .artifacts import Blobs
from .errors import WorkerError
from .store import RevisionRecord, Store

_log = logging.getLogger(__name__)

MAX_HISTORY: Final = 500
"""Revisions kept for each environment: the newest."""
MAX_LOCKFILES: Final = 50
"""Lockfiles kept for each environment: those of its newest revisions that have one."""
MAX_LOCKFILE_STORE: Final = 64 * 1024 * 1024
"""Bytes of lockfiles kept for each environment."""
_FINGERPRINT: Final = re.compile(r"[0-9a-f]{32}")
_PAGE_FRAME: Final = 1024
"""Bytes of a page's JSON besides its revisions, and more."""


def _now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class Prepared:
    """A lockfile checked, renamed and stored, ready for a revision to name."""

    ref: LockfileRef
    data: bytes


class Revisions:
    """Each environment's current revision and its history, in the worker's store.

    ``cache`` is the package cache's URL as sandboxes reach it (the OpenShell backend's
    ``package_cache``): lockfiles name its channels and files by their upstream names,
    and never name it.
    """

    def __init__(self, store: Store, blobs: Blobs, *, cache: str | None = None) -> None:
        self.store = store
        self.blobs = blobs
        self.cache = cache
        self._mark = _address(cache) if cache is not None else None
        self._sweep()

    def _sweep(self) -> None:
        """Delete the stored lockfiles no revision names, and unfinished ones, as a worker
        that stopped between storing a lockfile and recording its revision leaves them."""
        with contextlib.suppress(OSError):
            for path in [*self.blobs.root.glob("blobs/*/*"), *self.blobs.root.glob("tmp/*")]:
                if path.parent.name == "tmp" or not self.store.lockfile_used(path.name):
                    with contextlib.suppress(OSError):
                        path.unlink()

    # Lockfiles, in a thread.

    def prepare(self, raw: object) -> Prepared | None:
        """Check a lockfile from the executor, name its channels and URLs, and store it;
        None if it is no lockfile the worker keeps. Runs in a thread: it can take a while.
        """
        if not isinstance(raw, dict):
            return None
        fields = cast(dict[str, Any], raw)
        for section in ("conda", "pip"):
            entries = fields.get(section)
            if (
                isinstance(entries, list)
                and len(cast(list[object], entries)) > MAX_LOCKFILE_ENTRIES
            ):
                _log.warning("a lockfile from the executor has too many entries")
                return None
        try:
            lockfile = canonical(Lockfile.model_validate(fields), self.cache)
        except ValidationError as exc:
            _log.warning("a lockfile from the executor does not validate: %s", str(exc)[:300])
            return None
        if lockfile is None:
            _log.warning("a lockfile from the executor names the package cache or a bad URL")
            return None
        data = lockfile.model_dump_json().encode()
        if self._mark is not None and self._mark in data.lower():
            _log.warning("a lockfile from the executor names the package cache")
            return None
        if len(data) > MAX_LOCKFILE_BYTES:
            _log.warning("a lockfile of %d bytes from the executor is too large", len(data))
            return None
        ref = LockfileRef(
            sha256=hashlib.sha256(data).hexdigest(),
            size=len(data),
            conda_packages=len(lockfile.conda),
            pip_distributions=len(lockfile.pip),
        )
        prepared = Prepared(ref, data)
        self._write(prepared)
        return prepared

    def _write(self, prepared: Prepared) -> None:
        if self.blobs.path(prepared.ref.sha256).is_file():
            return
        writer = self.blobs.writer()
        try:
            writer.write(prepared.data)
            writer.commit(prepared.ref.sha256)
        except BaseException:
            writer.discard()
            raise

    # Revisions, on the event loop: the store only.

    def observe(
        self,
        environment_id: str,
        fingerprint: object,
        changed_by: str = "",
        *,
        managed: bool | None = None,
        commands: Sequence[PackageCommand] = (),
        omitted: int = 0,
        lockfile: Prepared | None = None,
    ) -> int | None:
        """The environment's revision once ``fingerprint`` is counted.

        The first fingerprint is revision 0, and each different one counts one up,
        recorded with how its change came. ``lockfile`` is kept for a new revision, or for
        the current one while it has none, and deleted otherwise. Returns None while the
        environment has no revision, as when the fingerprint is not one.
        """
        try:
            return self._observe(
                environment_id, fingerprint, changed_by, managed, commands, omitted, lockfile
            )
        finally:
            if lockfile is not None:
                self._collect([lockfile.ref.sha256])

    def _observe(
        self,
        environment_id: str,
        fingerprint: object,
        changed_by: str,
        managed: bool | None,
        commands: Sequence[PackageCommand],
        omitted: int,
        lockfile: Prepared | None,
    ) -> int | None:
        record = self.store.revision(environment_id)
        if not isinstance(fingerprint, str) or not _FINGERPRINT.fullmatch(fingerprint):
            return None if record is None else record.revision
        if record is not None and record.fingerprint == fingerprint:
            if lockfile is not None:
                self._complete(environment_id, record, lockfile)
            return record.revision
        revision = 0 if record is None else record.revision + 1
        first = record is None  # the packages as first seen: no change, so nobody made one
        entry = EnvironmentRevision(
            revision=revision,
            fingerprint=fingerprint,
            managed=None if first else managed,
            changed_by=None if first else changed_by or None,
            commands=() if first else tuple(commands)[-MAX_COMMANDS:],
            omitted_commands=0 if first else omitted,
            lockfile=lockfile.ref if lockfile is not None else None,
            recorded_at=_now(),
        )
        current = RevisionRecord(revision, fingerprint, entry.changed_by or "")
        self.store.add_revision(environment_id, current, entry)
        if lockfile is not None:
            self._write(lockfile)  # in case it was deleted as unused meanwhile
        self._keep(environment_id)
        if record is not None:
            how = {True: "package commands", False: "an unmanaged change", None: "unknown"}
            _log.info(
                "environment %s is at revision %d: its installed packages changed (%s, %s)",
                environment_id,
                revision,
                how[entry.managed],
                changed_by or "no attempt known",
            )
        return revision

    def needs_lockfile(self, environment_id: str) -> bool:
        """Whether the current revision has no lockfile yet, as revision 0 has none at first."""
        record = self.store.revision(environment_id)
        if record is None:
            return False
        entry = self.store.revision_entry(environment_id, record.revision)
        return entry is None or entry.lockfile is None

    def attach(self, environment_id: str, fingerprint: object, lockfile: Prepared) -> bool:
        """Keep ``lockfile`` for the current revision if it has none and ``fingerprint`` is
        its; otherwise delete it."""
        record = self.store.revision(environment_id)
        kept = record is not None and fingerprint == record.fingerprint
        if record is not None and kept:
            kept = self._complete(environment_id, record, lockfile)
        if not kept:
            self._collect([lockfile.ref.sha256])
        return kept

    def _complete(self, environment_id: str, record: RevisionRecord, lockfile: Prepared) -> bool:
        entry = self.store.revision_entry(environment_id, record.revision)
        if entry is not None and entry.lockfile is not None:
            return False
        if entry is None:
            # The revision was recorded before the worker kept a history.
            entry = EnvironmentRevision(
                revision=record.revision,
                fingerprint=record.fingerprint,
                changed_by=record.changed_by or None,
                recorded_at=_now(),
            )
        self.store.save_revision_entry(
            environment_id, entry.model_copy(update={"lockfile": lockfile.ref})
        )
        self._write(lockfile)
        self._keep(environment_id)
        return True

    def _keep(self, environment_id: str) -> None:
        """Hold the environment to its newest revisions and lockfiles, and delete the
        lockfiles no revision names any more."""
        dropped = self.store.trim_revisions(environment_id, MAX_HISTORY)
        held = self.store.lockfiles(environment_id)
        total = sum(size for _, _, size in held)
        while held and (len(held) > MAX_LOCKFILES or total > MAX_LOCKFILE_STORE):
            revision, sha256, size = held.pop(0)
            entry = self.store.revision_entry(environment_id, revision)
            if entry is not None:
                self.store.save_revision_entry(
                    environment_id, entry.model_copy(update={"lockfile": None})
                )
            dropped.append(sha256)
            total -= size
        self._collect(dropped)

    def _collect(self, sha256s: Sequence[str]) -> None:
        for sha256 in dict.fromkeys(sha256s):
            if not self.store.lockfile_used(sha256):
                with contextlib.suppress(OSError):
                    self.blobs.path(sha256).unlink()

    def forget(self, environment_id: str) -> None:
        """Forget an environment's revisions, as a new environment takes its ID (D78)."""
        self._collect(self.store.forget_revisions(environment_id))

    # Reading.

    def history(
        self,
        environment_id: str,
        *,
        after: int = -1,
        limit: int = MAX_REVISION_PAGE,
        since: datetime | None = None,
    ) -> RevisionHistory:
        """A page of revisions after ``after``, oldest first, none recorded before ``since``:
        at most ``limit``, and fewer where their JSON would pass MAX_REVISION_PAGE_BYTES."""
        limit = max(1, min(limit, MAX_REVISION_PAGE))
        found = self.store.revision_page(
            environment_id, after, limit + 1, since.timestamp() if since else None
        )
        page: list[EnvironmentRevision] = []
        size = _PAGE_FRAME
        for entry in found[:limit]:
            size += len(entry.model_dump_json().encode()) + 1
            if page and size > MAX_REVISION_PAGE_BYTES:
                break
            page.append(entry)
        more = len(found) > len(page)
        return RevisionHistory(
            environment_id=environment_id,
            revisions=tuple(page),
            next_after=page[-1].revision if more and page else None,
        )

    def lockfile(
        self, environment_id: str, revision: int, *, since: datetime | None = None
    ) -> tuple[Path, LockfileRef]:
        """Where a revision's lockfile is stored; ``not_found`` if it has none."""
        entry = self.store.revision_entry(environment_id, revision)
        if entry is None or (since is not None and entry.recorded_at < since):
            raise WorkerError(
                ErrorKind.NOT_FOUND, f"environment {environment_id} has no revision {revision}"
            )
        if entry.lockfile is None:
            raise WorkerError(ErrorKind.NOT_FOUND, f"revision {revision} has no lockfile")
        path = self.blobs.path(entry.lockfile.sha256)
        if not path.is_file():
            raise WorkerError(ErrorKind.INTERNAL, f"the lockfile of revision {revision} is gone")
        return path, entry.lockfile


def canonical(lockfile: Lockfile, cache: str | None) -> Lockfile | None:
    """``lockfile`` with its channels and URLs named as CP-18 asks, and its entries sorted;
    None if a channel or URL cannot be named safely, or still names the package cache.

    Only conda.anaconda.org and the cache's own URL are renamed: a channel or file on any
    other host, laid out like the cache or not, keeps its URL.
    """
    host = _hostname(cache) if cache is not None else None
    conda: list[CondaPackage] = []
    for package in lockfile.conda:
        channel = package.channel
        if channel is not None:
            channel = provisioning.canonical_channel(channel, cache)
            if channel is None or (host is not None and _hostname(channel) == host):
                return None
        conda.append(package.model_copy(update={"channel": channel}))
    pip: list[PipDistribution] = []
    for item in lockfile.pip:
        url, sha256 = item.url, item.sha256
        if url is not None:
            kept = provisioning.canonical_url(url, cache)
            if kept is None or (host is not None and _hostname(kept[0]) == host):
                return None
            url, sha256 = kept[0], sha256 or kept[1]
        pip.append(item.model_copy(update={"url": url, "sha256": sha256}))
    conda.sort(key=lambda p: (p.name, p.version, p.build, p.subdir or "", p.file or ""))
    pip.sort(key=lambda d: (d.name.lower(), d.version, d.location, d.url or ""))
    # Built again from plain values, so that each renamed value passes the patterns too.
    return Lockfile.model_validate(
        lockfile.model_copy(update={"conda": tuple(conda), "pip": tuple(pip)}).model_dump()
    )


def _address(cache: str) -> bytes | None:
    """The package cache's host and port as a lockfile would name them, lower case: no
    field of a kept lockfile holds them."""
    try:
        parts = urllib.parse.urlsplit(cache)
        port = parts.port
    except ValueError:
        return None
    if not parts.hostname:
        return None
    host = parts.hostname.lower()
    return (f"{host}:{port}" if port is not None else host).encode()


def _hostname(value: str) -> str | None:
    """The host a URL names, lower case; None for a channel name, or no URL."""
    try:
        host = urllib.parse.urlsplit(value).hostname
    except ValueError:
        return None
    return host.lower() if host else None


def package_commands(raw: object, omitted: object) -> tuple[tuple[PackageCommand, ...], int]:
    """The package commands a ``revision`` event names, checked and redacted, and how many
    earlier ones it left out."""
    items = cast(list[object], raw) if isinstance(raw, list) else []
    commands: list[PackageCommand] = []
    for item in items[-MAX_COMMANDS:]:
        command = package_command(item)
        if command is not None:
            commands.append(command)
    count = _integer(omitted, 0, 2**31 - 1) or 0
    return tuple(commands), count + max(0, len(items) - MAX_COMMANDS)


def package_command(raw: object) -> PackageCommand | None:
    """One package command of a ``revision`` event, checked; None if it names none.

    Its arguments are bounded and redacted again (provisioning.bounded_command), whatever
    the executor sent: credentials on a command line are never kept.
    """
    if not isinstance(raw, dict):
        return None
    fields = cast(dict[str, Any], raw)
    argv: object = fields.get("command")
    items = cast(list[object], argv) if isinstance(argv, list) else []
    head = items[: provisioning.MAX_ARGUMENTS + 1]  # what a bounded command line holds
    words = provisioning.bounded_command(
        [item for item in head if isinstance(item, str)], len(items)
    )
    try:
        return PackageCommand.model_validate(
            {
                "argv": words,
                "via": fields.get("via"),
                "exit_code": _integer(fields.get("exit_code"), -(2**31), 2**31 - 1),
                "signal": _integer(fields.get("signal"), 1, 255),
                "started_at": _epoch(fields.get("started")),
                "ended_at": _epoch(fields.get("ended")),
            }
        )
    except ValidationError:
        return None


def _integer(value: object, low: int, high: int) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool) and low <= value <= high:
        return value
    return None


def _epoch(value: object) -> datetime | None:
    if isinstance(value, int | float) and not isinstance(value, bool) and math.isfinite(value):
        with contextlib.suppress(OverflowError, OSError, ValueError):
            return datetime.fromtimestamp(value, UTC)
    return None
