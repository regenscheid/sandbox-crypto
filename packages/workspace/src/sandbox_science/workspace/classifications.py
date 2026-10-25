# SPDX-License-Identifier: MIT
"""The classification review view's routes (D96; workspace browsers, CL-1 to CL-6).

Each configured MCP service's classification in force, the drafts ``classify-draft`` wrote
for the project, accepting a draft (edited or not) and re-accepting a server's tool list
after drift. The worker holds all of it: the workspace reaches its operator routes with
its own operator client, passes the worker's JSON through and never parses a draft's
YAML (the worker checks it as the loader checks a file). A worker refusal answers as the
workspace's error; viewing calls no tool and grants nothing.

Routes (``…`` is ``/api/v0/projects/{project}``):

- ``GET …/classifications``: every configured service's classification in force.
- ``GET …/mcp-services/{server}/classification``: one service's, with ``project``.
- ``GET …/mcp-services/{server}/classification/drafts``: its drafts.
- ``POST …/mcp-services/{server}/classification/drafts/{draft}/accept`` with
  ``{"yaml", "reason", "operation_id"}``: accept the (edited) draft.
- ``POST …/mcp-services/{server}/drift/accept`` with ``{"reason", "operation_id"}``:
  re-accept the server's tool list, as ``accept-tools`` does.
- ``GET …/mcp-services/{server}/classification/revisions/{revision}/export``: the
  project's accepted revision, its YAML exactly as the worker stored it, with the
  worker's ``X-Classification-*`` headers, to check in.

A write's ``operation_id`` goes to the worker, which keeps the receipt: the same request
again answers the first result; another request under that ID is a conflict, which the
workspace answers as its own (409).
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable
from typing import Final, Protocol, cast

from aiohttp import web
from pydantic import BaseModel, Field, ValidationError

from sandbox_science.contracts import Contract, ErrorKind
from sandbox_science.contracts.classifications import (
    ClassificationAcceptance,
    ClassificationDraft,
    ClassificationInForce,
    DriftAcceptance,
)
from sandbox_science.orchestrator.projects import ProjectError
from sandbox_science.orchestrator.worker_client import ClassificationExport, WorkerClient

from .context import CONTEXT, identifier, json_answer, json_body, no_query
from .mcp_services import CONFIGURATION, Configuration
from .requests import dumps

PROJECT: Final = "/api/v0/projects/{project}"
SERVICE: Final = PROJECT + "/mcp-services/{server}"
MAX_YAML_BYTES: Final = 1024 * 1024
"""A classification file's limit, as the policy loader's."""
MAX_REASON: Final = 500
_OPERATION: Final = r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$"
_DRAFT: Final = r"^[0-9a-f]{64}$"
_REVISION: Final = _DRAFT
_METHODS: Final = (
    "classifications",
    "classification_drafts",
    "accept_classification_draft",
    "accept_classification_drift",
    "export_classification",
)


class ClassificationListing(Contract):
    """``GET …/classifications``: the project's configured services' classifications."""

    project: str
    classifications: list[ClassificationInForce]


class ClassificationView(ClassificationInForce):
    """``GET …/mcp-services/{server}/classification``: one service's, with its project."""

    project: str


class DraftListing(Contract):
    """``GET …/mcp-services/{server}/classification/drafts``: newest first."""

    project: str
    server: str
    drafts: list[ClassificationDraft]


class ClassificationSource(Protocol):
    """The worker client's classification methods, as the workspace's operator client has
    them (``WorkerClient``): argument names and order are the client's.

    Each answer is the worker's contract value, or its JSON."""

    async def classifications(self, project_id: str) -> object: ...

    async def classification_drafts(self, project_id: str, server: str) -> object: ...

    async def accept_classification_draft(
        self, server: str, draft: str, acceptance: ClassificationAcceptance
    ) -> object: ...

    async def accept_classification_drift(
        self, server: str, acceptance: DriftAcceptance
    ) -> object: ...

    async def export_classification(
        self, project_id: str, server: str, revision: str
    ) -> ClassificationExport: ...


SOURCE: Final = web.AppKey("classification_source", ClassificationSource)


