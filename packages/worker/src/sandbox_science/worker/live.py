# SPDX-License-Identifier: MIT
"""An execution the worker is running, and the saved request it reattaches from."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Final

from pydantic import TypeAdapter

from sandbox_science.contracts import (
    ErrorKind,
    Execution,
    ExecutionRequest,
    ExecutionState,
    Result,
)
from sandbox_science.contracts.output_observations import Gap, OutputReport
from sandbox_science.contracts.worker import DEFAULT_OUTPUT_LIMIT

from .channel import ExecutorChannel
from .errors import WorkerError

REQUEST: Final = TypeAdapter[ExecutionRequest](ExecutionRequest)
"""Reads a request saved for recovery."""


def time_limit(request: ExecutionRequest) -> float:
    """The request's time limit; a job's comes from the policy at submission when unset."""
    if request.timeout_seconds is None:
        raise WorkerError(ErrorKind.INTERNAL, "the job's time limit was never resolved")
    return request.timeout_seconds


@dataclass
class Live:
    """The manager's record of an execution that has not ended, or ended only lately."""

    execution: Execution
    environment_id: str
    changed: asyncio.Future[None]
    final_source_seq: int | None = None
    observation: OutputReport | None = None
    observation_gap: Gap | None = "missing"
    limit: int = DEFAULT_OUTPUT_LIMIT
    cancel_requested: bool = False
    dispatched: bool = False
    reattached: bool = False
    """An unseen kernel birth replayed after detach cannot establish a new start pin."""
    watchdog: asyncio.Task[None] | None = None
    cancel_watchdog: asyncio.Task[None] | None = None
    stored: int = 0
    """Bytes of stream text and rich output stored as events."""
    events: int = 0
    overflowed: bool = False
    channel: ExecutorChannel | None = None
    """The executor the execution was sent to: only it may cancel or lose it."""
    notices: list[str] = field(default_factory=list[str])
    """What syncing the project's shared files told before the execution ran (D79)."""
    dispatched_at: float = 0.0
    """When the request went to the executor, by the monotonic clock."""
    time_limit: float | None = None
    """The request's time limit, which the executor counts from the start (CP-12)."""
    running_at: float | None = None
    """When the executor reported it running, by the monotonic clock."""
    final: tuple[ExecutionState, Result] | None = None
    """The executor's outcome, held while the shared files sync after it (D79)."""
    ended_at: float = 0.0
    """When the executor reported the outcome, by the monotonic clock."""
    revisions: int = 0
    """``revision`` events it carried."""
    lockfiles: int = 0
    """Lockfiles its ``revision`` events brought or had asked for."""
    lockfile_attempts: set[str] = field(default_factory=set[str])
    """The attempts whose lockfiles its ``revision`` events brought."""
