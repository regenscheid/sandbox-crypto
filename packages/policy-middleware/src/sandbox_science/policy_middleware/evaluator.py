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
import functools
import logging
import re
import threading
from collections import OrderedDict
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
    EscalationTerms,
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
    PACKAGES_MAX_GRANT,
    Approvals,
    Approved,
    Denied,
    RequestKey,
    Resolution,
    TimedOut,
    Unrecorded,
    Waiting,
    body_digest,
    http_summary,
    network_max_grant,
)
from sandbox_science.policy.classification import ServerClassification
from sandbox_science.policy.detection import (
    ACTION_URL,
    DAILY_CEILING,
    HOLD_TIMEOUT,
    LISTING_REPORT_GRACE,
    RULE_PREFIX,
    TOOL_LIST_DRIFT,
    Call,
    Detector,
    DriftLedger,
    Holds,
    Subject,
    UnreadListings,
    action_like,
    pin_digest,
)
from sandbox_science.policy.detection import Finding as Detected
from sandbox_science.policy.mcp_answers import callable_tools, tool_summary
from sandbox_science.policy.mcp_wire import listed_tools, params_digest
from sandbox_science.policy.openshell import CREDENTIAL_PORT
from sandbox_science.policy.package_requests import SANDBOX_HEADER
from sandbox_science.policy.reasons import (
    APPROVAL_DENIED,
    APPROVAL_REQUIRED,
    ARGUMENTS_TOO_LARGE,
    AUDIT_UNAVAILABLE,
    CREDENTIAL_IN_BODY,
    DESTINATION_FORBIDDEN,
    ESCALATION_TIMED_OUT,
    LAYER_CODES,
    POLICY_DENIED,
    POLICY_UNAVAILABLE,
    POOL_CEILING_DENIED,
    REQUEST_CODES,
    REQUEST_UNCLASSIFIABLE,
    SESSION_HELD,
    with_request,
)

