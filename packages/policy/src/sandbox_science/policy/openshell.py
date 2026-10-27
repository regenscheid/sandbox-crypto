# SPDX-License-Identifier: MIT
"""Compile a project policy into NVIDIA OpenShell v0.1.5 network policy.

Two sandboxes need OpenShell policy (docs/POLICY.md, "Mapping to enforcement"):

* A research sandbox's HTTP and git traffic goes through OpenShell's own proxy,
  which terminates TLS, injects provider credentials and logs. The compiled
  policy admits the destinations the project could ever reach, and attaches the
  platform's policy middleware to every host: OpenShell calls it for each
  admitted request, before injecting credentials, and the middleware decides
  with this library. The package cache is an inspected endpoint too; the
  middleware recognizes it by host and port. Research code has no MCP access.
  Nothing is excluded from the middleware, because OpenShell selects middleware
  by host alone. Provider credentials are bound
  here too, to the hosts the project names for each provider, so that OpenShell
  substitutes them nowhere else.
* The orchestrator's own sandbox (Z2 in the threat model), the agent runtime's,
  reaches the worker API, the model gateway and the control capability service; no
  project policy applies to those. On the direct MCP path (D98) it also reaches the
  declared MCP servers, through ``protocol: mcp`` endpoints whose native rules allow
  and deny tools by name and the policy middleware, which decides the rest
  (``compile_agent_mcp``). The MCP proxy, whose sandbox had those endpoints before,
  retired on 2026-10-26.

Every document also carries the sections OpenShell fixes when the sandbox is
created. Its filesystem section lists OpenShell's baseline system paths itself,
since the supervisor adds them only where they exist on its own host, which is
macOS for the Mac's VM driver, and the paths the deployment's image adds; a
research sandbox's also lists the devices research code needs (pseudo-terminals,
shared memory). Its Landlock section makes that confinement a hard requirement.

Everything the compiler cannot express exactly is reported, and where the
compiled policy differs from the project policy it is stricter. The compiler
also reports a ``packages.opam`` action that the network rules undo: opam
downloads an archive from its own URL whenever the package cache does not
deliver it.
"""

from __future__ import annotations

import ipaddress
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from os import PathLike
from typing import Annotated, Final, Literal
from urllib.parse import urlsplit

import yaml
from pydantic import AfterValidator, BaseModel, ConfigDict, Field, StringConstraints

from .ceiling import PoolCeiling
from .classification import (
    ServerClassification,
    class_action,
    classified_names,
    possible_classes,
)
from .decision import Action
from .effects import EffectClass
from .evaluate import (
    DEFAULT_FLOOR,
    MCP_PROTOCOL_METHODS,
    evaluate_request,
    host_reach,
    registry_reason,
    reserved_host_reason,
    tool_pattern_matches,
)
from .floor import HardFloor
from .hosts import (
    HostKind,
    HostPattern,
    address_variants,
    floor_addresses,
    floor_reason,
    is_public,
    normalize_host,
    parse_ip,
)
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
    ProjectPolicy,
    StrictModel,
    ToolRules,
)
from .operations import HttpRequest

CONTROL_PLANE_PORTS: Final = frozenset({2379, 2380, 6443, 10250, 10255})
"""Ports OpenShell blocks on exact-host endpoints (etcd, Kubernetes API, kubelet)."""

CREDENTIAL_PORT: Final = 443
"""The only port credentials are bound to. OpenShell substitutes them there whatever the
transport, so the policy middleware refuses requests OpenShell reports as plain HTTP."""

SANDBOX_DEVICES_READ_ONLY: Final = ("/dev/random",)
SANDBOX_DEVICES_READ_WRITE: Final = (
    "/dev/full",
    "/dev/ptmx",
    "/dev/pts",
    "/dev/shm",  # noqa: S108 - a path Landlock lets the sandbox open, not a temporary file
    "/dev/tty",
    "/dev/zero",
)
"""Devices beyond OpenShell's baseline paths (``/dev/null``, ``/dev/urandom``) that research
code needs: pseudo-terminals for pexpect-driven tools such as Sage's GAP, gp and Singular
interfaces, and ``/dev/shm`` for Python's multiprocessing (lab 5). Without them, Landlock
refuses every open."""

RESEARCH_IMAGE_READ_ONLY: Final = ("/opt/sandbox-science-frozen", "/opt/sandbox-science-base")
"""What the research images keep out of the workspace they seed each environment with:
the frozen trees (docs/IMAGES.md, "Prefix layout"), GAP's, Giac's and Maxima's data, ECL's
library and the compiler toolchain, and the base environment under the workspace's layer
("The environment outside the workspace"), which the layer links to. Every research sandbox
may read them, and none may write them: no filesystem section may name a path, or a directory
above or below it, writable. OpenShell skips a path an image lacks, such as an earlier
image's base."""

BASELINE_READ_ONLY: Final = ("/usr", "/lib", "/etc", "/proc", "/var/log", "/dev/urandom")
BASELINE_READ_WRITE: Final = ("/tmp", "/dev/null")  # noqa: S108 - a Landlock path, not a temporary file
"""OpenShell's baseline paths for a sandbox with network rules (``PROXY_BASELINE_*`` in its
supervisor, unchanged from v0.1.3 to v0.1.5), less ``/app``, which the images lack. The
supervisor adds each one only if it exists where the supervisor runs. With the macOS VM
driver that is the host, which has no ``/lib`` or ``/proc``, so every compiled section
lists them itself; on Linux the supervisor then adds nothing it would not have added."""

BASELINE_UNLISTED: Final = 1
"""Baseline paths the supervisor may still add to a compiled section (``/app``)."""

MAX_FILESYSTEM_PATHS: Final = 256
"""OpenShell's limit on ``read_only`` plus ``read_write`` paths, its baseline included."""

OPAM_SOURCES: Final = (
    "https://github.com/ocaml/ocamlfind/archive/refs/tags/findlib-1.9.8.tar.gz",
    "https://github.com/jasmin-lang/jasmin/releases/download/v2026.09.0/"
    "jasmin-compiler-v2026.09.0.tar.bz2",
    "https://raw.githubusercontent.com/ocaml/ocaml/8c17fd444391392c5f9801241374d749c080c1a8/"
    "ocaml-variants.install",
    "https://gitlab.com/dannywillems/ocaml-bls12-381/-/archive/0.4.3/ocaml-bls12-381-0.4.3.tar.bz2",
    "https://ocaml.janestreet.com/ocaml-core/v0.16/files/accessor_base-v0.16.0.tar.gz",
)
"""Archive URLs from opam-repository's opam files, which opam downloads itself when the
package cache does not deliver the archive: GitHub archives, releases and raw files (87% of
the url sections in October 2026), GitLab, and another public host."""

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
    tls: Literal["skip"] | None = None
    """``skip`` relays a ``tcp`` endpoint's TLS without terminating it, for a client that
    completes TLS itself against a CA it pins."""
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


class FilesystemPolicy(_OpenShellModel):
    """Paths Landlock lets sandbox processes open, beyond the workdir when it is included.

    OpenShell's supervisor adds those of its baseline paths that exist on its host and
    are not listed when a sandbox with network rules starts; compiled sections list them.
    """

    include_workdir: bool = True
    read_only: tuple[str, ...] = ()
    read_write: tuple[str, ...] = ()


