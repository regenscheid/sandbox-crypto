# SPDX-License-Identifier: MIT
"""Network and package permission requests, listed and answered for the workspace (D97):
the worker's routes over the policy middleware's event store."""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest_asyncio
from aiohttp.test_utils import TestClient

from sandbox_science.contracts import EnvironmentInfo, EnvironmentState
from sandbox_science.contracts.worker import PROTOCOL_HEADER, PROTOCOL_VERSION
from sandbox_science.event_store import EventStore, StoreReader
from sandbox_science.policy.approvals import KINDS, Approvals, RequestKey, body_digest
from sandbox_science.testing import FakeClock
from sandbox_science.worker.app import create_app
from sandbox_science.worker.artifacts import Blobs
from sandbox_science.worker.backends import LocalBackend
from sandbox_science.worker.environments import Environments
from sandbox_science.worker.manager import ExecutionManager
from sandbox_science.worker.permission_requests import PermissionDesk
from sandbox_science.worker.permissions import Permissions
from sandbox_science.worker.settings import ClientSettings, LocalBackendSettings, token_digest
from sandbox_science.worker.store import Store

POOL = "test-secret-permissions-pool-token-0000"
TOKENS = {name: f"test-secret-permissions-{name}-0000" for name in ("workspace", "harness")}
DIGEST = "sha256:" + "b" * 64
NOW = datetime(2026, 10, 24, 12, 0, tzinfo=UTC)
ClientFactory = Callable[..., Awaitable[TestClient[Any, Any]]]


def environment(env: str, project: str, sandbox: str) -> EnvironmentInfo:
    return EnvironmentInfo(
        environment_id=env, project_id=project, profile="minimal", image="img", sandbox=env,
        sandbox_id=sandbox, policy=DIGEST, state=EnvironmentState.READY, created_at=NOW,
        updated_at=NOW,
    )  # fmt: skip


def escalate(store: EventStore, key: RequestKey, **extra: object) -> int:
    payload = {
        "target": key.payload(),
        "escalation": {"timeout_seconds": 1800, "on_timeout": "deny"},
        "rule": "writes",
        **extra,
    }
    return store.append("escalation", "policy-middleware", payload).seq


def headers(name: str | None = None) -> dict[str, str]:
    token = POOL if name is None else TOKENS[name]
    return {"Authorization": f"Bearer {token}", PROTOCOL_HEADER: str(PROTOCOL_VERSION)}


@pytest_asyncio.fixture
async def api(
    aiohttp_client: ClientFactory, tmp_path: Path
) -> AsyncIterator[tuple[TestClient[Any, Any], EventStore, Permissions]]:
    store = Store(tmp_path / "worker.sqlite3")
    manager = ExecutionManager(
        store, LocalBackend(LocalBackendSettings(), tmp_path), Blobs(tmp_path / "artifacts"),
        pool_id="test", max_artifact_size=1024, state_dir=tmp_path,
    )  # fmt: skip
    events = tmp_path / "events.db"
    sink = EventStore(events, timeout=5)
    permissions = Permissions(events)
    owned = [environment("env1", "p1", "sbx-1"), environment("env2", "p2", "sbx-2")]
    environments = cast(
        Environments,
        SimpleNamespace(
            deleted=[], required_deleted=[], required_deleting=[], confirmed_deleted=[],
            reusing=[], records=SimpleNamespace(listed=lambda _projects: owned),
        ),
    )  # fmt: skip
    desk = PermissionDesk(permissions, lambda: owned, FakeClock(NOW))
    clients = [
        ClientSettings(
            name=name, token_sha256=token_digest(token), projects=("p1",),
            operator=name == "workspace",
        )
        for name, token in TOKENS.items()
    ]  # fmt: skip
    client = await aiohttp_client(
        create_app(manager, POOL, environments, clients, permission_desk=desk)
    )
    try:
        yield client, sink, permissions
    finally:
        permissions.close()
        sink.close()
        store.close()


