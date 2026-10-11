# SPDX-License-Identifier: MIT
"""Decide one sandbox request with the policy library, independent of gRPC.

OpenShell has already applied its network rules (admission endpoints, address
checks) when it asks; this module applies the project policy, the pool
ceiling and the hard floor to the request exactly, and turns the library's
decision into an allow or a deny with a reason code (docs/POLICY.md, "Mapping
to enforcement").
"""

from __future__ import annotations

import logging
import re
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Final, cast

from sandbox_science.policy import (
    Action,
    ClassificationError,
    Decision,
    EgressUsage,
    GitOperation,
    HardFloor,
    HttpOperation,
    HttpRequest,
    Layer,
    LoadedPolicy,
    PackageOperation,
    PoolCeiling,
    classify_http_request,
    classify_package_request,
    evaluate,
    evaluate_monitors,
    normalize_host,
)
from sandbox_science.policy.openshell import CREDENTIAL_PORT

from .errors import MiddlewareError, StoreError
from .events import EventSink
from .gitproto import GitRequestError, check_upload_pack_request
from .meter import EgressMeter
from .store import PolicyStore, check_digest

_log = logging.getLogger(__name__)

# Reason codes OpenShell returns to the sandbox with a denial. The agent reads
# them, so each names what the agent can do about the denial.
APPROVAL_REQUIRED: Final = "approval_required"
"""An escalate rule matched; the request is pending approval and may be retried after it."""
POLICY_DENIED: Final = "policy_denied"
"""A project rule or the policy's default denies the request."""
POOL_CEILING_DENIED: Final = "pool_ceiling_denied"
"""The worker pool's ceiling denies the destination."""
DESTINATION_FORBIDDEN: Final = "destination_forbidden"
"""The hard floor denies the destination."""
REQUEST_UNCLASSIFIABLE: Final = "request_unclassifiable"
"""The request is ambiguous or unsupported (including WebSocket and other upgrades)."""
GIT_REQUEST_INVALID: Final = "git_request_invalid"
"""A git fetch whose body is not a well-formed upload-pack request."""
AUDIT_UNAVAILABLE: Final = "audit_unavailable"
"""An alert rule matched but the alert could not be recorded."""
SANDBOX_UNBOUND: Final = "sandbox_unbound"
"""The orchestrator has not bound the sandbox to a policy revision."""
POLICY_STALE: Final = "policy_stale"
"""The sandbox's compiled policy names a revision other than the one it is bound to."""
POLICY_UNAVAILABLE: Final = "policy_unavailable"
"""The bound policy revision cannot be loaded."""
CONFIG_INVALID: Final = "config_invalid"
"""The middleware entry's config is malformed or names another policy."""
CREDENTIAL_UNENCRYPTED: Final = "credential_unencrypted"
"""Plain HTTP to the port a credential is bound to, which would carry it unencrypted; use HTTPS."""

REASON_CODE_RE: Final = re.compile(r"[a-z][a-z0-9_]{0,63}")
"""OpenShell's format for reason codes; anything else is a middleware failure."""

_LAYER_CODES: Final[Mapping[Layer, str]] = {
    "classification": REQUEST_UNCLASSIFIABLE,
    "floor": DESTINATION_FORBIDDEN,
    "ceiling": POOL_CEILING_DENIED,
    "policy": POLICY_DENIED,
    "default": POLICY_DENIED,
}
_URL_SCHEMES: Final = {"http": "http", "https": "https", "ws": "http", "wss": "https"}
_DEFAULT_PORTS: Final = {"http": 80, "https": 443}
# OpenShell labels the scheme by how a request reached its proxy, not by
# whether it used TLS: everything through a CONNECT tunnel is "https", even
# plain HTTP to port 80. Rules without ports match the scheme's default port,
# so the port decides on the default ports; elsewhere the scheme only changes
# the URL's length.
_PORT_SCHEMES: Final = {80: "http", 443: "https"}
_HOST_RE: Final = re.compile(r"[A-Za-z0-9._-]+|[0-9A-Fa-f:.]+")
_MONITOR_REPEAT: Final = timedelta(hours=1)


class PolicyConfigError(MiddlewareError):
    """A middleware entry's ``config`` does not name a policy revision correctly."""


