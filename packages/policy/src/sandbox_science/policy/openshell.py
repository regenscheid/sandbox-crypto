# SPDX-License-Identifier: MIT
"""Compile a project policy into NVIDIA OpenShell v0.1.3 network policy.

Two sandboxes need OpenShell policy (docs/POLICY.md, "Mapping to enforcement"):

* A research sandbox's HTTP and git traffic goes through OpenShell's own proxy,
  which terminates TLS, injects provider credentials and logs. The compiled
  policy admits the destinations the project could ever reach, and attaches the
  platform's policy middleware to every host: OpenShell calls it for each
  admitted request, before injecting credentials, and the middleware decides
  with this library. The platform services (the package cache and, with worker
  access, the MCP proxy) are inspected endpoints too; the middleware recognizes
  them by host and port. Nothing is excluded from the middleware, because
  OpenShell selects middleware by host alone. Provider credentials are bound
  here too, to the hosts the project names for each provider, so that OpenShell
  substitutes them nowhere else.
* The MCP proxy sandbox reaches the declared MCP servers. Its policy re-enforces
  the tool rules as OpenShell ``protocol: mcp`` endpoints.

Everything the compiler cannot express exactly is reported, and where the
compiled policy differs from the project policy it is stricter.
"""

from __future__ import annotations

import ipaddress
import re
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from os import PathLike
from typing import Annotated, Final, Literal
from urllib.parse import urlsplit

import yaml
from pydantic import AfterValidator, BaseModel, ConfigDict, Field, StringConstraints

from .ceiling import PoolCeiling
from .decision import Action
from .evaluate import DEFAULT_FLOOR, MCP_PROTOCOL_METHODS, registry_reason, reserved_host_reason
from .floor import HardFloor
from .hosts import HostKind, floor_reason, is_public, normalize_host, parse_ip
from .loader import LoadedPolicy, parse_model, read_limited
from .model import (
    API_VERSION,
    BearerAuth,
    GitRule,
    HttpRule,
    McpServer,
    Name,
    NoAuth,
    OAuthAuth,
    Port,
    StrictModel,
)

CONTROL_PLANE_PORTS: Final = frozenset({2379, 2380, 6443, 10250, 10255})
"""Ports OpenShell blocks on exact-host endpoints (etcd, Kubernetes API, kubelet)."""

CREDENTIAL_PORT: Final = 443
"""Credentials are bound to HTTPS only, so they never cross the network in clear text."""

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
    """A network endpoint. ``tcp`` endpoints are raw relays; ``rest`` and ``mcp`` are inspected.

    An endpoint without ``host`` admits any host name whose addresses all lie in
    ``allowed_ips``.
    """

    host: str | None = None
    ports: tuple[int, ...]
    path: str | None = None
    protocol: Literal["tcp", "rest", "mcp"]
    enforcement: Literal["enforce"] | None = None
    access: Literal["full", "read-only"] | None = None
    allow_encoded_slash: bool | None = None
    """Accept ``%2F`` in paths, as npm's scoped package names need."""
    allowed_ips: tuple[str, ...] | None = None
    rules: tuple[Rule, ...] | None = None
    deny_rules: tuple[DenyEntry, ...] | None = None
    mcp: McpSettings | None = None
    credential_binding: CredentialBinding | None = None


class NetworkPolicy(_OpenShellModel):
    """One entry of ``network_policies``."""

    name: str
    endpoints: tuple[Endpoint, ...]
    binaries: tuple[Binary, ...]


class MiddlewareSelector(_OpenShellModel):
    """Destination hosts a middleware runs for; ``exclude`` wins over ``include``."""

    include: tuple[str, ...]
    exclude: tuple[str, ...] | None = None


class NetworkMiddleware(_OpenShellModel):
    """One entry of ``network_middlewares``: a registered middleware service and where it runs."""

    middleware: str
    order: int
    on_error: Literal["fail_closed"]
    endpoints: MiddlewareSelector
    config: dict[str, dict[str, str | int]]


