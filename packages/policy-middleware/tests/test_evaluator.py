# SPDX-License-Identifier: MIT
"""How the middleware turns policy decisions into OpenShell verdicts."""

import textwrap
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol, cast

import pytest

from sandbox_science.policy import Action, HardFloor, LoadedPolicy, PoolCeiling, parse_ceiling
from sandbox_science.policy_middleware import evaluator, service
from sandbox_science.policy_middleware.evaluator import (
    HttpRequestFacts,
    PolicyMiddleware,
    Verdict,
    parse_config,
)
from sandbox_science.policy_middleware.meter import EgressMeter
from sandbox_science.policy_middleware.store import PolicyStore

FactsFactory = Callable[..., HttpRequestFacts]
OID = "0123456789abcdef0123456789abcdef01234567"
GIT_FETCH_TYPE = "application/x-git-upload-pack-request"
HEADER = """\
apiVersion: sandbox-science/v0alpha1
kind: ProjectPolicy
metadata:
  name: test
"""


class Sink(Protocol):
    """The recording sink from conftest."""

    events: list[Mapping[str, object]]
    fail: bool

    def record(self, event: Mapping[str, object]) -> None: ...


def pkt(line: str) -> bytes:
    data = line.encode()
    return f"{len(data) + 4:04x}".encode() + data


def fetch_body() -> bytes:
    """A protocol v0 fetch request, as git sends it."""
    return (
        pkt(f"want {OID} multi_ack_detailed side-band-64k thin-pack ofs-delta agent=git/2.43.0\n")
        + b"0000"
        + pkt("done\n")
    )


def fetch_facts(
    make_facts: FactsFactory, body: bytes, host: str = "github.com"
) -> HttpRequestFacts:
    return make_facts(
        method="POST",
        host=host,
        path="/team/repo.git/git-upload-pack",
        headers=(("content-type", GIT_FETCH_TYPE),),
        body=body,
    )


def bind_policy(store: PolicyStore, sandbox_id: str, body: str) -> LoadedPolicy:
    loaded = store.add_policy((HEADER + textwrap.dedent(body)).encode())
    store.bind(sandbox_id, loaded.digest)
    return loaded


# --- Decisions -------------------------------------------------------------------------------


def test_an_allowed_request_is_allowed_without_a_reason_code(
    middleware: PolicyMiddleware, make_facts: FactsFactory
) -> None:
    verdict = middleware.evaluate_http(make_facts(path="/papers", query="q=lattice"))
    assert verdict.allowed
    assert verdict.reason_code == ""
    assert verdict.decision is not None
    assert verdict.decision.rule == "read-only-web"
    assert verdict.findings == ()


def test_an_escalation_is_denied_as_approval_required_and_recorded(
    middleware: PolicyMiddleware, make_facts: FactsFactory, sink: Sink, example: LoadedPolicy
) -> None:
    verdict = middleware.evaluate_http(
        make_facts(method="POST", path="/v1/items", query="token=secret-ish", body=b"{}")
    )
    assert not verdict.allowed
    assert verdict.reason_code == evaluator.APPROVAL_REQUIRED
    events = sink.events
    assert len(events) == 1
    event = events[0]
    assert event["type"] == "escalation"
    assert event["sandbox_id"] == "sbx-0001"
    assert event["rule"] == "external-writes"
    assert event["policy"] == {"name": "permissive-default", "digest": example.digest}
    assert event["request"] == {
        "method": "POST",
        "scheme": "https",
        "host": "example.org",
        "port": 443,
        "path": "/v1/items",
        "query_bytes": len("token=secret-ish"),
    }
    # external-writes sets no on_timeout of its own, so the policy-wide deny applies.
    assert event["escalation"] == {"timeout_seconds": 1800, "on_timeout": "deny"}
    assert "secret-ish" not in repr(event)


def test_an_unmatched_request_follows_the_default(
    middleware: PolicyMiddleware, make_facts: FactsFactory
) -> None:
    verdict = middleware.evaluate_http(make_facts(method="OPTIONS"))
    assert verdict.reason_code == evaluator.APPROVAL_REQUIRED
    assert verdict.decision is not None
    assert verdict.decision.layer == "default"


@pytest.mark.parametrize(
    ("overrides", "code"),
    [
        pytest.param({"host": "metadata.google.internal"}, "destination_forbidden", id="floor"),
        pytest.param({"host": "10.0.0.5", "scheme": "http", "port": 80}, "policy_denied", id="lan"),
        pytest.param({"query": "a" * 5000}, "policy_denied", id="url-limit"),
    ],
)
def test_denials_carry_the_layer_reason_code(
    middleware: PolicyMiddleware,
    make_facts: FactsFactory,
    overrides: dict[str, object],
    code: str,
) -> None:
    verdict = middleware.evaluate_http(make_facts(**overrides))
    assert not verdict.allowed
    assert verdict.reason_code == code


