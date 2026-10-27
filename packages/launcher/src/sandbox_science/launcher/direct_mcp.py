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
sandbox is made. Only the configured servers' endpoints are kept.

The fence admits tools by the classification in force for the project when it is
compiled: the project's accepted revision of a server's classification where the policy
store binds one, else the catalog's. The host launcher compiles it on ``up`` and on every
replacement of the runtime, reading the policy store (``PolicyStore.fence_classifications``);
the Kubernetes runtime manager at every creation of the runtime's sandbox, reading the
revisions through the worker (``runtime_manager.fence``). A revision accepted while a
runtime runs therefore reaches its fence at the next replacement. Until
a fence is compiled again the policy middleware, which reads the binding at every call, is
the authority: a narrowing applies to the next call whatever the fence admits, and a
widening is refused at the stale fence (stricter).
A binding or revision that cannot be read leaves the server's fence on the catalog's, with
a warning; the middleware refuses that server's calls until it can be read.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sandbox_science.contracts.classifications import RuntimeClassifications
from sandbox_science.policy import load_ceiling, load_policy
from sandbox_science.policy.classification import ServerClassification, revision_classification
from sandbox_science.policy.errors import PolicyLoadError
from sandbox_science.policy.model import McpServer
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
from sandbox_science.policy.store import FenceClassifications

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
    revisions: dict[str, str] = field(default_factory=dict[str, str])
    """The project's accepted classification revision (hex) the fence admits each
    server's tools by; a server not named here is admitted by the catalog's."""

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
    classifications: FenceClassifications | None = None,
) -> DirectEgress:
    """Compile the runtime sandbox's egress to ``servers`` for ``project``'s ``policy``,
    admitting tools by the project's classification revisions in force
    (``classifications``: the host reads them from the policy store the worker and the
    middleware share, the Kubernetes runtime manager through the worker) where the policy
    names a classification for the server, else by the catalog's."""
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
    revisions, notes = _classifications(classifications, servers, declared)
    try:
        compiled = compile_agent_mcp(loaded, target, ceiling=pool, classifications=revisions)
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
    return DirectEgress(
        project,
        fragment,
        runtime,
        (*compiled.findings, *notes),
        {name: found.revision for name, found in revisions.items() if found.revision},
    )


def _classifications(
    found: FenceClassifications | None,
    servers: Mapping[str, McpDirectServer],
    declared: Mapping[str, McpServer],
) -> tuple[dict[str, ServerClassification], list[Finding]]:
    """The project's accepted revisions of the configured servers' classifications, and a
    finding for each server whose fence they decide or whose binding cannot be read."""
    if found is None:
        return {}, []
    # A revision applies only where the policy names a classification for the server
    # (``server_classification``), so only those servers' are taken.
    classified = {name for name in servers if declared[name].classification is not None}
    revisions = {name: rev for name, rev in found.revisions.items() if name in classified}
    unreadable = {name: why for name, why in found.unreadable.items() if name in classified}
    notes: list[Finding] = []
    for name, revision in sorted(revisions.items()):
        notes.append(
            Finding(
                Severity.INFO,
                f"mcp.servers.{name}.classification",
                f"the fence admits tools by the project's accepted revision {revision.revision}",
            )
        )
    for name, why in sorted(unreadable.items()):
        notes.append(
            Finding(
                Severity.WARNING,
                f"mcp.servers.{name}.classification",
                f"the project's classification binding cannot be read ({why}); the fence "
                "admits tools by the catalog's, and the policy middleware refuses the "
                "server's calls until the binding can be read",
            )
        )
    return revisions, notes


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


def worker_classifications(answer: RuntimeClassifications) -> FenceClassifications:
    """The revisions in force the worker answered (``GET /v1/runtime-classifications``),
    each checked as the policy store checks its own: a file that does not digest to its
    revision or does not load as that server's classification is unreadable, as is one
    the worker could not read, and its server's fence takes the catalog's."""
    revisions: dict[str, ServerClassification] = {}
    unreadable: dict[str, str] = {}
    for entry in answer.servers:
        if entry.error is not None:
            unreadable[entry.server] = entry.error
            continue
        if entry.revision is None:
            continue
        data = (entry.classification or "").encode()
        if hashlib.sha256(data).hexdigest() != entry.revision:
            unreadable[entry.server] = f"the file does not hold revision {entry.revision}"
            continue
        try:
            revisions[entry.server] = revision_classification(data, entry.server)
        except PolicyLoadError as exc:
            unreadable[entry.server] = f"revision {entry.revision} cannot be loaded: {exc}"
    return FenceClassifications(revisions, unreadable)
