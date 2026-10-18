# SPDX-License-Identifier: MIT
"""Host-to-proxy inspection. Only an explicit USER check performs tools/list."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Literal
from urllib.parse import quote

import aiohttp
from pydantic import ValidationError

from sandbox_science.contracts import Contract
from sandbox_science.contracts.mcp_metadata import McpServiceMetadata
from sandbox_science.orchestrator.connectors import ConnectorError, ProxyClient, check_proxy


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
                raw = await response.content.read(1024 * 1024 + 1)
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
        """Bounded initialize/list only, through the ordinary policy-enforcing proxy."""
        async with self.lock:
            client = ProxyClient(self.url, self.token, timeout=10)
            status: Literal["listed", "unavailable", "denied", "timeout"] = "listed"
            try:
                async with asyncio.timeout(15):
                    cursor: str | None = None
                    seen: set[str] = set()
                    for _ in range(32):
                        result = await client.request(
                            server,
                            "tools/list",
                            {} if cursor is None else {"cursor": cursor},
                            session="workspace-metadata",
                        )
                        if not isinstance(result.get("tools"), list):
                            status = "unavailable"
                            break
                        following = result.get("nextCursor")
                        if following is None:
                            break
                        if not isinstance(following, str) or following in seen:
                            status = "unavailable"
                            break
                        seen.add(following)
                        cursor = following
                    else:
                        status = "unavailable"
            except TimeoutError:
                status = "timeout"
            except ConnectorError as error:
                status = "denied" if error.kind == "policy_denied" else "unavailable"
            except (aiohttp.ClientError, ValueError):
                status = "unavailable"
            finally:
                await client.close()
            checked = AvailabilityCheck(checked_at=datetime.now(UTC), status=status)
            self.checks[server] = checked
            return checked
