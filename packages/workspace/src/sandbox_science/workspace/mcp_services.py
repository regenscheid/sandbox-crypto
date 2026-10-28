# SPDX-License-Identifier: MIT
"""A bounded, project-scoped view of configured MCP services and past listings.

Only safe service identity and operator-written purpose enter this process from the
launcher. The latest run observation is historical evidence, not a live health check.
No address, credential, server response body or untrusted failure message is sent to
the browser.

Each service's tools are those of the latest complete listing the agent runtime's client
reported (``report_mcp_listing``, D98), which the worker keeps, each tool with its hint,
its class in force and what a call of it by name is decided as under the project's policy
and classification in force (``GET /v1/mcp-listings`` with the workspace's operator
client). ``listing_state`` says when no listing was reported yet (``none``: no run or
canary has listed the server since the worker started) or the worker could not be asked
(``unavailable``). The user's "Check availability" is one listing by the runtime's client,
asked of the control service (``mcp_metadata``). The MCP proxy, whose metadata route the
view read until it retired (2026-10-26), is no source.

``GET …/mcp-services/settings`` and ``POST …/mcp-services/settings`` with ``{"expected",
"disabled", "operation_id"}`` read and change which configured services the project's
user turned off (``connector_settings.py``): narrowing only, since a name the launcher
does not configure is refused. Turning a service back on widens what agents may reach,
and is an admin-only action (identity.py, D104); turning one off is every user's. A run
does not offer a service turned off, and a running run's calls to it are refused.

Each service also shows its latest scheduled canary (``canary``), read from the control
store, where the launcher's canary runner records one bounded listing per server every
interval (docs/MCP-CONNECTORS.md, "Canaries"); reading it calls nothing.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime
from itertools import islice
from typing import Final, Literal, Protocol

from aiohttp import web
from pydantic import Field

from sandbox_science.contracts import Contract, ErrorKind, Identifier
from sandbox_science.contracts.classifications import ReportedListing
from sandbox_science.orchestrator.connector_settings import ProjectConnectors
from sandbox_science.orchestrator.documents import DocumentStore
from sandbox_science.orchestrator.mcp_canaries import CanaryResult, CanaryStore
from sandbox_science.orchestrator.projects import ProjectError, ProjectStore
from sandbox_science.orchestrator.worker_client import WorkerAPIError

from .context import CONTEXT, identifier, json_answer, json_body, no_query
from .identity import author, identity
from .mcp_metadata import AvailabilityCheck, AvailabilityClient
from .requests import document_answer, dumps

_log = logging.getLogger(__name__)

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


class ListingSource(Protocol):
    """The worker client's read of the reported listings (``WorkerClient.mcp_listings``)."""

    async def mcp_listings(self, project_id: str) -> list[ReportedListing]: ...


@dataclass(frozen=True)
class Configuration:
    project: str | None = None
    services: tuple[ServiceConfig, ...] = ()
    checks: AvailabilityClient | None = None
    """The user's availability check, through the control service; none configured, the
    check answers that it is unavailable."""
    listings: ListingSource | None = None
    """The worker's reported listings; none configured, the view shows none."""


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
    listing: ReportedListing | None = None
    """The latest listing the agent runtime's client reported, as the worker keeps it."""
    listing_state: Literal["reported", "none", "unavailable"] = "unavailable"
    """``reported``: ``listing`` is the worker's; ``none``: the worker has none yet, since
    no run or canary listed the server since it started; ``unavailable``: the worker could
    not be asked, or the workspace has no worker."""
    availability: AvailabilityCheck | None = None
    canary: McpCanary | None = None


class McpInventory(Contract):
    project_id: Identifier
    policy_revision: str
    services: list[McpServiceView]
    inspected_runs: int
    skipped_runs: int
    observation_scope: str
    checks: bool = False
    """Whether the user's availability check is configured."""


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
    by, admin = author(request), identity(request).admin

    def write(store: DocumentStore) -> dict[str, object]:
        answer, replayed = ProjectConnectors(store).change(
            project, value, by, configured=configured, widen=admin
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


def canary_view(found: CanaryResult | None) -> McpCanary | None:
    """A service's latest canary as the views show it."""
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
                canary=canary_view(canaries.get(item.name)),
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
    if config.project != project or not answer.services:
        return json_answer(dumps(answer.model_dump(mode="json")))
    reported: dict[str, ReportedListing] | None = None
    if config.listings is not None:
        try:
            found = await config.listings.mcp_listings(project)
            reported = {listing.server: listing for listing in found}
        except (WorkerAPIError, OSError, TimeoutError):
            _log.warning("the worker's reported MCP listings are unavailable", exc_info=True)
    checks = config.checks.checks if config.checks is not None else {}
    answer = answer.model_copy(
        update={
            "checks": config.checks is not None,
            "services": [
                item.model_copy(
                    update={
                        "listing": (reported or {}).get(item.name),
                        "listing_state": "unavailable"
                        if reported is None
                        else "reported"
                        if item.name in reported
                        else "none",
                        "availability": checks.get(item.name),
                    }
                )
                for item in answer.services
            ],
        }
    )
    return json_answer(dumps(answer.model_dump(mode="json")))


async def check_service(request: web.Request) -> web.Response:
    """An explicit same-origin USER check: one listing by the agent runtime's client, asked
    of the control service; it never calls a tool."""
    project, server = identifier(request, "project"), identifier(request, "server")
    if request.query or await json_body(request, "MCP availability check") != {}:
        raise ProjectError(ErrorKind.INVALID_REQUEST, "an availability check is an empty object")
    request.app[CONTEXT].projects.get(project)
    config = request.app.get(CONFIGURATION, Configuration())
    if config.project != project or server not in {item.name for item in config.services}:
        raise ProjectError(ErrorKind.NOT_FOUND, "configured MCP service was not found")
    if config.checks is None:
        raise ProjectError(ErrorKind.UNAVAILABLE, "the MCP availability check is not configured")
    answer = await config.checks.check(server)
    return json_answer(answer.model_dump_json())