class LandlockPolicy(_OpenShellModel):
    """Whether a sandbox may start when Landlock cannot apply its filesystem section.

    ``best_effort``, OpenShell's default, logs a warning and runs the sandbox without
    filesystem restrictions when the kernel lacks Landlock or ``restrict_self()`` fails.
    ``hard_requirement`` refuses to start it. Either way a sandbox, which applies the
    rules as its own user, skips a listed path that is missing from the image, which can
    only narrow what it may open (Mac test, 2026-10-21).
    """

    compatibility: Literal["best_effort", "hard_requirement"]


HARD_LANDLOCK: Final = LandlockPolicy(compatibility="hard_requirement")


class OpenShellPolicy(_OpenShellModel):
    """An OpenShell policy document.

    Create a sandbox with the whole document. OpenShell fixes the filesystem and
    Landlock sections then, and refuses a later revision that removes a filesystem path
    or changes the Landlock section. Apply a revision with ``with_static()`` and the
    sections the sandbox reports (``openshell policy get --base``), with ``policy set``,
    so that it replaces the sandbox's network sections entirely: an entry left over from
    an earlier compilation could admit what the policy no longer allows. Process
    settings belong to the environment layer (D48).
    """

    version: Literal[1] = 1
    filesystem_policy: FilesystemPolicy | None = None
    landlock: LandlockPolicy | None = None
    network_policies: dict[str, NetworkPolicy]
    network_middlewares: dict[str, NetworkMiddleware] | None = None

    def to_yaml(self) -> str:
        """Render deterministic YAML (OpenShell's policy hash depends on list order).

        Strings that a YAML 1.2 parser could read as numbers, booleans or null are
        quoted; PyYAML only quotes the YAML 1.1 forms.
        """
        document = self.model_dump(mode="json", exclude_none=True)
        return yaml.dump(document, Dumper=_Dumper, sort_keys=False, default_flow_style=False)

    def with_static(
        self, filesystem: FilesystemPolicy, landlock: LandlockPolicy | None
    ) -> OpenShellPolicy:
        """The document with a running sandbox's filesystem and Landlock sections, for
        revising it. A sandbox created before compiled policies had a Landlock section
        reports none, and keeps none."""
        return self.model_copy(update={"filesystem_policy": filesystem, "landlock": landlock})


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

    ``policy_digest`` identifies the project policy revision it came from. For a
    research sandbox, ``hosts`` and ``suffixes`` are the host names its endpoints
    name, exactly and as ``**.suffix``, and ``admitted`` the entries admitted on demand
    among them: hosts, and hosts with another port (``admission_entry``).
    """

    policy: OpenShellPolicy
    findings: tuple[Finding, ...]
    policy_digest: str
    hosts: frozenset[str] = frozenset()
    suffixes: tuple[str, ...] = ()
    admitted: tuple[str, ...] = ()

    def names(self, host: str) -> bool:
        """Whether a sandbox under this policy can resolve ``host``: an endpoint names it."""
        try:
            name = normalize_host(host)
        except ValueError:
            return False
        return name in self.hosts or any(name.endswith(f".{suffix}") for suffix in self.suffixes)

    def admits(self, host: str, port: int) -> bool:
        """Whether an endpoint admits connections to ``host`` on ``port``."""
        try:
            name = normalize_host(host)
        except ValueError:
            return False
        for entry in self.policy.network_policies.values():
            for endpoint in entry.endpoints:
                if endpoint.host is None or port not in endpoint.ports:
                    continue
                if endpoint.host == name or (
                    endpoint.host.startswith("**.") and name.endswith(endpoint.host[2:])
                ):
                    return True
        return False


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
    relay_tls: bool = False
    """The client completes TLS itself against a private CA it pins, so OpenShell relays the
    connection (``protocol: tcp``, ``tls: skip``) and checks only host, port and binary.
    Its inspecting proxy would present OpenShell's own certificate to the client and
    could not verify the service's (KRB, the cluster's control and worker Services)."""


BinaryPattern = Annotated[str, StringConstraints(pattern=r"^/[!-~]*$")]
"""An absolute executable path or a '/'-delimited glob, in printable ASCII."""


def _sandbox_path(value: str) -> str:
    if not value.startswith("/") or len(value.encode()) > 4096:
        raise ValueError("a sandbox path is absolute and at most 4096 bytes")
    if any(part in {".", ".."} for part in value.split("/")) or "//" in value:
        raise ValueError("a sandbox path has no '.', '..' or empty segments")
    if value != "/" and value.endswith("/"):
        raise ValueError("a sandbox path has no trailing '/'")
    return value


SandboxPath = Annotated[str, StringConstraints(pattern=r"^/[!-~]*$"), AfterValidator(_sandbox_path)]
"""An absolute path inside the sandbox, in printable ASCII."""


def _writable_path(value: str) -> str:
    if value == "/":
        raise ValueError("the root cannot be writable")
    for frozen in RESEARCH_IMAGE_READ_ONLY:
        if value == frozen or frozen.startswith(f"{value}/") or value.startswith(f"{frozen}/"):
            raise ValueError(f"{frozen} holds the research images' packages and stays read-only")
    return value


class SandboxFilesystem(StrictModel):
    """Paths the deployment's image adds, such as a Conda prefix outside the workdir (D48).

    The compiler adds the devices research code needs, and OpenShell its baseline.
    """

    read_only: tuple[SandboxPath, ...] = ()
    read_write: tuple[Annotated[SandboxPath, AfterValidator(_writable_path)], ...] = ()


McpVersion = Literal["2025-03-26", "2025-06-18", "2025-11-25", "2026-07-28"]


class SandboxTarget(StrictModel):
    """Platform services for research sandboxes.

    ``middleware`` is the name under which the policy middleware service is
    registered in the gateway configuration. ``binaries`` defaults to every
    executable: research code runs arbitrary programs, and the middleware and
    services decide what they may do. ``filesystem`` names paths outside the
    sandbox's workdir that its image provides.
    """

    middleware: Name
    package_cache: ServiceEndpoint
    mcp_proxy: ServiceEndpoint | None = None
    """Where the MCP proxy listened, retired with it (D98, 2026-10-26): research code has
    no MCP access. Read, and kept out of the hosts a research sandbox may be admitted to."""
    binaries: tuple[BinaryPattern, ...] = Field(default=("/**",), min_length=1)
    filesystem: SandboxFilesystem = SandboxFilesystem()


class McpProxyTarget(StrictModel):
    """Settings for the MCP proxy's own sandbox, retired with the proxy (D98, 2026-10-26).

    A targets file may still have them; nothing compiles them any more.
    """

    binaries: tuple[BinaryPattern, ...] = Field(min_length=1)
    versions: tuple[McpVersion, ...] = Field(default=("2025-11-25",), min_length=1)
    max_body_bytes: Annotated[int, Field(gt=0)] | None = None
    filesystem: SandboxFilesystem = SandboxFilesystem()
    """Paths the proxy's image provides outside the workdir, such as its interpreter's prefix."""


class AgentMcpTarget(StrictModel):
    """Settings for a sandbox whose agent calls MCP servers directly (the D98 prototype).

    ``middleware`` names the policy middleware's registration, ``project`` the project
    whose policy and approvals apply, and ``caller`` the name permission requests give
    the sandbox's calls, as the MCP proxy names its callers. ``binaries`` names the
    interpreter that runs the agent's MCP client.
    """

    middleware: Name
    project: Name
    caller: Name = "agent-runtime"
    binaries: tuple[BinaryPattern, ...] = Field(min_length=1)
    versions: tuple[McpVersion, ...] = Field(default=("2025-11-25",), min_length=1)
    max_body_bytes: Annotated[int, Field(gt=0)] | None = None
    filesystem: SandboxFilesystem = SandboxFilesystem()


class OrchestratorTarget(StrictModel):
    """Settings for the orchestrator's own sandbox (Z2): the services it may reach.

    ``binaries`` names the interpreter that runs the harness, so that nothing else in
    that sandbox reaches the worker API.
    """

    worker_api: ServiceEndpoint
    model_gateway: ServiceEndpoint | None = None
    mcp_proxy: ServiceEndpoint | None = None
    """Where the harness reached the MCP proxy, retired with it (D98, 2026-10-26): read and
    ignored. The agent runtime's MCP egress is compiled from the project policy
    (``compile_agent_mcp``)."""
    state_service: ServiceEndpoint | None = None
    """The scoped control capability service; never a database or filesystem endpoint."""
    binaries: tuple[BinaryPattern, ...] = Field(min_length=1)
    filesystem: SandboxFilesystem = SandboxFilesystem()
    """Paths the harness's image provides outside the workdir, such as its interpreter's prefix."""


class OpenShellTargets(StrictModel):
    """A targets file: where platform services run for one deployment."""

    api_version: Literal["sandbox-science/v0alpha1"] = Field(
        default=API_VERSION, alias="apiVersion"
    )
    kind: Literal["OpenShellTargets"] = "OpenShellTargets"
    sandbox: SandboxTarget
    mcp_proxy: McpProxyTarget | None = None
    """The retired MCP proxy's sandbox (D98): read and ignored."""
    orchestrator: OrchestratorTarget | None = None


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


