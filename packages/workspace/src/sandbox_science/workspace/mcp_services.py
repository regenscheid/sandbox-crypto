# SPDX-License-Identifier: MIT
"""A bounded, project-scoped view of configured MCP services and past listings.

Only safe service identity and operator-written purpose enter this process from the
launcher. The latest run observation is historical evidence, not a live health check.
No proxy URL, credential, server response body or untrusted failure message is sent to
the browser.

``GET …/mcp-services/settings`` and ``POST …/mcp-services/settings`` with
``{"expected", "disabled", "operation_id"}`` read and change which configured services the
project's user turned off (``connector_settings.py``): narrowing only, since a name the
launcher does not configure is refused. A run does not offer a service turned off, and a
running run's calls to it are refused.

Each service also shows its latest scheduled canary (``canary``), read from the control
store, where the launcher's canary runner records one bounded listing per server every
interval (docs/MCP-CONNECTORS.md, "Canaries"); reading it calls nothing.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime
from itertools import islice
from typing import Final, Literal

from aiohttp import web
from pydantic import Field

from sandbox_science.contracts import Contract, ErrorKind, Identifier
from sandbox_science.contracts.mcp_metadata import McpServiceMetadata
from sandbox_science.orchestrator.connector_settings import ProjectConnectors
from sandbox_science.orchestrator.documents import USER, DocumentStore
from sandbox_science.orchestrator.mcp_canaries import CanaryResult, CanaryStore
from sandbox_science.orchestrator.projects import ProjectError, ProjectStore

from .context import CONTEXT, identifier, json_answer, json_body, no_query
from .mcp_metadata import AvailabilityCheck, MetadataClient
from .requests import document_answer, dumps

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
    source: MetadataClient | None = None


CONFIGURATION: Final = web.AppKey("mcp_service_configuration", Configuration)


class McpObservation(Contract):
    """The newest listing found in the bounded sample, not its present state."""

    session: Identifier
    run: str
    at: datetime
    status: Literal["listed", "unavailable"]
    listed_tools: int | None


class McpCanary(Contract):
    """The newest scheduled canary of a service: a bounded listing from the trusted side,
    not a tool call, and not the user's own check."""

    at: datetime
    status: Literal["listed", "unavailable", "denied", "timeout"]
    tool_count: int | None = Field(ge=0)
    elapsed_ms: int = Field(ge=0)


class McpServiceView(Contract):
    name: Identifier
    purpose: str
    observation: McpObservation | None
    metadata: McpServiceMetadata | None = None
    proxy_checked_at: datetime | None = None
    proxy_status: Literal["reachable", "unknown", "unavailable"] = "unknown"
    availability: AvailabilityCheck | None = None
    canary: McpCanary | None = None


class McpInventory(Contract):
    project_id: Identifier
    policy_revision: str
    services: list[McpServiceView]
    inspected_runs: int
    skipped_runs: int
    observation_scope: str


def add_routes(app: web.Application) -> None:
    app.router.add_get(PATH, services)
    app.router.add_post(PATH + "/{server}/check", check_service)
    app.router.add_get(PATH + "/settings", settings)
    app.router.add_post(PATH + "/settings", change_settings)


def _configured(request: web.Request, project: str) -> tuple[str, ...]:
    config = request.app.get(CONFIGURATION, Configuration())
    return tuple(item.name for item in config.services) if config.project == project else ()


async def settings(request: web.Request) -> web.Response:
    """Which configured services the project's user turned off."""
    project = identifier(request, "project")
    configured = _configured(request, project)
    return await document_answer(
        request,
        lambda store: ProjectConnectors(store).get(project, configured).model_dump(mode="json"),
    )


async def change_settings(request: web.Request) -> web.Response:
    """The user turns configured services off, or back on, at the revision read."""
    project = identifier(request, "project")
    configured = _configured(request, project)
    value = await json_body(request, "connector settings")

    def write(store: DocumentStore) -> dict[str, object]:
        answer, replayed = ProjectConnectors(store).change(
            project, value, USER, configured=configured
        )
        return {**answer.model_dump(mode="json"), "replayed": replayed}

    return await document_answer(request, write, access="write")


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


def _canary(found: CanaryResult | None) -> McpCanary | None:
    if found is None:
        return None
    return McpCanary(
        at=found.at, status=found.status, tool_count=found.tool_count, elapsed_ms=found.elapsed_ms
    )


def _inventory(
    store: ProjectStore, project: str, config: Configuration, canary_store: CanaryStore
) -> McpInventory:
    record = store.get(project)
    configured = config.services if config.project == project else ()
    names = {item.name for item in configured}
    observations, inspected, skipped = _observations(store, project, names) if names else ({}, 0, 0)
    canaries = canary_store.latest(names) if names else {}
    return McpInventory(
        project_id=project,
        policy_revision=record.policy,
        services=[
            McpServiceView(
                name=item.name,
                purpose=item.purpose,
                observation=observations.get(item.name),
                canary=_canary(canaries.get(item.name)),
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
    context = request.app[CONTEXT]
    # The read-only store, as every GET route reads it.
    canaries = CanaryStore.of(context.documents)
    answer = await asyncio.to_thread(_inventory, context.projects, project, config, canaries)
    source = config.source
    if source is not None and config.project == project:

        async def inspect(item: McpServiceView) -> McpServiceView:
            metadata = await source.read(item.name)
            return item.model_copy(
                update={
                    "metadata": metadata or source.last.get(item.name),
                    "proxy_status": "reachable" if metadata else "unavailable",
                    "proxy_checked_at": datetime.now().astimezone(),
                    "availability": source.checks.get(item.name),
                }
            )

        answer = answer.model_copy(
            update={
                "services": list(await asyncio.gather(*(inspect(item) for item in answer.services)))
            }
        )
    return json_answer(dumps(answer.model_dump(mode="json")))


async def check_service(request: web.Request) -> web.Response:
    """An explicit same-origin USER check lists metadata, never executes a tool."""
    project, server = identifier(request, "project"), identifier(request, "server")
    if request.query or await json_body(request, "MCP metadata check") != {}:
        raise ProjectError(ErrorKind.INVALID_REQUEST, "metadata check needs an empty object")
    request.app[CONTEXT].projects.get(project)
    config = request.app.get(CONFIGURATION, Configuration())
    if config.project != project or server not in {item.name for item in config.services}:
        raise ProjectError(ErrorKind.NOT_FOUND, "configured MCP service was not found")
    if config.source is None:
        raise ProjectError(ErrorKind.UNAVAILABLE, "MCP metadata source is not configured")
    answer = await config.source.check(server)
    return json_answer(answer.model_dump_json())
