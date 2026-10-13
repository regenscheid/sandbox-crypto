# SPDX-License-Identifier: MIT
"""Real scoped control transactions and owned threads; no model or worker effects."""

from __future__ import annotations

import asyncio
import os
import threading
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
import pytest_asyncio

from sandbox_science.contracts import ExecutionContext
from sandbox_science.orchestrator.delegated_plan_tools import DelegatedPlanProgress
from sandbox_science.orchestrator.delegation_records import (
    DelegationBrief,
    DelegationRecords,
    DelegationScope,
    HandbackAuthority,
    HostObservation,
    ParentAuthority,
    ProfileIntent,
    RequestedLimits,
    StepPin,
    TrackPin,
)
from sandbox_science.orchestrator.documents.errors import POLICY_DENIED, DocumentError
from sandbox_science.orchestrator.documents.store import DocumentStore
from sandbox_science.orchestrator.plan_tools import Progress
from sandbox_science.orchestrator.projects import ProjectStore
from sandbox_science.orchestrator.session_plans import (
    PlanAuthority,
    PlanBody,
    PlanProgress,
    PlanRef,
    PlanScope,
    SessionPlans,
    _steps,
)

pytestmark = pytest.mark.asyncio


def body(description="Inspect assumptions"):
    return PlanBody.model_validate(
        {
            "goal": "Check the claim",
            "feasibility": "Use scoped evidence",
            "outputs": (
                {"title": "Result", "description": "Claim", "evidence_standard": "Reproduce"},
            ),
            "tracks": ({"track_id": "a", "title": "Analysis"}, {"track_id": "b", "title": "Other"}),
            "phases": (
                {
                    "phase_id": "phase",
                    "title": "Work",
                    "steps": (
                        {
                            "step_id": "one",
                            "title": "Analyze",
                            "description": description,
                            "evidence_standard": "Derive",
                            "track_id": "a",
                        },
                        {
                            "step_id": "two",
                            "title": "Independent",
                            "description": "Other work",
                            "evidence_standard": "Check",
                            "track_id": "b",
                        },
                    ),
                },
            ),
        }
    )


@pytest_asyncio.fixture
async def fixture(tmp_path):
    projects = ProjectStore(tmp_path / "state")
    projects.create("p", "minimal", "sha256:" + "a" * 64)
    session = projects.open_session("p", "s", dedicated=False)
    context = ExecutionContext(
        project_id="p", session_id="s", environment_id=session.environment_id
    )
    documents = DocumentStore(tmp_path / "state/control.sqlite3")
    plans = SessionPlans(documents)
    main = PlanAuthority(
        scope=PlanScope(project_id="p", session_id="s"), role="main", run_id="parent", alias="main"
    )
    created = plans.publish(main, "create", body(), "Initial")
    plans.set_status(main, "activate", created.ref, 1, 0, "active", "Existing authorization")
    records = DelegationRecords(documents, initialize=True)
    parent = ParentAuthority(
        scope=DelegationScope(
            project_id="p", session_id="s", environment_id=session.environment_id
        ),
        parent_run_id="parent",
    )
    value = plans.version(main, created.ref)
    prepared = records.prepare(
        parent,
        "prepare",
        DelegationBrief(
            task="Analyze own track",
            context_summary="Synthetic trusted host fixture",
            track=TrackPin(
                plan_id=created.ref.plan_id,
                version=created.ref.version,
                track_id="a",
                steps=(StepPin(step_id="one", sha256=_steps(value.body)["one"][1]),),
            ),
            inputs=(),
            profile=ProfileIntent(
                profile_id="explicit", profile_sha256="b" * 64, model_alias="child"
            ),
            limits=RequestedLimits(turns=2, wall_seconds=30, output_tokens=128),
        ),
    )
    authority = PlanAuthority(
        scope=main.scope,
        role="delegated",
        run_id=prepared.child_run_id,
        alias="child",
        grant=created.ref,
        track_id="a",
    )
    original = projects.session_snapshot("p", "s")
    admission = {"allowed": True}
    adapters = []

    async def opened(**overrides):
        args = {
            "authority": authority,
            "prepared": prepared,
            "original_session": original,
            "admitted": lambda: admission["allowed"],
        }
        args.update(overrides)
        adapter = await DelegatedPlanProgress.open(plans, records, projects, context, **args)
        adapters.append(adapter)
        return adapter

    f = SimpleNamespace(
        projects=projects,
        context=context,
        documents=documents,
        plans=plans,
        main=main,
        records=records,
        parent=parent,
        ref=created.ref,
        prepared=prepared,
        authority=authority,
        original=original,
        admission=admission,
        opened=opened,
        adapters=adapters,
    )
    yield f
    for adapter in adapters:
        await adapter.close()
    original.close()
    documents.close()


