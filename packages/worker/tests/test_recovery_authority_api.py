# SPDX-License-Identifier: MIT
"""Authenticated recovery control and operator grants never derive from actor text."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest_asyncio
from aiohttp.test_utils import TestClient

from sandbox_science.contracts import EnvironmentState, WriteFileResult
from sandbox_science.contracts.logs import configure_logging
from sandbox_science.contracts.recovery import RecoveryFence
from sandbox_science.worker.app import ENVIRONMENTS, create_app
from sandbox_science.worker.artifacts import Blobs
from sandbox_science.worker.backends import LocalBackend
from sandbox_science.worker.environments import Environments
from sandbox_science.worker.manager import ExecutionManager
from sandbox_science.worker.settings import ClientSettings, LocalBackendSettings, token_digest
from sandbox_science.worker.store import Store

from .harness import view
from .test_operation_lookup import CONTEXT, execution
from .test_recovery_fence import owner

POOL = "test-secret-recovery-pool-token-000000"
TOKENS = {
    name: f"test-secret-{name}-recovery-token-000000"
    for name in ("control", "ordinary", "operator")
}
ClientFactory = Callable[..., Awaitable[TestClient[Any, Any]]]


def auth(name: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {TOKENS[name]}"}


@pytest_asyncio.fixture
async def api(
    aiohttp_client: ClientFactory, tmp_path: Path
) -> AsyncIterator[tuple[TestClient[Any, Any], ExecutionManager]]:
    store = Store(tmp_path / "worker.sqlite3")
    manager = ExecutionManager(
        store,
        LocalBackend(LocalBackendSettings(), tmp_path),
        Blobs(tmp_path / "artifacts"),
        pool_id="test",
        max_artifact_size=1024,
        state_dir=tmp_path,
    )
    environment = SimpleNamespace(project_id="p", state=EnvironmentState.READY)
    environments = cast(
        Environments,
        SimpleNamespace(
            records=SimpleNamespace(get=lambda name: environment if name == "e" else None),
            deleted=[],
            required_deleted=[],
            required_deleting=[],
            confirmed_deleted=[],
            reusing=[],
        ),
    )
    clients = [
        ClientSettings(
            name=name,
            token_sha256=token_digest(token),
            projects=("p",),
            recovery_control=name == "control",
            operator=name == "operator",
        )
        for name, token in TOKENS.items()
    ]
    client = await aiohttp_client(create_app(manager, POOL, environments, clients))
    try:
        yield client, manager
    finally:
        await client.close()
        await manager.close()
        store.close()


async def test_only_explicit_control_grant_installs_scoped_durable_fences(api) -> None:
    client, manager = api
    body = {"project_id": "p", "session_id": "s", "epoch": 1}
    for name in ("ordinary", "operator"):
        assert (
            await client.post("/v1/recovery/fence", json=body, headers=auth(name))
        ).status == 403
    assert manager.store.recovery_fences.epoch("p", "s") is None
    assert (
        await client.post("/v1/recovery/fence", json=body, headers=auth("control"))
    ).status == 200
    body["project_id"] = "other"
    assert (
        await client.post("/v1/recovery/fence", json=body, headers=auth("control"))
    ).status == 403
    assert not view(manager).connected()


async def test_stale_unowned_actor_cannot_write_but_explicit_operator_can(api, monkeypatch) -> None:
    client, manager = api
    await client.post(
        "/v1/recovery/fence",
        json={"project_id": "p", "session_id": "s", "epoch": 2},
        headers=auth("control"),
    )
    writes = []

    async def write(request):
        writes.append(request)
        return WriteFileResult(sha256="ab" * 32, size=0)

    monkeypatch.setattr(view(manager).file_tools(), "write", write)
    body = {"context": CONTEXT.model_dump(mode="json"), "path": "x", "content_b64": ""}
    for context in (CONTEXT, owner(1), CONTEXT.model_copy(update={"actor": "user"})):
        body["context"] = context.model_dump(mode="json")
        response = await client.post("/v1/files/write", json=body, headers=auth("ordinary"))
        assert response.status == 403, await response.text()
    assert not writes
    body["context"] = owner(2).model_dump(mode="json")
    assert (await client.post("/v1/files/write", json=body, headers=auth("ordinary"))).status == 200
    body["context"] = CONTEXT.model_dump(mode="json")
    assert (await client.post("/v1/files/write", json=body, headers=auth("operator"))).status == 200
    assert len(writes) == 2
    assert not view(manager).connected()


async def test_operation_lookup_is_passive_scoped_and_does_not_resubmit(api, monkeypatch) -> None:
    client, manager = api
    manager.store.insert_execution(execution(), "ab" * 32)
    # Retiring the environment must not retire the durable reconciliation obligation.
    monkeypatch.setattr(client.server.app[ENVIRONMENTS].records, "get", lambda name: None)
    body = {"context": CONTEXT.model_dump(mode="json"), "operation_id": "original"}
    response = await client.post("/v1/operations/lookup", json=body, headers=auth("ordinary"))
    assert response.status == 200
    assert (await response.json())["execution"]["execution_id"] == "exe-original"
    body["context"] = CONTEXT.model_copy(update={"session_id": "other"}).model_dump(mode="json")
    response = await client.post("/v1/operations/lookup", json=body, headers=auth("ordinary"))
    assert await response.json() == {"execution": None, "provision": None}
    assert not view(manager).connected()


async def test_cancel_requires_supplied_current_context_or_authenticated_operator(api) -> None:
    client, manager = api
    manager.store.insert_execution(execution(), "ab" * 32)
    manager.store.recovery_fences.install(RecoveryFence(project_id="p", session_id="s", epoch=2))
    path = "/v1/executions/exe-original/cancel"
    assert (await client.post(path, headers=auth("ordinary"))).status == 403
    for context in (CONTEXT, owner(1), owner(2).model_copy(update={"session_id": "other"})):
        response = await client.post(
            path, json={"context": context.model_dump(mode="json")}, headers=auth("ordinary")
        )
        assert response.status in {403, 404}, await response.text()
    assert (
        await client.post(
            path, json={"context": owner(2).model_dump(mode="json")}, headers=auth("ordinary")
        )
    ).status == 200
    assert (await client.post(path, headers=auth("operator"))).status == 200
    assert not view(manager).connected()


async def test_a_request_logs_under_its_own_id_and_event(api, capsys) -> None:
    client, _ = api
    configure_logging("worker", fmt="json")
    body = {"project_id": "p", "session_id": "s", "epoch": 3}
    assert (
        await client.post("/v1/recovery/fence", json=body, headers=auth("control"))
    ).status == 200
    assert (
        await client.post("/v1/recovery/fence", json=body, headers=auth("control"))
    ).status == 200
    audits = [json.loads(line) for line in capsys.readouterr().err.splitlines()]
    audits = [line for line in audits if line.get("event") == "worker_request"]
    assert [(line["service"], line["status"]) for line in audits] == [("worker", 200)] * 2
    assert "by control: 200" in audits[0]["msg"]
    assert len({line["request"] for line in audits}) == 2  # an ID for each request
    assert "test-secret" not in json.dumps(audits)
