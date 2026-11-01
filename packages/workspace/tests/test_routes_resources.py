# SPDX-License-Identifier: MIT
"""Resource check-ins and usage accounts are scoped reads; no token approvals remain (D102)."""

from datetime import UTC, datetime
from pathlib import Path

from sandbox_science.contracts import new_id
from sandbox_science.orchestrator.context.history import Archive, Entry
from sandbox_science.orchestrator.context.history.usage import AnsweredUsage
from sandbox_science.orchestrator.continuity.blueprints import Blueprint, Blueprints
from sandbox_science.orchestrator.continuity.models import ModelIntents
from sandbox_science.orchestrator.continuity.records import WorkRecords
from sandbox_science.orchestrator.continuity.resources import ResourceRequests
from sandbox_science.orchestrator.delegation.records import Delegation, DelegationRecords
from sandbox_science.orchestrator.projects import ModelUsage, ProjectStore
from sandbox_science.orchestrator.runstate import RunState

from .authentik_support import ADMINS, USERS, Authentik, as_user
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
        RunState(store.control).set_cost_notices("p1", [5.0])
    finally:
        store.close()


async def test_resource_views_are_authenticated_scoped_and_cursor_validated(
    aiohttp_client: ClientFactory, state_dir: Path, static_dir: Path
) -> None:
    seed_resources(state_dir)
    anonymous = await workspace(aiohttp_client, state_dir, static_dir, login=False)
    await error_of(await anonymous.get(ROOT + "/resources"), 401, "policy_denied")
    await error_of(await anonymous.get(ROOT + "/usage"), 401, "policy_denied")
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
        await client.get(ROOT.replace("/projects/p1/", "/projects/p2/") + "/usage"),
        404,
        "not_found",
    )


async def test_token_approvals_are_gone_and_usage_is_accounted_by_role(
    aiohttp_client: ClientFactory, state_dir: Path, static_dir: Path
) -> None:
    seed_resources(state_dir)
    store = ProjectStore(state_dir)
    try:
        for ordinal, (run, cost) in enumerate(((RUN, 1.5), (CHILD, 0.5)), 1):
            Archive(store.control).append(
                "p1",
                "a",
                run,
                Entry("assistant", "assistant", "An answer"),
                model_usage=AnsweredUsage(
                    ordinal=ordinal,
                    observed_at=datetime.now(UTC),
                    usage=ModelUsage(input_tokens=100, output_tokens=10, cost=cost),
                ),
            )
        owner = WorkRecords(store.control).owner("p1", "a", CHILD)
        assert owner is not None
        with store.control.write() as tx:
            tx.sql.execute("UPDATE research_work SET state='running' WHERE work=?", (CHILD,))
        intents = ModelIntents(store.control)
        identity = intents.begin(owner, kind="reviewer", alias="fixture")
        intents.observe(owner, identity, ModelUsage(input_tokens=100, output_tokens=10, cost=0.25))
        RunState(store.control).note_cost_crossings("p1")
    finally:
        store.close()
    client = await workspace(aiohttp_client, state_dir, static_dir)
    for path in ("/token-grants/" + CHILD, "/token-grants/next", "/token-grants"):
        assert (await client.get(ROOT + path)).status == 404
        posted = await client.post(ROOT + path, json={}, headers=origin(client))
        assert posted.status in (404, 405)  # no route writes a grant
    response = await client.get(ROOT + "/usage")
    assert response.status == 200, await response.text()
    account = await response.json()
    assert account["session"] == "a"
    assert account["daily_cost_limit"] is None
    roles = {item["role"]: item for item in account["total"]["by_role"]}
    assert roles["reviewer"]["cost"] == 0.25
    assert roles["reviewer"]["tokens"] == 110
    assert {item["run"] for item in account["runs"]} == {RUN, CHILD}
    assert account["notices"] == [
        {"threshold": 5.0, "crossed_at": None, "spent": None}
    ]  # 2.25 recorded: not crossed
    project = await (await client.get("/api/v0/projects/p1/usage")).json()
    assert project["total"]["cost"] == 2.25
    assert [item["session"] for item in project["sessions"]] == ["a"]
    await error_of(await client.get(ROOT + "/usage?x=1"), 400, "invalid_request")


