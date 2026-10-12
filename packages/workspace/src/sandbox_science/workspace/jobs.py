# SPDX-License-Identifier: MIT
"""Authenticated job views and cancellation, proxied through the worker boundary.

The browser names a recorded project/session; its environment comes from the
trusted project record. It never selects a worker address or sees a worker key.
"""

from __future__ import annotations

import asyncio
from typing import Final

from aiohttp import hdrs, web

from sandbox_science.contracts import ErrorKind, ExecutionContext
from sandbox_science.orchestrator.jobs import JobTools, summary
from sandbox_science.orchestrator.jobstate import JobState
from sandbox_science.orchestrator.projects import ProjectError
from sandbox_science.orchestrator.worker_client import WorkerAPIError, WorkerClient

from . import api

WORKER: Final = web.AppKey("job_worker", WorkerClient)
STATE: Final = web.AppKey("job_state", JobState)
PATH: Final = "/api/v0/projects/{project}/sessions/{session}/jobs"


def add_routes(app: web.Application, worker: WorkerClient | None, state: JobState | None) -> None:
    if worker is not None:
        app[WORKER] = worker
    if state is not None:
        app[STATE] = state
    app.router.add_get(PATH, listed)
    app.router.add_get(PATH + "/{job}", status)
    app.router.add_post(PATH + "/{job}/cancel", cancel)


async def _tools(request: web.Request) -> JobTools:
    project, session = request.match_info["project"], request.match_info["session"]
    record = await asyncio.to_thread(request.app[api.STORE].get, project)
    found = record.sessions.get(session)
    if found is None:
        raise ProjectError(ErrorKind.NOT_FOUND, "no such session in this project")
    if WORKER not in request.app:
        raise ProjectError(ErrorKind.UNSUPPORTED, "job views need a configured worker connection")
    context = ExecutionContext(
        project_id=project, session_id=session, environment_id=found.environment_id
    )
    return JobTools(request.app[WORKER], context, run_id="workspace")


def _answer(value: object) -> web.Response:
    return web.Response(
        text=api.dumps(value),
        content_type="application/json",
        headers={hdrs.CACHE_CONTROL: "no-store"},
    )


def _error(exc: WorkerAPIError) -> ProjectError:
    return ProjectError(exc.kind, exc.message)


async def listed(request: web.Request) -> web.Response:
    try:
        tools = await _tools(request)
        listing = await tools.recent(limit=20)
        # Fail closed if an unexpected peer response breaks session scoping.
        checkins = (
            await asyncio.to_thread(request.app[STATE].checkins, tools.context)
            if STATE in request.app
            else []
        )
        latest = {str(check["job_id"]): check for check in reversed(checkins)}
        values: list[dict[str, object]] = []
        for execution in listing.jobs:
            tools.check_owner(execution)
            values.append(
                {
                    **summary(await tools.owned(execution.execution_id)),
                    "checkin": latest.get(execution.execution_id),
                }
            )
        waiting = (
            await asyncio.to_thread(request.app[STATE].waits, tools.context)
            if STATE in request.app
            else []
        )
        return _answer(
            {"enabled": True, "jobs": values, "truncated": listing.truncated, "waits": waiting}
        )
    except WorkerAPIError as exc:
        raise _error(exc) from exc


async def status(request: web.Request) -> web.Response:
    try:
        tools = await _tools(request)
        job_id = tools.job_id({"job_id": request.match_info["job"]})
        return _answer(summary(await tools.owned(job_id)))
    except WorkerAPIError as exc:
        raise _error(exc) from exc


async def cancel(request: web.Request) -> web.Response:
    try:
        tools = await _tools(request)
        # The route intentionally accepts no model-supplied context or script.
        text, _ = await tools.cancel({"job_id": request.match_info["job"]}, "workspace")
        return web.Response(
            text=text, content_type="application/json", headers={hdrs.CACHE_CONTROL: "no-store"}
        )
    except WorkerAPIError as exc:
        raise _error(exc) from exc