async def test_a_projects_network_and_package_requests_are_listed_with_their_cards(
    api: tuple[TestClient[Any, Any], EventStore, Permissions],
) -> None:
    client, sink, _ = api
    post = RequestKey.http("sbx-1", DIGEST, "POST", "api.example.org", 443, "/send", "a" * 64)
    summary = {
        "method": "POST", "host": "api.example.org", "port": 443, "path": "/send",
        "size": 12, "content_type": "application/json", "fields": {"to": "x@example.org"},
    }  # fmt: skip
    first = escalate(sink, post, caller_kind="code", summary=summary, max_grant="once")
    cache = RequestKey.http("sbx-1", DIGEST, "GET", "cache.local", 8090, "/pypi/f/x.whl")
    package = {"ecosystem": "pypi", "name": "fpylll", "channel": None}
    second = escalate(sink, cache, caller_kind="code", package=package)
    files = RequestKey.packages("sbx-1", DIGEST, "op-1", ["/conda/c/x.conda"])
    third = escalate(
        sink, files, files=[{"path": "/conda/c/x.conda", "ecosystem": "conda", "channel": "c"}],
        valid_until="2030-01-01T00:00:00+00:00", session_id="s-1", caller_kind="agent",
        purpose="the agent's words", asked="Conda: x",
    )  # fmt: skip
    escalate(sink, RequestKey.http("sbx-2", DIGEST, "GET", "other.org", 443, "/"))  # p2's
    listed = await client.get("/v1/permission-requests?project=p1", headers=headers("workspace"))
    assert listed.status == 200, await listed.text()
    requests = (await listed.json())["requests"]
    assert [item["seq"] for item in requests] == [first, second, third]
    network, download, install = requests
    assert (network["kind"], network["caller_kind"], network["max_grant"]) == (
        "network", "code", "once",
    )  # fmt: skip
    assert network["http"]["fields"] == {"to": "x@example.org"}
    assert network["session_id"] is None
    assert (download["kind"], download["packages"]["package"]) == ("packages", "fpylll")
    assert download["packages"]["sources"] == [{"ecosystem": "pypi", "channel": None}]
    assert (install["kind"], install["session_id"], install["caller_kind"]) == (
        "packages", "s-1", "agent",
    )  # fmt: skip
    assert install["packages"]["sources"] == [{"ecosystem": "conda", "channel": "c"}]
    assert install["max_grant"] == "project"
    # Another project's requests are not this client's to list.
    other = await client.get("/v1/permission-requests?project=p2", headers=headers("workspace"))
    assert other.status == 403


async def test_only_an_operator_client_lists_or_answers(
    api: tuple[TestClient[Any, Any], EventStore, Permissions],
) -> None:
    client, sink, _ = api
    seq = escalate(sink, RequestKey.http("sbx-1", DIGEST, "GET", "data.org", 443, "/a"))
    # The run service's token never answers its own sandbox's requests.
    listed = await client.get("/v1/permission-requests?project=p1", headers=headers("harness"))
    assert listed.status == 403
    answered = await client.post(
        f"/v1/permission-requests/{seq}/answer",
        json={"approve": True, "scope": "host"},
        headers=headers("harness"),
    )
    assert answered.status == 403


