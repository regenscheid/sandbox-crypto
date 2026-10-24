# SPDX-License-Identifier: MIT
"""Transient failures recover without a person and without repeating effects (D100).

The run service runs a real harness against the in-process worker, whose shell calls
start real processes. A run makes one effectful shell call, then meets a transient
failure: a gateway 524, a stream that ends without its final answer, or a worker lost
mid-operation. The service's automatic recovery queues the tree Resume, which reconciles
the recorded operation by its identity and finishes the research; the shell effect
happens once. An explicit Stop stays stopped.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from typing import Any

import pytest

from sandbox_science.contracts import ErrorKind, Event, Execution
from sandbox_science.contracts.worker import ExecutionRequest
from sandbox_science.orchestrator.continuity.records import WorkRecords
from sandbox_science.orchestrator.documents import DocumentStore, store_path
from sandbox_science.orchestrator.harness import Harness as Core
from sandbox_science.orchestrator.harness import Role, Settings, Stores
from sandbox_science.orchestrator.jobstate import JobState
from sandbox_science.orchestrator.model import (
    IncompleteStreamError,
    Message,
    ModelError,
    ModelTurn,
    ToolCall,
    ToolSpec,
)
from sandbox_science.orchestrator.projects import ProjectStore
from sandbox_science.orchestrator.runstate import RunState
from sandbox_science.orchestrator.service import RunService
from sandbox_science.orchestrator.worker_client import WorkerAPIError, WorkerClient
from sandbox_science.testing import eventually

from .loop_harness import PROJECT, SESSION, TOKEN, Harness
from .loop_harness import gateway as gateway
from .loop_harness import harness as harness
from .loop_harness import worker as worker

pytestmark = pytest.mark.slow

EFFECT = "effects.txt"
SHELL = ToolCall("call-effect", "shell", json.dumps({"script": f"echo once >> {EFFECT}"}))


class FlakyModel:
    """Scripted turns; an exception in the script is raised instead of answering."""

    def __init__(self, script: Sequence[ModelTurn | BaseException | asyncio.Event]) -> None:
        self.script = list(script)
        self.calls = 0

    async def complete(self, messages: Sequence[Message], tools: Sequence[ToolSpec]) -> ModelTurn:
        self.calls += 1
        step = self.script.pop(0) if self.script else ModelTurn("(the script has ended)")
        if isinstance(step, asyncio.Event):  # a call the lost bridge never answers
            await step.wait()
            raise AssertionError("the hanging call was answered")
        if isinstance(step, BaseException):
            raise step
        return step


class LosingWorker(WorkerClient):
    """The real worker; the first shell call's answer is lost after the worker ran it."""

    def __init__(self, url: str, token: str, *, lose: bool) -> None:
        super().__init__(url, token)
        self.lose = lose
        self.runs = 0

    async def run(
        self,
        request: ExecutionRequest,
        on_event: Callable[[Event], Awaitable[None]] | None = None,
        *,
        on_submitted: Callable[[Execution, bool], None] | None = None,
    ) -> Execution:
        self.runs += 1
        execution = await super().run(request, on_event, on_submitted=on_submitted)
        if self.lose:
            self.lose = False
            raise WorkerAPIError(
                ErrorKind.UNAVAILABLE, "synthetic: worker lost after submit", uncertain=True
            )
        return execution


@contextlib.asynccontextmanager
async def service_for(
    harness: Harness, model: FlakyModel, *, lose: bool = False
) -> AsyncIterator[tuple[RunService, LosingWorker]]:
    projects = ProjectStore(harness.state)
    documents = DocumentStore(store_path(harness.state), producer="harness")
    try:
        client = LosingWorker(harness.worker.url, TOKEN, lose=lose)
        async with client:
            stores = Stores(
                projects,
                documents,
                JobState(harness.state, control=documents.control),
                RunState(projects.control),
            )
            core = Core(
                client,
                Role(model, "main"),
                stores=stores,
                settings=Settings(min_isolation_tier=None),
            )
            yield RunService(core, poll=0.02), client
    finally:
        documents.close()
        projects.close()


def effects(harness: Harness) -> list[str]:
    session = ProjectStore(harness.state).get(PROJECT).sessions[SESSION]
    found = list(harness.tmp.glob(f"worker/**/{session.environment_id}/**/{EFFECT}"))
    found = found or list(harness.tmp.glob(f"worker/**/{EFFECT}"))
    assert len(found) == 1, found
    return found[0].read_text().splitlines()


