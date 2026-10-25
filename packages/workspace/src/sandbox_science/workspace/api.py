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

from datetime import UTC, datetime
from typing import Self

from aiohttp import web

from sandbox_science.contracts import Contract, ErrorKind, Identifier, PolicyRevision
from sandbox_science.orchestrator.accounting import UsageAccount, account
from sandbox_science.orchestrator.projects import (
    ProjectError,
    ProjectRecord,
    ProjectStore,
    RunEntry,
    SessionRecord,
)
from sandbox_science.orchestrator.runstate import WINDOW, CostRole, RunState
from sandbox_science.orchestrator.runtree import RunTree, RunTrees

from . import agent_messages
from .context import CONTEXT
from .requests import json_response

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


class RoleCost(Contract):
    """What one role's model calls cost: the main agent's runs, delegated runs, the
    reviewer's, the job watcher's (lab 40, B5), the skills' embeddings or the import's
    model pass over a pack's catalog."""

    role: CostRole
    last_day: float | None
    """Its calls in the last 24 hours; null when the gateway priced none of them."""
    total: float
    """All its calls the project keeps."""


class ModelCost(Contract):
    """What the project's model calls cost, in the gateway's currency (LiteLLM's is USD):
    as the gateway reported it, or priced from its model information when it reported
    none (lab 43, B2); null where neither gave a cost. Calls without one count for
    nothing here: nothing is estimated."""

    last_day: float | None
    """Runs that started in the last 24 hours, and the reviewer's and job watcher's calls
    made in them."""
    total: float | None
    """Every run the project keeps, and every reviewer and job watcher call."""
    by_role: list[RoleCost]
    """The same by role, for each role the gateway priced a call of: main, delegated,
    reviewer, watcher, in that order."""
    model_info_last_day: float | None = None
    """The part of ``last_day`` priced from the gateway's model information; null when
    none was."""
    model_info_total: float | None = None
    """The part of ``total`` priced from the gateway's model information."""


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
    app.router.add_get("/api/v0/projects/{project}/usage", usage)
    app.router.add_get("/api/v0/projects/{project}/sessions/{session}", session)
    app.router.add_get("/api/v0/projects/{project}/sessions/{session}/runs/{run}", run)
    app.router.add_get("/api/v0/projects/{project}/sessions/{session}/tree", tree)
    agent_messages.add_routes(app)


async def projects(request: web.Request) -> web.Response:
    return await json_response(
        request, lambda: ProjectListing(projects=_projects(request.app[CONTEXT].projects))
    )


async def project(request: web.Request) -> web.Response:
    found = request.match_info["project"]
    return await json_response(
        request, lambda: ProjectAnswer(project=_project(request.app[CONTEXT].projects, found))
    )


async def usage(request: web.Request) -> web.Response:
    """The project's recorded spend and tokens by session, run and role, its optional
    local limit and its cost notices (D102). Accounting only."""
    found = request.match_info["project"]
    store = request.app[CONTEXT].projects

    def read() -> UsageAccount:
        store.get(found)
        return account(RunState(store), found)

    return await json_response(request, read)


async def session(request: web.Request) -> web.Response:
    found = request.match_info["project"], request.match_info["session"]
    return await json_response(request, lambda: _session(request.app[CONTEXT].projects, *found))


async def run(request: web.Request) -> web.Response:
    found = request.match_info["project"], request.match_info["session"], request.match_info["run"]
    limit = request.app[CONTEXT].max_run_bytes
    return await json_response(request, lambda: _run(request.app[CONTEXT].projects, *found, limit))


async def tree(request: web.Request) -> web.Response:
    """The session's run tree: its runs, running ones included, and its delegations
    (PD-42)."""
    found = request.match_info["project"], request.match_info["session"]
    return await json_response(request, lambda: _tree(request.app[CONTEXT].projects, *found))


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
    cost = model_cost(store, project_id)
    return ProjectView(**facts, sessions=sessions, environments=environments, model_cost=cost)


def model_cost(store: ProjectStore, project_id: str) -> ModelCost:
    """What the project's model calls cost in the last 24 hours and in all, as the project
    page shows it (and the Monitor view, D103)."""
    runs = RunState(store)
    day = runs.model_costs(project_id, datetime.now(UTC) - WINDOW)
    every = runs.model_costs(project_id)
    return ModelCost(
        last_day=sum(day.values()) if day else None,
        total=sum(every.values()) if every else None,
        by_role=[
            RoleCost(role=role, last_day=day.get(role), total=total)
            for role, total in every.items()
        ],
        model_info_last_day=runs.model_info_cost(project_id, datetime.now(UTC) - WINDOW),
        model_info_total=runs.model_info_cost(project_id),
    )


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
