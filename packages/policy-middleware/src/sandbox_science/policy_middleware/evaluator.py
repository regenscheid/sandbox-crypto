# SPDX-License-Identifier: MIT
"""Decide one sandbox request with the policy library, independent of gRPC.

OpenShell has already applied its network rules (admission endpoints, address
checks) when it asks; this module applies the project policy, the pool
ceiling and the hard floor to the request exactly, and turns the library's
decision into an allow or a deny with a reason code (docs/POLICY.md, "Mapping
to enforcement").
"""

from __future__ import annotations

import dataclasses
import logging
import re
import threading
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Final, cast

from sandbox_science.event_store import Record
from sandbox_science.policy import (
    Action,
    ClassificationError,
    Decision,
    EgressUsage,
    GitOperation,
    HardFloor,
    HttpOperation,
    HttpRequest,
    LoadedPolicy,
    Operation,
    PackageOperation,
    PoolCeiling,
    classify_http_request,
    classify_package_request,
    evaluate,
    evaluate_mcp_message,
    evaluate_monitors,
    normalize_host,
)
from sandbox_science.policy.approvals import (
    Approvals,
    Approved,
    Denied,
    RequestKey,
    Resolution,
    TimedOut,
    Unrecorded,
    Waiting,
)
from sandbox_science.policy.mcp_wire import params_digest
from sandbox_science.policy.openshell import CREDENTIAL_PORT
from sandbox_science.policy.package_requests import SANDBOX_HEADER
from sandbox_science.policy.reasons import (
    APPROVAL_DENIED,
    APPROVAL_REQUIRED,
    AUDIT_UNAVAILABLE,
    DESTINATION_FORBIDDEN,
    ESCALATION_TIMED_OUT,
    LAYER_CODES,
    POLICY_UNAVAILABLE,
    REQUEST_CODES,
    REQUEST_UNCLASSIFIABLE,
    with_request,
)

from .archives import MAX_PACKAGES, ArchiveLookup
from .errors import MiddlewareError, StoreError
from .events import EventSink
from .gitproto import GitRequestError, check_upload_pack_request
from .mcp import (
    Listing,
    ListingCheck,
    McpCall,
    McpRef,
    McpRequestError,
    ToolListings,
    declared_server,
    read_call,
)
from .meter import EgressMeter
from .store import PolicyStore, check_digest

_log = logging.getLogger(__name__)

# Reason codes OpenShell returns to the sandbox with a denial. The agent reads
# them, so each names what the agent can do about the denial.
GIT_REQUEST_INVALID: Final = "git_request_invalid"
"""A git fetch whose body is not a well-formed upload-pack request."""
SANDBOX_UNBOUND: Final = "sandbox_unbound"
"""The orchestrator has not bound the sandbox to a policy revision."""
POLICY_STALE: Final = "policy_stale"
"""The sandbox's compiled policy names a revision other than the one it is bound to."""
CONFIG_INVALID: Final = "config_invalid"
"""The middleware entry's config is malformed or names another policy."""
CREDENTIAL_UNENCRYPTED: Final = "credential_unencrypted"
"""Plain HTTP to the port a credential is bound to, which would carry it unencrypted; use HTTPS."""

REASON_CODE_RE: Final = re.compile(r"[a-z][a-z0-9_]{0,63}")
"""OpenShell's format for reason codes; anything else is a middleware failure."""

_RECORDING_LOCKS: Final = 64
_RECORDING_WAIT: Final = 0.25
"""Seconds an escalation waits for its fingerprint's lock, as long as the store waits to
append: past it, the request is refused and its retry records or finds the request."""
_URL_SCHEMES: Final = {"http": "http", "https": "https", "ws": "http", "wss": "https"}
_DEFAULT_PORTS: Final = {"http": 80, "https": 443}
# OpenShell v0.1.3 labelled everything through a CONNECT tunnel "https", even
# plain HTTP to port 80; since v0.1.4 the tunnel reports "http" for plain HTTP it
# detects. Rules without ports match the scheme's default port, so the port
# decides on the default ports either way; elsewhere the scheme only changes the
# URL's text and length.
_PORT_SCHEMES: Final = {80: "http", 443: "https"}
_HOST_RE: Final = re.compile(r"[A-Za-z0-9._-]+|[0-9A-Fa-f:.]+")
_NAME_RE: Final = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
_MONITOR_REPEAT: Final = timedelta(hours=1)