def admission_ranges(
    policy: LoadedPolicy, ceiling: PoolCeiling, floor: HardFloor = DEFAULT_FLOOR
) -> tuple[str, ...]:
    """The public address ranges, less every address the library denies whatever the name.

    Those are the floor's metadata addresses, the deployment's administration
    endpoints, the pool ceiling's denied addresses and MCP servers declared by
    address, with the IPv4 addresses each embeds. The policy middleware sees only
    host names, so these ranges, as ``allowed_ips``, are what keeps a name that
    resolves to such an address from reaching it.
    """
    return _public_ranges_less(
        [
            *_denied_hosts(ceiling, floor),
            *(server.host for server in policy.policy.mcp.servers.values()),
        ]
    )


def mcp_server_ranges(ceiling: PoolCeiling, floor: HardFloor = DEFAULT_FLOOR) -> tuple[str, ...]:
    """The addresses an MCP server's name may resolve to, as a sandbox's fence checks.

    The public address ranges, less the floor's metadata addresses, the deployment's
    administration endpoints and the pool ceiling's denied addresses. Inside OpenShell
    every name resolves to a synthetic address, so a client's own check cannot see
    where a name leads; these ranges, as ``allowed_ips``, apply the floor and the
    ceiling to the addresses OpenShell dials (lab 13).
    """
    return _public_ranges_less(_denied_hosts(ceiling, floor))


def _denied_hosts(ceiling: PoolCeiling, floor: HardFloor) -> list[str]:
    """Hosts the floor or the ceiling denies by name; the addresses among them matter here."""
    return [
        *floor.admin_hosts,
        *(p.value for p in ceiling.network.deny_hosts if p.kind is HostKind.EXACT),
    ]


def _public_ranges_less(hosts: Sequence[str]) -> tuple[str, ...]:
    """The public address ranges, less the floor's metadata addresses and the IP addresses
    among ``hosts``, with the IPv4 addresses each embeds."""
    addresses = set(floor_addresses())
    for host in hosts:
        address = parse_ip(host)
        if address is not None:
            addresses.update(address_variants(address))
    ipv4 = tuple(
        ipaddress.IPv4Network(a) for a in addresses if isinstance(a, ipaddress.IPv4Address)
    )
    ipv6 = tuple(
        ipaddress.IPv6Network(a) for a in addresses if isinstance(a, ipaddress.IPv6Address)
    )
    return tuple(
        str(network)
        for network in (
            *_without(ipaddress.IPv4Network("0.0.0.0/0"), (*_NON_PUBLIC_IPV4, *sorted(ipv4))),
            *_without(ipaddress.IPv6Network("2000::/3"), (*_NON_PUBLIC_IPV6, *sorted(ipv6))),
        )
    )


def public_address_ranges() -> tuple[str, ...]:
    """Public unicast address space, for the ``allowed_ips`` of endpoints for public hosts.

    IPv4 without its special-purpose blocks, and IPv6 global unicast
    (``2000::/3``) without documentation, 6to4 and IETF protocol assignments.
    Every address in these ranges is public by the library's rule, so OpenShell
    admits no address the library would refuse to a wildcard rule.
    """
    ipv4 = _without(ipaddress.IPv4Network("0.0.0.0/0"), _NON_PUBLIC_IPV4)
    ipv6 = _without(ipaddress.IPv6Network("2000::/3"), _NON_PUBLIC_IPV6)
    return tuple(str(network) for network in (*ipv4, *ipv6))


def _wildcard_patterns(patterns: Iterable[HostPattern], findings: list[Finding]) -> list[str]:
    """OpenShell host patterns for the rules' ``*.suffix`` wildcards.

    The library's ``*.suffix`` matches names at any depth below the suffix, which
    is OpenShell's ``**.suffix``. OpenShell requires three labels in a wildcard
    host, so a wildcard over a top-level domain cannot be named; wildcards never
    grant non-public names, so those are not named either.
    """
    hosts: set[str] = set()
    for pattern in patterns:
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


def _reachable(project: ProjectPolicy) -> list[HttpRule | GitRule]:
    return [rule for rule in project.network.rules if rule.action is not Action.DENY]


def _ports(reachable: Sequence[HttpRule | GitRule]) -> set[int]:
    """The ports every endpoint admits: the schemes' defaults and every port a rule names.

    A rule for ``ports: any`` names none: OpenShell endpoints list their ports, so a
    host's other ports are admitted on demand, one at a time (``admission_entry``).
    """
    return {80, 443} | {
        port for rule in reachable if rule.match.ports != "any" for port in rule.match.ports
    }


def _any_port(reachable: Sequence[HttpRule | GitRule]) -> bool:
    """Whether some rule that could allow a request matches every port (``ports: any``)."""
    return any(rule.match.ports == "any" for rule in reachable)


def admitted_ports(policy: LoadedPolicy) -> tuple[int, ...]:
    """The ports every compiled egress endpoint admits; other ports are admitted on demand."""
    return tuple(sorted(_ports(_reachable(policy.policy)) - CONTROL_PLANE_PORTS))