from .archives import MAX_PACKAGES, ArchiveLookup
from .errors import MiddlewareError, StoreError
from .events import EventSink
from .gitproto import GitRequestError, check_upload_pack_request
from .inspection import AnswerInspection, Inspection, Tracked, run_key
from .mcp import (
    LISTINGS_OFF,
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
_HELD_REFRESH: Final = timedelta(seconds=1)
"""How often held or drifted calls read the store for the user's answer, at most."""
_MAX_OUTCOMES: Final = 4096
"""Allowed requests whose response status the middleware waits for, oldest forgotten first."""
_REFUSALS: Final = frozenset(
    {
        POLICY_DENIED,
        POOL_CEILING_DENIED,
        DESTINATION_FORBIDDEN,
        REQUEST_UNCLASSIFIABLE,
        APPROVAL_DENIED,
        ESCALATION_TIMED_OUT,
        ARGUMENTS_TOO_LARGE,
        CREDENTIAL_IN_BODY,
        GIT_REQUEST_INVALID,
        CREDENTIAL_UNENCRYPTED,
    }
)
"""Refusals the denial streak counts: not a request waiting for an answer, nor a hold."""


_LISTINGS_OFF_REASON: Final = (
    "a sandbox that calls MCP servers directly needs a middleware that reads their tool "
    "listings (mcp_listings = true), so that the agent is offered no tool it cannot use"
)

_Payload = Callable[[Mapping[str, object] | None], dict[str, object]]
"""Builds a record of the request being decided, with extra fields."""


class PolicyConfigError(MiddlewareError):
    """A middleware entry's ``config`` does not name a policy revision correctly."""


@dataclass(frozen=True, slots=True)
class PolicyRef:
    """The project policy revision a compiled sandbox policy was built from.

    ``package_cache`` is where the sandbox's package cache listens (host, port), when
    the compiled policy admits it: requests to it are package operations. ``mcp`` marks
    a sandbox whose agent calls the declared MCP servers directly (``compile_agent_mcp``):
    every request is an MCP call. ``retired_hosts`` holds the MCP proxy's host that a
    sandbox compiled before the proxy retired (2026-10-26, D98) still names: nothing
    serves there, and every request to it is refused.
    """

    name: str
    digest: str
    package_cache: tuple[str, int] | None = None
    mcp: McpRef | None = None
    retired_hosts: frozenset[str] = frozenset()

    def service_hosts(self) -> frozenset[str]:
        """Hosts that serve (or served) platform services; other ports on them are refused."""
        cache = frozenset((self.package_cache[0],)) if self.package_cache else frozenset[str]()
        return cache | self.retired_hosts


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
    ``"package_cache"``, ``{"host": ..., "port": ...}``, or with
    ``"mcp": {"project": ..., "caller": ...}`` alone. A sandbox compiled before the MCP
    proxy retired may also name ``"mcp_proxy"`` beside the cache, of the same shape: its
    host is kept only so that every request to it is refused.
    """
    expected = (
        'config must be {"policy": {"name": <policy name>, "digest": <revision>}} and '
        'optionally "package_cache": {"host": <name>, "port": <port>}, '
        'or "mcp": {"project": <name>, "caller": <name>}'
    )
    keys = {"policy", "package_cache", "mcp_proxy", "mcp"}
    if config is None or "policy" not in config or not set(config) <= keys:
        raise PolicyConfigError(expected)
    package_cache = _service(config.get("package_cache"), expected)
    retired_proxy = _service(config.get("mcp_proxy"), expected)
    mcp = _mcp_ref(config.get("mcp"), expected)
    if mcp is not None and (package_cache is not None or retired_proxy is not None):
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
    retired = frozenset((retired_proxy[0],)) if retired_proxy else frozenset[str]()
    return PolicyRef(
        name=name, digest=digest, package_cache=package_cache, mcp=mcp, retired_hosts=retired
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
        response_statuses: bool = False,
        listing_report_grace: timedelta = LISTING_REPORT_GRACE,
        inspection: Inspection | None = None,
    ) -> None:
        """``archives``, when given, names the opam packages of escalated opam archives.

        ``requests`` reads the event store's records after a sequence number, from which
        permission requests and their answers are known
        (``sandbox_science.policy.approvals``). Without it,
        every escalation is recorded and denied, and none can be approved.
        ``listings`` holds pinned tool lists, against which the tool listings that
        sandboxes calling MCP servers directly receive are compared. With
        ``response_statuses``, the service reads every response's status, and failures
        count towards the detection rules (``host-failures``). ``listing_report_grace`` is
        how long the agent runtime has to report a listing this service let pass unread
        before the server's non-read calls ask (D98). ``inspection``, when given, records
        the arguments and answers of the MCP calls sandboxes send servers directly, and
        checks the answers for the policy's patterns (it needs ``listings``, whose response
        binding it uses).
        """
        self.listings = listings
        self.inspection = inspection if listings is not None else None
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
        self.response_statuses = response_statuses
        self._detector = Detector()
        self._holds = Holds()
        self._drifts = DriftLedger()
        self._unread = UnreadListings(listing_report_grace)
        self._hold_lock = threading.Lock()
        self._held_read = datetime.min.replace(tzinfo=UTC)
        self._hold_projects: dict[str, str] = {}
        """The project count each open hold's subject counts towards, to clear a ceiling."""
        self._outcomes: OrderedDict[str, tuple[Subject, str, LoadedPolicy, str, str]] = (
            OrderedDict()
        )
        """Allowed requests whose response status counts, by OpenShell's request id."""

    def prime_approvals(self) -> None:
        """Read the permission requests and answers already stored, at startup, so that
        no request's evaluation folds the whole store within its time budget."""
        self._refresh_approvals()

    def _refresh_approvals(self) -> None:
        if self._requests is None:
            return
        try:
            records = list(self._requests(self._approvals.last_seq))
        except Exception:
            # Without the records no answer is known; the request waits, as before.
            _log.exception("could not read permission requests from the event store")
            return
        self._approvals.load(records)
        self._drifts.load(records)
        self._unread.load(records)
        for answered in self._holds.load(records):
            project = self._hold_projects.pop(answered.subject.key, None)
            if answered.cleared:
                # Only new calls hold it again; a stopped session's counts stand.
                ceiling = answered.rule == DAILY_CEILING
                self._detector.clear(answered.subject, project if ceiling else None)

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
        if ref.mcp is not None and self.listings is None:
            return _LISTINGS_OFF_REASON
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
            if self.listings is None:
                return _deny(LISTINGS_OFF, _LISTINGS_OFF_REASON)
            return self._evaluate_mcp(facts, ref.mcp, loaded)
        try:
            destination = (normalize_host(facts.host), facts.port)
        except ValueError as exc:
            return _deny(REQUEST_UNCLASSIFIABLE, f"malformed destination host: {exc}")
        if destination == ref.package_cache:
            # A platform service: the cache budgets its own fetches.
            return self._decide_http(facts, loaded, ref, destination)
        # Research code's requests name no session: its calls are held by sandbox (D103).
        subject = Subject(facts.sandbox_id)
        project = f"policy:{loaded.policy.metadata.name}"
        query = f"?({len(facts.query.encode())} bytes)" if facts.query else ""
        call = Call(
            destination[0],
            facts.method.upper(),
            f"{facts.port}{facts.path}?{facts.query}",
            path=facts.path,
            label=facts.path + query,
        )
        held = self._detect(facts.request_id, loaded, subject, project, call, "code")
        if held is not None:
            return held
        verdict = self._decide_http(facts, loaded, ref, destination)
        return self._counted(facts.request_id, loaded, subject, project, call.host, verdict, "code")

    def _decide_http(
        self,
        facts: HttpRequestFacts,
        loaded: LoadedPolicy,
        ref: PolicyRef,
        destination: tuple[str, int],
    ) -> Verdict:
        """Decide a request of research code's under its bound policy."""
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
        decision = _action_url(facts, loaded, operation, decision)
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
        card: Mapping[str, object] | None = None,
    ) -> Verdict:
        """The verdict for a decision. ``target``, ``payload`` and ``card`` replace the
        request's key, records and what its permission request's card shows for a request
        that is not plain HTTP (an MCP call)."""
        findings: tuple[Finding, ...]

        def http_payload(extra: Mapping[str, object] | None) -> dict[str, object]:
            return self._payload(facts, loaded, decision, operation, extra=extra)

        payload = payload or http_payload
        match decision.action:
            case Action.DENY:
                return _deny(LAYER_CODES[decision.layer], decision.reason, decision)
            case Action.ESCALATE:
                answered = self._escalation(
                    facts, loaded, decision, operation, target, payload, card
                )
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
        card: Mapping[str, object] | None = None,
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
                body_digest(facts.body),
            )
            asked = _http_card(facts, operation)
            if (decision.rule or "").startswith(RULE_PREFIX):
                asked["max_grant"] = "once"  # a detection's hold is answered call by call
        else:
            asked = card or {"caller_kind": "agent"}  # an MCP call the run service sent (D98)
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
                    outcome = self._escalate(facts, decision, target, payload, asked)
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
        asked: Mapping[str, object],
    ) -> Verdict | Resolution:
        """Record a permission request for ``target``, once, and refuse: an identical
        escalation evaluated at the same time waits on its fingerprint's lock, then finds
        the request recorded. A request answered or recorded meanwhile is returned for the
        caller to decide; every other outcome is a refusal. ``asked`` is what the request's
        card shows and its grants compare (D97)."""
        try:
            # Built before the lock: the opam archive lookup can take a while.
            record = payload({**asked, "target": target.payload()})
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
            self.listings,
            listing,
            stream=stream,
            events=self._events,
            request_id=request_id,
            drifts=self._drifts,
            unread=self._unread,
            now=self._now(),
        )

    def _evaluate_mcp(self, facts: HttpRequestFacts, ref: McpRef, loaded: LoadedPolicy) -> Verdict:
        """Decide a request of a sandbox whose agent calls the declared MCP servers directly.

        Every request must be one MCP message to a declared server's endpoint, read as the
        MCP proxy reads one; the library's tool rules decide it, with the orchestrator as
        its origin, and the call is recorded. Escalations are permission requests keyed as
        the proxy keys them, and a refusal that concerns one names it in its reason code.
        """
        call: McpCall | None = None
        refused: bytes | None = None
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
            refused = exc.params
        except ValueError as exc:
            verdict = _deny(REQUEST_UNCLASSIFIABLE, f"malformed destination host: {exc}")
        else:
            subject = Subject(facts.sandbox_id, ref.project, call.session)
            project = f"project:{ref.project}"
            target = f"{call.method} {call.tool or ''} {params_digest(call.params)}"
            counted = Call(server, call.method, target, label=f"{call.method} {call.tool or ''}")
            host = normalize_host(facts.host)
            held = self._detect(facts.request_id, loaded, subject, project, counted, "agent")
            verdict = held or self._counted(
                facts.request_id,
                loaded,
                subject,
                project,
                host,
                self._mcp_verdict(facts, ref, loaded, call),
                "agent",
            )
        self._record_mcp_call(facts, ref, loaded, call, verdict, refused)
        if verdict.allowed and call is not None and self.inspection is not None:
            subject = call.subject(ref, facts.sandbox_id)
            rpc_id = call.message.get("id")
            self.inspection.expect(
                facts.request_id,
                Tracked(
                    facts.sandbox_id,
                    subject,
                    rpc_id if isinstance(rpc_id, str | int) else None,
                    self.inspection.patterns(loaded, call.server),
                    run_key(subject),
                ),
            )
        return verdict

    def inspect_answer(
        self, request_id: str, sandbox_id: str, *, status: int, media: str | None
    ) -> AnswerInspection | None:
        """The inspection of the answer to an admitted MCP call of ``sandbox_id``'s, or
        None for any other response."""
        if self.inspection is None:
            return None
        return self.inspection.answer(request_id, sandbox_id, status=status, media=media)

    def _mcp_verdict(
        self, facts: HttpRequestFacts, ref: McpRef, loaded: LoadedPolicy, call: McpCall
    ) -> Verdict:
        try:
            # The project's accepted classification of the server, read at every call so
            # that an acceptance applies to the next one (CL-3); unreadable, nothing passes.
            revision = self._store.classification(ref.project, call.server)
        except StoreError as exc:
            return _deny(
                POLICY_UNAVAILABLE, f"the project's classification of {call.server}: {exc}"
            )
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
            classification=revision,
        )
        decision = self._drifted(call.server, loaded, decision, ref.project, revision)
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

        server = loaded.policy.mcp.servers[call.server]
        card = {
            "caller_kind": "agent",
            # The call's significant arguments for its card, as the MCP proxy records them.
            "summary": tool_summary(server, call.tool, call.message.get("params"), revision),
        }
        verdict = self._verdict(facts, loaded, decision, target=target, payload=payload, card=card)
        if verdict.request is not None and verdict.reason_code in REQUEST_CODES:
            code = with_request(verdict.reason_code, verdict.request)
            verdict = dataclasses.replace(verdict, reason_code=code)
        if verdict.allowed and call.method == "tools/list" and self.listings is not None:
            listing = Listing(
                facts.sandbox_id,
                call.server,
                subject,
                call.cursor is None,
                self.listings.pin(call.server, server, revision),
                rpc_id=cast("str | int", call.message.get("id", "")),
                rewrite=functools.partial(
                    _callable_page, call.server, loaded, self._ceiling, self._floor, revision
                ),
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
        refused: bytes | None = None,
    ) -> None:
        """One ``mcp-call`` record for every request, on a best-effort basis, as the MCP
        proxy writes one. With inspection, it carries the call's parameters, redacted and
        bounded (``arguments``); without, only their size."""
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
        params = call.params if call is not None else refused
        if params is not None and self.inspection is not None:
            payload["arguments"] = self.inspection.arguments(params)
        try:
            self._events.record("mcp-call", payload)
        except Exception:
            _log.exception("could not record an MCP call of request %s", facts.request_id)

    def _drifted(
        self,
        server: str,
        loaded: LoadedPolicy,
        decision: Decision,
        project: str,
        revision: ServerClassification | None,
    ) -> Decision:
        """Escalate a non-read call to a server whose tool list drifted from its pin, until
        the project accepts its classification again (D96, D103)."""
        if (
            self.listings is None
            or decision.action not in (Action.ALLOW, Action.ALERT)
            or decision.effect in (None, "read")
        ):
            return decision
        pin = self.listings.pin(server, loaded.policy.mcp.servers[server], revision)
        digest = pin_digest(pin) if pin is not None else None
        # Every non-read call reads the store's new records first: an acceptance ends a drift,
        # and a drift the worker recorded from the agent runtime's report, or an unread
        # listing the runtime has not reported in time, starts one, with no window between.
        self._refresh_approvals()
        seq = self._drifts.drifted(server, digest, project)
        if seq is not None:
            reason = (
                f"MCP server {server!r}'s tool list drifted from its pinned list (record "
                f"{seq}): its {decision.effect} calls ask until its classification is accepted "
                "again"
            )
            return _detected(decision, loaded, TOOL_LIST_DRIFT, reason)
        unread = self._unread.pending(project, server, self._now())
        if unread is None:
            return decision
        reason = (
            f"MCP server {server!r}'s tool list passed unread (record {unread}) and the agent "
            f"runtime has not reported it: its {decision.effect} calls ask until it does, or "
            "its tools are accepted again"
        )
        return _detected(decision, loaded, TOOL_LIST_DRIFT, reason)

    def _reread(self) -> None:
        """Read the store for answers to holds and acceptances of drifts, at most once a
        second: a held subject's calls keep coming."""
        now = self._now()
        if now - self._held_read >= _HELD_REFRESH:
            self._held_read = now
            self._refresh_approvals()

    def response_status(self, request_id: str, sandbox_id: str, status: int) -> None:
        """Count a failed response (4xx or 5xx) to an allowed request of ``sandbox_id``'s
        towards the detection rules; a hold it raises pauses the subject's next calls."""
        with self._lock:
            entry = self._outcomes.pop(request_id, None)
        if entry is None or status < 400:
            return
        subject, host, loaded, project, caller = entry
        if subject.sandbox_id != sandbox_id:
            return
        found = self._detector.outcome(
            loaded.policy.detection, subject, host, self._now(), denied=False
        )
        self._judged(request_id, loaded, subject, project, found, caller)

    def _detect(
        self,
        request_id: str,
        loaded: LoadedPolicy,
        subject: Subject,
        project: str,
        call: Call,
        caller: str,
    ) -> Verdict | None:
        """Refuse a held subject's call; count any other, and refuse it when a detection rule
        holds the subject now. None when the call goes on to be decided."""
        if self._holds.active(subject) is not None:
            self._reread()
        held = self._holds.active(subject)
        if held is not None:
            return _held(held, "")
        found = self._detector.observe(loaded.policy.detection, subject, project, call, self._now())
        return self._judged(request_id, loaded, subject, project, found, caller)

    def _counted(
        self,
        request_id: str,
        loaded: LoadedPolicy,
        subject: Subject,
        project: str,
        host: str,
        verdict: Verdict,
        caller: str,
    ) -> Verdict:
        """Count a refusal towards the detection rules, or wait for an allowed request's
        response status; the verdict stands, and a hold pauses the subject's next calls."""
        if verdict.allowed:
            if self.response_statuses and request_id:
                with self._lock:
                    self._outcomes[request_id] = (subject, host, loaded, project, caller)
                    while len(self._outcomes) > _MAX_OUTCOMES:
                        self._outcomes.popitem(last=False)
            return verdict
        if _REQUEST_SUFFIX.sub("", verdict.reason_code) not in _REFUSALS:
            return verdict
        settings = loaded.policy.detection
        found = self._detector.outcome(settings, subject, host, self._now(), denied=True)
        self._judged(request_id, loaded, subject, project, found, caller)
        return verdict

    def _judged(
        self,
        request_id: str,
        loaded: LoadedPolicy,
        subject: Subject,
        project: str,
        found: Sequence[Detected],
        caller: str,
    ) -> Verdict | None:
        """Log what the rules saw, and hold the subject if one of them fired: the refusal of
        the call that fired it, or None."""
        for finding in found:
            _log_finding(subject, request_id, finding)
        fired = next((finding for finding in found if finding.hold), None)
        if fired is None:
            return None
        seq = self._hold(request_id, loaded, subject, project, fired, caller)
        if seq is None:
            return _deny(AUDIT_UNAVAILABLE, "the session's hold could not be recorded")
        return _held(seq, fired.rule)

    def _hold(
        self,
        request_id: str,
        loaded: LoadedPolicy,
        subject: Subject,
        project: str,
        finding: Detected,
        caller: str,
    ) -> int | None:
        """Record a hold of the subject's calls, a permission request for the workspace's
        card, once; its sequence number (0 when the sink does not say), or None when it
        could not be recorded."""
        key = subject.hold_key(finding.rule, self._now())
        payload: dict[str, object] = {
            "sandbox_id": subject.sandbox_id,
            "project": subject.project or None,
            "session": subject.session or None,
            "request_id": request_id,
            "policy": {"name": loaded.policy.metadata.name, "digest": loaded.digest},
            "rule": RULE_PREFIX + finding.rule,
            "reason": _hold_reason(finding),
            "target": key.payload(),
            "escalation": {
                "timeout_seconds": int(HOLD_TIMEOUT.total_seconds()),
                "on_timeout": "deny",
            },
            "caller_kind": caller,
            "max_grant": "once",
            "hold": {**finding.payload(), "requests": self._detector.recent(subject)},
        }
        with self._hold_lock:
            opened = self._holds.active(subject)
            if opened is not None:
                return opened
            try:
                seq = self._events.record("escalation", payload)
            except Exception:
                _log.exception("could not record the hold of %s", subject.key)
                return None
            self._holds.note(seq or 0, key)
            self._hold_projects[subject.key] = project
        return seq or 0

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


