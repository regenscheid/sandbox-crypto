# SPDX-License-Identifier: MIT
"""The hard floor: destinations denied whatever the ceiling and project policy say."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Annotated, Final

from pydantic import AfterValidator, Field

from .hosts import (
    IPNetwork,
    address_forms,
    address_variants,
    floor_reason,
    normalize_host,
    parse_ip,
    parse_network,
)

MAX_OWNER_ADDRESSES: Final = 16
"""Owner addresses per deployment: each splits the public ranges every public endpoint has."""


def _canonical_network(raw: str) -> str:
    return str(parse_network(raw))


OwnerAddresses = Annotated[
    tuple[Annotated[str, AfterValidator(_canonical_network)], ...],
    Field(max_length=MAX_OWNER_ADDRESSES),
]
"""A settings field listing ``HardFloor.owner_addresses``, each in canonical CIDR form."""


@dataclass(frozen=True, slots=True)
class HardFloor:
    """The non-overridable layer.

    Built-in rules (loopback, link-local, unspecified and cloud metadata
    addresses, Kubernetes API names) always apply. A deployment adds its own
    administration endpoints, such as the OpenShell gateway, monitor and policy
    services, through ``admin_hosts``. List their IP addresses as well as their
    names: a name only matches itself, while addresses also catch other names
    that resolve to them.

    ``owner_addresses`` lists the deployment's own public addresses and
    prefixes (the host's global IPv6 prefix, the site's WAN address, the
    cluster's ingress addresses), as IP addresses or CIDRs. An address within
    one, in any form an address can take, is denied like the built-in ranges,
    and the public address ranges the compiler admits leave them out.
    """

    admin_hosts: frozenset[str] = frozenset()
    owner_addresses: frozenset[str] = frozenset()
    owner_networks: tuple[IPNetwork, ...] = field(init=False, repr=False, compare=False)
    """``owner_addresses`` parsed, in canonical order."""

    def __post_init__(self) -> None:
        normalized = frozenset(normalize_host(host) for host in self.admin_hosts)
        object.__setattr__(self, "admin_hosts", normalized)
        if len(self.owner_addresses) > MAX_OWNER_ADDRESSES:
            raise ValueError(f"at most {MAX_OWNER_ADDRESSES} owner addresses")
        networks = tuple(
            sorted(
                {parse_network(raw) for raw in self.owner_addresses},
                key=lambda network: (network.version, network),
            )
        )
        object.__setattr__(self, "owner_addresses", frozenset(str(n) for n in networks))
        object.__setattr__(self, "owner_networks", networks)

    def check_host(self, host: str) -> str | None:
        """Return why the floor denies a host or IP address, or None."""
        reason = floor_reason(host)
        if reason is not None:
            return reason
        if any(form in self.admin_hosts for form in address_forms(host)):
            return f"{host} is a platform administration endpoint"
        address = parse_ip(host)
        if address is not None and any(
            candidate in network
            for candidate in address_variants(address)
            for network in self.owner_networks
        ):
            return f"{host} is one of the deployment's own addresses"
        return None

    def check_destination(self, host: str, resolved: Sequence[str] = ()) -> str | None:
        """Check a request host and the addresses the egress proxy resolved it to."""
        reason = self.check_host(host)
        if reason is not None:
            return reason
        for address in resolved:
            reason = self.check_host(address)
            if reason is not None:
                return f"{host} resolves to {address}: {reason}"
        return None
