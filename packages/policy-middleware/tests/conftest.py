# SPDX-License-Identifier: MIT
"""Shared fixtures: a policy store holding the example policy, bound to one sandbox."""

from collections.abc import Callable, Mapping
from pathlib import Path

import pytest

from sandbox_science.policy import HardFloor, LoadedPolicy, PoolCeiling
from sandbox_science.policy_middleware.evaluator import HttpRequestFacts, PolicyMiddleware
from sandbox_science.policy_middleware.store import PolicyStore

REPO_ROOT = Path(__file__).resolve().parents[3]
EXAMPLE_POLICY = REPO_ROOT / "examples" / "policy.yaml"
SANDBOX_ID = "sbx-0001"


class RecordingSink:
    """An event sink that keeps ``(kind, payload)`` records in memory, or fails on demand."""

    def __init__(self) -> None:
        self.events: list[tuple[str, Mapping[str, object]]] = []
        self.fail = False

    def record(self, kind: str, payload: Mapping[str, object]) -> None:
        if self.fail:
            raise OSError("test: the event store is unavailable")
        self.events.append((kind, payload))


FactsFactory = Callable[..., HttpRequestFacts]


@pytest.fixture
def sandbox_id() -> str:
    return SANDBOX_ID


@pytest.fixture
def store(tmp_path: Path) -> PolicyStore:
    return PolicyStore(tmp_path / "state")


@pytest.fixture
def example(store: PolicyStore) -> LoadedPolicy:
    loaded = store.add_policy(EXAMPLE_POLICY.read_bytes())
    store.bind(SANDBOX_ID, loaded.digest)
    return loaded


@pytest.fixture
def sink() -> RecordingSink:
    return RecordingSink()


@pytest.fixture
def middleware(store: PolicyStore, example: LoadedPolicy, sink: RecordingSink) -> PolicyMiddleware:
    del example
    return PolicyMiddleware(
        store, ceiling=PoolCeiling.unrestricted(), floor=HardFloor(), events=sink
    )


def config_for(policy: LoadedPolicy) -> dict[str, object]:
    """The middleware config the compiler writes for ``policy``."""
    return {"policy": {"name": policy.policy.metadata.name, "digest": policy.digest}}


@pytest.fixture
def policy_config() -> Callable[[LoadedPolicy], dict[str, object]]:
    """``config_for``, for test modules (importlib mode cannot import conftest)."""
    return config_for


@pytest.fixture
def make_facts(example: LoadedPolicy) -> FactsFactory:
    """Build request facts for the bound example policy; keyword arguments override."""

    def make(**overrides: object) -> HttpRequestFacts:
        fields: dict[str, object] = {
            "sandbox_id": SANDBOX_ID,
            "request_id": "req-1",
            "scheme": "https",
            "host": "example.org",
            "port": 443,
            "method": "GET",
            "path": "/",
            "query": "",
            "headers": (),
            "body": b"",
            "config": config_for(example),
        }
        fields.update(overrides)
        return HttpRequestFacts(**fields)  # pyright: ignore[reportArgumentType]

    return make
