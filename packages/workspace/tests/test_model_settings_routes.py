# SPDX-License-Identifier: MIT
"""The three agent roles' model and effort settings over the workspace API."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from aiohttp.test_utils import TestClient

from sandbox_science.orchestrator.model_profiles import ModelProfile
from sandbox_science.orchestrator.projects import ProjectStore
from sandbox_science.workspace.app import create_app
from sandbox_science.workspace.project_settings import ModelConfiguration

from .test_reviews import TOKEN

pytestmark = pytest.mark.asyncio
ClientFactory = Callable[..., Awaitable[TestClient[Any, Any]]]


@pytest_asyncio.fixture
async def client(aiohttp_client: ClientFactory, state_dir: Path, static_dir: Path):
    configuration = ModelConfiguration(
        defaults={
            "main": ModelProfile(alias="sol", api="responses", reasoning_effort="max"),
            "delegated": ModelProfile(alias="sol", api="responses", reasoning_effort="max"),
            "reviewer": ModelProfile(alias="luna", api="responses", reasoning_effort="medium"),
        },
        available={"sol": "responses", "luna": "responses"},
    )
    found = await aiohttp_client(
        create_app(
            ProjectStore(state_dir, readonly=True),
            static_dir,
            login_token=TOKEN,
            model_configuration=configuration,
        )
    )
    assert (await found.get("/login", params={"token": TOKEN}, allow_redirects=False)).status == 303
    return found


async def test_read_and_change_agent_models(client) -> None:
    base = "/api/v0/projects/p1/models/settings"
    assert (await client.get("/api/v0/projects/missing/models/settings")).status == 404
    shown = await (await client.get(base)).json()
    assert shown["revision"] == 0
    assert shown["roles"]["reviewer"] == {
        "alias": "luna",
        "reasoning_effort": "medium",
    }
    roles = {
        "main": {"alias": "sol", "reasoning_effort": "high"},
        "delegated": {"alias": "luna", "reasoning_effort": "medium"},
        "reviewer": {"alias": "luna", "reasoning_effort": "low"},
    }
    value = {"expected": 0, "roles": roles, "operation_id": "models-1"}
    assert (await client.post(base, json=value)).status == 403
    headers = {"Origin": str(client.make_url("/")).rstrip("/")}
    changed = await client.post(base, json=value, headers=headers)
    assert changed.status == 200
    body = await changed.json()
    assert (body["revision"], body["roles"], body["replayed"]) == (1, roles, False)
    replay = await client.post(base, json=value, headers=headers)
    assert (await replay.json())["replayed"] is True
    stale = await client.post(base, json={**value, "operation_id": "models-2"}, headers=headers)
    assert stale.status == 409
    widened = {**roles, "main": {"alias": "outside", "reasoning_effort": "max"}}
    bad = {"expected": 1, "roles": widened, "operation_id": "models-3", "api": "chat"}
    assert (await client.post(base, json=bad, headers=headers)).status == 400
