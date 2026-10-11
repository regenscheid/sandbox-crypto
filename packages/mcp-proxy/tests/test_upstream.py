# SPDX-License-Identifier: MIT
"""The upstream client's own guards: resolved addresses and placeholders in bodies."""

import socket
from typing import Any, Protocol

import pytest
from aiohttp.abc import AbstractResolver, ResolveResult

from sandbox_science.mcp_proxy.errors import RefusalError
from sandbox_science.mcp_proxy.upstream import (
    DestinationRefusedError,
    GuardedResolver,
    Target,
    Upstream,
    address_check,
    client_session,
)
from sandbox_science.policy import HardFloor, PoolCeiling, parse_ceiling

pytestmark = pytest.mark.asyncio

CEILING = """\
apiVersion: sandbox-science/v0alpha1
kind: PoolCeiling
network:
  deny_hosts: ["198.51.100.7", "*.blocked.example"]
"""


class Fake(Protocol):
    port: int
    seen: list[Any]


class Tls(Protocol):
    client: Any


class FixedResolver(AbstractResolver):
    """Resolves every name to the given addresses."""

    def __init__(self, *addresses: str) -> None:
        self.addresses = addresses

    async def resolve(
        self, host: str, port: int = 0, family: socket.AddressFamily = socket.AF_INET
    ) -> list[ResolveResult]:
        return [
            {
                "hostname": host,
                "host": address,
                "port": port,
                "family": family,
                "proto": 0,
                "flags": 0,
            }
            for address in self.addresses
        ]

    async def close(self) -> None:
        pass


async def test_the_address_check_applies_the_floor_and_the_ceiling() -> None:
    check = address_check(HardFloor(frozenset({"10.0.0.2"})), parse_ceiling(CEILING))
    assert check("mcp.example.org", ["93.184.215.14"]) is None
    for address in ("127.0.0.1", "169.254.169.254", "::1", "::ffff:127.0.0.1", "10.0.0.2"):
        refused = check("mcp.example.org", [address])
        assert refused is not None
        assert refused[0] == "destination_forbidden", address
    refused = check("mcp.example.org", ["93.184.215.14", "198.51.100.7"])
    assert refused is not None
    assert refused[0] == "pool_ceiling_denied"
    refused = check("mcp.blocked.example", ["93.184.215.14"])
    assert refused is not None
    assert refused[0] == "pool_ceiling_denied"


async def test_the_resolver_refuses_before_any_connection() -> None:
    check = address_check(HardFloor(), PoolCeiling.unrestricted())
    allowed = GuardedResolver(FixedResolver("93.184.215.14"), check)
    [result] = await allowed.resolve("mcp.example.org", 443)
    assert result["host"] == "93.184.215.14"
    # One forbidden address among several refuses the name.
    refused = GuardedResolver(FixedResolver("93.184.215.14", "169.254.169.254"), check)
    with pytest.raises(DestinationRefusedError) as raised:
        await refused.resolve("mcp.example.org", 443)
    assert raised.value.reason_code == "destination_forbidden"


async def test_a_body_with_a_placeholder_is_never_sent(fake: Fake, tls: Tls) -> None:
    resolver = FixedResolver("127.0.0.1")
    upstream = Upstream(client_session(resolver, timeout=5, tls=tls.client))
    target = Target(f"https://mcp.example.test:{fake.port}/mcp", (("Authorization", "Bearer x"),))
    body = (
        b'{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{"x":"openshell:resolve:env:v1"}}'
    )
    try:
        with pytest.raises(RefusalError) as raised:
            await upstream.request(target, body, request_id=1, limit=1024)
        assert raised.value.reason_code == "credential_in_body"
        with pytest.raises(RefusalError):
            await upstream.send(target, {"jsonrpc": "2.0", "method": "openshell:resolve:x"})
    finally:
        await upstream.close()
    assert fake.seen == []
