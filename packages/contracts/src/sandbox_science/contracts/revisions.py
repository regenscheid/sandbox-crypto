# SPDX-License-Identifier: MIT
"""Environment revisions and their lockfiles (docs/specs/conda-provisioning.md, CP-16 to CP-19).

An environment's installed packages have a revision: ``0`` when the worker first sees
them, one more for each change. Each revision says how its change came: through a
package command the environment's wrappers ran (managed), or not (unmanaged, CP-4).
Its lockfile lists the packages it left, by channel, subdir, file name and SHA-256,
never by the package cache's address, so the environment can be rebuilt in another
pool (CP-18). The worker keeps lockfiles in a store of their own, by their SHA-256.

Every field of a lockfile has a pattern: printable ASCII, without the spaces, line
breaks or path separators a value could use to pass for another. A channel or URL is a
plain ``http(s)`` or ``file`` URL with an ASCII host, so a look-alike host shows as the
``xn--`` name it is.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Annotated, Final, Literal

from pydantic import Field, StringConstraints

from .model import Contract, Identifier, Sha256


def pep503_name(name: str) -> str:
    """A pip distribution's name as PEP 503 compares it (``policy.names.pep503`` too)."""
    return re.sub(r"[-_.]+", "-", name).lower()


MAX_LOCKFILE_ENTRIES: Final = 20_000
"""Entries in each section of a lockfile."""
MAX_COMMANDS: Final = 8
"""Package commands a revision lists: the last ones of its change."""
MAX_REVISION_PAGE: Final = 100
"""Revisions in one page of an environment's history."""
MAX_REVISION_PAGE_BYTES: Final = 4 * 1024 * 1024
"""The largest page of an environment's history a worker serves, as JSON."""
MAX_LOCKFILE_BYTES: Final = 8 * 1024 * 1024
"""The largest lockfile a worker keeps and serves, as JSON."""

