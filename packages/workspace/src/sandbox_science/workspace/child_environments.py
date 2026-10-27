# SPDX-License-Identifier: MIT
"""Read-only views of an open session's child environments (docs/specs/child-environments.md,
CE-8, CE-11): the environments a delegated run works in when its project gives it one of
its own.

``GET /api/v0/projects/{project}/sessions/{session}/child-environments`` lists the
session's child environments as the worker keeps them, ended ones included, each with
the delegation it was made for: its kind and brief, whether it runs, awaits its
parent's review (PD-46) or has closed, the hand-back's account, its retention, and, for
one whose task has settled, when the next idle sweep may release it (``child_idle_max``,
or ``child_orphan_idle``).

``GET …/child-environments/{environment}/kernels`` is the kernel panel's observation of
one of them that is ready, as ``…/kernels`` is the session environment's.

The environment the worker is asked about comes from the project record, and a child
environment's ID is checked with the worker against that session before its kernels
are read; no browser input selects a worker or another session's environment. Nothing
here changes an environment: releasing one is the orchestrator's (CE-4).
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from typing import Final, Literal

from aiohttp import web
from pydantic import Field, JsonValue

from sandbox_science.contracts import Contract, ErrorKind, ExecutionContext, Identifier
from sandbox_science.contracts.child_environments import (
    ChildEnvironment,
    ChildFailure,
    ChildPhase,
    is_child_environment,
)
from sandbox_science.contracts.worker import UncollectedFile
from sandbox_science.orchestrator.delegation.records import (
    Delegation,
    DelegationRecords,
    DelegationSettings,
    Ended,
    Review,
    ReviewReason,
)
from sandbox_science.orchestrator.projects import ProjectError
from sandbox_science.orchestrator.worker_client import WorkerClient

from .context import CONTEXT, WorkspaceContext, identifier, json_answer, no_query, session_record
from .kernels import observe

PATH: Final = "/api/v0/projects/{project}/sessions/{session}/child-environments"
LEFT_SHOWN: Final = 20
"""The files a hand-back left out that a view lists; ``left_count`` says how many."""
LIVE: Final = frozenset({"creating", "preparing", "ready", "collecting"})

Use = Literal["preparing", "running", "awaiting_review", "idle", "unclaimed", "ended"]
"""What holds a child environment now: being made; its delegation's run; its returned
task, until the parent accepts it (PD-46); nothing, its task settled, until the idle
sweep; nothing, no delegation names it (admitted, never written); or it has ended,
released or retained."""


class HandBackView(Contract):
    """A hand-back's account: what came back into ``target``, and what did not."""

    target: str
    complete: bool
    copied: int
    retained: int
    """Files named by their artifact revision instead of copied (CE-6)."""
    left: tuple[UncollectedFile, ...] = Field(max_length=LEFT_SHOWN)
    left_count: int


class DelegationView(Contract):
    """The delegation a child environment holds: the one it was made for, or a Resume's
    continuation placed in it (CE-12), the latest."""

    delegation_id: Identifier
    run: str
    kind: Literal["check", "task"]
    brief: dict[str, JsonValue]
    ended: Ended | None
    ended_at: datetime | None
    review: Review | None
    review_reason: ReviewReason | None


class ChildEnvironmentView(Contract):
    """One child environment, as the environment views and the Compute tab show it."""

    kind: Literal["child"] = "child"
    environment_id: Identifier
    delegation_id: Identifier
    profile: str
    phase: ChildPhase
    failure: ChildFailure | None
    message: str | None
    """The worker's words on why it failed or was kept; may quote file paths."""
    isolation_tier: int | None
    created_at: datetime
    ready_by: datetime
    ready_at: datetime | None
    expires_at: datetime
    retained_until: datetime | None
    hand_back: HandBackView | None
    delegation: DelegationView | None
    use: Use
    idle_release_after: datetime | None
    """For one whose task has settled: from when the next main run's idle sweep releases
    it (CE-4)."""


class ChildEnvironmentListing(Contract):
    """An open session's child environments, oldest first."""

    project_id: Identifier
    session_id: Identifier
    environments: tuple[ChildEnvironmentView, ...]


def add_routes(app: web.Application) -> None:
    app.router.add_get(PATH, listed)
    app.router.add_get(PATH + "/{environment}/kernels", kernels)


