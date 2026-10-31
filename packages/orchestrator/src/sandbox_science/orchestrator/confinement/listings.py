# SPDX-License-Identifier: MIT
"""Listings the runtime's direct MCP client read, reported to the trusted side (D98, D103).

OpenShell does not offer the policy middleware an event-stream ``tools/list`` answer whole,
so the middleware lets it pass unread. The agent runtime's ``DirectClient`` reads every
listing it gets: it removes the tools the compiled native rules deny before the model sees
any (``DirectServer.tools``), and sends the server's own complete listing (each tool's name,
digest and hint, and the listing's digest) over ``report_mcp_listing``
(``ControlListingReports``). The host names the project, session and run (the attempt's,
or for the canaries' client the canaries' project) and forwards the report to the worker
(``WorkerListings``), which compares it with the project's pin and records a drift as the
middleware would. The runtime reports; the trusted side decides drift.

The host also takes each listed tool's class from the trusted side, never from the listing
(DA-7): ``WorkerToolClasses`` asks the worker, by each tool's name and the digest the host
computed of what it listed, for the class the project's classification in force gives it.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping, Sequence
from typing import Final, Protocol

from pydantic import ValidationError

from sandbox_science.contracts import SYSTEM_CLOCK, Clock, ErrorKind
from sandbox_science.contracts.classifications import (
    MAX_CLASSED_TOOLS,
    ListingReport,
    ReportedTool,
    ToolAsk,
    ToolClassQuery,
    ToolPin,
)
from sandbox_science.orchestrator.connectors import TrustedClass
from sandbox_science.orchestrator.documents import DocumentStore
from sandbox_science.orchestrator.project_grants import Asked, ProjectGrants, covering
from sandbox_science.orchestrator.worker_client import WorkerAPIError, WorkerClient
from sandbox_science.policy.mcp_wire import ListedTool

from .transport import Capability, Channel, TransportError
from .wire import ListingReportReply, ListingReportRequest, payload, typed

_log = logging.getLogger(__name__)

REPORT_SECONDS = 30.0
"""How long the runtime waits for the worker's word on one listing."""
CLASS_SECONDS: Final = 10.0
"""How long the host waits for the worker's classes of one server's listed tools."""


class ControlListingReports:
    """The runtime's ``ListingReporter``: each complete listing goes to the host over
    ``report_mcp_listing``, for an attempt or (``attempt`` None) for the canaries."""

    def __init__(self, channel: Channel, attempt: str | None) -> None:
        self._channel, self._attempt = channel, attempt

    async def __call__(self, server: str, tools: Sequence[ListedTool], listing: str) -> None:
        asked = ListingReportRequest(
            attempt=self._attempt,
            server=server,
            tools=tuple(
                ReportedTool(name=tool.name, digest=tool.digest, hint=tool.hint.value)
                for tool in tools
            ),
            listing=listing,
        )
        typed(
            ListingReportReply,
            await self._channel.request(
                Capability.REPORT_MCP_LISTING, payload(asked), timeout=REPORT_SECONDS
            ),
        )


class WorkerListings:
    """The host's side: a report, bound to a project, session and run the host names,
    forwarded to the worker with the host's own token."""

    def __init__(self, worker: WorkerClient) -> None:
        self._worker = worker

    async def report(
        self, value: ListingReportRequest, project: str, session: str | None, run: str | None
    ) -> ListingReportReply:
        try:
            answer = await self._worker.report_mcp_listing(
                ListingReport(
                    project_id=project,
                    server=value.server,
                    session=session,
                    run=run,
                    tools=value.tools,
                    listing=value.listing,
                )
            )
        except WorkerAPIError as exc:
            refused = exc.kind in (
                ErrorKind.POLICY_DENIED,
                ErrorKind.NOT_FOUND,
                ErrorKind.INVALID_REQUEST,
                ErrorKind.CONFLICT,  # a replay of an accepted listing, or past the rate
            )
            raise TransportError(
                "denied" if refused else "unavailable", f"the listing was not checked: {exc}"
            ) from None
        return ListingReportReply(drifted=answer.drifted)