def request(f, **updates):
    fields = {
        "operation_id": "progress",
        "base": f.ref,
        "step_id": "one",
        "expected_step_cursor": 0,
        "expected_status_revision": 2,
        "status": "in_progress",
        "note": "Work started",
        "evidence": (),
    }
    fields.update(updates)
    return Progress(**fields)


async def ready(adapter):
    current = await adapter.read_current()
    await adapter.model_delivered(current)
    return current


async def test_live_progress_is_durable_and_own_track_only(fixture):
    f = fixture
    adapter = await f.opened()
    current = await ready(adapter)
    assert [step.step.step_id for step in current.steps] == ["one"]
    assert current.steps[0].status == "pending"
    receipt = await adapter.progress(request(f), resolved_model="actual-model")
    reader = DocumentStore(f.documents.path, readonly=True)
    try:
        latest = SessionPlans(reader).compact_current(f.main)
        assert latest is not None
        assert latest.steps[0].latest is not None
        assert latest.steps[0].latest.status == "in_progress"
        assert latest.steps[0].latest.author.run_id == f.prepared.child_run_id
        assert latest.steps[0].latest.author.resolved_model == "actual-model"
    finally:
        reader.close()
    assert receipt.progress_cursor == 1
    with pytest.raises(DocumentError, match="outside"):
        await adapter.progress(
            request(f, operation_id="foreign-step", step_id="two"), resolved_model=None
        )
    assert not hasattr(adapter, "activate")
    assert not hasattr(adapter, "finish")


async def test_read_or_history_never_clears_delivery_fence(fixture):
    f = fixture
    adapter = await f.opened()
    with pytest.raises(DocumentError, match="successfully deliver"):
        await adapter.progress(request(f), resolved_model=None)
    current = await adapter.read_current()
    await adapter.history("one")
    with pytest.raises(DocumentError, match="successfully deliver"):
        await adapter.progress(request(f), resolved_model=None)
    with pytest.raises(DocumentError, match="exact"):
        await adapter.model_delivered(current)
    await ready(adapter)
    await adapter.progress(request(f), resolved_model=None)


async def test_exact_replay_and_changed_model_conflict(fixture):
    f = fixture
    adapter = await f.opened()
    await ready(adapter)
    first = await adapter.progress(request(f), resolved_model="model-a")
    replay = await adapter.progress(request(f), resolved_model="model-a")
    assert first == replay
    with pytest.raises(DocumentError, match="operation ID"):
        await adapter.progress(request(f), resolved_model="model-b")
    await ready(adapter)
    with pytest.raises(DocumentError, match="operation ID"):
        await adapter.progress(request(f, note="changed"), resolved_model="model-a")
    current = f.plans.compact_current(f.main)
    assert current is not None
    assert current.head.progress_cursor == 1


@pytest.mark.parametrize(
    ("field", "value"), [("role", "main"), ("run_id", "other-child"), ("track_id", "b")]
)
async def test_wrong_explicit_authority_refuses(fixture, field, value):
    f = fixture
    with pytest.raises(DocumentError):
        await f.opened(authority=f.authority.model_copy(update={field: value}))


