# SPDX-License-Identifier: MIT
"""New sessions from the workspace: in the project's shared environment, never over one
the project has, and only from the workspace's own origin (session_opening.py)."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from aiohttp.test_utils import TestClient

from sandbox_science.orchestrator.projects import ProjectStore
from sandbox_science.workspace.app import create_app

pytestmark = pytest.mark.asyncio

TOKEN = "test-secret-login-token-0123456789abcdef"
ClientFactory = Callable[..., Awaitable[TestClient[Any, Any]]]


@pytest_asyncio.fixture
async def client(
    aiohttp_client: ClientFactory, state_dir: Path, static_dir: Path
) -> TestClient[Any, Any]:
    app = create_app(ProjectStore(state_dir, readonly=True), static_dir, login_token=TOKEN)
    found = await aiohttp_client(app)
    response = await found.get("/login", params={"token": TOKEN}, allow_redirects=False)
    assert response.status == 303
    return found


async def post(
    client: TestClient[Any, Any], body: object, *, project: str = "p1", status: int = 201
) -> Any:
    response = await client.post(
        f"/api/v0/projects/{project}/sessions",
        json=body,
        headers={"Origin": str(client.make_url("/")).rstrip("/")},
    )
    assert response.status == status, await response.text()
    return await response.json()


async def test_a_new_session_opens_in_the_projects_environment(
    client: TestClient[Any, Any], state_dir: Path
) -> None:
    opened = await post(client, {"session_id": "snova-2"})
    assert (opened["session_id"], opened["dedicated"], opened["closed_at"]) == (
        "snova-2", False, None,
    )  # fmt: skip
    store = ProjectStore(state_dir, readonly=True)
    project = store.get("p1")
    store.close()
    assert opened["environment_id"] == project.default_environment_id
    assert "snova-2" in project.sessions
    listed = await (await client.get("/api/v0/projects/p1/sessions/snova-2")).json()
    assert listed["session"]["session_id"] == "snova-2"


async def test_a_name_the_project_has_is_a_conflict_open_or_closed(
    client: TestClient[Any, Any], state_dir: Path
) -> None:
    for taken in ("a", "b", "c"):  # open, closed, and in its own environment
        found = await post(client, {"session_id": taken}, status=409)
        assert "already has a session" in found["error"]["message"]
    store = ProjectStore(state_dir, readonly=True)
    assert store.get("p1").sessions["b"].closed_at is not None  # not reopened
    store.close()


async def test_bad_names_unknown_fields_projects_and_origins_are_refused(
    client: TestClient[Any, Any],
) -> None:
    for body in ({"session_id": "../x"}, {"session_id": ""}, {"session_id": 7}, {}):
        await post(client, body, status=400)
    await post(client, {"session_id": "s2", "dedicated": True}, status=400)
    await post(client, {"session_id": "s2"}, project="nowhere", status=404)
    for headers in ({}, {"Origin": "https://untrusted.invalid"}):
        response = await client.post(
            "/api/v0/projects/p1/sessions", json={"session_id": "s2"}, headers=headers
        )
        assert response.status == 403
