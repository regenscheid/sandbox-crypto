# SPDX-License-Identifier: MIT
"""A bounded, project-scoped view of configured MCP services and past listings.

Only safe service identity and operator-written purpose enter this process from the
launcher. The latest run observation is historical evidence, not a live health check.
No proxy URL, credential, server response body or untrusted failure message is sent to
the browser.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime
from itertools import islice
from typing import Final, Literal

from aiohttp import web
from pydantic import Field

from sandbox_science.contracts import Contract, Identifier
from sandbox_science.orchestrator.projects import ProjectError, ProjectStore

from .api import dumps
from .context import CONTEXT, identifier, json_answer, no_query

PATH: Final = "/api/v0/projects/{project}/mcp-services"
MAX_SESSIONS: Final = 8
MAX_RUNS_PER_SESSION: Final = 8
MAX_ENTRIES_PER_SESSION: Final = 64
MAX_RECORDS: Final = 24
MAX_RUN_BYTES: Final = 4 * 1024 * 1024


class ServiceConfig(Contract):
    """The explicitly allowed part of a launcher service's configuration."""

    name: Identifier
    purpose: str = Field(max_length=200)


@dataclass(frozen=True)
class Configuration:
    project: str | None = None
    services: tuple[ServiceConfig, ...] = ()


CONFIGURATION: Final = web.AppKey("mcp_service_configuration", Configuration)


class McpObservation(Contract):
    """The newest listing found in the bounded sample, not its present state."""

    session: Identifier
    run: str
    at: datetime
    status: Literal["listed", "unavailable"]
    listed_tools: int | None


class McpServiceView(Contract):
    name: Identifier
    purpose: str
    observation: McpObservation | None


class McpInventory(Contract):
    project_id: Identifier
    policy_revision: str
    services: list[McpServiceView]
    inspected_runs: int
    skipped_runs: int
    observation_scope: str


def add_routes(app: web.Application) -> None:
    app.router.add_get(PATH, services)


def _observations(
    store: ProjectStore, project: str, names: set[str]
) -> tuple[dict[str, McpObservation], int, int]:
    found: dict[str, McpObservation] = {}
    inspected = skipped = 0
    sessions = sorted(
        store.get(project).sessions.values(), key=lambda item: item.opened_at, reverse=True
    )[:MAX_SESSIONS]
    for session in sessions:
        main_seen = 0
        for entry in islice(
            store.iter_runs(project, session.session_id, newest_first=True),
            MAX_ENTRIES_PER_SESSION,
        ):
            if entry.role != "main":
                continue
            main_seen += 1
            if main_seen > MAX_RUNS_PER_SESSION:
                break
            if inspected + skipped >= MAX_RECORDS:
                return found, inspected, skipped
            try:
                run = store.run(project, session.session_id, entry.name, max_bytes=MAX_RUN_BYTES)
            except ProjectError:
                skipped += 1
                continue
            inspected += 1
            summary = run.summaries.get("connectors")
            if not isinstance(summary, dict):
                continue
            counts = summary.get("servers")
            failed = summary.get("unavailable")
            counts = counts if isinstance(counts, dict) else {}
            failed = failed if isinstance(failed, dict) else {}
            for name in names:
                previous = found.get(name)
                if previous is not None and previous.at >= run.ended_at:
                    continue
                count = counts.get(name)
                if isinstance(count, int) and not isinstance(count, bool) and count >= 0:
                    found[name] = McpObservation(
                        session=session.session_id,
                        run=entry.name,
                        at=run.ended_at,
                        status="listed",
                        listed_tools=count,
                    )
                elif name in failed:
                    found[name] = McpObservation(
                        session=session.session_id,
                        run=entry.name,
                        at=run.ended_at,
                        status="unavailable",
                        listed_tools=None,
                    )
    return found, inspected, skipped


def _inventory(store: ProjectStore, project: str, config: Configuration) -> McpInventory:
    record = store.get(project)
    configured = config.services if config.project == project else ()
    names = {item.name for item in configured}
    observations, inspected, skipped = _observations(store, project, names) if names else ({}, 0, 0)
    return McpInventory(
        project_id=project,
        policy_revision=record.policy,
        services=[
            McpServiceView(
                name=item.name, purpose=item.purpose, observation=observations.get(item.name)
            )
            for item in configured
        ],
        inspected_runs=inspected,
        skipped_runs=skipped,
        observation_scope=(
            "At most 24 main runs sampled from 8 recently opened sessions, "
            "examining no more than 64 recent run entries and 8 main runs per "
            "session; older sessions and records over 4 MiB or unreadable are "
            "skipped. This may omit a service's latest run."
        ),
    )


async def services(request: web.Request) -> web.Response:
    """Read one project's safe configuration and bounded historical observations."""
    no_query(request, "MCP service inventory")
    project = identifier(request, "project")
    config = request.app.get(CONFIGURATION, Configuration())
    store = request.app[CONTEXT].projects
    answer = await asyncio.to_thread(_inventory, store, project, config)
    return json_answer(dumps(answer.model_dump(mode="json")))
