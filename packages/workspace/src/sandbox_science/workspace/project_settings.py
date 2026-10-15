# SPDX-License-Identifier: MIT
"""Explicit authenticated USER project settings: macros, and the files executions
write (research artifacts, RA-29, RA-39); same-origin guard is shared."""

from __future__ import annotations

from aiohttp import web

from sandbox_science.orchestrator.documents import USER, DocumentStore
from sandbox_science.orchestrator.documents.project_macros import ProjectMacros
from sandbox_science.orchestrator.file_settings import DEFAULT_IGNORE, ProjectFiles

from . import documents
from .context import json_body


def add_routes(app: web.Application) -> None:
    app.router.add_get("/api/v0/projects/{project}/macros", macros)
    app.router.add_post("/api/v0/projects/{project}/macros", change)
    app.router.add_get("/api/v0/projects/{project}/files/settings", file_settings)
    app.router.add_post("/api/v0/projects/{project}/files/settings", change_files)


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


async def file_settings(request: web.Request) -> web.Response:
    """The project's file settings, and the default patterns to start from."""
    return await documents.answer(
        request,
        lambda store: {
            **ProjectFiles(store).get(request.match_info["project"]).model_dump(),
            "defaults": list(DEFAULT_IGNORE),
        },
    )


async def change_files(request: web.Request) -> web.Response:
    value = await json_body(request, "file settings JSON")

    def changed(store: DocumentStore) -> dict[str, object]:
        settings, replayed = ProjectFiles(store).change(request.match_info["project"], value, USER)
        return {**settings.model_dump(), "replayed": replayed}

    return await documents.answer(request, changed, writer=True)