async def test_prepare_and_original_session_are_canonical(fixture):
    f = fixture
    with pytest.raises(DocumentError):
        await f.opened(prepared=f.prepared.model_copy(update={"request_sha256": "c" * 64}))
    f.projects.open_session("p", "other", dedicated=False)
    other = f.projects.session_snapshot("p", "other")
    try:
        with pytest.raises(DocumentError, match="session"):
            await f.opened(original_session=other)
    finally:
        other.close()


@pytest.mark.parametrize("allowed", [False, 1, "yes", None])
async def test_admission_requires_exact_true(fixture, allowed):
    f = fixture
    f.admission["allowed"] = allowed
    with pytest.raises(DocumentError, match="admission"):
        await f.opened()


async def test_callback_exception_is_fixed(fixture):
    f = fixture

    def raised():
        raise ValueError("synthetic-private-not-for-output")

    with pytest.raises(DocumentError, match="admission is unavailable") as caught:
        await f.opened(admitted=raised)
    assert "synthetic-private" not in str(caught.value)


async def test_user_revision_invalidates_original_grant(fixture):
    f = fixture
    adapter = await f.opened()
    read = await adapter.read_current()
    f.plans.revise(
        f.main, "change", f.ref, body("Changed intent"), "User-equivalent canonical change"
    )
    with pytest.raises(DocumentError, match="no longer current"):
        await adapter.model_delivered(read)
    with pytest.raises(DocumentError):
        await adapter.progress(request(f), resolved_model=None)
    assert (await adapter.safe_point())["planning"] == "held"


async def test_delivered_read_copy_or_mutation_cannot_clear(fixture):
    f = fixture
    adapter = await f.opened()
    read = await adapter.read_current()
    with pytest.raises(DocumentError, match="exact"):
        await adapter.model_delivered(read.model_copy())
    read = await adapter.read_current()
    object.__setattr__(read, "child_run_id", "replaced")
    with pytest.raises(DocumentError, match="content changed"):
        await adapter.model_delivered(read)
    with pytest.raises(DocumentError):
        await adapter.progress(request(f), resolved_model=None)


async def test_other_actor_progress_requires_new_successful_read(fixture):
    f = fixture
    adapter = await f.opened()
    await ready(adapter)
    f.plans.progress(
        f.main,
        "other-track",
        f.ref,
        "two",
        0,
        "in_progress",
        "Other work",
        expected_status_revision=2,
    )
    with pytest.raises(DocumentError, match="successfully deliver"):
        await adapter.progress(request(f), resolved_model=None)
    await ready(adapter)
    await adapter.progress(request(f), resolved_model=None)


async def test_close_reopen_registry_fence(fixture):
    f = fixture
    adapter = await f.opened()
    await ready(adapter)
    f.projects.close_session("p", "s")
    f.projects.open_session("p", "s", dedicated=False)
    with pytest.raises(DocumentError, match="session"):
        await adapter.progress(request(f), resolved_model=None)


async def test_admission_and_status_rechecked_after_writer_admission(fixture, monkeypatch):
    f = fixture
    adapter = await f.opened()
    await ready(adapter)
    entered, release = threading.Event(), threading.Event()
    original = f.documents._write

    @contextmanager
    def paused():
        entered.set()
        assert release.wait(3)
        with original() as db:
            yield db

    monkeypatch.setattr(f.documents, "_write", paused)
    task = asyncio.create_task(adapter.progress(request(f), resolved_model=None))
    assert await asyncio.to_thread(entered.wait, 2)
    other = DocumentStore(f.documents.path)
    other_plans = SessionPlans(other)
    try:
        other_plans.set_status(f.main, "wait", f.ref, 2, 0, "awaiting_approval", "Explicit hold")
    finally:
        other.close()
    release.set()
    with pytest.raises(DocumentError, match="active"):
        await task
    assert f.plans.summary(f.main).progress_cursor == 0