PackageName = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_][A-Za-z0-9._+-]{0,255}$")]
"""A Conda package's or a distribution's name, such as ``numpy`` or ``_openmp_mutex``."""
Version = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_.+!-]{1,256}$")]
"""A version, Conda's or PEP 440's, such as ``2.1.0``, ``1!2.0`` or ``1.0+local``."""
Build = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_.+]{1,256}$")]
"""A Conda build string, such as ``py313h0_0``."""
Subdir = Annotated[str, StringConstraints(pattern=r"^[a-z0-9]+(?:-[a-z0-9_]+)?$")]
"""A Conda subdir, such as ``linux-64`` or ``noarch``."""
PackageFile = Annotated[
    str, StringConstraints(pattern=r"^[A-Za-z0-9_][A-Za-z0-9._+!-]{0,250}\.(?:conda|tar\.bz2)$")
]
"""A Conda package's file name, such as ``numpy-2.1.0-py313h0_0.conda``."""
Channel = Annotated[
    str,
    StringConstraints(
        max_length=2048,
        pattern=r"^(?:[A-Za-z0-9_][A-Za-z0-9._-]{0,255}(?:/label/[A-Za-z0-9._-]{1,255})?"
        r"|(?:https?|file)://[!-~]+)$",
    ),
]
"""A channel's name, such as ``conda-forge`` or ``conda-forge/label/dev``, or the URL of a
channel that is neither on conda.anaconda.org nor behind the package cache."""
SourceUrl = Annotated[
    str,
    StringConstraints(
        max_length=2048,
        pattern=r"^(?:https?|file|(?:git|hg|svn|bzr)\+(?:https?|ssh|file))://[!-~]+$",
    ),
]
"""Where a direct-URL install came from: an ``http(s)``, ``file`` or version-control URL."""
Installer = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_.+-]{1,64}$")]
License = Annotated[str, StringConstraints(pattern=r"^[ -~]{1,256}$")]
"""A license expression, such as ``BSD-3-Clause AND MIT``: printable ASCII."""
Argument = Annotated[str, StringConstraints(max_length=512, pattern=r"^[^\x00-\x1f\x7f-\x9f]*$")]
"""A command-line argument, without control characters."""
Fingerprint = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{32}$")]
"""The executor's digest of an environment's package records, which tells revisions apart."""
Md5 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{32}$")]
CapabilityName = Annotated[str, StringConstraints(pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$")]
"""A capability of the catalog, such as ``gp`` or ``sage`` (Conda provisioning, CP-3)."""
ProbeId = Annotated[str, StringConstraints(pattern=r"^[a-z0-9][a-z0-9:._-]{0,127}$")]
"""A probe of the catalog, such as ``gp-factor``, or a version check, ``installed:conda:pari``."""
MAX_PROBES: Final = 64
"""Probe results a revision keeps: the newest of each probe."""
MAX_PROBE_OUTPUT: Final = 2000
"""Characters of a probe's output a result keeps."""


class ProbeResult(Contract):
    """A probe's result at one environment revision (CP-21 to CP-24).

    A capability's status belongs to the revision its probes ran at. A version check,
    which finds a requested package in the revision's lockfile, is ``import_check``: it
    shows the package is installed, not that it works (CP-22).
    """

    capability: CapabilityName | None = None
    """None for the version check of a package requested outside the catalog."""
    probe: ProbeId
    version: Annotated[int, Field(ge=1)] = 1
    kind: Literal["functional", "import_check"]
    status: Literal["passed", "failed", "error"]
    """``failed``: it ran and the result was wrong; ``error``: it could not run to an end,
    such as a timeout."""
    revision: Annotated[int, Field(ge=0)] | None = None
    """The revision it ran at; None when the worker could not tell, and then it belongs to
    no revision's record."""
    execution_id: Identifier | None = None
    """The execution that ran it; None for a check of the lockfile."""
    output: Annotated[str, StringConstraints(max_length=MAX_PROBE_OUTPUT)] = ""
    duration_seconds: Annotated[float, Field(ge=0, allow_inf_nan=False)] = 0.0
    operation: Identifier | None = None
    """The provisioning operation that ran it."""
    recorded_at: datetime


class CondaPackage(Contract):
    """A Conda package: by channel, subdir, file name and SHA-256 (CP-18)."""

    name: PackageName
    version: Version
    build: Build
    channel: Channel | None = None
    """A channel name such as ``conda-forge``, for conda.anaconda.org and the package cache
    alike; a URL only for a channel that is neither."""
    subdir: Subdir | None = None
    file: PackageFile | None = None
    sha256: Sha256 | None = None
    """None where the package's index gave none."""
    md5: Md5 | None = None
    license: License | None = None


class PipDistribution(Contract):
    """A Python distribution that pip or uv installed beside the Conda packages."""

    name: PackageName
    version: Version
    installer: Installer | None = None
    location: Literal["prefix", "user"] = "prefix"
    """The prefix's site-packages, or the user's (``pip install --user``)."""
    url: SourceUrl | None = None
    """For a direct-URL install (PEP 610): where it came from, without credentials. A file
    that came through the package cache is named by its files.pythonhosted.org URL."""
    sha256: Sha256 | None = None
    """The archive's SHA-256, where the installer recorded it (a direct-URL install)."""
    license: License | None = None
    """A bounded declaration from installed core metadata; absent or ambiguous is unknown.
    This does not verify the license, its SPDX identifiers or permission to use it."""


class Lockfile(Contract):
    """The packages of an environment revision (CP-18): Conda packages, then what pip or uv
    installed on top of them. Entries are sorted by name."""

    format: Literal["sandbox-science-lockfile"] = "sandbox-science-lockfile"
    version: Literal[1] = 1
    python: Version | None = None
    """The environment's Python version."""
    platform: Subdir | None = None
    """The Conda subdir of the sandbox's machine, such as ``linux-64``."""
    conda: tuple[CondaPackage, ...] = Field(default=(), max_length=MAX_LOCKFILE_ENTRIES)
    pip: tuple[PipDistribution, ...] = Field(default=(), max_length=MAX_LOCKFILE_ENTRIES)
    skipped: Annotated[int, Field(ge=0)] = 0
    """Package records left out, because they name a package in a form these fields
    cannot hold."""


class LockfileRef(Contract):
    """A lockfile in the worker's lockfile store, by the SHA-256 of its JSON.

    ``GET /v1/environments/{id}/revisions/{revision}/lockfile`` serves it.
    """

    sha256: Sha256
    size: Annotated[int, Field(ge=0)]
    conda_packages: Annotated[int, Field(ge=0)] = 0
    pip_distributions: Annotated[int, Field(ge=0)] = 0


class PackageCommand(Contract):
    """A package command that changed an environment through its wrappers (CP-4)."""

    argv: tuple[Argument, ...] = Field(max_length=33)
    """As it ran, with credentials in URLs and token options replaced by ``***`` and control
    characters by ``?``: at most 32 arguments of 512 characters and 4,096 in all, and a
    note for any beyond."""
    via: Literal["wrapper", "module"]
    """A wrapper on ``PATH`` (``pip install …``), or the hook for a module run by Python
    (``python -m pip``, IPython's ``%pip``)."""
    exit_code: int | None = None
    signal: int | None = None
    started_at: datetime | None = None
    """When it got its turn, by the sandbox's clock: it may have waited for another first."""
    ended_at: datetime | None = None


MAX_REVISION_NOTES: Final = 16
MAX_REVISION_NOTE: Final = 500
RevisionNote = Annotated[
    str,
    StringConstraints(min_length=1, max_length=MAX_REVISION_NOTE, pattern=r"^[^\x00-\x1f\x7f]+$"),
]
"""One line about a revision, in the worker's words."""


class EnvironmentRevision(Contract):
    """A state of an environment's installed packages (W3; CP-16, CP-19)."""

    revision: Annotated[int, Field(ge=0)]
    fingerprint: Fingerprint
    managed: bool | None = None
    """True when package commands made the change through the environment's wrappers;
    False for an unmanaged change, which no such command accounts for (CP-4); None for
    revision 0, and for a change the worker learned of from a fingerprint alone."""
    changed_by: Identifier | None = None
    """The attempt that ran the package commands or, for an unmanaged change, the attempt
    after which it was seen; None when nobody can tell."""
    commands: tuple[PackageCommand, ...] = Field(default=(), max_length=MAX_COMMANDS)
    """The package commands that made a managed change, oldest first: the last 8."""
    omitted_commands: Annotated[int, Field(ge=0)] = 0
    """Earlier commands of the same change that ``commands`` leaves out."""
    lockfile: LockfileRef | None = None
    """None until the worker has it. A revision that changed again before its lockfile
    was taken has none, and the worker keeps only the newest lockfiles of an environment."""
    operation: Identifier | None = None
    """The provisioning operation whose install made the change, when one did."""
    probes: tuple[ProbeResult, ...] = Field(default=(), max_length=MAX_PROBES)
    """The probes that ran at this revision, the newest of each: the status of the
    capabilities at this revision (CP-24)."""
    notes: tuple[RevisionNote, ...] = Field(default=(), max_length=MAX_REVISION_NOTES)
    """What the worker was told about the change beyond its packages: for a new image's
    base under a retained workspace layer, the packages installed in the layer that the
    new base leaves without a dependency, or that stay over a base version (docs/IMAGES.md,
    "The environment outside the workspace")."""
    recorded_at: datetime


class RevisionHistory(Contract):
    """A page of an environment's revisions, as ``GET /v1/environments/{id}/revisions``
    returns it."""

    environment_id: Identifier
    revisions: tuple[EnvironmentRevision, ...] = Field(default=(), max_length=MAX_REVISION_PAGE)
    """Oldest first: at most 100, in 4 MiB of JSON. The worker keeps an environment's
    newest revisions only."""
    next_after: int | None = None
    """When more revisions follow: the ``after`` that asks for the next page."""