def test_the_pool_ceiling_applies(
    store: PolicyStore, example: LoadedPolicy, make_facts: FactsFactory
) -> None:
    del example
    ceiling = parse_ceiling(
        "apiVersion: sandbox-science/v0alpha1\n"
        "kind: PoolCeiling\n"
        "network:\n"
        "  deny_hosts: [blocked.example.org]\n"
    )
    middleware = PolicyMiddleware(store, ceiling=ceiling, floor=HardFloor(), events=_NullSink())
    verdict = middleware.evaluate_http(make_facts(host="blocked.example.org"))
    assert verdict.reason_code == evaluator.POOL_CEILING_DENIED


def test_the_deployment_floor_applies(
    store: PolicyStore, example: LoadedPolicy, make_facts: FactsFactory
) -> None:
    del example
    middleware = PolicyMiddleware(
        store,
        ceiling=PoolCeiling.unrestricted(),
        floor=HardFloor(frozenset({"gateway.example.org"})),
        events=_NullSink(),
    )
    verdict = middleware.evaluate_http(make_facts(host="gateway.example.org"))
    assert verdict.reason_code == evaluator.DESTINATION_FORBIDDEN


# --- Classification --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param({"method": "get"}, id="lowercase-method"),
        pytest.param({"scheme": "wss"}, id="websocket-scheme"),
        pytest.param({"scheme": "ws", "port": 80}, id="plain-websocket-scheme"),
        pytest.param({"headers": (("upgrade", "websocket"),)}, id="upgrade-header"),
        pytest.param({"headers": (("upgrade", "h2c"),)}, id="h2c-upgrade"),
        pytest.param({"headers": (("sec-websocket-key", "dGhlIHNhbXBsZQ=="),)}, id="ws-key"),
        pytest.param({"scheme": "ftp"}, id="scheme"),
        pytest.param({"host": "example.org/evil"}, id="host-with-path"),
        pytest.param({"host": "example.org?x"}, id="host-with-query"),
        pytest.param({"host": "user@example.org"}, id="host-with-userinfo"),
        pytest.param({"host": ""}, id="empty-host"),
        pytest.param({"path": "example.org:443"}, id="authority-form"),
        pytest.param({"path": "*"}, id="asterisk-form"),
        pytest.param({"path": ""}, id="empty-path"),
        pytest.param({"path": "/a?b"}, id="question-mark-in-path"),
        pytest.param({"path": "/a#b"}, id="fragment-in-path"),
        pytest.param({"query": "a#b"}, id="fragment-in-query"),
        pytest.param({"port": 0}, id="port-zero"),
        pytest.param({"port": 70000}, id="port-out-of-range"),
        pytest.param({"body": b"x"}, id="get-with-body"),
    ],
)
def test_unclassifiable_requests_are_denied(
    middleware: PolicyMiddleware, make_facts: FactsFactory, overrides: dict[str, object]
) -> None:
    verdict = middleware.evaluate_http(make_facts(**overrides))
    assert not verdict.allowed
    assert verdict.reason_code == evaluator.REQUEST_UNCLASSIFIABLE


def test_repeated_content_type_headers_are_ambiguous(
    middleware: PolicyMiddleware, make_facts: FactsFactory
) -> None:
    facts = fetch_facts(make_facts, fetch_body())
    facts = make_facts(
        method=facts.method,
        host=facts.host,
        path=facts.path,
        body=facts.body,
        headers=(("content-type", GIT_FETCH_TYPE), ("content-type", "application/json")),
    )
    assert middleware.evaluate_http(facts).reason_code == evaluator.REQUEST_UNCLASSIFIABLE


def test_ipv6_literals_are_bracketed_for_classification(
    middleware: PolicyMiddleware, make_facts: FactsFactory
) -> None:
    verdict = middleware.evaluate_http(make_facts(host="2606:4700:4700::1111"))
    assert verdict.allowed


def test_non_default_ports_are_kept(middleware: PolicyMiddleware, make_facts: FactsFactory) -> None:
    # read-only-web lists no ports, so it covers 443 only and port 8443 falls to the default.
    verdict = middleware.evaluate_http(make_facts(port=8443))
    assert verdict.reason_code == evaluator.APPROVAL_REQUIRED
    assert verdict.decision is not None
    assert verdict.decision.layer == "default"


PORTLESS_DENY = """\
defaults: {action: allow}
network:
  rules:
    - {name: no-evil, operation: http, match: {hosts: [evil.example.org]}, action: deny}
"""


