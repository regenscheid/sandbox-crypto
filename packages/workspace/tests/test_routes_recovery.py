# SPDX-License-Identifier: MIT
"""Recovery controls require operator login and explicit same-origin decisions."""

from __future__ import annotations

from pathlib import Path

from sandbox_science.orchestrator.continuity.models import ModelIntents
from sandbox_science.orchestrator.continuity.records import WorkRecords
from sandbox_science.orchestrator.projects import ProjectStore

from .route_support import ClientFactory, error_of, origin, workspace

ROOT = "/api/v0/projects/p1/sessions/a/recovery"
RUN = "20261017T000000000000Z-operator"


def seed(state_dir: Path) -> str:
    store = ProjectStore(state_dir)
    try:
        works = WorkRecords(store.control)
        owner = works.admit("p1", "a", RUN)
        identity = ModelIntents(store.control).begin(owner, kind="main", alias="fixture")
        works.interrupt("p1", "a", RUN)
        return identity
    finally:
        store.close()


def resolution(identity: str) -> dict[str, object]:
    return {
        "work": RUN,
        "current": RUN,
        "run": RUN,
        "epoch": 1,
        "kind": "model_usage",
        "identity": identity,
        "reason": "Operator checked provider records; result remains unknown.",
        "cost": 2,
    }


async def test_status_and_resolution_need_login_and_resolution_needs_origin(
    aiohttp_client: ClientFactory, state_dir: Path, static_dir: Path
) -> None:
    identity = seed(state_dir)
    anonymous = await workspace(aiohttp_client, state_dir, static_dir, login=False)
    await error_of(await anonymous.get(ROOT), 401, "policy_denied")
    await error_of(
        await anonymous.post(
            ROOT + "/resolve", json=resolution(identity), headers=origin(anonymous)
        ),
        401,
        "policy_denied",
    )
    client = await workspace(aiohttp_client, state_dir, static_dir)
    assert (await client.post(ROOT + "/resolve", json=resolution(identity))).status == 403
    response = await client.post(
        ROOT + "/resolve", json=resolution(identity), headers=origin(client)
    )
    assert response.status == 200, await response.text()
    decision = await response.json()
    assert decision["operator"] == "user"
    assert response.headers["Cache-Control"] == "no-store"
    repeated = await client.post(
        ROOT + "/resolve", json=resolution(identity), headers=origin(client)
    )
    assert await repeated.json() == decision


async def test_status_lists_work_and_original_unknown_id_with_bounded_scope(
    aiohttp_client: ClientFactory, state_dir: Path, static_dir: Path
) -> None:
    identity = seed(state_dir)
    client = await workspace(aiohttp_client, state_dir, static_dir)
    response = await client.get(ROOT)
    assert response.status == 200, await response.text()
    status = await response.json()
    assert status["works"][0]["current"] == RUN
    pending = await (await client.get(ROOT + "/works/" + RUN)).json()
    assert pending["models"][0]["identity"] == identity
    assert pending["models"][0]["run"] == RUN
    await error_of(await client.get(ROOT + "?limit=101"), 400, "invalid_request")
    await error_of(
        await client.get(ROOT.replace("/sessions/a/", "/sessions/unknown/")), 404, "not_found"
    )
    await error_of(
        await client.get(ROOT.replace("/projects/p1/", "/projects/p2/") + "/works/" + RUN),
        404,
        "not_found",
    )


async def test_resolution_validates_explicit_cost_and_stale_epoch(
    aiohttp_client: ClientFactory, state_dir: Path, static_dir: Path
) -> None:
    identity = seed(state_dir)
    client = await workspace(aiohttp_client, state_dir, static_dir)
    body = resolution(identity)
    del body["cost"]
    await error_of(
        await client.post(ROOT + "/resolve", json=body, headers=origin(client)),
        400,
        "invalid_request",
    )
    body = {**resolution(identity), "epoch": 2}
    await error_of(
        await client.post(ROOT + "/resolve", json=body, headers=origin(client)),
        409,
        "conflict",
    )
    pending = await (await client.get(ROOT + "/works/" + RUN)).json()
    assert pending["models"][0]["identity"] == identity
