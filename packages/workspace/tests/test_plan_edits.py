# SPDX-License-Identifier: MIT
"""User edits of a plan body: the compare-and-swap, replay and the plans store's refusals."""

from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
from types import SimpleNamespace
from typing import cast

import pytest

from sandbox_science.contracts import ExecutionContext
from sandbox_science.orchestrator.documents import DocumentStore, store_path
from sandbox_science.orchestrator.loop import run_task
from sandbox_science.orchestrator.model import ModelTurn, ScriptedModel, ToolCall
from sandbox_science.orchestrator.plan_tools import PlanTools
from sandbox_science.orchestrator.projects import ProjectStore
from sandbox_science.orchestrator.session_plans import (
    PlanAuthority,
    PlanBody,
    PlanScope,
    SessionPlans,
)
from sandbox_science.orchestrator.tools import Toolbox, ToolEntry, ToolOutcome
from sandbox_science.orchestrator.worker_client import WorkerClient
from sandbox_science.workspace import plan_edits
from sandbox_science.workspace.app import create_app

TOKEN = "test-secret-plan-edit-login-0123456789"
PATH = "/api/v0/projects/p1/sessions/a/plan"


def authority(session="a"):
    return PlanAuthority(
        scope=PlanScope(project_id="p1", session_id=session), role="main", run_id="main-run"
    )


def body(description="Check the claim"):
    return PlanBody.model_validate_json(
        json.dumps(
            {
                "goal": "A bounded goal",
                "feasibility": "Inspect assumptions",
                "outputs": [
                    {"title": "Result", "description": "Derive", "evidence_standard": "Check"}
                ],
                "phases": [
                    {
                        "phase_id": "phase",
                        "title": "Analysis",
                        "steps": [
                            {
                                "step_id": "step",
                                "title": "Analyze",
                                "description": description,
                                "evidence_standard": "Retain evidence",
                            },
                            {
                                "step_id": "other",
                                "title": "Compare",
                                "description": "Keep this intent",
                                "evidence_standard": "Known case",
                            },
                        ],
                    }
                ],
            }
        )
    )


@pytest.fixture
def journal(state_dir):
    owner = DocumentStore(store_path(state_dir))
    core = SessionPlans(owner)
    yield core
    owner.close()


def seed(journal, session="a"):
    receipt = journal.publish(authority(session), "create", body(), "Initial plan")
    journal.set_status(authority(session), "active", receipt.ref, 1, 0, "active", "Authorized")
    return receipt.ref


def payload(ref, *, operation="user-edit", description="USER refined ∀ claim"):
    return {
        "operation_id": operation,
        "base": ref.model_dump(mode="json"),
        "body": body(description).model_dump(mode="json"),
        "reason": "USER steering ∀",
    }


async def create(aiohttp_client, state_dir, static_dir, *, login=True, **kwargs):
    client = await aiohttp_client(
        create_app(ProjectStore(state_dir), static_dir, login_token=TOKEN, **kwargs)
    )
    if login:
        assert (
            await client.get("/login", params={"token": TOKEN}, allow_redirects=False)
        ).status == 303
    return client


def origin(client):
    return {"Origin": str(client.make_url("/")).rstrip("/")}


async def post(client, value, path=PATH):
    return await client.post(path + "/changes", json=value, headers=origin(client))


