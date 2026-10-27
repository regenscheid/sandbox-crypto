# SPDX-License-Identifier: MIT
"""A project's inspected MCP traffic (``GET /v1/mcp-traffic``): the middleware's
``mcp-call`` and ``mcp-answer`` records joined, paged oldest first, for an operator client
only."""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
import pytest_asyncio
from aiohttp.test_utils import TestClient

from sandbox_science.contracts.mcp_traffic import MAX_TRAFFIC_CALLS, WorkerMcpTraffic
from sandbox_science.contracts.worker import PROTOCOL_HEADER, PROTOCOL_VERSION
from sandbox_science.event_store import EventStore
from sandbox_science.testing import FakeClock
from sandbox_science.worker.app import create_app
from sandbox_science.worker.artifacts import Blobs
from sandbox_science.worker.backends import LocalBackend
from sandbox_science.worker.environments import Environments
from sandbox_science.worker.manager import ExecutionManager
from sandbox_science.worker.mcp_traffic import TrafficQuery, read_traffic
from sandbox_science.worker.monitor import MonitorDesk
from sandbox_science.worker.permissions import Permissions
from sandbox_science.worker.settings import ClientSettings, LocalBackendSettings, token_digest
from sandbox_science.worker.store import Store

POOL = "test-secret-traffic-pool-token-0000"
TOKENS = {name: f"test-secret-traffic-{name}-0000" for name in ("workspace", "harness")}
NOW = datetime(2026, 10, 26, 12, 0, tzinfo=UTC)
SHA = "a" * 64
ClientFactory = Callable[..., Awaitable[TestClient[Any, Any]]]


class Clocked:
    def __init__(self, path: Path) -> None:
        self.at = NOW - timedelta(hours=1)
        self.store = EventStore(path, timeout=5, clock=lambda: self.at)

    def add(self, kind: str, payload: dict[str, object]) -> int:
        self.at += timedelta(seconds=1)
        return self.store.append(kind, "policy-middleware", payload).seq


def call(
    request: str,
    *,
    project: str = "p1",
    run: str = "main-1",
    tool: str = "search",
    outcome: str = "admitted",
) -> dict[str, object]:
    return {
        "project": project, "caller": "agent-runtime", "sandbox_id": "sbx-a", "session": "s-1",
        "run": run, "server": "papers", "method": "tools/call", "tool": tool,
        "request_id": request, "decision": {"action": "allow", "layer": "policy", "rule": "r",
        "effect": "read", "max_grant": "session"}, "reason_code": None, "outcome": outcome,
        "sizes": {"body": 10, "params": 8},
        "arguments": {"text": '{"name":"search"}', "truncated": False, "bytes": 17, "sha256": SHA},
    }  # fmt: skip


def answer(request: str, *, project: str = "p1", findings: int = 0) -> dict[str, object]:
    return {
        "project": project, "caller": "agent-runtime", "sandbox_id": "sbx-a", "session": "s-1",
        "run": "main-1", "server": "papers", "method": "tools/call", "tool": "search",
        "request_id": request, "status": 200, "content_type": "text/event-stream",
        "mode": "stream", "bytes": 40, "complete": True, "sha256": SHA,
        "excerpt": {"text": "found \u202e it", "truncated": False}, "note": None, "events": 1,
        "findings": [
            {"name": "you-are-now", "action": "alert", "offset": 3, "excerpt": "you are now"}
        ] * findings,
        "decision": {"action": "passed", "pattern": None, "reason_code": None},
    }  # fmt: skip


def test_calls_are_joined_to_their_answers_and_filtered(tmp_path: Path) -> None:
    events = Clocked(tmp_path / "events.db")
    events.add("mcp-call", call("r1"))
    events.add("mcp-call", call("r2", outcome="refused"))
    events.add("mcp-call", call("r3", project="p2"))
    events.add("mcp-call", call("r4", run="main-2", tool="fetch"))
    events.add("mcp-answer", answer("r1", findings=1))
    events.add("mcp-answer", answer("r3", project="p2"))
    found = read_traffic(events.store.path, TrafficQuery("p1"))
    assert [item.request_id for item in found.calls] == ["r1", "r2", "r4"]
    first = found.calls[0]
    assert first.arguments is not None
    assert first.arguments.sha256 == SHA
    assert first.answer is not None
    assert first.answer.excerpt == "found \u202e it"
    assert [finding.name for finding in first.answer.findings] == ["you-are-now"]
    assert found.calls[1].answer is None  # refused: no answer
    assert found.calls[1].decision.outcome == "refused"
    assert found.next_after is None
    by_run = read_traffic(events.store.path, TrafficQuery("p1", run="main-2"))
    assert [item.request_id for item in by_run.calls] == ["r4"]
    by_tool = read_traffic(events.store.path, TrafficQuery("p1", tool="fetch"))
    assert [item.request_id for item in by_tool.calls] == ["r4"]
    events.store.close()