@pytest.mark.parametrize(
    ("scheme", "port"),
    [
        pytest.param("https", 80, id="plain-http-through-connect"),
        pytest.param("http", 443, id="forward-proxy-to-443"),
        pytest.param("http", 80, id="http"),
        pytest.param("https", 443, id="https"),
    ],
)
def test_the_port_decides_the_scheme_on_default_ports(
    store: PolicyStore,
    sandbox_id: str,
    make_facts: FactsFactory,
    policy_config: Callable[[LoadedPolicy], dict[str, object]],
    scheme: str,
    port: int,
) -> None:
    # OpenShell reports "https" for everything through a CONNECT tunnel, even
    # plain HTTP to port 80, so a rule without ports must still cover both.
    loaded = bind_policy(store, sandbox_id, PORTLESS_DENY)
    middleware = PolicyMiddleware(
        store, ceiling=PoolCeiling.unrestricted(), floor=HardFloor(), events=_NullSink()
    )
    verdict = middleware.evaluate_http(
        make_facts(host="evil.example.org", scheme=scheme, port=port, config=policy_config(loaded))
    )
    assert verdict.reason_code == evaluator.POLICY_DENIED


def test_rules_without_ports_do_not_cover_other_ports(
    store: PolicyStore,
    sandbox_id: str,
    make_facts: FactsFactory,
    policy_config: Callable[[LoadedPolicy], dict[str, object]],
) -> None:
    loaded = bind_policy(store, sandbox_id, PORTLESS_DENY)
    middleware = PolicyMiddleware(
        store, ceiling=PoolCeiling.unrestricted(), floor=HardFloor(), events=_NullSink()
    )
    verdict = middleware.evaluate_http(
        make_facts(host="evil.example.org", port=8443, config=policy_config(loaded))
    )
    assert verdict.allowed


# --- Git -------------------------------------------------------------------------------------


def test_a_well_formed_fetch_is_allowed(
    middleware: PolicyMiddleware, make_facts: FactsFactory
) -> None:
    verdict = middleware.evaluate_http(fetch_facts(make_facts, fetch_body()))
    assert verdict.allowed
    assert verdict.decision is not None
    assert verdict.decision.rule == "git-fetch"


@pytest.mark.parametrize(
    "body",
    [
        pytest.param(b'{"exfiltrated": "data"}', id="json"),
        pytest.param(fetch_body() + b"trailing data", id="trailing-data"),
        pytest.param(pkt("hello world\n") + b"0000", id="unknown-keyword"),
    ],
)
def test_a_fetch_must_send_an_upload_pack_request(
    middleware: PolicyMiddleware, make_facts: FactsFactory, body: bytes
) -> None:
    verdict = middleware.evaluate_http(fetch_facts(make_facts, body))
    assert not verdict.allowed
    assert verdict.reason_code == evaluator.GIT_REQUEST_INVALID


def test_a_junk_fetch_body_is_not_escalated(
    middleware: PolicyMiddleware, make_facts: FactsFactory, sink: Sink
) -> None:
    verdict = middleware.evaluate_http(fetch_facts(make_facts, b"not git", host="git.example.org"))
    assert verdict.reason_code == evaluator.GIT_REQUEST_INVALID
    assert sink.events == []


def test_repeated_content_encoding_headers_are_rejected_for_fetches(
    middleware: PolicyMiddleware, make_facts: FactsFactory
) -> None:
    facts = make_facts(
        method="POST",
        host="github.com",
        path="/team/repo.git/git-upload-pack",
        headers=(
            ("content-type", GIT_FETCH_TYPE),
            ("content-encoding", "gzip"),
            ("content-encoding", "identity"),
        ),
        body=fetch_body(),
    )
    assert middleware.evaluate_http(facts).reason_code == evaluator.GIT_REQUEST_INVALID


def test_fetch_discovery_is_allowed(middleware: PolicyMiddleware, make_facts: FactsFactory) -> None:
    verdict = middleware.evaluate_http(
        make_facts(
            host="github.com", path="/team/repo.git/info/refs", query="service=git-upload-pack"
        )
    )
    assert verdict.allowed


def test_a_push_is_escalated_without_inspecting_its_body(
    middleware: PolicyMiddleware, make_facts: FactsFactory
) -> None:
    verdict = middleware.evaluate_http(
        make_facts(
            method="POST",
            host="github.com",
            path="/team/repo.git/git-receive-pack",
            headers=(("content-type", "application/x-git-receive-pack-request"),),
            body=b"0000PACK...",
        )
    )
    assert verdict.reason_code == evaluator.APPROVAL_REQUIRED


