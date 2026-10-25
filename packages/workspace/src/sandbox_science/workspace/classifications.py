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

A write keeps a receipt under its operation ID, as the settings routes do: the same
request again answers the first result with ``replayed: true`` and does not reach the
worker twice; another request under that ID is a conflict. The receipts last as long as
the workspace process, and its logins (``Receipts``).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Final, Protocol, cast

from aiohttp import web
from pydantic import BaseModel, Field, ValidationError

from sandbox_science.contracts import Contract, ErrorKind
from sandbox_science.orchestrator.projects import ProjectError
from sandbox_science.orchestrator.worker_client import WorkerClient

from .context import CONTEXT, identifier, json_answer, json_body, no_query
from .mcp_services import CONFIGURATION, Configuration
from .requests import dumps

PROJECT: Final = "/api/v0/projects/{project}"
SERVICE: Final = PROJECT + "/mcp-services/{server}"
MAX_YAML_BYTES: Final = 1024 * 1024
"""A classification file's limit, as the policy loader's."""
MAX_REASON: Final = 500
MAX_RECEIPTS: Final = 4096
_OPERATION: Final = r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$"
_DRAFT: Final = r"^[0-9a-f]{1,128}$"
_METHODS: Final = (
    "classifications",
    "classification_drafts",
    "accept_classification_draft",
    "accept_drift",
)


class ClassificationSource(Protocol):
    """The worker's classification routes, as the workspace's operator client calls them.

    Each answer is the worker's contract value, or its JSON."""

    async def classifications(self, project: str, /) -> object: ...

    async def classification_drafts(self, project: str, server: str, /) -> object: ...

    async def accept_classification_draft(
        self, project: str, server: str, draft: str, yaml: str, reason: str, /
    ) -> object: ...

    async def accept_drift(self, project: str, server: str, reason: str, /) -> object: ...


SOURCE: Final = web.AppKey("classification_source", ClassificationSource)


@dataclass
class Receipts:
    """The first result of each write, by operation ID, in this workspace process.

    The workspace keeps no store of its own (it reaches the control store only through
    the orchestrator's repositories, which name no classification receipts yet), and a
    restart ends every login, so a page's retry meets the process that answered it. At most
    ``MAX_RECEIPTS`` are kept; the oldest go first. Writes are serialized, since the
    worker's call cannot be in a receipt's transaction: two tabs sending one operation
    never both reach the worker."""

    found: OrderedDict[tuple[str, str], tuple[str, dict[str, object]]] = field(
        default_factory=OrderedDict[tuple[str, str], tuple[str, dict[str, object]]]
    )
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def find(self, project: str, operation: str, fingerprint: str) -> dict[str, object] | None:
        """The first result under ``operation``; another request under it is a conflict."""
        kept = self.found.get((project, operation))
        if kept is None:
            return None
        if kept[0] != fingerprint:
            raise ProjectError(
                ErrorKind.CONFLICT,
                f"operation ID {operation!r} already names another classification change",
            )
        return kept[1]

    def record(
        self, project: str, operation: str, fingerprint: str, result: dict[str, object]
    ) -> None:
        self.found[(project, operation)] = (fingerprint, result)
        while len(self.found) > MAX_RECEIPTS:
            self.found.popitem(last=False)


RECEIPTS: Final = web.AppKey("classification_receipts", Receipts)


def from_worker(worker: WorkerClient | None) -> ClassificationSource | None:
    """The worker client as the classification source, when it has the routes."""
    if worker is None or not all(callable(getattr(worker, name, None)) for name in _METHODS):
        return None
    return cast(ClassificationSource, worker)


def configure(app: web.Application, source: ClassificationSource | None) -> None:
    if source is not None:
        app[SOURCE] = source
    app[RECEIPTS] = Receipts()


def add_routes(app: web.Application) -> None:
    app.router.add_get(PROJECT + "/classifications", project_classifications)
    app.router.add_get(SERVICE + "/classification", in_force)
    app.router.add_get(SERVICE + "/classification/drafts", drafts)
    app.router.add_post(SERVICE + "/classification/drafts/{draft}/accept", accept_draft)
    app.router.add_post(SERVICE + "/drift/accept", accept_drift)


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


def _fingerprint(kind: str, fields: dict[str, str]) -> str:
    text = json.dumps({"kind": kind, **fields}, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode()).hexdigest()


async def _once(
    request: web.Request,
    project: str,
    operation: str,
    fingerprint: str,
    send: Callable[[], Awaitable[dict[str, object]]],
) -> web.Response:
    """Send a write once per operation ID; a retry answers the first result again."""
    receipts = request.app[RECEIPTS]
    async with receipts.lock:
        replay = receipts.find(project, operation, fingerprint)
        if replay is not None:
            return json_answer(dumps({**replay, "replayed": True}))
        result = await send()
        receipts.record(project, operation, fingerprint, result)
    return json_answer(dumps({**result, "replayed": False}))


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
    source = _source(request)
    fields = {
        "project": project, "server": server, "draft": draft,
        "yaml": body.yaml, "reason": body.reason,
    }  # fmt: skip

    async def send() -> dict[str, object]:
        answer = await source.accept_classification_draft(
            project, server, draft, body.yaml, body.reason
        )
        return _record(answer, "acceptance")

    return await _once(request, project, body.operation_id, _fingerprint("draft", fields), send)


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
    source = _source(request)
    fields = {"project": project, "server": server, "reason": body.reason}

    async def send() -> dict[str, object]:
        return _record(await source.accept_drift(project, server, body.reason), "re-acceptance")

    return await _once(request, project, body.operation_id, _fingerprint("drift", fields), send)
