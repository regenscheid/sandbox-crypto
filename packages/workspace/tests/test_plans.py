# SPDX-License-Identifier: MIT
"""Authenticated compact control-state views; no worker/model/lifecycle authority."""

from __future__ import annotations

import tracemalloc

import pytest

from sandbox_science.orchestrator.documents import DocumentStore, store_path
from sandbox_science.orchestrator.projects import ProjectStore
from sandbox_science.orchestrator.session_plans import (
    GoalOutcome,
    PlanAuthority,
    PlanBody,
    PlanOutput,
    PlanPhase,
    PlanScope,
    PlanStep,
    SessionPlans,
)
from sandbox_science.workspace import plans
from sandbox_science.workspace.app import create_app

TOKEN = "test-secret-session-plan-login-0123456789"
PATH = "/api/v0/projects/p1/sessions/a/plan"


def authority(project="p1", session="a"):
    return PlanAuthority(
        scope=PlanScope(project_id=project, session_id=session), role="main", run_id="run-one"
    )


def body(description="Check the claim"):
    return PlanBody(
        goal="A bounded goal",
        feasibility="Check assumptions",
        outputs=(
            PlanOutput(title="Result", description="Derivation", evidence_standard="Reproduce"),
        ),
        phases=(
            PlanPhase(
                phase_id="phase",
                title="Work",
                steps=(
                    PlanStep(
                        step_id="step",
                        title="Analyze",
                        description=description,
                        evidence_standard="Evidence",
                    ),
                ),
            ),
        ),
    )


@pytest.fixture
def inventory(state_dir):
    owner = DocumentStore(store_path(state_dir))
    journal = SessionPlans(owner)
    yield journal
    owner.close()


def seed(inventory, *, project="p1", session="a"):
    return inventory.publish(authority(project, session), "create", body(), "Initial plan")


async def create(aiohttp_client, state_dir, static_dir, *, login=True, **kwargs):
    client = await aiohttp_client(
        create_app(
            ProjectStore(state_dir),
            static_dir,
            login_token=TOKEN,
            **kwargs,
        )
    )
    if login:
        assert (
            await client.get("/login", params={"token": TOKEN}, allow_redirects=False)
        ).status == 303
    return client


@pytest.mark.asyncio
async def test_live_compact_read_sees_committed_progress_without_saved_run(
    aiohttp_client, state_dir, static_dir, inventory
):
    first = seed(inventory)
    client = await create(aiohttp_client, state_dir, static_dir)
    response = await client.get(PATH)
    assert response.status == 200
    assert response.headers["Cache-Control"] == "no-store"
    before = plans.WorkspacePlan.model_validate_json(await response.read(), strict=True)
    assert before.plan is not None
    assert before.plan.head.ref == first.ref
    assert before.plan.steps[0].latest is None
    assert before.session_closed_at is None
    inventory.progress(
        authority(), "work", first.ref, "step", 0, "in_progress", "Long note is omitted"
    )
    response = await client.get(PATH)
    after = plans.WorkspacePlan.model_validate_json(await response.read(), strict=True)
    assert after.plan is not None
    assert after.plan.steps[0].latest is not None
    assert after.plan.event_cursor == 2
    assert after.plan.head.progress_cursor == 1
    assert after.plan.steps[0].latest.status == "in_progress"
    assert after.plan.steps[0].latest.author.run_id == "run-one"
    assert "note" not in after.plan.steps[0].latest.model_dump()
    assert after.observed_at >= before.observed_at


@pytest.mark.asyncio
async def test_changed_intent_retains_old_completion_as_nonmatching(
    aiohttp_client, state_dir, static_dir, inventory
):
    first = seed(inventory)
    inventory.progress(authority(), "done", first.ref, "step", 0, "completed", "Assertion")
    changed = inventory.revise(authority(), "edit", first.ref, body("Different intent"), "Revise")
    client = await create(aiohttp_client, state_dir, static_dir)
    result = plans.WorkspacePlan.model_validate_json(
        await (await client.get(PATH)).read(), strict=True
    )
    assert result.plan is not None
    assert result.plan.steps[0].latest is not None
    assert result.plan.head.ref == changed.ref
    assert result.plan.steps[0].latest.status == "completed"
    assert not result.plan.steps[0].matches_current_step
    assert result.plan.steps[0].latest.ref == first.ref


@pytest.mark.asyncio
async def test_explicit_partial_goal_outcome_is_not_omitted(
    aiohttp_client, state_dir, static_dir, inventory
):
    first = seed(inventory)
    inventory.progress(authority(), "blocked", first.ref, "step", 0, "blocked", "Missing evidence")
    inventory.set_status(
        authority(),
        "finish",
        first.ref,
        1,
        1,
        "completed",
        "Workflow ended",
        outcome=GoalOutcome(
            status="partially_achieved",
            summary="Partial result",
            evidence=(),
            remaining_work=("Resolve limit",),
        ),
    )
    client = await create(aiohttp_client, state_dir, static_dir)
    result = plans.WorkspacePlan.model_validate_json(
        await (await client.get(PATH)).read(), strict=True
    )
    assert result.plan is not None
    assert result.plan.head.outcome is not None
    assert result.plan.head.status == "completed"
    assert result.plan.head.outcome.status == "partially_achieved"
    assert result.plan.head.outcome.remaining_work == ("Resolve limit",)