_REQUEST_SUFFIX: Final = re.compile(r"_r[0-9]+$")


def _held(seq: int, rule: str) -> Verdict:
    """The refusal of a held subject's call, naming the hold for the agent."""
    code = with_request(SESSION_HELD, seq) if seq else SESSION_HELD
    fired = f" by {RULE_PREFIX}{rule}" if rule else ""
    reason = f"the session's outward calls are held{fired}, until the user clears the hold"
    findings = (Finding("detect_hold", rule or "hold", "high"),) if rule else ()
    return Verdict(allowed=False, reason_code=code, reason=reason, findings=findings)


def _hold_reason(finding: Detected) -> str:
    where = f" to {finding.host}" if finding.host else ""
    return (
        f"{finding.count} calls{where} within {finding.window_seconds} s, over the "
        f"{finding.rule} threshold of {finding.threshold}"
    )


def _log_finding(subject: Subject, request_id: str, finding: Detected) -> None:
    """A structured record of what a rule saw, for the log pipeline's dashboards (D103)."""
    _log.info(
        "detection %s %s: %d of %d within %d s",
        finding.rule,
        "holds" if finding.hold else "near miss",
        finding.count,
        finding.threshold,
        finding.window_seconds,
        extra={
            "event": "detect_finding",
            "outcome": "hold" if finding.hold else "near_miss",
            **finding.payload(),
            "sandbox_id": subject.sandbox_id,
            "project": subject.project or None,
            "session": subject.session or None,
            "request": request_id,
        },
    )


