# SPDX-License-Identifier: MIT
"""The kernel check before each turn: kernels that predate the environment's packages."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any, cast

from sandbox_science.contracts import (
    ErrorKind,
    ExecutionContext,
    KernelInfo,
    KernelObservation,
)
from sandbox_science.orchestrator.kernel_check import KernelCheck
from sandbox_science.orchestrator.loop import run_task
from sandbox_science.orchestrator.model import ModelTurn
from sandbox_science.orchestrator.notices import KERNELS
from sandbox_science.orchestrator.tools import Toolbox
from sandbox_science.orchestrator.worker_client import WorkerAPIError, WorkerClient

from .scripted import ScriptedModel, ToolCall

CONTEXT = ExecutionContext(project_id="p", session_id="a", environment_id="env")


class Worker:
    """The worker's kernel observation: Python started at revision 1, Sage unknown, and a
    delegated run's own Python kernel started at revision 1 too."""

    def __init__(self) -> None:
        self.current: int | None = 1
        self.observed = 0
        self.restarted: list[tuple[str, str | None]] = []

    async def restart_kernel(
        self, context: ExecutionContext, language: str = "python", run: str | None = None
    ) -> KernelInfo:
        self.restarted.append((language, run))
        return KernelInfo(kernel_id="kernel-restarted", language="python", generation=3, run=run)

    async def observe_kernels(self, context: ExecutionContext) -> KernelObservation:
        self.observed += 1
        if self.current is None:
            raise WorkerAPIError(ErrorKind.UNAVAILABLE, "the executor is not connected")
        python = KernelInfo(
            kernel_id="kernel-python",
            language="python",
            generation=2,
            start_revision=1,
            current_revision=self.current,
            revision_status="current" if self.current == 1 else "predates",
        )
        sage = KernelInfo(
            kernel_id="kernel-sage", language="sage", generation=1, current_revision=self.current
        )
        delegated = python.model_copy(update={"kernel_id": "kernel-child", "run": "child"})
        return KernelObservation(
            environment_id=context.environment_id,
            session_id=context.session_id,
            current_revision=self.current,
            observed_at=datetime.now(UTC),
            kernels=(python, sage, delegated),
        )


def turn() -> ModelTurn:
    return ModelTurn(None, (ToolCall("c", "no_such_tool", "{}"),))


def kernel_notices(messages: Sequence[Mapping[str, object]]) -> list[list[dict[str, object]]]:
    return [
        json.loads(str(m["content"]).removeprefix(KERNELS.text))
        for m in messages
        if m["role"] == "user" and str(m["content"]).startswith(KERNELS.text)
    ]


async def test_a_package_change_from_another_session_is_told_once_per_revision() -> None:
    worker = Worker()
    revisions = iter((2, 2, 3))

    class Model(ScriptedModel):
        async def complete(self, messages, tools):
            # Another session installs packages while this run works.
            worker.current = next(revisions, worker.current)
            return await super().complete(messages, tools)

    box = Toolbox(cast(WorkerClient, worker), CONTEXT, check_kernels=True)
    model = Model([turn(), turn(), turn(), ModelTurn("done")])
    transcript = await run_task(model, box, "go", run_id="r")
    told = kernel_notices(transcript.context.messages)
    # Revision 2 is told once, though two turns saw it; revision 3 once more. The Sage
    # kernel's start is unknown, so it is never told about.
    assert [[(n["kernel_id"], n["current_revision"]) for n in notice] for notice in told] == [
        [("kernel-python", 2)],
        [("kernel-python", 3)],
    ]
    assert transcript.delivered_ids("kernels") == {str(notice[0]["notice_id"]) for notice in told}
    assert worker.observed == 4


async def test_an_unavailable_observation_tells_nothing_and_the_run_goes_on() -> None:
    worker = Worker()
    worker.current = None
    box = Toolbox(cast(WorkerClient, worker), CONTEXT, check_kernels=True)
    transcript = await run_task(ScriptedModel([ModelTurn("done")]), box, "go", run_id="r")
    assert (transcript.final_text, kernel_notices(transcript.context.messages)) == ("done", [])


async def test_each_run_is_told_again_while_a_kernel_predates_the_packages() -> None:
    worker = Worker()
    worker.current = 2
    box = Toolbox(cast(WorkerClient, worker), CONTEXT, check_kernels=True)
    first = await run_task(ScriptedModel([ModelTurn("one")]), box, "go", run_id="r1")
    second = await run_task(ScriptedModel([ModelTurn("two")]), box, "go", run_id="r2")
    assert first.delivered_ids("kernels") == second.delivered_ids("kernels")
    assert len(kernel_notices(second.context.messages)) == 1


async def test_runs_without_the_check_observe_no_kernels() -> None:
    worker = Worker()
    box = Toolbox(cast(WorkerClient, worker), CONTEXT)
    await run_task(ScriptedModel([ModelTurn("done")]), box, "go", run_id="r")
    assert worker.observed == 0


async def test_a_run_is_told_only_about_the_kernels_its_cells_use() -> None:
    """PD-37: a main run uses the session's kernels, a delegated run its own."""
    told: dict[str | None, list[object]] = {}
    for run in (None, "child"):
        worker = Worker()
        worker.current = 2
        box = Toolbox(cast(WorkerClient, worker), CONTEXT, check_kernels=True, kernel_run=run)
        transcript = await run_task(ScriptedModel([ModelTurn("done")]), box, "go", run_id="r")
        told[run] = [
            n["kernel_id"] for notice in kernel_notices(transcript.context.messages) for n in notice
        ]
    assert told == {None: ["kernel-python"], "child": ["kernel-child"]}


async def test_a_delegated_run_restarts_its_own_kernel() -> None:
    worker = Worker()
    box = Toolbox(cast(WorkerClient, worker), CONTEXT, kernel_run="child")
    model = ScriptedModel(
        [ModelTurn(None, (ToolCall("c", "restart_kernel", "{}"),)), ModelTurn("done")]
    )
    await run_task(model, box, "go", run_id="r")
    assert worker.restarted == [("python", "child")]


async def test_a_main_run_closes_left_kernels_once_the_executor_is_connected() -> None:
    """PD-40: the first observation fails (nothing connected yet); after the run's first
    call, the next one succeeds and the sweep closes the kernels earlier runs left."""

    class Unconnected(Worker):
        def __init__(self) -> None:
            super().__init__()
            self.closed: list[str | None] = []

        async def observe_kernels(self, context: ExecutionContext) -> KernelObservation:
            self.observed += 1
            if self.observed == 1:
                raise WorkerAPIError(ErrorKind.UNAVAILABLE, "the executor is not connected")
            observation = await super().observe_kernels(context)
            self.observed -= 1
            return observation

        async def close_session(self, context: ExecutionContext, run: str | None = None) -> None:
            self.closed.append(run)

    async def live() -> set[str]:
        return {"live-child"}

    worker = Unconnected()
    check = KernelCheck(cast(Any, worker), CONTEXT, sweep=True, keep=live)
    box = Toolbox(cast(WorkerClient, worker), CONTEXT)
    model = ScriptedModel([turn(), turn(), ModelTurn("done")])
    await run_task(model, box, "go", run_id="r", hooks=(check,))
    assert worker.closed == ["child"]  # once, and never a run the tree keeps