_Payload = Callable[[Mapping[str, object] | None], dict[str, object]]
"""Builds a record of the request being decided, with extra fields."""


class PolicyConfigError(MiddlewareError):
    """A middleware entry's ``config`` does not name a policy revision correctly."""


@dataclass(frozen=True, slots=True)
class PolicyRef:
    """The project policy revision a compiled sandbox policy was built from.

    ``package_cache`` and ``mcp_proxy`` are where the sandbox's platform services
    listen (host, port), when the compiled policy admits them: requests to the
    cache are package operations, and requests to the MCP proxy go to it, which
    decides each tool call. ``mcp`` marks a sandbox whose agent calls the declared
    MCP servers directly (``compile_agent_mcp``): every request is an MCP call.
    """

    name: str
    digest: str
    package_cache: tuple[str, int] | None = None
    mcp_proxy: tuple[str, int] | None = None
    mcp: McpRef | None = None

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
    headers: tuple[tuple[str, str], ...] = ()
    """Request headers to write on an allowed request, replacing any the sandbox sent."""
    request: int | None = None
    """The permission request a refusal concerns, when it is known."""

    def __post_init__(self) -> None:
        if self.allowed != (self.reason_code == ""):
            raise ValueError("a verdict has a reason code exactly when it denies")
        if self.reason_code and not REASON_CODE_RE.fullmatch(self.reason_code):
            raise ValueError(f"invalid reason code {self.reason_code!r}")


def parse_config(config: Mapping[str, object] | None) -> PolicyRef:
    """Read the compiler's middleware config.

    ``{"policy": {"name": ..., "digest": ...}}``, optionally with
    ``"package_cache"`` and ``"mcp_proxy"``, each ``{"host": ..., "port": ...}``, or
    with ``"mcp": {"project": ..., "caller": ...}`` alone.
    """
    expected = (
        'config must be {"policy": {"name": <policy name>, "digest": <revision>}} and '
        'optionally "package_cache" and "mcp_proxy", each {"host": <name>, "port": <port>}, '
        'or "mcp": {"project": <name>, "caller": <name>}'
    )
    keys = {"policy", "package_cache", "mcp_proxy", "mcp"}
    if config is None or "policy" not in config or not set(config) <= keys:
        raise PolicyConfigError(expected)
    package_cache = _service(config.get("package_cache"), expected)
    mcp_proxy = _service(config.get("mcp_proxy"), expected)
    mcp = _mcp_ref(config.get("mcp"), expected)
    if mcp is not None and (package_cache is not None or mcp_proxy is not None):
        raise PolicyConfigError(expected)
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
    return PolicyRef(
        name=name, digest=digest, package_cache=package_cache, mcp_proxy=mcp_proxy, mcp=mcp
    )


