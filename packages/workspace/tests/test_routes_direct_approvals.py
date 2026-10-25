# SPDX-License-Identifier: MIT
"""Cards for MCP calls the policy middleware holds (D98), end to end: the real middleware
escalates an agent's direct call into its event store, the real worker lists it and takes
the answer, and the workspace shows and answers it as it does a call the MCP proxy holds.
A grant given there answers the middleware's later calls it covers, through the relay."""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from aiohttp.test_utils import TestClient, TestServer

from sandbox_science.contracts import new_id
from sandbox_science.event_store import EventStore
from sandbox_science.orchestrator.delegation.records import Delegation, DelegationRecords
from sandbox_science.orchestrator.projects import ProjectStore
from sandbox_science.orchestrator.runstate import RunState
from sandbox_science.policy import HardFloor, LoadedPolicy, PoolCeiling
from sandbox_science.policy_middleware.cli import _read_records
from sandbox_science.policy_middleware.evaluator import HttpRequestFacts, PolicyMiddleware
from sandbox_science.policy_middleware.events import StoreSink
from sandbox_science.policy_middleware.mcp import ToolListings
from sandbox_science.policy_middleware.store import PolicyStore
from sandbox_science.worker.app import create_app
from sandbox_science.worker.artifacts import Blobs
from sandbox_science.worker.backends import LocalBackend
from sandbox_science.worker.environments import Environments
from sandbox_science.worker.manager import ExecutionManager
from sandbox_science.worker.permission_requests import PermissionDesk
from sandbox_science.worker.permissions import Permissions
from sandbox_science.worker.settings import ClientSettings, LocalBackendSettings, token_digest
from sandbox_science.worker.store import Store
from sandbox_science.workspace import approvals

from .route_support import WORKER_TOKEN, workspace

ClientFactory = Callable[..., Awaitable[TestClient[Any, Any]]]
ROOT = Path(__file__).resolve().parents[3]
SERVER = "crypto-research-mcp"
WRITE_TOOL = "firecrawl-mcp_firecrawl_monitor_delete"
ANSWER = "/api/v0/projects/p1/permission-requests/{}/answer"
POOL = "test-secret-direct-workspace-pool-0000"


@dataclass
class Middleware:
    """The real policy middleware, deciding the agent's direct calls of project p1."""

    middleware: PolicyMiddleware
    config: dict[str, object]
    calls: int = 0

    def call(self, arguments: dict[str, object], session: str = "a", run: str = "main-1") -> str:
        """One tools/call of the write tool: '' if admitted, else the reason code."""
        self.calls += 1
        body = {
            "jsonrpc": "2.0",
            "id": self.calls,
            "method": "tools/call",
            "params": {"name": WRITE_TOOL, "arguments": arguments},
        }
        facts = HttpRequestFacts(
            sandbox_id="sbx-agent",
            request_id=f"req-{self.calls}",
            scheme="https",
            host="mcp.example.invalid",
            port=443,
            method="POST",
            path="/crypto-research",
            query="",
            headers=(
                ("content-type", "application/json"),
                ("sandbox-science-session", session),
                ("sandbox-science-run", run),
            ),
            body=json.dumps(body, sort_keys=True).encode(),
            config=self.config,
        )
        return self.middleware.evaluate_http(facts).reason_code


@pytest.fixture
def events(tmp_path: Path) -> Iterator[EventStore]:
    with EventStore(tmp_path / "middleware" / "events.db", timeout=0.5) as store:
        yield store


@pytest.fixture
def middleware(tmp_path: Path, events: EventStore) -> Middleware:
    store = PolicyStore(tmp_path / "middleware")
    loaded: LoadedPolicy = store.add_policy((ROOT / "examples" / "policy.yaml").read_bytes())
    store.bind("sbx-agent", loaded.digest)
    decider = PolicyMiddleware(
        store,
        ceiling=PoolCeiling.unrestricted(),
        floor=HardFloor(),
        events=StoreSink(events),
        requests=partial(_read_records, events.path),
        listings=ToolListings(),
    )
    config: dict[str, object] = {
        "policy": {"name": loaded.policy.metadata.name, "digest": loaded.digest},
        "mcp": {"project": "p1", "caller": "agent-runtime"},
    }
    return Middleware(decider, config)


