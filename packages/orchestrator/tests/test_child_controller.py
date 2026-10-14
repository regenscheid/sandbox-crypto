# SPDX-License-Identifier: MIT
"""Actual controller/store composition with scripted models and selected-only fakes."""

from __future__ import annotations

import asyncio
import hashlib
import json
import threading
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from sandbox_science.contracts import Execution, ExecutionState, Result, StreamOutput
from sandbox_science.contracts.run_api import RunControlInfo, RunControlResult
from sandbox_science.contracts.run_cells import RunCellExecution
from sandbox_science.contracts.run_kernels import (
    RunKernelBinding,
    RunKernelEnrollment,
    run_kernel_binding_sha256,
)
from sandbox_science.orchestrator.child_attempts import (
    AppliedBudget,
    AttemptAuthority,
    ChildAttempts,
    RequestedProfile,
)
from sandbox_science.orchestrator.child_controller import (
    AppliedChildModel,
    ChildConfiguration,
    ChildController,
)
from sandbox_science.orchestrator.child_prompt import CHILD_SYSTEM, ChildPromptBundle
from sandbox_science.orchestrator.child_tools import ChildTools, ChildToolSettings
from sandbox_science.orchestrator.delegation_records import (
    DelegationBrief,
    DelegationRecords,
    DelegationScope,
    ParentAuthority,
    ProfileIntent,
    RequestedLimits,
)
from sandbox_science.orchestrator.documents.errors import DocumentError
from sandbox_science.orchestrator.documents.store import DocumentStore
from sandbox_science.orchestrator.model import ModelTurn, ToolCall
from sandbox_science.orchestrator.projects import ProjectStore


@pytest.fixture
def f(tmp_path):
    projects = ProjectStore(tmp_path / "state")
    projects.create("p", "minimal", "sha256:" + "a" * 64)
    session = projects.open_session("p", "s", dedicated=False)
    documents = DocumentStore(tmp_path / "state/control.sqlite3")
    records = DelegationRecords(documents, initialize=True)
    parent = ParentAuthority(
        scope=DelegationScope(
            project_id="p", session_id="s", environment_id=session.environment_id
        ),
        parent_run_id="parent",
    )
    prepared = records.prepare(
        parent,
        "prepare",
        DelegationBrief(
            task="Use the explicitly offered synthetic tools",
            context_summary="No external effects",
            track=None,
            inputs=(),
            profile=ProfileIntent(
                profile_id="profile", profile_sha256="b" * 64, model_alias="explicit"
            ),
            limits=RequestedLimits(turns=8, wall_seconds=30, output_tokens=256),
        ),
    )
    authority = AttemptAuthority(
        scope=parent.scope,
        parent_run_id="parent",
        delegation_id=prepared.delegation_id,
        child_run_id=prepared.child_run_id,
        controller_id="owner",
    )
    original = projects.session_snapshot("p", "s")
    journal = ChildAttempts(documents, records, projects, initialize=True)
    settings = ChildToolSettings(("python",), 2.0, 1024, 1.0, 0.001, 4, 1)
    budget = AppliedBudget(
        max_calls=4,
        per_call_output_tokens=128,
        total_output_tokens=512,
        cumulative_input_bytes=1048576,
        wall_seconds=30,
    )
    configuration = ChildConfiguration(
        RequestedProfile(profile_id="profile", profile_sha256="b" * 64, alias="explicit"),
        budget,
        settings,
    )
    state = SimpleNamespace(
        projects=projects,
        documents=documents,
        records=records,
        parent=parent,
        prepared=prepared,
        authority=authority,
        original=original,
        journal=journal,
        settings=settings,
        configuration=configuration,
        admitted=True,
    )
    yield state
    original.close()
    documents.close()


class Model:
    def __init__(self, fixture, turns):
        self.f, self.turns = fixture, list(turns)
        self.inputs = []
        self.closed = False

    async def complete(self, messages, tools):
        self.inputs.append((json.loads(json.dumps(messages)), tools))
        assert messages[0] == {"role": "system", "content": CHILD_SYSTEM}
        # Actual model seam sees the already committed reservation, not a synthetic receipt.
        db = self.f.documents._connection()
        row = db.execute(
            "SELECT record FROM child_attempt_events ORDER BY sequence DESC LIMIT 1"
        ).fetchone()
        assert json.loads(row[0])["body"]["kind"] == "reserve"
        return self.turns.pop(0)