async def recover(service: RunService, *, started: bool = True) -> str:
    """Let the run fail, then let the service recover it; the root's final state.
    ``started`` false: an earlier service already ran it."""
    works = WorkRecords(service.stores.projects.control)
    if started:
        await service.tick()

    async def idle() -> bool:
        await service.maintenance_tick()  # forgets ended runs, as each look does
        return not service.active

    await eventually(idle, timeout=60)
    (root,) = _roots(service)
    assert root.state in ("paused", "blocked"), root
    # No person acts: the service's own automatic recovery queues the tree Resume, as
    # its periodic look does.
    await service.auto_recovery_tick()
    assert works.queued(), works.get(PROJECT, SESSION, root.work)

    async def resumed() -> bool:
        await service.tick()  # starts the Resume, then offers its ready branches
        return not service.active and not works.queued()

    await eventually(resumed, timeout=60)
    return works.get(PROJECT, SESSION, root.work).state


def _roots(service: RunService) -> list[Any]:
    control = service.stores.projects.control
    with control.read() as tx:
        rows = tx.sql.execute(
            "SELECT work FROM research_work WHERE project=? AND session=? AND parent IS NULL",
            (PROJECT, SESSION),
        ).fetchall()
    works = WorkRecords(control)
    return [works.get(PROJECT, SESSION, row[0]) for row in rows]


@pytest.mark.parametrize(
    "failure",
    [
        ModelError("the model gateway returned 524: <html>timeout</html>", status=524),
        IncompleteStreamError(
            "the model gateway's Responses stream ended without a final response"
        ),
    ],
    ids=["gateway-524", "stream-without-final"],
)
async def test_model_failure_recovers_without_a_person_or_a_repeated_effect(
    harness: Harness, failure: ModelError
) -> None:
    RunState(ProjectStore(harness.state).control).post(
        PROJECT, SESSION, "m-1", "Investigate.", author="user"
    )
    model = FlakyModel([ModelTurn(None, (SHELL,)), failure, ModelTurn("Recovered answer.")])
    async with service_for(harness, model) as (service, client):
        assert await recover(service) == "completed"
        assert client.runs == 1  # the shell call ran once and was not replayed
    assert effects(harness) == ["once"]
    assert model.calls == 3


async def test_worker_loss_reconciles_the_operation_by_identity(harness: Harness) -> None:
    RunState(ProjectStore(harness.state).control).post(
        PROJECT, SESSION, "m-1", "Investigate.", author="user"
    )
    model = FlakyModel([ModelTurn(None, (SHELL,)), ModelTurn("Recovered answer.")])
    async with service_for(harness, model, lose=True) as (service, client):
        assert await recover(service) == "completed"
        assert client.runs == 1  # recovered by lookup, never submitted again
    assert effects(harness) == ["once"]
    assert model.calls == 2


async def test_bridge_loss_recovers_in_the_next_service(harness: Harness) -> None:
    """The runtime's connection ends mid-call; the next service retries the tree."""
    RunState(ProjectStore(harness.state).control).post(
        PROJECT, SESSION, "m-1", "Investigate.", author="user"
    )
    model = FlakyModel([ModelTurn(None, (SHELL,)), asyncio.Event(), ModelTurn("Recovered.")])
    async with service_for(harness, model) as (service, client):
        await service.tick()
        await eventually(lambda: model.calls == 2, timeout=60)
        assert client.runs == 1
        service.lost = "bridge_loss"  # as the control server sets it, not draining
        await service.close()
    async with service_for(harness, model) as (service, client):
        assert await recover(service, started=False) == "completed"
        assert client.runs == 0
    assert effects(harness) == ["once"]
    assert model.calls == 3


async def test_explicit_stop_after_a_transient_failure_stays_stopped(harness: Harness) -> None:
    RunState(ProjectStore(harness.state).control).post(
        PROJECT, SESSION, "m-1", "Investigate.", author="user"
    )
    failure = ModelError("the model gateway returned 524: <html>timeout</html>", status=524)
    model = FlakyModel([ModelTurn(None, (SHELL,)), failure, ModelTurn("Must not run.")])
    async with service_for(harness, model) as (service, _):
        await service.tick()

        async def idle() -> bool:
            await service.maintenance_tick()
            return not service.active

        await eventually(idle, timeout=60)
        (root,) = _roots(service)
        works = WorkRecords(service.stores.projects.control)
        works.stop(PROJECT, SESSION, root.work)  # the user's Stop of the paused tree
        await service.auto_recovery_tick()
        await service.tick()
        assert works.queued() == []
        assert not service.active
        assert works.get(PROJECT, SESSION, root.work).state == "stopped"
    assert model.calls == 2
    assert effects(harness) == ["once"]
