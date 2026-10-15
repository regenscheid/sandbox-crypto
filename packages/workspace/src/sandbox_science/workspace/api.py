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
from datetime import UTC, datetime
from typing import Final, Self, cast

from aiohttp import hdrs, web

from sandbox_science.contracts import Contract, ErrorKind, Identifier, PolicyRevision
from sandbox_science.orchestrator.projects import (
    ProjectError,
    ProjectRecord,
    ProjectStore,
    RunEntry,
    SessionRecord,
)
from sandbox_science.orchestrator.runstate import WINDOW, RunState
from sandbox_science.orchestrator.runtree import RunTree, RunTrees

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
    environment: float = 2.0
    """The worker's record of an environment, which the chat reads at every poll while a
    message waits for its run: it never holds the chat up for long."""


TIMEOUTS: Final = Timeouts()

Json = dict[str, object]


class SessionView(Contract):
    """A session as the page sees it. Only these fields are copied from the store's
    record, so its lifetime marker, and any field the store adds or renames, stay out."""

    session_id: Identifier
    environment_id: Identifier
    dedicated: bool
    opened_at: datetime
    closed_at: datetime | None

    @classmethod
    def of(cls, found: SessionRecord) -> Self:
        return cls.model_validate(found.model_dump(include=set(cls.model_fields)))


class ProjectFacts(Contract):
    """A project's own facts, copied field by field from the store's record."""

    project_id: Identifier
    profile: Identifier
    policy: PolicyRevision
    default_environment_id: Identifier
    created_at: datetime
    updated_at: datetime

    @classmethod
    def of(cls, record: ProjectRecord) -> Self:
        return cls.model_validate(record.model_dump(include=set(cls.model_fields)))


class ProjectSummary(ProjectFacts):
    """A project in the list, with how many sessions it has."""

    sessions: int


class DamagedProject(Contract):
    """A project whose record cannot be read, listed with the store's error."""

    project_id: str
    error: str


class ProjectListing(Contract):
    projects: list[ProjectSummary | DamagedProject]


class ProjectSession(SessionView):
    runs: int
    """How many runs the session keeps."""


class EnvironmentView(Contract):
    environment_id: Identifier
    default: bool
    sessions: list[Identifier]


class ModelCost(Contract):
    """What the project's model calls cost, as the gateway reported it, in its currency
    (LiteLLM's is USD); null where it reported none. Calls it gave no cost for count for
    nothing: nothing is estimated."""

    last_day: float | None
    """Runs that started in the last 24 hours, and the reviewer's and job watcher's calls
    made in them."""
    total: float | None
    """Every run the project keeps, and every reviewer and job watcher call."""


class ProjectView(ProjectFacts):
    sessions: list[ProjectSession]
    environments: list[EnvironmentView]
    model_cost: ModelCost


class ProjectAnswer(Contract):
    project: ProjectView


class SessionDetail(Contract):
    project_id: Identifier
    session: SessionView
    runs: list[RunEntry]


def add_routes(app: web.Application) -> None:
    app.router.add_get("/api/v0/projects", projects)
    app.router.add_get("/api/v0/projects/{project}", project)
    app.router.add_get("/api/v0/projects/{project}/sessions/{session}", session)
    app.router.add_get("/api/v0/projects/{project}/sessions/{session}/runs/{run}", run)
    app.router.add_get("/api/v0/projects/{project}/sessions/{session}/tree", tree)


async def projects(request: web.Request) -> web.Response:
    return await _answer(lambda store: ProjectListing(projects=_projects(store)), request)


async def project(request: web.Request) -> web.Response:
    found = request.match_info["project"]
    return await _answer(lambda store: ProjectAnswer(project=_project(store, found)), request)


async def session(request: web.Request) -> web.Response:
    found = request.match_info["project"], request.match_info["session"]
    return await _answer(lambda store: _session(store, *found), request)


async def run(request: web.Request) -> web.Response:
    found = request.match_info["project"], request.match_info["session"], request.match_info["run"]
    limit = request.app[MAX_RUN]
    return await _answer(lambda store: _run(store, *found, limit), request)


async def tree(request: web.Request) -> web.Response:
    """The session's run tree: its runs, running ones included, and its delegations
    (PD-42)."""
    found = request.match_info["project"], request.match_info["session"]
    return await _answer(lambda store: _tree(store, *found), request)


async def _answer(
    view: Callable[[ProjectStore], Json | Contract], request: web.Request
) -> web.Response:
    """The view of the store as JSON; reading and encoding happen in a thread."""
    store = request.app[STORE]

    def text() -> str:
        found = view(store)
        return dumps(found.model_dump(mode="json") if isinstance(found, Contract) else found)

    text_ = await asyncio.to_thread(text)
    return web.Response(
        text=text_, content_type="application/json", headers={hdrs.CACHE_CONTROL: "no-store"}
    )


def _projects(store: ProjectStore) -> list[ProjectSummary | DamagedProject]:
    listed: list[ProjectSummary | DamagedProject] = []
    for project_id in store.project_ids():
        try:
            record = store.get(project_id)
        except ProjectError as error:
            listed.append(DamagedProject(project_id=project_id, error=error.message))
            continue
        facts = ProjectFacts.of(record).model_dump()
        listed.append(ProjectSummary(**facts, sessions=len(record.sessions)))
    return listed


def _project(store: ProjectStore, project_id: str) -> ProjectView:
    record = store.get(project_id)
    sessions = [
        ProjectSession(
            **SessionView.of(found).model_dump(), runs=len(store.runs(project_id, session_id))
        )
        for session_id, found in sorted(record.sessions.items())
    ]
    environments = [
        EnvironmentView(
            environment_id=environment_id,
            default=environment_id == record.default_environment_id,
            sessions=sorted(
                found.session_id
                for found in record.sessions.values()
                if found.environment_id == environment_id
            ),
        )
        for environment_id in record.environments()
    ]
    facts = ProjectFacts.of(record).model_dump()
    runs = RunState(store)
    cost = ModelCost(
        last_day=runs.model_cost(project_id, datetime.now(UTC) - WINDOW),
        total=runs.model_cost(project_id),
    )
    return ProjectView(**facts, sessions=sessions, environments=environments, model_cost=cost)


def _session(store: ProjectStore, project_id: str, session_id: str) -> SessionDetail:
    found = store.get(project_id).sessions.get(session_id)
    if found is None:
        raise _no_session(project_id, session_id)
    return SessionDetail(
        project_id=project_id,
        session=SessionView.of(found),
        runs=store.runs(project_id, session_id),
    )


def _tree(store: ProjectStore, project_id: str, session_id: str) -> RunTree:
    if session_id not in store.get(project_id).sessions:
        raise _no_session(project_id, session_id)
    return RunTrees(store).session(project_id, session_id)


def _run(store: ProjectStore, project_id: str, session_id: str, name: str, limit: int) -> Json:
    if session_id not in store.get(project_id).sessions:
        raise _no_session(project_id, session_id)
    return store.run(project_id, session_id, name, max_bytes=limit).model_dump(mode="json")


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