class Worker:
    def __init__(self, fixture):
        self.f, self.requests, self.closes = fixture, [], []
        self.polls, self.enrolls = 0, 0
        self.value = None
        self.queue = False
        self.lose_ack = False

    async def enroll_run(self, scope, operation_id, *, timeout_seconds):
        self.enrolls += 1
        self.binding = RunKernelBinding(
            scope=scope, sandbox="original", sandbox_id="original-id", incarnation="inc-" + "c" * 24
        )
        return RunKernelEnrollment(
            format_version=1,
            operation_id=operation_id,
            request_sha256=hashlib.sha256(
                b"run-kernel-enrollment-v1\0"
                + json.dumps(
                    {"binding": self.binding.model_dump(mode="json"), "operation_id": operation_id},
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=True,
                ).encode("ascii")
            ).hexdigest(),
            binding_sha256=run_kernel_binding_sha256(self.binding),
            binding=self.binding,
            recorded_at=datetime.now(UTC).isoformat(),
        )

    async def submit_run_cell(self, request, *, timeout_seconds):
        self.requests.append(request)
        self.value = RunCellExecution(
            binding=request.binding,
            binding_sha256=run_kernel_binding_sha256(request.binding),
            execution=Execution(
                execution_id="exe-" + "a" * 24,
                attempt_id="att-" + "b" * 24,
                operation_id=request.operation_id,
                kind="cell",
                context=request.context,
                state=ExecutionState.QUEUED if self.queue else ExecutionState.SUCCEEDED,
                submitted_at=datetime.now(UTC),
                result=Result(
                    status="ok",
                    stdout=StreamOutput(
                        name="stdout", text="synthetic 2", truncated=False, total_bytes=11
                    ),
                ),
            ),
        )
        if self.lose_ack:
            raise TimeoutError("synthetic ignored secret must not be retained")
        return self.value

    async def get_run(self, binding, execution_id, attempt_id, *, timeout_seconds):
        self.polls += 1
        db = self.f.documents._connection()
        raw = db.execute(
            "SELECT record FROM child_attempt_events ORDER BY sequence DESC LIMIT 1"
        ).fetchone()[0]
        messages = json.loads(raw)["body"]["messages"]
        assert any('"status":"accepted"' in (m["content"] or "") for m in messages)
        assert self.value is not None
        self.value = self.value.model_copy(
            update={
                "execution": self.value.execution.model_copy(
                    update={"state": ExecutionState.SUCCEEDED}
                )
            }
        )
        return self.value

    async def cancel_run(self, binding, execution_id, attempt_id, *, timeout_seconds):
        assert self.value is not None
        return self.value

    async def close_run(self, binding, operation_id, *, timeout_seconds):
        self.closes.append((binding, operation_id))
        return RunControlResult(
            request=RunControlInfo(
                binding=binding,
                operation_id=operation_id,
                action="close",
                language=None,
                recorded_at=datetime.now(UTC).isoformat(),
            ),
            state="done",
        )


def controller(f, model, worker):
    return ChildController(
        f.journal,
        f.records,
        worker,
        AppliedChildModel(model, 128, None),
        f.configuration,
        plans=None,
    )


async def run(f, c):
    return await c.run(f.authority, f.prepared, f.original, "begin", admitted=lambda: f.admitted)


def ending():
    return json.dumps(
        {
            "schema_version": 1,
            "status": "partial",
            "summary": "Synthetic observation",
            "results": [],
            "deviations": [],
            "open_questions": ["Scientific correctness unverified"],
        }
    )


@pytest.mark.asyncio
async def test_actual_selected_tool_input_partial_and_durable_before_finish(f):
    model = Model(
        f,
        [
            ModelTurn(None, (ToolCall("one", "python", '{"code":"2"}'),), model="actual"),
            ModelTurn(ending(), model="actual"),
        ],
    )
    worker = Worker(f)
    worker.queue = True
    value = await run(f, controller(f, model, worker))
    assert value.ending == "returned"
    assert value.held is None
    assert len(worker.requests) == 1
    assert worker.polls == 1
    assert len(worker.closes) == 1
    assert worker.requests[0].binding.scope.child_run_id == f.prepared.child_run_id
    assert worker.requests[0].context.session_id == f.prepared.scope.session_id
    assert [spec.name for spec in model.inputs[0][1]] == ["python"]
    assert "synthetic 2" in json.dumps(model.inputs[1][0])
    assert value.handback.result.status == "partial"
    assert value.handback.observation.resolved_model == "actual"
    state = f.journal.get(f.authority, value.attempt_id)
    assert state.sealed is not None
    assert state.pending is None
    assert state.calls == 2
    assert state.sealed.recorded_at <= value.handback.recorded_at
    assert not model.closed


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "name", ["shell", "plan_revise", "restart_kernel", "delegate", "read_file"]
)
async def test_unoffered_names_never_parse_or_dispatch(f, name):
    model = Model(f, [ModelTurn(None, (ToolCall("one", name, "not JSON"),)), ModelTurn(ending())])
    worker = Worker(f)
    result = await run(f, controller(f, model, worker))
    assert result.ending == "returned"
    assert not worker.requests
    assert "unoffered_tool" in json.dumps(model.inputs[1][0])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "raw",
    [
        '{"code":"1","code":"2"}',
        '{"code":"1","binding":{}}',
        '{"code":true}',
        '{"code":"1","context":{}}',
    ],
)
async def test_invalid_arguments_never_dispatch(f, raw):
    model = Model(f, [ModelTurn(None, (ToolCall("one", "python", raw),)), ModelTurn(ending())])
    worker = Worker(f)
    value = await run(f, controller(f, model, worker))
    assert value.ending == "returned"
    assert not worker.requests