async def test_cancel_after_commit_retains_progress_and_close_drains_fd(fixture, monkeypatch):
    f = fixture
    adapter = await f.opened()
    await ready(adapter)
    entered, release = threading.Event(), threading.Event()
    original = f.documents._write

    @contextmanager
    def committed_then_blocked():
        with original() as db:
            yield db
        entered.set()
        assert release.wait(3)

    monkeypatch.setattr(f.documents, "_write", committed_then_blocked)
    task = asyncio.create_task(adapter.progress(request(f), resolved_model="actual"))
    assert await asyncio.to_thread(entered.wait, 2)
    snapshot = adapter._snapshot
    assert snapshot is not None
    descriptor = snapshot._descriptor
    task.cancel("first")
    await asyncio.sleep(0)
    task.cancel("second")
    close = asyncio.create_task(adapter.close())
    await asyncio.sleep(0)
    assert not task.done()
    assert not close.done()
    os.fstat(descriptor)
    release.set()
    with pytest.raises(asyncio.CancelledError) as caught:
        await task
    assert caught.value.args == ("first",)
    await close
    with pytest.raises(OSError, match="Bad file descriptor"):
        os.fstat(descriptor)
    os.fstat(f.original._descriptor)  # borrowed caller snapshot was not closed
    current = f.plans.summary(f.main)
    assert current is not None
    assert current.progress_cursor == 1


async def test_no_schema_initialization_or_borrowed_close(fixture):
    f = fixture
    statements = []
    f.documents._db.set_trace_callback(statements.append)
    try:
        adapter = await f.opened()
        await ready(adapter)
        await adapter.close()
    finally:
        f.documents._db.set_trace_callback(None)
    assert not any(
        sql.lstrip().upper().startswith(("CREATE", "ALTER", "INSERT", "UPDATE", "DELETE"))
        for sql in statements
    )
    assert f.plans.summary(f.main) is not None


@pytest.mark.parametrize(
    ("field", "value"),
    [("expected_step_cursor", True), ("expected_status_revision", 2.0), ("note", "\x7f" * 20000)],
)
async def test_copied_progress_refuses_before_writer(fixture, field, value):
    f = fixture
    adapter = await f.opened()
    await ready(adapter)
    copied = request(f).model_copy(update={field: value})
    with pytest.raises(DocumentError):
        await adapter.progress(copied, resolved_model=None)
    summary = f.plans.summary(f.main)
    assert summary is not None
    assert summary.progress_cursor == 0


async def test_copied_authority_extra_and_nested_bool_refuse(fixture):
    f = fixture
    copied = f.authority.model_copy(update={f"extra-{i}": "synthetic" for i in range(2048)})
    with pytest.raises(DocumentError, match="undeclared"):
        await f.opened(authority=copied)
    grant = f.ref.model_copy(update={"version": True})
    with pytest.raises(DocumentError):
        await f.opened(authority=f.authority.model_copy(update={"grant": grant}))


@pytest.mark.parametrize("pretend", [PlanRef, tuple, list])
async def test_copied_type_cannot_impersonate_known_type(fixture, pretend):
    f = fixture
    visits = []

    class Pretend(type):
        def __hash__(cls):
            visits.append("hash")
            return hash(pretend)

        def __eq__(cls, other):
            visits.append("eq")
            return other is pretend

    class Copied(metaclass=Pretend):
        model_fields = PlanRef.model_fields

    foreign = Copied()
    vars(foreign).update(
        {
            "__pydantic_extra__": None,
            "plan_id": f.ref.plan_id,
            "version": 1,
        }
    )
    with pytest.raises(DocumentError, match="invalid delegated input type"):
        await f.opened(authority=f.authority.model_copy(update={"grant": foreign}))
    assert visits == []


