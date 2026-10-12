# SPDX-License-Identifier: MIT
"""Scoped review previews, authenticated decisions, revision guards and retries."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from aiohttp.test_utils import TestClient

from sandbox_science.orchestrator.consistency.findings import Findings
from sandbox_science.orchestrator.consistency.store import ReviewStore
from sandbox_science.orchestrator.documents import DocumentStore, store_path
from sandbox_science.orchestrator.projects import ProjectStore
from sandbox_science.workspace.app import create_app

TOKEN = "review-fixture-synthetic-login-0123456789abcdef"  # noqa: S105
ClientFactory = Callable[..., Awaitable[TestClient[Any, Any]]]
pytestmark = pytest.mark.asyncio


@pytest.fixture
def review_data(state_dir: Path):
    documents = DocumentStore(store_path(state_dir))
    store = ReviewStore(documents)
    store.observe(
        "p1",
        "a",
        "claim",
        "transcript",
        {"run": "fixture"},
        {"content": "<script>untrusted()</script> There are 16."},
    )
    review = store.dispatch(
        "p1", "a", "terminal", author_run="fixture", owner="fixture", alias="scripted-reviewer"
    )
    assert review
    evidence = store.manifest("p1", "a", review)["evidence"][0]["evidence"]
    finding = Findings(store).create(
        "p1",
        "a",
        review,
        "fixture",
        {
            "severity": "fail",
            "category": "contradicting_value",
            "statement": "Claim lacks its asserted output.",
            "anchors": [{"evidence": evidence}],
            "operation_id": "fixture",
        },
    )
    store.finish("p1", "a", review, "fixture", "findings", "A retained finding.")
    documents.close()
    return review, evidence, finding["finding_id"]


@pytest_asyncio.fixture
async def client(aiohttp_client: ClientFactory, state_dir: Path, static_dir: Path):
    found = await aiohttp_client(create_app(ProjectStore(state_dir), static_dir, login_token=TOKEN))
    assert (await found.get("/login", params={"token": TOKEN}, allow_redirects=False)).status == 303
    return found


async def test_pinned_scope_reads_and_origin_guard(client, review_data) -> None:
    review, evidence, finding = review_data
    base = "/api/v0/projects/p1/sessions/a"
    result = await client.get(base + "/reviews")
    assert result.status == 200
    assert (await result.json())["reviews"][0]["alias"] == "scripted-reviewer"
    result = await client.get(base + f"/reviews/{review}/evidence/{evidence}")
    assert result.status == 200
    assert "<script>" in (await result.json())["text"]
    result = await client.get(
        f"/api/v0/projects/p1/sessions/b/reviews/{review}/evidence/{evidence}"
    )
    assert result.status == 404
    body = {
        "finding": finding,
        "expected": 1,
        "action": "dismiss",
        "explanation": "Explicit user decision.",
        "operation_id": "dismiss",
    }
    assert (await client.post(base + f"/findings/{finding}", json=body)).status == 403
    assert (
        await client.post(
            base + f"/findings/{finding}", json=body, headers={"Origin": "https://attacker.invalid"}
        )
    ).status == 403
    origin = str(client.make_url("/")).rstrip("/")
    result = await client.post(base + f"/findings/{finding}", json=body, headers={"Origin": origin})
    assert result.status == 200
    assert (await result.json())["status"] == "withdrawn"
    replay = await client.post(base + f"/findings/{finding}", json=body, headers={"Origin": origin})
    assert (await replay.json())["replayed"]
    history = await (await client.get(base + f"/findings/{finding}")).json()
    assert history["history"][-1]["actor"] == "user:user"
    stale = await client.post(
        base + f"/findings/{finding}",
        json={**body, "action": "reopen", "operation_id": "stale"},
        headers={"Origin": origin},
    )
    assert stale.status == 409


@pytest.mark.parametrize(
    "extra",
    [
        {"author": {"kind": "user"}},
        {"role": "main"},
        {"expected": True},
        {"operation_id": "x" * 129},
    ],
)
async def test_finding_writes_reject_invented_identity_and_malformed_fields(
    client, review_data, extra
) -> None:
    _, _, finding = review_data
    body = {
        "finding": finding,
        "expected": 1,
        "action": "dismiss",
        "explanation": "User decision.",
        "operation_id": "bad",
        **extra,
    }
    result = await client.post(
        f"/api/v0/projects/p1/sessions/a/findings/{finding}",
        json=body,
        headers={"Origin": str(client.make_url("/")).rstrip("/")},
    )
    assert result.status == 400


async def test_manual_request_is_queued_without_invented_wake(client, review_data) -> None:
    result = await client.post(
        "/api/v0/projects/p1/sessions/a/reviews",
        json={},
        headers={"Origin": str(client.make_url("/")).rstrip("/")},
    )
    assert result.status == 200
    value = await result.json()
    assert value["queued"]
    assert "Idle sessions do not start" in value["detail"]
