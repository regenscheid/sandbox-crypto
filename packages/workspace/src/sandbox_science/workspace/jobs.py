# SPDX-License-Identifier: MIT
"""Job views, cancellation and early release of a job sandbox, for a recorded session.

The browser names a recorded project and session; the environment comes from the
project record. The browser never selects a worker address or sees a worker key.

``POST …/jobs/{job}/release`` with ``{"operation_id"}`` is the user's "release now" for
a retained job sandbox (JS-24): the worker deletes it by its identity and refuses one
that is not retained (409). The worker keeps the receipt, so the same request again
answers the first result.
"""

from __future__ import annotations

import asyncio
from typing import Final

from aiohttp import web
from pydantic import ValidationError

from sandbox_science.contracts import Contract, ErrorKind, ExecutionContext, Identifier
from sandbox_science.orchestrator.jobs import JobTools, summary
from sandbox_science.orchestrator.projects import ProjectError

from .context import CONTEXT, identifier, json_answer, json_body, session_record
from .requests import dumps

PATH: Final = "/api/v0/projects/{project}/sessions/{session}/jobs"


def add_routes(app: web.Application) -> None:
    app.router.add_get(PATH, listed)
    app.router.add_post(PATH + "/{job}/cancel", cancel)
    app.router.add_post(PATH + "/{job}/release", release)


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


class _Release(Contract):
    operation_id: Identifier


async def release(request: web.Request) -> web.Response:
    """Delete the job's retained sandbox now; the session's own job only."""
    tools = await _tools(request)
    job = identifier(request, "job")
    if request.query:
        raise ProjectError(ErrorKind.INVALID_REQUEST, "a release takes no query")
    raw = await json_body(request, "job sandbox release", limit=4096)
    try:
        body = _Release.model_validate(raw, strict=True)
    except ValidationError:
        raise ProjectError(
            ErrorKind.INVALID_REQUEST, "a release is an operation_id, a short token"
        ) from None
    # The job must be this session's before the worker is asked to act on it.
    status = await tools.owned(job)
    released = await tools.client.release_job_sandbox(
        status.execution.execution_id, tools.context, body.operation_id
    )
    return json_answer(dumps({"job_id": released.execution_id, "phase": released.phase}))