def _detected(decision: Decision, loaded: LoadedPolicy, rule: str, reason: str) -> Decision:
    """A detection rule's escalation of one call that the policy lets through: a permission
    request of its own, answered once, that denies when nobody answers."""
    return Decision(
        action=Action.ESCALATE,
        layer="policy",
        reason=reason,
        rule=RULE_PREFIX + rule,
        policy_digest=loaded.digest,
        escalation=EscalationTerms(loaded.policy.escalation.timeout, Action.DENY),
        effect=decision.effect,
        max_grant="once",
    )


def _action_url(
    facts: HttpRequestFacts, loaded: LoadedPolicy, operation: Operation, decision: Decision
) -> Decision:
    """Hold a GET or HEAD that the policy lets through when its URL reads as an action."""
    settings = loaded.policy.detection
    if (
        not (settings.enabled and settings.action_urls)
        or decision.action not in (Action.ALLOW, Action.ALERT)
        or not isinstance(operation, HttpOperation)
        or facts.method.upper() not in ("GET", "HEAD")
        or not action_like(facts.path, facts.query)
    ):
        return decision
    _log.info(
        "detection %s holds request %s",
        ACTION_URL,
        facts.request_id,
        extra={
            "event": "detect_finding",
            "outcome": "hold_call",
            "rule": ACTION_URL,
            "host": facts.host,
            "sandbox_id": facts.sandbox_id,
            "request": facts.request_id,
        },
    )
    reason = "the URL reads as an action (a token beside a word such as confirm or delete)"
    return _detected(decision, loaded, ACTION_URL, reason)