def from_worker(worker: WorkerClient | None) -> ClassificationSource | None:
    """The worker client as the classification source, when it has the routes."""
    if worker is None or not all(callable(getattr(worker, name, None)) for name in _METHODS):
        return None
    return cast(ClassificationSource, worker)


def configure(app: web.Application, source: ClassificationSource | None) -> None:
    if source is not None:
        app[SOURCE] = source


def add_routes(app: web.Application) -> None:
    app.router.add_get(PROJECT + "/classifications", project_classifications)
    app.router.add_get(SERVICE + "/classification", in_force)
    app.router.add_get(SERVICE + "/classification/drafts", drafts)
    app.router.add_post(SERVICE + "/classification/drafts/{draft}/accept", accept_draft)
    app.router.add_post(SERVICE + "/drift/accept", accept_drift)
    app.router.add_get(SERVICE + "/classification/revisions/{revision}/export", export)


class _DraftAcceptance(Contract):
    yaml: str = Field(min_length=1, max_length=MAX_YAML_BYTES)
    reason: str = Field(min_length=1, max_length=MAX_REASON)
    operation_id: str = Field(pattern=_OPERATION)


class _DriftAcceptance(Contract):
    reason: str = Field(min_length=1, max_length=MAX_REASON)
    operation_id: str = Field(pattern=_OPERATION)


def _source(request: web.Request) -> ClassificationSource:
    found = request.app.get(SOURCE)
    if found is None:
        raise ProjectError(ErrorKind.UNAVAILABLE, "classifications need the worker")
    return found


def _configured(request: web.Request, project: str) -> frozenset[str]:
    config = request.app.get(CONFIGURATION, Configuration())
    if config.project != project:
        return frozenset()
    return frozenset(item.name for item in config.services)


async def _service(request: web.Request) -> tuple[str, str]:
    """The path's project, which must exist, and its configured server; else 404."""
    project, server = identifier(request, "project"), identifier(request, "server")
    await asyncio.to_thread(request.app[CONTEXT].projects.get, project)
    if server not in _configured(request, project):
        raise ProjectError(ErrorKind.NOT_FOUND, "configured MCP service was not found")
    return project, server


def _plain(value: object) -> object:
    """A worker answer as JSON values: a contract dumped as its routes dump it."""
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, list | tuple):
        return [_plain(item) for item in cast(list[object] | tuple[object, ...], value)]
    return value


def _records(value: object, what: str) -> list[dict[str, object]]:
    """The worker's list of records, each an object naming its ``server``."""
    found = _plain(value)
    if not isinstance(found, list):
        raise ProjectError(ErrorKind.UNAVAILABLE, f"the worker answered badly ({what})")
    records: list[dict[str, object]] = []
    for item in cast(list[object], found):
        if not isinstance(item, dict) or not isinstance(
            cast(dict[str, object], item).get("server"), str
        ):
            raise ProjectError(ErrorKind.UNAVAILABLE, f"the worker answered badly ({what})")
        records.append(cast(dict[str, object], item))
    return records


def _record(value: object, what: str) -> dict[str, object]:
    found = _plain(value)
    if not isinstance(found, dict):
        raise ProjectError(ErrorKind.UNAVAILABLE, f"the worker answered badly ({what})")
    return cast(dict[str, object], found)


async def _read[T](request: web.Request, call: Awaitable[T]) -> T:
    """A worker read, within the workspace's read deadline."""
    async with asyncio.timeout(request.app[CONTEXT].timeouts.call):
        return await call


async def project_classifications(request: web.Request) -> web.Response:
    """Every configured service's classification in force, by name."""
    no_query(request, "classification listing")
    project = identifier(request, "project")
    await asyncio.to_thread(request.app[CONTEXT].projects.get, project)
    configured = _configured(request, project)
    found: list[dict[str, object]] = []
    if configured:
        listed = await _read(request, _source(request).classifications(project))
        found = [
            item for item in _records(listed, "classifications") if item["server"] in configured
        ]
    found.sort(key=lambda item: str(item["server"]))
    return json_answer(dumps({"project": project, "classifications": found}))


