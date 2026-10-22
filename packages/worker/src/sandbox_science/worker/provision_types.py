# SPDX-License-Identifier: MIT
"""Internal provisioning values and typed coordinator ports."""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Final, Protocol

from sandbox_science.contracts import (
    ErrorInfo,
    ErrorKind,
    Execution,
    ExecutionState,
    ProbeResult,
    ProvisionOperation,
    ProvisionOutcome,
    ProvisionRequest,
    ProvisionState,
    ResolvedFile,
)
from sandbox_science.contracts.provisioning import (
    MAX_NOTES,
    MAX_TEXT,
    Reason,
    Stage,
)
from sandbox_science.contracts.worker import MAX_TIMEOUT_SECONDS
from sandbox_science.policy import HardFloor, LoadedPolicy, PoolCeiling
from sandbox_science.policy.ceiling import effective_resources
from sandbox_science.policy.reasons import APPROVAL_REQUIRED

from .catalog import ProbeSpec
from .resolution import Decided

_log = logging.getLogger(__name__)

DRY_RUN_TIMEOUT: Final = 900.0
"""Seconds a dry run may take."""
INSTALL_TIMEOUT: Final = 3600.0
"""Seconds an install may take when the policy's resources name no wall time."""
DRY_RUN_OUTPUT: Final = 16 * 1024 * 1024
"""Output a dry run may print: its JSON names every package to install."""
STEP_OUTPUT: Final = 1024 * 1024
PERMISSION_POLL: Final = 2.0
"""Seconds between looks at the event store while an operation waits for permission."""
LOOK_TIMEOUT: Final = 120.0
"""Seconds a look at the packages may take, after an install step that reported no revision."""
GRANT_MARGIN: Final = timedelta(minutes=30)
"""How long a permission request grants beyond its wait and its install's time limit: its
own deadline (``valid_until``), which holds even if the worker fails to withdraw it."""
MAX_WAIT: Final = 60.0
"""The longest a client may ask ``wait`` to wait for a change."""
ERROR_TEXT: Final = 1500
"""Characters of a step's output an error quotes."""
PROBE_ID: Final = re.compile(r"[^a-z0-9:._-]")
STAGES: Final[dict[ProvisionState, Stage]] = {
    ProvisionState.QUEUED: "queue",
    ProvisionState.CHECKING: "check",
    ProvisionState.RESOLVING: "resolve",
    ProvisionState.AWAITING_PERMISSION: "permission",
    ProvisionState.INSTALLING: "link",
    ProvisionState.PROBING: "probe",
    ProvisionState.FINISHED: "probe",
}


@dataclass(frozen=True, slots=True)
class Governing:
    """What decides an environment's packages: its policy revision, the pool's ceiling and
    floor, and the sandbox the policy middleware knows it by."""

    policy: LoadedPolicy
    ceiling: PoolCeiling
    floor: HardFloor
    sandbox_id: str | None


class PolicySource(Protocol):
    """Finds what governs an environment: the worker's environments (environments.py)."""

    def governing(self, environment_id: str) -> Governing | None: ...


class Restorer(Protocol):
    """Puts an environment back to a revision after a failed install (CP-13). The next
    slice provides one, from a volume snapshot or by rebuilding from the revision's
    lockfile; without one, such an operation ends ``needs_recovery``."""

    async def restore(self, environment_id: str, revision: int) -> bool: ...


@dataclass(frozen=True, slots=True)
class Completion:
    """An operation's classified result, before the coordinator records its end."""

    outcome: ProvisionOutcome
    reason: Reason | None = None
    stage: Stage | None = None
    error: ErrorInfo | None = None
    next_step: str | None = None
    updates: dict[str, object] = field(default_factory=lambda: dict[str, object]())


class Stop(Exception):  # noqa: N818 - it ends an operation, as a return would
    """How an operation ends: its outcome and the fields it records."""

    def __init__(
        self,
        outcome: ProvisionOutcome,
        reason: Reason | None = None,
        stage: Stage | None = None,
        error: ErrorInfo | None = None,
        next_step: str | None = None,
        **updates: object,
    ) -> None:
        super().__init__(outcome.value)
        self.outcome = outcome
        self.reason: Reason | None = reason
        self.stage: Stage | None = stage
        self.error = error
        self.next_step = next_step
        self.updates = updates

    @property
    def completion(self) -> Completion:
        """The same result when a stage ends early instead of returning normally."""
        return Completion(
            self.outcome, self.reason, self.stage, self.error, self.next_step, self.updates
        )


