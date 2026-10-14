# SPDX-License-Identifier: MIT
"""Read-only provisioning observations for the environments a project records.

The environment list comes from the project record alone. Every other route checks
that the environment is one of the project's, then reads the observation from the
configured worker through ``ProvisionObserver``. The browser selects only what the
path and a few query fields name; it never supplies a worker, a token or an execution
context. Errors carry their own ``observed_at``.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final, Literal

from aiohttp import web
from pydantic import TypeAdapter, ValidationError

from sandbox_science.contracts import ErrorKind, Identifier
from sandbox_science.contracts.provision_observations import (
    Observation,
    ProvisionDetail,
    ProvisionFilePage,
    ProvisionObservation,
    RevisionProbePage,
    RevisionSummaryPage,
)
from sandbox_science.orchestrator.projects import ProjectError
from sandbox_science.orchestrator.worker_client import WorkerAPIError

from . import errors
from .context import CONTEXT, identifier, json_answer
from .provisioning_client import ProvisionObserver

PATH: Final = "/api/v0/projects/{project}/environments/{environment}/provisioning/observations"
ENVIRONMENTS_PATH: Final = "/api/v0/projects/{project}/provisioning/environments"
_IDENTIFIER: Final = TypeAdapter[str](Identifier)
_PIN: Final = re.compile(r"[0-9a-f]{64}")
Kind = Literal[
    "environments", "summary", "detail", "files", "revisions", "probes", "lockfile-diff", "packages"
]
_QUERY: Final[dict[Kind, frozenset[str]]] = {
    "environments": frozenset(),
    "summary": frozenset(),
    "detail": frozenset({"seq"}),
    "files": frozenset({"seq", "offset", "limit"}),
    "revisions": frozenset({"after", "limit", "target"}),
    "probes": frozenset({"pin", "offset", "limit"}),
    "packages": frozenset({"pin", "offset", "limit"}),
    "lockfile-diff": frozenset({"before", "before_pin", "after", "after_pin", "offset", "limit"}),
}
"""The query fields each route accepts."""


@dataclass(frozen=True)
class Selection:
    """What a route reads, from its path and query only."""

    kind: Kind
    operation: str | None = None
    seq: int | None = None
    revision: int | None = None
    pin: str | None = None
    after: int = -1
    target: int | None = None
    offset: int = 0
    limit: int = 8
    before: int | None = None
    before_pin: str | None = None
    after_pin: str | None = None


def _number(raw: str, minimum: int, maximum: int = 2**62) -> int:
    if len(raw) > 19 or re.fullmatch(r"(?:0|[1-9][0-9]*|-1)", raw) is None:
        raise ProjectError(ErrorKind.INVALID_REQUEST, "invalid observation selection counter")
    value = int(raw)
    if not minimum <= value <= maximum:
        raise ProjectError(
            ErrorKind.INVALID_REQUEST, "observation selection counter exceeds its bound"
        )
    return value


def _selection(request: web.Request, kind: Kind) -> Selection:
    query = request.query
    if request.can_read_body or len(query) != len(set(query)) or set(query) - _QUERY[kind]:
        raise ProjectError(ErrorKind.INVALID_REQUEST, "observation accepts only its selected scope")
    operation = request.match_info.get("operation")
    try:
        if operation is not None:
            _IDENTIFIER.validate_python(operation, strict=True)
    except ValidationError as error:
        raise ProjectError(
            ErrorKind.INVALID_REQUEST, "invalid provisioning operation ID"
        ) from error
    if kind == "files" and "seq" not in query:
        raise ProjectError(ErrorKind.INVALID_REQUEST, "operation selection requires its exact seq")
    pins = ("pin",) if kind in {"probes", "packages"} else ()
    if kind == "lockfile-diff":
        if not {"before", "after"} <= set(query):
            raise ProjectError(
                ErrorKind.INVALID_REQUEST, "comparison requires both exact revision pins"
            )
        pins = ("before_pin", "after_pin")
    if any(_PIN.fullmatch(query.get(name, "")) is None for name in pins):
        raise ProjectError(ErrorKind.INVALID_REQUEST, "revision selection requires its exact pin")
    revision = request.match_info.get("revision")
    return Selection(
        kind,
        operation=operation,
        seq=_number(query["seq"], 0) if "seq" in query else None,
        revision=_number(revision, 0) if revision is not None else None,
        pin=query.get("pin"),
        after=_number(query.get("after", "-1"), 0 if kind == "lockfile-diff" else -1),
        target=_number(query["target"], 0) if "target" in query else None,
        offset=_number(
            query.get("offset", "0"),
            0,
            80_000 if kind == "lockfile-diff" else 40_000 if kind == "packages" else 2**62,
        ),
        limit=_number(query.get("limit", "8"), 1, 8),
        before=_number(query["before"], 0) if "before" in query else None,
        before_pin=query.get("before_pin"),
        after_pin=query.get("after_pin"),
    )


async def _fetch(
    observer: ProvisionObserver, project: str, environment: str, wanted: Selection
) -> Observation:
    path = f"/v1/environments/{environment}/provisioning/observations"
    params = {"project_id": project}
    page = {"offset": str(wanted.offset), "limit": str(wanted.limit)}
    match wanted.kind:
        case "summary":
            return await observer.get(path, params, ProvisionObservation)
        case "detail":
            seq = {"seq": str(wanted.seq)} if wanted.seq is not None else {}
            return await observer.get(
                f"{path}/operations/{wanted.operation}", params | seq, ProvisionDetail
            )
        case "files":
            return await observer.get(
                f"{path}/operations/{wanted.operation}/files",
                params | {"seq": str(wanted.seq)} | page,
                ProvisionFilePage,
            )
        case "revisions":
            target = {"target": str(wanted.target)} if wanted.target is not None else {}
            return await observer.get(
                f"{path}/revisions",
                params | {"after": str(wanted.after), "limit": str(wanted.limit)} | target,
                RevisionSummaryPage,
            )
        case "probes":
            return await observer.get(
                f"{path}/revisions/{wanted.revision}/probes",
                params | {"pin": str(wanted.pin)} | page,
                RevisionProbePage,
            )
        case "packages":
            return await observer.get_packages(
                f"{path}/revisions/{wanted.revision}/packages",
                params | {"pin": str(wanted.pin)} | page,
            )
        case _:
            return await observer.get_diff(
                f"{path}/lockfile-diff",
                params
                | {
                    "before": str(wanted.before),
                    "before_pin": str(wanted.before_pin),
                    "after": str(wanted.after),
                    "after_pin": str(wanted.after_pin),
                }
                | page,
            )


async def _observe(request: web.Request, kind: Kind) -> str:
    context = request.app[CONTEXT]
    project = identifier(request, "project")
    environment = identifier(request, "environment") if kind != "environments" else None
    wanted = _selection(request, kind)
    record = await asyncio.to_thread(context.projects.get, project)
    environments = sorted(set(record.environments()))
    if environment is None:
        return json.dumps(
            {
                "project_id": project,
                "default_environment_id": record.default_environment_id,
                "environment_ids": environments,
                "observed_at": datetime.now(UTC).isoformat(),
            },
            separators=(",", ":"),
        )
    if environment not in environments:
        raise ProjectError(ErrorKind.NOT_FOUND, "no recorded environment in this project")
    if context.worker is None:
        raise ProjectError(ErrorKind.UNSUPPORTED, "provisioning views need a configured worker")
    timeouts = context.timeouts
    long = kind in {"lockfile-diff", "packages"}
    async with asyncio.timeout(timeouts.comparison if long else timeouts.call):
        observer = ProvisionObserver(context.worker, timeouts)
        return (await _fetch(observer, project, environment, wanted)).model_dump_json()


def _handler(kind: Kind) -> Callable[[web.Request], Awaitable[web.StreamResponse]]:
    async def observed(request: web.Request) -> web.Response:
        try:
            return json_answer(await _observe(request, kind))
        except (ProjectError, WorkerAPIError, TimeoutError) as error:
            return errors.to_response(error, extra={"observed_at": datetime.now(UTC).isoformat()})

    return observed


def add_routes(app: web.Application) -> None:
    routes: tuple[tuple[Kind, str], ...] = (
        ("environments", ENVIRONMENTS_PATH),
        ("summary", PATH),
        ("detail", PATH + "/operations/{operation}"),
        ("files", PATH + "/operations/{operation}/files"),
        ("revisions", PATH + "/revisions"),
        ("probes", PATH + "/revisions/{revision}/probes"),
        ("packages", PATH + "/revisions/{revision}/packages"),
        ("lockfile-diff", PATH + "/lockfile-diff"),
    )
    for kind, path in routes:
        app.router.add_get(path, _handler(kind))
