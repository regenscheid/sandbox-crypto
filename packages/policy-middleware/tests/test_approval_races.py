# SPDX-License-Identifier: MIT
"""Approval-path races and costs (plan item D-09): concurrent identical escalations record
one permission request, and the store is folded at startup, not in a request."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterable, Iterator, Mapping
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from typing import Any

import pytest

from sandbox_science.event_store import EventStore, Record, StoreReader
from sandbox_science.policy import HardFloor, LoadedPolicy, PoolCeiling, reasons
from sandbox_science.policy.approvals import Approvals, prepare_answer
from sandbox_science.policy_middleware import evaluator
from sandbox_science.policy_middleware.cli import _read_records
from sandbox_science.policy_middleware.evaluator import HttpRequestFacts, PolicyMiddleware, Verdict
from sandbox_science.policy_middleware.events import StoreSink
from sandbox_science.policy_middleware.store import PolicyStore

from .conftest import FactsFactory

POLICY = """\
apiVersion: sandbox-science/v0alpha1
kind: ProjectPolicy
metadata:
  name: test
defaults: {action: allow}
network:
  rules:
    - {name: writes, operation: http, match: {hosts: ["*"], methods: [POST]}, action: escalate}
"""


WAIT = 10.0
"""The longest a test waits for another thread: a cap, never a pause."""


class Race:
    """Two identical escalations, A and B, held at the steps that decide the race, by
    events with capped waits rather than by timing.

    Both read the store before either records (``read``), so each finds no permission
    request outside the recording lock. With ``held_read``, B's first read returns what
    it read only once A's evaluation has returned, and A's read just after it recorded
    does not show the request yet, as a store that lags would: so B, outside the lock,
    still finds no request, takes the lock after A released it, and must find the
    request inside it. Without it, the one that takes the lock records only once the
    other's evaluation has returned: the other waited on the lock longer than an
    escalation waits for it, as on a slow store."""

    def __init__(self, events: EventStore, *, held_read: bool = False) -> None:
        self.events = events
        self.sink = StoreSink(events)
        self.held_read = held_read
        self.met = threading.Barrier(2)
        self.roles: dict[int, str] = {}
        self.racers: set[int] = set()
        self.lock = threading.Lock()
        self.done = {"A": threading.Event(), "B": threading.Event()}
        self.recorded: set[int] = set()

    def read(self, after: int) -> list[Record]:
        found = _read_records(self.events.path, after)
        me = threading.get_ident()
        if self.held_read and me in self.recorded:
            self.recorded.discard(me)
            return []  # the read just after recording lags behind the store
        with self.lock:
            racing = me not in self.racers and len(self.racers) < 2
            if racing:
                self.racers.add(me)
        if racing:
            # Both read before either records: each finds no request outside the lock.
            # A later evaluation, such as a retry, is not held.
            self.roles[me] = "B" if self.met.wait(timeout=WAIT) == 0 else "A"
            if self.held_read and self.roles[me] == "B":
                assert self.done["A"].wait(timeout=WAIT)
        return found

    def record(self, kind: str, payload: Mapping[str, object]) -> int:
        if kind == "escalation" and not self.held_read:
            # Whichever holds the lock records only once the other has given up on it.
            other = "B" if self.roles[threading.get_ident()] == "A" else "A"
            assert self.done[other].wait(timeout=WAIT)
        seq = self.sink.record(kind, payload)
        if kind == "escalation":
            self.recorded.add(threading.get_ident())
        return seq

    def evaluate(self, middleware: PolicyMiddleware, facts: HttpRequestFacts) -> Verdict:
        try:
            return middleware.evaluate_http(facts)
        finally:
            self.done[self.roles[threading.get_ident()]].set()


@pytest.fixture
def loaded(store: PolicyStore, sandbox_id: str, example: LoadedPolicy) -> LoadedPolicy:
    del example  # bound first by make_facts; rebound here
    policy = store.add_policy(POLICY.encode())
    store.bind(sandbox_id, policy.digest)
    return policy


@pytest.fixture
def events(tmp_path: Path) -> Iterator[EventStore]:
    with EventStore(tmp_path / "events.db") as store:
        yield store


def escalations(events: EventStore) -> list[Record]:
    with StoreReader(events.path) as reader:
        return [record for record in reader.records() if record.kind == "escalation"]


def race(
    race: Race, store: PolicyStore, loaded: LoadedPolicy, make_facts: FactsFactory
) -> tuple[PolicyMiddleware, list[Verdict], Callable[[str], HttpRequestFacts]]:
    """Evaluate two identical escalations at once, as ``race`` holds them: the
    middleware, the two verdicts, and the requests' facts by ID."""
    middleware = PolicyMiddleware(
        store,
        ceiling=PoolCeiling.unrestricted(),
        floor=HardFloor(),
        events=race,
        requests=race.read,
    )
    config = {"policy": {"name": loaded.policy.metadata.name, "digest": loaded.digest}}

    def facts(request_id: str) -> HttpRequestFacts:
        return make_facts(method="POST", path="/v1/items", request_id=request_id, config=config)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(race.evaluate, middleware, facts(f"req-{n}")) for n in (1, 2)]
        verdicts = [future.result(timeout=WAIT) for future in futures]
    assert sorted(race.roles.values()) == ["A", "B"]
    return middleware, verdicts, facts


