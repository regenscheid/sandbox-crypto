# SPDX-License-Identifier: MIT
"""Job views and cancellation for a recorded session, through the worker.

The browser names a recorded project and session; the environment comes from the
project record. The browser never selects a worker address or sees a worker key.
"""

from __future__ import annotations

import asyncio
from typing import Final

from aiohttp import web

from sandbox_science.contracts import ErrorKind, ExecutionContext
from sandbox_science.orchestrator.jobs import JobTools, summary
from sandbox_science.orchestrator.projects import ProjectError

from .context import CONTEXT, identifier, json_answer, session_record
from .requests import dumps

PATH: Final = "/api/v0/projects/{project}/sessions/{session}/jobs"


def add_routes(app: web.Application) -> None:
    app.router.add_get(PATH, listed)
    app.router.add_post(PATH + "/{job}/cancel", cancel)


async def _tools(request: web.Request) -> JobTools:
    context = request.app[CONTEXT]
    project, session = identifier(request, "project"), identifier(request, "session")
    found = await asyncio.to_thread(session_record, context.projects, project, session)
    if context.worker is None:
        raise ProjectError(ErrorKind.UNSUPPORTED, "job views need a configured worker connection")
    scope = ExecutionContext(
        project_id=project, session_id=session, environment_id=found.environment_id
    )
    return JobTools(context.worker, scope, run_id="workspace")


async def listed(request: web.Request) -> web.Response:
    tools = await _tools(request)
    state = request.app[CONTEXT].job_state
    listing = await tools.recent(limit=20)
    checkins = await asyncio.to_thread(state.checkins, tools.context) if state is not None else []
    latest = {str(check["job_id"]): check for check in reversed(checkins)}
    values: list[dict[str, object]] = []
    for execution in listing.jobs:
        # Fail closed if an unexpected peer response breaks session scoping.
        tools.check_owner(execution)
        values.append(
            {
                **summary(await tools.owned(execution.execution_id), tools.clock.now()),
                "checkin": latest.get(execution.execution_id),
            }
        )
    waiting = await asyncio.to_thread(state.waits, tools.context) if state is not None else []
    return json_answer(
        dumps({"enabled": True, "jobs": values, "truncated": listing.truncated, "waits": waiting})
    )


async def cancel(request: web.Request) -> web.Response:
    tools = await _tools(request)
    # The route accepts no model-supplied context or script.
    text, _ = await tools.cancel({"job_id": request.match_info["job"]}, "workspace")
    return json_answer(text)
