# SPDX-License-Identifier: MIT
"""Authenticated USER views of research check-ins, soft extensions and recorded usage.

The token approvals of D87 are gone (D102): a soft cap checks in with the work's parent,
and only LiteLLM's budget, or a configured local limit, stops spending.
"""

from __future__ import annotations

import asyncio

from aiohttp import web
from pydantic import ValidationError

from sandbox_science.contracts import ErrorKind
from sandbox_science.orchestrator.accounting import account
from sandbox_science.orchestrator.continuity.resources import (
    ResourceApprovalRequest,
    ResourceRequests,
)
from sandbox_science.orchestrator.projects import ProjectError
from sandbox_science.orchestrator.runstate import RunState

from .context import (
    CONTEXT,
    USER_ID,
    identifier,
    json_answer,
    json_body,
    no_query,
    session_record,
)


def add_routes(app: web.Application, path: str) -> None:
    app.router.add_get(path + "/resources", resources)
    app.router.add_post(path + "/resources/decide", approve_resource)
    app.router.add_get(path + "/usage", session_usage)


async def session_usage(request: web.Request) -> web.Response:
    """The session's recorded spend and tokens by run and role, beside its project's."""
    project, session = identifier(request, "project"), identifier(request, "session")
    no_query(request, "usage")
    context = request.app[CONTEXT]

    def read() -> str:
        session_record(context.projects, project, session)
        return account(RunState(context.projects), project, session).model_dump_json()

    return json_answer(await asyncio.to_thread(read))


async def resources(request: web.Request) -> web.Response:
    project, session = identifier(request, "project"), identifier(request, "session")
    after = request.query.get("after", "")
    if len(after) > 512:
        raise ProjectError(ErrorKind.INVALID_REQUEST, "invalid resource cursor")
    context = request.app[CONTEXT]

    def read() -> str:
        session_record(context.projects, project, session)
        return (
            ResourceRequests(context.documents)
            .page(project, session, after=after)
            .model_dump_json()
        )

    return json_answer(await asyncio.to_thread(read))


async def approve_resource(request: web.Request) -> web.Response:
    """Record a USER soft-allowance extension; root Resume remains a separate action."""
    project, session = identifier(request, "project"), identifier(request, "session")
    try:
        body = ResourceApprovalRequest.model_validate(
            await json_body(request, "USER resource approval")
        )
    except ValidationError as error:
        raise ProjectError(ErrorKind.INVALID_REQUEST, error.errors()[0]["msg"][:300]) from None
    context = request.app[CONTEXT]

    def write() -> str:
        session_record(context.projects, project, session)
        return (
            ResourceRequests(context.writer)
            .approve(project, session, body, operator=USER_ID or "user")
            .model_dump_json()
        )

    return json_answer(await asyncio.to_thread(write))