def test_concurrent_identical_escalations_record_one_request(
    store: PolicyStore, loaded: LoadedPolicy, events: EventStore, make_facts: FactsFactory
) -> None:
    """D-09: both escalations find no request outside the lock; one records it, and the
    other, taking the lock after it, finds the request inside and waits on it."""
    _, verdicts, _ = race(Race(events, held_read=True), store, loaded, make_facts)
    assert [v.reason_code for v in verdicts] == [reasons.APPROVAL_REQUIRED] * 2
    [request] = escalations(events)
    waiting = f"permission request {request.seq}, still waiting"
    assert sum(waiting in verdict.reason for verdict in verdicts) == 1
    recorded = f"(permission request {request.seq})"
    assert sum(verdict.reason.endswith(recorded) for verdict in verdicts) == 1


def test_an_escalation_that_waits_too_long_for_the_lock_records_nothing(
    store: PolicyStore, loaded: LoadedPolicy, events: EventStore, make_facts: FactsFactory
) -> None:
    """D-09, a slow store: while A records, B waits on the lock longer than an escalation
    waits for it, and is refused, asked to retry, recording nothing; its retry waits on
    A's request. (The old version of the test above failed this way when CI was slow.)"""
    middleware, verdicts, facts = race(Race(events), store, loaded, make_facts)
    [request] = escalations(events)
    reasons_given = sorted(verdict.reason for verdict in verdicts)
    assert any("a permission request is being recorded; retry" in r for r in reasons_given)
    assert any(r.endswith(f"(permission request {request.seq})") for r in reasons_given)
    retried = middleware.evaluate_http(facts("req-3"))
    assert retried.reason_code == reasons.APPROVAL_REQUIRED
    assert f"permission request {request.seq}, still waiting" in retried.reason
    assert len(escalations(events)) == 1


class CountingReader:
    """The middleware's record reader, noting where each read started."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.starts: list[int] = []

    def __call__(self, after: int) -> Iterable[Record]:
        self.starts.append(after)
        return _read_records(self.path, after)


def test_the_store_is_folded_at_startup_not_in_a_request(
    store: PolicyStore, loaded: LoadedPolicy, events: EventStore, make_facts: FactsFactory
) -> None:
    for number in range(2000):
        events.append("monitor", "policy-middleware", {"rule": f"m{number}"})
    events.append("escalation", "policy-middleware", {"target": {"kind": "http"}})
    reader = CountingReader(events.path)
    middleware = PolicyMiddleware(
        store,
        ceiling=PoolCeiling.unrestricted(),
        floor=HardFloor(),
        events=StoreSink(events),
        requests=reader,
    )
    middleware.prime_approvals()
    assert reader.starts == [0]
    config = {"policy": {"name": loaded.policy.metadata.name, "digest": loaded.digest}}
    facts = make_facts(method="POST", path="/v1/items", config=config)
    assert middleware.evaluate_http(facts).reason_code == reasons.APPROVAL_REQUIRED
    # The request read only what came after the primed records.
    assert reader.starts[1] == 2001


def test_a_read_moves_past_the_other_records_after_the_last_request(
    store: PolicyStore, loaded: LoadedPolicy, events: EventStore, make_facts: FactsFactory
) -> None:
    # The records after the last permission request are of other kinds: a read that kept
    # its place by the last request it saw would scan them all again on every request.
    events.append("escalation", "policy-middleware", {"target": {"kind": "http"}})
    for number in range(2000):
        events.append("monitor", "policy-middleware", {"rule": f"m{number}"})
    reader = CountingReader(events.path)
    middleware = PolicyMiddleware(
        store,
        ceiling=PoolCeiling.unrestricted(),
        floor=HardFloor(),
        events=StoreSink(events),
        requests=reader,
    )
    middleware.prime_approvals()
    config = {"policy": {"name": loaded.policy.metadata.name, "digest": loaded.digest}}
    facts = make_facts(method="POST", path="/v1/items", config=config)
    assert middleware.evaluate_http(facts).reason_code == reasons.APPROVAL_REQUIRED
    # Every read the request made started after the newest record, not the request.
    assert reader.starts[0] == 0
    assert set(reader.starts[1:]) == {2001}


def middleware_on(
    store: PolicyStore,
    events: EventStore,
    requests: Callable[[int], Iterable[Record]] | None = None,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> PolicyMiddleware:
    return PolicyMiddleware(
        store,
        ceiling=PoolCeiling.unrestricted(),
        floor=HardFloor(),
        events=StoreSink(events),
        now=now,
        requests=requests if requests is not None else partial(_read_records, events.path),
    )


def held(middleware: PolicyMiddleware) -> bool:
    """Whether any of the middleware's recording locks is held."""
    return any(lock.locked() for lock in middleware._recording)


