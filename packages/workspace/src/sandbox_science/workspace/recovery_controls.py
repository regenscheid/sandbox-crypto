# SPDX-License-Identifier: MIT
"""Operator-only recovery routes; workspace login and same-origin middleware apply."""

from __future__ import annotations

import asyncio

from aiohttp import web
from pydantic import TypeAdapter, ValidationError

from sandbox_science.contracts import ErrorKind, Identifier
from sandbox_science.orchestrator.continuity.resolutions import RecoveryControls, Resolution
from sandbox_science.orchestrator.continuity.status import RecoveryStatus
from sandbox_science.orchestrator.projects import ProjectError, RunName

from .context import CONTEXT, USER_ID, identifier, json_answer, json_body, session_record
from .resource_requests import add_routes as add_resource_routes

PATH = "/api/v0/projects/{project}/sessions/{session}/recovery"
_RUN = TypeAdapter[str](RunName)
_ID = TypeAdapter[str](Identifier)


def add_routes(app: web.Application) -> None:
    add_resource_routes(app, PATH)
    app.router.add_get(PATH, status)
    app.router.add_get(PATH + "/works/{work}", pending)
    app.router.add_post(PATH + "/resolve", resolve)


def _cursor(request: web.Request, name: str, *, run: bool = False) -> str:
    value = request.query.get(name, "")
    if not value:
        return ""
    try:
        return (_RUN if run else _ID).validate_python(value, strict=True)
    except ValidationError:
        raise ProjectError(ErrorKind.INVALID_REQUEST, f"invalid {name} cursor") from None


def _number(request: web.Request, name: str, default: int, top: int) -> int:
    value = request.query.get(name, str(default))
    if not value.isascii() or not value.isdecimal() or len(value) > 20:
        raise ProjectError(ErrorKind.INVALID_REQUEST, f"invalid {name}")
    parsed = int(value)
    if not (1 if name == "limit" else 0) <= parsed <= top:
        raise ProjectError(ErrorKind.INVALID_REQUEST, f"invalid {name}")
    return parsed


async def status(request: web.Request) -> web.Response:
    project, session = identifier(request, "project"), identifier(request, "session")
    context = request.app[CONTEXT]
    after_work, after_resume = (
        _cursor(request, "after_work", run=True),
        _cursor(request, "after_resume"),
    )
    limit = _number(request, "limit", 100, 100)

    def read() -> str:
        session_record(context.projects, project, session)
        return (
            RecoveryStatus(context.documents)
            .page(project, session, after_work=after_work, after_resume=after_resume, limit=limit)
            .model_dump_json()
        )

    return json_answer(await asyncio.to_thread(read))


async def pending(request: web.Request) -> web.Response:
    project, session = identifier(request, "project"), identifier(request, "session")
    try:
        work = _RUN.validate_python(request.match_info["work"], strict=True)
    except ValidationError:
        raise ProjectError(ErrorKind.INVALID_REQUEST, "invalid work identity") from None
    context = request.app[CONTEXT]
    after_run = _cursor(request, "model_run", run=True)
    after_identity = request.query.get("model_identity", "")
    if len(after_identity) > 512 or bool(after_run) != bool(after_identity):
        raise ProjectError(
            ErrorKind.INVALID_REQUEST, "model cursor requires original run and identity"
        )
    after_operation = _number(request, "operation_after", 0, 2**62)
    limit = _number(request, "limit", 100, 100)

    def read() -> str:
        session_record(context.projects, project, session)
        return (
            RecoveryStatus(context.documents)
            .pending(
                project,
                session,
                work,
                model_after=(after_run, after_identity),
                operation_after=after_operation,
                limit=limit,
            )
            .model_dump_json()
        )

    return json_answer(await asyncio.to_thread(read))


async def resolve(request: web.Request) -> web.Response:
    project, session = identifier(request, "project"), identifier(request, "session")
    try:
        body = Resolution.model_validate(await json_body(request, "recovery resolution"))
    except ValidationError as error:
        raise ProjectError(ErrorKind.INVALID_REQUEST, error.errors()[0]["msg"][:300]) from None
    context = request.app[CONTEXT]

    def write() -> str:
        session_record(context.projects, project, session)
        return (
            RecoveryControls(context.writer)
            .resolve(project, session, body, operator=USER_ID or "user")
            .model_dump_json()
        )

    return json_answer(await asyncio.to_thread(write))
