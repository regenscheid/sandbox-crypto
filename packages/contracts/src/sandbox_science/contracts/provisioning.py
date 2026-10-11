# SPDX-License-Identifier: MIT
"""Provisioning operations: an agent asks for a capability or packages (Conda provisioning).

docs/specs/conda-provisioning.md defines the behavior (CP-1 to CP-25). An operation
resolves the request in a dry run inside the target environment's sandbox, decides each
resolved file with the project policy, as the policy middleware will decide its
download, asks for permission when the policy escalates, installs in the environment's
turn, records what changed, and probes the capability. Operations on one environment run
one at a time, in the order they were requested (CP-11).

Specs are checked before they reach a package manager: a Conda spec is a channel, a name
and a version constraint; a pip spec is a PEP 508 requirement without a URL or markers.
Neither may look like an option, a file or a URL.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Final, Literal, Self

from pydantic import Field, StringConstraints, model_validator

from .artifacts import Sha256
from .context import ExecutionContext
from .environments import PolicyRevision
from .model import Contract, Identifier
from .revisions import (
    Build,
    CapabilityName,
    Channel,
    License,
    PackageName,
    ProbeResult,
    SourceUrl,
    Subdir,
    Version,
)
from .worker import ErrorInfo

MAX_SPECS: Final = 32
"""Specs of each kind one request may name."""
MAX_FILES: Final = 2000
"""Resolved package files one operation may install."""
MAX_TEXT: Final = 2000
"""Characters of a purpose, reason or next step."""

CondaSpec = Annotated[
    str,
    StringConstraints(
        max_length=256,
        pattern=r"^(?:[a-z0-9][a-z0-9._-]{0,63}(?:/[a-z0-9_-]{1,32})?::)?"
        r"[a-z0-9_][a-z0-9._+-]{0,127}(?:[ =<>!~][A-Za-z0-9 .*+_!,|<>=~-]{0,120})?$",
    ),
]
"""A Conda spec: an optional ``channel::`` (or ``channel/subdir::``), a lower-case name and
a version constraint, such as ``pari>=2.15`` or ``conda-forge::fpylll``. No brackets,
options, files or URLs."""
_PIP_OPERATOR: Final = r"(?:===|==|!=|<=|>=|~=|<|>)"
_PIP_VERSION: Final = r"[A-Za-z0-9.*+!_-]{1,64}"
PipSpec = Annotated[
    str,
    StringConstraints(
        max_length=256,
        pattern=rf"^[A-Za-z0-9](?:[A-Za-z0-9._-]{{0,126}}[A-Za-z0-9])?"
        rf"(?:\[[A-Za-z0-9._,-]{{1,128}}\])?"
        rf"(?: ?{_PIP_OPERATOR} ?{_PIP_VERSION}(?: ?, ?{_PIP_OPERATOR} ?{_PIP_VERSION}){{0,7}})?$",
    ),
]
"""A PEP 508 requirement without a URL or markers, such as ``pdf_oxide>=0.3``."""
SystemPackage = Annotated[str, StringConstraints(pattern=r"^[a-z0-9][a-z0-9.+-]{0,127}$")]
"""A distribution package, such as Ubuntu's ``libgmp-dev``: never installed at runtime (CP-5)."""
Text = Annotated[
    str,
    StringConstraints(min_length=1, max_length=MAX_TEXT, pattern=r"^[^\x00-\x08\x0b-\x1f\x7f]+$"),
]
"""Text for people: one to 2,000 characters, without control characters but tab and newline."""
FileName = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_][A-Za-z0-9._+!-]{0,250}$")]
"""A package file's name: a Conda package or a Python distribution's wheel or sdist."""
RuleName = Annotated[str, StringConstraints(max_length=256, pattern=r"^[ -~]+$")]


class ProvisionRequest(Contract):
    """Install a capability of the catalog, or package specs, into an environment (CP-1).

    ``context.environment_id`` is the target: the session's own environment unless the
    agent names another of the project's. Either ``capability``, or package specs:
    Conda specs, and pip specs where conda-forge lacks a package (CP-15), with the reason.
    System packages can never be installed at runtime (CP-5): naming them records the
    need, and the operation is denied.
    """

    operation_id: Identifier
    context: ExecutionContext
    purpose: Text
    """Why the work needs it, in the agent's words; recorded with the operation."""
    capability: CapabilityName | None = None
    conda: tuple[CondaSpec, ...] = Field(default=(), max_length=MAX_SPECS)
    pip: tuple[PipSpec, ...] = Field(default=(), max_length=MAX_SPECS)
    pip_reason: Text | None = None
    """Why pip: what conda-forge lacks. Recorded as the fallback's reason (CP-15)."""
    system: tuple[SystemPackage, ...] = Field(default=(), max_length=MAX_SPECS)

    @model_validator(mode="after")
    def _one_kind(self) -> Self:
        specs = bool(self.conda or self.pip or self.system)
        if (self.capability is None) == (not specs):
            raise ValueError("name a capability, or package specs (conda, pip or system)")
        if bool(self.pip) != (self.pip_reason is not None):
            raise ValueError("pip specs need pip_reason (what conda-forge lacks), and only they")
        return self


class ProvisionState(StrEnum):
    """Where an operation is (CP-11): waiting for its turn, then each step in turn."""

    QUEUED = "queued"
    CHECKING = "checking"
    """Running a capability's probes, to see whether it works already (CP-2)."""
    RESOLVING = "resolving"
    AWAITING_PERMISSION = "awaiting_permission"
    INSTALLING = "installing"
    PROBING = "probing"
    FINISHED = "finished"