async def test_an_answer_is_recorded_for_the_middleware_and_bounded_by_kind(
    api: tuple[TestClient[Any, Any], EventStore, Permissions],
) -> None:
    client, sink, permissions = api
    key = RequestKey.http("sbx-1", DIGEST, "GET", "data.org", 443, "/a", body_digest(b""))
    seq = escalate(sink, key)
    cache = RequestKey.http("sbx-1", DIGEST, "GET", "cache.local", 8090, "/pypi/f/x.whl")
    download = escalate(sink, cache, package={"ecosystem": "pypi", "name": "x"})
    foreign = escalate(sink, RequestKey.http("sbx-2", DIGEST, "GET", "o.org", 443, "/"))
    path = "/v1/permission-requests/{}/answer"
    refused = await client.post(
        path.format(download), json={"approve": True, "scope": "host"}, headers=headers()
    )
    assert refused.status == 409  # a host answer would cover the whole package cache
    missing = await client.post(
        path.format(foreign), json={"approve": True, "scope": "host"}, headers=headers("workspace")
    )
    assert missing.status == 404  # another project's request is not found
    bad = await client.post(
        path.format(seq), json={"approve": True, "scope": "server"}, headers=headers("workspace")
    )
    assert bad.status == 400
    answered = await client.post(
        path.format(seq),
        json={"approve": True, "scope": "host", "hours": 1},
        headers=headers("workspace"),
    )
    assert answered.status == 201, await answered.text()
    body = await answered.json()
    assert (body["seq"], body["kind"]) == (seq, "approval")
    with StoreReader(sink.path) as reader:
        records = list(reader.records(kinds=KINDS))
    assert (records[-1].kind, records[-1].source) == ("approval", "workspace")
    approvals = Approvals()
    approvals.load(records)
    other_path = RequestKey.http("sbx-1", DIGEST, "GET", "data.org", 443, "/b")
    assert approvals.answer(other_path, NOW) is not None  # the middleware lets it through
    listed = await client.get("/v1/permission-requests?project=p1", headers=headers("workspace"))
    assert seq not in [item["seq"] for item in (await listed.json())["requests"]]
    del permissions


ARGS = "c" * 64
LONG = {"timeout_seconds": 10**9, "on_timeout": "deny"}
"""Terms that keep a request waiting whatever the test clock says."""


def held_call(sink: EventStore, project: str = "p1", tool: str = "send", **extra: object) -> int:
    """An MCP call the middleware holds (D98), recorded as the middleware records one."""
    key = RequestKey.mcp(project, "agent-runtime", DIGEST, "papers", "tools/call", tool, ARGS)
    return escalate(
        sink, key, escalation=LONG, project=project, caller="agent-runtime", session="s-1",
        run="main-1", server="papers", method="tools/call", tool=tool, effect="communicate",
        max_grant="session", caller_kind="agent", summary={"recipient": "a@example.org"},
    )  # fmt: skip


async def test_a_held_direct_mcp_call_is_listed_and_answered_for_its_call(
    api: tuple[TestClient[Any, Any], EventStore, Permissions],
) -> None:
    client, sink, _ = api
    seq = held_call(sink)
    held_call(sink, project="p2")  # another project's
    listed = await client.get("/v1/permission-requests?project=p1", headers=headers("workspace"))
    (item,) = (await listed.json())["requests"]
    assert (item["seq"], item["kind"], item["environment_id"]) == (seq, "mcp", None)
    assert (item["session_id"], item["caller_kind"], item["max_grant"]) == (
        "s-1",
        "agent",
        "session",
    )
    assert item["run"] == "main-1"  # the run the middleware recorded (DA-4)
    assert item["mcp"] == {
        "server": "papers", "method": "tools/call", "tool": "send", "effect": "communicate",
        "summary": {"recipient": "a@example.org"},
    }  # fmt: skip
    path = f"/v1/permission-requests/{seq}/answer"
    wrong = await client.post(
        path, json={"approve": True, "scope": "host"}, headers=headers("workspace")
    )
    assert wrong.status == 409  # a call is answered for itself only
    answered = await client.post(
        path, json={"approve": True, "scope": "call", "hours": 0.25}, headers=headers("workspace")
    )
    assert answered.status == 201, await answered.text()
    with StoreReader(sink.path) as reader:
        approvals = Approvals()
        approvals.load(list(reader.records(kinds=KINDS)))
    key = RequestKey.mcp("p1", "agent-runtime", DIGEST, "papers", "tools/call", "send", ARGS)
    found = approvals.answer(key, NOW)
    assert found is not None
    assert found.kind == "approval"


