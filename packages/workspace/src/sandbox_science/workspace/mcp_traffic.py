# SPDX-License-Identifier: MIT
"""Inspected MCP traffic for the run page and the chat's tool rows (docs/WORKSPACE.md).

``GET /api/v0/projects/{project}/mcp-traffic?session=S&run=R&tool=T&after=N`` pages
through the project's MCP calls on the direct path (D98) as the policy middleware
recorded them: each call's arguments and decision, its answer's excerpt, the patterns
found in it and what inspection did with it (docs/POLICY-MIDDLEWARE.md, "MCP traffic
inspection"). The worker reads them for the workspace's operator client
(``GET /v1/mcp-traffic``). Everything in them was written by the agent or a server and
redacted by the middleware: the browser shows it as text through ``visible()``. When the
worker is not configured, cannot be reached or refuses, ``traffic`` is null with a short
reason; the worker's own error text is not passed on. The route reads and never writes.
"""

from __future__ import annotations

import asyncio
import re
from typing import Final, Protocol, cast

from aiohttp import web

from sandbox_science.contracts import Contract, ErrorKind, Identifier
from sandbox_science.contracts.mcp_traffic import WorkerMcpTraffic
from sandbox_science.orchestrator.projects import ProjectError
from sandbox_science.orchestrator.worker_client import WorkerAPIError, WorkerClient

from .context import CONTEXT, identifier, json_answer
from .requests import dumps

ROUTE: Final = "/api/v0/projects/{project}/mcp-traffic"
_QUERY: Final = frozenset({"session", "run", "tool", "after"})
_ID: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
_TOOL: Final = re.compile(r"[A-Za-z0-9_./-]{1,256}")


class TrafficSource(Protocol):
    """The worker client's traffic read, as the workspace's operator client has it."""

    async def mcp_traffic(
        self,
        project_id: str,
        *,
        session: str | None = None,
        run: str | None = None,
        tool: str | None = None,
        after: int = 0,
    ) -> WorkerMcpTraffic: ...


SOURCE: Final = web.AppKey("mcp_traffic_source", TrafficSource)


class ProjectMcpTraffic(Contract):
    """``GET …/mcp-traffic``: a page of inspected calls, or why there is none."""

    project_id: Identifier
    traffic: WorkerMcpTraffic | None
    unavailable: str | None = None


def configure(app: web.Application, worker: WorkerClient | None) -> None:
    if worker is not None:
        app[SOURCE] = cast(TrafficSource, worker)


def add_routes(app: web.Application) -> None:
    app.router.add_get(ROUTE, traffic)


def _query(request: web.Request) -> tuple[str | None, str | None, str | None, int]:
    query = request.query
    if set(query) - _QUERY or len(query) != len(set(query)):
        raise ProjectError(
            ErrorKind.INVALID_REQUEST, "the traffic takes a session, run, tool and after"
        )
    session, run, tool = query.get("session"), query.get("run"), query.get("tool")
    for value in (session, run):
        if value is not None and not _ID.fullmatch(value):
            raise ProjectError(ErrorKind.INVALID_REQUEST, "invalid session or run")
    if tool is not None and not _TOOL.fullmatch(tool):
        raise ProjectError(ErrorKind.INVALID_REQUEST, "invalid tool name")
    after = query.get("after", "0")
    if not after.isascii() or not after.isdigit() or len(after) > 18:
        raise ProjectError(ErrorKind.INVALID_REQUEST, "after is a record number")
    return session, run, tool, int(after)


async def _worker(
    request: web.Request, project: str, query: tuple[str | None, str | None, str | None, int]
) -> tuple[WorkerMcpTraffic | None, str | None]:
    source = request.app.get(SOURCE)
    if source is None:
        return None, "no worker is configured"
    session, run, tool, after = query
    try:
        async with asyncio.timeout(request.app[CONTEXT].timeouts.call):
            found = await source.mcp_traffic(
                project, session=session, run=run, tool=tool, after=after
            )
    except TimeoutError:
        return None, "the worker did not answer in time"
    except WorkerAPIError as exc:
        if exc.status == 404:
            return None, "the worker has no MCP traffic (an older worker)"
        if exc.kind is ErrorKind.UNAVAILABLE and exc.status is None:
            return None, "the worker cannot be reached"
        return None, f"the worker refused ({exc.kind.value})"
    return found, found.unavailable


async def traffic(request: web.Request) -> web.Response:
    """A page of the project's inspected MCP calls, oldest first."""
    project = identifier(request, "project")
    query = _query(request)
    await asyncio.to_thread(request.app[CONTEXT].projects.get, project)
    found, unavailable = await _worker(request, project, query)
    answer = ProjectMcpTraffic(project_id=project, traffic=found, unavailable=unavailable)
    return json_answer(dumps(answer.model_dump(mode="json")))