def _mcp_ref(entry: object, expected: str) -> McpRef | None:
    if entry is None:
        return None
    empty: Mapping[str, object] = {}
    fields = cast("Mapping[str, object]", entry) if isinstance(entry, Mapping) else empty
    project, caller = fields.get("project"), fields.get("caller")
    if (
        set(fields) != {"project", "caller"}
        or not isinstance(project, str)
        or not isinstance(caller, str)
        or not _NAME_RE.fullmatch(project)
        or not _NAME_RE.fullmatch(caller)
    ):
        raise PolicyConfigError(expected)
    return McpRef(project=project, caller=caller)


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
        archives: ArchiveLookup | None = None,
        requests: Callable[[int], Iterable[Record]] | None = None,
        listings: ToolListings | None = None,
    ) -> None:
        """``archives``, when given, names the opam packages of escalated opam archives.

        ``requests`` reads the event store's records after a sequence number, from which
        permission requests and their answers are known
        (``sandbox_science.policy.approvals``). Without it,
        every escalation is recorded and denied, and none can be approved.
        ``listings`` holds pinned tool lists, against which the tool listings that
        sandboxes calling MCP servers directly receive are compared.
        """
        self.listings = listings
        self._store = store
        self._ceiling = ceiling
        self._floor = floor
        self._events = events
        self._meter = meter if meter is not None else EgressMeter()
        self._now = now
        self._archives = archives
        self._monitor_alerts: dict[tuple[str, str], datetime] = {}
        self._lock = threading.Lock()
        self._requests = requests
        self._approvals = Approvals()
        self._recording = tuple(threading.Lock() for _ in range(_RECORDING_LOCKS))
        """Held while a permission request is recorded, by its fingerprint: identical
        escalations evaluated at once (on the gRPC thread pool) record one request."""

    def prime_approvals(self) -> None:
        """Read the permission requests and answers already stored, at startup, so that
        no request's evaluation folds the whole store within its time budget."""
        self._refresh_approvals()

    def _refresh_approvals(self) -> None:
        if self._requests is None:
            return
        try:
            self._approvals.load(self._requests(self._approvals.last_seq))
        except Exception:
            # Without the records no answer is known; the request waits, as before.
            _log.exception("could not read permission requests from the event store")

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
        if ref.mcp is not None:
            return self._evaluate_mcp(facts, ref.mcp, loaded)
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
            # transport, and forwards plain HTTP unencrypted. Its forward proxy
            # reports such requests as "http", and since v0.1.4 so does its tunnel
            # when it detects plain HTTP (docs/POLICY.md, "Residual risks").
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
        verdict = self._verdict(facts, loaded, decision, operation)
        if verdict.allowed and destination == ref.package_cache:
            # The cache budgets upstream fetches by sandbox; a sandbox cannot name another.
            verdict = dataclasses.replace(verdict, headers=((SANDBOX_HEADER, facts.sandbox_id),))
        return verdict

    def _verdict(
        self,
        facts: HttpRequestFacts,
        loaded: LoadedPolicy,
        decision: Decision,
        operation: Operation | None = None,
        *,
        target: RequestKey | None = None,
        payload: _Payload | None = None,
    ) -> Verdict:
        """The verdict for a decision. ``target`` and ``payload`` replace the request's
        key and records for a request that is not plain HTTP (an MCP call)."""
        findings: tuple[Finding, ...]

        def http_payload(extra: Mapping[str, object] | None) -> dict[str, object]:
            return self._payload(facts, loaded, decision, operation, extra=extra)

        payload = payload or http_payload
        match decision.action:
            case Action.DENY:
                return _deny(LAYER_CODES[decision.layer], decision.reason, decision)
            case Action.ESCALATE:
                answered = self._escalation(facts, loaded, decision, operation, target, payload)
                if isinstance(answered, Verdict):
                    return answered
                findings = answered
            case Action.ALERT:
                try:
                    self._events.record("alert", payload(None))
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

    def _escalation(
        self,
        facts: HttpRequestFacts,
        loaded: LoadedPolicy,
        decision: Decision,
        operation: Operation | None,
        target: RequestKey | None,
        payload: _Payload,
    ) -> Verdict | tuple[Finding, ...]:
        """Answer an escalated request from its permission request and any answers to it.

        A denial, or a request still waiting, is a Verdict; an approval, or a read whose
        request timed out, proceeds with the findings returned.
        """
        terms = decision.escalation
        if terms is None:  # the library pairs terms with every escalation
            return _deny(APPROVAL_REQUIRED, decision.reason, decision)
        if target is None:
            target = RequestKey.http(
                facts.sandbox_id,
                loaded.digest,
                facts.method.upper(),
                normalize_host(facts.host),
                facts.port,
                facts.path,
            )
        self._refresh_approvals()
        now = self._now()
        rule = decision.rule or "escalation"
        resolution = self._approvals.resolve(target, now)

        def approved(extra: Mapping[str, object]) -> Verdict | tuple[Finding, ...]:
            # An approved request proceeds only once that is recorded.
            try:
                self._events.record("approved", payload(extra))
            except Exception:
                _log.exception("could not record approved request %s", facts.request_id)
                return _deny(
                    AUDIT_UNAVAILABLE, "the approved request could not be recorded", decision
                )
            return (Finding("policy_approval", rule, "low"),)

        if isinstance(operation, PackageOperation) and operation.kind == "artifact":
            # A provisioning operation's approved file set (Conda provisioning, CP-8): the
            # files it lists download to this sandbox under this revision, nothing else. An
            # answer to this very download that came later still decides it.
            grant = self._approvals.granted(facts.sandbox_id, loaded.digest, facts.path, now)
            answered = resolution.answer if isinstance(resolution, Approved | Denied) else None
            if grant is not None and (answered is None or answered.seq < grant.seq):
                return approved(
                    {
                        "approval_seq": grant.seq,
                        "request_seq": grant.request_seq,
                        "grant": "packages",
                    }
                )
        # At most twice: inside its lock, an escalation records the request or returns
        # how it stands, never Unrecorded.
        while True:
            match resolution:
                case Denied(answer):
                    reason = f"permission request {answer.request_seq} was denied"
                    return _deny(APPROVAL_DENIED, reason, decision, answer.request_seq)
                case Approved(answer):
                    return approved({"approval_seq": answer.seq, "request_seq": answer.request_seq})
                case Unrecorded():
                    outcome = self._escalate(facts, decision, target, payload)
                    if isinstance(outcome, Verdict):
                        return outcome
                    # Answered or recorded meanwhile: decided as any other request is.
                    resolution = outcome
                case Waiting(pending):
                    reason = f"{decision.reason} (permission request {pending.seq}, still waiting)"
                    return _deny(APPROVAL_REQUIRED, reason, decision, pending.seq)
                case TimedOut(pending) if pending.on_timeout == "alert":
                    extra = {"timed_out_request": pending.seq}
                    try:
                        self._events.record("alert", payload(extra))
                    except Exception:
                        _log.exception("could not record alert for request %s", facts.request_id)
                        return _deny(AUDIT_UNAVAILABLE, "the alert could not be recorded", decision)
                    return (Finding("policy_alert", rule, "medium"),)
                case TimedOut(pending):
                    minutes = int(pending.timeout.total_seconds() // 60)
                    reason = (
                        f"permission request {pending.seq} was not answered"
                        f" within {minutes} minutes"
                    )
                    return _deny(ESCALATION_TIMED_OUT, reason, decision, pending.seq)
                case _:
                    # A state this code does not know refuses: only the cases above proceed.
                    return _deny(
                        ESCALATION_TIMED_OUT, "the permission request is undecided", decision
                    )

    def _escalate(
        self,
        facts: HttpRequestFacts,
        decision: Decision,
        target: RequestKey,
        payload: _Payload,
    ) -> Verdict | Resolution:
        """Record a permission request for ``target``, once, and refuse: an identical
        escalation evaluated at the same time waits on its fingerprint's lock, then finds
        the request recorded. A request answered or recorded meanwhile is returned for the
        caller to decide; every other outcome is a refusal."""
        try:
            # Built before the lock: the opam archive lookup can take a while.
            record = payload({"target": target.payload()})
        except Exception:
            _log.exception("could not build the escalation of request %s", facts.request_id)
            return _deny(
                AUDIT_UNAVAILABLE, "the permission request could not be recorded", decision
            )
        fingerprint = target.fingerprint()
        lock = self._recording[int(fingerprint[:8], 16) % _RECORDING_LOCKS]
        if not lock.acquire(timeout=_RECORDING_WAIT):
            # Another escalation is being recorded; a retry records or finds this one.
            reason = f"{decision.reason} (a permission request is being recorded; retry)"
            return _deny(APPROVAL_REQUIRED, reason, decision)
        try:
            self._refresh_approvals()
            resolution = self._approvals.resolve(target, self._now())
            if not isinstance(resolution, Unrecorded):
                return resolution
            try:
                seq = self._events.record("escalation", record)
            except Exception:
                # No permission request exists, so the agent must not wait for an approval.
                _log.exception("could not record escalation of request %s", facts.request_id)
                return _deny(
                    AUDIT_UNAVAILABLE, "the permission request could not be recorded", decision
                )
            # Known at once, so a retry before the next read waits on it too.
            self._refresh_approvals()
        finally:
            lock.release()
        named = f" (permission request {seq})" if seq is not None else ""
        return _deny(APPROVAL_REQUIRED, decision.reason + named, decision, seq)

    def check_listing(
        self, request_id: str, sandbox_id: str, *, stream: bool
    ) -> ListingCheck | None:
        """A check of the answer to an allowed ``tools/list`` of ``sandbox_id``'s, or None
        for any other response, which passes unread."""
        if self.listings is None:
            return None
        listing = self.listings.answer(request_id)
        if listing is None or listing.sandbox_id != sandbox_id:
            return None
        return ListingCheck(
            self.listings, listing, stream=stream, events=self._events, request_id=request_id
        )

    def _evaluate_mcp(self, facts: HttpRequestFacts, ref: McpRef, loaded: LoadedPolicy) -> Verdict:
        """Decide a request of a sandbox whose agent calls the declared MCP servers directly.

        Every request must be one MCP message to a declared server's endpoint, read as the
        MCP proxy reads one; the library's tool rules decide it, with the orchestrator as
        its origin, and the call is recorded. Escalations are permission requests keyed as
        the proxy keys them, and a refusal that concerns one names it in its reason code.
        """
        call: McpCall | None = None
        try:
            server = declared_server(loaded, normalize_host(facts.host), facts.port, facts.path)
            if server is None or facts.scheme.lower() != "https":
                raise McpRequestError(
                    DESTINATION_FORBIDDEN,
                    f"{facts.scheme}://{facts.host}:{facts.port} is not a declared MCP server",
                )
            call = read_call(
                server,
                facts.method,
                facts.query,
                facts.headers,
                facts.body,
                max_argument_bytes=int(loaded.policy.mcp.proxy.max_argument_bytes),
            )
        except McpRequestError as exc:
            verdict = _deny(exc.code, str(exc))
        except ValueError as exc:
            verdict = _deny(REQUEST_UNCLASSIFIABLE, f"malformed destination host: {exc}")
        else:
            verdict = self._mcp_verdict(facts, ref, loaded, call)
        self._record_mcp_call(facts, ref, loaded, call, verdict)
        return verdict

    def _mcp_verdict(
        self, facts: HttpRequestFacts, ref: McpRef, loaded: LoadedPolicy, call: McpCall
    ) -> Verdict:
        decision = evaluate_mcp_message(
            call.server,
            call.message,
            loaded,
            ceiling=self._ceiling,
            floor=self._floor,
            hints=(
                self.listings.hints(facts.sandbox_id, call.server)
                if self.listings is not None
                else None
            ),
        )
        subject = call.subject(ref, facts.sandbox_id)
        target = RequestKey.mcp(
            ref.project,
            ref.caller,
            loaded.digest,
            call.server,
            call.method,
            call.tool or "",
            params_digest(call.params),
        )

        def payload(extra: Mapping[str, object] | None) -> dict[str, object]:
            record: dict[str, object] = {
                **subject,
                "request_id": facts.request_id,
                "policy": {"name": loaded.policy.metadata.name, "digest": loaded.digest},
                "rule": decision.rule,
                "reason": decision.reason,
                "effect": decision.effect,
                "max_grant": decision.max_grant,
            }
            if decision.escalation is not None:
                record["escalation"] = {
                    "timeout_seconds": int(decision.escalation.timeout.total_seconds()),
                    "on_timeout": decision.escalation.on_timeout.value,
                }
            record.update(extra or {})
            return record

        verdict = self._verdict(facts, loaded, decision, target=target, payload=payload)
        if verdict.request is not None and verdict.reason_code in REQUEST_CODES:
            code = with_request(verdict.reason_code, verdict.request)
            verdict = dataclasses.replace(verdict, reason_code=code)
        if verdict.allowed and call.method == "tools/list" and self.listings is not None:
            server = loaded.policy.mcp.servers[call.server]
            listing = Listing(
                facts.sandbox_id,
                call.server,
                subject,
                call.cursor is None,
                self.listings.pin(call.server, server),
            )
            self.listings.expect(facts.request_id, listing)
        return verdict

    def _record_mcp_call(
        self,
        facts: HttpRequestFacts,
        ref: McpRef,
        loaded: LoadedPolicy,
        call: McpCall | None,
        verdict: Verdict,
    ) -> None:
        """One ``mcp-call`` record for every request, on a best-effort basis, as the MCP
        proxy writes one: never the arguments, only their size."""
        decision = verdict.decision
        subject = (
            call.subject(ref, facts.sandbox_id)
            if call is not None
            else {"project": ref.project, "caller": ref.caller, "sandbox_id": facts.sandbox_id}
        )
        payload: dict[str, object] = {
            **subject,
            "request_id": facts.request_id,
            "policy": {"name": loaded.policy.metadata.name, "digest": loaded.digest},
            "decision": (
                {
                    "action": decision.action.value,
                    "layer": decision.layer,
                    "rule": decision.rule,
                    **(
                        {"effect": decision.effect, "max_grant": decision.max_grant}
                        if decision.effect is not None
                        else {}
                    ),
                }
                if decision is not None
                else None
            ),
            "reason_code": verdict.reason_code or None,
            "outcome": "admitted" if verdict.allowed else "refused",
            "sizes": {
                "body": len(facts.body),
                **({"params": len(call.params)} if call is not None else {}),
            },
        }
        if verdict.request is not None:
            payload["permission_request"] = verdict.request
        try:
            self._events.record("mcp-call", payload)
        except Exception:
            _log.exception("could not record an MCP call of request %s", facts.request_id)

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

    def _payload(
        self,
        facts: HttpRequestFacts,
        loaded: LoadedPolicy,
        decision: Decision,
        operation: Operation | None = None,
        *,
        target: RequestKey | None = None,
        extra: Mapping[str, object] | None = None,
    ) -> dict[str, object]:
        """A record of the request and its decision."""
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
        if isinstance(operation, PackageOperation) and operation.digest is not None:
            payload["opam_archive"] = self._opam_archive(operation.digest)
        if target is not None:
            payload["target"] = target.payload()
        if extra:
            payload.update(extra)
        return payload

    def _opam_archive(self, digest: str) -> dict[str, object]:
        """An opam archive's digest and, if the package cache can tell, its packages.

        opam fetches archives by digest, so the request names no package; the
        cache finds the packages in the opam index it holds. An empty list means
        that no package in that index lists the digest.
        """
        archive: dict[str, object] = {"digest": digest}
        packages = None
        if self._archives is not None:
            try:
                packages = self._archives.packages(digest)
            except Exception:
                # The record matters more than the names: never lose it to the lookup.
                _log.exception("could not look up the packages of opam archive %s", digest)
        if packages is not None:
            archive["packages"] = list(packages[:MAX_PACKAGES])
            if len(packages) > MAX_PACKAGES:
                archive["more_packages"] = len(packages) - MAX_PACKAGES
        return archive


def _deny(
    code: str, reason: str, decision: Decision | None = None, request: int | None = None
) -> Verdict:
    return Verdict(
        allowed=False, reason_code=code, reason=reason, decision=decision, request=request
    )


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