def _scope(request: web.Request) -> tuple[WorkspaceContext, WorkerClient, ExecutionContext]:
    """The open session's own context, from its record, and the workspace's worker."""
    context = request.app[CONTEXT]
    project, session = identifier(request, "project"), identifier(request, "session")
    found = session_record(context.projects, project, session)
    if found.closed_at is not None:
        raise ProjectError(ErrorKind.NOT_FOUND, "no open session in this project")
    if context.worker is None:
        raise ProjectError(ErrorKind.UNSUPPORTED, "child environments need a configured worker")
    return (
        context,
        context.worker,
        ExecutionContext(
            project_id=project, session_id=session, environment_id=found.environment_id
        ),
    )


async def listed(request: web.Request) -> web.Response:
    no_query(request, "child environment listing")
    context, worker, scope = await asyncio.to_thread(_scope, request)
    async with asyncio.timeout(context.timeouts.call):
        found = (await worker.child_environments(scope)).environments
    views = await asyncio.to_thread(_views, context, scope, found)
    answer = ChildEnvironmentListing(
        project_id=scope.project_id, session_id=scope.session_id, environments=views
    )
    return json_answer(answer.model_dump_json())


async def kernels(request: web.Request) -> web.Response:
    """The kernels of one of the session's child environments that is ready."""
    no_query(request, "kernel observation")
    environment = identifier(request, "environment")
    if not is_child_environment(environment):
        raise ProjectError(ErrorKind.NOT_FOUND, "no such child environment in this session")
    context, worker, scope = await asyncio.to_thread(_scope, request)
    async with asyncio.timeout(context.timeouts.call):
        # The worker answers only the session's own: another's is not found.
        found = await worker.child_environment(environment, scope)
    if found.session_environment != scope.environment_id or found.phase != "ready":
        raise ProjectError(ErrorKind.NOT_FOUND, "the child environment is not ready")
    child = scope.model_copy(update={"environment_id": environment})
    return await observe(context, child)


def _views(
    context: WorkspaceContext, scope: ExecutionContext, found: tuple[ChildEnvironment, ...]
) -> tuple[ChildEnvironmentView, ...]:
    records = DelegationRecords(context.documents)
    settings = records.settings(scope.project_id)
    return tuple(
        view(
            environment,
            records.by_environment(
                scope.project_id,
                scope.session_id,
                environment.environment_id,
                environment.delegation_id,
            ),
            settings,
        )
        for environment in found
        if environment.session_environment == scope.environment_id
    )


def view(
    found: ChildEnvironment, holders: list[Delegation], settings: DelegationSettings
) -> ChildEnvironmentView:
    """A child environment with the delegations that name it (``by_environment``)."""
    holder = max(holders, key=lambda d: (d.started_at, d.delegation_id), default=None)
    use: Use
    idle: datetime | None = None
    if found.phase not in LIVE:
        use = "ended"
    elif found.phase in ("creating", "preparing"):
        use = "preparing"
    elif any(d.ended is None for d in holders):
        use = "running"
    elif any(d.review == "awaiting" for d in holders):
        use = "awaiting_review"
    elif holder is None:
        use = "unclaimed"
    else:
        use = "idle"
        ends = [d.ended_at for d in holders if d.ended_at is not None]
        if ends:
            bound = settings.child_idle_max or settings.child_orphan_idle
            idle = max(ends) + timedelta(seconds=bound)
    back = found.hand_back
    return ChildEnvironmentView(
        environment_id=found.environment_id,
        delegation_id=found.delegation_id,
        profile=found.profile,
        phase=found.phase,
        failure=found.failure,
        message=found.message,
        isolation_tier=found.isolation_tier,
        created_at=found.created_at,
        ready_by=found.ready_by,
        ready_at=found.ready_at,
        expires_at=found.expires_at,
        retained_until=found.retained_until,
        hand_back=None
        if back is None
        else HandBackView(
            target=back.target,
            complete=back.complete,
            copied=len(back.collected),
            retained=len(back.retained),
            left=back.not_collected[:LEFT_SHOWN],
            left_count=len(back.not_collected),
        ),
        delegation=None
        if holder is None
        else DelegationView(
            delegation_id=holder.delegation_id,
            run=holder.run,
            kind=holder.kind,
            brief=holder.brief,
            ended=holder.ended,
            ended_at=holder.ended_at,
            review=holder.review,
            review_reason=holder.review_reason,
        ),
        use=use,
        idle_release_after=idle,
    )
