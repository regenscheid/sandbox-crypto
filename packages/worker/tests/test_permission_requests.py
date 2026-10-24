# SPDX-License-Identifier: MIT
"""Network and package permission requests, listed and answered for the workspace (D97):
the worker's routes over the policy middleware's event store."""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
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
