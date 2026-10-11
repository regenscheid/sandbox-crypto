# SPDX-License-Identifier: MIT
"""Permission requests, approvals, denials and escalation timeouts (D76), in the middleware."""

from __future__ import annotations

import dataclasses
import re
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from functools import partial
from pathlib import Path

import pytest

from sandbox_science.event_store import EventStore, StoreReader, verify_store
from sandbox_science.policy import HardFloor, LoadedPolicy, PoolCeiling
from sandbox_science.policy.approvals import APPROVAL, DENIAL, Approvals, RequestKey
from sandbox_science.policy_middleware import evaluator
from sandbox_science.policy_middleware.cli import _read_records, main  # pyright: ignore[reportPrivateUsage]
from sandbox_science.policy_middleware.evaluator import HttpRequestFacts, PolicyMiddleware
from sandbox_science.policy_middleware.events import OPERATOR, StoreSink
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
    - {name: writes, operation: http, match: {hosts: ["*"], methods: [POST]}, action: escalate}
    - name: reads
      operation: http
      match: {hosts: [reads.example.org], methods: [GET]}
      action: escalate
      escalation: {timeout: 5m, on_timeout: allow}
"""


class Clock:
    """The middleware's idea of now, which a test can move forward."""

    def __init__(self) -> None:
        self.offset = timedelta()

    def __call__(self) -> datetime:
        return datetime.now(UTC) + self.offset


@pytest.fixture
def events(tmp_path: Path) -> Iterator[EventStore]:
    with EventStore(tmp_path / "state" / "events.db", timeout=0.5) as store:
        yield store


@pytest.fixture
def loaded(store: PolicyStore, sandbox_id: str, example: LoadedPolicy) -> LoadedPolicy:
    del example  # bound first by make_facts; rebound here
    policy = store.add_policy(POLICY.encode())
    store.bind(sandbox_id, policy.digest)
    return policy


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def middleware(
    store: PolicyStore, loaded: LoadedPolicy, events: EventStore, clock: Clock
) -> PolicyMiddleware:
    del loaded
    return PolicyMiddleware(
        store,
        ceiling=PoolCeiling.unrestricted(),
        floor=HardFloor(),
        events=StoreSink(events),
        now=clock,
        requests=partial(_read_records, events.path),
    )


@pytest.fixture
def facts(make_facts: FactsFactory, loaded: LoadedPolicy, policy_config: ConfigFor) -> FactsFactory:
    def make(**overrides: object) -> HttpRequestFacts:
        defaults: dict[str, object] = {"config": policy_config(loaded), "method": "POST"}
        return make_facts(**{**defaults, "path": "/v1/items", **overrides})

    return make


@pytest.fixture
def settings(tmp_path: Path) -> Path:
    path = tmp_path / "middleware.toml"
    path.write_text('state_dir = "state"\nceiling = "c.yaml"\nallow_unauthenticated = true\n')
    return path


def kinds(events: EventStore) -> list[str]:
    with StoreReader(events.path) as reader:
        return [record.kind for record in reader.records()]


def listed(output: str) -> list[int]:
    """The request numbers ``pending`` printed, one per line."""
    return [int(line.split()[0]) for line in output.splitlines() if line[:1].isdigit()]


def request_number(reason: str) -> int:
    found = re.search(r"permission request (\d+)", reason)
    assert found is not None, reason
    return int(found[1])


def test_a_retry_waits_on_the_same_request(
    middleware: PolicyMiddleware, facts: FactsFactory, events: EventStore
) -> None:
    first = middleware.evaluate_http(facts())
    assert first.reason_code == evaluator.APPROVAL_REQUIRED
    seq = request_number(first.reason)
    again = middleware.evaluate_http(facts(request_id="req-2"))
    assert again.reason_code == evaluator.APPROVAL_REQUIRED
    assert f"permission request {seq}, still waiting" in again.reason
    assert kinds(events) == ["escalation"]