class OpenShellPolicy(_OpenShellModel):
    """An OpenShell policy document holding network policy only.

    Filesystem, Landlock and process settings are fixed when a sandbox starts
    and belong to the environment layer (D48). Apply the compiled document so
    that it replaces the sandbox's network sections entirely: an entry left over
    from an earlier compilation could admit what the policy no longer allows.
    """

    version: Literal[1] = 1
    network_policies: dict[str, NetworkPolicy]
    network_middlewares: dict[str, NetworkMiddleware] | None = None

    def to_yaml(self) -> str:
        """Render deterministic YAML (OpenShell's policy hash depends on list order).

        Strings that a YAML 1.2 parser could read as numbers, booleans or null are
        quoted; PyYAML only quotes the YAML 1.1 forms.
        """
        document = self.model_dump(mode="json", exclude_none=True)
        return yaml.dump(document, Dumper=_Dumper, sort_keys=False, default_flow_style=False)


_YAML12_NON_STRING_RE: Final = re.compile(
    r"[-+]?[0-9]+|0o[0-7]+|0x[0-9a-fA-F]+|[-+]?(\.[0-9]+|[0-9]+(\.[0-9]*)?)([eE][-+]?[0-9]+)?"
    r"|[-+]?\.(inf|Inf|INF)|\.(nan|NaN|NAN)|true|True|TRUE|false|False|FALSE|null|Null|NULL|~|"
)


class _Dumper(yaml.SafeDumper):
    """A SafeDumper that also quotes strings YAML 1.2 would read as another type."""


def _represent_str(dumper: yaml.SafeDumper, value: str) -> yaml.ScalarNode:
    style = '"' if _YAML12_NON_STRING_RE.fullmatch(value) else None
    # The stubs leave represent_scalar's value parameter untyped.
    return dumper.represent_scalar("tag:yaml.org,2002:str", value, style=style)  # pyright: ignore[reportUnknownMemberType]


_Dumper.add_representer(str, _represent_str)


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


class CompileError(ValueError):
    """The targets and the project policy cannot produce a policy OpenShell would load."""


@dataclass(frozen=True, slots=True)
class Compiled:
    """A compiled OpenShell policy with its findings.

    ``policy_digest`` identifies the project policy revision it came from.
    """

    policy: OpenShellPolicy
    findings: tuple[Finding, ...]
    policy_digest: str


# Targets: where the platform services run, and what may use them.


HOST_GATEWAY_ALIAS: Final = "host.openshell.internal"
"""OpenShell's name for the gateway host, where a single-host deployment runs its services."""


