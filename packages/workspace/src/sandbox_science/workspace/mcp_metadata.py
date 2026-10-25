# SPDX-License-Identifier: MIT
"""Host-to-proxy inspection. In this process only an explicit USER check performs
tools/list; the scheduled canaries (``mcp_canaries``) make the same listing elsewhere."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Literal
from urllib.parse import quote

import aiohttp
from pydantic import ValidationError

from sandbox_science.contracts import Contract
from sandbox_science.contracts.mcp_metadata import McpServiceMetadata
from sandbox_science.orchestrator.connectors import ProxyClient, check_proxy
from sandbox_science.orchestrator.mcp_canaries import list_tools


class AvailabilityCheck(Contract):
    checked_at: datetime
    status: Literal["listed", "unavailable", "denied", "timeout"]


class MetadataClient:
    """Private host credentials never enter a browser response or exception."""

    def __init__(self, url: str, token: str) -> None:
        check_proxy(url, token)
        self.url, self.token = url.rstrip("/"), token
        self.last: dict[str, McpServiceMetadata] = {}
        self.checks: dict[str, AvailabilityCheck] = {}
        self.lock = asyncio.Lock()

    async def read(self, server: str) -> McpServiceMetadata | None:
        try:
            async with (
                aiohttp.ClientSession(
                    timeout=aiohttp.ClientTimeout(total=3), trust_env=False
                ) as http,
                http.get(
                    self.url + "/metadata/" + quote(server, safe=""),
                    headers={"Authorization": "Bearer " + self.token},
                    allow_redirects=False,
                ) as response,
            ):
                if response.status != 200:
                    return None
                try:
                    raw = await response.content.readexactly(1024 * 1024 + 1)
                except asyncio.IncompleteReadError as error:
                    raw = error.partial
                if len(raw) > 1024 * 1024:
                    return None
                found = McpServiceMetadata.model_validate_json(raw)
                if found.server != server:
                    return None
                self.last[server] = found
                return found
        except (aiohttp.ClientError, TimeoutError, ValidationError, ValueError):
            return None

    async def check(self, server: str) -> AvailabilityCheck:
        """Bounded initialize/list only, through the ordinary policy-enforcing proxy: the
        listing the scheduled canaries make too (``mcp_canaries.list_tools``)."""
        async with self.lock:
            client = ProxyClient(self.url, self.token, timeout=10)
            try:
                found = await list_tools(client, server, session="workspace-metadata")
            finally:
                # End the MCP session, so a check never holds one of the caller's places.
                await client.end(server, session="workspace-metadata")
                await client.close()
            checked = AvailabilityCheck(checked_at=datetime.now(UTC), status=found.status)
            self.checks[server] = checked
            return checked