def admission_entry(policy: LoadedPolicy, host: str, port: int) -> str:
    """How ``compile_sandbox``'s ``admitted`` names a host admitted on demand on ``port``.

    A port every endpoint admits needs only the host: ``example.org``. Another port,
    which only a ``ports: any`` rule reaches, is named with it: ``example.org:8080``,
    ``[2001:db8::1]:8080``.
    """
    name = normalize_host(host)
    if port in admitted_ports(policy):
        return name
    return f"[{name}]:{port}" if ":" in name else f"{name}:{port}"


def _parse_entry(entry: str) -> tuple[str, int | None]:
    """An ``admitted`` entry's normalized host and port (None: the admitted ports).

    Raises ValueError for one that is not a host, or a host and port.
    """
    port: int | None = None
    host = entry
    if entry.startswith("["):
        address, separator, rest = entry[1:].partition("]")
        if not separator:
            raise ValueError("an unclosed '['")
        host = address
        if rest:
            if not rest.startswith(":"):
                raise ValueError("text after ']' that is not a port")
            port = _entry_port(rest[1:])
    elif entry.count(":") == 1:
        host, _, number = entry.partition(":")
        port = _entry_port(number)
    return normalize_host(host), port


def _entry_port(text: str) -> int:
    if not text.isascii() or not text.isdigit() or not 0 < int(text) < 65536:
        raise ValueError(f"{text!r} is not a port")
    return int(text)


def _wide(project: ProjectPolicy, reachable: Sequence[HttpRule | GitRule]) -> bool:
    """Whether the rules reach hosts they do not name, before any lockdown.

    Unmatched requests take defaults.action, so anything but deny does, as does
    every wildcard rule.
    """
    return project.defaults.action is not Action.DENY or any(
        pattern.kind is not HostKind.EXACT for rule in reachable for pattern in rule.match.hosts
    )


def _open_internet(project: ProjectPolicy, reachable: Sequence[HttpRule | GitRule]) -> bool:
    """Whether requests can reach hosts the compiled policy does not name.

    Under ``network: allowlist`` (D95) only listed hosts are reachable, so unnamed ones
    are reached only through a listed domain or address range, or when unlisted hosts
    escalate instead of being denied.
    """
    if not _wide(project, reachable):
        return False
    lockdown = project.lockdown
    return (
        lockdown.network != "allowlist"
        or lockdown.unlisted is Action.ESCALATE
        or any(
            entry.pattern is None or entry.pattern.kind is not HostKind.EXACT
            for entry in lockdown.hosts
        )
    )


def _listed_hosts(
    policy: LoadedPolicy,
    reachable: Sequence[HttpRule | GitRule],
    ports: Iterable[int],
    findings: list[Finding],
    *,
    ceiling: PoolCeiling,
    floor: HardFloor,
) -> tuple[list[str], list[HostPattern]]:
    """The exact hosts and the wildcards that endpoints name, under the network lockdown.

    Without an allowlist, those the rules name. With one, the rules' exact hosts that it
    lists, its own exact hosts that some request could reach, and its domains, plus the
    rules' wildcards when unlisted hosts escalate.
    """
    project = policy.policy
    lockdown = project.lockdown
    named = {
        pattern.value
        for rule in reachable
        for pattern in rule.match.hosts
        if pattern.kind is HostKind.EXACT
    }
    patterns = [pattern for rule in reachable for pattern in rule.match.hosts]
    if lockdown.network != "allowlist":
        return sorted(named), patterns
    unlisted = sorted(host for host in named if not lockdown.listed(host))
    if unlisted and lockdown.unlisted is Action.DENY:
        findings.append(
            Finding(
                Severity.INFO,
                "lockdown.hosts",
                "not admitted, since the network allowlist does not list them: "
                + ", ".join(unlisted),
            )
        )
        named -= set(unlisted)
    wide = _wide(project, reachable)
    for entry in lockdown.hosts:
        pattern = entry.pattern
        if pattern is None or pattern.kind is not HostKind.EXACT or pattern.value in named:
            continue
        if any(
            host_reach(pattern.value, port, policy, ceiling=ceiling, floor=floor).action
            is not Action.DENY
            for port in ports
        ):
            named.add(pattern.value)
    listed = (
        [entry.pattern for entry in lockdown.hosts if entry.pattern is not None] if wide else []
    )
    if lockdown.unlisted is Action.ESCALATE:
        listed += patterns
    return sorted(named), listed


def _admission_refusal(
    policy: LoadedPolicy,
    host: str,
    ports: Iterable[int],
    service_hosts: set[str],
    *,
    ceiling: PoolCeiling,
    floor: HardFloor,
) -> str | None:
    """Why a normalized host must not be admitted on demand on any of ``ports``, or None."""
    if not is_public(host):
        return f"{host} is not public; only a rule naming it exactly can reach it"
    if host in service_hosts:
        return f"{host} is a platform service host"
    refusal: str | None = None
    for port in ports:
        decision = host_reach(host, port, policy, ceiling=ceiling, floor=floor)
        if decision.action is not Action.DENY:
            return None
        refusal = refusal or decision.reason
    return f"the policy allows no request to {host}: {refusal}"


def admission_refusal(
    policy: LoadedPolicy,
    target: SandboxTarget,
    host: str,
    port: int,
    *,
    ceiling: PoolCeiling,
    floor: HardFloor = DEFAULT_FLOOR,
) -> str | None:
    """Why on-demand admission must not name ``host`` after a refused connection to ``port``.

    Returns None when it may (D69). A sandbox resolves only the host names its
    compiled policy names, so OpenShell refuses a connection to any other host and
    drafts a proposal for it. The worker then admits the host by compiling it into
    ``compile_sandbox``'s ``admitted`` hosts, when the policy reaches hosts it does
    not name (through a wildcard rule or a non-deny default), the port is one the
    compiled policy admits, and some request to the host there could be allowed or
    escalated. The policy middleware still decides every request. A host the policy
    names (``names_host``) passes too; admitting it changes nothing.
    """
    try:
        name = normalize_host(host)
    except ValueError as exc:
        return f"{host!r} is not a host name: {exc}"
    project = policy.policy
    reachable = _reachable(project)
    ports = sorted(_ports(reachable) - CONTROL_PLANE_PORTS)
    other_port = port not in ports
    if not _open_internet(project, reachable) and not (other_port and _any_port(reachable)):
        return "the policy reaches only the hosts it names, which are admitted already"
    if other_port and (port in CONTROL_PLANE_PORTS or not _any_port(reachable)):
        return f"port {port} is not admitted; the policy admits ports {', '.join(map(str, ports))}"
    return _admission_refusal(
        policy, name, (port,), _service_hosts(target), ceiling=ceiling, floor=floor
    )


def _service_hosts(target: SandboxTarget) -> set[str]:
    """The platform services' hosts, which no research sandbox is admitted to: the package
    cache's, and the retired MCP proxy's where the targets file still names it."""
    hosts = {target.package_cache.host}
    if target.mcp_proxy is not None:
        hosts.add(target.mcp_proxy.host)
    return hosts