class ProvisionOutcome(StrEnum):
    """What the operation came to (CP-25)."""

    ALREADY_SATISFIED = "already_satisfied"
    COMPLETED = "completed"
    AWAITING_PERMISSION = "awaiting_permission"
    """Not an end: the operation waits for an answer to its permission request (CP-8)."""
    DENIED = "denied"
    FAILED = "failed"
    ROLLED_BACK = "rolled_back"
    NEEDS_RECOVERY = "needs_recovery"


Stage = Literal["queue", "check", "resolve", "permission", "download", "link", "post-link", "probe"]
"""Where an operation stopped (CP-17): before its turn came, while checking a capability,
resolving, waiting for permission, downloading, linking, in a post-link script, or probing."""

Reason = Literal[
    "base_image_change_required",
    "agpl_license",
    "policy_denied",
    "permission_denied",
    "permission_timed_out",
    "permission_unavailable",
    "resolve_failed",
    "install_failed",
    "environment_changed",
    "policy_changed",
    "probe_failed",
    "cancelled",
    "worker_restarted",
]
"""Why an operation did not complete, for programs; ``error`` says it in words."""


class ResolvedFile(Contract):
    """One package file the dry run resolved, and the policy's decision on its download (CP-7).

    Channels are named as lockfiles name them, never by the package cache's address
    (CP-18). ``action`` is what the policy middleware will decide for the file's download,
    found with the same policy library (CP-6).
    """

    ecosystem: Literal["conda", "pypi"]
    name: PackageName
    version: Version
    build: Build | None = None
    channel: Channel | None = None
    subdir: Subdir | None = None
    file: FileName
    url: SourceUrl | None = None
    """For a Python distribution, its files.pythonhosted.org URL."""
    sha256: Sha256 | None = None
    license: License | None = None
    """As the package declares it."""
    agpl: bool = False
    """The declared license is the GNU Affero GPL, or names it (CP-9)."""
    action: Literal["allow", "alert", "escalate", "deny"]
    rule: RuleName | None = None
    """The policy rule that decides it, such as ``packages.conda``."""


class PackageChange(Contract):
    """How a package differs between the revisions before and after an operation."""

    ecosystem: Literal["conda", "pypi"]
    name: PackageName
    before: Version | None = None
    """None: newly installed."""
    after: Version | None = None
    """None: removed."""
    build_before: Build | None = None
    build_after: Build | None = None


class CatalogRef(Contract):
    """The capability catalog an operation used (CP-3): its version and the file's SHA-256."""

    version: Annotated[int, Field(ge=1)]
    sha256: Sha256


class ProvisionOperation(Contract):
    """A provisioning operation and everything it recorded (CP-16, CP-17, CP-25).

    The tool result states ``outcome``, the revisions before and after, the package
    changes, the probes and, short of completion, the stage, the error and what to do
    next. It claims nothing these records do not show.
    """

    provision_id: Identifier
    request: ProvisionRequest
    state: ProvisionState
    outcome: ProvisionOutcome | None = None
    reason: Reason | None = None
    stage: Stage | None = None
    """Where it stopped, short of completing."""
    error: ErrorInfo | None = None
    next_step: Text | None = None
    """What the agent, or the user, can do next."""
    notes: tuple[Text, ...] = Field(default=(), max_length=32)
    """What else the records show, such as a package the install linked that the decision
    did not see."""
    catalog: CatalogRef | None = None
    planned_revision: Annotated[int, Field(ge=0)] | None = None
    """The environment's revision when the operation was requested: the revision it was
    planned against. Operations wait their turn, so it may have changed before it ran."""
    revision_before: Annotated[int, Field(ge=0)] | None = None
    """The revision when its install, or its check, started."""
    revision_after: Annotated[int, Field(ge=0)] | None = None
    policy: PolicyRevision | None = None
    """The policy revision its decisions were made under."""
    files: tuple[ResolvedFile, ...] = Field(default=(), max_length=MAX_FILES)
    permission_request: Annotated[int, Field(ge=1)] | None = None
    """The permission request it made, by its sequence number in the event store: the
    operator's ``approve --request`` names it."""
    changes: tuple[PackageChange, ...] = Field(default=(), max_length=2 * MAX_FILES)
    probes: tuple[ProbeResult, ...] = Field(default=(), max_length=64)
    executions: tuple[Identifier, ...] = Field(default=(), max_length=128)
    """The executions it ran, in order: probes, dry runs, installs. Each can be read, and
    cancelled, like any other."""
    queue_position: Annotated[int, Field(ge=0)] | None = None
    """While queued: how many operations on the environment come before it."""
    submitted_at: datetime
    started_at: datetime | None = None
    ended_at: datetime | None = None
    seq: Annotated[int, Field(ge=0)] = 0
    """Counts the changes to this record; ``after`` asks for a later one."""


class ProvisioningQueue(Contract):
    """An environment's operations that have not finished, oldest first (CP-11).

    The first is the one whose turn it is, which may be waiting for permission.
    """

    environment_id: Identifier
    status: Literal["ready", "changing", "needs_recovery"]
    """``changing`` while an operation installs; ``needs_recovery`` after an operation
    that failed left the packages changed (CP-13)."""
    revision: Annotated[int, Field(ge=0)] | None = None
    operations: tuple[ProvisionOperation, ...] = ()
