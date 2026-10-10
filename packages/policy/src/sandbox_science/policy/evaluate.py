# SPDX-License-Identifier: MIT
"""Evaluation of operations against the hard floor, the pool ceiling and a project policy.

docs/POLICY.md defines the semantics; the section names in comments refer to it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from typing import Final, NamedTuple, assert_never

from .ceiling import PoolCeiling
from .decision import Action, Alert, Decision, EscalationTerms, Layer, most_restrictive
from .errors import ClassificationError
from .floor import HardFloor
from .hosts import HostKind, is_public
from .loader import LoadedPolicy
from .model import GitRule, HttpRule, OnTimeout, RuleEscalation
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
) -> Decision:
    """Decide what happens to one operation.

    The hard floor and the pool ceiling apply first and cannot be overridden;
    the project policy decides the rest.
    """
    match operation:
        case HttpOperation() | GitOperation():
            return _evaluate_network(operation, policy, ceiling, floor)
        case McpOperation():
            return _evaluate_mcp(operation, policy, ceiling, floor)
        case PackageOperation():
            return _evaluate_package(operation, policy)
        case _:
            assert_never(operation)


def evaluate_request(
    request: HttpRequest,
    policy: LoadedPolicy,
    *,
    ceiling: PoolCeiling,
    floor: HardFloor = DEFAULT_FLOOR,
) -> Decision:
    """Classify and evaluate an egress request; unclassifiable requests are denied."""
    try:
        operation = classify_http_request(request)
    except ClassificationError as exc:
        return _deny("classification", str(exc), policy)
    return evaluate(operation, policy, ceiling=ceiling, floor=floor)


def evaluate_mcp_message(
    server: str,
    message: object,
    policy: LoadedPolicy,
    *,
    origin: Origin = "orchestrator",
    ceiling: PoolCeiling,
    floor: HardFloor = DEFAULT_FLOOR,
) -> Decision:
    """Classify and evaluate one MCP JSON-RPC message; unclassifiable messages are denied."""
    try:
        operation = classify_mcp_message(server, message, origin=origin)
    except ClassificationError as exc:
        return _deny("classification", str(exc), policy)
    return evaluate(operation, policy, ceiling=ceiling, floor=floor)


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


def _decide(
    action: Action,
    layer: Layer,
    reason: str,
    policy: LoadedPolicy,
    *,
    rule: str | None = None,
    rule_escalation: RuleEscalation | None = None,
    read_only: bool = False,
    credentialed: bool = False,
) -> Decision:
    """Build a decision, attaching escalation terms ("Escalation timeout")."""
    terms: EscalationTerms | None = None
    if action is Action.ESCALATE:
        timeout = policy.policy.escalation.timeout
        on_timeout = Action.DENY
        if rule_escalation is not None:
            if rule_escalation.timeout is not None:
                timeout = rule_escalation.timeout
            # The model only accepts on_timeout: allow on read-only rules; recheck here so a
            # hand-built model cannot widen it. Credentialed requests never time out to allow.
            if rule_escalation.on_timeout is OnTimeout.ALLOW and read_only and not credentialed:
                on_timeout = Action.ALERT
        terms = EscalationTerms(timeout=timeout, on_timeout=on_timeout)
    return Decision(
        action=action,
        layer=layer,
        reason=reason,
        rule=rule,
        policy_digest=policy.digest,
        escalation=terms,
    )


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
        path = operation.path if isinstance(operation, HttpOperation) else operation.repo_path
        segments = path_segments(path)
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


def _evaluate_network(
    operation: HttpOperation | GitOperation,
    policy: LoadedPolicy,
    ceiling: PoolCeiling,
    floor: HardFloor,
) -> Decision:
    reason = floor.check_host(operation.host)
    if reason is not None:
        return _deny("floor", f"hard floor: {reason}", policy)
    reason = ceiling.check_host(operation.host)
    if reason is not None:
        return _deny("ceiling", reason, policy)

    operation_class = "http" if isinstance(operation, HttpOperation) else "git"
    winner: _Candidate | None = None
    for rule in policy.policy.network.rules:
        if rule.operation != operation_class:
            continue
        specificity = _match_rule(rule, operation)
        if specificity is None:
            continue
        candidate = _Candidate(rule, specificity)
        if (
            winner is None
            or specificity > winner.specificity
            or (
                specificity == winner.specificity
                and rule.action.restrictiveness > winner.rule.action.restrictiveness
            )
        ):
            winner = candidate

    if winner is None:
        action = policy.policy.defaults.action
        exact_host = False
        decision = _decide(
            action,
            "default",
            f"no {operation_class} rule matched; defaults.action is {action}",
            policy,
            credentialed=operation.credentialed,
        )
    else:
        rule = winner.rule
        action = rule.action
        exact_host = winner.specificity.host_kind is HostKind.EXACT
        if (
            isinstance(rule, HttpRule)
            and isinstance(operation, HttpOperation)
            and action in (Action.ALLOW, Action.ALERT)
            and rule.limits is not None
            and rule.limits.max_url_bytes is not None
            and operation.url_bytes > rule.limits.max_url_bytes
        ):
            return _deny(
                "policy",
                f"URL is {operation.url_bytes} bytes; rule {rule.name!r} allows at most "
                f"{rule.limits.max_url_bytes}",
                policy,
                rule=rule.name,
            )
        decision = _decide(
            action,
            "policy",
            f"rule {rule.name!r} ({action})",
            policy,
            rule=rule.name,
            rule_escalation=rule.escalation,
            read_only=rule.read_only,
            credentialed=operation.credentialed,
        )

    if action in (Action.ALLOW, Action.ALERT) and not exact_host and not is_public(operation.host):
        return _deny(
            decision.layer,
            f"{operation.host} is not a public destination; allowing it needs a rule naming it "
            "exactly",
            policy,
            rule=decision.rule,
        )
    return decision


@lru_cache(maxsize=1024)
def _tool_regex(pattern: str) -> re.Pattern[str]:
    return re.compile(".*".join(re.escape(part) for part in pattern.split("*")), re.DOTALL)


def _tool_matches(pattern: str, tool: str) -> bool:
    if "*" not in pattern:
        return pattern == tool
    return _tool_regex(pattern).fullmatch(tool) is not None


def _evaluate_mcp(
    operation: McpOperation,
    policy: LoadedPolicy,
    ceiling: PoolCeiling,
    floor: HardFloor,
) -> Decision:
    mcp = policy.policy.mcp
    server = mcp.servers.get(operation.server)
    if server is None:
        return _deny("policy", f"MCP server {operation.server!r} is not declared", policy)
    reason = floor.check_host(server.host)
    if reason is not None:
        return _deny("floor", f"hard floor: {reason}", policy)
    reason = ceiling.check_mcp_server(operation.server) or ceiling.check_host(server.host)
    if reason is not None:
        return _deny("ceiling", reason, policy)
    if operation.origin == "worker" and not mcp.proxy.worker_access:
        return _deny(
            "policy", "worker code may not call MCP servers (mcp.proxy.worker_access)", policy
        )
    if operation.method in MCP_PROTOCOL_METHODS:
        return _decide(
            Action.ALLOW,
            "policy",
            f"MCP protocol method {operation.method}",
            policy,
            rule="mcp-protocol",
        )
    if operation.method != "tools/call" or operation.tool is None:
        return _deny("policy", f"MCP method {operation.method!r} is not supported", policy)

    tool = operation.tool
    worker_tools = mcp.proxy.worker_tools
    if (
        operation.origin == "worker"
        and worker_tools != "inherit"
        and not any(_tool_matches(pattern, tool) for pattern in worker_tools)
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
        if _tool_matches(pattern, tool)
    ]
    exact = [entry for entry in matches if "*" not in entry[2]]
    pool = exact or matches
    if not pool:
        action = policy.policy.defaults.action
        return _decide(
            action,
            "default",
            f"no tool rule matched {tool!r}; defaults.action is {action}",
            policy,
        )
    action = most_restrictive(entry[1] for entry in pool)
    list_name, _, pattern = next(entry for entry in pool if entry[1] is action)
    label = f"mcp.servers.{operation.server}.tools.{list_name}[{pattern}]"
    return _decide(action, "policy", f"tool {tool!r} matched {label}", policy, rule=label)


def _evaluate_package(operation: PackageOperation, policy: LoadedPolicy) -> Decision:
    packages = policy.policy.packages
    if operation.ecosystem == "conda":
        conda = packages.conda
        if conda is None:
            action = policy.policy.defaults.action
            return _decide(
                action, "default", f"conda is not configured; defaults.action is {action}", policy
            )
        if operation.channel not in conda.channels:
            return _deny(
                "policy",
                f"conda channel {operation.channel!r} is not allowed",
                policy,
                rule="packages.conda",
            )
        return _decide(
            conda.action, "policy", f"conda package {operation.name}", policy, rule="packages.conda"
        )
    pypi = packages.pypi
    if pypi is None:
        action = policy.policy.defaults.action
        return _decide(
            action, "default", f"PyPI is not configured; defaults.action is {action}", policy
        )
    return _decide(
        pypi.action, "policy", f"PyPI package {operation.name}", policy, rule="packages.pypi"
    )