async def in_force(request: web.Request) -> web.Response:
    """One service's classification in force, per tool with its pin state."""
    no_query(request, "classification")
    project, server = await _service(request)
    listed = await _read(request, _source(request).classifications(project))
    found = next(
        (item for item in _records(listed, "classifications") if item["server"] == server), None
    )
    if found is None:
        raise ProjectError(ErrorKind.NOT_FOUND, "no classification is in force for this service")
    return json_answer(dumps({**found, "project": project}))


async def drafts(request: web.Request) -> web.Response:
    """The drafts written for the service in the project: the model's text, as data."""
    no_query(request, "classification drafts")
    project, server = await _service(request)
    listed = await _read(request, _source(request).classification_drafts(project, server))
    found = [item for item in _records(listed, "drafts") if item["server"] == server]
    return json_answer(dumps({"project": project, "server": server, "drafts": found}))


async def accept_draft(request: web.Request) -> web.Response:
    """Accept a draft, edited or not, as the project's classification of the service."""
    project, server = await _service(request)
    draft = request.match_info["draft"]
    if request.query or re.fullmatch(_DRAFT, draft) is None:
        raise ProjectError(ErrorKind.INVALID_REQUEST, "invalid classification draft")
    raw = await json_body(request, "classification acceptance", limit=MAX_YAML_BYTES + 8192)
    try:
        body = _DraftAcceptance.model_validate(raw, strict=True)
    except ValidationError:
        raise ProjectError(
            ErrorKind.INVALID_REQUEST,
            f"an acceptance is the YAML and a reason of 1 to {MAX_REASON} characters",
        ) from None
    if not body.reason.strip():
        raise ProjectError(ErrorKind.INVALID_REQUEST, "an acceptance needs a reason")
    if len(body.yaml.encode()) > MAX_YAML_BYTES:
        raise ProjectError(ErrorKind.INVALID_REQUEST, "the classification YAML is too large")
    acceptance = ClassificationAcceptance(
        project=project, operation_id=body.operation_id, yaml=body.yaml, reason=body.reason
    )
    answer = await _source(request).accept_classification_draft(server, draft, acceptance)
    return json_answer(dumps(_record(answer, "acceptance")))


async def accept_drift(request: web.Request) -> web.Response:
    """Re-accept the service's tool list after drift, as ``accept-tools`` does."""
    project, server = await _service(request)
    if request.query:
        raise ProjectError(ErrorKind.INVALID_REQUEST, "re-acceptance takes no query")
    raw = await json_body(request, "drift re-acceptance")
    try:
        body = _DriftAcceptance.model_validate(raw, strict=True)
    except ValidationError:
        raise ProjectError(
            ErrorKind.INVALID_REQUEST,
            f"a re-acceptance is a reason of 1 to {MAX_REASON} characters",
        ) from None
    if not body.reason.strip():
        raise ProjectError(ErrorKind.INVALID_REQUEST, "a re-acceptance needs a reason")
    acceptance = DriftAcceptance(
        project=project, operation_id=body.operation_id, reason=body.reason
    )
    answer = await _source(request).accept_classification_drift(server, acceptance)
    return json_answer(dumps(_record(answer, "re-acceptance")))


async def export(request: web.Request) -> web.Response:
    """The project's accepted revision, its YAML exactly as stored, to check in (CL-3)."""
    no_query(request, "classification export")
    project, server = await _service(request)
    revision = request.match_info["revision"]
    if re.fullmatch(_REVISION, revision) is None:
        raise ProjectError(ErrorKind.INVALID_REQUEST, "invalid classification revision")
    found = await _read(request, _source(request).export_classification(project, server, revision))
    headers = {
        "Content-Disposition": f'attachment; filename="{server}.classification.yaml"',
        "Cache-Control": "no-store",
        "X-Content-Type-Options": "nosniff",
        "X-Classification-Server": found.server,
        "X-Classification-Revision": found.revision,
    }
    if found.accepted_at is not None:
        headers["X-Classification-Accepted-At"] = found.accepted_at.isoformat()
    return web.Response(body=found.yaml, content_type="application/yaml", headers=headers)
