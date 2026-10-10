# SPDX-License-Identifier: MIT
"""The hard floor: destinations denied whatever the ceiling and project policy say."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from .hosts import address_forms, floor_reason, normalize_host


@dataclass(frozen=True, slots=True)
class HardFloor:
    """The non-overridable layer.

    Built-in rules (loopback, link-local, unspecified and cloud metadata
    addresses, Kubernetes API names) always apply. A deployment adds its own
    administration endpoints, such as the OpenShell gateway, monitor and policy
    services, through ``admin_hosts``. List their IP addresses as well as their
    names: a name only matches itself, while addresses also catch other names
    that resolve to them.
    """

    admin_hosts: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        normalized = frozenset(normalize_host(host) for host in self.admin_hosts)
        object.__setattr__(self, "admin_hosts", normalized)

    def check_host(self, host: str) -> str | None:
        """Return why the floor denies a host or IP address, or None."""
        reason = floor_reason(host)
        if reason is not None:
            return reason
        if any(form in self.admin_hosts for form in address_forms(host)):
            return f"{host} is a platform administration endpoint"
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
