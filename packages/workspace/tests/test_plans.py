# SPDX-License-Identifier: MIT
"""Authenticated compact control-state views; no worker/model/lifecycle authority."""

from __future__ import annotations

import asyncio
import json
import os
import threading
import tracemalloc
from collections.abc import Awaitable, Callable
from typing import Any

import pytest
from aiohttp.test_utils import TestClient

from sandbox_science.orchestrator.documents import DocumentStore, store_path
from sandbox_science.orchestrator.projects import ProjectError, ProjectStore
from sandbox_science.orchestrator.session_plans import (
    GoalOutcome,
    PlanAuthority,
    PlanBody,
    PlanEvidence,
    PlanOutput,
    PlanPhase,
    PlanScope,
    PlanStep,
    SessionPlans,
)
from sandbox_science.workspace import plans
from sandbox_science.workspace.api import Timeouts
from sandbox_science.workspace.app import create_app

TOKEN = "test-secret-session-plan-login-0123456789"
PATH = "/api/v0/projects/p1/sessions/a/plan"
ClientFactory = Callable[..., Awaitable[TestClient[Any, Any]]]


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
        pytest.fail("plan read must not use unbounded/lifecycle registry methods")

    monkeypatch.setattr(ProjectStore, "get", forbidden)
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
    aiohttp_client, state_dir, static_dir, monkeypatch
):
    def forbidden(*args):
        pytest.fail("unauthenticated request must not read registry")

    monkeypatch.setattr(plans, "session_snapshot", forbidden)
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
    client = await create(aiohttp_client, state_dir, static_dir, document_store=owner)
    response = await client.get(PATH)
    assert response.status == 200
    assert (await response.json())["plan"] is None
    if not existing:
        assert not path.exists()
    else:
        with owner._read() as db:
            assert db is not None
            assert db.execute(
                "SELECT count(*) FROM sqlite_master WHERE name LIKE 'session_plan_%'"
            ).fetchone() == (0,)
    assert client.app[plans.READER].documents.readonly


@pytest.mark.asyncio
@pytest.mark.parametrize("damage", ["partial", "version", "pointer", "head", "large"])
async def test_corrupt_history_is_503_not_null_or_unbounded_allocation(
    aiohttp_client, state_dir, static_dir, damage
):
    owner = DocumentStore(store_path(state_dir))
    try:
        if damage == "partial":
            owner.initialize()
            with owner._write() as db:
                db.execute(
                    "CREATE TABLE session_plan_meta(singleton INTEGER PRIMARY KEY,version INTEGER)"
                )
        else:
            journal = SessionPlans(owner)
            seed(journal)
            with owner._write() as db:
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


@pytest.mark.asyncio
async def test_entire_response_cap_refuses_without_clipping_json(
    aiohttp_client, state_dir, static_dir, inventory, monkeypatch
):
    seed(inventory)
    monkeypatch.setattr(plans, "MAX_RESPONSE_BYTES", 128)
    client = await create(aiohttp_client, state_dir, static_dir)
    response = await client.get(PATH)
    assert response.status == 503
    assert set(await response.json()) == {"error"}


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["close", "aba", "identical-aba", "policy", "rebind"])
async def test_delayed_sql_read_cannot_survive_registry_transition(
    aiohttp_client, state_dir, static_dir, inventory, monkeypatch, change
):
    seed(inventory)
    entered, release = threading.Event(), threading.Event()
    original = plans.PlanReader._plan

    def blocked(self, scope, login):
        result = original(self, scope, login)
        entered.set()
        assert release.wait(5)
        return result

    monkeypatch.setattr(plans.PlanReader, "_plan", blocked)
    client = await create(aiohttp_client, state_dir, static_dir)
    call = asyncio.create_task(client.get(PATH))
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        store = ProjectStore(state_dir)
        path = store.root / "p1.json"
        before = path.read_bytes()
        if change in {"close", "aba", "identical-aba"}:
            store.close_session("p1", "a")
            if change != "close":
                store.open_session("p1", "a", dedicated=False)
            if change == "identical-aba":
                replacement = store.root / "restore"
                replacement.write_bytes(before)
                os.replace(replacement, path)
        elif change == "policy":
            store.set_policy("p1", "sha256:" + "b" * 64)
        else:
            value = json.loads(before)
            value["sessions"]["a"]["environment_id"] = "forged-binding"
            path.write_text(json.dumps(value))
        release.set()
        assert (await call).status == 503
    finally:
        release.set()
        await asyncio.gather(call, return_exceptions=True)