# --- Policy selection ------------------------------------------------------------------------


@pytest.mark.parametrize(
    "config",
    [
        pytest.param(None, id="missing"),
        pytest.param({}, id="empty"),
        pytest.param({"policy": "permissive-default"}, id="not-a-mapping"),
        pytest.param({"policy": {"name": "permissive-default"}}, id="no-digest"),
        pytest.param({"policy": {"name": "x", "digest": "sha256:abc"}}, id="short-digest"),
        pytest.param({"policy": {"name": 1, "digest": "sha256:" + "0" * 64}}, id="name-type"),
    ],
)
def test_malformed_config_is_denied(
    middleware: PolicyMiddleware, make_facts: FactsFactory, config: object
) -> None:
    verdict = middleware.evaluate_http(make_facts(config=config))
    assert verdict.reason_code == evaluator.CONFIG_INVALID


def test_extra_config_keys_are_rejected(example: LoadedPolicy) -> None:
    config = {"policy": {"name": "permissive-default", "digest": example.digest, "x": 1}}
    with pytest.raises(evaluator.PolicyConfigError):
        parse_config(config)
    with pytest.raises(evaluator.PolicyConfigError):
        parse_config({"policy": {"name": "permissive-default", "digest": example.digest}, "x": 1})


def test_a_config_naming_another_policy_is_denied(
    middleware: PolicyMiddleware, make_facts: FactsFactory, example: LoadedPolicy
) -> None:
    config = {"policy": {"name": "other", "digest": example.digest}}
    assert middleware.evaluate_http(make_facts(config=config)).reason_code == "config_invalid"


@pytest.mark.parametrize("sandbox", ["sbx-unknown", "../sbx-0001", "", ".hidden", "a/b"])
def test_unbound_or_malformed_sandboxes_are_denied(
    middleware: PolicyMiddleware, make_facts: FactsFactory, sandbox: str
) -> None:
    verdict = middleware.evaluate_http(make_facts(sandbox_id=sandbox))
    assert verdict.reason_code == evaluator.SANDBOX_UNBOUND


def test_a_stale_compiled_policy_is_denied(
    middleware: PolicyMiddleware,
    make_facts: FactsFactory,
    store: PolicyStore,
    sandbox_id: str,
) -> None:
    bind_policy(store, sandbox_id, "defaults: {action: allow}\n")
    verdict = middleware.evaluate_http(make_facts())
    assert verdict.reason_code == evaluator.POLICY_STALE


def test_a_missing_policy_file_is_denied(
    middleware: PolicyMiddleware, make_facts: FactsFactory, example: LoadedPolicy, tmp_path: Path
) -> None:
    (tmp_path / "state" / "policies" / f"{example.digest.removeprefix('sha256:')}.yaml").unlink()
    verdict = PolicyMiddleware(
        PolicyStore(tmp_path / "state"),
        ceiling=PoolCeiling.unrestricted(),
        floor=HardFloor(),
        events=_NullSink(),
    ).evaluate_http(make_facts())
    assert verdict.reason_code == evaluator.POLICY_UNAVAILABLE


def test_validate_config(middleware: PolicyMiddleware, example: LoadedPolicy) -> None:
    good = {"policy": {"name": "permissive-default", "digest": example.digest}}
    assert middleware.validate_config(good) is None
    unknown = {"policy": {"name": "permissive-default", "digest": "sha256:" + "1" * 64}}
    assert "no policy" in (middleware.validate_config(unknown) or "")
    renamed = {"policy": {"name": "renamed", "digest": example.digest}}
    assert "not 'renamed'" in (middleware.validate_config(renamed) or "")
    assert middleware.validate_config(None) is not None


# --- Alerts, escalation terms and monitors ---------------------------------------------------


ALERT_POLICY = """\
network:
  rules:
    - {name: watched, operation: http, match: {hosts: [watched.example.org]}, action: alert}
"""


def test_an_alert_allows_with_a_finding_and_a_record(
    store: PolicyStore,
    sandbox_id: str,
    sink: Sink,
    make_facts: FactsFactory,
    policy_config: Callable[[LoadedPolicy], dict[str, object]],
) -> None:
    loaded = bind_policy(store, sandbox_id, ALERT_POLICY)
    middleware = PolicyMiddleware(
        store, ceiling=PoolCeiling.unrestricted(), floor=HardFloor(), events=sink
    )
    verdict = middleware.evaluate_http(
        make_facts(host="watched.example.org", config=policy_config(loaded))
    )
    assert verdict.allowed
    assert [(f.type, f.label) for f in verdict.findings] == [("policy_alert", "watched")]
    assert [event["type"] for event in sink.events] == ["alert"]