@pytest.mark.asyncio
async def test_lost_submit_ack_holds_and_no_second_attempt_or_call(f):
    model = Model(f, [ModelTurn(None, (ToolCall("one", "python", '{"code":"2"}'),))])
    worker = Worker(f)
    worker.lose_ack = True
    first = await run(f, controller(f, model, worker))
    assert first.held == "tool_unknown"
    assert first.ending == "interrupted"
    with pytest.raises(DocumentError):
        await run(f, controller(f, model, worker))
    assert worker.enrolls == 1
    assert len(worker.requests) == 1
    assert len(model.inputs) == 1
    assert f.journal.get(f.authority, first.attempt_id).sealed is not None


@pytest.mark.asyncio
async def test_unknown_model_stays_reserved_and_sealed(f):
    class Broken(Model):
        async def complete(self, messages, tools):
            await super().complete(messages, tools)
            raise RuntimeError("synthetic secret not retained")

    worker = Worker(f)
    model = Broken(f, [ModelTurn("unused")])
    result = await run(f, controller(f, model, worker))
    state = f.journal.get(f.authority, result.attempt_id)
    assert result.held == "model_unknown"
    assert state.pending is not None
    assert state.sealed is not None
    assert state.sealed.body.ended == "interrupted"
    assert not worker.requests
    assert "synthetic secret" not in json.dumps([m.model_dump() for m in state.messages])


@pytest.mark.asyncio
async def test_applied_output_overage_is_retained_and_stops(f):
    model = Model(f, [ModelTurn(ending(), output_tokens=129)])
    value = await run(f, controller(f, model, Worker(f)))
    assert value.held == "budget_exhausted"
    assert value.ending == "limit_reached"
    state = f.journal.get(f.authority, value.attempt_id)
    assert state.pending is None
    assert state.calls == 1


@pytest.mark.asyncio
async def test_revoke_during_actual_reply_keeps_late_response_without_tools(f):
    class Revoked(Model):
        async def complete(self, messages, tools):
            value = await super().complete(messages, tools)
            f.admitted = False
            return value

    model = Revoked(f, [ModelTurn(None, (ToolCall("one", "python", '{"code":"2"}'),))])
    worker = Worker(f)
    result = await run(f, controller(f, model, worker))
    assert result.ending == "failed"
    assert not worker.requests
    state = f.journal.get(f.authority, result.attempt_id)
    assert state.pending is None
    assert any(message.role == "assistant" for message in state.messages)


@pytest.mark.asyncio
async def test_repeated_cancellation_drains_ignored_model_and_borrowed_owner(f):
    entered, release = asyncio.Event(), asyncio.Event()

    class Slow(Model):
        async def complete(self, messages, tools):
            entered.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                await release.wait()
            return await super().complete(messages, tools)

    model = Slow(f, [ModelTurn(ending())])
    c = controller(f, model, Worker(f))
    task = asyncio.create_task(run(f, c))
    await entered.wait()
    task.cancel("original")
    await asyncio.sleep(0.01)
    task.cancel("repeated")
    await asyncio.sleep(0.01)
    assert not task.done()
    release.set()
    with pytest.raises(asyncio.CancelledError, match="original"):
        await task
    assert f.documents._connection() is not None
    value = f.records.get(f.parent, f.prepared.delegation_id)
    assert value.outcome.observation.ended == "cancelled"


def test_explicit_positive_settings_and_model_allowance(f):
    for names in [("shell",), ("python", "python")]:
        with pytest.raises(ValueError, match=r"child|selected|unsupported|allowance"):
            ChildToolSettings(names, 2.0, 1024, 1.0, 0.001, 4, 1)
    with pytest.raises(ValueError, match=r"child|selected|unsupported|allowance"):
        AppliedChildModel(Model(f, []), True, None)
    with pytest.raises(ValueError, match=r"child|selected|unsupported|allowance"):
        AppliedChildModel(Model(f, []), 128, 1.0)
    with pytest.raises(ValueError, match=r"child|selected|unsupported|allowance"):
        ChildController(
            f.journal,
            f.records,
            Worker(f),
            AppliedChildModel(Model(f, []), 64, None),
            f.configuration,
            plans=None,
        )


