# SPDX-License-Identifier: MIT
"""MCP canaries on the direct path: the host asks the runtime to list (D98, D103).

On the direct path no trusted service can reach an MCP server: the client, OpenShell's
proxy and the provider's placeholders are in the agent runtime's sandbox. So the control
service's canary runner (``mcp_canaries.CanaryRunner``) asks the runtime, over the control
connection it already has, with the host-to-runtime ``list_mcp_tools`` request, as it asks
the runtime for model completions, whose key is also only the runtime's. The runtime
lists the one server's tools through its own ``DirectClient`` with ``list_tools``, bounded
(15 s, 32 pages, ``tools/list`` only: never a tool call), and
answers the outcome, never the tools. The policy middleware sees and records that listing
like any other, drift included. The runner stores the outcome in ``mcp_canaries``.

The runtime's word on a listing is a monitor's, not evidence: a runtime that lies about a
canary hides only an outage, which its other calls would show.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Callable
from dataclasses import asdict
from typing import Final

from pydantic import Field, JsonValue, ValidationError, field_validator

from sandbox_science.contracts.model import Contract, Identifier
from sandbox_science.orchestrator.mcp_canaries import (
    LIST_SECONDS,
    REQUEST_SECONDS,
    SESSION,
    Listing,
    ListingClient,
    list_tools,
)
from sandbox_science.orchestrator.mcp_direct import DirectClient, DirectServer, ListingReporter
from sandbox_science.policy.openshell import ToolAdmission

from .transport import Capability, Channel, TransportError
from .wire import ToolListing, ToolsListed, payload, typed

MARGIN: Final = 5.0
"""Seconds the host allows beyond the runtime's own listing bound, for the connection."""
_HEADER = r"^[A-Za-z0-9!#$%&'*+.^_`|~-]{1,128}$"
_VARIABLE = r"^[A-Za-z_][A-Za-z0-9_]{0,127}$"


class DirectServerSettings(Contract):
    """A server the runtime's direct client reaches: its URL, as the project policy names
    it, and its credential headers, each the environment variable with its placeholder."""

    url: str = Field(pattern=r"^https?://[\x21-\x7e]{1,2040}$")
    credentials: dict[str, str] = Field(default_factory=dict[str, str], max_length=16)
    tools: ToolAdmission = Field(default_factory=ToolAdmission)
    """The tool names the server's compiled endpoint admits (``tool_admission``): the only
    tools a listing shows. None by default: a server whose rules are not given shows none."""

    @field_validator("credentials")
    @classmethod
    def _named(cls, value: dict[str, str]) -> dict[str, str]:
        for header, variable in value.items():
            if not re.fullmatch(_HEADER, header) or not re.fullmatch(_VARIABLE, variable):
                raise ValueError("a credential maps a header name to an environment variable")
        return value

    def server(self) -> DirectServer:
        return DirectServer(self.url, dict(self.credentials), self.tools)


class DirectSettings(Contract):
    """The runtime's direct MCP client (``runtime.json``'s ``mcp``)."""

    project: Identifier
    servers: dict[Identifier, DirectServerSettings] = Field(min_length=1, max_length=64)

    def client(self, reporter: ListingReporter | None) -> DirectClient:
        """A client for canary listings only: no request check, since a listing is never
        held, the canary's request bound, and its complete listings reported for the drift
        check (``report_mcp_listing``), as an attempt's are."""
        return DirectClient(
            {name: spec.server() for name, spec in self.servers.items()},
            project=self.project,
            timeout=REQUEST_SECONDS,
            reporter=reporter,
        )


class RuntimeListings:
    """The runtime's side of ``list_mcp_tools``."""

    def __init__(
        self,
        client: ListingClient | None,
        servers: frozenset[str],
        *,
        seconds: float = LIST_SECONDS,
    ) -> None:
        self.client, self.servers, self.seconds = client, servers, seconds

    async def list(self, value: JsonValue) -> JsonValue:
        asked = typed(ToolListing, value)
        if self.client is None:
            raise TransportError("denied", "this runtime has no direct MCP client")
        if asked.server not in self.servers:
            listing = Listing("unavailable", error_kind="undeclared_server")
        else:
            listing = await list_tools(
                self.client, asked.server, session=SESSION, seconds=self.seconds
            )
        return payload(ToolsListed.model_validate(asdict(listing)))


class RuntimeSource:
    """The control service's ``ListingSource`` on the direct path: each listing asked of the
    connected, handshaken runtime. No runtime is ``unavailable`` (``runtime_unavailable``);
    no answer within the runtime's bound and ``MARGIN`` is ``timeout``."""

    def __init__(
        self, channel: Callable[[], Channel | None], *, seconds: float = LIST_SECONDS + MARGIN
    ) -> None:
        self._channel, self.seconds = channel, seconds

    async def list(self, server: str) -> Listing:
        channel = self._channel()
        if channel is None or channel.closed:
            return Listing("unavailable", error_kind="runtime_unavailable")
        try:
            asked = payload(ToolListing(server=server))
            async with asyncio.timeout(self.seconds):
                value = await channel.request(Capability.LIST_MCP_TOOLS, asked, timeout=None)
            listed = typed(ToolsListed, value)
        except TimeoutError:
            return Listing("timeout", error_kind="timeout")
        except TransportError as error:
            kind = "runtime_refused" if error.kind == "denied" else "runtime_unavailable"
            return Listing("unavailable", error_kind=kind)
        except ValidationError:
            return Listing("unavailable", error_kind="malformed_listing")
        return Listing(listed.status, listed.tool_count, listed.digest, listed.error_kind)
