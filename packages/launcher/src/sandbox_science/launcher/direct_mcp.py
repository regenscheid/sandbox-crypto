# SPDX-License-Identifier: MIT
"""The agent runtime sandbox's direct MCP egress (D98), compiled from the project policy.

With ``[mcp_direct]`` (or the Kubernetes bundle's ``[mcp_direct]``) the agent's connector
calls leave from the runtime's own sandbox, through OpenShell's proxy (D98; the MCP proxy
retired on 2026-10-26, docs/MCP-CONNECTORS.md). Beside its platform services
(``compile_orchestrator``), the sandbox then gets (``compile_agent_mcp``):

- one inspected ``protocol: mcp`` endpoint per configured server, whose native rules allow
  and deny tools by name, bound to its provider's credentials, and admitting only the
  runtime's interpreter;
- the policy middleware, registered under the targets file's name, for those servers'
  hosts, with the project and the caller ``agent-runtime`` in its config: it decides every
  request the native rules admit, holds escalated calls and records each one.

The runtime's client gets, for each server, the tool names that same compiled endpoint
admits (``tool_admission``), so its listings show the model no tool OpenShell refuses by
name. That list only narrows what the model sees: OpenShell enforces the compiled rules
whatever the runtime shows or asks, so a runtime that widened it gains nothing.

A configured server must be one of the project policy's, at the policy's URL, and one the
floor and the pool ceiling let the compiler admit; anything else is refused before a
sandbox is made. Only the configured servers' endpoints are kept. The fence admits tools
by the catalog's classification: a project's accepted revision of a classification
(``PolicyStore.classification``) is not read here.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from sandbox_science.policy import load_ceiling, load_policy
from sandbox_science.policy.errors import PolicyLoadError
from sandbox_science.policy.openshell import (
    AgentMcpTarget,
    CompileError,
    Finding,
    MiddlewareSelector,
    NetworkPolicy,
    OpenShellPolicy,
    Severity,
    compile_agent_mcp,
    load_targets,
    tool_admission,
)

from .config import LauncherError, McpDirectServer

CALLER = "agent-runtime"
"""The name the policy middleware's records and permission requests give the runtime."""


@dataclass(frozen=True)
class DirectEgress:
    """The compiled egress: the network policies and middleware to merge into the runtime
    sandbox's policy, and the runtime client's servers (``runtime.json``'s ``mcp``)."""

    project: str
    fragment: OpenShellPolicy
    servers: dict[str, dict[str, Any]]
    findings: tuple[Finding, ...]

    def runtime(self) -> dict[str, Any]:
        """``runtime.json``'s ``mcp``: the project and each server's URL, credential
        headers and admitted tools."""
        return {"project": self.project, "servers": self.servers}

    def warnings(self) -> list[str]:
        return [
            f"{finding.subject}: {finding.message}"
            for finding in self.findings
            if finding.severity is not Severity.INFO
        ]


def compile_direct(
    *,
    policy: Path,
    ceiling: Path,
    targets: Path,
    project: str,
    servers: Mapping[str, McpDirectServer],
    python: str,
) -> DirectEgress:
    """Compile the runtime sandbox's egress to ``servers`` for ``project``'s ``policy``."""
    try:
        loaded = load_policy(policy)
        pool = load_ceiling(ceiling)
        middleware = load_targets(targets).sandbox.middleware
    except (OSError, PolicyLoadError) as exc:
        raise LauncherError(f"the direct MCP path cannot read its policy files: {exc}") from None
    declared = loaded.policy.mcp.servers
    for name, server in servers.items():
        if name not in declared:
            raise LauncherError(
                f"mcp_direct.servers.{name} is not one of the project policy's MCP servers"
            )
        if server.url is not None and server.url != declared[name].url:
            raise LauncherError(
                f"mcp_direct.servers.{name}.url differs from the project policy's URL for it"
            )
    target = AgentMcpTarget(
        middleware=middleware, project=project, caller=CALLER, binaries=(python + "*",)
    )
    try:
        compiled = compile_agent_mcp(loaded, target, ceiling=pool)
    except CompileError as exc:
        raise LauncherError(f"the direct MCP path does not compile: {exc}") from None
    network: dict[str, NetworkPolicy] = {}
    runtime: dict[str, dict[str, Any]] = {}
    for name, server in servers.items():
        entry = compiled.policy.network_policies.get(f"mcp-{name}")
        if entry is None:
            why = next(
                (f.message for f in compiled.findings if f.subject == f"mcp.servers.{name}"),
                "not compiled",
            )
            raise LauncherError(f"mcp_direct.servers.{name}: {why}")
        network[f"mcp-{name}"] = entry
        (endpoint,) = entry.endpoints
        runtime[name] = {
            "url": declared[name].url,
            "credentials": dict(server.credentials),
            "tools": tool_admission(endpoint).model_dump(mode="json"),
        }
    hosts = sorted({e.host for entry in network.values() for e in entry.endpoints if e.host})
    middlewares = {
        key: value.model_copy(update={"endpoints": MiddlewareSelector(include=tuple(hosts))})
        for key, value in (compiled.policy.network_middlewares or {}).items()
    }
    fragment = OpenShellPolicy(network_policies=network, network_middlewares=middlewares or None)
    return DirectEgress(project, fragment, runtime, compiled.findings)


def merged(base: OpenShellPolicy, fragment: OpenShellPolicy) -> OpenShellPolicy:
    """``base``, the runtime sandbox's platform services, with the egress ``fragment``
    (``DirectEgress.fragment``) added; a name the two share is refused, never replaced."""
    shared = base.network_policies.keys() & fragment.network_policies.keys()
    middlewares = {**(base.network_middlewares or {})}
    shared |= middlewares.keys() & (fragment.network_middlewares or {}).keys()
    if shared:
        raise LauncherError(f"the runtime's policy already has {', '.join(sorted(shared))}")
    middlewares.update(fragment.network_middlewares or {})
    return base.model_copy(
        update={
            "network_policies": {**base.network_policies, **fragment.network_policies},
            "network_middlewares": middlewares or None,
        }
    )


def fragment_yaml(egress: DirectEgress) -> str:
    """The fragment as the Kubernetes runtime manager reads it back (``load_fragment``)."""
    return egress.fragment.to_yaml()


def load_fragment(text: str) -> OpenShellPolicy:
    """A fragment ``fragment_yaml`` wrote: network policies and middlewares only."""
    try:
        document = yaml.safe_load(text)
        policy = OpenShellPolicy.model_validate(document)
    except (yaml.YAMLError, ValueError) as exc:
        raise LauncherError(f"the runtime's MCP egress is not a policy fragment: {exc}") from None
    if policy.filesystem_policy is not None or policy.landlock is not None:
        raise LauncherError("the runtime's MCP egress may carry network sections only")
    return policy
