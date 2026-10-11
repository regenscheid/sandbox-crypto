# SPDX-License-Identifier: MIT
"""Environment revisions and their lockfiles (docs/specs/conda-provisioning.md, CP-16 to CP-19).

An environment's installed packages have a revision: ``0`` when the worker first sees
them, one more for each change. Each revision says how its change came: through a
package command the environment's wrappers ran (managed), or not (unmanaged, CP-4).
Its lockfile lists the packages it left, by channel, subdir, file name and SHA-256,
never by the package cache's address, so the environment can be rebuilt in another
pool (CP-18). The worker keeps lockfiles in its artifact store, by their SHA-256.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Final, Literal

from pydantic import Field, StringConstraints

from .artifacts import Sha256
from .model import Contract, Identifier

MAX_LOCKFILE_ENTRIES: Final = 20_000
"""Entries in each section of a lockfile."""

Name = Annotated[str, StringConstraints(min_length=1, max_length=256)]
"""A package's name, version, build, subdir, installer or license."""
Text = Annotated[str, StringConstraints(min_length=1, max_length=2048)]
"""A channel, file name or URL."""
Argument = Annotated[str, StringConstraints(max_length=1024)]
Fingerprint = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{32}$")]
"""The executor's digest of an environment's package records, which tells revisions apart."""
Md5 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{32}$")]


class CondaPackage(Contract):
    """A Conda package: by channel, subdir, file name and SHA-256 (CP-18)."""

    name: Name
    version: Name
    build: Name
    channel: Text | None = None
    """A channel name such as ``conda-forge``, for conda.anaconda.org and the package cache
    alike; a URL only for a channel that is neither."""
    subdir: Name | None = None
    file: Text | None = None
    sha256: Sha256 | None = None
    """None where the package's index gave none."""
    md5: Md5 | None = None
    license: Name | None = None


class PipDistribution(Contract):
    """A Python distribution that pip or uv installed beside the Conda packages."""

    name: Name
    version: Name
    installer: Name | None = None
    location: Literal["prefix", "user"] = "prefix"
    """The prefix's site-packages, or the user's (``pip install --user``)."""
    url: Text | None = None
    """For a direct-URL install (PEP 610): where it came from, without credentials. A file
    that came through the package cache is named by its files.pythonhosted.org URL."""
    sha256: Sha256 | None = None
    """The archive's SHA-256, where the installer recorded it (a direct-URL install)."""


class Lockfile(Contract):
    """The packages of an environment revision (CP-18): Conda packages, then what pip or uv
    installed on top of them. Entries are sorted by name."""

    format: Literal["sandbox-science-lockfile"] = "sandbox-science-lockfile"
    version: Literal[1] = 1
    python: Name | None = None
    """The environment's Python version."""
    platform: Name | None = None
    """The Conda subdir of the sandbox's machine, such as ``linux-64``."""
    conda: tuple[CondaPackage, ...] = Field(default=(), max_length=MAX_LOCKFILE_ENTRIES)
    pip: tuple[PipDistribution, ...] = Field(default=(), max_length=MAX_LOCKFILE_ENTRIES)


class LockfileRef(Contract):
    """A lockfile in the worker's artifact store, by the SHA-256 of its JSON.

    ``GET /v1/environments/{id}/revisions/{revision}/lockfile`` serves it.
    """

    sha256: Sha256
    size: Annotated[int, Field(ge=0)]
    conda_packages: Annotated[int, Field(ge=0)] = 0
    pip_distributions: Annotated[int, Field(ge=0)] = 0


class PackageCommand(Contract):
    """A package command that changed an environment through its wrappers (CP-4)."""

    argv: tuple[Argument, ...] = Field(max_length=65)
    """As it ran: at most 64 arguments of 1,024 characters, and a note for any beyond."""
    via: Literal["wrapper", "module"]
    """A wrapper on ``PATH`` (``pip install …``), or the hook for a module run by Python
    (``python -m pip``, IPython's ``%pip``)."""
    exit_code: int | None = None
    signal: int | None = None
    started_at: datetime | None = None
    """When it got its turn, by the sandbox's clock: it may have waited for another first."""
    ended_at: datetime | None = None


class EnvironmentRevision(Contract):
    """A state of an environment's installed packages (W3; CP-16, CP-19)."""

    revision: Annotated[int, Field(ge=0)]
    fingerprint: Fingerprint
    managed: bool | None = None
    """True when a package command made the change through the environment's wrappers;
    False for an unmanaged change, which no such command accounts for (CP-4); None for
    revision 0 and for a change the worker found when an executor started."""
    changed_by: Identifier | None = None
    """The attempt that ran the package command or, for an unmanaged change, the attempt
    after which it was seen; None when nobody can tell."""
    command: PackageCommand | None = None
    lockfile: LockfileRef | None = None
    """None until the worker has it. A revision that changed again before its lockfile
    was taken has none."""
    recorded_at: datetime


class RevisionHistory(Contract):
    """An environment's revisions, as ``GET /v1/environments/{id}/revisions`` returns them."""

    environment_id: Identifier
    revisions: tuple[EnvironmentRevision, ...] = ()
    """Oldest first: the last is the current revision. Revisions the worker recorded before
    it kept a history are missing."""