def test_bundle_owns_fresh_nested_schemas_and_main_absent(f):
    from sandbox_science.orchestrator.child_tools import tool_specs

    bundle = ChildPromptBundle.assemble(tool_specs(f.settings))
    first = bundle.tool_specs()
    first[0].parameters["properties"]["code"]["type"] = "boolean"
    second = bundle.tool_specs()
    assert second[0].parameters["properties"]["code"]["type"] == "string"
    with pytest.raises(ValueError, match=r"child|selected|unsupported|allowance"):
        bundle.verify(bundle.initial(f.prepared), first)
    bundle.verify(bundle.initial(f.prepared), second)
    assert "sandbox-main" not in bundle.system
    assert "sandbox-delegated-child-1" in bundle.system


@pytest.mark.asyncio
async def test_multiple_requested_calls_refuse_whole_batch_before_effect(f):
    model = Model(
        f,
        [
            ModelTurn(
                None,
                (
                    ToolCall("one", "python", '{"code":"1"}'),
                    ToolCall("two", "python", '{"code":"2"}'),
                ),
            ),
            ModelTurn(ending()),
        ],
    )
    worker = Worker(f)
    value = await run(f, controller(f, model, worker))
    assert value.ending == "returned"
    assert not worker.requests
    assert json.dumps(model.inputs[1][0]).count("one_call_per_turn_required") == 2


