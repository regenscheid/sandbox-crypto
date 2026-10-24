# SPDX-License-Identifier: MIT
"""The execution watchdog's deadlines: a call's limit counts from its start (CP-12)."""

from __future__ import annotations

import asyncio

from sandbox_science.contracts import Execution, ExecutionState
from sandbox_science.worker.dispatcher import watch_deadline
from sandbox_science.worker.live import Live

from .harness import CONTEXT

GRACE = 30.0


def live(state: ExecutionState, *, running_at: float | None = None) -> Live:
    execution = Execution.model_validate(
        {
            "execution_id": "exe-1",
            "attempt_id": "att-1",
            "operation_id": "op-1",
            "kind": "shell",
            "context": CONTEXT,
            "state": state,
            "submitted_at": "2026-10-24T00:00:00Z",
        }
    )
    record = Live(execution, "env1", asyncio.get_running_loop().create_future())
    record.time_limit = 120.0
    record.running_at = running_at
    return record


async def test_a_running_execution_has_its_limit_from_its_start() -> None:
    record = live(ExecutionState.RUNNING, running_at=1000.0)
    # Started at 1000, however long it waited behind an install: the limit counts from then.
    assert watch_deadline(record, 120.0, now=1100.0, grace=GRACE, beside_running=False) == 1150.0
    assert watch_deadline(record, 120.0, now=1100.0, grace=GRACE, beside_running=True) == 1150.0
    assert watch_deadline(record, 120.0, now=1150.0, grace=GRACE, beside_running=False) is None


async def test_a_queued_execution_waits_on_only_beside_a_running_one() -> None:
    record = live(ExecutionState.QUEUED)
    assert watch_deadline(record, 120.0, now=2000.0, grace=GRACE, beside_running=True) == 2030.0
    # Nothing else runs, so nothing can hold it up: the executor overran.
    assert watch_deadline(record, 120.0, now=2000.0, grace=GRACE, beside_running=False) is None


async def test_a_reattached_execution_keeps_its_remaining_time() -> None:
    record = live(ExecutionState.RUNNING)
    record.time_limit = None
    assert watch_deadline(record, 50.0, now=10.0, grace=GRACE, beside_running=True) is None
