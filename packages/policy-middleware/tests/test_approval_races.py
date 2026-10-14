# SPDX-License-Identifier: MIT
"""Approval-path races and costs (plan item D-09): concurrent identical escalations record
one permission request, and the store is folded at startup, not in a request."""

from __future__ import annotations

import contextlib
import threading
from collections.abc import Iterable, Iterator, Mapping
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from pathlib import Path

import pytest

from sandbox_science.event_store import EventStore, Record, StoreReader
from sandbox_science.policy import HardFloor, LoadedPolicy, PoolCeiling, reasons
from sandbox_science.policy_middleware.cli import _read_records
from sandbox_science.policy_middleware.evaluator import HttpRequestFacts, PolicyMiddleware
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
    passes: two requests that both found no permission request meet here."""

    def __init__(self, sink: StoreSink) -> None:
        self.sink = sink
        self.barrier = threading.Barrier(2)

    def record(self, kind: str, payload: Mapping[str, object]) -> int:
        if kind == "escalation":
            with contextlib.suppress(threading.BrokenBarrierError):
                self.barrier.wait(timeout=0.5)
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

    def evaluate(request_id: str) -> str | None:
        facts: HttpRequestFacts = make_facts(
            method="POST", path="/v1/items", request_id=request_id, config=config
        )
        return middleware.evaluate_http(facts).reason_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        codes = list(pool.map(evaluate, ["req-1", "req-2"]))
    assert codes == [reasons.APPROVAL_REQUIRED] * 2
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