def _callable_page(
    server: str,
    loaded: LoadedPolicy,
    ceiling: PoolCeiling,
    floor: HardFloor,
    revision: ServerClassification | None,
    result: dict[str, object],
) -> dict[str, object]:
    """A listing page as the agent gets it: without the tools the policy denies it, the rest
    marked, each judged with the hint this page gives it (D96) and the project's accepted
    classification, as its later calls are."""
    hints = {tool.name: tool.hint for tool in listed_tools(result)}
    kept, _ = callable_tools(
        server,
        result,
        loaded,
        ceiling=ceiling,
        floor=floor,
        hints=hints,
        classification=revision,
    )
    return kept


def _http_card(facts: HttpRequestFacts, operation: Operation | None) -> dict[str, object]:
    """What an escalated request's card shows and its grants compare (D97): research code
    asked; its method, host, path, content type and size, and a JSON body's top-level
    fields, never the body; the widest grant for it; and for a package file, which."""
    types = _header_values(facts.headers, "content-type")
    card: dict[str, object] = {
        "caller_kind": "code",
        "summary": http_summary(
            facts.method.upper(),
            facts.host,
            facts.port,
            facts.path,
            types[0] if len(types) == 1 else None,
            facts.body,
        ),
        "max_grant": network_max_grant(facts.method).value,
    }
    if isinstance(operation, PackageOperation):
        card["package"] = {
            "ecosystem": operation.ecosystem,
            "name": operation.name,
            "channel": operation.channel,
        }
        card["max_grant"] = PACKAGES_MAX_GRANT.value
    return card


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
