# SPDX-License-Identifier: MIT
"""Host normalization, host patterns and destination classes.

Policy files and requests normalize hosts the same way, so matching is plain
string comparison. Destinations are classified for two checks: the hard floor
(always denied) and the public/non-public split (wildcards and defaults never
grant non-public destinations). See docs/POLICY.md.
"""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass
from enum import IntEnum
from typing import Final

IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address

_LABEL_RE: Final = re.compile(r"[a-z0-9_](?:[a-z0-9_-]{0,61}[a-z0-9_])?")
# inet_aton() and many resolvers read "2130706433", "0x7f.1" or "127.1" as IPv4
# addresses. A host whose last label looks numeric is therefore never treated as
# a DNS name; IPv4 must be written as a canonical dotted quad.
_NUMERIC_LABEL_RE: Final = re.compile(r"[0-9]+|0x[0-9a-f]*")

_FLOOR_NAMES: Final = frozenset(
    {
        "localhost",
        "localhost.localdomain",
        "ip6-localhost",
        "ip6-loopback",
        "host.docker.internal",
        "gateway.docker.internal",
        "host.containers.internal",
        "host.lima.internal",
        "metadata",
        "metadata.google.internal",
        "metadata.goog",
        "metadata.tencentyun.com",
        "instance-data",
        "instance-data.ec2.internal",
        "kubernetes",
        "kubernetes.default",
        "kubernetes.default.svc",
        "kubernetes.default.svc.cluster.local",
    }
)
"""Loopback aliases, container-host names, cloud metadata and Kubernetes API names."""

_METADATA_ADDRESSES: Final[frozenset[IPAddress]] = frozenset(
    {
        ipaddress.ip_address("169.254.169.254"),  # AWS, Azure, GCP, OpenStack and others
        ipaddress.ip_address("169.254.170.2"),  # AWS ECS task metadata
        ipaddress.ip_address("fd00:ec2::254"),  # AWS instance metadata over IPv6
        ipaddress.ip_address("fd00:ec2::23"),  # AWS EKS Pod Identity agent
        ipaddress.ip_address("fd20:ce::254"),  # GCP metadata over IPv6
        ipaddress.ip_address("100.100.100.200"),  # Alibaba Cloud
        ipaddress.ip_address("168.63.129.16"),  # Azure WireServer (a public address)
        ipaddress.ip_address("192.0.0.192"),  # Oracle Cloud
        ipaddress.ip_address("fd00:c1::a9fe:a9fe"),  # Oracle Cloud over IPv6
    }
)
_THIS_NETWORK: Final = ipaddress.IPv4Network("0.0.0.0/8")

NON_PUBLIC_SUFFIXES: Final = (
    "local",
    "localdomain",
    "internal",
    "home.arpa",
    "lan",
    "home",
    "corp",
    "svc",
    "test",
    "example",
    "invalid",
)
"""Special-use and private-use name suffixes that are never public destinations."""

_GLOBAL_UNICAST_V6: Final = ipaddress.IPv6Network("2000::/3")
_NAT64_WELL_KNOWN: Final = ipaddress.IPv6Network("64:ff9b::/96")
_NAT64_LOCAL_USE: Final = ipaddress.IPv6Network("64:ff9b:1::/48")
_IPV4_COMPATIBLE: Final = ipaddress.IPv6Network("::/96")
_IPV4_TRANSLATED: Final = ipaddress.IPv6Network("::ffff:0:0:0/96")


def parse_ip(host: str) -> IPAddress | None:
    """Return the IP address a host string denotes, or None for a name."""
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        return None


def _embedded_ipv4(address: ipaddress.IPv6Address) -> list[ipaddress.IPv4Address]:
    """IPv4 addresses that an IPv6 address carries (mapped, translated, NAT64, 6to4, ...)."""
    found: list[ipaddress.IPv4Address] = []
    if address.ipv4_mapped is not None:
        found.append(address.ipv4_mapped)
    if address.sixtofour is not None:
        found.append(address.sixtofour)
    if address.teredo is not None:
        found.extend(address.teredo)
    if (
        address in _NAT64_WELL_KNOWN
        or address in _NAT64_LOCAL_USE
        or address in _IPV4_TRANSLATED
        or (address in _IPV4_COMPATIBLE and int(address) > 1)
    ):
        found.append(ipaddress.IPv4Address(int(address) & 0xFFFFFFFF))
    return found