def test_an_approval_lets_retries_through_and_is_recorded(
    middleware: PolicyMiddleware,
    facts: FactsFactory,
    events: EventStore,
    settings: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    seq = request_number(middleware.evaluate_http(facts()).reason)
    assert main(["pending", "--settings", str(settings)]) == 0
    assert listed(capsys.readouterr().out) == [seq]
    assert main(["approve", "--settings", str(settings), "--request", str(seq)]) == 0
    allowed = middleware.evaluate_http(facts(request_id="req-2"))
    assert allowed.allowed
    assert [finding.type for finding in allowed.findings] == ["policy_approval"]
    # Another path on the host is another request: this approval covers the request only.
    other = middleware.evaluate_http(facts(path="/v1/other"))
    assert other.reason_code == evaluator.APPROVAL_REQUIRED
    assert kinds(events) == ["escalation", APPROVAL, "approved", "escalation"]
    assert main(["pending", "--settings", str(settings)]) == 0
    assert seq not in listed(capsys.readouterr().out)
    assert verify_store(events.path).ok


def test_a_host_wide_approval_covers_other_paths_with_the_same_method(
    middleware: PolicyMiddleware, facts: FactsFactory, settings: Path
) -> None:
    seq = request_number(middleware.evaluate_http(facts()).reason)
    assert (
        main(["approve", "--settings", str(settings), "--request", str(seq), "--scope", "host"])
        == 0
    )
    assert middleware.evaluate_http(facts(path="/v1/other")).allowed
    # Not for another host (or another sandbox: see below).
    elsewhere = middleware.evaluate_http(facts(host="other.example.org"))
    assert elsewhere.reason_code == evaluator.APPROVAL_REQUIRED


def test_a_denial_refuses_retries(
    middleware: PolicyMiddleware, facts: FactsFactory, settings: Path
) -> None:
    seq = request_number(middleware.evaluate_http(facts()).reason)
    assert main(["deny", "--settings", str(settings), "--request", str(seq)]) == 0
    denied = middleware.evaluate_http(facts(request_id="req-2"))
    assert denied.reason_code == evaluator.APPROVAL_DENIED
    assert f"permission request {seq} was denied" in denied.reason


def test_the_latest_answer_decides(
    middleware: PolicyMiddleware, facts: FactsFactory, settings: Path
) -> None:
    seq = request_number(middleware.evaluate_http(facts()).reason)
    assert main(["approve", "--settings", str(settings), "--request", str(seq)]) == 0
    assert main(["deny", "--settings", str(settings), "--request", str(seq)]) == 0
    assert middleware.evaluate_http(facts()).reason_code == evaluator.APPROVAL_DENIED


def test_an_approval_expires(
    middleware: PolicyMiddleware, facts: FactsFactory, settings: Path, clock: Clock
) -> None:
    seq = request_number(middleware.evaluate_http(facts()).reason)
    assert main(["approve", "--settings", str(settings), "--request", str(seq), "--for", "1"]) == 0
    assert middleware.evaluate_http(facts()).allowed
    clock.offset = timedelta(hours=2)
    # The approval is over; the request itself has timed out, and writes time out to deny.
    assert middleware.evaluate_http(facts()).reason_code == evaluator.ESCALATION_TIMED_OUT


def test_an_unanswered_write_times_out_to_deny(
    middleware: PolicyMiddleware, facts: FactsFactory, clock: Clock
) -> None:
    seq = request_number(middleware.evaluate_http(facts()).reason)
    clock.offset = timedelta(minutes=31)
    late = middleware.evaluate_http(facts(request_id="req-2"))
    assert late.reason_code == evaluator.ESCALATION_TIMED_OUT
    assert f"permission request {seq} was not answered within 30 minutes" in late.reason


def test_an_unanswered_read_proceeds_as_an_alert_after_its_timeout(
    middleware: PolicyMiddleware, facts: FactsFactory, events: EventStore, clock: Clock
) -> None:
    read = partial(facts, method="GET", host="reads.example.org", path="/paper.pdf")
    seq = request_number(middleware.evaluate_http(read()).reason)
    assert middleware.evaluate_http(read()).reason_code == evaluator.APPROVAL_REQUIRED
    clock.offset = timedelta(minutes=6)
    late = middleware.evaluate_http(read(request_id="req-3"))
    assert late.allowed
    assert [finding.type for finding in late.findings] == ["policy_alert"]
    with StoreReader(events.path) as reader:
        alert = [record for record in reader.records() if record.kind == "alert"][-1]
    assert alert.data()["timed_out_request"] == seq


def test_requests_belong_to_their_sandbox(
    middleware: PolicyMiddleware,
    facts: FactsFactory,
    events: EventStore,
    store: PolicyStore,
    loaded: LoadedPolicy,
    settings: Path,
) -> None:
    seq = request_number(middleware.evaluate_http(facts()).reason)
    assert (
        main(["approve", "--settings", str(settings), "--request", str(seq), "--scope", "host"])
        == 0
    )
    store.bind("sbx-0002", loaded.digest)
    other = middleware.evaluate_http(facts(sandbox_id="sbx-0002"))
    assert other.reason_code == evaluator.APPROVAL_REQUIRED
    assert request_number(other.reason) != seq


def test_answering_an_unknown_request_fails(settings: Path, events: EventStore) -> None:
    del events
    assert main(["approve", "--settings", str(settings), "--request", "99"]) == 1
    assert main(["approve", "--settings", str(settings), "--request", "1", "--for", "0"]) == 1


def test_malformed_records_are_skipped(events: EventStore) -> None:
    events.append(APPROVAL, OPERATOR, {"request_seq": "one", "scope": "request"})
    events.append(DENIAL, OPERATOR, {"request_seq": 1, "scope": "everything", "target": {}})
    events.append("escalation", OPERATOR, {"target": {"sandbox_id": "s"}, "escalation": {}})
    approvals = Approvals()
    approvals.load(_read_records(events.path, 0))
    assert approvals.unanswered() == []
    assert approvals.last_seq == 3
    target = RequestKey.http("s", "sha256:" + "0" * 64, "POST", "example.org", 443, "/")
    assert approvals.answer(target, datetime.now(UTC)) is None


def test_without_a_record_reader_every_escalation_is_recorded(
    store: PolicyStore, loaded: LoadedPolicy, events: EventStore, facts: FactsFactory
) -> None:
    del loaded
    plain = PolicyMiddleware(
        store, ceiling=PoolCeiling.unrestricted(), floor=HardFloor(), events=StoreSink(events)
    )
    plain.evaluate_http(facts())
    plain.evaluate_http(facts(request_id="req-2"))
    assert kinds(events) == ["escalation", "escalation"]


# A provisioning operation's request for an exact set of package files (Conda provisioning).

PACKAGES_POLICY = """\
apiVersion: sandbox-science/v0alpha1
kind: ProjectPolicy
metadata:
  name: packages
packages:
  conda: {channels: [conda-forge], action: escalate}
  pypi: {action: allow}
"""
PARI = "/conda/conda-forge/linux-64/pari-2.15.4-h0_0.conda"
GMP = "/conda/conda-forge/linux-64/gmp-6.3.0-h0_1.conda"
FLINT = "/conda/conda-forge/linux-64/flint-3.1.0-h0_0.conda"


Fetch = Callable[..., evaluator.Verdict]


@pytest.fixture
def packages(
    store: PolicyStore,
    sandbox_id: str,
    events: EventStore,
    clock: Clock,
    make_facts: FactsFactory,
) -> tuple[LoadedPolicy, Fetch]:
    """A policy that escalates Conda packages, bound to two sandboxes, and a download of a
    file at the package cache by one of them (the first by default)."""
    policy = store.add_policy(PACKAGES_POLICY.encode())
    store.bind(sandbox_id, policy.digest)
    store.bind("sbx-0002", policy.digest)
    middleware = PolicyMiddleware(
        store,
        ceiling=PoolCeiling.unrestricted(),
        floor=HardFloor(),
        events=StoreSink(events),
        now=clock,
        requests=partial(_read_records, events.path),
    )
    config = {
        "policy": {"name": "packages", "digest": policy.digest},
        "package_cache": {"host": "cache.internal", "port": 8090},
    }

    def fetch(path: str, sandbox: str = sandbox_id) -> evaluator.Verdict:
        facts = make_facts(
            sandbox_id=sandbox, host="cache.internal", port=8090, scheme="http", path=path
        )
        return middleware.evaluate_http(dataclasses.replace(facts, config=config))

    return policy, fetch


def packages_request(
    events: EventStore, sandbox_id: str, policy: LoadedPolicy, operation: str, paths: list[str]
) -> int:
    """Record a provisioning operation's request for ``paths``, as the worker does."""
    key = RequestKey.packages(sandbox_id, policy.digest, operation, paths)
    payload: dict[str, object] = {
        "target": key.payload(),
        "escalation": {"timeout_seconds": 1800, "on_timeout": "deny"},
        "files": [{"path": path} for path in paths],
    }
    return events.append("escalation", "worker", payload).seq


def test_an_approved_file_set_downloads_and_nothing_else(
    packages: tuple[LoadedPolicy, Fetch],
    sandbox_id: str,
    events: EventStore,
    clock: Clock,
    settings: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    policy, fetch = packages
    # The worker asks once for the files a resolved install would download.
    key = RequestKey.packages(sandbox_id, policy.digest, "prv-1", [PARI, GMP])
    payload: dict[str, object] = {
        "target": key.payload(),
        "escalation": {"timeout_seconds": 1800, "on_timeout": "deny"},
        "files": [{"path": PARI, "name": "pari"}, {"path": GMP, "name": "gmp"}],
    }
    seq = events.append("escalation", "worker", payload).seq
    assert main(["pending", "--settings", str(settings)]) == 0
    out = capsys.readouterr().out
    assert listed(out) == [seq]
    assert PARI in out
    assert GMP in out
    # In full, on demand, before anyone answers it.
    assert main(["pending", "--settings", str(settings), "--request", str(seq)]) == 0
    shown = capsys.readouterr().out.splitlines()
    assert "waiting" in shown[1]
    assert [line.strip() for line in shown[3:]] == sorted([PARI, GMP])
    assert main(["pending", "--settings", str(settings), "--request", "999"]) == 1
    # Unanswered, its files wait like any escalated download; index reads go on.
    assert fetch(PARI).reason_code == evaluator.APPROVAL_REQUIRED
    assert fetch("/conda/conda-forge/linux-64/repodata.json").allowed
    assert main(["approve", "--settings", str(settings), "--request", str(seq)]) == 0
    for path in (PARI, GMP):
        allowed = fetch(path)
        assert allowed.allowed, path
        assert [finding.type for finding in allowed.findings] == ["policy_approval"]
    with StoreReader(events.path) as reader:
        approved = [record for record in reader.records() if record.kind == "approved"]
    assert [record.data()["grant"] for record in approved] == ["packages", "packages"]
    assert {record.data()["request_seq"] for record in approved} == {seq}
    # Nothing else: another file, another sandbox, or once a denial withdraws the set.
    assert fetch(FLINT).reason_code == evaluator.APPROVAL_REQUIRED
    assert fetch(PARI, "sbx-0002").reason_code == evaluator.APPROVAL_REQUIRED
    assert main(["deny", "--settings", str(settings), "--request", str(seq)]) == 0
    assert fetch(GMP).reason_code == evaluator.APPROVAL_REQUIRED
    # And not once the approval expires.
    second = RequestKey.packages(sandbox_id, policy.digest, "prv-2", [FLINT])
    payload = {**payload, "target": second.payload(), "files": [{"path": FLINT}]}
    seq = events.append("escalation", "worker", payload).seq
    assert main(["approve", "--settings", str(settings), "--request", str(seq), "--for", "1"]) == 0
    assert fetch(FLINT).allowed
    clock.offset = timedelta(hours=2)
    assert not fetch(FLINT).allowed


def test_a_later_answer_to_the_download_itself_decides_it(
    packages: tuple[LoadedPolicy, Fetch], sandbox_id: str, events: EventStore, settings: Path
) -> None:
    policy, fetch = packages
    # The sandbox asks for PARI before anyone approves: a permission request of its own.
    direct = request_number(fetch(PARI).reason)
    seq = packages_request(events, sandbox_id, policy, "prv-1", [PARI, GMP])
    assert main(["approve", "--settings", str(settings), "--request", str(seq)]) == 0
    assert fetch(PARI).allowed
    # Denied afterwards, that download is refused: the newest answer decides.
    assert main(["deny", "--settings", str(settings), "--request", str(direct)]) == 0
    assert fetch(PARI).reason_code == evaluator.APPROVAL_DENIED
    assert fetch(GMP).allowed
    # A set approved later still decides it again.
    seq = packages_request(events, sandbox_id, policy, "prv-2", [PARI])
    assert main(["approve", "--settings", str(settings), "--request", str(seq)]) == 0
    assert fetch(PARI).allowed