async def test_a_refusal_is_confirmed_only_for_its_own_call_and_project(
    api: tuple[TestClient[Any, Any], EventStore, Permissions],
) -> None:
    client, sink, _ = api
    seq = held_call(sink)
    network = escalate(sink, RequestKey.http("sbx-1", DIGEST, "GET", "data.org", 443, "/a"))
    call = {
        "reason": "approval_required", "project_id": "p1", "server": "papers",
        "method": "tools/call", "tool": "send", "arguments": ARGS,
    }  # fmt: skip

    async def confirmed(number: int, body: Mapping[str, object], who: str = "harness") -> object:
        response = await client.post(
            f"/v1/permission-requests/{number}/confirm", json=body, headers=headers(who)
        )
        assert response.status == 200, await response.text()
        return (await response.json())["confirmed"]

    # The run service's own token may ask, for its projects; the answer is yes or no.
    assert await confirmed(seq, call) is True
    assert await confirmed(seq, {**call, "arguments": "d" * 64}) is False  # another call's
    assert await confirmed(seq, {**call, "tool": "other"}) is False
    assert await confirmed(seq, {**call, "reason": "approval_denied"}) is False  # not so
    assert await confirmed(network, call) is False  # not an MCP request
    assert await confirmed(999, call) is False  # none
    other = await client.post(
        f"/v1/permission-requests/{seq}/confirm",
        json={**call, "project_id": "p2"},
        headers=headers("harness"),
    )
    assert other.status == 403  # not this client's project
    malformed = await client.post(
        f"/v1/permission-requests/{seq}/confirm",
        json={**call, "arguments": "not-a-digest"},
        headers=headers("harness"),
    )
    assert malformed.status == 400


def hold(store: EventStore, key: RequestKey, caller: str) -> int:
    payload = {
        "target": key.payload(),
        "escalation": {"timeout_seconds": 30 * 86400, "on_timeout": "deny"},
        "rule": "detect.same-url",
        "reason": "101 calls to a.org within 600 s",
        "caller_kind": caller,
        "policy": {"name": "p", "digest": DIGEST},
        "max_grant": "once",
        "hold": {
            "rule": "same-url", "count": 101, "threshold": 100, "window_seconds": 600,
            "host": "a.org", "requests": [{"method": "GET", "host": "a.org", "target": "/x"}],
        },
    }  # fmt: skip
    return store.append("escalation", "policy-middleware", payload).seq


async def test_holds_of_a_sessions_calls_are_listed_and_cleared_or_stopped(
    api: tuple[TestClient[Any, Any], EventStore, Permissions],
) -> None:
    # D103: research code's hold is its sandbox's, whose environment names the project;
    # the agent's is its session's, which its key names.
    client, sink, _ = api
    stamp = "2026-10-24T12:00:00.000000+00:00"
    code = hold(sink, RequestKey.hold("", "sbx-1", "", "same-url", stamp), "code")
    agent = hold(sink, RequestKey.hold("p1", "sbx-a", "s-1", "same-url", stamp), "agent")
    hold(sink, RequestKey.hold("", "sbx-2", "", "same-url", stamp), "code")  # p2's
    hold(sink, RequestKey.hold("p2", "sbx-b", "s-9", "same-url", stamp), "agent")  # p2's
    listed = await client.get("/v1/permission-requests?project=p1", headers=headers("workspace"))
    requests = (await listed.json())["requests"]
    assert [(item["seq"], item["kind"]) for item in requests] == [(code, "hold"), (agent, "hold")]
    first, second = requests
    assert (first["session_id"], first["caller_kind"], second["session_id"]) == (
        None, "code", "s-1",
    )  # fmt: skip
    assert first["hold"]["requests"] == [{"method": "GET", "host": "a.org", "target": "/x"}]
    assert (first["max_grant"], first["timed_out"], first["rule"]) == (
        "once", False, "detect.same-url",
    )  # fmt: skip
    path = "/v1/permission-requests/{}/answer"
    wrong = await client.post(
        path.format(code), json={"approve": True, "scope": "host"}, headers=headers("workspace")
    )
    assert wrong.status == 409
    for seq, approve in ((code, True), (agent, False)):
        answered = await client.post(
            path.format(seq),
            json={"approve": approve, "scope": "session"},
            headers=headers("workspace"),
        )
        assert answered.status == 201, await answered.text()
    with StoreReader(sink.path) as reader:
        answers = [r.kind for r in reader.records(kinds=KINDS) if r.kind != "escalation"]
    assert answers == ["approval", "denial"]
    left = await client.get("/v1/permission-requests?project=p1", headers=headers("workspace"))
    assert (await left.json())["requests"] == []
