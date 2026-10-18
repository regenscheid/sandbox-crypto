# SPDX-License-Identifier: MIT
"""Authenticated USER views and explicit incremental research token approvals."""

from __future__ import annotations

import asyncio

from aiohttp import web
from pydantic import TypeAdapter, ValidationError

from sandbox_science.contracts import ErrorKind
from sandbox_science.orchestrator.continuity.grants import TokenGrantRequest, TokenGrants
from sandbox_science.orchestrator.continuity.resources import (
    ResourceApprovalRequest,
    ResourceRequests,
)
from sandbox_science.orchestrator.projects import ProjectError, RunName

from .context import CONTEXT, USER_ID, identifier, json_answer, json_body, session_record

_RUN = TypeAdapter[str](RunName)


def add_routes(app: web.Application, path: str) -> None:
    app.router.add_get(path + "/resources", resources)
    app.router.add_post(path + "/resources/decide", approve_resource)
    app.router.add_get(path + "/token-grants/{work}", token_status)
    app.router.add_post(path + "/token-grants/next", approve_tokens)


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


async def token_status(request: web.Request) -> web.Response:
    project, session = identifier(request, "project"), identifier(request, "session")
    try:
        work = _RUN.validate_python(request.match_info["work"], strict=True)
    except ValidationError:
        raise ProjectError(ErrorKind.INVALID_REQUEST, "invalid work identity") from None
    context = request.app[CONTEXT]

    def read() -> str:
        session_record(context.projects, project, session)
        return TokenGrants(context.documents).status(project, session, work).model_dump_json()

    return json_answer(await asyncio.to_thread(read))


async def approve_tokens(request: web.Request) -> web.Response:
    project, session = identifier(request, "project"), identifier(request, "session")
    try:
        body = TokenGrantRequest.model_validate(await json_body(request, "USER token approval"))
    except ValidationError as error:
        raise ProjectError(ErrorKind.INVALID_REQUEST, error.errors()[0]["msg"][:300]) from None
    context = request.app[CONTEXT]

    def write() -> str:
        session_record(context.projects, project, session)
        return (
            TokenGrants(context.writer)
            .renew_next(project, session, body, operator=USER_ID or "user")
            .model_dump_json()
        )

    return json_answer(await asyncio.to_thread(write))


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