def normalize_host(raw: str) -> str:
    """Return the canonical form of a host name or IP literal.

    Names are lowercased and lose one trailing dot; IP addresses take Python's
    canonical text form. Raises ValueError for anything else: non-ASCII names
    (use the IDNA form), numeric forms that are not canonical IP addresses, and
    IPv6 literals with a zone ID or an embedded IPv4 address, which would let
    one destination be written several ways.
    """
    if not raw or not raw.isascii():
        raise ValueError(f"invalid host {raw!r}; use the ASCII (IDNA) form")
    host = raw.lower()
    bracketed = host.startswith("[") or host.endswith("]")
    if bracketed:
        host = host[1:-1] if host.startswith("[") and host.endswith("]") else ""
    address = parse_ip(host)
    if bracketed and not isinstance(address, ipaddress.IPv6Address):
        raise ValueError(f"invalid host {raw!r}; brackets may only enclose an IPv6 address")
    if isinstance(address, ipaddress.IPv6Address):
        if address.scope_id is not None:
            raise ValueError(f"invalid host {raw!r}; IPv6 zone IDs are not allowed")
        if _embedded_ipv4(address):
            raise ValueError(f"invalid host {raw!r}; write the embedded IPv4 address directly")
    if address is not None:
        return str(address)
    if host.endswith("."):
        host = host[:-1]
    if not host or len(host) > 253:
        raise ValueError(f"invalid host {raw!r}")
    labels = host.split(".")
    if _NUMERIC_LABEL_RE.fullmatch(labels[-1]):
        raise ValueError(f"invalid host {raw!r}; write IPv4 addresses as canonical dotted quads")
    if not all(_LABEL_RE.fullmatch(label) for label in labels):
        raise ValueError(f"invalid host name {raw!r}")
    return host


def _embedded_addresses(address: IPAddress) -> list[IPAddress]:
    """The address (without IPv6 scope) plus any IPv4 address embedded in it."""
    if isinstance(address, ipaddress.IPv4Address):
        return [address]
    if address.scope_id is not None:
        address = ipaddress.IPv6Address(int(address))
    return [address, *_embedded_ipv4(address)]


def address_forms(host: str) -> tuple[str, ...]:
    """The forms under which a host can appear in a host list.

    For an IP address: its canonical text plus any IPv4 address it embeds, so
    that a resolved ``::ffff:10.0.0.5`` meets an entry for ``10.0.0.5``. A name
    returns itself.
    """
    address = parse_ip(host)
    if address is None:
        return (host,)
    return tuple(dict.fromkeys(str(candidate) for candidate in _embedded_addresses(address)))


def floor_reason(host: str) -> str | None:
    """Return why the built-in hard floor denies a host, or None.

    Accepts normalized hosts and any IP address text, so the egress proxy can
    re-check the addresses that names resolve to.
    """
    address = parse_ip(host)
    if address is None:
        if host in _FLOOR_NAMES or host.endswith(".localhost"):
            return f"{host} is a loopback, container-host, metadata or cluster administration name"
        return None
    for candidate in _embedded_addresses(address):
        if candidate in _METADATA_ADDRESSES:
            return f"{host} is a cloud metadata endpoint"
        if candidate.is_loopback:
            return f"{host} is a loopback address"
        if candidate.is_link_local:
            return f"{host} is a link-local address"
        if candidate.is_unspecified or candidate in _THIS_NETWORK:
            return f"{host} is an unspecified address"
    return None


def _is_public_address(address: IPAddress) -> bool:
    if isinstance(address, ipaddress.IPv6Address) and address not in _GLOBAL_UNICAST_V6:
        return False  # e.g. deprecated site-local fec0::/10, which Python calls global
    return address.is_global and not (
        address.is_private
        or address.is_multicast
        or address.is_reserved
        or address.is_loopback
        or address.is_link_local
        or address.is_unspecified
    )


def is_public(host: str) -> bool:
    """Whether a host is a public destination (docs/POLICY.md).

    Single-label names count as non-public: resolvers complete them with local
    search domains. Accepts normalized hosts and any IP address text.
    """
    address = parse_ip(host)
    if address is None:
        if "." not in host or floor_reason(host) is not None:
            return False
        return not any(
            host == suffix or host.endswith("." + suffix) for suffix in NON_PUBLIC_SUFFIXES
        )
    return all(_is_public_address(candidate) for candidate in _embedded_addresses(address))


class HostKind(IntEnum):
    """Host pattern kinds, ordered from least to most specific."""

    ANY = 1
    WILDCARD = 2
    EXACT = 3


@dataclass(frozen=True, slots=True)
class HostPattern:
    """A parsed host pattern: ``*``, ``*.suffix`` or an exact host."""

    kind: HostKind
    value: str

    @classmethod
    def parse(cls, raw: str) -> HostPattern:
        """Parse a pattern as written in a policy file. Raises ValueError."""
        if raw == "*":
            return cls(HostKind.ANY, "*")
        if raw.startswith("*."):
            suffix = normalize_host(raw[2:])
            if parse_ip(suffix) is not None:
                raise ValueError(f"invalid host pattern {raw!r}; wildcards apply to names only")
            return cls(HostKind.WILDCARD, suffix)
        if "*" in raw:
            raise ValueError(f"invalid host pattern {raw!r}; use '*' or a leading '*.'")
        return cls(HostKind.EXACT, normalize_host(raw))

    @property
    def labels(self) -> int:
        """The number of labels after ``*.``; zero for other kinds."""
        return self.value.count(".") + 1 if self.kind is HostKind.WILDCARD else 0

    def matches(self, host: str) -> bool:
        """Whether this pattern matches a normalized host."""
        if self.kind is HostKind.ANY:
            return True
        if self.kind is HostKind.EXACT:
            return host == self.value
        return host.endswith("." + self.value) and parse_ip(host) is None

    def __str__(self) -> str:
        if self.kind is HostKind.WILDCARD:
            return f"*.{self.value}"
        return self.value