def compile_sandbox(
    policy: LoadedPolicy,
    target: SandboxTarget,
    *,
    ceiling: PoolCeiling,
    floor: HardFloor = DEFAULT_FLOOR,
    admitted: Iterable[str] = (),
) -> Compiled:
    """Compile a research sandbox's policy: admission endpoints plus the policy middleware.

    OpenShell admits the destinations the project could ever reach and calls the
    policy middleware for every admitted request; the middleware decides with
    this library. ``admitted`` names hosts the worker admitted on demand
    (``admission_refusal``); they are checked again and get endpoints like the
    hosts the policy names. Raises CompileError when the result would not load.
    """
    project = policy.policy
    findings: list[Finding] = []
    binaries = tuple(Binary(path=path) for path in target.binaries)
    reachable = _reachable(project)

    ports = _ports(reachable)
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

    open_internet = _open_internet(project, reachable)
    exact_hosts, host_patterns = _listed_hosts(
        policy, reachable, admitted_ports, findings, ceiling=ceiling, floor=floor
    )
    endpoints: list[Endpoint] = []
    # OpenShell's policy DNS answers only for host names that an endpoint names,
    # and a VM sandbox has no other route, so every reachable host needs an
    # endpoint. Public hosts get the public address ranges, as the library's
    # wildcard rule allows only public destinations; hosts a rule names exactly
    # may be private, as in the library.
    allowed_ips = admission_ranges(policy, ceiling, floor) if _wide(project, reachable) else None
    service_hosts = _service_hosts(target)
    # A closed policy admits only hosts its rules name, so it binds only those.
    bound = _bound_hosts(
        policy,
        ceiling,
        floor,
        service_hosts,
        None if open_internet else set(exact_hosts),
        findings,
    )

    # Other ports of hosts admitted on demand (``ports: any`` rules), by host.
    extra_ports: dict[str, set[int]] = {}

    def egress_ports(host: str, *, default: bool = True) -> tuple[int, ...]:
        # A bound host's other endpoints leave the credential port out, so that
        # none competes with its credential endpoint at equal path specificity.
        # Port 80 is always admitted, so some port remains.
        ports = set(admitted_ports) if default else set[int]()
        ports |= extra_ports.get(host, set())
        if host in bound:
            ports.discard(CREDENTIAL_PORT)
        return tuple(sorted(ports))

    def egress(host: str, *, default: bool = True) -> Endpoint:
        return Endpoint(
            host=host,
            ports=egress_ports(host, default=default),
            protocol="rest",
            enforcement="enforce",
            access="full",
            allowed_ips=allowed_ips if is_public(host) else None,
        )

    named_hosts: list[str] = []
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
        named_hosts.append(host)
    wildcards = _wildcard_patterns(host_patterns, findings) if open_internet else []
    suffixes = tuple(host.removeprefix("**.") for host in wildcards)

    # Entries the worker admitted on demand: hosts the policy reaches without naming
    # them, and other ports of hosts that ``ports: any`` rules reach.
    any_port = _any_port(reachable)
    on_demand: list[str] = []
    entries: list[str] = []
    for raw in sorted(set(admitted)):
        try:
            host, port = _parse_entry(raw)
        except ValueError as exc:
            findings.append(
                Finding(Severity.WARNING, f"hosts.{raw}", f"not admitted on demand: {exc}")
            )
            continue
        if port is not None and port not in admitted_ports:
            if port in CONTROL_PLANE_PORTS or not any_port:
                reason: str | None = f"port {port} is not admitted"
            else:
                reason = _admission_refusal(
                    policy, host, (port,), service_hosts, ceiling=ceiling, floor=floor
                )
            if reason is not None:
                findings.append(
                    Finding(Severity.WARNING, f"hosts.{raw}", f"not admitted on demand: {reason}")
                )
            elif port not in extra_ports.setdefault(host, set()):
                extra_ports[host].add(port)
                entries.append(admission_entry(policy, host, port))
            continue
        if not open_internet:
            reason = "the policy reaches only the hosts it names"
        else:
            reason = _admission_refusal(
                policy, host, admitted_ports, service_hosts, ceiling=ceiling, floor=floor
            )
        if reason is not None:
            findings.append(
                Finding(Severity.WARNING, f"hosts.{host}", f"not admitted on demand: {reason}")
            )
        elif (
            host not in on_demand
            and host not in named_hosts
            and not any(host.endswith(f".{suffix}") for suffix in suffixes)
        ):
            on_demand.append(host)
            entries.append(host)
    if on_demand:
        findings.append(
            Finding(
                Severity.INFO,
                "network",
                f"{len(on_demand)} hosts admitted on demand, limited to public address ranges: "
                + ", ".join(on_demand),
            )
        )
    other_ports = sorted(entry for entry in entries if entry not in on_demand)
    if other_ports:
        findings.append(
            Finding(
                Severity.INFO,
                "network",
                f"{len(other_ports)} other ports admitted on demand: " + ", ".join(other_ports),
            )
        )
    # Hosts admitted on other ports only, which no endpoint names otherwise.
    port_only = sorted(
        host for host in extra_ports if host not in named_hosts and host not in on_demand
    )

    endpoints += [egress(host) for host in named_hosts]
    if open_internet:
        # Overlapping OpenShell endpoints must agree on allowed_ips: a wildcard's
        # endpoint and those of the public hosts under it carry the same ranges.
        endpoints += [egress(host) for host in wildcards]
        findings.append(
            Finding(
                Severity.INFO,
                "network",
                "endpoints for public hosts are limited to public address ranges, less the "
                "addresses the library always denies (cloud metadata, administration "
                "endpoints, the ceiling's denied addresses, MCP servers), so OpenShell refuses "
                "names that resolve to them, as the library's wildcard rule does",
            )
        )
        if project.defaults.action is not Action.DENY or any(
            pattern.kind is HostKind.ANY for rule in reachable for pattern in rule.match.hosts
        ):
            findings.append(
                Finding(
                    Severity.WARNING,
                    "network",
                    "rules for '*' and the default action reach other hosts and addresses only "
                    "once the worker has admitted them on demand: OpenShell refuses the first "
                    "connection to one and drafts a proposal, which the worker answers (D69, "
                    "stricter)",
                )
            )
    if any_port:
        findings.append(
            Finding(
                Severity.WARNING,
                "network",
                "rules for 'ports: any' reach ports other than "
                + ", ".join(map(str, admitted_ports))
                + " only once the worker has admitted the host on that port on demand (D69, "
                "stricter)",
            )
        )

    cache = target.package_cache
    services = [("package-cache", "package cache", cache)]
    # Research code has no MCP access since the MCP proxy retired (D98, 2026-10-26): no
    # endpoint admits the proxy's address, which the targets file still names.
    findings.append(
        Finding(
            Severity.INFO,
            "mcp.proxy.worker_access",
            "worker code cannot reach MCP servers: no endpoint admits them",
        )
    )
    findings.append(
        Finding(
            Severity.WARNING,
            "services",
            "platform services must have private addresses, so that no endpoint for a public "
            "host reaches them",
        )
    )
    for _, label, service in services:
        under = [s for s in suffixes if service.host.endswith(f".{s}")]
        if under and service.port in admitted_ports:
            raise CompileError(
                f"the {label} at {service.host}:{service.port} is under the wildcard "
                f"**.{under[0]} on a port it admits; OpenShell rejects overlapping endpoints "
                "that differ, so move the service to another port or name"
            )

    network_policies: dict[str, NetworkPolicy] = {}
    if endpoints:
        network_policies["sandbox-science-egress"] = NetworkPolicy(
            name="Sandbox Science egress", endpoints=tuple(endpoints), binaries=binaries
        )
    if on_demand or port_only:
        network_policies["sandbox-science-admitted"] = NetworkPolicy(
            name="Sandbox Science hosts admitted on demand",
            endpoints=(
                *(egress(host) for host in on_demand),
                *(egress(host, default=False) for host in port_only),
            ),
            binaries=binaries,
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
    if bound:
        # The path selector makes each endpoint more specific than the wildcard
        # endpoints that also admit its host on this port, so that OpenShell does
        # not require them to agree on the binding, and selects this endpoint for
        # the host's requests there. Overlapping endpoints must agree on
        # allowed_ips.
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
    opam = _opam_fallback(policy, ceiling, floor)
    if opam is not None:
        findings.append(opam)
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
            filesystem_policy=_sandbox_filesystem(target.filesystem),
            landlock=HARD_LANDLOCK,
            network_policies=network_policies,
            network_middlewares={"sandbox-science-policy": middleware},
        ),
        findings=tuple(findings),
        policy_digest=policy.digest,
        hosts=frozenset(
            endpoint.host
            for entry in network_policies.values()
            for endpoint in entry.endpoints
            if endpoint.host is not None and not endpoint.host.startswith("**.")
        ),
        suffixes=suffixes,
        admitted=tuple(entries),
    )


