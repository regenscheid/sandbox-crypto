# SPDX-License-Identifier: MIT
"""Worker recovery authority and passive operation identity queries."""

from __future__ import annotations

from typing import Self

from pydantic import model_validator

from .context import ExecutionContext
from .model import Contract, Identifier, Revision
from .provisioning import ProvisionOperation
from .worker import Execution


class RecoveryFence(Contract):
    """The monotonically installed root epoch of an exact research session."""

    project_id: Identifier
    session_id: Identifier
    epoch: Revision


class EffectControl(Contract):
    """Current context accompanying cancellation of an already recorded effect."""

    context: ExecutionContext


class OperationLookupRequest(Contract):
    """Find an original operation without replaying its request or waking an executor."""

    context: ExecutionContext
    operation_id: Identifier


class OperationLookup(Contract):
    """An exact session/environment's operation, or absence; collisions are refused."""

    execution: Execution | None = None
    provision: ProvisionOperation | None = None

    @model_validator(mode="after")
    def _one(self) -> Self:
        if self.execution is not None and self.provision is not None:
            raise ValueError("operation lookup has two different operations")
        return self
