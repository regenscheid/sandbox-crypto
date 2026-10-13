# SPDX-License-Identifier: MIT
"""USER body CAS, real HTTP/MAIN fences and actual mutation ownership."""

from __future__ import annotations

import asyncio
import fcntl
import json
import os
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
    PlanLimits,
    PlanScope,
    SessionPlans,
)
from sandbox_science.orchestrator.tools import Toolbox, ToolOutcome
from sandbox_science.orchestrator.worker_client import WorkerClient
from sandbox_science.workspace import plan_edits, plans
from sandbox_science.workspace.app import create_app

TOKEN = "test-secret-plan-edit-login-0123456789"  # noqa: S105
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
    assert not store_path(state_dir).exists()
    assert (await client.get(PATH)).status == 200
    request = payload(SimpleNamespace(model_dump=lambda **_: {"plan_id": "plan", "version": 1}))
    assert (await post(client, request)).status == 503
    assert not store_path(state_dir).exists()


@pytest.mark.asyncio
async def test_quota_corruption_and_io_failure_leave_body_unchanged(
    aiohttp_client, state_dir, static_dir, journal, monkeypatch
):
    ref = seed(journal)
    client = await create(aiohttp_client, state_dir, static_dir)
    editor = client.server.app[plan_edits.EDITOR]
    assert (await post(client, payload(ref))).status == 200
    second = journal.summary(authority()).head
    editor._plans.limits = PlanLimits(project_versions=1)
    assert (await post(client, payload(second, operation="quota"))).status == 400
    assert journal.summary(authority()).head == second
    editor._plans.limits = PlanLimits()

    def fail(*args, **kwargs):
        raise sqlite3.OperationalError("test-secret-not-for-logs")

    monkeypatch.setattr(editor._plans, "revise", fail)
    response = await post(client, payload(second, operation="io"))
    assert response.status == 503
    assert "test-secret" not in await response.text()
    assert journal.summary(authority()).head == second


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
@pytest.mark.parametrize("kind", ["symlink", "fifo", "directory", "held", "hardlink"])
async def test_project_lock_special_files_or_other_owner_are_refused(
    aiohttp_client, state_dir, static_dir, journal, kind, tmp_path
):
    ref = seed(journal)
    path = ProjectStore(state_dir).root / "p1.lock"
    path.unlink()
    owner = None
    if kind == "symlink":
        target = tmp_path / "target"
        target.write_text("Leave this file unchanged")
        path.symlink_to(target)
    elif kind == "fifo":
        os.mkfifo(path)
    elif kind == "directory":
        path.mkdir()
    else:
        path.touch()
        if kind == "hardlink":
            os.link(path, tmp_path / "linked-lock")
        else:
            owner = path.open("a")
            fcntl.flock(owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
    client = await create(aiohttp_client, state_dir, static_dir)
    try:
        response = await asyncio.wait_for(post(client, payload(ref)), 1)
        assert response.status == 503
        assert journal.summary(authority()).head == ref
    finally:
        if owner is not None:
            owner.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("after_commit", [False, True])
async def test_original_cookie_revocation_relogin_and_postadmission_unknown(
    aiohttp_client, state_dir, static_dir, journal, monkeypatch, after_commit
):
    ref = seed(journal)
    client = await create(aiohttp_client, state_dir, static_dir)
    editor = client.server.app[plan_edits.EDITOR]
    entered, release = threading.Event(), threading.Event()
    if after_commit:
        original_revise = SessionPlans.revise

        def blocked_revise(self, *args, **kwargs):
            result = original_revise(self, *args, **kwargs)
            entered.set()
            assert release.wait(5)
            return result

        monkeypatch.setattr(SessionPlans, "revise", blocked_revise)
    else:
        original_bind = editor._bind_database

        def blocked_bind(*args, **kwargs):
            original_bind(*args, **kwargs)
            entered.set()
            assert release.wait(5)

        monkeypatch.setattr(editor, "_bind_database", blocked_bind)
    call = asyncio.create_task(post(client, payload(ref)))
    try:
        assert await asyncio.to_thread(entered.wait, 3)
        assert (await client.post("/api/v0/logout", headers=origin(client))).status == 204
        await client.get("/login", params={"token": TOKEN}, allow_redirects=False)
    finally:
        release.set()
    response = await call
    assert response.status == 401
    assert journal.summary(authority()).head.version == (2 if after_commit else 1)


@pytest.mark.asyncio
async def test_request_body_deadline_and_capacity_are_owned_before_sql(
    state_dir, journal, monkeypatch
):
    ref = seed(journal)
    canonical = DocumentStore(store_path(state_dir), readonly=True)
    editor = plan_edits.PlanEditor(ProjectStore(state_dir), canonical)
    entered = asyncio.Event()

    async def slow():
        entered.set()
        await asyncio.Event().wait()
        return json.dumps(payload(ref)).encode()

    monkeypatch.setattr(plan_edits, "CALL_TIMEOUT", 0.03)
    first = asyncio.create_task(editor.change("p1", "a", "login", slow, lambda: True))
    await entered.wait()
    try:
        with pytest.raises(Exception, match="capacity"):
            await editor.change("p1", "a", "login", slow, lambda: True)
        with pytest.raises(Exception, match="timed out"):
            await first
        await asyncio.sleep(0)
        assert not editor._tasks
        assert journal.summary(authority()).head == ref
    finally:
        await editor.close()
        canonical.close()


@pytest.mark.asyncio
async def test_admitted_sql_repeated_cancel_and_shutdown_keep_true_owner(
    state_dir, journal, monkeypatch
):
    ref = seed(journal)
    canonical = DocumentStore(store_path(state_dir), readonly=True)
    editor = plan_edits.PlanEditor(ProjectStore(state_dir), canonical)
    entered, release = threading.Event(), threading.Event()
    original = SessionPlans.revise

    def blocked(self, *args, **kwargs):
        entered.set()
        assert release.wait(5)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(SessionPlans, "revise", blocked)

    async def read():
        return json.dumps(payload(ref)).encode()

    call = asyncio.create_task(editor.change("p1", "a", "login", read, lambda: True))
    assert await asyncio.to_thread(entered.wait, 3)
    database = editor._database
    try:
        call.cancel()
        call.cancel()
        with pytest.raises(asyncio.CancelledError):
            await call
        assert len(editor._tasks) == 1
        assert editor._scopes == {("p1", "a")}
        os.fstat(database)
        closing = asyncio.create_task(editor.close())
        await asyncio.sleep(0.01)
        closing.cancel()
        closing.cancel()
        await asyncio.sleep(0.01)
        assert not closing.done()
        assert not editor._closed
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await closing
    assert editor._closed
    assert not editor._tasks
    assert not editor._scopes
    with pytest.raises(OSError, match="Bad file descriptor"):
        os.fstat(database)
    assert journal.summary(authority()).head.version == 2
    canonical.close()


@pytest.mark.asyncio
async def test_registry_close_reopen_during_request_refuses_but_fresh_logical_edit_is_allowed(
    aiohttp_client, state_dir, static_dir, journal, monkeypatch
):
    ref = seed(journal)
    client = await create(aiohttp_client, state_dir, static_dir)
    snapshot = plans.session_snapshot
    changed = False

    def replaced(store, project, session):
        nonlocal changed
        original = snapshot(store, project, session)
        if not changed:
            changed = True
            store.close_session(project, session)
            store.open_session(project, session, dedicated=False)
        return original

    monkeypatch.setattr(plan_edits, "session_snapshot", replaced)
    assert (await post(client, payload(ref))).status == 409
    assert journal.summary(authority()).head == ref
    # The route deliberately has no cross-request opaque open-lifetime token.
    assert (await post(client, payload(ref))).status == 200


@pytest.mark.asyncio
@pytest.mark.parametrize("damage", ["symlink", "fifo", "directory", "oversize", "scope"])
async def test_bounded_registry_damage_refuses_before_mutation(
    aiohttp_client, state_dir, static_dir, journal, damage, tmp_path
):
    ref = seed(journal)
    client = await create(aiohttp_client, state_dir, static_dir)
    path = ProjectStore(state_dir).root / "p1.json"
    raw = path.read_bytes()
    path.unlink()
    if damage == "symlink":
        target = tmp_path / "registry"
        target.write_bytes(raw)
        path.symlink_to(target)
    elif damage == "fifo":
        os.mkfifo(path)
    elif damage == "directory":
        path.mkdir()
    elif damage == "oversize":
        path.write_bytes(b" " * (1024 * 1024 + 1))
    else:
        record = json.loads(raw)
        record["project_id"] = "p2"
        path.write_text(json.dumps(record))
    assert (await asyncio.wait_for(post(client, payload(ref)), 1)).status == 503
    assert journal.summary(authority()).head == ref


@pytest.mark.asyncio
@pytest.mark.parametrize("damage", ["symlink", "fifo", "directory"])
async def test_control_special_file_refuses_without_open_or_create(
    aiohttp_client, state_dir, static_dir, journal, damage
):
    ref = seed(journal)
    journal.documents.close()
    path = store_path(state_dir)
    backup = path.with_suffix(".original")
    path.rename(backup)
    if damage == "symlink":
        path.symlink_to(backup)
    elif damage == "fifo":
        os.mkfifo(path)
    else:
        path.mkdir()
    client = await create(aiohttp_client, state_dir, static_dir)
    try:
        assert (await asyncio.wait_for(post(client, payload(ref)), 1)).status == 503
        assert client.app[plan_edits.EDITOR]._database == -1
    finally:
        if path.is_dir():
            path.rmdir()
        else:
            path.unlink()
        backup.rename(path)
    assert journal.summary(authority()).head == ref


@pytest.mark.asyncio
async def test_sql_busy_is_immediate_and_exact_retry_can_commit_after_release(
    aiohttp_client, state_dir, static_dir, journal
):
    ref = seed(journal)
    client = await create(aiohttp_client, state_dir, static_dir)
    with sqlite3.connect(store_path(state_dir), isolation_level=None) as other:
        other.execute("BEGIN IMMEDIATE")
        try:
            assert (await asyncio.wait_for(post(client, payload(ref)), 1)).status == 503
            assert journal.summary(authority()).head == ref
        finally:
            other.execute("ROLLBACK")
    assert (await post(client, payload(ref))).status == 200


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
async def test_postcommit_registry_failure_is_unknown_and_exact_retry_recovers_receipt(
    aiohttp_client, state_dir, static_dir, journal, monkeypatch
):
    ref = seed(journal)
    client = await create(aiohttp_client, state_dir, static_dir)
    editor = client.app[plan_edits.EDITOR]
    original = editor._current
    checks = 0

    def failed_receipt_check(snapshot):
        nonlocal checks
        checks += 1
        if checks == 3:
            raise FileNotFoundError("test-secret-not-exported")
        original(snapshot)

    monkeypatch.setattr(editor, "_current", failed_receipt_check)
    response = await post(client, payload(ref))
    assert response.status == 503
    assert "test-secret" not in await response.text()
    assert journal.summary(authority()).head.version == 2
    assert (await post(client, payload(ref))).status == 200


@pytest.mark.asyncio
async def test_invalid_returned_receipt_after_commit_is_not_an_input_refusal(
    aiohttp_client, state_dir, static_dir, journal, monkeypatch
):
    ref = seed(journal)
    original = SessionPlans.revise

    def invalid(self, *args, **kwargs):
        return original(self, *args, **kwargs).model_copy(update={"action": "publish"})

    monkeypatch.setattr(SessionPlans, "revise", invalid)
    client = await create(aiohttp_client, state_dir, static_dir)
    assert (await post(client, payload(ref))).status == 503
    assert journal.summary(authority()).head.version == 2


@pytest.mark.asyncio
async def test_global_and_login_capacity_include_body_waits_and_release_after_actual_cancel(
    state_dir, journal, monkeypatch
):
    seed(journal)
    canonical = DocumentStore(store_path(state_dir), readonly=True)
    editor = plan_edits.PlanEditor(ProjectStore(state_dir), canonical)

    async def wait_body():
        await asyncio.Event().wait()
        return b"{}"

    calls = [
        asyncio.create_task(editor.change("p1", name, "login", wait_body, lambda: True))
        for name in ("a", "c")
    ]
    try:
        await asyncio.sleep(0)
        with pytest.raises(Exception, match="capacity"):
            await editor.change("p1", "d", "login", wait_body, lambda: True)
        monkeypatch.setattr(plan_edits, "MAX_CALLS", 2)
        with pytest.raises(Exception, match="capacity"):
            await editor.change("p1", "d", "another-login", wait_body, lambda: True)
        assert len(editor._tasks) == 2
    finally:
        for call in calls:
            call.cancel()
        await asyncio.gather(*calls, return_exceptions=True)
        await editor.close()
        canonical.close()
    assert not editor._tasks
    assert not editor._scopes
    assert not editor._logins


@pytest.mark.asyncio
async def test_actual_http_disconnect_before_write_refuses_and_drains_owned_preparation(
    aiohttp_client, state_dir, static_dir, journal, monkeypatch
):
    ref = seed(journal)
    entered, release = threading.Event(), threading.Event()
    original = plan_edits.PlanEditor._bind_database

    def blocked(self, actor):
        original(self, actor)
        entered.set()
        assert release.wait(5)

    monkeypatch.setattr(plan_edits.PlanEditor, "_bind_database", blocked)
    client = await create(aiohttp_client, state_dir, static_dir)
    editor = client.app[plan_edits.EDITOR]
    call = asyncio.create_task(post(client, payload(ref)))
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        call.cancel()
        await asyncio.gather(call, return_exceptions=True)
        # Peer cancellation does not prove this synchronous owner has stopped.
        await asyncio.sleep(0.03)
        assert len(editor._tasks) == 1
        assert editor._scopes == {("p1", "a")}
        release.set()
        for _ in range(100):
            if not editor._tasks:
                break
            await asyncio.sleep(0.002)
        assert not editor._tasks
        assert journal.summary(authority()).head == ref
    finally:
        release.set()
        await editor.close()


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

    async def synthetic_shell(args, operation):
        dispatched.append(operation)
        return ToolOutcome('{"observed":"synthetic"}', {"observed": "synthetic"})

    monkeypatch.setattr(toolbox, "_shell", synthetic_shell)
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