@pytest.mark.asyncio
async def test_http_user_version_preserves_progress_and_historical_operation_replay(
    aiohttp_client, state_dir, static_dir, journal
):
    first = seed(journal)
    journal.progress(authority(), "done", first, "step", 0, "completed", "Original evidence")
    journal.progress(authority(), "other-done", first, "other", 0, "completed", "Keep evidence")
    client = await create(aiohttp_client, state_dir, static_dir)
    request = payload(first)
    response = await post(client, request)
    assert response.status == 200
    assert response.headers["Cache-Control"] == "no-store"
    receipt = plan_edits.EditReceipt.model_validate_json(await response.read(), strict=True)
    assert receipt.project_id == "p1"
    assert receipt.receipt.operation_id == request["operation_id"]
    assert receipt.receipt.action == "revise"
    assert receipt.receipt.ref.version == 2
    current = journal.compact_current(authority())
    assert current is not None
    assert current.head.status == "active"
    assert current.head.status_revision == 2
    assert current.head.progress_cursor == 2
    assert current.version.author.role == "user"
    assert current.version.author.user_id is not None
    assert len(current.version.author.user_id) == 64
    assert current.version.author.run_id is None
    assert current.version.reason == request["reason"]
    assert not current.steps[0].matches_current_step
    assert current.steps[0].latest is not None
    assert current.steps[0].latest.status == "completed"
    assert current.steps[1].matches_current_step
    assert journal.version(authority(), first).body == body()
    third = journal.revise(authority(), "main-after", receipt.receipt.ref, body("Later"), "Later")
    replay = await post(client, request)
    assert replay.status == 200
    assert plan_edits.EditReceipt.model_validate_json(await replay.read()) == receipt
    assert journal.summary(authority()).head == third.ref
    different = {**request, "reason": "Changed under the same operation"}
    assert (await post(client, different)).status == 409
    assert journal.summary(authority()).head == third.ref
    conflict = await post(client, payload(first, operation="stale-other"))
    assert conflict.status == 409
    assert (await conflict.json())["error"]["current"] == third.ref.model_dump(mode="json")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    [
        {"role": "user"},
        {"author": {"role": "main", "run_id": "forged"}},
        {"project_id": "p2"},
        {"session_id": "c"},
        {"status": "active"},
        {"progress": []},
        {"outcome": {"status": "achieved"}},
        {"approval": True},
        {"grant": {}},
        {"action": "publish"},
        {"base": {"plan_id": "plan", "version": True}},
        {"operation_id": "../foreign"},
        {"reason": "\0"},
        {"reason": "\ud800"},
        {"body": {"goal": "Incomplete body"}},
    ],
)
async def test_strict_body_only_json_has_no_authority_injection(
    aiohttp_client, state_dir, static_dir, journal, change
):
    ref = seed(journal)
    client = await create(aiohttp_client, state_dir, static_dir)
    response = await post(client, {**payload(ref), **change})
    assert response.status == 400
    assert response.headers["Cache-Control"] == "no-store"
    assert journal.summary(authority()).head == ref


@pytest.mark.asyncio
@pytest.mark.parametrize(("target", "expected"), [("b", 409), ("missing", 404), ("c", 404)])
async def test_missing_closed_and_foreign_plan_ids_do_not_mutate(
    aiohttp_client, state_dir, static_dir, journal, target, expected
):
    ref = seed(journal)
    client = await create(aiohttp_client, state_dir, static_dir)
    response = await post(client, payload(ref), f"/api/v0/projects/p1/sessions/{target}/plan")
    assert response.status == expected
    assert journal.summary(authority()).head == ref
    assert ProjectStore(state_dir).get("p1").sessions["b"].closed_at is not None


@pytest.mark.asyncio
async def test_terminal_refuses_new_edit_and_awaiting_approval_is_not_activated(
    aiohttp_client, state_dir, static_dir, journal
):
    ref = seed(journal)
    journal.set_status(authority(), "waiting", ref, 2, 0, "awaiting_approval", "Recorded wait")
    client = await create(aiohttp_client, state_dir, static_dir)
    response = await post(client, payload(ref))
    assert response.status == 200
    receipt = plan_edits.EditReceipt.model_validate_json(await response.read())
    assert journal.summary(authority()).status == "awaiting_approval"
    journal.set_status(authority(), "abandon", receipt.receipt.ref, 3, 0, "abandoned", "Stop")
    assert (await post(client, payload(receipt.receipt.ref, operation="new"))).status == 409
    # An exact old operation can still acknowledge a historical commit while open.
    assert (await post(client, payload(ref))).status == 200


