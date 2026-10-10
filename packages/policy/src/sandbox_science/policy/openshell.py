# SPDX-License-Identifier: MIT
"""Compile a project policy into NVIDIA OpenShell v0.1.3 network policy.

Two sandboxes need OpenShell policy (docs/POLICY.md, "Mapping to enforcement"):

* A research sandbox reaches the outside only through platform services: the
  egress proxy, which applies this library to every HTTP and git request; the
  package mirror; and, when worker access is on, the MCP proxy. Its OpenShell
  policy is a fence of raw TCP endpoints for those services.
* The MCP proxy sandbox reaches the declared MCP servers. Its policy re-enforces
  the tool rules as OpenShell ``protocol: mcp`` endpoints.

Project HTTP and git rules are not translated into direct OpenShell endpoints.
OpenShell has no URL limits, cannot tell a git fetch from a REST write (it does
not see the content type), lets a GET rule admit HEAD, can only deny an
escalation, trusts private addresses for exact hosts and adds provider rules of
its own. Everything the compiler cannot express exactly is reported, and where
the compiled policy differs from the project policy it is stricter.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from os import PathLike
from types import MappingProxyType
from typing import Annotated, Final, Literal
from urllib.parse import urlsplit

import yaml
from pydantic import AfterValidator, BaseModel, ConfigDict, Field, StringConstraints

from .ceiling import PoolCeiling
from .decision import Action
from .evaluate import DEFAULT_FLOOR, MCP_PROTOCOL_METHODS, tool_pattern_matches
from .floor import HardFloor
from .hosts import floor_reason, normalize_host, parse_ip
from .loader import LoadedPolicy, parse_model, read_limited
from .model import (
    API_VERSION,
    BearerAuth,
    McpServer,
    NoAuth,
    OAuthAuth,
    Port,
    StrictModel,
)

CONTROL_PLANE_PORTS: Final = frozenset({2379, 2380, 6443, 10250, 10255})
"""Ports OpenShell blocks on exact-host endpoints (etcd, Kubernetes API, kubelet)."""

_PLACEHOLDER_PROVIDER_RE: Final = re.compile(
    r"\$\{provider:([a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)\."
)


# Output: the subset of OpenShell's authored policy schema (version 1) the compiler emits.


class _OpenShellModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Binary(_OpenShellModel):
    """An executable (or glob) allowed to use a network policy entry."""

    path: str


class AnyOf(_OpenShellModel):
    """A tool matcher that accepts any of several names."""

    any: tuple[str, ...]


class AllowEntry(_OpenShellModel):
    """An allowed request: an MCP method, optionally limited to tools."""

    method: str
    tool: str | AnyOf | None = None


class Rule(_OpenShellModel):
    """One entry of an endpoint's ``rules`` list."""

    allow: AllowEntry


class DenyEntry(_OpenShellModel):
    """One entry of an endpoint's ``deny_rules`` list; deny wins over allow."""

    method: str
    tool: str | AnyOf | None = None


class McpSettings(_OpenShellModel):
    """MCP inspection settings of an endpoint."""

    versions: tuple[str, ...]
    strict_tool_names: bool = True
    max_body_bytes: int | None = None


class CredentialBinding(_OpenShellModel):
    """Binds an attached, endpointless provider's credentials to the endpoint."""

    provider: str


class Endpoint(_OpenShellModel):
    """A network endpoint. ``tcp`` endpoints are raw relays; ``mcp`` ones are inspected."""

    host: str
    ports: tuple[int, ...]
    path: str | None = None
    protocol: Literal["tcp", "mcp"]
    enforcement: Literal["enforce"] | None = None
    rules: tuple[Rule, ...] | None = None
    deny_rules: tuple[DenyEntry, ...] | None = None
    mcp: McpSettings | None = None
    credential_binding: CredentialBinding | None = None


class NetworkPolicy(_OpenShellModel):
    """One entry of ``network_policies``."""

    name: str
    endpoints: tuple[Endpoint, ...]
    binaries: tuple[Binary, ...]


class OpenShellPolicy(_OpenShellModel):
    """An OpenShell policy document holding network policy only.

    Filesystem, Landlock and process settings are fixed when a sandbox starts
    and belong to the environment layer (D48), so the compiled document is
    applied with ``openshell policy update`` semantics: network sections only.
    """

    version: Literal[1] = 1
    network_policies: dict[str, NetworkPolicy]

    def to_yaml(self) -> str:
        """Render deterministic YAML (OpenShell's policy hash depends on list order)."""
        document = self.model_dump(mode="json", exclude_none=True)
        return yaml.safe_dump(document, sort_keys=False, default_flow_style=False)


# Report.


class Severity(StrEnum):
    """How much a finding matters to someone reviewing the compiled policy."""

    INFO = "info"
    WARNING = "warning"