async def test_model_delivered_other_actor_same_version_does_not_clear(fixture):
    f = fixture
    adapter = await f.opened()
    read = await adapter.read_current()
    f.plans.progress(
        f.main, "other", f.ref, "two", 0, "in_progress", "Concurrent", expected_status_revision=2
    )
    with pytest.raises(DocumentError, match="changed before"):
        await adapter.model_delivered(read)
    with pytest.raises(DocumentError):
        await adapter.progress(request(f), resolved_model=None)
    await ready(adapter)
    await adapter.progress(request(f), resolved_model=None)


async def test_many_preexisting_events_are_bounded_and_drainable(fixture):
    f = fixture
    for i in range(150):
        f.plans.progress(
            f.main,
            f"history-{i}",
            f.ref,
            "two",
            i,
            "in_progress",
            "Retained work",
            expected_status_revision=2,
        )
    adapter = await f.opened()
    assert adapter._cursor <= 64
    with pytest.raises(DocumentError):
        await adapter.progress(request(f), resolved_model=None)
    while (await adapter.safe_point())["event_scan_held"]:
        pass
    await ready(adapter)
    receipt = await adapter.progress(request(f), resolved_model=None)
    assert receipt.progress_cursor == 151


async def test_admission_revoked_inside_writer_rolls_back(fixture, monkeypatch):
    f = fixture
    adapter = await f.opened()
    await ready(adapter)
    original = adapter._view._finish

    def finish(*args, **kwargs):
        receipt = original(*args, **kwargs)
        f.admission["allowed"] = False
        return receipt

    monkeypatch.setattr(adapter._view, "_finish", finish)
    with pytest.raises(DocumentError, match="admission"):
        await adapter.progress(request(f), resolved_model=None)
    summary = f.plans.summary(f.main)
    assert summary is not None
    assert summary.progress_cursor == 0


async def test_rollback_cannot_hide_later_user_event_at_same_sequence(fixture):
    f = fixture
    adapter = await f.opened()
    await ready(adapter)
    original = adapter._view.documents.validate
    calls = 0

    def refused_after_scanning_uncommitted_event(db, write):
        nonlocal calls
        original(db, write)
        if write:
            calls += 1
            if calls == 2:
                assert adapter._cursor == 3
                raise DocumentError(POLICY_DENIED, "Synthetic final admission refusal")

    adapter._view.documents.validate = refused_after_scanning_uncommitted_event
    with pytest.raises(DocumentError, match="final admission"):
        await adapter.progress(request(f), resolved_model=None)
    assert adapter._cursor == 2
    assert adapter._pending is None
    assert adapter._requires_read is True
    summary = f.plans.summary(f.main)
    assert summary is not None
    assert summary.event_cursor == 2
    adapter._view.documents.validate = original
    user = PlanAuthority(scope=f.main.scope, role="user", user_id="human")
    f.plans.progress(
        user,
        "user-after-rollback",
        f.ref,
        "two",
        0,
        "in_progress",
        "User update",
        expected_status_revision=2,
    )
    with pytest.raises(DocumentError, match="successfully deliver"):
        await adapter.progress(request(f), resolved_model=None)
    await ready(adapter)
    receipt = await adapter.progress(request(f), resolved_model=None)
    assert receipt.progress_cursor == 2


@pytest.mark.parametrize("change", ["step", "version", "fingerprint"])
async def test_retained_history_decoded_scope_and_intent_refuse(fixture, change):
    f = fixture
    step = "two" if change == "step" else "one"
    f.plans.progress(
        f.main,
        "retained",
        f.ref,
        step,
        0,
        "in_progress",
        "Synthetic other-track note",
        expected_status_revision=2,
    )
    adapter = await f.opened()
    db = f.documents._db
    assert db is not None
    db.execute("DROP TRIGGER session_plan_records_no_update")
    if change == "step":
        db.execute("UPDATE session_plan_records SET key='one' WHERE kind='progress'")
    else:
        row = db.execute("SELECT record FROM session_plan_records WHERE kind='progress'").fetchone()
        assert row is not None
        value = PlanProgress.model_validate_json(row[0])
        updated = value.model_copy(
            update={
                "ref": f.ref.model_copy(update={"version": 2}) if change == "version" else f.ref,
                "step_sha256": "a" * 64 if change == "fingerprint" else value.step_sha256,
            }
        )
        db.execute(
            "UPDATE session_plan_records SET record=? WHERE kind='progress'",
            (updated.model_dump_json(),),
        )
    db.execute(
        "CREATE TRIGGER session_plan_records_no_update BEFORE UPDATE ON "
        "session_plan_records BEGIN SELECT RAISE(ABORT,'immutable'); END"
    )
    with pytest.raises(DocumentError, match="retained granted history"):
        await adapter.history("one")