@dataclass
class Operation:
    record: ProvisionOperation
    changed: asyncio.Future[None]
    turn: asyncio.Event = field(default_factory=asyncio.Event)
    cancelled: asyncio.Event = field(default_factory=asyncio.Event)
    step: str | None = None
    """The execution running for it now."""
    task: asyncio.Task[None] | None = None
    files: Decided | None = None


def asked(request: ProvisionRequest) -> str:
    """What an operation asks for, as its permission request shows the operator."""
    if request.capability is not None:
        return f"the capability {request.capability}"
    if request.conda:
        return "Conda: " + ", ".join(request.conda)
    return "pip: " + ", ".join(request.pip)


def clip(text: str, limit: int = MAX_TEXT) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def append_notes(notes: Sequence[str], *more: str) -> tuple[str, ...]:
    """Notes as a record holds them: each at most MAX_TEXT characters, the newest 32."""
    return tuple(clip(note) for note in (*notes, *more) if note)[-MAX_NOTES:]


def cancelled(stage: Stage) -> Stop:
    return Stop(
        ProvisionOutcome.FAILED,
        "cancelled",
        stage,
        ErrorInfo(kind=ErrorKind.CANCELLED, message="cancelled on request"),
        next_step="Nothing was installed.",
    )


def started_revision(execution: Execution) -> int | None:
    location = execution.location
    text = location.environment_revision if location is not None else None
    return int(text) if text is not None and text.isdigit() else None


def ended_revision(execution: Execution) -> int | None:
    result = execution.result
    text = result.environment_revision if result is not None else None
    return int(text) if text is not None and text.isdigit() else None


def tail(text: str, limit: int) -> str:
    return text if len(text) <= limit else "…" + text[-(limit - 1) :]


def step_error(execution: Execution, what: str) -> ErrorInfo:
    """Why a step failed, with the end of its output, which is where package managers say."""
    result = execution.result
    stderr = result.stderr.text if result is not None and result.stderr is not None else ""
    stdout = result.stdout.text if result is not None and result.stdout is not None else ""
    error = result.error if result is not None else None
    if execution.state is ExecutionState.TIMED_OUT:
        kind, summary = ErrorKind.TIMED_OUT, f"{what} ran out of time"
    elif execution.state is ExecutionState.CANCELLED:
        kind, summary = ErrorKind.CANCELLED, f"{what} was cancelled"
    elif error is not None and result is not None and result.exit_code is None:
        kind, summary = error.kind, f"{what} failed: {error.message[:300]}"
    else:
        code = result.exit_code if result is not None else None
        kind, summary = ErrorKind.EXCEPTION, f"{what} exited with {code}"
    if APPROVAL_REQUIRED in stderr or APPROVAL_REQUIRED in stdout:
        kind = ErrorKind.POLICY_DENIED
    detail = tail((stderr.strip() + "\n" + stdout.strip()).strip(), ERROR_TEXT)
    return ErrorInfo(kind=kind, message=f"{summary}: {detail}" if detail else summary)


def described(file: ResolvedFile) -> dict[str, object]:
    """A resolved file as a permission request lists it, for the operator."""
    return {
        key: value
        for key, value in file.model_dump(
            include={"ecosystem", "name", "version", "build", "channel", "subdir", "file"}
            | {"sha256", "license", "action", "rule"}
        ).items()
        if value is not None
    }


def install_timeout(governing: Governing) -> float:
    """The install's time limit: the policy's wall time under the pool's cap (CP-14)."""
    resources, _ = effective_resources(governing.policy.policy, governing.ceiling)
    if resources.wall_time is None:
        return INSTALL_TIMEOUT
    return min(resources.wall_time.total_seconds(), float(MAX_TIMEOUT_SECONDS))


class RecordUpdate(Protocol):
    """The coordinator persists and publishes every record change."""

    def __call__(self, op: Operation, **changes: object) -> None: ...


class Observe(Protocol):
    """Append an observation while retaining its actual writer ownership."""

    async def __call__(self, record: ProvisionOperation, kind: str) -> None: ...


@dataclass(frozen=True, slots=True)
class PermissionGrant:
    """The exact-file permission request approved for this operation."""

    request: int


class Probe(Protocol):
    """Execute a capability probe in the operation's original execution context."""

    async def __call__(
        self, op: Operation, capability: str, probe: ProbeSpec, label: str, stage: Stage
    ) -> ProbeResult: ...
