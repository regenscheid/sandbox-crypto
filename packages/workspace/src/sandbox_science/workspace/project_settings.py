# SPDX-License-Identifier: MIT
"""Explicit authenticated USER project macro settings; same-origin guard is shared."""

from __future__ import annotations

from aiohttp import web

from sandbox_science.orchestrator.documents import USER
from sandbox_science.orchestrator.documents.project_macros import ProjectMacros

from . import documents
from .context import json_body


def add_routes(app: web.Application) -> None:
    app.router.add_get("/api/v0/projects/{project}/macros", macros)
    app.router.add_post("/api/v0/projects/{project}/macros", change)


async def macros(request: web.Request) -> web.Response:
    return await documents.answer(
        request, lambda store: ProjectMacros(store).get(request.match_info["project"])
    )


async def change(request: web.Request) -> web.Response:
    value = await json_body(request, "project macro JSON")
    return await documents.answer(
        request,
        lambda store: ProjectMacros(store).change(request.match_info["project"], value, USER),
        writer=True,
    )