def test_an_alert_that_cannot_be_recorded_is_denied(
    store: PolicyStore,
    sandbox_id: str,
    sink: Sink,
    make_facts: FactsFactory,
    policy_config: Callable[[LoadedPolicy], dict[str, object]],
) -> None:
    loaded = bind_policy(store, sandbox_id, ALERT_POLICY)
    sink.fail = True
    middleware = PolicyMiddleware(
        store, ceiling=PoolCeiling.unrestricted(), floor=HardFloor(), events=sink
    )
    verdict = middleware.evaluate_http(
        make_facts(host="watched.example.org", config=policy_config(loaded))
    )
    assert verdict.reason_code == evaluator.AUDIT_UNAVAILABLE


def test_an_escalation_that_cannot_be_recorded_is_still_denied(
    middleware: PolicyMiddleware, make_facts: FactsFactory, sink: Sink
) -> None:
    sink.fail = True
    verdict = middleware.evaluate_http(make_facts(method="DELETE"))
    assert verdict.reason_code == evaluator.APPROVAL_REQUIRED


READ_ESCALATION = """\
network:
  rules:
    - name: reads
      operation: http
      match: {hosts: ["*"], methods: [GET]}
      action: escalate
      escalation: {on_timeout: allow}
"""


PROVIDER = "credentials: {providers: [{name: gh, profile: token, hosts: [example.org]}]}\n"


@pytest.mark.parametrize(
    ("providers", "host", "port", "allow_credentialed", "on_timeout"),
    [
        pytest.param("", "example.org", 443, False, Action.ALERT.value, id="no-providers"),
        pytest.param(PROVIDER, "example.org", 443, False, Action.DENY.value, id="bound-host"),
        pytest.param(
            PROVIDER, "EXAMPLE.org.", 443, False, Action.DENY.value, id="bound-host-normalized"
        ),
        # OpenShell binds credentials to HTTPS only; counting every port is conservative.
        pytest.param(
            PROVIDER, "example.org", 80, False, Action.DENY.value, id="bound-host-port-80"
        ),
        pytest.param(
            PROVIDER, "example.org", 443, True, Action.ALERT.value, id="allow-credentialed"
        ),
        pytest.param(
            PROVIDER, "www.example.org", 443, False, Action.ALERT.value, id="unbound-subdomain"
        ),
        pytest.param(PROVIDER, "example.net", 443, False, Action.ALERT.value, id="unbound-host"),
    ],
)
def test_requests_to_hosts_with_a_bound_credential_count_as_credentialed(
    store: PolicyStore,
    sandbox_id: str,
    sink: Sink,
    make_facts: FactsFactory,
    policy_config: Callable[[LoadedPolicy], dict[str, object]],
    providers: str,
    host: str,
    port: int,
    allow_credentialed: bool,
    on_timeout: str,
) -> None:
    escalation = READ_ESCALATION
    if allow_credentialed:
        escalation = escalation.replace(
            "escalation: {on_timeout: allow}",
            "escalation: {on_timeout: allow, allow_credentialed: true}",
        )
    loaded = bind_policy(store, sandbox_id, escalation + providers)
    middleware = PolicyMiddleware(
        store, ceiling=PoolCeiling.unrestricted(), floor=HardFloor(), events=sink
    )
    facts = make_facts(config=policy_config(loaded), host=host, port=port)
    verdict = middleware.evaluate_http(facts)
    assert verdict.reason_code == evaluator.APPROVAL_REQUIRED
    assert sink.events[0]["escalation"] == {"timeout_seconds": 1800, "on_timeout": on_timeout}


@pytest.mark.parametrize(
    ("scheme", "host", "port", "refused"),
    [
        pytest.param("http", "example.org", 443, True, id="plain-http-to-the-credential-port"),
        pytest.param("http", "EXAMPLE.org.", 443, True, id="normalized-host"),
        pytest.param("https", "example.org", 443, False, id="https"),
        pytest.param("http", "example.org", 80, False, id="port-80-carries-no-credential"),
        pytest.param("http", "example.net", 443, False, id="unbound-host"),
    ],
)
def test_plain_http_to_a_credential_port_is_refused(
    store: PolicyStore,
    sandbox_id: str,
    sink: Sink,
    make_facts: FactsFactory,
    policy_config: Callable[[LoadedPolicy], dict[str, object]],
    scheme: str,
    host: str,
    port: int,
    refused: bool,
) -> None:
    # OpenShell would substitute the credential and forward it unencrypted.
    loaded = bind_policy(store, sandbox_id, READ_ESCALATION + PROVIDER)
    middleware = PolicyMiddleware(
        store, ceiling=PoolCeiling.unrestricted(), floor=HardFloor(), events=sink
    )
    facts = make_facts(config=policy_config(loaded), scheme=scheme, host=host, port=port)
    verdict = middleware.evaluate_http(facts)
    assert not verdict.allowed
    expected = evaluator.CREDENTIAL_UNENCRYPTED if refused else evaluator.APPROVAL_REQUIRED
    assert verdict.reason_code == expected