@dataclass(frozen=True, slots=True)
class PolicyRef:
    """The project policy revision a compiled sandbox policy was built from.

    ``package_cache`` and ``mcp_proxy`` are where the sandbox's platform services
    listen (host, port), when the compiled policy admits them: requests to the
    cache are package operations, and requests to the MCP proxy go to it, which
    decides each tool call.
    """

    name: str
    digest: str
    package_cache: tuple[str, int] | None = None
    mcp_proxy: tuple[str, int] | None = None

    def service_hosts(self) -> frozenset[str]:
        """Hosts that serve platform services; other ports on them are refused."""
        services = (self.package_cache, self.mcp_proxy)
        return frozenset(service[0] for service in services if service is not None)


@dataclass(frozen=True, slots=True)
class HttpRequestFacts:
    """What OpenShell reports about one request (``HttpRequestEvaluation``).

    ``headers`` are lowercased names and values in wire order, without the
    credential, routing, framing and hop-by-hop headers OpenShell withholds.
    ``config`` is the middleware entry's config from the sandbox's policy.
    """

    sandbox_id: str
    request_id: str
    scheme: str
    host: str
    port: int
    method: str
    path: str
    query: str
    headers: tuple[tuple[str, str], ...]
    body: bytes
    config: Mapping[str, object] | None


@dataclass(frozen=True, slots=True)
class Finding:
    """An audit-safe observation returned to OpenShell with a decision."""

    type: str
    label: str
    severity: str
    count: int = 1


@dataclass(frozen=True, slots=True)
class Verdict:
    """The middleware's answer: allow, or deny with a reason code.

    ``reason`` is a diagnostic for the middleware's own log; OpenShell never
    shows it to the sandbox or writes it to its logs.
    """

    allowed: bool
    reason_code: str
    reason: str
    findings: tuple[Finding, ...] = ()
    decision: Decision | None = None

    def __post_init__(self) -> None:
        if self.allowed != (self.reason_code == ""):
            raise ValueError("a verdict has a reason code exactly when it denies")
        if self.reason_code and not REASON_CODE_RE.fullmatch(self.reason_code):
            raise ValueError(f"invalid reason code {self.reason_code!r}")


def parse_config(config: Mapping[str, object] | None) -> PolicyRef:
    """Read the compiler's middleware config.

    ``{"policy": {"name": ..., "digest": ...}}``, optionally with
    ``"package_cache"`` and ``"mcp_proxy"``, each ``{"host": ..., "port": ...}``.
    """
    expected = (
        'config must be {"policy": {"name": <policy name>, "digest": <revision>}} and '
        'optionally "package_cache" and "mcp_proxy", each {"host": <name>, "port": <port>}'
    )
    keys = {"policy", "package_cache", "mcp_proxy"}
    if config is None or "policy" not in config or not set(config) <= keys:
        raise PolicyConfigError(expected)
    package_cache = _service(config.get("package_cache"), expected)
    mcp_proxy = _service(config.get("mcp_proxy"), expected)
    entry = config["policy"]
    if not isinstance(entry, Mapping):
        raise PolicyConfigError(expected)
    fields = cast("Mapping[str, object]", entry)
    name = fields.get("name")
    digest = fields.get("digest")
    if (
        set(fields) != {"name", "digest"}
        or not isinstance(name, str)
        or not isinstance(digest, str)
    ):
        raise PolicyConfigError(expected)
    try:
        check_digest(digest)
    except StoreError as exc:
        raise PolicyConfigError(str(exc)) from exc
    return PolicyRef(name=name, digest=digest, package_cache=package_cache, mcp_proxy=mcp_proxy)


def _service(entry: object, expected: str) -> tuple[str, int] | None:
    if entry is None:
        return None
    if not isinstance(entry, Mapping):
        raise PolicyConfigError(expected)
    fields = cast("Mapping[str, object]", entry)
    host = fields.get("host")
    port = fields.get("port")
    # protobuf Structs carry every number as a float.
    if isinstance(port, float) and port.is_integer():
        port = int(port)
    if (
        set(fields) != {"host", "port"}
        or not isinstance(host, str)
        or not _HOST_RE.fullmatch(host)
        or isinstance(port, bool)
        or not isinstance(port, int)
        or not 1 <= port <= 65535
    ):
        raise PolicyConfigError(expected)
    try:
        return normalize_host(host), port
    except ValueError as exc:
        raise PolicyConfigError(f"{expected}: {exc}") from exc


