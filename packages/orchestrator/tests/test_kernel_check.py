# SPDX-License-Identifier: MIT
"""The kernel check before each turn: kernels that predate the environment's packages."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import cast

from sandbox_science.contracts import (
    ErrorKind,
    ExecutionContext,
    KernelInfo,
    KernelObservation,
)
from sandbox_science.orchestrator.notices import KERNELS
from sandbox_science.orchestrator.loop import run_task
from sandbox_science.orchestrator.model import ModelTurn, ScriptedModel, ToolCall
from sandbox_science.orchestrator.tools import Toolbox
from sandbox_science.orchestrator.worker_client import WorkerAPIError, WorkerClient

CONTEXT = ExecutionContext(project_id="p", session_id="a", environment_id="env")


class Worker:
    """The worker's kernel observation: Python started at revision 1, Sage unknown."""

    def __init__(self) -> None:
        self.current: int | None = 1
        self.observed = 0

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
        return KernelObservation(
            environment_id=context.environment_id,
            session_id=context.session_id,
            current_revision=self.current,
            observed_at=datetime.now(UTC),
            kernels=(python, sage),
        )


def turn() -> ModelTurn:
    return ModelTurn(None, (ToolCall("c", "no_such_tool", "{}"),))


def kernel_notices(messages: list[dict[str, object]]) -> list[list[dict[str, object]]]:
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
    told = kernel_notices(transcript.messages)
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
    assert (transcript.final_text, kernel_notices(transcript.messages)) == ("done", [])


async def test_each_run_is_told_again_while_a_kernel_predates_the_packages() -> None:
    worker = Worker()
    worker.current = 2
    box = Toolbox(cast(WorkerClient, worker), CONTEXT, check_kernels=True)
    first = await run_task(ScriptedModel([ModelTurn("one")]), box, "go", run_id="r1")
    second = await run_task(ScriptedModel([ModelTurn("two")]), box, "go", run_id="r2")
    assert first.delivered_ids("kernels") == second.delivered_ids("kernels")
    assert len(kernel_notices(second.messages)) == 1


async def test_runs_without_the_check_observe_no_kernels() -> None:
    worker = Worker()
    box = Toolbox(cast(WorkerClient, worker), CONTEXT)
    await run_task(ScriptedModel([ModelTurn("done")]), box, "go", run_id="r")
    assert worker.observed == 0
