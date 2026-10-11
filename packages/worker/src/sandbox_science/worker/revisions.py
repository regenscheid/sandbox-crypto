# SPDX-License-Identifier: MIT
"""Environment revisions: how each change came, and its lockfile (Conda provisioning, W3).

An environment's installed packages have a revision: ``0`` when the worker first sees
them, one more for each change. The executor reports each change as a ``revision``
event, before any result that shows it: the packages' fingerprint, whether a package
command made the change through the environment's wrappers (CP-4), which attempt, and
the lockfile (CP-16, CP-18). ``hello`` and results report fingerprints too; one seen
only there is a change nobody can account for.

The executor is untrusted: research code shares its sandbox, and can change only its
own environment's records. Lockfiles must validate; their channels and URLs are named
again as CP-18 asks, whatever the executor wrote, and they are stored in the artifact
store by the SHA-256 of their JSON.
"""

from __future__ import annotations

import contextlib
import hashlib
import logging
import math
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final, cast

from pydantic import ValidationError

from sandbox_science.contracts import (
    EnvironmentRevision,
    ErrorKind,
    Lockfile,
    LockfileRef,
    PackageCommand,
    RevisionHistory,
)

from . import provisioning
from .artifacts import Blobs
from .errors import WorkerError
from .store import RevisionRecord, Store

_log = logging.getLogger(__name__)

MAX_LOCKFILE_BYTES: Final = 8 * 1024 * 1024
"""The largest lockfile the worker keeps, as JSON."""
_FINGERPRINT: Final = re.compile(r"[0-9a-f]{32}")


def _now() -> datetime:
    return datetime.now(UTC)


class Revisions:
    """Each environment's current revision and its history, in the worker's store."""

    def __init__(self, store: Store, blobs: Blobs) -> None:
        self.store = store
        self.blobs = blobs

    def observe(
        self,
        environment_id: str,
        fingerprint: object,
        changed_by: str = "",
        *,
        managed: bool | None = None,
        command: PackageCommand | None = None,
        lockfile: object = None,
    ) -> int | None:
        """The environment's revision once ``fingerprint`` is counted.

        The first fingerprint is revision 0, and each different one counts one up,
        recorded with how its change came. ``lockfile`` is the executor's, unchecked: it
        is kept for a new revision, or for the current one while that has none. Returns
        None while the environment has no revision, as when the fingerprint is not one.
        """
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
            command=None if first else command,
            lockfile=self.store_lockfile(lockfile),
            recorded_at=_now(),
        )
        current = RevisionRecord(revision, fingerprint, entry.changed_by or "")
        self.store.add_revision(environment_id, current, entry)
        if record is not None:
            how = {True: "a package command", False: "an unmanaged change", None: "unknown"}
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

    def attach(self, environment_id: str, fingerprint: object, lockfile: object) -> bool:
        """Keep ``lockfile`` for the current revision if it has none and ``fingerprint`` is its."""
        record = self.store.revision(environment_id)
        if record is None or fingerprint != record.fingerprint:
            return False
        return self._complete(environment_id, record, lockfile)

    def _complete(self, environment_id: str, record: RevisionRecord, lockfile: object) -> bool:
        entry = self.store.revision_entry(environment_id, record.revision)
        if entry is not None and entry.lockfile is not None:
            return False
        stored = self.store_lockfile(lockfile)
        if stored is None:
            return False
        if entry is None:
            # The revision was recorded before the worker kept a history.
            entry = EnvironmentRevision(
                revision=record.revision,
                fingerprint=record.fingerprint,
                changed_by=_attempt(record.changed_by),
                recorded_at=_now(),
            )
        self.store.save_revision_entry(
            environment_id, entry.model_copy(update={"lockfile": stored})
        )
        return True

    def history(self, environment_id: str) -> RevisionHistory:
        return RevisionHistory(
            environment_id=environment_id,
            revisions=tuple(self.store.revision_history(environment_id)),
        )

    def lockfile(self, environment_id: str, revision: int) -> tuple[Path, LockfileRef]:
        """Where a revision's lockfile is stored; ``not_found`` if it has none."""
        entry = self.store.revision_entry(environment_id, revision)
        if entry is None:
            raise WorkerError(
                ErrorKind.NOT_FOUND, f"environment {environment_id} has no revision {revision}"
            )
        if entry.lockfile is None:
            raise WorkerError(ErrorKind.NOT_FOUND, f"revision {revision} has no lockfile")
        path = self.blobs.path(entry.lockfile.sha256)
        if not path.is_file():
            raise WorkerError(ErrorKind.INTERNAL, f"the lockfile of revision {revision} is gone")
        return path, entry.lockfile

    def store_lockfile(self, raw: object) -> LockfileRef | None:
        """Check a lockfile from the executor, name its channels and URLs as CP-18 asks, and
        keep it in the artifact store; None if it is no lockfile."""
        if raw is None:
            return None
        try:
            lockfile = canonical(Lockfile.model_validate(raw))
            data = lockfile.model_dump_json().encode()
            Lockfile.model_validate_json(data)  # still valid once renamed
        except ValidationError as exc:
            _log.warning("a lockfile from the executor does not validate: %s", str(exc)[:300])
            return None
        if len(data) > MAX_LOCKFILE_BYTES:
            _log.warning("a lockfile of %d bytes from the executor is too large", len(data))
            return None
        sha256 = hashlib.sha256(data).hexdigest()
        if not self.blobs.path(sha256).is_file():
            writer = self.blobs.writer()
            try:
                writer.write(data)
                if not writer.commit(sha256):
                    return None
            except BaseException:
                writer.discard()
                raise
        return LockfileRef(
            sha256=sha256,
            size=len(data),
            conda_packages=len(lockfile.conda),
            pip_distributions=len(lockfile.pip),
        )


def canonical(lockfile: Lockfile) -> Lockfile:
    """``lockfile`` with its channels and URLs named as CP-18 asks, and its entries sorted.

    Whatever the executor wrote, a channel or URL in the package cache's layout never
    names the cache (provisioning.canonical_channel and canonical_url).
    """
    conda = sorted(
        (
            package.model_copy(update={"channel": provisioning.canonical_channel(package.channel)})
            if package.channel
            else package
            for package in lockfile.conda
        ),
        key=lambda item: (item.name, item.version, item.build, item.subdir or "", item.file or ""),
    )
    pip = sorted(
        (
            item.model_copy(update={"url": provisioning.canonical_url(item.url)})
            if item.url
            else item
            for item in lockfile.pip
        ),
        key=lambda item: (item.name.lower(), item.version, item.location, item.url or ""),
    )
    return lockfile.model_copy(update={"conda": tuple(conda), "pip": tuple(pip)})


def package_command(raw: object) -> PackageCommand | None:
    """The package command a ``revision`` event names, checked; None if it names none."""
    if not isinstance(raw, dict):
        return None
    fields = cast(dict[str, Any], raw)
    argv: object = fields.get("command")
    items = cast(list[object], argv) if isinstance(argv, list) else []
    words = [item[:1024] for item in items if isinstance(item, str)][:65]
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


def _attempt(value: str) -> str | None:
    """An attempt ID as a revision keeps it; None for none."""
    return value or None


def _integer(value: object, low: int, high: int) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool) and low <= value <= high:
        return value
    return None


def _epoch(value: object) -> datetime | None:
    if isinstance(value, int | float) and not isinstance(value, bool) and math.isfinite(value):
        with contextlib.suppress(OverflowError, OSError, ValueError):
            return datetime.fromtimestamp(value, UTC)
    return None