class PolicyMiddleware:
    """Decide requests for every sandbox bound in a policy store."""

    def __init__(
        self,
        store: PolicyStore,
        *,
        ceiling: PoolCeiling,
        floor: HardFloor,
        events: EventSink,
        meter: EgressMeter | None = None,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._store = store
        self._ceiling = ceiling
        self._floor = floor
        self._events = events
        self._meter = meter if meter is not None else EgressMeter()
        self._now = now
        self._monitor_alerts: dict[tuple[str, str], datetime] = {}
        self._lock = threading.Lock()

    def validate_config(self, config: Mapping[str, object] | None) -> str | None:
        """Return why a middleware entry's config is unacceptable, or None if it is valid.

        The gateway asks before it accepts a sandbox policy, so a compiled
        policy is refused unless its project policy is already in the store.
        """
        try:
            ref = parse_config(config)
            loaded = self._store.policy(ref.digest)
        except (PolicyConfigError, StoreError) as exc:
            return str(exc)
        if loaded.policy.metadata.name != ref.name:
            return (
                f"revision {ref.digest} is policy {loaded.policy.metadata.name!r}, not {ref.name!r}"
            )
        return None

    def evaluate_http(self, facts: HttpRequestFacts) -> Verdict:
        """Decide one HTTP request."""
        try:
            ref = parse_config(facts.config)
        except PolicyConfigError as exc:
            return _deny(CONFIG_INVALID, str(exc))
        try:
            bound = self._store.binding(facts.sandbox_id)
        except StoreError as exc:
            return _deny(SANDBOX_UNBOUND, str(exc))
        if bound is None:
            return _deny(SANDBOX_UNBOUND, f"sandbox {facts.sandbox_id} is not bound to a policy")
        if bound != ref.digest:
            return _deny(
                POLICY_STALE,
                f"sandbox {facts.sandbox_id} is bound to {bound}, but its compiled policy "
                f"names {ref.digest}",
            )
        try:
            loaded = self._store.policy(ref.digest)
        except StoreError as exc:
            return _deny(POLICY_UNAVAILABLE, str(exc))
        if loaded.policy.metadata.name != ref.name:
            return _deny(CONFIG_INVALID, f"revision {ref.digest} is not policy {ref.name!r}")
        try:
            destination = (normalize_host(facts.host), facts.port)
        except ValueError as exc:
            return _deny(REQUEST_UNCLASSIFIABLE, f"malformed destination host: {exc}")
        # The middleware runs before OpenShell injects provider credentials and
        # cannot see whether a request will get one. OpenShell substitutes a
        # credential only at the hosts the policy binds it to, so a request to
        # one of them counts as credentialed, on any port: its escalations time
        # out into an alert only with allow_credentialed ("Escalation timeout").
        credentialed = destination[0] in loaded.policy.credentials.hosts
        if credentialed and facts.port == CREDENTIAL_PORT and facts.scheme.lower() == "http":
            # OpenShell substitutes a bound credential on its port whatever the
            # transport, and forwards plain HTTP unencrypted. It reports "http"
            # only for requests it knows are plain (its forward proxy): through a
            # tunnel everything reads "https", so that route remains a residual
            # risk (docs/POLICY.md).
            reason = f"plain HTTP to {facts.host}:{facts.port}, where a credential is bound"
            return _deny(CREDENTIAL_UNENCRYPTED, reason)
        if destination == ref.mcp_proxy:
            return self._verdict(facts, loaded, _mcp_proxy_decision(loaded))
        if destination[0] in ref.service_hosts() and destination != ref.package_cache:
            # Platform services listen on these hosts; nothing else there is for workers.
            reason = f"{facts.host} serves platform services; port {facts.port} is not one"
            return _deny(DESTINATION_FORBIDDEN, reason)
        try:
            if destination == ref.package_cache:
                operation = _package_operation(facts)
            else:
                operation = classify_http_request(_http_request(facts, credentialed=credentialed))
        except ClassificationError as exc:
            decision = Decision(
                action=Action.DENY,
                layer="classification",
                reason=str(exc),
                policy_digest=loaded.digest,
            )
            return self._verdict(facts, loaded, decision)
        decision = evaluate(operation, loaded, ceiling=self._ceiling, floor=self._floor)
        if decision.action is not Action.DENY and _is_fetch_post(operation):
            try:
                _check_git_body(facts)
            except GitRequestError as exc:
                return _deny(GIT_REQUEST_INVALID, str(exc), decision)
        return self._verdict(facts, loaded, decision)

    def _verdict(
        self, facts: HttpRequestFacts, loaded: LoadedPolicy, decision: Decision
    ) -> Verdict:
        findings: tuple[Finding, ...]
        match decision.action:
            case Action.DENY:
                return _deny(_LAYER_CODES[decision.layer], decision.reason, decision)
            case Action.ESCALATE:
                try:
                    self._record("escalation", facts, loaded, decision)
                except Exception:
                    # No permission request exists, so the agent must not wait for an approval.
                    _log.exception("could not record escalation of request %s", facts.request_id)
                    return _deny(
                        AUDIT_UNAVAILABLE, "the permission request could not be recorded", decision
                    )
                return _deny(APPROVAL_REQUIRED, decision.reason, decision)
            case Action.ALERT:
                try:
                    self._record("alert", facts, loaded, decision)
                except Exception:
                    # An alert lets a request through only with a recorded finding.
                    _log.exception("could not record alert for request %s", facts.request_id)
                    return _deny(AUDIT_UNAVAILABLE, "the alert could not be recorded", decision)
                findings = (Finding("policy_alert", decision.rule or "alert", "medium"),)
            case Action.ALLOW:
                findings = ()
        findings += self._monitor_findings(facts, loaded)
        return Verdict(
            allowed=True,
            reason_code="",
            reason=decision.reason,
            findings=findings,
            decision=decision,
        )

    def _monitor_findings(
        self, facts: HttpRequestFacts, loaded: LoadedPolicy
    ) -> tuple[Finding, ...]:
        """Count the request's egress and report monitors whose threshold it crossed.

        A monitor is reported at most once an hour per sandbox; monitors never
        change the decision.
        """
        if not loaded.policy.monitors:
            return ()
        total = self._meter.add(facts.sandbox_id, _egress_bytes(facts))
        findings: list[Finding] = []
        now = self._now()
        for alert in evaluate_monitors(loaded, EgressUsage(egress_bytes_last_hour=total)):
            key = (facts.sandbox_id, alert.rule)
            with self._lock:
                last = self._monitor_alerts.get(key)
                if last is not None and now - last < _MONITOR_REPEAT:
                    continue
                self._monitor_alerts[key] = now
            findings.append(Finding("policy_monitor", alert.rule, "low"))
            try:
                self._events.record(
                    "monitor",
                    {
                        "sandbox_id": facts.sandbox_id,
                        "request_id": facts.request_id,
                        "policy": {"name": loaded.policy.metadata.name, "digest": loaded.digest},
                        "rule": alert.rule,
                        "reason": alert.reason,
                    },
                )
            except Exception:
                _log.exception("could not record monitor %s", alert.rule)
        return tuple(findings)

    def _record(
        self, kind: str, facts: HttpRequestFacts, loaded: LoadedPolicy, decision: Decision
    ) -> None:
        # The store adds the time; the payload keeps the decision and the request.
        payload: dict[str, object] = {
            "sandbox_id": facts.sandbox_id,
            "request_id": facts.request_id,
            "policy": {"name": loaded.policy.metadata.name, "digest": loaded.digest},
            "rule": decision.rule,
            "reason": decision.reason,
            # The query string can carry credential placeholders and data, so
            # only its size is kept.
            "request": {
                "method": facts.method,
                "scheme": facts.scheme,
                "host": facts.host,
                "port": facts.port,
                "path": facts.path,
                "query_bytes": len(facts.query.encode()),
            },
        }
        if decision.escalation is not None:
            payload["escalation"] = {
                "timeout_seconds": int(decision.escalation.timeout.total_seconds()),
                "on_timeout": decision.escalation.on_timeout.value,
            }
        self._events.record(kind, payload)


def _deny(code: str, reason: str, decision: Decision | None = None) -> Verdict:
    return Verdict(allowed=False, reason_code=code, reason=reason, decision=decision)


def _header_values(headers: Sequence[tuple[str, str]], name: str) -> list[str]:
    return [value for header, value in headers if header == name]


def _http_request(facts: HttpRequestFacts, *, credentialed: bool) -> HttpRequest:
    """Rebuild the request URL and header facts the library classifies."""
    scheme = facts.scheme.lower()
    url_scheme = _URL_SCHEMES.get(scheme)
    if url_scheme is None:
        raise ClassificationError(f"unsupported scheme {facts.scheme!r}")
    url_scheme = _PORT_SCHEMES.get(facts.port, url_scheme)
    if not _HOST_RE.fullmatch(facts.host):
        raise ClassificationError(f"malformed destination host {facts.host!r}")
    if not facts.path.startswith("/") or "?" in facts.path or "#" in facts.path:
        raise ClassificationError(f"request target {facts.path!r} is not an absolute path")
    host = f"[{facts.host}]" if ":" in facts.host else facts.host
    authority = host if facts.port == _DEFAULT_PORTS[url_scheme] else f"{host}:{facts.port}"
    url = f"{url_scheme}://{authority}{facts.path}"
    if facts.query:
        url += f"?{facts.query}"
    content_types = _header_values(facts.headers, "content-type")
    if len(content_types) > 1:
        raise ClassificationError("the request has more than one Content-Type header")
    upgrades = _header_values(facts.headers, "upgrade")
    upgrade: str | None = ", ".join(upgrades) if upgrades else None
    if upgrade is None and (
        scheme in ("ws", "wss")
        or any(name.startswith("sec-websocket-") for name, _ in facts.headers)
    ):
        upgrade = "websocket"
    return HttpRequest(
        method=facts.method,
        url=url,
        credentialed=credentialed,
        content_type=content_types[0] if content_types else None,
        has_body=bool(facts.body),
        upgrade=upgrade,
    )


def _package_operation(facts: HttpRequestFacts) -> PackageOperation:
    """Classify a request to the package cache (docs/PACKAGE-CACHE.md)."""
    if facts.query or facts.body:
        raise ClassificationError("package cache requests carry no query string or body")
    if facts.scheme.lower() in ("ws", "wss") or any(
        name == "upgrade" or name.startswith("sec-websocket-") for name, _ in facts.headers
    ):
        raise ClassificationError("protocol upgrades such as WebSocket are not supported")
    return classify_package_request(facts.method, facts.path)


def _mcp_proxy_decision(loaded: LoadedPolicy) -> Decision:
    """Worker code may use the MCP proxy, which decides each tool call, if the policy says so."""
    if loaded.policy.mcp.proxy.worker_access:
        return Decision(
            action=Action.ALLOW,
            layer="policy",
            reason="the MCP proxy decides each tool call",
            rule="mcp.proxy.worker_access",
            policy_digest=loaded.digest,
        )
    return Decision(
        action=Action.DENY,
        layer="policy",
        reason="worker code may not use the MCP proxy",
        rule="mcp.proxy.worker_access",
        policy_digest=loaded.digest,
    )


def _is_fetch_post(operation: HttpOperation | GitOperation | PackageOperation) -> bool:
    return (
        isinstance(operation, GitOperation)
        and operation.service == "upload-pack"
        and operation.method == "POST"
    )


def _check_git_body(facts: HttpRequestFacts) -> None:
    """Allow a git fetch to send only a well-formed upload-pack request body."""
    encodings = _header_values(facts.headers, "content-encoding")
    protocols = _header_values(facts.headers, "git-protocol")
    if len(encodings) > 1 or len(protocols) > 1:
        raise GitRequestError("repeated Content-Encoding or Git-Protocol header")
    check_upload_pack_request(
        facts.body,
        content_encoding=encodings[0] if encodings else None,
        git_protocol=protocols[0] if protocols else None,
    )


def _egress_bytes(facts: HttpRequestFacts) -> int:
    """Approximate bytes a request sends: request line, visible headers and body."""
    headers = sum(len(name) + len(value) + 4 for name, value in facts.headers)
    return len(facts.method) + len(facts.path) + len(facts.query) + headers + len(facts.body)