def test_monitors_report_once_an_hour(
    store: PolicyStore,
    sandbox_id: str,
    sink: Sink,
    make_facts: FactsFactory,
    policy_config: Callable[[LoadedPolicy], dict[str, object]],
) -> None:
    loaded = bind_policy(
        store,
        sandbox_id,
        """\
        defaults: {action: allow}
        monitors:
          - {name: chatty, when: {egress_bytes_per_hour_over: 100}, action: alert}
        """,
    )
    now = [datetime(2026, 10, 10, 12, 0, tzinfo=UTC)]
    clock = [0.0]
    middleware = PolicyMiddleware(
        store,
        ceiling=PoolCeiling.unrestricted(),
        floor=HardFloor(),
        events=sink,
        meter=EgressMeter(clock=lambda: clock[0]),
        now=lambda: now[0],
    )

    def post(size: int) -> Verdict:
        return middleware.evaluate_http(
            make_facts(method="POST", body=b"x" * size, config=policy_config(loaded))
        )

    assert post(10).findings == ()
    assert [f.label for f in post(200).findings] == ["chatty"]
    assert post(200).findings == ()
    now[0] += timedelta(hours=1)
    clock[0] += 3600
    assert post(10).findings == ()
    assert [f.label for f in post(200).findings] == ["chatty"]
    assert [event["type"] for event in sink.events] == ["monitor", "monitor"]


def test_denied_requests_do_not_count_as_egress(
    store: PolicyStore,
    sandbox_id: str,
    sink: Sink,
    make_facts: FactsFactory,
    policy_config: Callable[[LoadedPolicy], dict[str, object]],
) -> None:
    loaded = bind_policy(
        store,
        sandbox_id,
        """\
        defaults: {action: deny}
        network:
          rules:
            - {name: small, operation: http, match: {hosts: [ok.example.org]}, action: allow}
        monitors:
          - {name: chatty, when: {egress_bytes_per_hour_over: 100}, action: alert}
        """,
    )
    middleware = PolicyMiddleware(
        store, ceiling=PoolCeiling.unrestricted(), floor=HardFloor(), events=sink
    )
    config = policy_config(loaded)
    denied = middleware.evaluate_http(make_facts(method="POST", body=b"x" * 500, config=config))
    assert denied.reason_code == evaluator.POLICY_DENIED
    allowed = middleware.evaluate_http(make_facts(host="ok.example.org", config=config))
    assert allowed.allowed
    assert allowed.findings == ()


# --- Verdicts --------------------------------------------------------------------------------


def test_every_reason_code_has_openshells_format() -> None:
    codes = [
        evaluator.APPROVAL_REQUIRED,
        evaluator.POLICY_DENIED,
        evaluator.POOL_CEILING_DENIED,
        evaluator.DESTINATION_FORBIDDEN,
        evaluator.REQUEST_UNCLASSIFIABLE,
        evaluator.GIT_REQUEST_INVALID,
        evaluator.AUDIT_UNAVAILABLE,
        evaluator.SANDBOX_UNBOUND,
        evaluator.POLICY_STALE,
        evaluator.POLICY_UNAVAILABLE,
        evaluator.CONFIG_INVALID,
        service.WEBSOCKET_DENIED,
    ]
    assert len(set(codes)) == len(codes)
    assert all(evaluator.REASON_CODE_RE.fullmatch(code) for code in codes)


def test_verdicts_pair_denials_with_reason_codes() -> None:
    with pytest.raises(ValueError, match="reason code"):
        Verdict(allowed=False, reason_code="", reason="x")
    with pytest.raises(ValueError, match="reason code"):
        Verdict(allowed=True, reason_code="policy_denied", reason="x")
    with pytest.raises(ValueError, match="invalid reason code"):
        Verdict(allowed=False, reason_code="Bad-Code", reason="x")


class _NullSink:
    def record(self, event: object) -> None:
        del event


# --- Package cache ---------------------------------------------------------------------------

CACHE_HOST = "package-cache.sandbox-science.internal"
CACHE_PORT = 8090


def with_cache(config: dict[str, object]) -> dict[str, object]:
    # protobuf Structs deliver every number as a float.
    return {**config, "package_cache": {"host": CACHE_HOST, "port": float(CACHE_PORT)}}


