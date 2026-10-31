# SPDX-License-Identifier: MIT
"""Sibling continuations in one shared environment start together, and the worker
observes the environment's kernels one at a time, answering the others busy. A
continuation's start asks again after a short wait instead of failing; when its bounded
retries run out, the failure is the worker's capacity, not a lost worker (D100)."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import cast

import pytest

from sandbox_science.contracts import ErrorKind, KernelInfo, KernelObservation
from sandbox_science.contracts.worker import KERNEL_OBSERVATION_BUSY
from sandbox_science.orchestrator.continuity.kernels import KernelJournal
from sandbox_science.orchestrator.continuity.records import WorkRecords
from sandbox_science.orchestrator.continuity.transient import classify
from sandbox_science.orchestrator.projects import ProjectStore
from sandbox_science.orchestrator.worker_client import WorkerAPIError, WorkerClient

from .test_tool_journal import CONTEXT, RUN
from .test_tool_journal import projects as projects

SIBLINGS = 4


def kernel(n: int) -> KernelInfo:
    return KernelInfo(kernel_id=f"kernel-{n}", language="python", generation=1, run=f"task-{n}")


def observation(*kernels: KernelInfo) -> KernelObservation:
    return KernelObservation(
        environment_id=CONTEXT.environment_id,
        session_id="s",
        observed_at=datetime.now(UTC),
        kernels=kernels,
    )


class OneLane:
    """A worker that observes one environment's kernels at a time and answers a
    concurrent observation busy, as the worker's admission lane does; ``busy`` answers
    every observation busy."""

    def __init__(self, *, busy: bool = False) -> None:
        self.busy = busy
        self.observing = False
        self.answered = 0
        self.refused = 0

    async def observe_kernels(self, context: object) -> KernelObservation:
        if self.busy or self.observing:
            self.refused += 1
            raise WorkerAPIError(ErrorKind.UNAVAILABLE, KERNEL_OBSERVATION_BUSY)
        self.observing = True
        try:
            await asyncio.sleep(0.02)
            self.answered += 1
            return observation(*(kernel(n) for n in range(SIBLINGS)))
        finally:
            self.observing = False


async def siblings(projects: ProjectStore) -> list[KernelJournal]:
    """Sibling tasks under one root, each with its kernel in the session's environment."""
    works = WorkRecords(projects.control)
    works.admit("p", "s", RUN)
    journals: list[KernelJournal] = []
    for n in range(SIBLINGS):
        owner = works.admit("p", "s", f"20261016T03000{n + 1}000000Z-task-{n}", parent=RUN)
        journal = KernelJournal(
            projects.control, owner, f"task-{n}", busy_seconds=0.01, busy_max_seconds=0.05
        )
        await journal.record(observation(kernel(n)))
        journals.append(journal)
    return journals


async def test_sibling_continuations_wait_their_turn_for_the_kernel_observation(
    projects: ProjectStore,
) -> None:
    journals = await siblings(projects)
    worker = OneLane()
    await asyncio.gather(
        *(journal.reconcile(cast(WorkerClient, worker), CONTEXT) for journal in journals)
    )
    assert worker.answered == SIBLINGS  # each found its kernel alive, none was lost
    assert worker.refused > 0  # and they did collide on the lane


async def test_a_busy_answer_that_outlasts_the_retries_is_capacity_not_a_lost_worker(
    projects: ProjectStore,
) -> None:
    (journal, *_) = await siblings(projects)
    journal.busy_retries = 3
    worker = OneLane(busy=True)
    with pytest.raises(WorkerAPIError) as raised:
        await journal.reconcile(cast(WorkerClient, worker), CONTEXT)
    assert worker.refused == 4  # the first answer and three more
    assert classify(raised.value) == "worker_busy"


async def test_another_unavailable_answer_is_not_retried(projects: ProjectStore) -> None:
    (journal, *_) = await siblings(projects)
    asked = 0

    class Gone:
        async def observe_kernels(self, context: object) -> KernelObservation:
            nonlocal asked
            asked += 1
            raise WorkerAPIError(ErrorKind.UNAVAILABLE, "cannot reach the worker")

    with pytest.raises(WorkerAPIError) as raised:
        await journal.reconcile(cast(WorkerClient, Gone()), CONTEXT)
    assert asked == 1
    assert classify(raised.value) == "worker_loss"