@pytest.mark.asyncio
async def test_blocked_initial_checkpoint_drains_before_owner_return(f, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    checkpoint = f.journal.checkpoint

    def blocked(*args, **kwargs):
        entered.set()
        assert release.wait(3)
        return checkpoint(*args, **kwargs)

    monkeypatch.setattr(f.journal, "checkpoint", blocked)
    worker = Worker(f)
    c = controller(f, Model(f, []), worker)
    task = asyncio.create_task(run(f, c))
    assert await asyncio.to_thread(entered.wait, 2)
    task.cancel("first")
    await asyncio.sleep(0.01)
    task.cancel("second")
    await asyncio.sleep(0.01)
    assert not task.done()
    assert not worker.enrolls
    release.set()
    with pytest.raises(asyncio.CancelledError, match="first"):
        await task
    assert f.documents._connection() is not None
    assert f.records.get(f.parent, f.prepared.delegation_id).outcome is not None


@pytest.mark.asyncio
async def test_cancel_during_admitted_cleanup_does_not_cancel_real_close(f):
    entered, release = asyncio.Event(), asyncio.Event()

    class SlowClose(Worker):
        async def close_run(self, *args, **kwargs):
            entered.set()
            await release.wait()
            return await super().close_run(*args, **kwargs)

    worker = SlowClose(f)
    task = asyncio.create_task(run(f, controller(f, Model(f, [ModelTurn(ending())]), worker)))
    await entered.wait()
    task.cancel("cleanup")
    await asyncio.sleep(0.01)
    task.cancel("again")
    await asyncio.sleep(0.01)
    assert not task.done()
    assert not worker.closes
    release.set()
    with pytest.raises(asyncio.CancelledError, match="cleanup"):
        await task
    assert len(worker.closes) == 1
    assert f.records.get(f.parent, f.prepared.delegation_id).outcome is not None


@pytest.mark.asyncio
async def test_replaced_original_registry_prevents_first_external_call(f):
    path = f.projects.root / "p.json"
    raw = path.read_bytes()
    path.unlink()
    path.write_bytes(raw)
    worker, model = Worker(f), Model(f, [])
    with pytest.raises(DocumentError):
        await run(f, controller(f, model, worker))
    assert not worker.enrolls
    assert not model.inputs


@pytest.mark.asyncio
async def test_oversized_response_stays_unknown_without_tool_dispatch(f):
    model = Model(f, [ModelTurn("x" * 65537)])
    worker = Worker(f)
    value = await run(f, controller(f, model, worker))
    assert value.held == "retention_failed"
    assert not worker.requests
    assert f.journal.get(f.authority, value.attempt_id).pending is not None


@pytest.mark.asyncio
async def test_complete_child_assertion_does_not_mean_scientific_success(f):
    raw = ending().replace('"partial"', '"complete"')
    value = await run(f, controller(f, Model(f, [ModelTurn(raw)]), Worker(f)))
    assert value.handback.result.status == "complete"
    assert value.handback.result.open_questions == ("Scientific correctness unverified",)
    assert value.handback.observation.ended == "returned"


async def track(f):
    from sandbox_science.orchestrator.delegated_plan_tools import DelegatedPlanProgress
    from sandbox_science.orchestrator.delegation_records import StepPin, TrackPin
    from sandbox_science.orchestrator.session_plans import (
        PlanAuthority,
        PlanBody,
        PlanScope,
        SessionPlans,
    )

    plans = SessionPlans(f.documents)
    main = PlanAuthority(
        scope=PlanScope(project_id="p", session_id="s"), role="main", run_id="parent", alias="main"
    )
    body = PlanBody.model_validate(
        {
            "goal": "Analyze",
            "feasibility": "Synthetic",
            "outputs": ({"title": "Claim", "description": "Result", "evidence_standard": "Check"},),
            "tracks": ({"track_id": "a", "title": "Own"}, {"track_id": "b", "title": "Other"}),
            "phases": (
                {
                    "phase_id": "work",
                    "title": "Work",
                    "steps": (
                        {
                            "step_id": "one",
                            "title": "Own analysis",
                            "description": "Analyze",
                            "evidence_standard": "Reproduce",
                            "track_id": "a",
                        },
                        {
                            "step_id": "two",
                            "title": "Foreign track private note",
                            "description": "Other",
                            "evidence_standard": "Reproduce",
                            "track_id": "b",
                        },
                    ),
                },
            ),
        }
    )
    created = plans.publish(main, "create", body, "Initial")
    plans.set_status(main, "activate", created.ref, 1, 0, "active", "Existing authorization")
    compact = plans.compact_current(main)
    assert compact is not None
    brief = f.prepared.brief
    assert brief is not None
    prepared = f.records.prepare(
        f.parent,
        "prepare-track",
        brief.model_copy(
            update={
                "track": TrackPin(
                    plan_id=created.ref.plan_id,
                    version=created.ref.version,
                    track_id="a",
                    steps=(StepPin(step_id="one", sha256=compact.steps[0].step_sha256),),
                )
            }
        ),
    )
    f.prepared = prepared
    f.authority = f.authority.model_copy(
        update={"delegation_id": prepared.delegation_id, "child_run_id": prepared.child_run_id}
    )
    authority = PlanAuthority(
        scope=main.scope,
        role="delegated",
        run_id=prepared.child_run_id,
        alias="explicit",
        grant=created.ref,
        track_id="a",
    )
    adapter = await DelegatedPlanProgress.open(
        plans,
        f.records,
        f.projects,
        f.original.context,
        authority=authority,
        prepared=prepared,
        original_session=f.original,
        admitted=lambda: f.admitted,
    )
    settings = ChildToolSettings(
        ("plan_read", "plan_history", "plan_progress", "python"), 2.0, 1024, 1.0, 0.001, 4, 1
    )
    f.configuration = ChildConfiguration(f.configuration.profile, f.configuration.budget, settings)
    return adapter, plans, main, created.ref


@pytest.mark.asyncio
async def test_current_read_reaches_successful_model_then_progress_is_live_own_track(f):
    adapter, plans, main, _ref = await track(f)

    class Adaptive(Model):
        async def complete(self, messages, tools):
            # Actual committed reserve and exact bounded input remain the same production seam.
            await super().complete(messages, tools)
            call = len(self.inputs)
            if call == 1:
                return ModelTurn(None, (ToolCall("read", "plan_read", "{}"),), model="actual-child")
            if call == 2:
                read = next(
                    json.loads(m["content"])
                    for m in messages
                    if m["role"] == "tool" and m["tool_call_id"] == "read"
                )
                assert [item["step"]["step_id"] for item in read["steps"]] == ["one"]
                assert "Foreign track private note" not in json.dumps(read)
                args = {
                    "base": read["head"]["ref"],
                    "step_id": "one",
                    "expected_step_cursor": read["steps"][0]["current"]["latest"]["sequence"]
                    if read["steps"][0]["current"]["latest"]
                    else 0,
                    "expected_status_revision": read["head"]["status_revision"],
                    "status": "in_progress",
                    "note": "Started during the actual conversation",
                    "evidence": [],
                }
                return ModelTurn(
                    None,
                    (ToolCall("progress", "plan_progress", json.dumps(args)),),
                    model="actual-child",
                )
            if call == 3:
                current = plans.compact_current(main)
                assert current is not None
                assert current.steps[0].latest is not None
                assert current.steps[0].latest.status == "in_progress"
                assert current.steps[0].latest.author.resolved_model == "actual-child"
                # No canonical handback/run save is needed to observe committed progress.
                assert f.records.get(f.parent, f.prepared.delegation_id).outcome is None
                return ModelTurn(ending(), model="actual-child")
            raise AssertionError("unbounded extra call")

    model = Adaptive(f, [ModelTurn(None)] * 3)
    try:
        value = await run(
            f,
            ChildController(
                f.journal,
                f.records,
                Worker(f),
                AppliedChildModel(model, 128, None),
                f.configuration,
                plans=adapter,
            ),
        )
        assert value.held is None
        assert value.ending == "returned"
        assert len(model.inputs) == 3
        current = plans.compact_current(main)
        assert current is not None
        assert current.steps[1].latest is None
    finally:
        await adapter.close()


@pytest.mark.asyncio
async def test_user_change_during_model_delivery_holds_old_read_and_all_effects(f):
    from sandbox_science.orchestrator.session_plans import PlanAuthority

    adapter, plans, main, ref = await track(f)

    class Changed(Model):
        async def complete(self, messages, tools):
            await super().complete(messages, tools)
            if len(self.inputs) == 1:
                return ModelTurn(None, (ToolCall("read", "plan_read", "{}"),))
            user = PlanAuthority(scope=main.scope, role="user", user_id="original-user")
            plans.progress(user, "user-note", ref, "one", 0, "pending", "USER changed the task")
            return ModelTurn(None, (ToolCall("cell", "python", '{"code":"2"}'),))

    worker, model = Worker(f), Changed(f, [ModelTurn(None), ModelTurn(None)])
    try:
        value = await run(
            f,
            ChildController(
                f.journal,
                f.records,
                worker,
                AppliedChildModel(model, 128, None),
                f.configuration,
                plans=adapter,
            ),
        )
        assert value.held == "plan_held"
        assert not worker.requests
        assert len(model.inputs) == 2
        current = plans.compact_current(main)
        assert current is not None
        assert current.steps[0].latest is not None
        assert current.steps[0].latest.author.role == "user"
    finally:
        await adapter.close()


@pytest.mark.asyncio
async def test_no_plan_read_same_batch_can_clear_delivery_or_write_other_track(f):
    adapter, plans, main, ref = await track(f)
    args = {
        "base": ref.model_dump(),
        "step_id": "two",
        "expected_step_cursor": 0,
        "expected_status_revision": 2,
        "status": "completed",
        "note": "forged",
        "evidence": [],
    }
    model = Model(
        f,
        [
            ModelTurn(
                None,
                (
                    ToolCall("read", "plan_read", "{}"),
                    ToolCall("write", "plan_progress", json.dumps(args)),
                ),
            ),
            ModelTurn(ending()),
        ],
    )
    worker = Worker(f)
    try:
        value = await run(
            f,
            ChildController(
                f.journal,
                f.records,
                worker,
                AppliedChildModel(model, 128, None),
                f.configuration,
                plans=adapter,
            ),
        )
        assert value.held == "plan_held"
        assert not worker.requests
        current = plans.compact_current(main)
        assert current is not None
        assert all(step.latest is None for step in current.steps)
    finally:
        await adapter.close()


@pytest.mark.asyncio
async def test_full_actual_selected_intent_durable_before_lost_ack(f):
    class Inspect(Worker):
        async def submit_run_cell(self, request, *, timeout_seconds):
            db = f.documents._connection()
            last = json.loads(
                db.execute(
                    "SELECT record FROM child_attempt_events ORDER BY sequence DESC LIMIT 1"
                ).fetchone()[0]
            )
            intent = json.loads(last["body"]["messages"][-1]["content"])
            assert intent["status"] == "intent"
            assert intent["request"] == request.model_dump(mode="json")
            return await super().submit_run_cell(request, timeout_seconds=timeout_seconds)

    worker = Inspect(f)
    worker.lose_ack = True
    model = Model(f, [ModelTurn(None, (ToolCall("cell", "python", '{"code":"2"}'),))])
    value = await run(f, controller(f, model, worker))
    assert value.held == "tool_unknown"
    assert len(worker.requests) == 1
    state = f.journal.get(f.authority, value.attempt_id)
    assert any('"timeout_seconds":2.0' in (m.content or "") for m in state.messages)
    assert any('"output_limit_bytes":1024' in (m.content or "") for m in state.messages)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["pending", "foreign", "invalid"])
