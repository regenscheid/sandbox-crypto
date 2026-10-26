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
"""

from __future__ import annotations

from collections.abc import Sequence

from sandbox_science.contracts import ErrorKind
from sandbox_science.contracts.classifications import ListingReport, ReportedTool
from sandbox_science.orchestrator.worker_client import WorkerAPIError, WorkerClient
from sandbox_science.policy.mcp_wire import ListedTool

from .transport import Capability, Channel, TransportError
from .wire import ListingReportReply, ListingReportRequest, payload, typed

REPORT_SECONDS = 30.0
"""How long the runtime waits for the worker's word on one listing."""


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
