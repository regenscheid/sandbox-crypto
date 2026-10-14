# SPDX-License-Identifier: MIT
"""The workspace API, version 0: read-only views of the harness's records.

Routes, for GET and HEAD:

- ``/api/v0/projects``: every project, briefly. A damaged record is listed with its error.
- ``/api/v0/projects/{project}``: one project: its base profile and policy revision, its
  sessions with the number of runs each keeps, and its environments with the sessions
  that use each.
- ``/api/v0/projects/{project}/sessions/{session}``: one session and its runs, oldest
  first: each run's name, run ID and start time.
- ``/api/v0/projects/{project}/sessions/{session}/runs/{run}``: one run's record, as
  ``sandbox-science-harness run --transcript`` writes it. A record larger than
  ``MAX_RUN_BYTES`` is refused.

Errors are ``{"error": {"kind", "message"}}`` with the kinds of the worker API. The
records are read from the control store, through a read-only connection, and the answer
encoded, in a thread, so a large run does not hold up other requests.
"""

from __future__ import annotations

import asyncio
import json
import math
from collections.abc import Callable
from dataclasses import dataclass
from typing import Final, cast

from aiohttp import hdrs, web

from sandbox_science.contracts import ErrorKind
from sandbox_science.orchestrator.projects import ProjectError, ProjectRecord, ProjectStore

MAX_RUN_BYTES: Final = 64 * 1024 * 1024
STORE: Final = web.AppKey("store", ProjectStore)
MAX_RUN: Final = web.AppKey("max_run", int)


@dataclass(frozen=True)
class Timeouts:
    """Seconds the workspace waits for its reads and for the worker."""

    call: float = 6.0
    """A kernel, plan, plan-edit or provisioning request as a whole."""
    comparison: float = 10.0
    """A lockfile comparison or a revision's package page as a whole."""
    worker_call: float = 5.0
    """One provisioning observation from the worker."""
    worker_comparison: float = 9.0
    """One comparison or package page from the worker, within its eight-second budget."""


TIMEOUTS: Final = Timeouts()

Json = dict[str, object]


def add_routes(app: web.Application) -> None:
    app.router.add_get("/api/v0/projects", projects)
    app.router.add_get("/api/v0/projects/{project}", project)
    app.router.add_get("/api/v0/projects/{project}/sessions/{session}", session)
    app.router.add_get("/api/v0/projects/{project}/sessions/{session}/runs/{run}", run)


async def projects(request: web.Request) -> web.Response:
    return await _answer(lambda store: {"projects": _projects(store)}, request)


async def project(request: web.Request) -> web.Response:
    found = request.match_info["project"]
    return await _answer(lambda store: {"project": _project(store, found)}, request)


async def session(request: web.Request) -> web.Response:
    found = request.match_info["project"], request.match_info["session"]
    return await _answer(lambda store: _session(store, *found), request)


async def run(request: web.Request) -> web.Response:
    found = request.match_info["project"], request.match_info["session"], request.match_info["run"]
    limit = request.app[MAX_RUN]
    return await _answer(lambda store: _run(store, *found, limit), request)


async def _answer(view: Callable[[ProjectStore], Json], request: web.Request) -> web.Response:
    """The view of the store as JSON; reading and encoding happen in a thread."""
    store = request.app[STORE]
    text = await asyncio.to_thread(lambda: dumps(view(store)))
    return web.Response(
        text=text, content_type="application/json", headers={hdrs.CACHE_CONTROL: "no-store"}
    )


def _projects(store: ProjectStore) -> list[Json]:
    listed: list[Json] = []
    for project_id in store.project_ids():
        try:
            record = store.get(project_id)
        except ProjectError as error:
            listed.append({"project_id": project_id, "error": error.message})
            continue
        listed.append({**_facts(record), "sessions": len(record.sessions)})
    return listed


def _project(store: ProjectStore, project_id: str) -> Json:
    record = store.get(project_id)
    sessions = [
        {**found.model_dump(mode="json"), "runs": len(store.runs(project_id, session_id))}
        for session_id, found in sorted(record.sessions.items())
    ]
    environments = [
        {
            "environment_id": environment_id,
            "default": environment_id == record.default_environment_id,
            "sessions": sorted(
                found.session_id
                for found in record.sessions.values()
                if found.environment_id == environment_id
            ),
        }
        for environment_id in record.environments()
    ]
    return {**_facts(record), "sessions": sessions, "environments": environments}


def _session(store: ProjectStore, project_id: str, session_id: str) -> Json:
    found = store.get(project_id).sessions.get(session_id)
    if found is None:
        raise _no_session(project_id, session_id)
    return {
        "project_id": project_id,
        "session": found.model_dump(mode="json"),
        "runs": [entry.model_dump(mode="json") for entry in store.runs(project_id, session_id)],
    }


def _run(store: ProjectStore, project_id: str, session_id: str, name: str, limit: int) -> Json:
    if session_id not in store.get(project_id).sessions:
        raise _no_session(project_id, session_id)
    return store.run(project_id, session_id, name, max_bytes=limit).model_dump(mode="json")


def _facts(record: ProjectRecord) -> Json:
    return record.model_dump(mode="json", exclude={"sessions"})


def _no_session(project_id: str, session_id: str) -> ProjectError:
    return ProjectError(
        ErrorKind.NOT_FOUND, f"project {project_id!r} has no session {session_id!r}"
    )


def dumps(data: object) -> str:
    """JSON a browser parses: ASCII, with lone surrogates escaped and no NaN or infinity,
    which a record can hold (a float a tool's JSON carried) but JSON cannot."""
    try:
        return json.dumps(data, allow_nan=False)
    except ValueError:
        return json.dumps(_finite(data), allow_nan=False)


def _finite(value: object) -> object:
    if isinstance(value, float) and not math.isfinite(value):
        return str(value)
    if isinstance(value, dict):
        return {key: _finite(item) for key, item in cast(dict[str, object], value).items()}
    if isinstance(value, list):
        return [_finite(item) for item in cast(list[object], value)]
    return value