@pytest.mark.asyncio
async def test_original_login_rechecked_after_sql_even_if_browser_logs_in_again(
    aiohttp_client, state_dir, static_dir, inventory, monkeypatch
):
    seed(inventory)
    entered, release = threading.Event(), threading.Event()
    original = plans.PlanReader._plan

    def blocked(self, scope, login):
        result = original(self, scope, login)
        entered.set()
        assert release.wait(5)
        return result

    monkeypatch.setattr(plans.PlanReader, "_plan", blocked)
    client = await create(aiohttp_client, state_dir, static_dir)
    call = asyncio.create_task(client.get(PATH))
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        origin = str(client.make_url("/")).rstrip("/")
        assert (await client.post("/api/v0/logout", headers={"Origin": origin})).status == 204
        await client.get("/login", params={"token": TOKEN}, allow_redirects=False)
        release.set()
        response = await call
        assert response.status == 401
        assert response.headers["Cache-Control"] == "no-store"
    finally:
        release.set()
        await asyncio.gather(call, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("damage", ["scope", "session", "large", "symlink", "fifo", "time"])
async def test_registry_bounds_and_special_files_are_prompt(
    aiohttp_client, state_dir, static_dir, tmp_path, damage
):
    path = state_dir / "projects" / "p1.json"
    value = json.loads(path.read_bytes())
    if damage == "scope":
        value["project_id"] = "p2"
    if damage == "session":
        value["sessions"]["a"]["session_id"] = "b"
    if damage == "time":
        value["sessions"]["a"]["opened_at"] = "2026-10-12T12:00:00"
    if damage in {"symlink", "fifo"}:
        copied = tmp_path / "saved-project"
        copied.write_bytes(path.read_bytes())
        path.unlink()
        if damage == "symlink":
            path.symlink_to(copied)
        else:
            os.mkfifo(path)
    else:
        path.write_text("x" * (1024 * 1024 + 1) if damage == "large" else json.dumps(value))
    client = await create(aiohttp_client, state_dir, static_dir)
    assert (await asyncio.wait_for(client.get(PATH), 1)).status == 503


def test_registry_growth_during_read_is_bounded_before_parse(state_dir, monkeypatch):
    path = state_dir / "projects" / "p1.json"
    original = os.read
    identity = path.stat().st_ino
    lengths = []
    grown = False
    monkeypatch.setattr(plans, "MAX_SESSION_RECORD_BYTES", 16 * 1024)

    def grow(descriptor, length):
        nonlocal grown
        if os.fstat(descriptor).st_ino == identity:
            lengths.append(length)
            if not grown:
                grown = True
                with path.open("ab") as stream:
                    stream.write(b"x" * (16 * 1024 + 1))
        return original(descriptor, length)

    monkeypatch.setattr(os, "read", grow)
    with pytest.raises(ProjectError, match="changed during read"):
        plans.session_snapshot(ProjectStore(state_dir), "p1", "a")
    assert grown
    assert max(lengths) <= 16 * 1024 + 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "invalid",
    ["scope", "ref", "step", "hash", "match", "cursor", "base-first", "base-later", "outcome"],
)
async def test_unvalidated_copy_cannot_escape_scope_or_step_coherence(
    aiohttp_client, state_dir, static_dir, inventory, monkeypatch, invalid
):
    first = seed(inventory)
    inventory.progress(authority(), "done", first.ref, "step", 0, "completed", "Assertion")
    if invalid == "base-later":
        inventory.revise(authority(), "revise", first.ref, body(), "Retain intent")
    if invalid == "outcome":
        inventory.set_status(
            authority(),
            "finish",
            first.ref,
            1,
            1,
            "completed",
            "Finished",
            outcome=GoalOutcome(status="achieved", summary="Claim", evidence=(), remaining_work=()),
        )
    valid = inventory.compact_current(authority())
    if invalid == "scope":
        value = valid.model_copy(
            update={
                "head": valid.head.model_copy(
                    update={"scope": PlanScope(project_id="other", session_id="a")}
                )
            }
        )
    elif invalid == "ref":
        value = valid.model_copy(
            update={
                "head": valid.head.model_copy(
                    update={"ref": valid.head.ref.model_copy(update={"version": 2})}
                )
            }
        )
    elif invalid == "step":
        value = valid.model_copy(update={"steps": ()})
    elif invalid == "hash":
        value = valid.model_copy(
            update={"steps": (valid.steps[0].model_copy(update={"step_sha256": "a" * 64}),)}
        )
    elif invalid == "match":
        value = valid.model_copy(
            update={"steps": (valid.steps[0].model_copy(update={"matches_current_step": False}),)}
        )
    elif invalid in {"base-first", "base-later"}:
        value = valid.model_copy(
            update={
                "version": valid.version.model_copy(
                    update={"base": 1 if invalid == "base-first" else None}
                )
            }
        )
    elif invalid == "outcome":
        value = valid.model_copy(
            update={
                "head": valid.head.model_copy(
                    update={
                        "outcome": valid.head.outcome.model_copy(
                            update={
                                "evidence": (
                                    PlanEvidence(
                                        project_id="other",
                                        kind="ledger",
                                        reference_id="claim",
                                        revision=1,
                                    ),
                                )
                            }
                        )
                    }
                )
            }
        )
    else:
        value = valid.model_copy(
            update={
                "steps": (
                    valid.steps[0].model_copy(
                        update={"latest": valid.steps[0].latest.model_copy(update={"sequence": 2})}
                    ),
                )
            }
        )
    monkeypatch.setattr(plans.PlanReader, "_plan", lambda *args: value)
    client = await create(aiohttp_client, state_dir, static_dir)
    assert (await client.get(PATH)).status == 503


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True])
async def test_timeout_cancel_and_shutdown_retain_actual_thread_ownership(
    state_dir, inventory, monkeypatch, cancel
):
    seed(inventory)
    entered, release = threading.Event(), threading.Event()
    reader = plans.PlanReader(ProjectStore(state_dir), inventory.documents, timeout=0.03)
    original = reader._plan

    def blocked(scope, login):
        entered.set()
        assert release.wait(5)
        return original(scope, login)

    monkeypatch.setattr(reader, "_plan", blocked)
    call = asyncio.create_task(reader.read("p1", "a", "login", lambda: True))
    closing = None
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        if cancel:
            call.cancel()
            call.cancel()
            with pytest.raises(asyncio.CancelledError):
                await call
        else:
            with pytest.raises(ProjectError, match="timed out"):
                await call
        assert len(reader._tasks) == 1
        with pytest.raises(ProjectError, match="capacity"):
            await reader.read("p1", "a", "other-login", lambda: True)
        assert await asyncio.wait_for(asyncio.to_thread(lambda: 7), 0.2) == 7
        closing = asyncio.create_task(reader.close())
        await asyncio.sleep(0)
        closing.cancel()
        await asyncio.sleep(0)
        closing.cancel()
        await asyncio.sleep(0)
        assert not closing.done()
        assert not reader._closed
    finally:
        release.set()
        if closing is not None:
            await asyncio.gather(closing, return_exceptions=True)
        await reader.close()
    assert not reader._tasks
    assert not reader._scopes
    assert not reader._logins