def test_pages_follow_one_another_and_find_answers_after_a_full_page(tmp_path: Path) -> None:
    events = Clocked(tmp_path / "events.db")
    total = MAX_TRAFFIC_CALLS + 5
    for index in range(total):
        events.add("mcp-call", call(f"r{index}"))
    for index in range(total):
        events.add("mcp-answer", answer(f"r{index}"))
    first = read_traffic(events.store.path, TrafficQuery("p1"))
    assert len(first.calls) == MAX_TRAFFIC_CALLS
    assert all(item.answer is not None for item in first.calls)
    assert first.next_after == first.calls[-1].seq
    second = read_traffic(events.store.path, TrafficQuery("p1", after=first.next_after or 0))
    assert [item.request_id for item in second.calls] == [
        f"r{index}" for index in range(MAX_TRAFFIC_CALLS, total)
    ]
    assert all(item.answer is not None for item in second.calls)
    assert second.next_after is None
    events.store.close()


def test_a_missing_store_has_no_traffic(tmp_path: Path) -> None:
    found = read_traffic(tmp_path / "none.db", TrafficQuery("p1"))
    assert (found.calls, found.next_after) == ([], None)


def headers(name: str | None = None) -> dict[str, str]:
    token = POOL if name is None else TOKENS[name]
    return {"Authorization": f"Bearer {token}", PROTOCOL_HEADER: str(PROTOCOL_VERSION)}


@pytest_asyncio.fixture
async def api(
    aiohttp_client: ClientFactory, tmp_path: Path
) -> AsyncIterator[tuple[TestClient[Any, Any], Clocked]]:
    store = Store(tmp_path / "worker.sqlite3")
    manager = ExecutionManager(
        store, LocalBackend(LocalBackendSettings(), tmp_path), Blobs(tmp_path / "artifacts"),
        pool_id="test", max_artifact_size=1024, state_dir=tmp_path,
    )  # fmt: skip
    events = Clocked(tmp_path / "events.db")
    environments = cast(
        Environments,
        SimpleNamespace(
            deleted=[], required_deleted=[], required_deleting=[], confirmed_deleted=[],
            reusing=[], records=SimpleNamespace(listed=lambda projects: []),
        ),
    )  # fmt: skip
    clients = [
        ClientSettings(
            name=name, token_sha256=token_digest(token), projects=("p1",),
            operator=name == "workspace",
        )
        for name, token in TOKENS.items()
    ]  # fmt: skip
    figures = MonitorDesk(
        Permissions(events.store.path),
        lambda projects: [],
        declared_tier=None,
        measured_tier=lambda _: None,
        clock=FakeClock(NOW),
    )
    client = await aiohttp_client(
        create_app(manager, POOL, environments, clients, monitor_desk=figures)
    )
    try:
        yield client, events
    finally:
        assert figures.permissions is not None
        figures.permissions.close()
        events.store.close()
        store.close()


async def test_the_route_pages_an_operator_clients_project(
    api: tuple[TestClient[Any, Any], Clocked],
) -> None:
    client, events = api
    for index in range(MAX_TRAFFIC_CALLS + 1):
        events.add("mcp-call", call(f"r{index}"))
    answered = await client.get(
        "/v1/mcp-traffic?project=p1&session=s-1&run=main-1", headers=headers("workspace")
    )
    assert answered.status == 200, await answered.text()
    first = WorkerMcpTraffic.model_validate(await answered.json())
    assert len(first.calls) == MAX_TRAFFIC_CALLS
    assert first.next_after is not None
    answered = await client.get(
        f"/v1/mcp-traffic?project=p1&after={first.next_after}", headers=headers("workspace")
    )
    second = WorkerMcpTraffic.model_validate(await answered.json())
    assert [item.request_id for item in second.calls] == [f"r{MAX_TRAFFIC_CALLS}"]


@pytest.mark.parametrize(
    ("query", "who", "status"),
    [
        ("project=p1", "harness", 403),  # the run service's token reads no traffic
        ("project=p2", "workspace", 403),  # not this client's project
        ("session=s-1", "workspace", 400),
        ("project=p1&after=-1", "workspace", 400),
        ("project=p1&after=x", "workspace", 400),
        ("project=p1&extra=1", "workspace", 400),
        ("project=p1&run=..", "workspace", 400),
        ("project=p1&tool=a%20b", "workspace", 400),
        ("project=p1&project=p2", "workspace", 400),
    ],
)
async def test_the_route_refuses_other_clients_projects_and_malformed_queries(
    api: tuple[TestClient[Any, Any], Clocked], query: str, who: str, status: int
) -> None:
    client, _ = api
    answered = await client.get(f"/v1/mcp-traffic?{query}", headers=headers(who))
    assert answered.status == status, await answered.text()
