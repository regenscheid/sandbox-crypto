# SPDX-License-Identifier: MIT
"""Approval-path races and costs (plan item D-09): concurrent identical escalations record
one permission request, and the store is folded at startup, not in a request."""

from __future__ import annotations

import contextlib
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


class Gate:
    """A sink that holds each escalation until a second one arrives, or a short while
    passes: two requests that both found no permission request meet here. The while is
    shorter than an escalation waits for its lock."""

    def __init__(self, sink: StoreSink) -> None:
        self.sink = sink
        self.barrier = threading.Barrier(2)

    def record(self, kind: str, payload: Mapping[str, object]) -> int:
        if kind == "escalation":
            with contextlib.suppress(threading.BrokenBarrierError):
                self.barrier.wait(timeout=0.1)
        return self.sink.record(kind, payload)


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


def test_concurrent_identical_escalations_record_one_request(
    store: PolicyStore,
    loaded: LoadedPolicy,
    events: EventStore,
    make_facts: FactsFactory,
    policy_config: object,
) -> None:
    middleware = PolicyMiddleware(
        store,
        ceiling=PoolCeiling.unrestricted(),
        floor=HardFloor(),
        events=Gate(StoreSink(events)),
        requests=partial(_read_records, events.path),
    )
    config = {"policy": {"name": loaded.policy.metadata.name, "digest": loaded.digest}}

    def evaluate(request_id: str) -> Verdict:
        facts: HttpRequestFacts = make_facts(
            method="POST", path="/v1/items", request_id=request_id, config=config
        )
        return middleware.evaluate_http(facts)

    with ThreadPoolExecutor(max_workers=2) as pool:
        verdicts = list(pool.map(evaluate, ["req-1", "req-2"]))
    assert [verdict.reason_code for verdict in verdicts] == [reasons.APPROVAL_REQUIRED] * 2
    [request] = escalations(events)
    # One recorded the request; the other found it, inside the lock, and waits on it.
    waiting = f"permission request {request.seq}, still waiting"
    assert sum(waiting in verdict.reason for verdict in verdicts) == 1


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