async def test_user_soft_extension_is_scoped_origin_checked_and_never_resumes(
    aiohttp_client: ClientFactory, state_dir: Path, static_dir: Path
) -> None:
    seed_resources(state_dir)
    store = ProjectStore(state_dir)
    try:
        # A warning is emitted during the run; then the tree pauses at its allowance.
        works = WorkRecords(store.control)
        with store.control.write() as tx:
            tx.sql.execute("UPDATE research_work SET state='running' WHERE work=?", (CHILD,))
        owner = works.owner("p1", "a", CHILD)
        assert owner is not None
        warning = ResourceRequests(store.control).observe(
            owner, "turns", used=300, total=300, revision=0
        )
        works.interrupt("p1", "a", RUN)
        assert warning is not None
    finally:
        store.close()
    client = await workspace(aiohttp_client, state_dir, static_dir)
    body = {
        "request_id": warning.request_id,
        "current": CHILD,
        "epoch": 1,
        "total": 300,
        "action": "extend_turns",
        "reason": "Continue the verified experiment",
    }
    assert (await client.post(ROOT + "/resources/decide", json=body)).status == 403
    granted = await client.post(ROOT + "/resources/decide", json=body, headers=origin(client))
    assert granted.status == 200, await granted.text()
    decision = await granted.json()
    assert decision["total"] == 600
    assert decision["operator"] == "user"
    await error_of(
        await client.post(
            ROOT.replace("/projects/p1/", "/projects/p2/") + "/resources/decide",
            json=body,
            headers=origin(client),
        ),
        404,
        "not_found",
    )
    store = ProjectStore(state_dir)
    try:
        assert WorkRecords(store.control).queued() == []
    finally:
        store.close()


async def test_with_an_admin_group_a_resource_extension_is_the_admins(
    aiohttp_client: ClientFactory, state_dir: Path, static_dir: Path
) -> None:
    seed_resources(state_dir)
    store = ProjectStore(state_dir)
    try:
        # A warning is emitted during the run; then the tree pauses at its allowance.
        works = WorkRecords(store.control)
        with store.control.write() as tx:
            tx.sql.execute("UPDATE research_work SET state='running' WHERE work=?", (CHILD,))
        owner = works.owner("p1", "a", CHILD)
        assert owner is not None
        warning = ResourceRequests(store.control).observe(
            owner, "turns", used=300, total=300, revision=0
        )
        works.interrupt("p1", "a", RUN)
        assert warning is not None
    finally:
        store.close()
    authentik = Authentik()
    client = await workspace(
        aiohttp_client, state_dir, static_dir, login=False, forward_auth=authentik.verifier(ADMINS)
    )
    body = {
        "request_id": warning.request_id,
        "current": CHILD,
        "epoch": 1,
        "total": 300,
        "action": "extend_turns",
        "reason": "Continue the verified experiment",
    }
    member = {**as_user(authentik.token("bob")), **origin(client)}
    admin = {**as_user(authentik.token("carol", (USERS, ADMINS))), **origin(client)}
    refused = await client.post(ROOT + "/resources/decide", json=body, headers=member)
    await error_of(refused, 403, "policy_denied")
    # Stopping narrows: the gate lets a member through (this request ID is unknown).
    stop = {**body, "action": "stop", "request_id": "no-such-request"}
    passed = await client.post(ROOT + "/resources/decide", json=stop, headers=member)
    assert passed.status != 403, await passed.text()
    granted = await client.post(ROOT + "/resources/decide", json=body, headers=admin)
    assert granted.status == 200, await granted.text()
    decision = await granted.json()
    assert (decision["total"], decision["operator"]) == (600, "authentik:carol")
