# SPDX-License-Identifier: MIT
"""Worker API types for research environments: sandboxes the worker creates under a policy (D73).

The orchestrator asks for an environment by naming a base profile (D46) and a project
policy revision that an operator has already added to the pool's policy store. It cannot
supply a policy itself.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated

from pydantic import StringConstraints

from .model import Contract, Identifier

PolicyRevision = Annotated[str, StringConstraints(pattern=r"^sha256:[0-9a-f]{64}$")]
"""A project policy revision: ``sha256:`` and the hex SHA-256 of the policy file's bytes."""


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
    them, which the sandbox can resolve only once admitted."""
    created_at: datetime
    updated_at: datetime