async def test_canonical_child_finish_during_admission_refuses(fixture, monkeypatch):
    f = fixture
    adapter = await f.opened()
    await ready(adapter)
    entered, release = threading.Event(), threading.Event()
    original = f.documents._write

    @contextmanager
    def paused():
        entered.set()
        assert release.wait(3)
        with original() as db:
            yield db

    monkeypatch.setattr(f.documents, "_write", paused)
    task = asyncio.create_task(adapter.progress(request(f), resolved_model=None))
    assert await asyncio.to_thread(entered.wait, 2)
    other = DocumentStore(f.documents.path)
    try:
        records = DelegationRecords(other)
        records.finish(
            HandbackAuthority(
                scope=f.prepared.scope,
                parent_run_id=f.prepared.parent_run_id,
                delegation_id=f.prepared.delegation_id,
                child_run_id=f.prepared.child_run_id,
            ),
            "ended",
            1,
            HostObservation(ended="cancelled"),
            None,
        )
    finally:
        other.close()
        release.set()
    with pytest.raises(DocumentError, match="child changed or ended"):
        await task
    summary = f.plans.summary(f.main)
    assert summary is not None
    assert summary.progress_cursor == 0


async def test_callback_is_rechecked_inside_actual_writer_transaction(fixture):
    f = fixture
    seen = []

    def admitted():
        seen.append(f.documents._db.in_transaction)
        return True

    adapter = await f.opened(admitted=admitted)
    await ready(adapter)
    seen.clear()
    await adapter.progress(request(f), resolved_model=None)
    assert True in seen
    assert False in seen


async def test_one_actual_thread_lane_and_timeout_drain(fixture, monkeypatch):
    f = fixture
    adapter = await f.opened()
    await ready(adapter)
    entered, release = threading.Event(), threading.Event()
    original = f.documents._write

    @contextmanager
    def paused_after_commit():
        with original() as db:
            yield db
        entered.set()
        assert release.wait(3)

    monkeypatch.setattr(f.documents, "_write", paused_after_commit)
    task = asyncio.create_task(
        asyncio.wait_for(adapter.progress(request(f), resolved_model=None), 0.05)
    )
    assert await asyncio.to_thread(entered.wait, 2)
    with pytest.raises(DocumentError, match="actual thread"):
        await adapter.read_current()
    await asyncio.sleep(0.08)
    assert not task.done()
    release.set()
    with pytest.raises(TimeoutError):
        await task
    summary = f.plans.summary(f.main)
    assert summary is not None
    assert summary.progress_cursor == 1


async def test_canceled_factory_drains_and_closes_only_owned_snapshot(fixture, monkeypatch):
    f = fixture
    entered, release = threading.Event(), threading.Event()
    captured = []
    original = f.projects.session_snapshot

    def blocked(*args):
        snapshot = original(*args)
        captured.append(snapshot._descriptor)
        entered.set()
        assert release.wait(3)
        return snapshot

    monkeypatch.setattr(f.projects, "session_snapshot", blocked)
    task = asyncio.create_task(f.opened())
    assert await asyncio.to_thread(entered.wait, 2)
    task.cancel("first")
    await asyncio.sleep(0)
    task.cancel("second")
    await asyncio.sleep(0)
    assert not task.done()
    os.fstat(captured[0])
    release.set()
    with pytest.raises(asyncio.CancelledError) as caught:
        await task
    assert caught.value.args == ("first",)
    with pytest.raises(OSError, match="Bad file descriptor"):
        os.fstat(captured[0])
    os.fstat(f.original._descriptor)


