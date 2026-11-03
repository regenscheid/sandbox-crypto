# SPDX-License-Identifier: MIT
"""Which of a delegated run's requests name its write area (delegated agents spec, DA-20):
its shell, cells, kernel restarts and jobs in its environment, whose processes the worker's
executor then holds to the area, as its file writes (DA-19). The platform's requests for
it (the PDF helper) and a job in a job sandbox of its own do not; a main run's never do."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, cast

from sandbox_science.contracts import ErrorKind, ExecutionContext, ExecutionRequest
from sandbox_science.orchestrator.tools import Toolbox
from sandbox_science.orchestrator.worker_client import WorkerAPIError, WorkerClient

from .scripted import ToolCall
from .test_jobs import MEDIUM, FakeWorker, tools

CONTEXT = ExecutionContext(project_id="p1", session_id="s1", environment_id="env1")
AREA = "work/dlg-1"


class Recording:
    """Keeps each execution request and restart, then refuses it: what was sent is all
    these tests look at."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, ExecutionContext]] = []

    async def run(self, request: ExecutionRequest, *_: Any, **__: Any) -> Any:
        self.sent.append((request.kind, request.context))
        raise WorkerAPIError(ErrorKind.UNAVAILABLE, "recorded")

    async def restart_kernel(self, context: ExecutionContext, *_: Any, **__: Any) -> Any:
        self.sent.append(("restart", context))
        raise WorkerAPIError(ErrorKind.UNAVAILABLE, "recorded")


def _calls(toolbox: Toolbox) -> None:
    for name, arguments in (
        ("shell", {"script": "true"}),
        ("python", {"code": "1"}),
        ("restart_kernel", {"language": "python"}),
        ("pdf_info", {"path": "a.pdf"}),
    ):
        call = ToolCall(id=name, name=name, arguments=json.dumps(arguments))
        asyncio.run(toolbox.call(call, f"op-{name}"))


def test_a_childs_code_names_its_write_area_and_the_platforms_requests_do_not() -> None:
    worker = Recording()
    toolbox = Toolbox(
        cast(WorkerClient, worker),
        CONTEXT,
        languages=("python",),
        features=("pdf",),
        delegated=True,
        kernel_run="r1",
        write_area=AREA,
    )
    _calls(toolbox)
    areas = {kind: context.write_area for kind, context in worker.sent}
    assert areas == {"shell": AREA, "cell": AREA, "restart": AREA, "pdf": None}


def test_a_main_runs_requests_name_none() -> None:
    worker = Recording()
    toolbox = Toolbox(cast(WorkerClient, worker), CONTEXT, languages=("python",), features=("pdf",))
    _calls(toolbox)
    assert {kind for kind, _ in worker.sent} == {"shell", "cell", "restart", "pdf"}
    assert all(context.write_area is None for _, context in worker.sent)


async def test_a_childs_job_names_its_area_unless_it_has_a_sandbox_of_its_own(
    tmp_path: Path,
) -> None:
    worker = FakeWorker()
    jobs = tools(worker, tmp_path, job_profiles=(MEDIUM,))
    jobs.write_area = AREA
    base = {"script": "python big.py", "purpose": "search", "expected_progress": "lines"}
    await jobs.start(base, "op-1")
    await jobs.start({**base, "profile": "medium"}, "op-2")
    here, sandboxed = worker.requested
    assert (here.context.write_area, sandboxed.context.write_area) == (AREA, None)
    jobs.write_area = None  # a main run's
    await jobs.start(base, "op-3")
    assert worker.requested[-1].context.write_area is None
