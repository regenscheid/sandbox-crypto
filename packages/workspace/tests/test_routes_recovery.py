# SPDX-License-Identifier: MIT
"""Recovery controls require operator login and explicit same-origin decisions."""

from __future__ import annotations

from pathlib import Path

from pydantic import TypeAdapter

from sandbox_science.contracts import new_id
from sandbox_science.orchestrator.continuity.blueprints import Blueprint, Blueprints
from sandbox_science.orchestrator.continuity.models import ModelIntents
from sandbox_science.orchestrator.continuity.records import WorkRecords
from sandbox_science.orchestrator.delegation.records import Delegation, DelegationRecords
from sandbox_science.orchestrator.harness import RunRequest
from sandbox_science.orchestrator.projects import ProjectStore
from sandbox_science.orchestrator.runstate import RunState

from .authentik_support import ADMINS, USERS, Authentik, as_user
from .route_support import ClientFactory, context_of, error_of, origin, workspace

ROOT = "/api/v0/projects/p1/sessions/a/recovery"
RUN = "20261017T000000000000Z-operator"
CHILD = "20261017T000001000000Z-check"


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


def seed_child(state_dir: Path) -> None:
    store = ProjectStore(state_dir)
    try:
        delegation_id = new_id("dlg")
        works = WorkRecords(store.control)
        works.admit("p1", "a", RUN)
        owner = works.admit("p1", "a", CHILD, parent=RUN)
        blueprints = Blueprints(store.control)
        blueprints.save(
            owner,
            Blueprint(
                context=context_of(state_dir),
                request=TypeAdapter(RunRequest).dump_python(
                    RunRequest(
                        "p1",
                        "a",
                        "Check an evidence item",
                        parent=RUN,
                        delegation=delegation_id,
                        tokens=100_000,
                        alias="scripted",
                    ),
                    mode="json",
                    exclude={"history", "stop_by"},
                ),
            ),
        )
        delegations = DelegationRecords(store.control)
        delegations.start(
            "p1",
            "a",
            Delegation(
                delegation_id=delegation_id,
                parent=RUN,
                run=CHILD,
                kind="check",
                brief={"task": "Check an evidence item"},
                started_at=delegations.clock.now(),
            ),
        )
        works.interrupt("p1", "a", RUN)
        works.block("p1", "a", CHILD, "run_budget")
        delegations.configure("p1", check_tokens=500_000)
        RunState(store.control).set_cost_limit("p1", 100)
    finally:
        store.close()


async def test_tree_resume_does_not_globally_release_held_messages(
    aiohttp_client: ClientFactory, state_dir: Path, static_dir: Path
) -> None:
    seed(state_dir)
    store = ProjectStore(state_dir)
    try:
        state = RunState(store.control)
        state.post("p1", "a", "unrelated", "Held independent input", author="user")
        state.hold("p1", "a", ["unrelated"])
        client = await workspace(aiohttp_client, state_dir, static_dir)
        response = await client.post(
            ROOT.removesuffix("/recovery") + "/resume", json={}, headers=origin(client)
        )
        assert response.status == 200
        body = await response.json()
        assert body["resume_request"]
        assert body["resumed"] == 0  # original inputs are admitted by the durable claim
        assert state.messages("p1", "a")[0].state == "held"
    finally:
        store.close()


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


async def test_the_user_token_grant_route_is_gone(
    aiohttp_client: ClientFactory, state_dir: Path, static_dir: Path
) -> None:
    """D102: a child's tokens are a soft cap that checks in with its parent."""
    seed_child(state_dir)
    client = await workspace(aiohttp_client, state_dir, static_dir)
    body = {"work": CHILD, "current": CHILD, "epoch": 1, "expected_revision": 0}
    response = await client.post(ROOT + "/token-grants", json=body, headers=origin(client))
    assert response.status in (404, 405)  # no route writes a grant


async def test_with_an_admin_group_resolving_is_the_admins(
    aiohttp_client: ClientFactory, state_dir: Path, static_dir: Path
) -> None:
    """D104: a recovery resolution is an operator's: with an admin group, a member is
    refused and an admin's resolution names them, not the operator."""
    identity = seed(state_dir)
    authentik = Authentik()
    client = await workspace(
        aiohttp_client, state_dir, static_dir, login=False, forward_auth=authentik.verifier(ADMINS)
    )
    member = {**as_user(authentik.token("bob")), **origin(client)}
    admin = {**as_user(authentik.token("carol", (USERS, ADMINS))), **origin(client)}
    refused = await client.post(ROOT + "/resolve", json=resolution(identity), headers=member)
    await error_of(refused, 403, "policy_denied")
    response = await client.post(ROOT + "/resolve", json=resolution(identity), headers=admin)
    assert response.status == 200, await response.text()
    assert (await response.json())["operator"] == "authentik:carol"