async def report(
    sink: WorkerListings | None,
    value: ListingReportRequest,
    *,
    project: str,
    servers: Sequence[str],
    session: str | None = None,
    run: str | None = None,
) -> ListingReportReply:
    """Forward one report for ``project``, of one of ``servers`` only."""
    if sink is None:
        raise TransportError("denied", "this host checks no listings")
    if value.server not in servers:
        raise TransportError("denied", "the server is not one this caller lists")
    return await sink.report(value, project, session, run)


class GrantCheck(Protocol):
    """Whether the user's grants in force cover a call of ``server`` in ``session`` that
    resolves to any of ``asks`` (D97); one that cannot tell says yes."""

    async def __call__(self, session: str, server: str, asks: Sequence[ToolAsk]) -> bool: ...


class ControlGrants:
    """The project's grants in the control store (``project_grants``), read at each
    question, so a grant given or revoked during a run applies to its next call."""

    def __init__(
        self, documents: DocumentStore, project: str, *, clock: Clock = SYSTEM_CLOCK
    ) -> None:
        self._grants, self._project, self._clock = ProjectGrants(documents), project, clock

    async def __call__(self, session: str, server: str, asks: Sequence[ToolAsk]) -> bool:
        try:
            listing = await asyncio.to_thread(self._grants.listing, self._project)
        except Exception:  # an unreadable grant could cover it: the fence then refuses
            _log.exception("the grants of %s cannot be read", self._project)
            return True
        now = self._clock.now()
        asked = [
            Asked("mcp", "agent", session, ask.max_grant, server=server, effect=ask.effect)
            for ask in asks
        ]
        return any(covering(listing.grants, one, now) for one in asked)


class WorkerToolClasses:
    """The host's ``ToolClassSource`` on the direct path: the worker's classes of a
    project's listed tools, by name and digest (``POST /v1/mcp-tool-classes``), every server
    asked at once within one deadline. A worker that refuses or does not answer gives no
    class, and the tools are asked again at their calls. A tool every call of which asks the
    user is marked so only when no grant in force answers such a call: a granted call goes
    out unasked, so a child's fence refuses it under a draft plan like any other (D97,
    D99)."""

    def __init__(
        self,
        worker: WorkerClient,
        project: str,
        grants: GrantCheck,
        *,
        seconds: float = CLASS_SECONDS,
    ) -> None:
        self._worker, self._project, self._grants = worker, project, grants
        self._seconds = seconds

    async def classes(
        self, asked: Mapping[str, Mapping[str, str]], *, session: str
    ) -> Mapping[str, Mapping[str, TrustedClass]]:
        servers = list(asked)
        try:
            async with asyncio.timeout(self._seconds):
                answers = await asyncio.gather(
                    *(self._server(server, asked[server], session) for server in servers)
                )
        except TimeoutError:
            _log.warning("the classes of %s's listed tools are not known: no answer in time",
                         ", ".join(servers))  # fmt: skip
            return {}
        return {
            server: found for server, found in zip(servers, answers, strict=True)
            if found is not None
        }  # fmt: skip

    async def _server(
        self, server: str, tools: Mapping[str, str], session: str
    ) -> dict[str, TrustedClass] | None:
        pins = [ToolPin(name=name, digest=digest) for name, digest in tools.items()]
        found: dict[str, TrustedClass] = {}
        for start in range(0, len(pins), MAX_CLASSED_TOOLS):
            part = pins[start : start + MAX_CLASSED_TOOLS]
            try:
                answer = await self._worker.mcp_tool_classes(
                    ToolClassQuery(project_id=self._project, server=server, tools=tuple(part))
                )
            except (WorkerAPIError, ValidationError) as exc:
                _log.warning("the classes of %s's listed tools are not known: %s", server, exc)
                return None
            if answer.project_id != self._project or answer.server != server:
                _log.warning("the worker answered the classes of another server than %s", server)
                return None
            named = {pin.name for pin in part}
            for tool in answer.tools:
                if tool.name not in named or tool.effect is None or tool.state != "classified":
                    continue
                escalates = tool.escalates and not await self._grants(session, server, tool.asks)
                found[tool.name] = TrustedClass(tool.effect, escalates)
        return found