@pytest.fixture
async def worker_url(tmp_path: Path, events: EventStore) -> AsyncIterator[str]:
    """The real worker's permission routes over the middleware's store, for the
    workspace's operator client."""
    store = Store(tmp_path / "worker.sqlite3")
    manager = ExecutionManager(
        store, LocalBackend(LocalBackendSettings(), tmp_path), Blobs(tmp_path / "artifacts"),
        pool_id="test", max_artifact_size=1024, state_dir=tmp_path,
    )  # fmt: skip
    environments = cast(
        Environments,
        SimpleNamespace(
            deleted=[], required_deleted=[], required_deleting=[], confirmed_deleted=[],
            reusing=[], records=SimpleNamespace(listed=lambda _projects: []),
        ),
    )  # fmt: skip
    permissions = Permissions(events.path)
    clients = [
        ClientSettings(
            name="workspace", token_sha256=token_digest(WORKER_TOKEN), projects=("p1",),
            operator=True,
        )
    ]  # fmt: skip
    app = create_app(
        manager, POOL, environments, clients, permission_desk=PermissionDesk(permissions, list)
    )
    server = TestServer(app, host="127.0.0.1")
    await server.start_server()
    try:
        yield str(server.make_url("/")).rstrip("/")
    finally:
        await server.close()
        permissions.close()
        store.close()