@pytest.mark.asyncio
async def test_cancelled_initial_file_read_keeps_and_then_closes_its_descriptors(
    state_dir, inventory, monkeypatch
):
    entered, release = threading.Event(), threading.Event()
    retained = []
    original = plans.session_snapshot

    def blocked(*args):
        snapshot = original(*args)
        retained.extend((snapshot._descriptor, snapshot._root))
        entered.set()
        assert release.wait(5)
        return snapshot

    monkeypatch.setattr(plans, "session_snapshot", blocked)
    reader = plans.PlanReader(ProjectStore(state_dir), inventory.documents)
    call = asyncio.create_task(reader.read("p1", "a", "login", lambda: True))
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        call.cancel()
        with pytest.raises(asyncio.CancelledError):
            await call
        assert len(reader._tasks) == 1
        for descriptor in retained:
            os.fstat(descriptor)
    finally:
        release.set()
        await reader.close()
    for descriptor in retained:
        with pytest.raises(OSError, match="Bad file descriptor"):
            os.fstat(descriptor)


@pytest.mark.asyncio
async def test_actual_http_disconnect_does_not_release_blocked_sql_ownership(
    aiohttp_client, state_dir, static_dir, inventory, monkeypatch
):
    seed(inventory)
    entered, release = threading.Event(), threading.Event()
    original = plans.PlanReader._plan

    def blocked(self, scope, login):
        entered.set()
        assert release.wait(5)
        return original(self, scope, login)

    monkeypatch.setattr(plans.PlanReader, "_plan", blocked)
    client = await create(aiohttp_client, state_dir, static_dir, timeouts=Timeouts(call=0.03))
    reader = client.app[plans.READER]
    call = asyncio.create_task(client.get(PATH))
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        call.cancel()
        with pytest.raises(asyncio.CancelledError):
            await call
        await asyncio.sleep(0.06)  # past the deadline, the blocked owner is still held
        assert len(reader._tasks) == 1
        assert reader._scopes == {("p1", "a")}
        with pytest.raises(ProjectError, match="capacity"):
            await reader.read("p1", "a", "new-login", lambda: True)
    finally:
        release.set()
        await reader.close()
    assert not reader._tasks