async def test_cleanup_requires_exact_bounded_original_done_receipt(f, kind):
    class WrongClose(Worker):
        async def close_run(self, binding, operation_id, *, timeout_seconds):
            value = await super().close_run(binding, operation_id, timeout_seconds=timeout_seconds)
            if kind == "pending":
                return value.model_copy(update={"state": "pending"})
            if kind == "foreign":
                return value.model_copy(
                    update={
                        "request": value.request.model_copy(update={"operation_id": "different"})
                    }
                )
            return value.model_copy(update={"undeclared": "synthetic"})

    worker = WrongClose(f)
    value = await run(f, controller(f, Model(f, [ModelTurn(ending())]), worker))
    assert value.held == "cleanup_unknown"
    assert value.cleanup == ("pending" if kind == "pending" else "unknown")
    state = f.journal.get(f.authority, value.attempt_id)
    assert any('"action":"close"' in (m.content or "") for m in state.messages)
    assert state.sealed is not None
    assert value.handback is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["operation_id", "request_sha256"])
async def test_wrong_enrollment_receipt_does_not_reach_model_or_cell(f, change):
    class WrongEnrollment(Worker):
        async def enroll_run(self, *args, **kwargs):
            value = await super().enroll_run(*args, **kwargs)
            return value.model_copy(
                update={change: "different" if change == "operation_id" else "0" * 64}
            )

    worker, model = WrongEnrollment(f), Model(f, [])
    value = await run(f, controller(f, model, worker))
    assert value.held == "admission_changed"
    assert not model.inputs
    assert not worker.requests


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "state", [ExecutionState.LOST, ExecutionState.TIMED_OUT, ExecutionState.CANCELLED]
)
async def test_terminal_unknown_or_partial_worker_state_stops_before_next_model(f, state):
    class Lost(Worker):
        async def submit_run_cell(self, *args, **kwargs):
            value = await super().submit_run_cell(*args, **kwargs)
            return value.model_copy(
                update={"execution": value.execution.model_copy(update={"state": state})}
            )

    worker = Lost(f)
    model = Model(f, [ModelTurn(None, (ToolCall("one", "python", '{"code":"2"}'),))])
    result = await run(f, controller(f, model, worker))
    assert result.held == "tool_unknown"
    assert len(model.inputs) == 1
    assert len(worker.requests) == 1
    assert f.journal.get(f.authority, result.attempt_id).held_effects


