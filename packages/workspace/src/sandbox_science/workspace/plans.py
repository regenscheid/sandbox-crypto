# SPDX-License-Identifier: MIT
"""The compact, read-only view of a recorded session's current plan.

``GET /api/v0/projects/{project}/sessions/{session}/plan`` answers the plan's head,
current version and steps, with each step's latest progress but no notes or evidence.
A closed session's plan stays readable. Plans are read through the read-only control
store, so a read never creates the plans' tables.
"""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import UTC, datetime
from typing import Final

from aiohttp import web
from pydantic import AwareDatetime, ConfigDict

from sandbox_science.contracts import Contract, ErrorKind, Identifier
from sandbox_science.orchestrator.documents import DocumentError, DocumentStore
from sandbox_science.orchestrator.projects import ProjectError
from sandbox_science.orchestrator.session_plans import (
    CompactPlan,
    PlanAuthority,
    PlanScope,
    SessionPlans,
)

from .context import CONTEXT, identifier, json_answer, no_query, session_record

PATH: Final = "/api/v0/projects/{project}/sessions/{session}/plan"


class WorkspacePlan(Contract):
    """A session's compact plan; ``plan`` is None when the session has none."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    project_id: Identifier
    session_id: Identifier
    observed_at: AwareDatetime
    session_closed_at: AwareDatetime | None
    plan: CompactPlan | None


def add_routes(app: web.Application) -> None:
    app.router.add_get(PATH, read)


def has_records(documents: DocumentStore, scope: PlanScope) -> bool:
    """Whether the control store keeps any plan record of the session, even without a
    current pointer, which tells a damaged plan from an absent one."""
    if documents.control.missing:
        return False
    with documents.control.read() as tx:
        return (
            tx.sql.execute(
                "SELECT 1 FROM session_plan_records WHERE project=? AND session=? "
                "UNION ALL SELECT 1 FROM session_plan_projections WHERE "
                "project=? AND session=? LIMIT 1",
                (scope.project_id, scope.session_id) * 2,
            ).fetchone()
            is not None
        )


def _compact(documents: DocumentStore, scope: PlanScope, login: str) -> CompactPlan | None:
    try:
        plans = SessionPlans(documents)
    except DocumentError as error:
        if error.kind == "not_found":  # no session has a plan yet
            return None
        raise
    result = plans.compact_current(PlanAuthority(scope=scope, role="user", user_id=login))
    if result is None and has_records(documents, scope):
        raise ProjectError(ErrorKind.UNAVAILABLE, "session plan pointer is missing")
    return result


async def read(request: web.Request) -> web.Response:
    context = request.app[CONTEXT]
    project, session = identifier(request, "project"), identifier(request, "session")
    no_query(request, "session plan read")
    login = context.principal(request)

    def view() -> str:
        found = session_record(context.projects, project, session)
        scope = PlanScope(project_id=project, session_id=session)
        try:
            plan = _compact(context.documents, scope, login)
        except (DocumentError, sqlite3.Error, OSError, ValueError, RecursionError) as error:
            raise ProjectError(ErrorKind.UNAVAILABLE, "session plan is unavailable") from error
        return WorkspacePlan(
            project_id=project,
            session_id=session,
            observed_at=datetime.now(UTC),
            session_closed_at=found.closed_at,
            plan=plan,
        ).model_dump_json()

    async with asyncio.timeout(context.timeouts.call):
        return json_answer(await asyncio.to_thread(view))
