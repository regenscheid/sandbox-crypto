# SPDX-License-Identifier: MIT
"""The context every execution request carries."""

from __future__ import annotations

from typing import Annotated

from pydantic import Field, StringConstraints

from .model import Contract, Identifier, Revision

OwnershipName = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,150}$")]
"""A timestamped work/attempt name, including the longest 128-character run ID."""


class ExecutionAuthority(Contract):
    """Host-control ownership, never supplied by an agent's tool arguments."""

    work: OwnershipName
    run: OwnershipName
    epoch: Revision


class ExecutionContext(Contract):
    """Who asks for an execution, and for which project, session and environment.

    ``environment_id`` names the logical environment (the project's default
    environment or a dedicated session environment). The worker reports the
    concrete sandbox and the environment revision it used in ``Location``.
    """

    project_id: Identifier
    session_id: Identifier
    environment_id: Identifier
    policy_revision: str | None = None
    """The project policy revision in force, such as ``sha256:…``."""
    actor: Identifier = "agent"
    """Who requested the execution: ``agent``, ``user``, ``reviewer`` or a delegated role."""
    authority: ExecutionAuthority | None = Field(default=None, exclude_if=lambda v: v is None)