async def test_current_projection_keeps_old_completion_historical(fixture):
    f = fixture
    f.plans.progress(
        f.main,
        "old-complete",
        f.ref,
        "one",
        0,
        "completed",
        "Old claim",
        expected_status_revision=2,
    )
    changed = f.plans.revise(f.main, "changed-intent", f.ref, body("New intent"), "USER edit")
    current = f.plans.version(f.main, changed.ref)
    prepared = f.records.prepare(
        f.parent,
        "new-prepare",
        DelegationBrief(
            task="Current task",
            context_summary="Explicit new child",
            track=TrackPin(
                plan_id=changed.ref.plan_id,
                version=changed.ref.version,
                track_id="a",
                steps=(StepPin(step_id="one", sha256=_steps(current.body)["one"][1]),),
            ),
            inputs=(),
            profile=ProfileIntent(profile_id="explicit", profile_sha256="b" * 64),
            limits=RequestedLimits(turns=2, wall_seconds=30, output_tokens=128),
        ),
    )
    authority = f.authority.model_copy(
        update={"run_id": prepared.child_run_id, "grant": changed.ref}
    )
    adapter = await f.opened(authority=authority, prepared=prepared)
    read = await ready(adapter)
    assert read.steps[0].status == "pending"
    assert read.steps[0].historical_assertion is True
    assert read.steps[0].current.latest is not None
    assert read.steps[0].current.latest.status == "completed"
    assert read.steps[0].current.latest.sequence == 1
    await adapter.progress(
        request(f, base=changed.ref, expected_step_cursor=1), resolved_model=None
    )


async def test_progress_completed_does_not_finish_or_achieve_goal(fixture):
    f = fixture
    adapter = await f.opened()
    await ready(adapter)
    await adapter.progress(request(f, status="completed"), resolved_model=None)
    summary = f.plans.summary(f.main)
    assert summary is not None
    assert summary.status == "active"
    assert summary.goal_outcome is None


async def test_complete_history_pages_do_not_clip_notes(fixture):
    f = fixture
    adapter = await f.opened()
    await ready(adapter)
    for i in range(8):
        await adapter.progress(
            request(f, operation_id=f"long-{i}", expected_step_cursor=i, note="x" * 4096),
            resolved_model=None,
        )
    page = await adapter.history("one")
    assert len(page.model_dump_json().encode()) <= 256 * 1024
    assert page.more is False
    assert all(item.note == "x" * 4096 for item in page.items)
    tail = await adapter.history("one", after=page.after, target=page.target)
    assert len(page.items) + len(tail.items) == 8
    assert tail.more is False
    with pytest.raises(DocumentError):
        await adapter.history("two")


async def test_final_canonical_registry_root_path_rebind_refuses(fixture, monkeypatch, tmp_path):
    f = fixture
    adapter = await f.opened()
    await ready(adapter)
    original = f.projects.session_snapshot
    held_root = tmp_path / "old-projects"
    calls = 0

    def changed_after_open(*args):
        nonlocal calls
        snapshot = original(*args)
        calls += 1
        if calls == 2:
            f.projects.root.rename(held_root)
            f.projects.root.mkdir()
        return snapshot

    monkeypatch.setattr(f.projects, "session_snapshot", changed_after_open)
    try:
        with pytest.raises(DocumentError, match="session"):
            await adapter.read_current()
    finally:
        if held_root.exists():
            f.projects.root.rmdir()
            held_root.rename(f.projects.root)
    with pytest.raises(DocumentError, match="held"):
        await adapter.read_current()
