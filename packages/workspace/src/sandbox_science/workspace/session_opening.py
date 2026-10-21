# SPDX-License-Identifier: MIT
"""New sessions from the workspace (projects and sessions; run service, RS-2).

``POST /api/v0/projects/{project}/sessions`` with ``{"session_id": NAME}`` opens a session
of the project in the project's shared environment, as the harness opens one on first
use, and answers it (201). A name the project already has is a conflict: this route
neither reopens a closed session nor joins an open one, which the session list does. A
session in its own environment, which mints one, stays with the command line.

The workspace's project store only reads (``create_app``), so the record is written
through a store opened for this write alone. The workspace starts no run: the session's
first message does, through the run service.
"""

from __future__ import annotations

import asyncio

from aiohttp import web
from pydantic import ValidationError

from sandbox_science.contracts import Contract, ErrorKind, Identifier
from sandbox_science.orchestrator.projects import ProjectError, ProjectStore

from . import api
from .context import CONTEXT, identifier, json_body


class NewSession(Contract):
    session_id: Identifier


def add_routes(app: web.Application) -> None:
    app.router.add_post("/api/v0/projects/{project}/sessions", create)


async def create(request: web.Request) -> web.Response:
    project = identifier(request, "project")
    raw = await json_body(request, "new session")
    try:
        wanted = NewSession.model_validate(raw, strict=True)
    except ValidationError:
        raise ProjectError(
            ErrorKind.INVALID_REQUEST,
            "a session is named by up to 128 letters, digits, '.', '_' and '-', "
            "starting with a letter or digit",
        ) from None
    context = request.app[CONTEXT]

    def write() -> str:
        found = context.projects.get(project)  # a project that does not exist is not found
        if wanted.session_id in found.sessions:
            raise ProjectError(
                ErrorKind.CONFLICT, f"the project already has a session {wanted.session_id!r}"
            )
        store = ProjectStore(context.projects.root.parent)
        try:
            opened = store.open_session(project, wanted.session_id, dedicated=False)
        finally:
            store.close()
        return api.dumps(api.SessionView.of(opened).model_dump(mode="json"))

    text = await asyncio.to_thread(write)
    return web.Response(
        status=201,
        text=text,
        content_type="application/json",
        headers={"Cache-Control": "no-store"},
    )
