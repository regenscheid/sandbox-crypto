# SPDX-License-Identifier: MIT
"""Resource snapshots are scoped reads; one USER approval adds exactly ten million."""

from pathlib import Path

from sandbox_science.contracts import new_id
from sandbox_science.orchestrator.continuity.blueprints import Blueprint, Blueprints
from sandbox_science.orchestrator.continuity.records import WorkRecords
from sandbox_science.orchestrator.continuity.resources import ResourceRequests
from sandbox_science.orchestrator.delegation.records import Delegation, DelegationRecords
from sandbox_science.orchestrator.projects import ProjectStore
from sandbox_science.orchestrator.runstate import RunState

from .route_support import ClientFactory, context_of, error_of, origin, workspace
from .test_routes_recovery import CHILD, ROOT, RUN


def seed_resources(state_dir: Path) -> None:
    store = ProjectStore(state_dir)
    try:
        works = WorkRecords(store.control)
        works.admit("p1", "a", RUN)
        owner = works.admit("p1", "a", CHILD, parent=RUN)
        Blueprints(store.control).save(
            owner,
            Blueprint(
                context=context_of(state_dir),
                request={
                    "project": "p1",
                    "session": "a",
                    "task": "Verify finite-field claims",
                    "tokens": 20_000_000,
                    "parent": RUN,
                },
            ),
        )
        ResourceRequests(store.control).observe(
            owner, "tokens", used=16_000_000, total=20_000_000, revision=0
        )
        delegations = DelegationRecords(store.control)
        delegations.start(
            "p1",
            "a",
            Delegation(
                delegation_id=new_id("dlg"),
                parent=RUN,
                run=CHILD,
                kind="check",
                brief={"task": "Verify finite-field claims"},
                started_at=delegations.clock.now(),
            ),
        )
        works.interrupt("p1", "a", RUN)
        works.block("p1", "a", CHILD, "run_budget")
        delegations.configure("p1", max_token_grant=50_000_000)
        RunState(store.control).set_cost_limit("p1", 100)
    finally:
        store.close()


async def test_resource_views_are_authenticated_scoped_and_cursor_validated(
    aiohttp_client: ClientFactory, state_dir: Path, static_dir: Path
) -> None:
    seed_resources(state_dir)
    anonymous = await workspace(aiohttp_client, state_dir, static_dir, login=False)
    await error_of(await anonymous.get(ROOT + "/resources"), 401, "policy_denied")
    await error_of(await anonymous.get(ROOT + "/token-grants/" + CHILD), 401, "policy_denied")
    client = await workspace(aiohttp_client, state_dir, static_dir)
    response = await client.get(ROOT + "/resources")
    assert response.status == 200, await response.text()
    data = await response.json()
    assert data["requests"][0]["task"] == "Verify finite-field claims"
    assert data["requests"][0]["work_state"] == "blocked"
    assert response.headers["Cache-Control"] == "no-store"
    await error_of(await client.get(ROOT + "/resources?after=invalid"), 400, "invalid_request")
    await error_of(
        await client.get(ROOT.replace("/sessions/a/", "/sessions/missing/") + "/resources"),
        404,
        "not_found",
    )
    await error_of(
        await client.get(ROOT.replace("/projects/p1/", "/projects/p2/") + "/token-grants/" + CHILD),
        404,
        "not_found",
    )


async def test_increment_requires_origin_exact_amount_and_fresh_revision(
    aiohttp_client: ClientFactory, state_dir: Path, static_dir: Path
) -> None:
    seed_resources(state_dir)
    client = await workspace(aiohttp_client, state_dir, static_dir)
    status = await (await client.get(ROOT + "/token-grants/" + CHILD)).json()
    assert status["total_tokens"] == 20_000_000
    assert status["next_total_tokens"] == 30_000_000
    body = {
        "work": CHILD,
        "current": status["current"],
        "epoch": status["epoch"],
        "expected_revision": status["revision"],
        "total_tokens": status["next_total_tokens"],
        "reason": "Review confirms this branch is making useful progress.",
    }
    assert (await client.post(ROOT + "/token-grants/next", json=body)).status == 403
    anonymous = await workspace(aiohttp_client, state_dir, static_dir, login=False)
    await error_of(
        await anonymous.post(ROOT + "/token-grants/next", json=body, headers=origin(anonymous)),
        401,
        "policy_denied",
    )
    await error_of(
        await client.post(
            ROOT + "/token-grants/next",
            json={**body, "total_tokens": 40_000_000},
            headers=origin(client),
        ),
        400,
        "invalid_request",
    )
    granted = await client.post(ROOT + "/token-grants/next", json=body, headers=origin(client))
    assert granted.status == 200, await granted.text()
    decision = await granted.json()
    assert decision["revision"] == 1
    assert decision["operator"] == "user"
    repeated = await client.post(ROOT + "/token-grants/next", json=body, headers=origin(client))
    assert await repeated.json() == decision
    await error_of(
        await client.post(
            ROOT + "/token-grants/next",
            json={**body, "total_tokens": 40_000_000},
            headers=origin(client),
        ),
        409,
        "conflict",
    )
    page = await (await client.get(ROOT + "/resources")).json()
    assert page["requests"][0]["user_grant_revision"] == 1
