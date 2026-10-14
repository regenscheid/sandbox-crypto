# SPDX-License-Identifier: MIT
"""Explicit USER revisions of a session plan's body.

``POST /api/v0/projects/{project}/sessions/{session}/plan/changes`` takes a JSON
``BodyChange``: an operation ID, the base version it edits, the new body and a reason.
The plans store checks the base (a stale one is 409 with the current version) and
replays a repeated operation. The user identity and the scope come from the login and
the path, never from the body. Creating a plan, progress, status and approval are not
offered here, and a closed session's plan cannot be edited.
"""

from __future__ import annotations

import asyncio
import sqlite3
from contextlib import suppress
from typing import Final

from aiohttp import web
from pydantic import Field, ValidationError

from sandbox_science.contracts import ErrorKind, Identifier
from sandbox_science.orchestrator.documents import DocumentError
from sandbox_science.orchestrator.projects import ProjectError
from sandbox_science.orchestrator.session_plans import (
    PlanAuthority,
    PlanBody,
    PlanModel,
    PlanReceipt,
    PlanRef,
    PlanScope,
    SessionPlans,
)

from . import errors
from .context import CONTEXT, identifier, json_answer, session_record
from .plans import PATH, has_records

MAX_REQUEST_BYTES: Final = 128 * 1024


class BodyChange(PlanModel):
    """A body edit against an exact base version."""

    operation_id: Identifier
    base: PlanRef
    body: PlanBody
    reason: str = Field(min_length=1, max_length=4096)


class EditReceipt(PlanModel):
    """The acknowledgement of one edit; the current plan is read separately."""

    project_id: Identifier
    session_id: Identifier
    receipt: PlanReceipt


def add_routes(app: web.Application) -> None:
    app.router.add_post(PATH + "/changes", change)


async def change(request: web.Request) -> web.Response:
    context = request.app[CONTEXT]
    project, session = identifier(request, "project"), identifier(request, "session")
    if request.query or request.content_type != "application/json":
        raise ProjectError(ErrorKind.INVALID_REQUEST, "plan edit needs path scope and JSON")
    try:
        raw = await request.clone(client_max_size=MAX_REQUEST_BYTES).read()
    except web.HTTPRequestEntityTooLarge:
        return errors.json_error(413, ErrorKind.INVALID_REQUEST, "plan edit exceeds its byte limit")
    try:
        wanted = BodyChange.model_validate_json(raw, strict=True)
    except ValidationError:
        raise ProjectError(ErrorKind.INVALID_REQUEST, "invalid plan edit fields or text") from None
    authority = PlanAuthority(
        scope=PlanScope(project_id=project, session_id=session),
        role="user",
        user_id=context.principal(request),
    )

    def write() -> str:
        if session_record(context.projects, project, session).closed_at is not None:
            raise ProjectError(ErrorKind.CONFLICT, "a closed session cannot be edited")
        # An edit never creates the plans' tables or a plan.
        if SessionPlans(context.documents).summary(authority) is None:
            if has_records(context.documents, authority.scope):
                raise ProjectError(ErrorKind.UNAVAILABLE, "session plan is damaged")
            raise ProjectError(ErrorKind.NOT_FOUND, "this session has no plan to edit")
        receipt = SessionPlans(context.writer).revise(
            authority, wanted.operation_id, wanted.base, wanted.body, wanted.reason
        )
        return EditReceipt(
            project_id=project, session_id=session, receipt=receipt
        ).model_dump_json()

    try:
        async with asyncio.timeout(context.timeouts.call):
            return json_answer(await asyncio.to_thread(write))
    except DocumentError as caught:
        current: dict[str, object] = {}
        if caught.kind == "conflict":
            with suppress(ValueError):
                ref = PlanRef.model_validate(caught.details.get("current"), strict=True)
                current = {"current": ref.model_dump(mode="json")}
        kind = errors.document_kind(caught)
        return errors.error(
            kind if kind in errors.STATUS else ErrorKind.UNAVAILABLE,
            "plan edit refused; reread the recorded plan",
            fields=current,
        )
    except (sqlite3.Error, OSError):
        raise ProjectError(
            ErrorKind.UNAVAILABLE, "plan edit unavailable; its outcome may be unknown"
        ) from None
