# SPDX-License-Identifier: MIT
"""Trusted control endpoint validation and verified TLS contexts."""

from __future__ import annotations

import ipaddress
import re
import ssl
import stat
from pathlib import Path
from urllib.parse import urlsplit

import aiohttp

from sandbox_science.contracts.model import Contract
from sandbox_science.orchestrator.transport_tls import client_tls as client_tls

from .transport import TransportError


class ServerTLS(Contract):
    certificate: Path
    private_key: Path

    def context(self) -> ssl.SSLContext:
        """Load a server identity; private keys follow capability-file permissions."""
        mode = self.private_key.stat().st_mode
        if self.private_key.is_symlink() or not stat.S_ISREG(mode) or mode & 0o077:
            raise ValueError("control TLS key must be a private regular file")
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(self.certificate, self.private_key)
        return context


def bind_address(value: str) -> tuple[str, int]:
    """An explicit IPv4 or bracketed IPv6 bind address and valid nonzero port."""
    host, separator, port = value.rpartition(":")
    if not separator or not port.isascii() or not port.isdigit() or not 1 <= int(port) <= 65535:
        raise ValueError("control listen requires an IP address and port from 1 to 65535")
    address = host[1:-1] if host.startswith("[") and host.endswith("]") else host
    parsed = ipaddress.ip_address(address)
    if parsed.version == 6 and host != f"[{address}]":
        raise ValueError("control IPv6 listen address must be bracketed")
    return str(parsed), int(port)


def loopback(value: str) -> bool:
    return ipaddress.ip_address(bind_address(value)[0]).is_loopback


def control_endpoint(value: str) -> str:
    """An operator-selected HTTP(S) origin and exact capability route, without redirects."""
    parsed = urlsplit(value)
    host = parsed.hostname
    if (
        parsed.scheme not in ("http", "https")
        or not value.startswith(parsed.scheme + "://")
        or not host
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path != "/control"
        or parsed.query
        or parsed.fragment
        or "?" in value
        or "#" in value
        or any(ord(char) <= 32 or ord(char) >= 127 for char in value)
    ):
        raise ValueError("control URL must be an explicit HTTP(S) /control endpoint")
    if ":" not in host and not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?", host):
        raise ValueError("control URL has an invalid host")
    if ":" in host:
        ipaddress.IPv6Address(host)
    if parsed.port == 0:
        raise ValueError("control URL port must be from 1 to 65535")
    return value


def control_trace() -> aiohttp.TraceConfig:
    """Refuse a redirect before an authenticated callback can reach another route."""
    trace = aiohttp.TraceConfig()

    async def redirect(
        session: aiohttp.ClientSession,
        context: object,
        params: aiohttp.TraceRequestRedirectParams,
    ) -> None:
        raise TransportError("denied", "control callback redirect refused")

    trace.on_request_redirect.append(redirect)
    return trace
