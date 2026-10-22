# SPDX-License-Identifier: MIT
"""Explicit authenticated USER project settings: macros, and the files executions
write (research artifacts, RA-29, RA-39); same-origin guard is shared."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from aiohttp import web

from sandbox_science.orchestrator.documents import USER, DocumentStore
from sandbox_science.orchestrator.documents.project_macros import ProjectMacros
from sandbox_science.orchestrator.file_settings import DEFAULT_IGNORE, ProjectFiles
from sandbox_science.orchestrator.model_profiles import ModelProfile
from sandbox_science.orchestrator.model_settings import AgentModelRole, ProjectModels

from .context import json_body
from .requests import document_answer


@dataclass(frozen=True)
class ModelConfiguration:
    defaults: dict[AgentModelRole, ModelProfile]
    available: dict[str, Literal["chat", "responses", "messages"]]


MODEL_CONFIGURATION = web.AppKey("model-configuration", ModelConfiguration)


def add_routes(app: web.Application) -> None:
    app.router.add_get("/api/v0/projects/{project}/macros", macros)
    app.router.add_post("/api/v0/projects/{project}/macros", change)
    app.router.add_get("/api/v0/projects/{project}/files/settings", file_settings)
    app.router.add_post("/api/v0/projects/{project}/files/settings", change_files)
    app.router.add_get("/api/v0/projects/{project}/models/settings", model_settings)
    app.router.add_post("/api/v0/projects/{project}/models/settings", change_models)


async def macros(request: web.Request) -> web.Response:
    return await document_answer(
        request, lambda store: ProjectMacros(store).get(request.match_info["project"])
    )


async def change(request: web.Request) -> web.Response:
    value = await json_body(request, "project macro JSON")
    return await document_answer(
        request,
        lambda store: ProjectMacros(store).change(request.match_info["project"], value, USER),
        access="write",
    )


async def file_settings(request: web.Request) -> web.Response:
    """The project's file settings, and the default patterns to start from."""
    return await document_answer(
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

    return await document_answer(request, changed, access="write")


def _models(request: web.Request) -> ModelConfiguration:
    configured = request.app.get(MODEL_CONFIGURATION)
    if configured is None:
        raise web.HTTPServiceUnavailable(text="agent model settings are not configured")
    return configured


async def model_settings(request: web.Request) -> web.Response:
    """The three agent roles' selected aliases and efforts, and trusted choices."""
    configured = _models(request)
    return await document_answer(
        request,
        lambda store: (
            ProjectModels(store)
            .get(request.match_info["project"], configured.defaults, configured.available)
            .model_dump(mode="json")
        ),
    )


async def change_models(request: web.Request) -> web.Response:
    value = await json_body(request, "project model settings JSON")
    configured = _models(request)

    def changed(store: DocumentStore) -> dict[str, object]:
        settings, replayed = ProjectModels(store).change(
            request.match_info["project"],
            value,
            USER,
            defaults=configured.defaults,
            available=configured.available,
        )
        return {**settings.model_dump(mode="json"), "replayed": replayed}

    return await document_answer(request, changed, access="write")
