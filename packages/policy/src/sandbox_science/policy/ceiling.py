# SPDX-License-Identifier: MIT
"""The pool ceiling: a worker pool's local limits, which projects can only narrow (D49)."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import timedelta
from typing import Literal, cast

from pydantic import Field, model_validator

from .hosts import HostKind, address_forms, is_public
from .model import API_VERSION, HostPatternField, Name, ProjectPolicy, Resources, StrictModel


class CeilingNetwork(StrictModel):
    """Destination limits. Omitting ``allow_hosts`` leaves destinations unrestricted here."""

    allow_hosts: tuple[HostPatternField, ...] | None = None
    deny_hosts: tuple[HostPatternField, ...] = ()


def _reject_explicit_nulls(data: object) -> object:
    """Fail closed on keys that are present but empty.

    In a ceiling, an omitted limit means "unrestricted at this layer". A key
    written without a value (for example ``allow_hosts:`` with every entry
    commented out) must not silently mean the same thing.
    """
    if not isinstance(data, dict):
        return data
    fields = cast("dict[str, object]", data)
    problems = [
        key
        for key in ("mcp_servers", "resources", "network")
        if key in fields and fields[key] is None
    ]
    for section in ("network", "resources"):
        nested = fields.get(section)
        if isinstance(nested, dict):
            entries = cast("dict[str, object]", nested)
            problems += [f"{section}.{key}" for key, value in entries.items() if value is None]
    if problems:
        raise ValueError(
            f"{', '.join(problems)} given without a value; write [] (or a value) to restrict, "
            "or remove the key to leave it unrestricted"
        )
    return fields


class PoolCeiling(StrictModel):
    """A pool's local ceiling, changed only by local user action."""

    _no_explicit_nulls = model_validator(mode="before")(_reject_explicit_nulls)

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

    def check_host(self, host: str, resolved: Sequence[str] = ()) -> str | None:
        """Return why the ceiling denies a host, or None.

        ``deny_hosts`` applies to the host and to every address it resolved to,
        in every form an address can take; ``allow_hosts`` applies to the host
        as requested, and non-public resolved addresses need an exact entry.
        """
        for candidate in (host, *resolved):
            for form in address_forms(candidate):
                for pattern in self.network.deny_hosts:
                    if pattern.matches(form):
                        return f"the pool ceiling denies {candidate} ({pattern})"
        allowed = self.network.allow_hosts
        if allowed is None:
            return None
        public = is_public(host) and all(is_public(address) for address in resolved)
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
        job_wall_time_default=_clamp(
            "job_wall_time_default",
            requested.job_wall_time_default,
            caps.job_wall_time_default,
            notices,
        ),
        job_wall_time_max=_clamp(
            "job_wall_time_max", requested.job_wall_time_max, caps.job_wall_time_max, notices
        ),
        jobs_per_session=_clamp(
            "jobs_per_session", requested.jobs_per_session, caps.jobs_per_session, notices
        ),
        jobs_per_environment=_clamp(
            "jobs_per_environment",
            requested.jobs_per_environment,
            caps.jobs_per_environment,
            notices,
        ),
        model_budget_usd_per_day=_clamp(
            "model_budget_usd_per_day",
            requested.model_budget_usd_per_day,
            caps.model_budget_usd_per_day,
            notices,
        ),
    )
    return effective, tuple(notices)
