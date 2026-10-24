# SPDX-License-Identifier: MIT
"""The workspace behind a TLS-terminating reverse proxy: its public origins, and the
proxies whose forwarded scheme and host it believes (docs/WORKSPACE.md, "Behind a
reverse proxy").

Both are opt-in, and without them a request is seen exactly as it arrives: the scheme of
the connection and the ``Host`` header. A *public origin* (``https://host[:port]``) is
what the browser sees in front of the proxy; given one, the server answers only to its
host name, a change must come from exactly that origin, and the session cookie is
``Secure`` and ``__Host-`` prefixed. A *trusted proxy* is a peer address in one of the
networks given; only for a connection from one is ``X-Forwarded-Proto`` or
``X-Forwarded-Host`` read, each at most once and with one value, and a request whose
forwarded headers are ambiguous or malformed is refused rather than guessed at.
``Forwarded`` (RFC 7239) is never read: Traefik, the proxy the Kubernetes bundle renders,
replaces the ``X-Forwarded-*`` headers a client sends but passes ``Forwarded`` through, so
it would carry the client's words. ``X-Forwarded-For`` is never read either: no decision
here depends on the client's address, only on the connection's peer.
"""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass
from typing import Final, cast

from aiohttp import hdrs, web

Network = ipaddress.IPv4Network | ipaddress.IPv6Network

FORWARDED_PROTO: Final = "X-Forwarded-Proto"
FORWARDED_HOST: Final = "X-Forwarded-Host"
_NAME: Final = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)*"
_PUBLIC: Final = re.compile(rf"https://({_NAME})(?::([0-9]{{1,5}}))?")
_HOST: Final = re.compile(rf"(?:{_NAME}|\[[0-9a-f:.]+\])(?::[0-9]{{1,5}})?")


class ForwardedHeaderError(Exception):
    """A trusted proxy's forwarded headers are repeated, list several values or are
    malformed: which hop said what cannot be told, so the request is refused."""


@dataclass(frozen=True)
class PublicOrigin:
    """An ``https://`` origin the browser reaches the server at, through a proxy."""

    host: str
    port: int = 443

    @classmethod
    def parse(cls, text: str) -> PublicOrigin:
        """The origin ``text`` names; ``ValueError`` unless it is exactly
        ``https://<host name>[:port]``: no path, query, fragment or user."""
        found = _PUBLIC.fullmatch(text.lower())
        if found is None or len(found[1]) > 253:
            raise ValueError(
                f"{text!r} is not an https:// origin (https://host or https://host:port)"
            )
        port = int(found[2]) if found[2] is not None else 443
        if not 0 < port <= 65535:
            raise ValueError(f"{text!r} has no valid port")
        return cls(found[1], port)

    @property
    def origin(self) -> str:
        """The origin as a browser serializes it in ``Origin``: no default port."""
        return f"https://{self.host}" if self.port == 443 else f"https://{self.host}:{self.port}"

    @property
    def hosts(self) -> frozenset[str]:
        """The ``Host`` values that name it."""
        named = {f"{self.host}:{self.port}"}
        if self.port == 443:
            named.add(self.host)
        return frozenset(named)


@dataclass(frozen=True)
class Seen:
    """A request's scheme and host as the server believes them, and whether a trusted
    proxy reported the scheme."""

    scheme: str
    host: str
    proxied: bool = False


def parse_network(text: str) -> Network:
    """A trusted proxy network; ``ValueError`` for a malformed one, one with host bits
    set (a typo for another network), or one that is every address."""
    try:
        network = ipaddress.ip_network(text, strict=True)
    except ValueError as exc:
        raise ValueError(f"{text!r} is not a network in CIDR form: {exc}") from None
    if network.prefixlen == 0:
        raise ValueError(f"{text!r} is every address: name the proxy's own networks")
    return network


def peer(request: web.Request) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    """The connection's peer address, never a header's; an IPv4-mapped IPv6 address as
    its IPv4 address."""
    transport = request.transport
    name: object = transport.get_extra_info("peername") if transport is not None else None
    if not isinstance(name, tuple) or not name:
        return None
    first = cast(tuple[object, ...], name)[0]
    if not isinstance(first, str):
        return None
    try:
        address = ipaddress.ip_address(first.partition("%")[0])
    except ValueError:
        return None
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        return address.ipv4_mapped
    return address


def _single(request: web.Request, name: str) -> str | None:
    values = request.headers.getall(name, [])
    if not values:
        return None
    if len(values) > 1 or "," in values[0]:
        raise ForwardedHeaderError(f"{name} lists more than one value")
    return values[0].strip().lower()


@dataclass(frozen=True)
class Exposure:
    """How the server is reached: directly (the default), or through a proxy, at a
    public origin and from trusted networks."""

    public: PublicOrigin | None = None
    trusted: tuple[Network, ...] = ()

    def trusts(self, request: web.Request) -> bool:
        """Whether the connection comes from a trusted proxy."""
        address = peer(request)
        return address is not None and any(address in network for network in self.trusted)

    def seen(self, request: web.Request) -> Seen:
        """The request's scheme and host: the connection's and ``Host``, unless a trusted
        proxy forwarded others. ``ForwardedHeaderError`` for a trusted proxy's ambiguous
        or malformed headers."""
        scheme, host = request.scheme, request.headers.get(hdrs.HOST, "").lower()
        if not self.trusted or not self.trusts(request):
            return Seen(scheme, host)
        proto = _single(request, FORWARDED_PROTO)
        forwarded = _single(request, FORWARDED_HOST)
        if proto is not None and proto not in ("http", "https"):
            raise ForwardedHeaderError(f"{FORWARDED_PROTO} is neither http nor https")
        if forwarded is not None and not _HOST.fullmatch(forwarded):
            raise ForwardedHeaderError(f"{FORWARDED_HOST} is not a host")
        return Seen(proto or scheme, forwarded or host, proxied=proto is not None)

    def answers(self, seen: Seen) -> bool:
        """Whether the server answers a request seen so: always without a public origin;
        with one, only when it names the public host, and, when a trusted proxy reported
        the scheme, only over HTTPS."""
        if self.public is None:
            return True
        return seen.host in self.public.hosts and (not seen.proxied or seen.scheme == "https")

    def origin(self, seen: Seen) -> str:
        """The one ``Origin`` a change may carry: the public origin, or else the server's
        own as seen."""
        return self.public.origin if self.public is not None else f"{seen.scheme}://{seen.host}"

    def secure(self, seen: Seen) -> bool:
        """Whether the session cookie is ``Secure``: with a public origin (always
        ``https://``), or when the request was seen over HTTPS."""
        return self.public is not None or seen.scheme == "https"

    def host_check(self, request: web.Request) -> bool:
        """For the preview origin's guard: whether the server answers this request;
        false for ambiguous forwarded headers, so the check fails closed."""
        try:
            return self.answers(self.seen(request))
        except ForwardedHeaderError:
            return False