@dataclass(frozen=True, slots=True)
class Finding:
    """Something the compiled policy does differently from the project policy, or adds."""

    severity: Severity
    subject: str
    message: str


@dataclass(frozen=True, slots=True)
class Compiled:
    """A compiled OpenShell policy with its findings.

    ``environment`` holds proxy variables for the sandbox's processes, and
    ``policy_digest`` identifies the project policy revision it came from.
    """

    policy: OpenShellPolicy
    findings: tuple[Finding, ...]
    environment: Mapping[str, str]
    policy_digest: str


# Targets: where the platform services run, and what may use them.


def _service_host(value: str) -> str:
    host = normalize_host(value)
    if parse_ip(host) is not None:
        raise ValueError(
            "address platform services by DNS name; OpenShell tcp endpoints reject IP literals"
        )
    reason = floor_reason(host)
    if reason is not None:
        raise ValueError(f"{host} is on the hard floor: {reason}")
    return host


def _service_port(value: int) -> int:
    if value in CONTROL_PLANE_PORTS:
        raise ValueError(f"port {value} is a control-plane port that OpenShell blocks")
    return value


class ServiceEndpoint(StrictModel):
    """A platform service the sandbox may reach (Z1 in the threat model)."""

    host: Annotated[str, AfterValidator(_service_host)]
    port: Annotated[Port, AfterValidator(_service_port)]


BinaryPattern = Annotated[str, StringConstraints(pattern=r"^/[!-~]*$")]
"""An absolute executable path or a '/'-delimited glob, in printable ASCII."""

McpVersion = Literal["2025-03-26", "2025-06-18", "2025-11-25", "2026-07-28"]


class SandboxTarget(StrictModel):
    """Platform services for research sandboxes.

    ``binaries`` defaults to every executable: research code runs arbitrary
    programs, and the services themselves enforce the policy.
    """

    egress_proxy: ServiceEndpoint
    package_mirror: ServiceEndpoint
    mcp_proxy: ServiceEndpoint
    binaries: tuple[BinaryPattern, ...] = Field(default=("/**",), min_length=1)


class McpProxyTarget(StrictModel):
    """Settings for the MCP proxy's own sandbox.

    ``binaries`` names the MCP proxy executable, so that nothing else in that
    sandbox reaches the servers. ``versions`` lists the MCP revisions clients
    send; OpenShell treats a missing version header as 2025-03-26.
    """

    binaries: tuple[BinaryPattern, ...] = Field(min_length=1)
    versions: tuple[McpVersion, ...] = Field(default=("2025-11-25",), min_length=1)
    max_body_bytes: Annotated[int, Field(gt=0)] | None = None


class OpenShellTargets(StrictModel):
    """A targets file: where platform services run for one deployment."""

    api_version: Literal["sandbox-science/v0alpha1"] = Field(
        default=API_VERSION, alias="apiVersion"
    )
    kind: Literal["OpenShellTargets"] = "OpenShellTargets"
    sandbox: SandboxTarget
    mcp_proxy: McpProxyTarget


def parse_targets(text: str | bytes, *, source: str | None = None) -> OpenShellTargets:
    """Parse a targets file in the strict policy dialect. Raises PolicyLoadError."""
    return parse_model(OpenShellTargets, text, kind="OpenShell targets", source=source)


def load_targets(path: str | PathLike[str]) -> OpenShellTargets:
    """Read and parse a targets file. Raises PolicyLoadError."""
    return parse_targets(read_limited(path), source=str(path))


# Compilation.


