# SPDX-License-Identifier: MIT
"""The user's "Check availability" of an MCP service: one bounded listing by the agent
runtime's own client, asked of the control service (D98).

No trusted service can reach an MCP server: the client, OpenShell's proxy and the
provider's placeholders are in the agent runtime's sandbox. So the workspace asks the
control service, which holds the runtime's connection, with a token of its own
(``POST /mcp-check``); the control service asks the runtime to list the one server, as a
scheduled canary does (``list_mcp_tools``: ``tools/list`` only, never a tool call), and
answers the outcome, never the tools. The result is kept in this process's memory only,
beside the scheduled canaries' rows, which the control store keeps. Neither the token nor
anything the control service says but the status enters a browser response.

The MCP proxy, through which this check listed until it retired (2026-10-26), also served
each server's tools and policy summary (``/metadata/{server}``); the view now reads the
latest listing the runtime reported, from the worker (``mcp_services``).
"""

from __future__ import annotations

import asyncio
import re
from datetime import UTC, datetime
from typing import Final, Literal
from urllib.parse import urlsplit

import aiohttp
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from sandbox_science.contracts import Contract

CHECK_SECONDS: Final = 25.0
"""The control service's bound on one check (20 s) and a margin for the connection."""
_TOKEN: Final = re.compile(r"[\x21-\x7e]{32,256}")
_LOOPBACK: Final = frozenset({"127.0.0.1", "localhost", "::1"})


class AvailabilityCheck(Contract):
    checked_at: datetime
    status: Literal["listed", "unavailable", "denied", "timeout"]
    tool_count: int | None = Field(default=None, ge=0)
    """The tools the runtime's client shows of the listing, those the native rules admit."""


class _Checked(BaseModel):
    """The control service's answer (``ToolsListed``), of which only these are read."""

    model_config = ConfigDict(extra="ignore", frozen=True)
    status: Literal["listed", "unavailable", "denied", "timeout"]
    tool_count: int | None = Field(default=None, ge=0)


class AvailabilityClient:
    """The control service's check route, with the workspace's own token. One check at a
    time; a failure to reach the control service is ``unavailable``."""

    def __init__(self, url: str, token: str) -> None:
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise ValueError("the control service's address is an http or https URL")
        if parts.scheme == "http" and parts.hostname not in _LOOPBACK:
            raise ValueError("the control service is reached over https, or http on loopback")
        if parts.username is not None or parts.query or parts.fragment:
            raise ValueError("the control service's address has no credentials or query")
        if not _TOKEN.fullmatch(token):
            raise ValueError("the check token is one line of 32 to 256 visible characters")
        self.url, self._token = url.rstrip("/"), token
        self.checks: dict[str, AvailabilityCheck] = {}
        self.lock = asyncio.Lock()

    async def check(self, server: str) -> AvailabilityCheck:
        """List ``server`` once through the runtime's client; the outcome, also kept."""
        async with self.lock:
            checked = AvailabilityCheck(
                checked_at=datetime.now(UTC), **(await self._ask(server)).model_dump()
            )
            self.checks[server] = checked
            return checked

    async def _ask(self, server: str) -> _Checked:
        try:
            async with (
                aiohttp.ClientSession(
                    timeout=aiohttp.ClientTimeout(total=CHECK_SECONDS), trust_env=False
                ) as http,
                http.post(
                    self.url + "/mcp-check",
                    json={"server": server},
                    headers={"Authorization": "Bearer " + self._token},
                    allow_redirects=False,
                ) as response,
            ):
                if response.status != 200:
                    return _Checked(status="unavailable")
                raw = await response.content.read(64 * 1024 + 1)
                if len(raw) > 64 * 1024:
                    return _Checked(status="unavailable")
                return _Checked.model_validate_json(raw)
        except (aiohttp.ClientError, TimeoutError, ValidationError, ValueError):
            return _Checked(status="unavailable")