def _sandbox_filesystem(extra: SandboxFilesystem) -> FilesystemPolicy:
    """The research sandbox's filesystem section: the baseline, the devices it needs, the
    research images' frozen trees, read-only, and the image's paths."""
    return _filesystem(
        (*SANDBOX_DEVICES_READ_ONLY, *RESEARCH_IMAGE_READ_ONLY, *extra.read_only),
        (*SANDBOX_DEVICES_READ_WRITE, *extra.read_write),
    )


def _filesystem(read_only: Iterable[str], read_write: Iterable[str]) -> FilesystemPolicy:
    """OpenShell's baseline paths and these, the workdir included.

    A path listed both ways stays writable. Raises CompileError past OpenShell's limit.
    """
    writable = tuple(dict.fromkeys((*BASELINE_READ_WRITE, *read_write)))
    readable = tuple(
        path for path in dict.fromkeys((*BASELINE_READ_ONLY, *read_only)) if path not in writable
    )
    if len(readable) + len(writable) + BASELINE_UNLISTED > MAX_FILESYSTEM_PATHS:
        raise CompileError(
            f"the sandbox would have more than {MAX_FILESYSTEM_PATHS} filesystem paths"
        )
    return FilesystemPolicy(include_workdir=True, read_only=readable, read_write=writable)


def compile_orchestrator(target: OrchestratorTarget) -> OpenShellPolicy:
    """Compile the orchestrator sandbox's policy: the worker API, the model gateway and the
    control capability service only.

    The orchestrator is semi-trusted (Z2 in the threat model): it chooses actions but
    holds no engine credentials, and its egress goes only to platform services. They
    are inspected HTTP endpoints, with no policy middleware, since no project policy
    governs the orchestrator itself. Its MCP egress, decided by native rules and the
    middleware, is compiled apart (``compile_agent_mcp``) and merged in. The retired MCP
    proxy's address (``mcp_proxy``) is ignored. Raises CompileError when two services
    share an endpoint.
    """
    binaries = tuple(Binary(path=path) for path in target.binaries)
    services = {"worker-api": ("Sandbox Science worker API", target.worker_api)}
    if target.model_gateway is not None:
        services["model-gateway"] = ("Sandbox Science model gateway", target.model_gateway)
    if target.state_service is not None:
        services["state-service"] = ("Sandbox Science control capabilities", target.state_service)
    seen: dict[tuple[str, int], str] = {}
    network_policies: dict[str, NetworkPolicy] = {}
    for key, (name, service) in services.items():
        where = (service.host, service.port)
        if where in seen:
            raise CompileError(
                f"the {seen[where]} and the {key} share {service.host}:{service.port}"
            )
        seen[where] = key
        if service.relay_tls:
            endpoint = Endpoint(
                host=service.host, ports=(service.port,), protocol="tcp", tls="skip"
            )
        else:
            endpoint = Endpoint(
                host=service.host,
                ports=(service.port,),
                protocol="rest",
                enforcement="enforce",
                access="full",
            )
        network_policies[f"sandbox-science-{key}"] = NetworkPolicy(
            name=name, endpoints=(endpoint,), binaries=binaries
        )
    return OpenShellPolicy(
        filesystem_policy=_service_filesystem(target.filesystem),
        landlock=HARD_LANDLOCK,
        network_policies=network_policies,
    )


def _service_filesystem(extra: SandboxFilesystem) -> FilesystemPolicy:
    """The filesystem of a platform service's sandbox: the baseline and its image's paths."""
    return _filesystem(extra.read_only, extra.read_write)