def compile_sandbox(policy: LoadedPolicy, target: SandboxTarget) -> Compiled:
    """Compile the OpenShell fence for a research sandbox.

    The sandbox may connect only to the egress proxy, the package mirror and,
    when ``mcp.proxy.worker_access`` is true, the MCP proxy. The services apply
    the project policy themselves.
    """
    project = policy.policy
    findings: list[Finding] = []
    binaries = tuple(Binary(path=path) for path in target.binaries)
    services = [
        ("egress-proxy", "egress proxy", target.egress_proxy),
        ("package-mirror", "package mirror", target.package_mirror),
    ]
    if project.mcp.proxy.worker_access:
        services.append(("mcp-proxy", "MCP proxy", target.mcp_proxy))
    else:
        findings.append(
            Finding(
                Severity.INFO,
                "mcp.proxy.worker_access",
                "worker code cannot reach the MCP proxy: the fence admits no connection to it",
            )
        )
    network_policies = {
        f"sandbox-science-{key}": NetworkPolicy(
            name=f"Sandbox Science {label}",
            endpoints=(Endpoint(host=service.host, ports=(service.port,), protocol="tcp"),),
            binaries=binaries,
        )
        for key, label, service in services
    }
    findings.extend(
        Finding(
            Severity.INFO,
            f"network.rules.{rule.name}",
            "enforced by the egress proxy with the full project semantics; OpenShell admits "
            "the sandbox only to the proxy",
        )
        for rule in project.network.rules
    )
    if project.monitors:
        findings.append(
            Finding(Severity.INFO, "monitors", "evaluated from the egress proxy's usage counters")
        )
    findings.extend(
        Finding(
            Severity.WARNING,
            f"credentials.providers.{provider.name}",
            "an attached OpenShell provider adds its profile's endpoints as direct rules "
            "(_provider_*) that bypass the egress proxy and this policy; review the profile's "
            "hosts and methods before attaching it",
        )
        for provider in project.credentials.providers
    )
    if project.resources.model_dump(exclude_none=True):
        findings.append(
            Finding(
                Severity.INFO,
                "resources",
                "applied by the compute driver and the LiteLLM virtual key, not by network policy",
            )
        )
    proxy = f"http://{target.egress_proxy.host}:{target.egress_proxy.port}"
    direct = ",".join(service.host for key, _, service in services if key != "egress-proxy")
    environment = {
        "HTTP_PROXY": proxy,
        "HTTPS_PROXY": proxy,
        "NO_PROXY": direct,
        "http_proxy": proxy,
        "https_proxy": proxy,
        "no_proxy": direct,
    }
    return Compiled(
        policy=OpenShellPolicy(network_policies=network_policies),
        findings=tuple(findings),
        environment=MappingProxyType(environment),
        policy_digest=policy.digest,
    )


def compile_mcp_proxy(
    policy: LoadedPolicy,
    target: McpProxyTarget,
    *,
    ceiling: PoolCeiling,
    floor: HardFloor = DEFAULT_FLOOR,
) -> Compiled:
    """Compile the MCP proxy sandbox's policy: one inspected endpoint per declared server.

    Servers the floor or the pool ceiling rule out are left out and reported.
    """
    project = policy.policy
    findings: list[Finding] = []
    binaries = tuple(Binary(path=path) for path in target.binaries)
    network_policies: dict[str, NetworkPolicy] = {}
    for name in sorted(project.mcp.servers):
        server = project.mcp.servers[name]
        subject = f"mcp.servers.{name}"
        reason = (
            floor.check_host(server.host)
            or ceiling.check_mcp_server(name)
            or ceiling.check_host(server.host)
        )
        if reason is not None:
            findings.append(Finding(Severity.WARNING, subject, f"not compiled: {reason}"))
            continue
        parts = urlsplit(server.url)
        port = parts.port or 443
        if port in CONTROL_PLANE_PORTS:
            findings.append(
                Finding(
                    Severity.WARNING,
                    subject,
                    f"port {port} is a control-plane port that OpenShell blocks; calls will fail",
                )
            )
        rules, deny_rules = _tool_rules(server, project.defaults.action, subject, findings)
        endpoint = Endpoint(
            host=server.host,
            ports=(port,),
            path=parts.path if parts.path not in ("", "/") else None,
            protocol="mcp",
            enforcement="enforce",
            rules=rules,
            deny_rules=deny_rules or None,
            mcp=McpSettings(versions=target.versions, max_body_bytes=target.max_body_bytes),
            credential_binding=_credential_binding(server, subject, findings),
        )
        network_policies[f"mcp-{name}"] = NetworkPolicy(
            name=f"MCP server {name}", endpoints=(endpoint,), binaries=binaries
        )
    if "2025-03-26" in target.versions:
        findings.append(
            Finding(
                Severity.INFO,
                "versions",
                "2025-03-26 admits JSON-RPC batches of up to 64 messages; OpenShell checks each "
                "member and the MCP proxy rejects batches",
            )
        )
    if project.mcp.proxy.worker_tools != "inherit":
        findings.append(
            Finding(
                Severity.INFO,
                "mcp.proxy.worker_tools",
                "applied by the MCP proxy per caller; OpenShell cannot tell callers apart",
            )
        )
    return Compiled(
        policy=OpenShellPolicy(network_policies=network_policies),
        findings=tuple(findings),
        environment=MappingProxyType({}),
        policy_digest=policy.digest,
    )


def _collapse(pattern: str) -> str:
    # Runs of '*' mean the same as one '*' here, while '**' means something else to
    # OpenShell; collapse them before compiling.
    return re.sub(r"\*+", "*", pattern)


