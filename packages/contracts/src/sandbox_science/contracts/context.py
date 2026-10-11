# SPDX-License-Identifier: MIT
"""The context every execution request carries."""

from __future__ import annotations

from .model import Contract, Identifier


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