@pytest.mark.asyncio
async def test_admission_is_immediate_per_login_and_global(state_dir, inventory, monkeypatch):
    seed(inventory)
    entered, release = threading.Event(), threading.Event()
    reader = plans.PlanReader(ProjectStore(state_dir), inventory.documents)
    original = reader._plan

    def blocked(scope, login):
        entered.set()
        assert release.wait(5)
        return original(scope, login)

    monkeypatch.setattr(reader, "_plan", blocked)
    store = ProjectStore(state_dir)
    store.open_session("p1", "d", dedicated=False)
    calls = [
        asyncio.create_task(reader.read("p1", name, "login", lambda: True)) for name in ("a", "c")
    ]
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        with pytest.raises(ProjectError, match="capacity"):
            await reader.read("p1", "d", "login", lambda: True)
        monkeypatch.setattr(plans, "MAX_CALLS", 2)
        with pytest.raises(ProjectError, match="capacity"):
            await reader.read("p1", "d", "another", lambda: True)
    finally:
        release.set()
        await asyncio.gather(*calls, return_exceptions=True)
        await reader.close()


@pytest.mark.asyncio
async def test_app_cleanup_cannot_close_shared_worker_before_plan_drain(
    aiohttp_client, state_dir, static_dir, inventory, monkeypatch
):
    seed(inventory)
    entered, release = threading.Event(), threading.Event()
    original = plans.PlanReader._plan

    def blocked(self, scope, login):
        entered.set()
        assert release.wait(5)
        return original(self, scope, login)

    monkeypatch.setattr(plans.PlanReader, "_plan", blocked)

    class Worker:
        closed = False

        async def close(self):
            self.closed = True

    worker = Worker()
    client = await create(aiohttp_client, state_dir, static_dir, job_worker=worker)
    reader = client.app[plans.READER]
    call = asyncio.create_task(reader.read("p1", "a", "login", lambda: True))
    assert await asyncio.to_thread(entered.wait, 2)
    call.cancel()
    await asyncio.gather(call, return_exceptions=True)
    closing = asyncio.create_task(client.close())
    try:
        await asyncio.sleep(0.01)
        assert not closing.done()
        assert not worker.closed
    finally:
        release.set()
        await closing
    assert reader._closed
    assert worker.closed


@pytest.mark.asyncio
async def test_cancelled_plan_close_still_closes_shared_worker(
    aiohttp_client, state_dir, static_dir, monkeypatch
):
    class Worker:
        closed = False

        async def close(self):
            self.closed = True

    worker = Worker()
    client = await create(aiohttp_client, state_dir, static_dir, job_worker=worker)
    reader = client.app[plans.READER]
    original = reader.close

    async def cancel_after_drain():
        await original()
        raise asyncio.CancelledError

    monkeypatch.setattr(reader, "close", cancel_after_drain)
    with pytest.raises(asyncio.CancelledError):
        await client.close()
    assert reader._closed
    assert worker.closed