@pytest.mark.asyncio
async def test_auth_origin_host_and_request_bounds_are_no_store(
    aiohttp_client, state_dir, static_dir, journal
):
    ref = seed(journal)
    client = await create(aiohttp_client, state_dir, static_dir, login=False)
    response = await post(client, payload(ref))
    assert response.status == 401
    assert response.headers["Cache-Control"] == "no-store"
    await client.get("/login", params={"token": TOKEN}, allow_redirects=False)
    for headers in ({}, {"Origin": "http://foreign.invalid"}, {"Host": "foreign.invalid"}):
        response = await client.post(PATH + "/changes", json=payload(ref), headers=headers)
        assert response.status == 403
        assert response.headers["Cache-Control"] == "no-store"
    response = await client.post(
        PATH + "/changes",
        data=b"x" * (plan_edits.MAX_REQUEST_BYTES + 1),
        headers={**origin(client), "Content-Type": "application/json"},
    )
    assert response.status == 413
    assert response.headers["Cache-Control"] == "no-store"
    response = await client.post(
        PATH + "/changes?not-scope=1", json=payload(ref), headers=origin(client)
    )
    assert response.status == 400
    assert response.headers["Cache-Control"] == "no-store"
    assert journal.summary(authority()).head == ref


@pytest.mark.asyncio
async def test_body_larger_than_app_default_but_within_core_cap_is_accepted(
    aiohttp_client, state_dir, static_dir, journal
):
    ref = seed(journal)
    request = payload(ref)
    # Whitespace is not retained body content; route cap must include the envelope.
    raw = json.dumps(request).encode() + b" " * 70_000
    assert 65_536 < len(raw) < plan_edits.MAX_REQUEST_BYTES
    client = await create(aiohttp_client, state_dir, static_dir)
    response = await client.post(
        PATH + "/changes", data=raw, headers={**origin(client), "Content-Type": "application/json"}
    )
    assert response.status == 200
    assert journal.summary(authority()).head.version == 2


@pytest.mark.asyncio
async def test_get_and_refused_absent_edit_do_not_initialize_plan_sql(
    aiohttp_client, state_dir, static_dir
):
    client = await create(aiohttp_client, state_dir, static_dir)
    assert (await client.get(PATH)).status == 200
    request = payload(SimpleNamespace(model_dump=lambda **_: {"plan_id": "plan", "version": 1}))
    assert (await post(client, request)).status == 404
    database = sqlite3.connect(store_path(state_dir))
    assert database.execute("SELECT count(*) FROM session_plan_records").fetchone() == (0,)
    database.close()


@pytest.mark.asyncio
async def test_missing_pointer_is_corruption_not_absence(
    aiohttp_client, state_dir, static_dir, journal
):
    ref = seed(journal)
    with sqlite3.connect(store_path(state_dir)) as db:
        db.execute("DELETE FROM session_plan_projections WHERE kind='current'")
    client = await create(aiohttp_client, state_dir, static_dir)
    assert (await post(client, payload(ref))).status == 503


@pytest.mark.asyncio
async def test_a_close_waits_for_an_edit_in_progress(
    aiohttp_client, state_dir, static_dir, journal, monkeypatch
):
    ref = seed(journal)
    entered, release = threading.Event(), threading.Event()
    revise = SessionPlans.revise

    def held(self, *args, **kwargs):
        entered.set()
        assert release.wait(5)
        return revise(self, *args, **kwargs)

    monkeypatch.setattr(SessionPlans, "revise", held)
    client = await create(aiohttp_client, state_dir, static_dir)
    edit = asyncio.create_task(post(client, payload(ref)))
    assert await asyncio.to_thread(entered.wait, 5)
    store = ProjectStore(state_dir)
    closer = threading.Thread(target=store.close_session, args=("p1", "a"))
    closer.start()
    try:
        # The edit holds the project lock, so the close cannot land before it commits.
        await asyncio.to_thread(closer.join, 0.2)
        assert closer.is_alive()
        assert store.get("p1").sessions["a"].closed_at is None
    finally:
        release.set()
    assert (await edit).status == 200
    await asyncio.to_thread(closer.join, 5)
    assert store.get("p1").sessions["a"].closed_at is not None
    summary = journal.summary(authority())
    assert summary is not None
    assert summary.head.version == 2
    assert (await post(client, payload(summary.head, operation="after-close"))).status == 409