def _bound_hosts(
    policy: LoadedPolicy,
    ceiling: PoolCeiling,
    floor: HardFloor,
    service_hosts: set[str],
    named: set[str] | None,
    findings: list[Finding],
) -> dict[str, str]:
    """Map each host a provider credential may be bound to onto its provider.

    Hosts the floor, the pool ceiling or a platform route rules out are left
    unbound and reported, as are hosts that are not public. When ``named`` is
    given (a closed policy), so are hosts outside it.
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
                or (
                    f"no rule that could allow a request names {host}"
                    if named is not None and host not in named
                    else None
                )
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


def _opam_fallback(policy: LoadedPolicy, ceiling: PoolCeiling, floor: HardFloor) -> Finding | None:
    """Report a ``packages.opam`` action that the network rules undo.

    opam downloads an archive from the package's own URL whenever the package
    cache does not deliver it: when the cache lacks it (404), and when the
    middleware refuses or escalates it (403). The action therefore decides only
    the cache's route, and holds only if the rules stop those downloads too.
    """
    project = policy.policy
    configured = project.packages.opam
    action = project.defaults.action if configured is None else configured.action
    if action is Action.ALLOW:
        return None
    setting = (
        f"packages.opam is {action}"
        if configured is not None
        else f"packages.opam is unset, so opam archives take defaults.action ({action})"
    )
    for url in OPAM_SOURCES:
        decision = evaluate_request(HttpRequest("GET", url), policy, ceiling=ceiling, floor=floor)
        if decision.action in (Action.ALLOW, Action.ALERT):
            allowed_by = f"rule {decision.rule}" if decision.rule else "defaults.action"
            return Finding(
                Severity.WARNING,
                "packages.opam",
                f"{setting}, which does not hold: it decides only the archives opam fetches "
                "through the package cache. When the cache refuses or lacks an archive, opam "
                f"downloads it from the package's own URL instead, and {allowed_by} allows "
                f"that (GET {url}). Deny or escalate those downloads too, or allow opam",
            )
    return None


def compile_agent_mcp(
    policy: LoadedPolicy,
    target: AgentMcpTarget,
    *,
    ceiling: PoolCeiling,
    floor: HardFloor = DEFAULT_FLOOR,
    classifications: Mapping[str, ServerClassification] | None = None,
) -> Compiled:
    """Compile the MCP endpoints of a sandbox whose agent calls MCP servers directly (D98).

    The path that replaced the MCP proxy (retired 2026-10-26): each declared server gets an
    inspected ``protocol: mcp`` endpoint, whose native rules allow and deny tools by name,
    and the policy middleware decides every request OpenShell admits
    there, as it does a research sandbox's HTTP: tool rules, argument bounds, records with
    the project, session and run, and escalations held as permission requests. The fence
    therefore admits the tools the policy escalates by name, which the middleware refuses
    until they are approved. ``classifications`` are the project's accepted revisions of
    its servers' classifications, by server (``PolicyStore.classification``), which the fence
    admits tools by in place of the catalog's. Raises CompileError when two servers would
    share an endpoint.
    """
    project = policy.policy
    findings: list[Finding] = []
    network_policies = _mcp_server_policies(
        policy,
        target,
        ceiling,
        floor,
        findings,
        classifications=classifications or {},
    )
    hosts = sorted(
        {
            endpoint.host
            for entry in network_policies.values()
            for endpoint in entry.endpoints
            if endpoint.host is not None
        }
    )
    middlewares: dict[str, NetworkMiddleware] | None = None
    if hosts:
        config: dict[str, dict[str, str | int]] = {
            "policy": {"name": project.metadata.name, "digest": policy.digest},
            "mcp": {"project": target.project, "caller": target.caller},
        }
        middlewares = {
            "sandbox-science-policy": NetworkMiddleware(
                middleware=target.middleware,
                order=0,
                on_error="fail_closed",
                endpoints=MiddlewareSelector(include=tuple(hosts)),
                config=config,
            )
        }
        findings.append(
            Finding(
                Severity.INFO,
                "network_middlewares",
                "every MCP request the native rules admit is decided by the policy middleware "
                "before OpenShell injects credentials; escalated calls are refused with "
                "approval_required until the permission request is approved, and the harness "
                "calls again",
            )
        )
        findings.append(
            Finding(
                Severity.INFO,
                "mcp.servers",
                "the tools the policy denies leave a listing in the middleware where it reads "
                "the listing whole, else in the agent runtime's client, by the native rules",
            )
        )
    return Compiled(
        policy=OpenShellPolicy(
            filesystem_policy=_service_filesystem(target.filesystem),
            landlock=HARD_LANDLOCK,
            network_policies=network_policies,
            network_middlewares=middlewares,
        ),
        findings=tuple(findings),
        policy_digest=policy.digest,
        hosts=frozenset(hosts),
    )


def _mcp_server_policies(
    policy: LoadedPolicy,
    target: AgentMcpTarget,
    ceiling: PoolCeiling,
    floor: HardFloor,
    findings: list[Finding],
    *,
    classifications: Mapping[str, ServerClassification] | None = None,
) -> dict[str, NetworkPolicy]:
    """One inspected ``protocol: mcp`` endpoint per declared server the floor and the
    ceiling allow, with the tool rules as native rules."""
    project = policy.policy
    binaries = tuple(Binary(path=path) for path in target.binaries)
    ranges = mcp_server_ranges(ceiling, floor)
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
        port, path = mcp_server_endpoint(server)
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
        rules, deny_rules = _tool_rules(
            _effective_tools(
                project, name, server, subject, findings, (classifications or {}).get(name)
            ),
            class_action(project, name, EffectClass.UNCLASSIFIED).action,
            subject,
            findings,
        )
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
                versions=_versions(server, target.versions, subject, findings),
                max_body_bytes=target.max_body_bytes,
            ),
            # An address literal is its own limit; a name could lead anywhere.
            allowed_ips=None if parse_ip(server.host) is not None else ranges,
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
                "servers named by host name may resolve only to public addresses, less those "
                "the floor and the pool ceiling deny: the fence checks the addresses, since the "
                "client sees only OpenShell's synthetic ones",
            )
        )
    if "2025-03-26" in target.versions:
        findings.append(
            Finding(
                Severity.INFO,
                "versions",
                "2025-03-26 admits JSON-RPC batches of up to 64 messages; OpenShell checks each "
                "member and the middleware refuses batches",
            )
        )
    return network_policies


def mcp_server_endpoint(server: McpServer) -> tuple[int, str]:
    """The port and path of a server's endpoint, as OpenShell matches them."""
    parts = urlsplit(server.url)
    return (parts.port if parts.port is not None else 443), (parts.path or "/")


_MCP_REVISIONS: Final = frozenset({"2025-03-26", "2025-06-18", "2025-11-25", "2026-07-28"})
_STRICT_TOOL_NAME_RE: Final = re.compile(r"[A-Za-z0-9_.*-]{1,128}")