def _service_host(value: str) -> str:
    host = normalize_host(value)
    if parse_ip(host) is not None:
        raise ValueError(
            "address platform services by DNS name; OpenShell tcp endpoints reject IP literals"
        )
    # The floor keeps project rules away from the gateway host; the operator may
    # still run platform services there (the VM driver on babbage).
    reason = None if host == HOST_GATEWAY_ALIAS else floor_reason(host)
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

    ``middleware`` is the name under which the policy middleware service is
    registered in the gateway configuration. ``binaries`` defaults to every
    executable: research code runs arbitrary programs, and the middleware and
    services decide what they may do.
    """

    middleware: Name
    package_cache: ServiceEndpoint
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


_NON_PUBLIC_IPV4: Final = tuple(
    ipaddress.IPv4Network(block)
    for block in (
        "0.0.0.0/8",
        "10.0.0.0/8",
        "100.64.0.0/10",
        "127.0.0.0/8",
        "169.254.0.0/16",
        "172.16.0.0/12",
        "192.0.0.0/24",
        "192.0.2.0/24",
        "192.88.99.0/24",
        "192.168.0.0/16",
        "198.18.0.0/15",
        "198.51.100.0/24",
        "203.0.113.0/24",
        "224.0.0.0/4",
        "240.0.0.0/4",
    )
)
_NON_PUBLIC_IPV6: Final = tuple(
    ipaddress.IPv6Network(block)
    for block in ("2001::/23", "2001:db8::/32", "2002::/16", "3fff::/20")
)


def _without[N: (ipaddress.IPv4Network, ipaddress.IPv6Network)](
    network: N, blocks: tuple[N, ...]
) -> list[N]:
    remaining = [network]
    for block in blocks:
        kept: list[N] = []
        for candidate in remaining:
            if not candidate.overlaps(block):
                kept.append(candidate)
            elif block.subnet_of(candidate):
                kept.extend(candidate.address_exclude(block))
        remaining = kept
    return sorted(ipaddress.collapse_addresses(remaining))


def public_address_ranges() -> tuple[str, ...]:
    """Public unicast address space, for a host-less endpoint's ``allowed_ips``.

    IPv4 without its special-purpose blocks, and IPv6 global unicast
    (``2000::/3``) without documentation, 6to4 and IETF protocol assignments.
    Every address in these ranges is public by the library's rule, so OpenShell
    admits no address the library would refuse to a wildcard rule.
    """
    ipv4 = _without(ipaddress.IPv4Network("0.0.0.0/0"), _NON_PUBLIC_IPV4)
    ipv6 = _without(ipaddress.IPv6Network("2000::/3"), _NON_PUBLIC_IPV6)
    return tuple(str(network) for network in (*ipv4, *ipv6))


def _wildcard_hosts(rules: Sequence[HttpRule | GitRule], findings: list[Finding]) -> list[str]:
    """OpenShell host patterns for the rules' ``*.suffix`` wildcards.

    The library's ``*.suffix`` matches names at any depth below the suffix, which
    is OpenShell's ``**.suffix``. OpenShell requires three labels in a wildcard
    host, so a wildcard over a top-level domain cannot be named; wildcards never
    grant non-public names, so those are not named either.
    """
    hosts: set[str] = set()
    for rule in rules:
        for pattern in rule.match.hosts:
            # The suffix itself may be a lone label such as "org"; what matters is
            # whether the names under it are public.
            if pattern.kind is not HostKind.WILDCARD or not is_public(f"x.{pattern.value}"):
                continue
            reason = registry_reason(f"x.{pattern.value}") or registry_reason(pattern.value)
            if reason is not None:
                findings.append(
                    Finding(Severity.WARNING, f"hosts.{pattern}", f"not admitted: {reason}")
                )
                continue
            if pattern.labels < 2:
                findings.append(
                    Finding(
                        Severity.WARNING,
                        f"hosts.{pattern}",
                        "OpenShell wildcard hosts need three labels, so names under this "
                        "pattern cannot be resolved in a sandbox (stricter)",
                    )
                )
                continue
            hosts.add(f"**.{pattern.value}")
    return sorted(hosts)


def compile_sandbox(
    policy: LoadedPolicy,
    target: SandboxTarget,
    *,
    ceiling: PoolCeiling,
    floor: HardFloor = DEFAULT_FLOOR,
) -> Compiled:
    """Compile a research sandbox's policy: admission endpoints plus the policy middleware.

    OpenShell admits the destinations the project could ever reach and calls the
    policy middleware for every admitted request; the middleware decides with
    this library. Raises CompileError when the result would not load.
    """
    project = policy.policy
    findings: list[Finding] = []
    binaries = tuple(Binary(path=path) for path in target.binaries)
    reachable = [rule for rule in project.network.rules if rule.action is not Action.DENY]

    ports = {80, 443} | {port for rule in reachable for port in rule.match.ports}
    blocked = sorted(ports & CONTROL_PLANE_PORTS)
    if blocked:
        findings.append(
            Finding(
                Severity.WARNING,
                "network.rules",
                f"ports {blocked} are control-plane ports that OpenShell blocks; requests to "
                "them fail",
            )
        )
    admitted_ports = tuple(sorted(ports - CONTROL_PLANE_PORTS))

    # Unmatched requests take defaults.action, so anything but deny must reach the
    # middleware too, as must every wildcard rule.
    open_internet = project.defaults.action is not Action.DENY or any(
        pattern.kind is not HostKind.EXACT for rule in reachable for pattern in rule.match.hosts
    )
    exact_hosts = sorted(
        {
            pattern.value
            for rule in reachable
            for pattern in rule.match.hosts
            if pattern.kind is HostKind.EXACT
        }
    )
    endpoints: list[Endpoint] = []
    private_addresses: list[str] = []
    # OpenShell's policy DNS answers only for host names that an endpoint names;
    # a sandbox cannot resolve any other name, so the host-less endpoint admits
    # only requests to addresses. Public names the rules mention get endpoints of
    # their own, beside it.
    named_hosts: list[str] = []
    service_hosts = {target.package_cache.host, target.mcp_proxy.host}
    bound = _bound_hosts(policy, ceiling, floor, service_hosts, findings)

    def egress_ports(host: str) -> tuple[int, ...]:
        # A credential's endpoint alone carries its host's HTTPS traffic, so that
        # no other endpoint competes with it there (lab 2).
        if host in bound:
            return tuple(port for port in admitted_ports if port != CREDENTIAL_PORT)
        return admitted_ports

    for host in exact_hosts:
        # Hosts the library always denies are not admitted either, so that the
        # middleware is never the only thing standing between them and a sandbox.
        reason = (
            floor.check_host(host)
            or ceiling.check_host(host)
            or reserved_host_reason(host, policy)
            or (f"{host} is a platform service host" if host in service_hosts else None)
        )
        if reason is not None:
            findings.append(Finding(Severity.WARNING, f"hosts.{host}", f"not admitted: {reason}"))
            continue
        if open_internet and is_public(host):
            if parse_ip(host) is None:
                named_hosts.append(host)
            continue  # addresses: admitted by the host-less endpoint
        if not open_internet:
            if egress_ports(host):
                endpoints.append(
                    Endpoint(
                        host=host,
                        ports=egress_ports(host),
                        protocol="rest",
                        enforcement="enforce",
                        access="full",
                    )
                )
        elif parse_ip(host) is not None:
            private_addresses.append(host)
            findings.append(
                Finding(
                    Severity.INFO,
                    f"hosts.{host}",
                    "admitted through the host-less endpoint's allowed_ips, so any name that "
                    "resolves to this address reaches the middleware, which sees only the name",
                )
            )
        else:
            findings.append(
                Finding(
                    Severity.WARNING,
                    f"hosts.{host}",
                    "not admitted: a non-public host name cannot sit beside the host-less "
                    "endpoint, because overlapping OpenShell endpoints must agree on "
                    "allowed_ips; name its IP address in the rule instead (stricter)",
                )
            )
    allowed_ips: tuple[str, ...] | None = None
    if open_internet:
        allowed_ips = (*public_address_ranges(), *private_addresses)
        endpoints.insert(
            0,
            Endpoint(
                ports=admitted_ports,
                protocol="rest",
                enforcement="enforce",
                access="full",
                allowed_ips=allowed_ips,
            ),
        )
        # Overlapping OpenShell endpoints must agree on allowed_ips, so the named
        # endpoints repeat the host-less endpoint's ranges.
        for host in [*named_hosts, *_wildcard_hosts(reachable, findings)]:
            if not egress_ports(host):
                continue
            endpoints.append(
                Endpoint(
                    host=host,
                    ports=egress_ports(host),
                    protocol="rest",
                    enforcement="enforce",
                    access="full",
                    allowed_ips=allowed_ips,
                )
            )
        findings.append(
            Finding(
                Severity.INFO,
                "network",
                "public destinations are admitted by a host-less endpoint limited to public "
                "address ranges, so OpenShell refuses private and special addresses, as the "
                "library's wildcard rule does; host names reach it only when an endpoint names "
                "them, because OpenShell's policy DNS answers only for named hosts",
            )
        )
        if project.defaults.action is not Action.DENY or any(
            pattern.kind is HostKind.ANY for rule in reachable for pattern in rule.match.hosts
        ):
            findings.append(
                Finding(
                    Severity.WARNING,
                    "network",
                    "rules for '*' and the default action reach only the hosts this policy "
                    "names and IP addresses: a sandbox cannot resolve other names until "
                    "on-demand admission approves them (stricter)",
                )
            )

    cache = target.package_cache
    proxy = target.mcp_proxy
    services = [("package-cache", "package cache", cache)]
    if project.mcp.proxy.worker_access:
        services.append(("mcp-proxy", "MCP proxy", proxy))
        if (proxy.host, proxy.port) == (cache.host, cache.port):
            raise CompileError("the package cache and the MCP proxy need different ports")
    else:
        findings.append(
            Finding(
                Severity.INFO,
                "mcp.proxy.worker_access",
                "worker code cannot reach the MCP proxy: no endpoint admits it",
            )
        )
    findings.append(
        Finding(
            Severity.WARNING,
            "services",
            "platform services must have private addresses, so that the host-less endpoint "
            "never admits them",
        )
    )
    for _, label, service in services:
        if open_internet and service.port in admitted_ports:
            raise CompileError(
                f"the {label} uses port {service.port}, which the host-less endpoint also "
                "admits; OpenShell rejects overlapping endpoints that differ, so move the "
                "service to another port"
            )

    network_policies: dict[str, NetworkPolicy] = {}
    if endpoints:
        network_policies["sandbox-science-egress"] = NetworkPolicy(
            name="Sandbox Science egress", endpoints=tuple(endpoints), binaries=binaries
        )
    # The package cache is inspected: the middleware sees every package request
    # and applies the packages section. OpenShell itself admits only reads.
    network_policies["sandbox-science-package-cache"] = NetworkPolicy(
        name="Sandbox Science package cache",
        endpoints=(
            Endpoint(
                host=cache.host,
                ports=(cache.port,),
                protocol="rest",
                enforcement="enforce",
                access="read-only",
                allow_encoded_slash=True,
            ),
        ),
        binaries=binaries,
    )
    config: dict[str, dict[str, str | int]] = {
        "policy": {"name": project.metadata.name, "digest": policy.digest},
        "package_cache": {"host": cache.host, "port": cache.port},
    }
    if project.mcp.proxy.worker_access:
        # Inspected as well: the middleware lets worker code reach the MCP proxy,
        # which decides each tool call.
        network_policies["sandbox-science-mcp-proxy"] = NetworkPolicy(
            name="Sandbox Science MCP proxy",
            endpoints=(
                Endpoint(
                    host=proxy.host,
                    ports=(proxy.port,),
                    protocol="rest",
                    enforcement="enforce",
                    access="full",
                ),
            ),
            binaries=binaries,
        )
        config["mcp_proxy"] = {"host": proxy.host, "port": proxy.port}
    if bound:
        # The path selector makes each endpoint more specific than the host-less
        # and wildcard endpoints that also admit its host, so that OpenShell does
        # not require them to agree on the binding, and selects this endpoint for
        # the host's requests. Overlapping endpoints must agree on allowed_ips.
        network_policies["sandbox-science-credentials"] = NetworkPolicy(
            name="Sandbox Science credentials",
            endpoints=tuple(
                Endpoint(
                    host=host,
                    ports=(CREDENTIAL_PORT,),
                    path="/**",
                    protocol="rest",
                    enforcement="enforce",
                    access="full",
                    allowed_ips=allowed_ips,
                    credential_binding=CredentialBinding(provider=provider),
                )
                for host, provider in sorted(bound.items())
            ),
            binaries=binaries,
        )
        findings.append(
            Finding(
                Severity.INFO,
                "credentials.providers",
                f"each provider's credential is bound to its hosts on port {CREDENTIAL_PORT}; "
                "OpenShell substitutes it there only, after the middleware has decided, and "
                "refuses requests that carry its placeholder anywhere else. Providers named here "
                "need profiles without endpoints",
            )
        )
    # OpenShell selects middleware by host alone, never by port: excluding a
    # service host would take every port on that host out of the middleware.
    middleware = NetworkMiddleware(
        middleware=target.middleware,
        order=0,
        on_error="fail_closed",
        endpoints=MiddlewareSelector(include=("**",)),
        config=config,
    )
    findings.append(
        Finding(
            Severity.INFO,
            "network_middlewares",
            "every admitted HTTP request, including those to hosts bound to provider "
            "credentials, is decided by the policy middleware before OpenShell injects "
            "credentials; if the middleware fails or times out, OpenShell blocks the request",
        )
    )
    findings.append(
        Finding(
            Severity.INFO,
            "escalation",
            "escalated requests are denied with reason code approval_required and a pending "
            "request is recorded; the agent retries once the request is approved",
        )
    )
    findings.append(
        Finding(
            Severity.WARNING,
            "network_middlewares",
            "requests whose body exceeds the middleware's registered max_payload_bytes (at most "
            "4 MiB) are blocked, so larger uploads fail",
        )
    )
    if project.monitors:
        findings.append(
            Finding(Severity.INFO, "monitors", "evaluated from the middleware's request records")
        )
    if project.resources.model_dump(exclude_none=True):
        findings.append(
            Finding(
                Severity.INFO,
                "resources",
                "applied by the compute driver and the LiteLLM virtual key, not by network policy",
            )
        )
    return Compiled(
        policy=OpenShellPolicy(
            network_policies=network_policies,
            network_middlewares={"sandbox-science-policy": middleware},
        ),
        findings=tuple(findings),
        policy_digest=policy.digest,
    )


def _bound_hosts(
    policy: LoadedPolicy,
    ceiling: PoolCeiling,
    floor: HardFloor,
    service_hosts: set[str],
    findings: list[Finding],
) -> dict[str, str]:
    """Map each host a provider credential may be bound to onto its provider.

    Hosts the floor, the pool ceiling or a platform route rules out are left
    unbound and reported, as are hosts that are not public: an endpoint for
    them could not share the host-less endpoint's address ranges.
    """
    bound: dict[str, str] = {}
    for provider in policy.policy.credentials.providers:
        for host in provider.hosts:
            reason = (
                floor.check_host(host)
                or ceiling.check_host(host)
                or reserved_host_reason(host, policy)
                or (f"{host} is a platform service host" if host in service_hosts else None)
                or (None if is_public(host) else f"{host} is not a public host")
            )
            if reason is not None:
                findings.append(
                    Finding(
                        Severity.WARNING,
                        f"credentials.providers.{provider.name}",
                        f"credential not bound to {host}: {reason} (stricter)",
                    )
                )
                continue
            bound[host] = provider.name
    return bound


def compile_mcp_proxy(
    policy: LoadedPolicy,
    target: McpProxyTarget,
    *,
    ceiling: PoolCeiling,
    floor: HardFloor = DEFAULT_FLOOR,
) -> Compiled:
    """Compile the MCP proxy sandbox's policy: one inspected endpoint per declared server.

    Servers the floor or the pool ceiling rule out are left out and reported.
    Raises CompileError when two servers would share an endpoint.
    """
    project = policy.policy
    findings: list[Finding] = []
    binaries = tuple(Binary(path=path) for path in target.binaries)
    network_policies: dict[str, NetworkPolicy] = {}
    endpoints_seen: dict[tuple[str, int, str], str] = {}
    bindings_by_host: dict[tuple[str, int], set[str | None]] = {}
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
        port = parts.port if parts.port is not None else 443
        path = parts.path or "/"
        key = (server.host, port, path)
        if key in endpoints_seen:
            raise CompileError(
                f"MCP servers {endpoints_seen[key]!r} and {name!r} share the endpoint "
                f"{server.host}:{port}{path}; OpenShell cannot tell their rules apart"
            )
        endpoints_seen[key] = name
        if port in CONTROL_PLANE_PORTS:
            findings.append(
                Finding(
                    Severity.WARNING,
                    subject,
                    f"port {port} is a control-plane port that OpenShell blocks; calls will fail",
                )
            )
        rules, deny_rules = _tool_rules(server, project.defaults.action, subject, findings)
        binding = _credential_binding(server, subject, findings)
        bindings_by_host.setdefault((server.host, port), set()).add(
            binding.provider if binding else None
        )
        endpoint = Endpoint(
            host=server.host,
            ports=(port,),
            path=path,
            protocol="mcp",
            enforcement="enforce",
            rules=rules,
            deny_rules=deny_rules or None,
            mcp=McpSettings(
                versions=_versions(server, target, subject, findings),
                max_body_bytes=target.max_body_bytes,
            ),
            credential_binding=binding,
        )
        network_policies[f"mcp-{name}"] = NetworkPolicy(
            name=f"MCP server {name}", endpoints=(endpoint,), binaries=binaries
        )
    for (host, port), bindings in sorted(bindings_by_host.items()):
        if len(bindings) > 1:
            findings.append(
                Finding(
                    Severity.WARNING,
                    f"hosts.{host}",
                    f"servers on {host}:{port} bind different credentials; OpenShell may reject "
                    "endpoints on one host and port that disagree on credential binding",
                )
            )
    if project.mcp.servers:
        findings.append(
            Finding(
                Severity.INFO,
                "mcp.servers",
                "the fence checks servers by name; address-based floor and ceiling entries are "
                "applied by the MCP proxy, which checks the addresses it connects to",
            )
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
                Severity.WARNING,
                "mcp.proxy.worker_tools",
                "looser at the fence: OpenShell cannot tell callers apart, so the narrower "
                "worker_tools list is applied only by the MCP proxy",
            )
        )
    return Compiled(
        policy=OpenShellPolicy(network_policies=network_policies),
        findings=tuple(findings),
        policy_digest=policy.digest,
    )


_MCP_REVISIONS: Final = frozenset({"2025-03-26", "2025-06-18", "2025-11-25", "2026-07-28"})
_STRICT_TOOL_NAME_RE: Final = re.compile(r"[A-Za-z0-9_.*-]{1,128}")


def _versions(
    server: McpServer, target: McpProxyTarget, subject: str, findings: list[Finding]
) -> tuple[str, ...]:
    """The MCP revisions OpenShell admits for a server: its own revision when it names one."""
    if server.revision not in _MCP_REVISIONS:
        findings.append(
            Finding(
                Severity.INFO,
                f"{subject}.revision",
                f"{server.revision!r} is not an MCP protocol revision; the endpoint admits the "
                f"targets' revisions {list(target.versions)}",
            )
        )
        return target.versions
    if server.revision not in target.versions:
        findings.append(
            Finding(
                Severity.WARNING,
                f"{subject}.revision",
                f"the server speaks {server.revision}, which is not among the revisions clients "
                f"send {list(target.versions)}; OpenShell refuses the others with 403",
            )
        )
    return (server.revision,)


def _collapse(pattern: str) -> str:
    # Runs of '*' mean the same as one '*' here, while '**' means something else to
    # OpenShell; collapse them before compiling.
    return re.sub(r"\*+", "*", pattern)


def _openshell_matches(pattern: str, name: str) -> bool:
    """OpenShell's tool glob: '.' separates, '*' stays within it, a lone '**' matches all."""
    if pattern == "**":
        return True
    pieces = re.split(r"(\*)", pattern)
    regex = "".join("[^.]*" if piece == "*" else re.escape(piece) for piece in pieces)
    return re.fullmatch(regex, name) is not None