@pytest.mark.asyncio
async def test_failed_event_append_rolls_back_version_receipt_and_projection(
    aiohttp_client, state_dir, static_dir, journal
):
    ref = seed(journal)
    client = await create(aiohttp_client, state_dir, static_dir)
    with sqlite3.connect(store_path(state_dir)) as db:
        before = db.execute("SELECT count(*) FROM session_plan_records").fetchone()
        db.execute(
            "CREATE TRIGGER test_event_failure BEFORE INSERT ON session_plan_records "
            "WHEN NEW.kind='event' BEGIN SELECT RAISE(ABORT,'test-secret-not-exported'); END"
        )
    response = await post(client, payload(ref))
    assert response.status == 503
    assert "test-secret" not in await response.text()
    assert journal.summary(authority()).head == ref
    with sqlite3.connect(store_path(state_dir)) as db:
        assert db.execute("SELECT count(*) FROM session_plan_records").fetchone() == before
        db.execute("DROP TRIGGER test_event_failure")
    assert (await post(client, payload(ref))).status == 200


@pytest.mark.asyncio
async def test_core_body_limit_and_duplicate_step_intent_cannot_add_version(
    aiohttp_client, state_dir, static_dir, journal
):
    ref = seed(journal)
    client = await create(aiohttp_client, state_dir, static_dir)
    request = payload(ref)
    step = request["body"]["phases"][0]["steps"][0]
    request["body"]["phases"][0]["steps"] = [
        {**step, "step_id": f"s-{i}", "title": f"Step {i}", "description": "x" * 4096}
        for i in range(17)
    ]
    assert 65_536 < len(json.dumps(request).encode()) < plan_edits.MAX_REQUEST_BYTES
    assert (await post(client, request)).status == 400
    request = payload(ref)
    request["body"]["phases"][0]["steps"][1]["title"] = "ANALYZE"
    assert (await post(client, request)).status == 400
    assert journal.summary(authority()).head == ref


@pytest.mark.asyncio
async def test_actual_http_user_edit_requires_successful_main_current_read(
    aiohttp_client, state_dir, static_dir, journal, monkeypatch
):
    ref = seed(journal)
    projects = ProjectStore(state_dir)
    context = ExecutionContext(
        project_id="p1",
        session_id="a",
        environment_id=projects.get("p1").sessions["a"].environment_id,
        policy_revision=projects.get("p1").policy,
    )
    adapter = PlanTools(journal, projects, context, run_id="main-run", alias="main")
    client = await create(aiohttp_client, state_dir, static_dir)
    toolbox = Toolbox(cast(WorkerClient, SimpleNamespace()), context, plans=adapter)
    dispatched = []

    async def synthetic_shell(request):
        dispatched.append(request.operation)
        return ToolOutcome('{"observed":"synthetic"}', {"observed": "synthetic"})

    shell = ToolEntry(toolbox.entries["shell"].spec, "effect", synthetic_shell)
    monkeypatch.setitem(toolbox.entries, "shell", shell)
    await adapter.safe_point()
    await adapter.call("plan_read", "{}", call_id="initial-read", model="scripted")
    await adapter.model_delivered()

    class Model(ScriptedModel):
        async def complete(self, messages, tools):
            if not self.requests:
                assert (await post(client, payload(ref))).status == 200
            return await super().complete(messages, tools)

    model = Model(
        [
            ModelTurn(
                None,
                (
                    ToolCall("read", "plan_read", "{}"),
                    ToolCall("too-early", "shell", '{"command":"synthetic"}'),
                ),
            ),
            ModelTurn(None, (ToolCall("after-delivery", "shell", '{"command":"synthetic"}'),)),
            ModelTurn("Finished the authorized work"),
        ]
    )
    try:
        result = await run_task(model, toolbox, "Follow USER steering", run_id="main-run")
        assert result.stopped == "answered"
        assert len(dispatched) == 1
        assert any(
            "plan_reread_required" in (message["content"] or "") for message in model.requests[1]
        )
        assert result.tool_records[-1]["observed"] == "synthetic"
        assert journal.compact_current(authority()).version.author.role == "user"
        assert len(projects.runs("p1", "a")) == 2
    finally:
        adapter.close()