@pytest.mark.asyncio
async def test_existing_begin_only_attempt_is_historical_not_new_dispatch(f):
    from sandbox_science.orchestrator.child_tools import tool_specs

    bundle = ChildPromptBundle.assemble(tool_specs(f.settings))
    pin = f.journal.pin_session(f.authority, f.original)
    old = f.journal.begin_attempt(
        f.authority,
        "old-unadmitted-controller",
        prepared=f.prepared,
        original=f.original,
        session=pin,
        profile=f.configuration.profile,
        budget=f.configuration.budget,
        system=bundle.system,
        tools=bundle.tools,
        messages=bundle.initial(f.prepared),
        admitted=lambda: True,
    )
    assert f.journal.get(f.authority, old.attempt_id).cursor == 1
    worker, model = Worker(f), Model(f, [])
    with pytest.raises(DocumentError):
        await run(f, controller(f, model, worker))
    assert not worker.enrolls
    assert not worker.requests
    assert not model.inputs


@pytest.mark.asyncio
async def test_crash_after_accepted_ids_never_replays_or_reattempts(f, monkeypatch):
    original = ChildTools._cell

    class ProcessLost(BaseException):
        pass

    async def crash(self, *args, **kwargs):
        result = await original(self, *args, **kwargs)
        assert result.effect is not None
        raise ProcessLost()

    monkeypatch.setattr(ChildTools, "_cell", crash)
    worker = Worker(f)
    model = Model(f, [ModelTurn(None, (ToolCall("one", "python", '{"code":"2"}'),))])
    with pytest.raises(ProcessLost):
        await run(f, controller(f, model, worker))
    with pytest.raises(DocumentError):
        await run(f, controller(f, model, worker))
    assert len(worker.requests) == 1
    assert len(model.inputs) == 1
    assert worker.enrolls == 1
    assert f.records.get(f.parent, f.prepared.delegation_id).outcome is None