def cache_facts(
    make_facts: FactsFactory, example: LoadedPolicy, path: str, **overrides: object
) -> HttpRequestFacts:
    fields: dict[str, object] = {
        "scheme": "http",
        "host": CACHE_HOST,
        "port": CACHE_PORT,
        "path": path,
        "config": with_cache(
            {"policy": {"name": example.policy.metadata.name, "digest": example.digest}}
        ),
    }
    fields.update(overrides)
    return make_facts(**fields)


@pytest.mark.parametrize(
    ("path", "rule"),
    [
        ("/pypi/simple/numpy/", "packages.pypi"),
        ("/conda/conda-forge/linux-64/repodata.json.zst", "packages.conda"),
        ("/npm/@types%2Fnode", "packages.npm"),
        ("/npm/files/-/lodash/-/lodash-4.17.21.tgz", "packages.npm"),
        ("/cargo/index/se/rd/serde", "packages.cargo"),
        ("/go/golang.org/x/mod/@v/v0.20.0.zip", "packages.go"),
    ],
)
def test_requests_to_the_package_cache_are_package_operations(
    middleware: PolicyMiddleware,
    make_facts: FactsFactory,
    example: LoadedPolicy,
    path: str,
    rule: str,
) -> None:
    verdict = middleware.evaluate_http(cache_facts(make_facts, example, path))
    assert verdict.allowed, verdict.reason
    assert verdict.decision is not None
    assert verdict.decision.rule == rule


@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param({"path": "/npm/-/v1/search"}, id="search"),
        pytest.param({"path": "/npm/-/npm/v1/security/advisories/bulk"}, id="audit"),
        pytest.param({"path": "/cargo/api/v1/crates/new", "method": "PUT"}, id="publish"),
        pytest.param({"path": "/npm/lodash", "method": "POST", "body": b"{}"}, id="post"),
        pytest.param({"path": "/npm/lodash", "query": "write=true"}, id="query"),
        pytest.param({"path": "/pypi/simple/"}, id="pypi-root"),
        pytest.param({"path": "/maven/org/x"}, id="unknown-ecosystem"),
    ],
)
def test_other_requests_to_the_package_cache_are_refused(
    middleware: PolicyMiddleware,
    make_facts: FactsFactory,
    example: LoadedPolicy,
    overrides: dict[str, object],
) -> None:
    path = cast("str", overrides.pop("path"))
    verdict = middleware.evaluate_http(cache_facts(make_facts, example, path, **overrides))
    assert verdict.reason_code == evaluator.REQUEST_UNCLASSIFIABLE


def test_only_the_cache_port_is_the_package_cache(
    middleware: PolicyMiddleware, make_facts: FactsFactory, example: LoadedPolicy
) -> None:
    # Platform services listen on this host; nothing else there is for workers.
    verdict = middleware.evaluate_http(
        cache_facts(make_facts, example, "/pypi/simple/numpy/", port=8091)
    )
    assert verdict.reason_code == evaluator.DESTINATION_FORBIDDEN


def test_package_escalations_ask_for_the_install_not_the_lookup(
    store: PolicyStore,
    sandbox_id: str,
    sink: Sink,
    make_facts: FactsFactory,
    policy_config: Callable[[LoadedPolicy], dict[str, object]],
) -> None:
    loaded = bind_policy(
        store, sandbox_id, "defaults: {action: deny}\npackages:\n  cargo: {action: escalate}\n"
    )
    middleware = PolicyMiddleware(
        store, ceiling=PoolCeiling.unrestricted(), floor=HardFloor(), events=sink
    )
    config = with_cache(policy_config(loaded))
    lookup = middleware.evaluate_http(
        cache_facts(make_facts, loaded, "/cargo/index/se/rd/serde", config=config)
    )
    assert lookup.allowed
    download = middleware.evaluate_http(
        cache_facts(make_facts, loaded, f"/cargo/dl/serde/1.0.200/{'a' * 64}", config=config)
    )
    assert download.reason_code == evaluator.APPROVAL_REQUIRED
    assert sink.events[-1]["rule"] == "packages.cargo"
    denied = middleware.evaluate_http(cache_facts(make_facts, loaded, "/npm/lodash", config=config))
    assert denied.reason_code == evaluator.POLICY_DENIED


