# SPDX-License-Identifier: MIT
"""The API table in docs/WORKSPACE.md lists exactly the routes the server registers."""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient

from sandbox_science.orchestrator.projects import ProjectStore
from sandbox_science.workspace.app import create_app

pytestmark = pytest.mark.asyncio

TOKEN = "test-secret-login-token-0123456789abcdef"
DOC = Path(__file__).resolve().parents[3] / "docs" / "WORKSPACE.md"
ROW = re.compile(r"^\| (GET|POST) \| `(/api/v0/[^`]*)` \|", re.MULTILINE)

AiohttpClient = Callable[[web.Application], Awaitable[TestClient[Any, Any]]]


def documented() -> set[tuple[str, str]]:
    rows = ROW.findall(DOC.read_text(encoding="utf-8"))
    assert len(rows) == len(set(rows)), "docs/WORKSPACE.md lists a route twice"
    return set(rows)


async def test_api_table_matches_routes(
    aiohttp_client: AiohttpClient, state_dir: Path, static_dir: Path
) -> None:
    client = await aiohttp_client(
        create_app(ProjectStore(state_dir, readonly=True), static_dir, login_token=TOKEN)
    )
    registered = {
        (route.method, route.resource.canonical)
        for route in client.app.router.routes()
        if route.method != "HEAD"
        and route.resource is not None
        and route.resource.canonical.startswith("/api/v0/")
    }
    listed = documented()
    assert registered - listed == set(), "add these routes to the API table in docs/WORKSPACE.md"
    assert listed - registered == set(), "docs/WORKSPACE.md's API table lists routes that are gone"