def test_exact_selected_copy_bounds_refuse_before_dump_or_subclass_callbacks(f, monkeypatch):
    from sandbox_science.orchestrator.child_tools import checked_response

    scope = f.authority.scope
    from sandbox_science.contracts.run_kernels import RunKernelScope

    binding = RunKernelBinding(
        scope=RunKernelScope(
            **scope.model_dump(),
            parent_run_id=f.authority.parent_run_id,
            delegation_id=f.authority.delegation_id,
            child_run_id=f.authority.child_run_id,
        ),
        sandbox="original",
        sandbox_id="id",
        incarnation="inc-" + "c" * 24,
    )
    value = RunKernelEnrollment(
        format_version=1,
        operation_id="enroll",
        request_sha256="0" * 64,
        binding_sha256=run_kernel_binding_sha256(binding),
        binding=binding,
        recorded_at=datetime.now(UTC).isoformat(),
    )

    def no_dump(*args, **kwargs):
        raise AssertionError("preflight must not call model_dump")

    monkeypatch.setattr(RunKernelEnrollment, "model_dump", no_dump)
    with pytest.raises(ValueError, match=r"child|selected|unsupported|allowance"):
        checked_response(
            RunKernelEnrollment, value.model_copy(update={f"extra-{i}": i for i in range(2048)})
        )
    visits = []

    class Text(str):
        def encode(self, *args, **kwargs):
            visits.append("encode")
            return super().encode(*args, **kwargs)

    with pytest.raises(ValueError, match=r"child|selected|unsupported|allowance"):
        checked_response(
            RunKernelEnrollment, value.model_copy(update={"operation_id": Text("enroll")})
        )
    assert visits == []
    assert checked_response(RunKernelEnrollment, value).operation_id == "enroll"


@pytest.mark.asyncio
@pytest.mark.parametrize("at", ["enrollment", "reservation", "selected_intent"])
async def test_fresh_admission_after_durable_wait_precedes_actual_call(f, monkeypatch, at):
    reserve, checkpoint = f.journal.reserve_turn, f.journal.checkpoint

    def reserve_then_revoke(*args, **kwargs):
        value = reserve(*args, **kwargs)
        if at == "reservation":
            f.admitted = False
        return value

    def checkpoint_then_revoke(*args, **kwargs):
        value = checkpoint(*args, **kwargs)
        messages = value.body.messages
        if at == "enrollment" or (
            at == "selected_intent"
            and messages[-1].role == "tool"
            and '"status":"intent"' in messages[-1].content
        ):
            f.admitted = False
        return value

    monkeypatch.setattr(f.journal, "reserve_turn", reserve_then_revoke)
    monkeypatch.setattr(f.journal, "checkpoint", checkpoint_then_revoke)
    model = Model(f, [ModelTurn(None, (ToolCall("one", "python", '{"code":"2"}'),))])
    worker = Worker(f)
    result = await run(f, controller(f, model, worker))
    assert result.held is not None
    assert not worker.requests
    assert len(model.inputs) == (1 if at == "selected_intent" else 0)
    assert worker.enrolls == (0 if at == "enrollment" else 1)
    assert f.journal.get(f.authority, result.attempt_id).sealed is not None


@pytest.mark.asyncio
async def test_normal_close_drains_without_cancelling_close_caller(f):
    entered, release = asyncio.Event(), asyncio.Event()

    class Slow(Model):
        async def complete(self, messages, tools):
            entered.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                await release.wait()
            return await super().complete(messages, tools)

    c = controller(f, Slow(f, [ModelTurn(ending())]), Worker(f))
    task = asyncio.create_task(run(f, c))
    await entered.wait()
    closing = asyncio.create_task(c.close())
    await asyncio.sleep(0.01)
    assert not closing.done()
    assert not task.done()
    release.set()
    await closing
    with pytest.raises(asyncio.CancelledError):
        await task
    assert f.records.get(f.parent, f.prepared.delegation_id).outcome is not None


@pytest.mark.asyncio
async def test_late_success_after_outer_revoke_never_clears_borrowed_read_fence(f, monkeypatch):
    adapter, _plans, _main, _ref = await track(f)
    # Two independently supplied cooperative controller callbacks, as permitted by the API.
    monkeypatch.setattr(adapter, "_admitted", lambda: True)
    delivered = []
    original = adapter.model_delivered

    async def spy(read):
        delivered.append(read)
        await original(read)

    monkeypatch.setattr(adapter, "model_delivered", spy)

    class Revoked(Model):
        async def complete(self, messages, tools):
            await super().complete(messages, tools)
            if len(self.inputs) == 1:
                return ModelTurn(None, (ToolCall("read", "plan_read", "{}"),))
            f.admitted = False
            return ModelTurn(ending())

    model, worker = Revoked(f, [ModelTurn(None), ModelTurn(None)]), Worker(f)
    try:
        value = await run(
            f,
            ChildController(
                f.journal,
                f.records,
                worker,
                AppliedChildModel(model, 128, None),
                f.configuration,
                plans=adapter,
            ),
        )
        assert not delivered
        assert not worker.requests
        assert f.journal.get(f.authority, value.attempt_id).pending is None
        assert any(
            message.role == "assistant" and message.content == ending()
            for message in f.journal.get(f.authority, value.attempt_id).messages
        )
    finally:
        await adapter.close()
