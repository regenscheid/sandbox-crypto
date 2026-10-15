# SPDX-License-Identifier: MIT
"""Passive kernel observations for an open session of a recorded project.

``GET /api/v0/projects/{project}/sessions/{session}/kernels`` asks the worker which
kernels the session has. The environment comes from the project record; no browser
input selects a worker or an environment.

The worker reads one environment's kernels for one caller at a time and refuses a second
at once (``KERNEL_OBSERVATION_BUSY``). That happens now and then, when this panel's poll
and a run's check before a turn fall together, and the panel asks again a few seconds
later. It is answered 429, ``unavailable``, without a log line, so the page can wait
quietly; any other worker failure is a 503 and a warning.
"""

from __future__ import annotations

import asyncio
from typing import Final

from aiohttp import web

from sandbox_science.contracts import ErrorKind, ExecutionContext, Identifier, KernelObservation
from sandbox_science.contracts.worker import KERNEL_OBSERVATION_BUSY
from sandbox_science.orchestrator.projects import ProjectError
from sandbox_science.orchestrator.worker_client import WorkerAPIError, WorkerProtocolError

from .context import CONTEXT, identifier, json_answer, no_query, session_record
from .errors import json_error, log_worker_failure

PATH: Final = "/api/v0/projects/{project}/sessions/{session}/kernels"


class WorkspaceObservation(KernelObservation):
    """The worker's observation plus the project it belongs to."""

    project_id: Identifier


def add_routes(app: web.Application) -> None:
    app.router.add_get(PATH, observed)


async def observed(request: web.Request) -> web.Response:
    context = request.app[CONTEXT]
    project, session = identifier(request, "project"), identifier(request, "session")
    no_query(request, "kernel observation")
    found = await asyncio.to_thread(session_record, context.projects, project, session)
    if found.closed_at is not None:
        raise ProjectError(ErrorKind.NOT_FOUND, "no open session in this project")
    if context.worker is None:
        raise ProjectError(ErrorKind.UNSUPPORTED, "kernel observations need a configured worker")
    scope = ExecutionContext(
        project_id=project, session_id=session, environment_id=found.environment_id
    )
    try:
        async with asyncio.timeout(context.timeouts.call):
            observation = await context.worker.observe_kernels(scope)
    except WorkerProtocolError as error:
        raise ProjectError(error.kind, error.message) from error
    except WorkerAPIError as error:
        if error.kind is ErrorKind.UNAVAILABLE and error.message == KERNEL_OBSERVATION_BUSY:
            return json_error(429, ErrorKind.UNAVAILABLE, "kernel observation is busy; try again")
        unsupported = error.kind is ErrorKind.UNSUPPORTED
        if not unsupported:
            log_worker_failure(error)
        raise ProjectError(
            ErrorKind.UNSUPPORTED if unsupported else ErrorKind.UNAVAILABLE,
            "kernel observation is unavailable or unsupported",
        ) from error
    answer = WorkspaceObservation(project_id=project, **observation.model_dump())
    return json_answer(answer.model_dump_json())