def _tool_rules(
    server: McpServer, default: Action, subject: str, findings: list[Finding]
) -> tuple[tuple[Rule, ...], tuple[DenyEntry, ...]]:
    """Translate tool lists into OpenShell rules that are never looser than the project's.

    OpenShell denies tools no allow entry names, and any matching deny entry wins.
    Escalated tools are denied at the fence until an approval is compiled in.
    """
    prefix = server.tool_prefix or ""
    tools = server.tools
    blocking = sorted({_collapse(pattern) for pattern in (*tools.escalate, *tools.deny)})
    exact_allows = sorted(pattern for pattern in tools.allow if "*" not in pattern)
    glob_allows = sorted({_collapse(pattern) for pattern in tools.allow if "*" in pattern})
    blocking_globs = any("*" in pattern for pattern in blocking)
    # OpenShell's tool globs use '.' as the separator: '*' stops at it and only '**'
    # matches any value, while the project's '*' matches dots too. A compiled deny
    # pattern can therefore miss a dotted name its project pattern blocks. When there
    # are deny patterns, allow patterns must admit only names whose part after the
    # prefix has no dot, where the two dialects agree.
    if blocking_globs and any("." in pattern for pattern in glob_allows):
        findings.append(
            Finding(
                Severity.WARNING,
                f"{subject}.tools",
                "allow patterns containing '.' are not compiled while escalate or deny "
                "contain patterns, which OpenShell matches differently for dotted names "
                "(stricter)",
            )
        )
        glob_allows = [pattern for pattern in glob_allows if "." not in pattern]
    if "*" in blocking and glob_allows:
        findings.append(
            Finding(
                Severity.INFO,
                f"{subject}.tools",
                "allow patterns are outranked by '*' in escalate or deny and are not compiled",
            )
        )
        glob_allows = []
    rules = [Rule(allow=AllowEntry(method=method)) for method in sorted(MCP_PROTOCOL_METHODS)]
    if exact_allows:
        names = tuple(prefix + pattern for pattern in exact_allows)
        rules.append(Rule(allow=AllowEntry(method="tools/call", tool=AnyOf(any=names))))
    for pattern in glob_allows:
        # A lone '*' without a prefix may admit every name ('**') only when no deny
        # pattern exists; otherwise it admits dot-free names ('*').
        everything = pattern == "*" and not prefix and not blocking_globs
        rules.append(
            Rule(
                allow=AllowEntry(method="tools/call", tool="**" if everything else prefix + pattern)
            )
        )
    deny_rules: list[DenyEntry] = []
    if glob_allows:
        # Only an allow pattern can admit a tool the project blocks, so deny entries
        # are needed only when there is one.
        deny_rules = [DenyEntry(method="tools/call", tool=prefix + pattern) for pattern in blocking]
        findings.append(
            Finding(
                Severity.INFO,
                f"{subject}.tools",
                "OpenShell's '*' does not match '.', so allow patterns admit fewer names when "
                "tool names contain dots (stricter)",
            )
        )
        for name in exact_allows:
            for pattern in blocking:
                if "*" in pattern and tool_pattern_matches(pattern, name):
                    findings.append(
                        Finding(
                            Severity.WARNING,
                            f"{subject}.tools",
                            f"{prefix + name} is allowed by exact name, but the deny entry "
                            f"{prefix + pattern} also matches it, so OpenShell denies it "
                            "(stricter)",
                        )
                    )
    if default in (Action.ALLOW, Action.ALERT):
        findings.append(
            Finding(
                Severity.WARNING,
                f"{subject}.tools",
                f"defaults.action is {default}, but OpenShell denies tools that no allow entry "
                "names, so such tools are denied at the fence (stricter)",
            )
        )
    if tools.escalate or default is Action.ESCALATE:
        findings.append(
            Finding(
                Severity.INFO,
                f"{subject}.tools",
                "escalated tools are denied at the fence until an approval is compiled in as a "
                "grant (OpenShell reloads network policy within about 10 seconds)",
            )
        )
    return tuple(rules), tuple(deny_rules)


def _credential_binding(
    server: McpServer, subject: str, findings: list[Finding]
) -> CredentialBinding | None:
    auth = server.auth
    if isinstance(auth, NoAuth):
        return None
    if isinstance(auth, OAuthAuth | BearerAuth):
        providers = {auth.provider}
    else:
        providers = {
            match
            for value in auth.headers.values()
            for match in _PLACEHOLDER_PROVIDER_RE.findall(value)
        }
    if len(providers) != 1:
        findings.append(
            Finding(
                Severity.WARNING,
                f"{subject}.auth",
                "OpenShell binds one provider per endpoint, and these headers name "
                f"{len(providers)}; no credential is bound, so calls will fail (stricter)",
            )
        )
        return None
    (provider,) = providers
    findings.append(
        Finding(
            Severity.INFO,
            f"{subject}.auth",
            f"provider {provider} must be attached to the MCP proxy sandbox and be endpointless",
        )
    )
    return CredentialBinding(provider=provider)
