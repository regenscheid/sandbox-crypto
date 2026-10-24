# SPDX-License-Identifier: MIT
"""Worker API types for research environments: sandboxes the worker creates under a policy (D73).

The orchestrator asks for an environment by naming a base profile (D46) and a project
policy revision that an operator has already added to the pool's policy store. It cannot
supply a policy itself.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Final, Literal

from pydantic import Field, StringConstraints

from .context import ExecutionContext
from .model import Contract, Identifier

PolicyRevision = Annotated[str, StringConstraints(pattern=r"^sha256:[0-9a-f]{64}$")]
"""A project policy revision: ``sha256:`` and the hex SHA-256 of the policy file's bytes."""

MAX_ADMISSION_HOSTS: Final = 32
"""The most hosts one admission request may name."""


class EnvironmentState(StrEnum):
    CREATING = "creating"
    READY = "ready"
    STOPPED = "stopped"
    """Suspended while idle: the sandbox keeps its files and installed tools, and the worker
    starts it again on the environment's next use. Kernels did not survive."""
    DELETED = "deleted"


class EnvironmentRequest(Contract):
    """Create the sandbox for a logical environment, or return the one that exists."""

    environment_id: Identifier
    project_id: Identifier
    profile: Identifier
    """A base profile the pool offers, such as ``minimal`` or ``crypto-base``."""
    policy: PolicyRevision


class PolicyRevisionRequest(Contract):
    """Move an environment's sandbox to another policy revision."""

    policy: PolicyRevision


class EnvironmentInfo(Contract):
    environment_id: Identifier
    project_id: Identifier
    profile: Identifier
    image: str
    sandbox: str
    """The OpenShell sandbox's name."""
    sandbox_id: str | None = None
    """OpenShell's ID for the sandbox, which the policy middleware's bindings use."""
    policy: PolicyRevision
    providers: tuple[str, ...] = ()
    """The OpenShell providers attached to the sandbox: exactly those the policy lists."""
    state: EnvironmentState
    findings: tuple[str, ...] = ()
    """What the policy compiler reported, such as rules OpenShell cannot express exactly."""
    admitted_hosts: tuple[str, ...] = ()
    """Hosts the worker admitted on demand (D69): names the policy reaches without naming
    them, which the sandbox can resolve only once admitted, and ``host:port`` for another
    port of a host that a rule for any port reaches (D95)."""
    created_at: datetime
    updated_at: datetime
    kind: Literal["session", "job"] = "session"
    """``job`` for a job sandbox (D93): made by the worker for one job, from a session
    environment's image and policy, and named by no agent tool (JS-1)."""
    job_id: Identifier | None = None
    """For a job sandbox: the job's execution ID."""


class HostPort(Contract):
    """A host some code is about to contact, and the port, 443 unless given."""

    host: Annotated[str, StringConstraints(min_length=1, max_length=253)]
    port: Annotated[int, Field(ge=1, le=65535)] = 443


class AdmissionRequest(Contract):
    """Ask the worker to admit hosts before code contacts them (D69).

    The worker decides each host as it decides one a sandbox could not reach, and
    names the admissible ones in one new compilation of the environment's policy.
    """

    hosts: tuple[HostPort, ...] = Field(min_length=1, max_length=MAX_ADMISSION_HOSTS)
    context: ExecutionContext | None = Field(default=None, exclude_if=lambda v: v is None)


class AdmissionDecision(Contract):
    host: str
    """The host as the worker decided it: normalized, lower case."""
    port: int
    admitted: bool
    """Whether code in the environment can now reach the host on this port, if the policy
    allows the request it makes."""
    reason: str
    """``admitted``, or why the host was refused."""


class AdmissionResult(Contract):
    decisions: tuple[AdmissionDecision, ...]
    environment: EnvironmentInfo


class EnvironmentList(Contract):
    """The environments of the projects a client may act for, deleted ones left out."""

    environments: tuple[EnvironmentInfo, ...]


class SuspendResult(Contract):
    """What asking to suspend an environment did (W3)."""

    environment: EnvironmentInfo
    suspended: bool
    """Whether OpenShell says its sandbox is stopped now: by this request, or already. False
    while it is in use (running jobs, a publication, a request in flight or kernels
    computing keep it running), while it is being created, or when its stop failed."""