@pytest.fixture(autouse=True)
def quick_relay(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(approvals, "RELAY_SECONDS", 0.05)


def request_of(code: str) -> int:
    match = re.fullmatch(r"approval_required_r([0-9]+)", code)
    assert match is not None, code
    return int(match.group(1))


async def shown(client: TestClient[Any, Any], session: str = "a") -> dict[int, Any]:
    response = await client.get(f"/api/v0/projects/p1/sessions/{session}/permission-requests")
    assert response.status == 200, await response.text()
    return {item["seq"]: item for item in (await response.json())["requests"]}


async def post(client: TestClient[Any, Any], path: str, body: object, status: int = 200) -> Any:
    response = await client.post(
        path, json=body, headers={"Origin": str(client.make_url("/")).rstrip("/")}
    )
    assert response.status == status, await response.text()
    return await response.json()


async def until(condition: Callable[[], bool]) -> None:
    for _ in range(200):
        if condition():
            return
        await asyncio.sleep(0.05)
    raise AssertionError("the condition never held")


async def test_a_held_direct_call_is_a_card_answered_like_a_proxy_call(
    aiohttp_client: ClientFactory,
    state_dir: Path,
    static_dir: Path,
    middleware: Middleware,
    worker_url: str,
) -> None:
    client = await workspace(aiohttp_client, state_dir, static_dir, worker_url=worker_url)
    seq = request_of(middleware.call({"id": "monitor-1"}))
    card = (await shown(client))[seq]
    # The fields a proxy-held call's card has, from the middleware's own record.
    assert (card["kind"], card["source"], card["server"], card["tool"], card["method"]) == (
        "mcp", "worker", SERVER, WRITE_TOOL, "tools/call",
    )  # fmt: skip
    assert (card["caller_kind"], card["effect"], card["max_grant"]) == ("agent", "write", "project")
    assert card["summary"] == {"title": "monitor-1"}
    assert (card["session"], card["run"], card["child"]) == ("a", "main-1", None)
    assert card["scopes"] == ["once", "session", "project"]  # within write's ceiling
    assert seq not in await shown(client, "c")  # another session's card list
    # Answered through the same route; the middleware lets the call through on its retry.
    answer = {"decision": "project", "kind": "mcp", "source": "worker"}
    result = await post(client, ANSWER.format(seq), answer)
    assert (result["source"], result["kind"]) == ("worker", "mcp")
    [grant] = result["granted"]
    assert (grant["kind"], grant["server"], grant["effect"]) == ("mcp", SERVER, "write")
    assert middleware.call({"id": "monitor-1"}) == ""
    assert seq not in await shown(client)
    # The grant covers a later call of that class on that server: the relay answers it.
    later = request_of(middleware.call({"id": "monitor-2"}))
    await until(lambda: middleware.call({"id": "monitor-2"}) == "")
    assert later not in await shown(client)


async def test_a_direct_call_is_never_answered_through_the_proxy_or_beyond_its_ceiling(
    aiohttp_client: ClientFactory,
    state_dir: Path,
    static_dir: Path,
    middleware: Middleware,
    worker_url: str,
) -> None:
    client = await workspace(aiohttp_client, state_dir, static_dir, worker_url=worker_url)
    seq = request_of(middleware.call({"id": "monitor-3"}))
    # Without its source, an MCP call is the proxy's, which this workspace does not answer.
    await post(client, ANSWER.format(seq), {"decision": "once", "kind": "mcp"}, 404)
    # A denial refuses the call's retry.
    await post(client, ANSWER.format(seq), {"decision": "deny", "kind": "mcp", "source": "worker"})
    assert middleware.call({"id": "monitor-3"}) == f"approval_denied_r{seq}"
    # The network kind of the same number is not this request.
    other = request_of(middleware.call({"id": "monitor-4"}))
    body = {"decision": "once", "kind": "network", "source": "worker"}
    await post(client, ANSWER.format(other), body, 409)


async def test_a_delegated_runs_direct_call_names_the_child_and_its_brief(
    aiohttp_client: ClientFactory,
    state_dir: Path,
    static_dir: Path,
    middleware: Middleware,
    worker_url: str,
) -> None:
    """DA-9 on the direct path: the card names the run, and for a delegated run the child
    and its parent's brief; a main run's names none, and nor does a run the session's
    tree does not list."""
    delegation = new_id("dlg")
    store = ProjectStore(state_dir)
    try:
        DelegationRecords(store.control).start(
            "p1",
            "a",
            Delegation(
                delegation_id=delegation, parent="main-1", run="child-1", kind="task",
                brief={"task": "Find the SNOVA parameter sets."},
                started_at=datetime.now(UTC),
            ),
        )  # fmt: skip
        # Another session's delegation of the same run name is not this session's child.
        DelegationRecords(store.control).start(
            "p1",
            "c",
            Delegation(
                delegation_id=new_id("dlg"), parent="main-2", run="stranger-1", kind="check",
                brief={"check": {"entry": "e1"}, "note": "Recheck."},
                started_at=datetime.now(UTC),
            ),
        )  # fmt: skip
    finally:
        store.close()
    client = await workspace(aiohttp_client, state_dir, static_dir, worker_url=worker_url)
    main = request_of(middleware.call({"id": "m-1"}))
    child = request_of(middleware.call({"id": "m-2"}, run="child-1"))
    unlisted = request_of(middleware.call({"id": "m-3"}, run="lost-9"))
    elsewhere = request_of(middleware.call({"id": "m-4"}, run="stranger-1"))
    cards = await shown(client)
    assert cards[main]["child"] is None
    assert cards[child]["run"] == "child-1"
    assert cards[child]["child"] == {
        "delegation_id": delegation,
        "run": "child-1",
        "kind": "task",
        "brief": "Find the SNOVA parameter sets.",
        "words": "",
    }
    assert (cards[unlisted]["run"], cards[unlisted]["child"]) == ("lost-9", None)
    assert cards[elsewhere]["child"] is None  # the delegation is another session's
    # The child's card is answered as any direct call's is.
    body = {"decision": "once", "kind": "mcp", "source": "worker"}
    await post(client, ANSWER.format(child), body)
    assert middleware.call({"id": "m-2"}, run="child-1") == ""


READ_TOOL = "attack-catalog-mcp_search_attacks"


@pytest.fixture
def hammered(tmp_path: Path, events: EventStore) -> Middleware:
    """The real middleware under a policy whose same-URL threshold is two calls (D103)."""
    store = PolicyStore(tmp_path / "middleware")
    text = (ROOT / "examples" / "policy.yaml").read_text() + "detection:\n  same_url: 2\n"
    loaded = store.add_policy(text.encode())
    store.bind("sbx-agent", loaded.digest)
    decider = PolicyMiddleware(
        store,
        ceiling=PoolCeiling.unrestricted(),
        floor=HardFloor(),
        events=StoreSink(events),
        requests=partial(_read_records, events.path),
        listings=ToolListings(),
    )
    config: dict[str, object] = {
        "policy": {"name": loaded.policy.metadata.name, "digest": loaded.digest},
        "mcp": {"project": "p1", "caller": "agent-runtime"},
    }
    return Middleware(decider, config)


def search(middleware: Middleware) -> str:
    """One tools/call of a read tool, always the same: '' if admitted, else the code."""
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call"}
    body["params"] = {"name": READ_TOOL, "arguments": {"query": "SNOVA"}}
    middleware.calls += 1
    facts = HttpRequestFacts(
        sandbox_id="sbx-agent", request_id=f"s-{middleware.calls}", scheme="https",
        host="mcp.example.invalid", port=443, method="POST", path="/crypto-research",
        query="", body=json.dumps(body).encode(), config=middleware.config,
        headers=(("content-type", "application/json"), ("sandbox-science-session", "a")),
    )  # fmt: skip
    return middleware.middleware.evaluate_http(facts).reason_code


def held_seq(code: str) -> int:
    match = re.fullmatch(r"session_held_r([0-9]+)", code)
    assert match is not None, code
    return int(match.group(1))


async def test_a_hold_is_a_card_the_user_clears_or_stops(
    aiohttp_client: ClientFactory,
    state_dir: Path,
    static_dir: Path,
    hammered: Middleware,
    worker_url: str,
) -> None:
    client = await workspace(aiohttp_client, state_dir, static_dir, worker_url=worker_url)
    assert search(hammered) == ""
    assert search(hammered) == ""
    seq = held_seq(search(hammered))
    card = (await shown(client))[seq]
    assert (card["kind"], card["source"], card["session"], card["scopes"]) == (
        "hold", "worker", "a", [],
    )  # fmt: skip
    assert (card["hold"]["rule"], card["hold"]["count"], card["rule"]) == (
        "same-url", 3, "detect.same-url",
    )  # fmt: skip
    assert seq not in await shown(client, "c")  # the agent's hold is its session's
    # A hold takes clear or stop, and nothing else does.
    await post(client, ANSWER.format(seq), {"decision": "once", "kind": "hold"}, 400)
    await post(client, ANSWER.format(seq), {"decision": "project", "kind": "hold"}, 400)
    result = await post(client, ANSWER.format(seq), {"decision": "clear", "kind": "hold"})
    assert (result["decision"], result["kind"]) == ("clear", "hold")
    await until(lambda: search(hammered) == "")  # cleared: the counts start again
    # Held again, and stopped: the session's live run is cancelled.
    state = RunState(ProjectStore(state_dir).control)
    state.acquire("p1", "a", "run-x", "holder")
    assert search(hammered) == ""
    again = held_seq(search(hammered))
    assert again > seq
    await post(client, ANSWER.format(again), {"decision": "stop", "kind": "hold"})
    assert state.cancelled("p1", "a", "run-x")
    assert again not in await shown(client)