def test_an_escalation_waits_for_its_lock_a_bounded_time(
    store: PolicyStore, loaded: LoadedPolicy, events: EventStore, make_facts: FactsFactory
) -> None:
    middleware = middleware_on(store, events)
    config = {"policy": {"name": loaded.policy.metadata.name, "digest": loaded.digest}}
    facts = make_facts(method="POST", path="/v1/items", config=config)
    locks = middleware._recording
    for lock in locks:
        lock.acquire()
    try:
        started = time.monotonic()
        verdict = middleware.evaluate_http(facts)
        waited = time.monotonic() - started
    finally:
        for lock in locks:
            lock.release()
    # Refused, with nothing recorded; a retry records the request.
    assert verdict.reason_code == reasons.APPROVAL_REQUIRED
    assert "being recorded" in verdict.reason
    assert waited < evaluator._RECORDING_WAIT + 0.2
    assert escalations(events) == []
    assert middleware.evaluate_http(facts).reason_code == reasons.APPROVAL_REQUIRED
    assert len(escalations(events)) == 1


def test_an_escalation_is_built_before_its_lock_is_taken(
    store: PolicyStore,
    loaded: LoadedPolicy,
    events: EventStore,
    make_facts: FactsFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The opam archive lookup, part of the record, can be slow: no other escalation waits on it.
    middleware = middleware_on(store, events)
    build = middleware._payload
    built_holding: list[bool] = []

    def payload(*args: Any, **kwargs: Any) -> dict[str, object]:
        built_holding.append(held(middleware))
        return build(*args, **kwargs)

    monkeypatch.setattr(middleware, "_payload", payload)
    config = {"policy": {"name": loaded.policy.metadata.name, "digest": loaded.digest}}
    facts = make_facts(method="POST", path="/v1/items", config=config)
    assert middleware.evaluate_http(facts).reason_code == reasons.APPROVAL_REQUIRED
    assert built_holding == [False]
    assert len(escalations(events)) == 1


def test_a_request_answered_while_it_waits_for_the_lock_gets_the_answer(
    store: PolicyStore, loaded: LoadedPolicy, events: EventStore, make_facts: FactsFactory
) -> None:
    # Another evaluation records the request and the operator denies it after this one
    # found none and before it read the store again inside the lock.
    config = {"policy": {"name": loaded.policy.metadata.name, "digest": loaded.digest}}
    facts = make_facts(method="POST", path="/v1/items", config=config)
    other = middleware_on(store, events)
    answered: list[int] = []

    def requests(after: int) -> list[Record]:
        if held(middleware) and not answered:
            assert other.evaluate_http(facts).reason_code == reasons.APPROVAL_REQUIRED
            [request] = escalations(events)
            approvals = Approvals()
            approvals.load(_read_records(events.path, 0))
            kind, payload = prepare_answer(
                approvals, request.seq, approve=False, scope=None, hours=None, now=datetime.now(UTC)
            )
            answered.append(events.append(kind, "operator", payload).seq)
        return _read_records(events.path, after)

    middleware = middleware_on(store, events, requests=requests)
    verdict = middleware.evaluate_http(facts)
    assert answered
    assert verdict.reason_code == reasons.APPROVAL_DENIED
    assert len(escalations(events)) == 1


def test_a_request_takes_the_time_after_reading_the_store(
    store: PolicyStore, loaded: LoadedPolicy, events: EventStore, make_facts: FactsFactory
) -> None:
    # An answer read in a refresh is judged at a time no earlier than the read.
    steps: list[str] = []

    def requests(after: int) -> list[Record]:
        steps.append("read")
        return _read_records(events.path, after)

    def now() -> datetime:
        steps.append("now")
        return datetime.now(UTC)

    middleware = middleware_on(store, events, requests=requests, now=now)
    config = {"policy": {"name": loaded.policy.metadata.name, "digest": loaded.digest}}
    facts = make_facts(method="POST", path="/v1/items", config=config)
    assert middleware.evaluate_http(facts).reason_code == reasons.APPROVAL_REQUIRED
    assert steps[0] == "read", steps
    assert steps.count("now") >= 2  # the resolutions outside and inside the lock