def _tool_rules(
    server: McpServer, default: Action, subject: str, findings: list[Finding]
) -> tuple[tuple[Rule, ...], tuple[DenyEntry, ...]]:
    """Translate tool lists into OpenShell rules that are never looser than the project's.

    OpenShell denies tools no allow entry names, and any matching deny entry wins.
    Escalated tools are denied at the fence.
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
    for pattern in (*exact_allows, *glob_allows):
        if not _STRICT_TOOL_NAME_RE.fullmatch(prefix + pattern):
            findings.append(
                Finding(
                    Severity.WARNING,
                    f"{subject}.tools",
                    f"{prefix + pattern} does not fit OpenShell's tool names (letters, digits, "
                    "_ . - and at most 128 characters), so calls to it are refused (stricter)",
                )
            )
    rules = [Rule(allow=AllowEntry(method=method)) for method in sorted(MCP_PROTOCOL_METHODS)]
    # A lone '*' without a prefix may admit every name ('**') only when no deny pattern
    # exists; '**' then subsumes every other allow entry, which OpenShell would reject
    # beside it.
    everything = "*" in glob_allows and not prefix and not blocking_globs
    if everything:
        rules.append(Rule(allow=AllowEntry(method="tools/call", tool="**")))
    else:
        if exact_allows:
            names = tuple(prefix + pattern for pattern in exact_allows)
            rules.append(Rule(allow=AllowEntry(method="tools/call", tool=AnyOf(any=names))))
        rules.extend(
            Rule(allow=AllowEntry(method="tools/call", tool=prefix + pattern))
            for pattern in glob_allows
        )
    deny_rules: list[DenyEntry] = []
    if glob_allows:
        # Only an allow pattern can admit a tool the project blocks, so deny entries
        # are needed only when there is one.
        deny_rules = [DenyEntry(method="tools/call", tool=prefix + pattern) for pattern in blocking]
        if prefix:
            # '*' also matches nothing, so 'portal-*' would admit a tool named just
            # 'portal-', which the project refuses.
            deny_rules.append(DenyEntry(method="tools/call", tool=prefix))
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
                if "*" in pattern and _openshell_matches(prefix + pattern, prefix + name):
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
                "escalated tools are always denied at this fence, even after approval: approved "
                "grants are not compiled in yet (stricter)",
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
    if not providers:
        return None  # literal, non-secret headers only
    if len(providers) > 1:
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