def _versions(
    server: McpServer, versions: tuple[str, ...], subject: str, findings: list[Finding]
) -> tuple[str, ...]:
    """The MCP revisions OpenShell admits for a server: its own revision when it names one."""
    if server.revision not in _MCP_REVISIONS:
        findings.append(
            Finding(
                Severity.INFO,
                f"{subject}.revision",
                f"{server.revision!r} is not an MCP protocol revision; the endpoint admits the "
                f"targets' revisions {list(versions)}",
            )
        )
        return versions
    if server.revision not in versions:
        findings.append(
            Finding(
                Severity.WARNING,
                f"{subject}.revision",
                f"the server speaks {server.revision}, which is not among the revisions clients "
                f"send {list(versions)}; OpenShell refuses the others with 403",
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


class ToolAdmission(StrictModel):
    """The ``tools/call`` names a compiled MCP endpoint's native rules admit, as OpenShell
    matches them: a name some ``allow`` pattern matches and no ``deny`` pattern does (D98).

    The agent runtime's direct client shows the model only these tools, so that it never
    offers a tool OpenShell refuses by name. Empty, it admits nothing: a server whose
    compiled rules are not known offers no tool.
    """

    allow: tuple[Annotated[str, Field(min_length=1, max_length=256)], ...] = Field(
        default=(), max_length=4096
    )
    deny: tuple[Annotated[str, Field(min_length=1, max_length=256)], ...] = Field(
        default=(), max_length=4096
    )

    def admits(self, name: str) -> bool:
        """Whether OpenShell's native rules would let a call of tool ``name`` through."""
        return any(_openshell_matches(p, name) for p in self.allow) and not any(
            _openshell_matches(p, name) for p in self.deny
        )


def tool_admission(endpoint: Endpoint) -> ToolAdmission:
    """The tool names a compiled ``protocol: mcp`` endpoint admits (``compile_agent_mcp``):
    its ``tools/call`` allow entries and deny entries, a missing tool matching every name."""

    def patterns(tool: str | AnyOf | None) -> tuple[str, ...]:
        if tool is None:
            return ("**",)
        return tool.any if isinstance(tool, AnyOf) else (tool,)

    allow = [
        pattern
        for rule in endpoint.rules or ()
        if rule.allow.method == "tools/call"
        for pattern in patterns(rule.allow.tool)
    ]
    deny = [
        pattern
        for entry in endpoint.deny_rules or ()
        if entry.method == "tools/call"
        for pattern in patterns(entry.tool)
    ]
    return ToolAdmission(allow=tuple(allow), deny=tuple(deny))


def _effective_tools(
    project: ProjectPolicy,
    name: str,
    server: McpServer,
    subject: str,
    findings: list[Finding],
    revision: ServerClassification | None = None,
) -> McpServer:
    """The server with its tool rules widened by name to what its classes admit.

    A classified tool no tool rule matches is admitted by exact name as its most
    permissive possible class allows it: argument rules can only make one call stricter
    at the fence, and the middleware decides each call with its
    arguments. Under ``mcp: locked`` only exact names are compiled, so a tool the server
    adds stays denied until the policy names it (D95).
    """
    tools = server.tools
    patterns = (*tools.allow, *tools.escalate, *tools.deny)
    admitted: dict[Action, list[str]] = {Action.ALLOW: [], Action.ESCALATE: [], Action.DENY: []}
    for tool in sorted(classified_names(server, revision)):
        if any(tool_pattern_matches(pattern, tool) for pattern in patterns):
            continue
        action = min(
            (
                class_action(project, name, effect).action
                for effect in possible_classes(server, tool, revision)
            ),
            key=lambda action: action.restrictiveness,
        )
        admitted[Action.DENY if action is Action.DENY else action].append(tool)
    allow, escalate = (
        (*tools.allow, *admitted[Action.ALLOW]),
        (*tools.escalate, *admitted[Action.ESCALATE]),
    )
    if project.lockdown.mcp == "locked":
        globs = sorted(pattern for pattern in (*allow, *escalate) if "*" in pattern)
        if globs:
            findings.append(
                Finding(
                    Severity.INFO,
                    f"{subject}.tools",
                    "MCP is locked, so only exact names are admitted; the patterns "
                    + ", ".join(globs)
                    + " are not compiled (lockdown.mcp)",
                )
            )
        allow = tuple(pattern for pattern in allow if "*" not in pattern)
        escalate = tuple(pattern for pattern in escalate if "*" not in pattern)
    if classified_names(server, revision):
        findings.append(
            Finding(
                Severity.INFO,
                f"{subject}.tools",
                "tools the policy does not classify are admitted only by name, so a tool "
                "that a later listing hints is read-only is denied at this fence (stricter)",
            )
        )
    widened = ToolRules(allow=allow, escalate=escalate, deny=(*tools.deny, *admitted[Action.DENY]))
    return server.model_copy(update={"tools": widened})


def _tool_rules(
    server: McpServer,
    default: Action,
    subject: str,
    findings: list[Finding],
) -> tuple[tuple[Rule, ...], tuple[DenyEntry, ...]]:
    """Translate tool lists into OpenShell rules that are never looser than the project's,
    but for the tools it escalates.

    OpenShell denies tools no allow entry names, and any matching deny entry wins. The
    tools the policy escalates by name are admitted like allowed ones, and the policy
    middleware holds each call for approval. Admitting an escalated pattern needs deny
    entries, which can deny a tool the policy allows by exact name; when it would, only
    the escalated exact names are admitted, so admitting escalated tools never denies a
    tool the fence would admit without them. (The MCP proxy's fence could also deny every
    escalated tool, holding each call itself; it retired on 2026-10-26.)
    """
    prefix = server.tool_prefix or ""
    tools = server.tools
    held = _CompiledTools.of(prefix, tools.allow, (*tools.escalate, *tools.deny), subject)
    chosen = held
    patterns_held = False
    if tools.escalate:
        exact_escalated = tuple(pattern for pattern in tools.escalate if "*" not in pattern)
        escalated_globs = tuple(pattern for pattern in tools.escalate if "*" in pattern)
        chosen = _CompiledTools.of(prefix, (*tools.allow, *tools.escalate), tools.deny, subject)
        allowed = [prefix + pattern for pattern in tools.allow if "*" not in pattern]
        newly_denied = sorted(set(chosen.denied(allowed)) - set(held.denied(allowed)))
        if newly_denied:
            patterns_held = True
            chosen = _CompiledTools.of(
                prefix,
                (*tools.allow, *exact_escalated),
                (*escalated_globs, *tools.deny),
                subject,
            )
            findings.append(
                Finding(
                    Severity.WARNING,
                    f"{subject}.tools",
                    "the escalated patterns "
                    + ", ".join(prefix + pattern for pattern in escalated_globs)
                    + " are not admitted at this fence: admitting them needs deny entries "
                    "that would deny "
                    + ", ".join(newly_denied)
                    + ", which the policy allows by name, so the tools they match stay "
                    "denied even after approval (stricter)",
                )
            )
    findings.extend(chosen.findings)
    if default in (Action.ALLOW, Action.ALERT):
        findings.append(
            Finding(
                Severity.WARNING,
                f"{subject}.tools",
                f"unclassified tools are {default}, but OpenShell denies tools that no allow "
                "entry names, so such tools are denied at the fence (stricter)",
            )
        )
    if tools.escalate:
        findings.append(
            Finding(
                Severity.INFO,
                f"{subject}.tools",
                (
                    "tools the escalate entries name exactly are admitted at this fence, but "
                    "not those only its escalated patterns match (see the warning),"
                    if patterns_held
                    else "tools the escalate entries match are admitted at this fence,"
                )
                + " so that approvals take effect; the policy middleware holds each call for "
                "approval (looser at the fence only)",
            )
        )
    if default is Action.ESCALATE:
        findings.append(
            Finding(
                Severity.INFO,
                f"{subject}.tools",
                "tools escalated by the default action, which no escalate entry names, are "
                "always denied at this fence, even after approval: approved grants are not "
                "compiled in yet (stricter)",
            )
        )
    return chosen.rules, chosen.deny_rules


@dataclass(frozen=True)
class _CompiledTools:
    """One server's tool rules for a set of admitted and a set of blocking patterns."""

    rules: tuple[Rule, ...]
    deny_rules: tuple[DenyEntry, ...]
    findings: tuple[Finding, ...]

    def denied(self, names: Iterable[str]) -> list[str]:
        """The names a deny entry matches."""
        patterns = [entry.tool for entry in self.deny_rules if isinstance(entry.tool, str)]
        return [name for name in names if any(_openshell_matches(p, name) for p in patterns)]

    @classmethod
    def of(
        cls,
        prefix: str,
        admitted: Iterable[str],
        blocked: Iterable[str],
        subject: str,
    ) -> _CompiledTools:
        """Rules admitting ``admitted`` and never a name ``blocked`` matches."""
        admitted = tuple(admitted)
        findings: list[Finding] = []
        blocking = sorted({_collapse(pattern) for pattern in blocked})
        exact_allows = sorted({pattern for pattern in admitted if "*" not in pattern})
        glob_allows = sorted({_collapse(pattern) for pattern in admitted if "*" in pattern})
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
                        f"{prefix + pattern} does not fit OpenShell's tool names (letters, "
                        "digits, _ . - and at most 128 characters), so calls to it are refused "
                        "(stricter)",
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
            deny_rules = [
                DenyEntry(method="tools/call", tool=prefix + pattern) for pattern in blocking
            ]
            if prefix:
                # '*' also matches nothing, so 'portal-*' would admit a tool named just
                # 'portal-', which the project refuses.
                deny_rules.append(DenyEntry(method="tools/call", tool=prefix))
            findings.append(
                Finding(
                    Severity.INFO,
                    f"{subject}.tools",
                    "OpenShell's '*' does not match '.', so allow patterns admit fewer names "
                    "when tool names contain dots (stricter)",
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
        return cls(tuple(rules), tuple(deny_rules), tuple(findings))


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
            f"provider {provider} must be attached to the agent runtime's sandbox and be "
            "endpointless",
        )
    )
    return CredentialBinding(provider=provider)
