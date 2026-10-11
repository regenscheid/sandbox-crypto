# SPDX-License-Identifier: MIT
"""The middleware's records in the event store, and failing closed when the store fails."""

import sqlite3
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from sandbox_science.event_store import EventStore, StoreReader, verify_store
from sandbox_science.policy import HardFloor, LoadedPolicy, PoolCeiling
from sandbox_science.policy_middleware import evaluator
from sandbox_science.policy_middleware.cli import main
from sandbox_science.policy_middleware.evaluator import HttpRequestFacts, PolicyMiddleware
from sandbox_science.policy_middleware.events import SOURCE, StoreSink
from sandbox_science.policy_middleware.store import PolicyStore

FactsFactory = Callable[..., HttpRequestFacts]
ConfigFor = Callable[[LoadedPolicy], dict[str, object]]
POLICY = """\
apiVersion: sandbox-science/v0alpha1
kind: ProjectPolicy
metadata:
  name: test
defaults: {action: allow}
network:
  rules:
    - {name: watched, operation: http, match: {hosts: [watched.example.org]}, action: alert}
    - {name: writes, operation: http, match: {hosts: ["*"], methods: [POST]}, action: escalate}
monitors:
  - {name: chatty, when: {egress_bytes_per_hour_over: 100}, action: alert}
"""
PRIVATE_QUERY = "token=test-secret-placeholder"
PRIVATE_BODY = b"test-secret-body"


@pytest.fixture
def events(tmp_path: Path) -> Iterator[EventStore]:
    with EventStore(tmp_path / "events" / "events.db", timeout=0.05) as store:
        yield store


@pytest.fixture
def loaded(store: PolicyStore, sandbox_id: str, example: LoadedPolicy) -> LoadedPolicy:
    del example  # bound first by make_facts; rebound here
    policy = store.add_policy(POLICY.encode())
    store.bind(sandbox_id, policy.digest)
    return policy


@pytest.fixture
def recording(store: PolicyStore, loaded: LoadedPolicy, events: EventStore) -> PolicyMiddleware:
    del loaded
    return PolicyMiddleware(
        store, ceiling=PoolCeiling.unrestricted(), floor=HardFloor(), events=StoreSink(events)
    )


@pytest.fixture
def facts(make_facts: FactsFactory, loaded: LoadedPolicy, policy_config: ConfigFor) -> FactsFactory:
    """Request facts under the test policy."""

    def make(**overrides: object) -> HttpRequestFacts:
        return make_facts(**{"config": policy_config(loaded), **overrides})

    return make


def stored(events: EventStore) -> list[tuple[str, str, dict[str, object]]]:
    with StoreReader(events.path) as reader:
        return [(record.kind, record.source, record.data()) for record in reader.records()]


def test_escalations_alerts_and_monitor_findings_are_stored(
    recording: PolicyMiddleware, facts: FactsFactory, events: EventStore, loaded: LoadedPolicy
) -> None:
    escalated = recording.evaluate_http(
        facts(method="POST", path="/v1/items", query=PRIVATE_QUERY, body=PRIVATE_BODY)
    )
    assert escalated.reason_code == evaluator.APPROVAL_REQUIRED
    # Allowed with an alert; its 200-byte path also crosses the monitor's threshold.
    alerted = recording.evaluate_http(facts(host="watched.example.org", path="/" + "a" * 200))
    assert alerted.allowed
    assert [finding.type for finding in alerted.findings] == ["policy_alert", "policy_monitor"]

    records = stored(events)
    assert [(kind, source) for kind, source, _ in records] == [
        ("escalation", SOURCE),
        ("alert", SOURCE),
        ("monitor", SOURCE),
    ]
    policy = {"name": "test", "digest": loaded.digest}
    assert records[0][2] == {
        "sandbox_id": "sbx-0001",
        "request_id": "req-1",
        "policy": policy,
        "rule": "writes",
        "reason": records[0][2]["reason"],
        "request": {
            "method": "POST",
            "scheme": "https",
            "host": "example.org",
            "port": 443,
            "path": "/v1/items",
            "query_bytes": len(PRIVATE_QUERY),
        },
        "escalation": {"timeout_seconds": 1800, "on_timeout": "deny"},
    }
    assert records[1][2]["rule"] == "watched"
    assert records[2][2] == {
        "sandbox_id": "sbx-0001",
        "request_id": "req-1",
        "policy": policy,
        "rule": "chatty",
        "reason": records[2][2]["reason"],
    }
    assert verify_store(events.path).ok


def test_query_strings_and_bodies_never_reach_the_disk(
    recording: PolicyMiddleware, facts: FactsFactory, events: EventStore
) -> None:
    recording.evaluate_http(facts(method="POST", query=PRIVATE_QUERY, body=PRIVATE_BODY))
    recording.evaluate_http(facts(host="watched.example.org", query=PRIVATE_QUERY))
    assert len(stored(events)) == 2
    files = list(events.path.parent.iterdir())
    assert files  # the database and its write-ahead log
    for file in files:
        data = file.read_bytes()
        assert b"test-secret" not in data, file.name


def test_an_alert_that_cannot_be_stored_is_denied(
    recording: PolicyMiddleware, facts: FactsFactory, events: EventStore
) -> None:
    # Another writer holds the store's lock for longer than the middleware waits.
    blocker = sqlite3.connect(events.path, isolation_level=None)
    try:
        blocker.execute("BEGIN IMMEDIATE")
        alert = recording.evaluate_http(facts(host="watched.example.org"))
        assert not alert.allowed
        assert alert.reason_code == evaluator.AUDIT_UNAVAILABLE
        escalation = recording.evaluate_http(facts(method="POST"))
        assert escalation.reason_code == evaluator.AUDIT_UNAVAILABLE
        # Requests that need no record are unaffected, monitors included.
        allowed = recording.evaluate_http(facts(path="/" + "a" * 200))
        assert allowed.allowed
        assert [finding.label for finding in allowed.findings] == ["chatty"]
        blocker.execute("ROLLBACK")
    finally:
        blocker.close()
    assert stored(events) == []
    assert recording.evaluate_http(facts(host="watched.example.org")).allowed
    assert [kind for kind, _, _ in stored(events)] == ["alert"]


def test_an_alert_is_denied_once_the_store_is_closed(
    recording: PolicyMiddleware, facts: FactsFactory, events: EventStore
) -> None:
    events.close()
    verdict = recording.evaluate_http(facts(host="watched.example.org"))
    assert verdict.reason_code == evaluator.AUDIT_UNAVAILABLE


def test_serve_refuses_a_file_that_is_not_an_event_store(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    other = tmp_path / "other.db"
    db = sqlite3.connect(other)
    db.execute("CREATE TABLE entries (key TEXT)")
    db.commit()
    db.close()
    (tmp_path / "ceiling.yaml").write_text(
        "apiVersion: sandbox-science/v0alpha1\nkind: PoolCeiling\n"
    )
    settings = tmp_path / "middleware.toml"
    settings.write_text(
        'state_dir = "state"\nceiling = "ceiling.yaml"\nevent_store = "other.db"\n'
        "allow_unauthenticated = true\n"
    )
    assert main(["serve", "--settings", str(settings)]) == 1
    assert "not an event store" in capsys.readouterr().err