@pytest.mark.parametrize(
    "cache",
    [
        pytest.param({"host": CACHE_HOST}, id="no-port"),
        pytest.param({"host": CACHE_HOST, "port": 0}, id="port-zero"),
        pytest.param({"host": CACHE_HOST, "port": 70000}, id="port-too-large"),
        pytest.param({"host": CACHE_HOST, "port": 8090.5}, id="fractional-port"),
        pytest.param({"host": CACHE_HOST, "port": True}, id="bool-port"),
        pytest.param({"host": "cache/evil", "port": 8090}, id="bad-host"),
        pytest.param({"host": CACHE_HOST, "port": 8090, "tls": True}, id="extra-key"),
        pytest.param("cache:8090", id="not-a-mapping"),
    ],
)
def test_malformed_package_cache_config_is_rejected(example: LoadedPolicy, cache: object) -> None:
    config = {
        "policy": {"name": "permissive-default", "digest": example.digest},
        "package_cache": cache,
    }
    with pytest.raises(evaluator.PolicyConfigError):
        parse_config(config)


def test_package_cache_config_is_accepted(
    middleware: PolicyMiddleware, example: LoadedPolicy
) -> None:
    config = with_cache({"policy": {"name": "permissive-default", "digest": example.digest}})
    assert parse_config(config).package_cache == (CACHE_HOST, CACHE_PORT)
    assert middleware.validate_config(config) is None


MCP_HOST = "mcp-proxy.sandbox-science.internal"


def with_services(config: dict[str, object]) -> dict[str, object]:
    return {
        **with_cache(config),
        "mcp_proxy": {"host": MCP_HOST, "port": 8080.0},
    }


def test_worker_code_reaches_the_mcp_proxy_when_the_policy_allows_it(
    middleware: PolicyMiddleware,
    make_facts: FactsFactory,
    example: LoadedPolicy,
    policy_config: Callable[[LoadedPolicy], dict[str, object]],
) -> None:
    facts = make_facts(
        scheme="http",
        host=MCP_HOST,
        port=8080,
        method="POST",
        path="/mcp",
        headers=(("content-type", "application/json"),),
        body=b'{"jsonrpc": "2.0", "method": "tools/list", "id": 1}',
        config=with_services(policy_config(example)),
    )
    verdict = middleware.evaluate_http(facts)
    assert verdict.allowed
    assert verdict.decision is not None
    assert verdict.decision.rule == "mcp.proxy.worker_access"


def test_worker_access_false_keeps_worker_code_from_the_mcp_proxy(
    store: PolicyStore,
    sandbox_id: str,
    make_facts: FactsFactory,
    policy_config: Callable[[LoadedPolicy], dict[str, object]],
) -> None:
    loaded = bind_policy(store, sandbox_id, "mcp:\n  proxy: {worker_access: false}\n")
    middleware = PolicyMiddleware(
        store, ceiling=PoolCeiling.unrestricted(), floor=HardFloor(), events=_NullSink()
    )
    facts = make_facts(
        scheme="http",
        host=MCP_HOST,
        port=8080,
        method="POST",
        path="/mcp",
        body=b"{}",
        config=with_services(policy_config(loaded)),
    )
    assert middleware.evaluate_http(facts).reason_code == evaluator.POLICY_DENIED


@pytest.mark.parametrize(
    "host", [CACHE_HOST.upper(), f"{CACHE_HOST}.", "Package-Cache.Sandbox-Science.Internal"]
)
def test_service_hosts_are_compared_normalized(
    middleware: PolicyMiddleware, make_facts: FactsFactory, example: LoadedPolicy, host: str
) -> None:
    verdict = middleware.evaluate_http(cache_facts(make_facts, example, "/npm/lodash", host=host))
    assert verdict.allowed
    assert verdict.decision is not None
    assert verdict.decision.rule == "packages.npm"


@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param({"scheme": "ws"}, id="ws-scheme"),
        pytest.param({"headers": (("sec-websocket-key", "x"),)}, id="ws-key"),
        pytest.param({"headers": (("upgrade", "websocket"),)}, id="upgrade"),
    ],
)
def test_websocket_requests_to_the_cache_are_refused(
    middleware: PolicyMiddleware,
    make_facts: FactsFactory,
    example: LoadedPolicy,
    overrides: dict[str, object],
) -> None:
    verdict = middleware.evaluate_http(cache_facts(make_facts, example, "/npm/lodash", **overrides))
    assert verdict.reason_code == evaluator.REQUEST_UNCLASSIFIABLE


def test_malformed_service_hosts_are_rejected(example: LoadedPolicy) -> None:
    base = {"policy": {"name": "permissive-default", "digest": example.digest}}
    for host in ("-bad-", "a..b", "fe80::1%eth0"):
        with pytest.raises(evaluator.PolicyConfigError):
            parse_config({**base, "mcp_proxy": {"host": host, "port": 8080}})
    with pytest.raises(evaluator.PolicyConfigError):
        parse_config({**base, "other_service": {"host": "x.internal", "port": 1}})
