# SPDX-License-Identifier: MIT
"""The pool ceiling: a worker pool's local limits, which projects can only narrow (D49)."""

from __future__ import annotations

from datetime import timedelta
from typing import Literal

from pydantic import Field

from .hosts import HostKind, is_public
from .model import API_VERSION, HostPatternField, Name, ProjectPolicy, Resources, StrictModel


class CeilingNetwork(StrictModel):
    """Destination limits. Omitting ``allow_hosts`` leaves destinations unrestricted here."""

    allow_hosts: tuple[HostPatternField, ...] | None = None
    deny_hosts: tuple[HostPatternField, ...] = ()


class PoolCeiling(StrictModel):
    """A pool's local ceiling, changed only by local user action."""

    api_version: Literal["sandbox-science/v0alpha1"] = Field(
        default=API_VERSION, alias="apiVersion"
    )
    kind: Literal["PoolCeiling"] = "PoolCeiling"
    resources: Resources = Resources()
    network: CeilingNetwork = CeilingNetwork()
    mcp_servers: tuple[Name, ...] | None = None

    @classmethod
    def unrestricted(cls) -> PoolCeiling:
        """A ceiling that adds no limits; callers must ask for it explicitly."""
        return cls()

    def check_host(self, host: str) -> str | None:
        """Return why the ceiling denies a normalized host, or None."""
        for pattern in self.network.deny_hosts:
            if pattern.matches(host):
                return f"the pool ceiling denies {host} ({pattern})"
        allowed = self.network.allow_hosts
        if allowed is None:
            return None
        public = is_public(host)
        for pattern in allowed:
            if pattern.matches(host) and (public or pattern.kind is HostKind.EXACT):
                return None
        return f"{host} is outside the pool ceiling's allow_hosts"

    def check_mcp_server(self, server: str) -> str | None:
        """Return why the ceiling denies an MCP server, or None."""
        if self.mcp_servers is None or server in self.mcp_servers:
            return None
        return f"the pool ceiling does not permit MCP server {server!r}"


def _clamp[T: (int, float, timedelta)](
    name: str, requested: T | None, cap: T | None, notices: list[str]
) -> T | None:
    if cap is None:
        return requested
    if requested is None:
        return cap
    if requested > cap:
        notices.append(f"resources.{name} reduced from {requested} to the pool ceiling's {cap}")
        return cap
    return requested


def effective_resources(
    policy: ProjectPolicy, ceiling: PoolCeiling
) -> tuple[Resources, tuple[str, ...]]:
    """Combine a project's resource requests with the pool's caps.

    Returns the effective limits and a notice for every request above a cap. A
    request the project leaves unset takes the cap.
    """
    requested = policy.resources
    caps = ceiling.resources
    notices: list[str] = []
    effective = Resources(
        cpu=_clamp("cpu", requested.cpu, caps.cpu, notices),
        memory=_clamp("memory", requested.memory, caps.memory, notices),
        pids=_clamp("pids", requested.pids, caps.pids, notices),
        workspace_disk=_clamp(
            "workspace_disk", requested.workspace_disk, caps.workspace_disk, notices
        ),
        wall_time=_clamp("wall_time", requested.wall_time, caps.wall_time, notices),
        model_budget_usd_per_day=_clamp(
            "model_budget_usd_per_day",
            requested.model_budget_usd_per_day,
            caps.model_budget_usd_per_day,
            notices,
        ),
    )
    return effective, tuple(notices)