@pytest.mark.asyncio
async def test_closed_session_history_is_read_without_waking_or_reopening(
    aiohttp_client, state_dir, static_dir, inventory, monkeypatch
):
    first = seed(inventory, session="b")

    def forbidden(*args, **kwargs):
        pytest.fail("plan read must not reopen the session")

    monkeypatch.setattr(ProjectStore, "open_session", forbidden)
    client = await create(aiohttp_client, state_dir, static_dir)
    response = await client.get(PATH.replace("/a/", "/b/"))
    assert response.status == 200
    result = plans.WorkspacePlan.model_validate_json(await response.read(), strict=True)
    assert result.plan is not None
    assert result.session_closed_at is not None
    assert result.plan.head.ref == first.ref


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "target", ["/p1/sessions/missing", "/p2/sessions/a", "/missing/sessions/a"]
)
async def test_missing_or_foreign_session_is_404(
    aiohttp_client, state_dir, static_dir, inventory, target
):
    seed(inventory)
    client = await create(aiohttp_client, state_dir, static_dir)
    response = await client.get("/api/v0/projects" + target + "/plan")
    assert response.status == 404
    assert response.headers["Cache-Control"] == "no-store"
    assert "plan" not in await response.json()


@pytest.mark.asyncio
async def test_same_named_foreign_session_never_returns_other_projects_plan(
    aiohttp_client, state_dir, static_dir, inventory
):
    seed(inventory)
    ProjectStore(state_dir).open_session("p2", "a", dedicated=False)
    client = await create(aiohttp_client, state_dir, static_dir)
    result = await (await client.get(PATH.replace("/p1/", "/p2/"))).json()
    assert result["project_id"] == "p2"
    assert result["plan"] is None


@pytest.mark.asyncio
async def test_authentication_and_no_browser_supplied_context(
    aiohttp_client, state_dir, static_dir
):
    client = await create(aiohttp_client, state_dir, static_dir, login=False)
    response = await client.get(PATH)
    assert response.status == 401
    assert response.headers["Cache-Control"] == "no-store"
    await client.get("/login", params={"token": TOKEN}, allow_redirects=False)
    for query in ("?project_id=p2", "?session_id=b", "?plan_id=foreign"):
        response = await client.get(PATH + query)
        assert response.status == 400
        assert response.headers["Cache-Control"] == "no-store"
    assert (await client.get(PATH, data=b"{}")).status == 400


@pytest.mark.asyncio
async def test_host_refusal_is_not_cacheable(aiohttp_client, state_dir, static_dir):
    client = await create(aiohttp_client, state_dir, static_dir)
    response = await client.get(PATH, headers={"Host": "foreign.example"})
    assert response.status == 403
    assert response.headers["Cache-Control"] == "no-store"


@pytest.mark.asyncio
@pytest.mark.parametrize("existing", [False, True])
async def test_absent_history_returns_null_without_creating_or_migrating_sql(
    aiohttp_client, state_dir, static_dir, existing
):
    path = store_path(state_dir)
    owner = DocumentStore(path)
    if existing:
        owner.initialize()
    client = await create(aiohttp_client, state_dir, static_dir)
    response = await client.get(PATH)
    assert response.status == 200
    assert (await response.json())["plan"] is None
    if not existing:
        assert not path.exists()
    else:
        with owner.control.read() as tx:
            db = tx.sql
            assert db.execute(
                "SELECT count(*) FROM sqlite_master WHERE name LIKE 'session_plan_%'"
            ).fetchone() == (0,)
    owner.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("damage", ["partial", "version", "pointer", "head", "large"])
async def test_corrupt_history_is_503_not_null_or_unbounded_allocation(
    aiohttp_client, state_dir, static_dir, damage
):
    owner = DocumentStore(store_path(state_dir))
    try:
        if damage == "partial":
            owner.initialize()
            with owner.control.write() as tx:
                db = tx.sql
                db.execute(
                    "CREATE TABLE session_plan_meta(singleton INTEGER PRIMARY KEY,version INTEGER)"
                )
        else:
            journal = SessionPlans(owner)
            seed(journal)
            with owner.control.write() as tx:
                db = tx.sql
                if damage == "version":
                    db.execute("DROP TRIGGER session_plan_records_no_delete")
                    db.execute("DELETE FROM session_plan_records WHERE kind='version'")
                elif damage == "pointer":
                    db.execute("DELETE FROM session_plan_projections WHERE kind='current'")
                else:
                    db.execute(
                        "UPDATE session_plan_projections SET record=? WHERE kind='current'",
                        ("x" * (20_000_000 if damage == "large" else 1),),
                    )
        client = await create(aiohttp_client, state_dir, static_dir)
        tracemalloc.start()
        try:
            response = await client.get(PATH)
            assert response.status == 503
            assert "plan" not in await response.json()
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        assert peak < 2_000_000
    finally:
        owner.close()
