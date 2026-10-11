# SPDX-License-Identifier: MIT
"""Evaluation of operations against the hard floor, the pool ceiling and a project policy.

docs/POLICY.md defines the semantics; the section names in comments refer to it.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final, NamedTuple, assert_never

from .ceiling import PoolCeiling
from .decision import Action, Alert, Decision, EscalationTerms, Layer, most_restrictive
from .errors import ClassificationError
from .floor import HardFloor
from .hosts import HostKind, address_forms, is_public, parse_ip
from .loader import LoadedPolicy
from .model import CondaPackages, GitRule, HttpRule, OnTimeout, Packages, RegistryPackages
from .operations import (
    DEFAULT_PORTS,
    GitOperation,
    HttpOperation,
    HttpRequest,
    McpOperation,
    Operation,
    Origin,
    PackageOperation,
    classify_http_request,
    classify_mcp_message,
    path_segments,
)

DEFAULT_FLOOR: Final = HardFloor()

MCP_PROTOCOL_METHODS: Final = frozenset(
    {
        "initialize",
        "ping",
        "notifications/initialized",
        "notifications/cancelled",
        "notifications/progress",
        "notifications/roots/list_changed",
        "tools/list",
        "resources/list",
        "resources/templates/list",
        "resources/read",
        "resources/subscribe",
        "resources/unsubscribe",
        "prompts/list",
        "prompts/get",
        "completion/complete",
        "logging/setLevel",
    }
)
"""MCP methods allowed without tool rules; any other method except tools/call is denied."""


class _Specificity(NamedTuple):
    """Comparison key for rule specificity, most important field first ("Pick the rule")."""

    host_kind: HostKind
    wildcard_labels: int
    path_segments: int
    qualified: int


@dataclass(frozen=True, slots=True)
class _Candidate:
    rule: HttpRule | GitRule
    specificity: _Specificity


@dataclass(frozen=True, slots=True)
class EgressUsage:
    """Usage counters that monitors compare against their thresholds."""

    egress_bytes_last_hour: int


def evaluate(
    operation: Operation,
    policy: LoadedPolicy,
    *,
    ceiling: PoolCeiling,
    floor: HardFloor = DEFAULT_FLOOR,
    resolved: Sequence[str] = (),
) -> Decision:
    """Decide what happens to one operation.

    The hard floor and the pool ceiling apply first and cannot be overridden;
    the project policy decides the rest. ``resolved`` holds the IP addresses
    the egress or MCP proxy is about to connect to for the operation's host
    (or the MCP server's host). The floor, the ceiling's deny list and the
    public/non-public rule apply to each of them as well as to the name.
    """
    for address in resolved:
        if parse_ip(address) is None:
            return _deny(
                "classification", f"resolved address {address!r} is not an IP address", policy
            )
    match operation:
        case HttpOperation() | GitOperation():
            return _evaluate_network(operation, policy, ceiling, floor, resolved)
        case McpOperation():
            return _evaluate_mcp(operation, policy, ceiling, floor, resolved)
        case PackageOperation():
            return _evaluate_package(operation, policy, ceiling, floor)
        case _:
            assert_never(operation)


def evaluate_request(
    request: HttpRequest,
    policy: LoadedPolicy,
    *,
    ceiling: PoolCeiling,
    floor: HardFloor = DEFAULT_FLOOR,
    resolved: Sequence[str] = (),
) -> Decision:
    """Classify and evaluate an egress request; unclassifiable requests are denied."""
    try:
        operation = classify_http_request(request)
    except ClassificationError as exc:
        return _deny("classification", str(exc), policy)
    return evaluate(operation, policy, ceiling=ceiling, floor=floor, resolved=resolved)


def evaluate_mcp_message(
    server: str,
    message: object,
    policy: LoadedPolicy,
    *,
    origin: Origin = "orchestrator",
    ceiling: PoolCeiling,
    floor: HardFloor = DEFAULT_FLOOR,
    resolved: Sequence[str] = (),
) -> Decision:
    """Classify and evaluate one MCP JSON-RPC message; unclassifiable messages are denied."""
    try:
        operation = classify_mcp_message(server, message, origin=origin)
    except ClassificationError as exc:
        return _deny("classification", str(exc), policy)
    return evaluate(operation, policy, ceiling=ceiling, floor=floor, resolved=resolved)


def evaluate_monitors(policy: LoadedPolicy, usage: EgressUsage) -> tuple[Alert, ...]:
    """Return the findings of every monitor whose threshold is exceeded ("Monitors")."""
    alerts: list[Alert] = []
    for monitor in policy.policy.monitors:
        threshold = monitor.when.egress_bytes_per_hour_over
        if threshold is not None and usage.egress_bytes_last_hour > threshold:
            alerts.append(
                Alert(
                    rule=monitor.name,
                    reason=(
                        f"{usage.egress_bytes_last_hour} bytes of egress in the last hour "
                        f"exceeds {threshold}"
                    ),
                )
            )
    return tuple(alerts)


def _deny(layer: Layer, reason: str, policy: LoadedPolicy, rule: str | None = None) -> Decision:
    return Decision(
        action=Action.DENY, layer=layer, reason=reason, rule=rule, policy_digest=policy.digest
    )


def _decision(
    action: Action,
    layer: Layer,
    reason: str,
    policy: LoadedPolicy,
    *,
    rule: str | None = None,
    terms: EscalationTerms | None = None,
) -> Decision:
    """Build a decision; escalations default to the policy-wide terms."""
    if action is Action.ESCALATE and terms is None:
        terms = EscalationTerms(timeout=policy.policy.escalation.timeout, on_timeout=Action.DENY)
    return Decision(
        action=action,
        layer=layer,
        reason=reason,
        rule=rule,
        policy_digest=policy.digest,
        escalation=terms if action is Action.ESCALATE else None,
    )


def _rule_terms(
    policy: LoadedPolicy,
    rules: Sequence[HttpRule | GitRule],
    *,
    credentialed: bool,
    limit_exceeded: bool,
) -> EscalationTerms:
    """Escalation terms for the tied escalate rules ("Escalation timeout").

    The shortest timeout applies. The operation proceeds as an alert on timeout
    only if every tied rule is read-only and permits it, and no tied rule's limit
    is exceeded. A request that may carry a credential proceeds only if every
    tied rule also sets ``allow_credentialed``. The model already restricts
    ``on_timeout: allow`` to read-only rules; this rechecks it so a hand-built
    model cannot widen it.
    """
    default = policy.policy.escalation.timeout
    timeout = min(
        rule.escalation.timeout
        if rule.escalation is not None and rule.escalation.timeout is not None
        else default
        for rule in rules
    )
    permitted = all(
        rule.read_only
        and rule.escalation is not None
        and rule.escalation.on_timeout is OnTimeout.ALLOW
        for rule in rules
    )
    credentials_permitted = not credentialed or all(
        rule.escalation is not None and rule.escalation.allow_credentialed for rule in rules
    )
    on_timeout = (
        Action.ALERT if permitted and credentials_permitted and not limit_exceeded else Action.DENY
    )
    return EscalationTerms(timeout=timeout, on_timeout=on_timeout)


def _match_rule(
    rule: HttpRule | GitRule, operation: HttpOperation | GitOperation
) -> _Specificity | None:
    """Return the rule's specificity for this operation, or None if it does not match."""
    match = rule.match
    host_key: tuple[HostKind, int] | None = None
    for pattern in match.hosts:
        if pattern.matches(operation.host):
            key = (pattern.kind, pattern.labels)
            if host_key is None or key > host_key:
                host_key = key
    if host_key is None:
        return None
    if match.ports:
        if operation.port not in match.ports:
            return None
    elif operation.port != DEFAULT_PORTS[operation.scheme]:
        return None
    matched_segments = 0
    if match.paths:
        segments = path_segments(_operation_path(operation))
        lengths = [
            len(prefix)
            for prefix in (path_segments(raw) for raw in match.paths)
            if segments[: len(prefix)] == prefix
        ]
        if not lengths:
            return None
        matched_segments = max(lengths)
    qualified = 0
    if isinstance(rule, HttpRule) and isinstance(operation, HttpOperation):
        if rule.match.methods:
            if operation.method not in rule.match.methods:
                return None
            qualified = 1
    elif isinstance(rule, GitRule) and isinstance(operation, GitOperation):
        if rule.match.git_service is not None:
            if operation.service != rule.match.git_service:
                return None
            qualified = 1
    else:
        return None  # rules only see operations of their own class
    return _Specificity(host_key[0], host_key[1], matched_segments, qualified)


def _operation_path(operation: HttpOperation | GitOperation) -> str:
    return operation.path if isinstance(operation, HttpOperation) else operation.repo_path


PACKAGE_INDEX_HOSTS: Final = frozenset(
    {
        # PyPI
        "pypi.org",
        "pypi.python.org",
        "files.pythonhosted.org",
        # conda
        "conda.anaconda.org",
        "repo.anaconda.com",
        "repo.prefix.dev",
        # Cargo
        "crates.io",
        "index.crates.io",
        "static.crates.io",
        # Go
        "proxy.golang.org",
        "sum.golang.org",
        # npm
        "registry.npmjs.org",
        "registry.yarnpkg.com",
        # Aliases and mirrors of the same registries
        "test.pypi.org",
        "anaconda.org",
        "repo.continuum.io",
        "prefix.dev",
        "registry.npmjs.com",
        "replicate.npmjs.com",
        "skimdb.npmjs.com",
        "npm.pkg.github.com",
        "registry.npmmirror.com",
        "cdn.jsdelivr.net",
        "unpkg.com",
        "goproxy.io",
        "goproxy.cn",
        "proxy.golang.com.cn",
        "sum.golang.google.cn",
    }
)
"""Package registries, which workers reach only through the package cache (D44, D71)."""

PACKAGE_REGISTRY_SUFFIXES: Final = (
    ".pythonhosted.org",
    ".anaconda.org",
    ".prefix.dev",
    ".crates.io",
)
"""Domains whose every host serves a package registry or its files."""

ECOSYSTEM_UPSTREAMS: Final[dict[str, tuple[str, ...]]] = {
    "pypi": ("pypi.org", "files.pythonhosted.org"),
    "conda": ("conda.anaconda.org",),
    "cargo": ("index.crates.io", "static.crates.io"),
    "go": ("proxy.golang.org", "sum.golang.org"),
    "npm": ("registry.npmjs.org",),
}
"""The registries the package cache fetches from, per ecosystem; the pool ceiling applies."""


def registry_reason(host: str) -> str | None:
    """Why a host is reachable only through the package cache, or None."""
    if host in PACKAGE_INDEX_HOSTS or host.endswith(PACKAGE_REGISTRY_SUFFIXES):
        return f"{host} is a package registry, reachable only through the package cache"
    return None


def reserved_host_reason(host: str, policy: LoadedPolicy) -> str | None:
    """Why a host is reserved for a platform route (a registry or a declared MCP server)."""
    reason = registry_reason(host)
    if reason is not None:
        return reason
    for name, server in policy.policy.mcp.servers.items():
        if server.host == host:
            return f"{host} hosts MCP server {name!r}, reachable only through the MCP proxy"
    return None


def _mcp_host(
    operation: HttpOperation | GitOperation, policy: LoadedPolicy, resolved: Sequence[str]
) -> str | None:
    """Return the declared MCP server this request would reach, if any.

    The whole host is reserved for the MCP proxy, not only the declared path:
    servers often offer other transports (such as /sse) beside it. A server
    declared by IP address also matches requests whose name resolves to it.
    """
    destinations = {form for address in resolved for form in address_forms(address)}
    destinations.add(operation.host)
    for name, server in policy.policy.mcp.servers.items():
        if server.host in destinations:
            return name
    return None


def _evaluate_network(
    operation: HttpOperation | GitOperation,
    policy: LoadedPolicy,
    ceiling: PoolCeiling,
    floor: HardFloor,
    resolved: Sequence[str],
) -> Decision:
    reason = floor.check_destination(operation.host, resolved)
    if reason is not None:
        return _deny("floor", f"hard floor: {reason}", policy)
    reason = ceiling.check_host(operation.host, resolved)
    if reason is not None:
        return _deny("ceiling", reason, policy)
    server = _mcp_host(operation, policy, resolved)
    if server is not None:
        return _deny(
            "floor",
            f"{operation.host} hosts MCP server {server!r}, which is reachable only through "
            "the MCP proxy (D67)",
            policy,
        )
    # Otherwise a read rule would let workers fetch packages around the package
    # cache and the packages section (ecosystem and channel rules).
    reason = registry_reason(operation.host)
    if reason is not None:
        return _deny("floor", reason, policy)

    operation_class = "http" if isinstance(operation, HttpOperation) else "git"
    candidates: list[_Candidate] = []
    for rule in policy.policy.network.rules:
        if rule.operation != operation_class:
            continue
        specificity = _match_rule(rule, operation)
        if specificity is not None:
            candidates.append(_Candidate(rule, specificity))

    if not candidates:
        action = policy.policy.defaults.action
        exact_host = False
        decision = _decision(
            action,
            "default",
            f"no {operation_class} rule matched; defaults.action is {action}",
            policy,
        )
    else:
        top = max(candidate.specificity for candidate in candidates)
        tied = [candidate.rule for candidate in candidates if candidate.specificity == top]
        action = most_restrictive(rule.action for rule in tied)
        winner = next(rule for rule in tied if rule.action is action)
        exact_host = top.host_kind is HostKind.EXACT
        # Limits of every tied rule apply, so adding an equally specific rule can
        # never remove a limit.
        limits = [
            rule.limits.max_url_bytes
            for rule in tied
            if rule.limits is not None and rule.limits.max_url_bytes is not None
        ]
        limit = min(limits) if limits else None
        exceeded = limit is not None and operation.url_bytes > limit
        if exceeded and action in (Action.ALLOW, Action.ALERT):
            return _deny(
                "policy",
                f"URL is {operation.url_bytes} bytes; rule {winner.name!r} allows at most {limit}",
                policy,
                rule=winner.name,
            )
        terms = None
        if action is Action.ESCALATE:
            terms = _rule_terms(
                policy,
                [rule for rule in tied if rule.action is Action.ESCALATE],
                credentialed=operation.credentialed,
                limit_exceeded=exceeded,
            )
        others = [rule.name for rule in tied if rule is not winner]
        tie_note = f"; tied with {', '.join(repr(name) for name in others)}" if others else ""
        decision = _decision(
            action,
            "policy",
            f"rule {winner.name!r} ({action}){tie_note}",
            policy,
            rule=winner.name,
            terms=terms,
        )

    public = is_public(operation.host) and all(is_public(address) for address in resolved)
    if action in (Action.ALLOW, Action.ALERT) and not exact_host and not public:
        return _deny(
            decision.layer,
            f"{operation.host} is not a public destination; allowing it needs a rule naming it "
            "exactly",
            policy,
            rule=decision.rule,
        )
    return decision


def tool_pattern_matches(pattern: str, name: str) -> bool:
    """Match a pattern whose only wildcard is '*' against a whole name.

    Greedy leftmost matching of the literal pieces is exact for '*'-only globs
    and avoids the backtracking blow-up a regular expression can show.
    """
    pieces = pattern.split("*")
    if len(pieces) == 1:
        return pattern == name
    first, last = pieces[0], pieces[-1]
    if len(first) + len(last) > len(name) or not name.startswith(first) or not name.endswith(last):
        return False
    position, end = len(first), len(name) - len(last)
    for piece in pieces[1:-1]:
        if not piece:
            continue
        index = name.find(piece, position, end)
        if index < 0:
            return False
        position = index + len(piece)
    return True


def _evaluate_mcp(
    operation: McpOperation,
    policy: LoadedPolicy,
    ceiling: PoolCeiling,
    floor: HardFloor,
    resolved: Sequence[str],
) -> Decision:
    mcp = policy.policy.mcp
    server = mcp.servers.get(operation.server)
    if server is None:
        return _deny("policy", f"MCP server {operation.server!r} is not declared", policy)
    reason = floor.check_destination(server.host, resolved)
    if reason is not None:
        return _deny("floor", f"hard floor: {reason}", policy)
    reason = ceiling.check_mcp_server(operation.server) or ceiling.check_host(server.host, resolved)
    if reason is not None:
        return _deny("ceiling", reason, policy)
    if operation.origin == "worker" and not mcp.proxy.worker_access:
        return _deny(
            "policy", "worker code may not call MCP servers (mcp.proxy.worker_access)", policy
        )
    if operation.method in MCP_PROTOCOL_METHODS:
        return _decision(
            Action.ALLOW,
            "policy",
            f"MCP protocol method {operation.method}",
            policy,
            rule="mcp-protocol",
        )
    if operation.method != "tools/call" or operation.tool is None:
        return _deny("policy", f"MCP method {operation.method!r} is not supported", policy)

    tool = operation.tool
    name = tool
    prefix = server.tool_prefix
    if prefix is not None:
        if not tool.startswith(prefix) or tool == prefix:
            return _deny(
                "policy", f"tool {tool!r} is outside the server's tool prefix {prefix!r}", policy
            )
        name = tool.removeprefix(prefix)
    worker_tools = mcp.proxy.worker_tools
    if (
        operation.origin == "worker"
        and worker_tools != "inherit"
        and not any(tool_pattern_matches(pattern, name) for pattern in worker_tools)
    ):
        return _deny("policy", f"tool {tool!r} is not in mcp.proxy.worker_tools", policy)

    lists = (
        ("allow", Action.ALLOW, server.tools.allow),
        ("escalate", Action.ESCALATE, server.tools.escalate),
        ("deny", Action.DENY, server.tools.deny),
    )
    matches = [
        (list_name, action, pattern)
        for list_name, action, patterns in lists
        for pattern in patterns
        if tool_pattern_matches(pattern, name)
    ]
    exact = [entry for entry in matches if "*" not in entry[2]]
    pool = exact or matches
    if not pool:
        action = policy.policy.defaults.action
        return _decision(
            action, "default", f"no tool rule matched {tool!r}; defaults.action is {action}", policy
        )
    action = most_restrictive(entry[1] for entry in pool)
    list_name, _, pattern = next(entry for entry in pool if entry[1] is action)
    label = f"mcp.servers.{operation.server}.tools.{list_name}[{pattern}]"
    return _decision(action, "policy", f"tool {tool!r} matched {label}", policy, rule=label)


def _evaluate_package(
    operation: PackageOperation, policy: LoadedPolicy, ceiling: PoolCeiling, floor: HardFloor
) -> Decision:
    """Decide a request to the package cache ("Packages").

    The ecosystem's action applies to package files. Index lookups install
    nothing, so they are allowed unless that action is ``deny``: an escalation
    then asks about the install, not about reading the index the solver needs.
    The pool ceiling applies to the registries the cache fetches from. A Go
    module request also counts as a read of the module's origin host, because
    the Go module proxy fetches modules it lacks from there.
    """
    packages = policy.policy.packages
    label = _ECOSYSTEM_LABELS[operation.ecosystem]
    for host in ECOSYSTEM_UPSTREAMS[operation.ecosystem]:
        reason = ceiling.check_host(host)
        if reason is not None:
            return _deny("ceiling", f"{label} registry: {reason}", policy)
    config = _ecosystem_config(packages, operation.ecosystem)
    if config is None:
        action = policy.policy.defaults.action
        layer: Layer = "default"
        rule = None
        reason = f"{label} is not configured; defaults.action is {action}"
        channels: tuple[str, ...] = ("conda-forge",)
    else:
        action = config.action
        layer = "policy"
        rule = f"packages.{operation.ecosystem}"
        reason = f"{label} package {operation.name}" if operation.name else f"{label} index"
        channels = config.channels if isinstance(config, CondaPackages) else ()
    if operation.channel is not None and operation.channel not in channels:
        return _deny("policy", f"conda channel {operation.channel!r} is not allowed", policy, rule)
    if operation.kind == "index":
        if action is Action.DENY:
            return _deny(layer, f"{reason}: {label} is denied", policy, rule)
        decision = _decision(Action.ALLOW, layer, f"{reason} (index lookup)", policy, rule=rule)
    else:
        decision = _decision(action, layer, reason, policy, rule=rule)
    if operation.ecosystem == "go" and operation.name is not None:
        origin = _go_origin_decision(operation.name, policy, ceiling, floor)
        if origin.action.restrictiveness > decision.action.restrictiveness:
            return origin
    return decision


def _go_origin_decision(
    module: str, policy: LoadedPolicy, ceiling: PoolCeiling, floor: HardFloor
) -> Decision:
    """Decide a Go module request as a read of its origin, which the module proxy contacts."""
    origin, _, rest = module.partition("/")
    path = f"/{rest}"
    try:
        probe = HttpOperation(
            method="GET",
            scheme="https",
            host=origin,
            port=443,
            path=path,
            url_bytes=len(f"https://{origin}{path}"),
        )
    except ClassificationError as exc:
        return _deny("classification", f"Go module origin {origin!r}: {exc}", policy)
    decision = _evaluate_network(probe, policy, ceiling, floor, ())
    return Decision(
        action=decision.action,
        layer=decision.layer,
        reason=f"Go module origin {origin}: {decision.reason}",
        rule=decision.rule,
        policy_digest=decision.policy_digest,
        escalation=decision.escalation,
    )


_ECOSYSTEM_LABELS: Final[dict[str, str]] = {
    "pypi": "PyPI",
    "conda": "conda",
    "cargo": "Cargo",
    "go": "Go",
    "npm": "npm",
}


def _ecosystem_config(
    packages: Packages, ecosystem: str
) -> CondaPackages | RegistryPackages | None:
    match ecosystem:
        case "conda":
            return packages.conda
        case "pypi":
            return packages.pypi
        case "cargo":
            return packages.cargo
        case "go":
            return packages.go
        case "npm":
            return packages.npm
        case _:
            return None
