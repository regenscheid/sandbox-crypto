# SPDX-License-Identifier: MIT
"""Actual MAIN planning, current-read fences and durable failure boundaries."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import threading
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import cast

import pytest

from sandbox_science.contracts import ErrorKind, ExecutionContext
from sandbox_science.orchestrator import cli, harness
from sandbox_science.orchestrator.consistency.controller import ReviewController
from sandbox_science.orchestrator.consistency.store import ReviewStore
from sandbox_science.orchestrator.documents import DocumentStore, DocumentTool
from sandbox_science.orchestrator.documents.errors import DocumentError
from sandbox_science.orchestrator.harness import save
from sandbox_science.orchestrator.hooks import Effect
from sandbox_science.orchestrator.jobs import JobTools
from sandbox_science.orchestrator.loop import run_task
from sandbox_science.orchestrator.model import ModelError, ModelTurn, ScriptedModel, ToolCall
from sandbox_science.orchestrator.plan_tools import (
    GUIDANCE_VERSION,
    MAX_REQUEST_BYTES,
    MAX_RESPONSE_BYTES,
    NAMES,
    PlanTools,
)
from sandbox_science.orchestrator.projects import ProjectStore
from sandbox_science.orchestrator.session_plans import (
    PlanAuthority,
    PlanBody,
    PlanScope,
    SessionPlans,
)
from sandbox_science.orchestrator.tools import Toolbox, ToolEntry, ToolOutcome
from sandbox_science.orchestrator.worker_client import WorkerAPIError, WorkerClient


def body(goal="Check the claim", *, count=1):
    return {
        "goal": goal,
        "feasibility": "Inspect assumptions before deriving a result",
        "outputs": (
            {"title": "Result", "description": "Derivation", "evidence_standard": "Reproduce"},
        ),
        "tracks": (),
        "phases": (
            {
                "phase_id": "analysis",
                "title": "Analysis",
                "steps": tuple(
                    {
                        "step_id": f"step-{i}",
                        "title": f"Step {i}",
                        "description": "Inspect and check",
                        "evidence_standard": "Retain evidence",
                    }
                    for i in range(count)
                ),
            },
        ),
    }


@pytest.fixture
def fixture(tmp_path):
    projects = ProjectStore(tmp_path / "state")
    projects.create("p", "minimal", "sha256:" + "a" * 64)
    session = projects.open_session("p", "s", dedicated=False)
    context = ExecutionContext(
        project_id="p",
        session_id="s",
        environment_id=session.environment_id,
        policy_revision="sha256:" + "a" * 64,
    )
    documents = DocumentStore(tmp_path / "state" / "control.sqlite3")
    core = SessionPlans(documents)
    adapter = PlanTools(core, projects, context, run_id="main-run", alias="main")
    yield SimpleNamespace(
        projects=projects,
        context=context,
        documents=documents,
        core=core,
        adapter=adapter,
        user=PlanAuthority(
            scope=PlanScope(project_id="p", session_id="s"), role="user", user_id="u"
        ),
    )
    adapter.close()
    documents.close()


async def call(f, name, **args):
    text, record = await f.adapter.call(
        name, json.dumps(args), call_id="call", model="actual-model"
    )
    return json.loads(text), record


async def made(f):
    result, _ = await call(f, "plan_create", operation_id="create", body=body(), reason="Initial")
    return result["receipt"]["ref"]


async def active(f):
    ref = await made(f)
    result, _ = await call(
        f,
        "plan_activate",
        operation_id="activate",
        base=ref,
        expected_status_revision=1,
        expected_progress_cursor=0,
        reason="Begin authorized work",
    )
    return ref, result["receipt"]["status_revision"]


async def read_and_deliver(f):
    await call(f, "plan_read")
    await f.adapter.model_delivered()


READS = {
    "read_file",
    "list_files",
    "document_list",
    "document_read",
    "document_comments",
    "document_comment_read",
    "review_findings",
    "review_finding_read",
    "review_evidence_read",
    "review_response_read",
    "list_jobs",
    "job_status",
    "wait_jobs",
    "provision_status",
}
"""The tools a plan's gate lets through: they change nothing it governs."""


def worker():
    return cast(WorkerClient, SimpleNamespace())


def tools(f):
    return Toolbox(worker(), f.context, plans=f.adapter)


def turn(name, **arguments):
    return ModelTurn(None, (ToolCall("c", name, json.dumps(arguments)),), model="actual-model")


@pytest.mark.asyncio
async def test_actual_model_creates_activates_updates_and_finishes_without_approval(fixture):
    f = fixture

    class Model:
        def __init__(self):
            self.requests = []

        async def complete(self, messages, tools):
            self.requests.append(list(messages))
            assert {tool.name for tool in tools} >= NAMES
            assert GUIDANCE_VERSION in messages[0]["content"]
            previous = [json.loads(m["content"]) for m in messages if m["role"] == "tool"]
            if not previous:
                return turn("plan_create", operation_id="create", body=body(), reason="Initial")
            ref = previous[0]["receipt"]["ref"]
            if len(previous) == 1:
                return turn(
                    "plan_activate",
                    operation_id="activate",
                    base=ref,
                    expected_status_revision=1,
                    expected_progress_cursor=0,
                    reason="Begin",
                )
            if len(previous) == 2:
                return turn(
                    "plan_progress",
                    operation_id="start",
                    base=ref,
                    step_id="step-0",
                    expected_step_cursor=0,
                    expected_status_revision=2,
                    status="in_progress",
                    note="Inspecting",
                )
            if len(previous) == 3:
                # Durable progress is visible to a separately opened control connection
                # while the model run is still active, before any RunRecord save.
                other = DocumentStore(f.documents.path)
                try:
                    snapshot = SessionPlans(other).current(f.adapter.authority)
                    assert snapshot is not None
                    latest = snapshot.steps[0].latest
                    assert latest is not None
                    assert latest.status == "in_progress"
                finally:
                    other.close()
                return turn(
                    "plan_progress",
                    operation_id="done",
                    base=ref,
                    step_id="step-0",
                    expected_step_cursor=1,
                    expected_status_revision=2,
                    status="completed",
                    note="Evidence retained",
                )
            if len(previous) == 4:
                return turn(
                    "plan_finish",
                    operation_id="finish",
                    base=ref,
                    expected_status_revision=2,
                    expected_progress_cursor=2,
                    reason="Finished",
                    outcome={
                        "status": "achieved",
                        "summary": "Checked",
                        "evidence": [],
                        "remaining_work": [],
                    },
                )
            return ModelTurn("The checked result is ready.", model="actual-model")

    transcript = await run_task(Model(), tools(f), "Check the claim", run_id="main-run")
    assert transcript.stopped == "answered"
    assert len(transcript.tool_records) == 5
    current = f.core.current(f.adapter.authority)
    assert current.head.status == "completed"
    assert current.head.outcome.status == "achieved"
    assert current.version.author.resolved_model == "actual-model"


@pytest.mark.asyncio
async def test_guidance_is_conditional_and_every_offered_tool_has_one_explicit_class(fixture):
    f = fixture
    documents = DocumentTool(f.documents, f.context, run_id="main-run", alias="main")
    reviews = ReviewController(
        ReviewStore(f.documents), f.context, run_id="main-run", model=None, alias=None
    )
    toolbox = Toolbox(
        worker(),
        f.context,
        languages=("python", "sage"),
        features=("pdf", "admission", "provisioning", "jobs"),
        jobs=JobTools(worker(), f.context, run_id="main-run"),
        documents=documents,
        reviews=reviews,
        plans=f.adapter,
    )
    effects = {name: entry.effect for name, entry in toolbox.entries.items()}
    assert set(effects) == {spec.name for spec in toolbox.specs}
    assert {name for name, effect in effects.items() if effect == "plan"} == NAMES
    assert {name for name, effect in effects.items() if effect == "cancel"} == {"cancel_job"}
    assert {name for name, effect in effects.items() if effect == "read"} == READS
    plain = Toolbox(worker(), f.context)
    model = ScriptedModel([ModelTurn("A small factual reply.")])
    result = await run_task(model, plain, "What is two plus two?", run_id="plain")
    assert result.stopped == "answered"
    assert GUIDANCE_VERSION not in model.requests[0][0]["content"]
    assert not (NAMES & {spec.name for spec in plain.specs})
    await reviews.close()


@pytest.mark.asyncio
async def test_a_toolbox_with_plan_tools_fences_effects_without_a_gate_passed(fixture):
    f = fixture
    await made(f)
    toolbox = tools(f)
    dispatched = []

    async def shell(request):
        dispatched.append(request.operation)
        return ToolOutcome('{"ran":true}', {})

    toolbox.entries["shell"] = ToolEntry(toolbox.entries["shell"].spec, "effect", shell)
    refused = await toolbox.call(ToolCall("c1", "shell", '{"script": "true"}'), "op-ungated")
    assert "not active" in json.loads(refused.text)["error"]["message"]
    assert dispatched == []
    # A class the fence does not know is checked like an effect.
    with pytest.raises(DocumentError, match="not active"):
        await f.adapter.check_dispatch(cast(Effect, "future"))
    await f.adapter.check_dispatch("plan")


@pytest.mark.asyncio
async def test_user_change_read_requires_successful_model_boundary_and_blocks_same_batch(fixture):
    f = fixture
    ref, _ = await active(f)
    f.core.revise(
        f.user,
        "user-edit",
        f.core.summary(f.user).head,
        PlanBody.model_validate(body("USER refined goal")),
        "Steering",
    )
    await f.adapter.safe_point()
    assert f.adapter.requires_read
    await call(f, "plan_read")
    with pytest.raises(DocumentError, match="reread_required"):
        await f.adapter.check_dispatch("effect")
    await f.adapter.check_dispatch("read")
    await f.adapter.check_dispatch("cancel")
    await f.adapter.model_delivered()
    await f.adapter.check_dispatch("effect")
    # A historical read is valid history and cannot clear the next USER change.
    f.core.revise(
        f.user,
        "user-again",
        f.core.summary(f.user).head,
        PlanBody.model_validate(body("Second USER goal")),
        "Steering",
    )
    await call(f, "plan_read", view="version", ref=ref)
    await f.adapter.model_delivered()
    assert f.adapter.requires_read


@pytest.mark.asyncio
async def test_old_read_race_and_failed_model_do_not_clear_current_read_requirement(fixture):
    f = fixture
    await active(f)
    await call(f, "plan_read")
    f.core.revise(
        f.user,
        "edit",
        f.core.summary(f.user).head,
        PlanBody.model_validate(body("Changed while model runs")),
        "Steering",
    )
    await f.adapter.model_delivered()
    await f.adapter.safe_point()
    assert f.adapter.requires_read
    model = ScriptedModel([ModelTurn(None, (ToolCall("read", "plan_read", "{}"),))])
    original = model.complete

    async def fail_second(messages, tools):
        if model.requests:
            raise ModelError("test-secret-failure-body")
        return await original(messages, tools)

    model.complete = fail_second
    result = await run_task(model, tools(f), "Resume", run_id="main-run")
    assert result.failure == "model"
    assert f.adapter.requires_read
    assert "test-secret-failure-body" not in json.dumps(result.messages)


@pytest.mark.asyncio
async def test_exact_replay_is_history_not_current_phase_and_preserves_model_author(fixture):
    f = fixture
    ref, _ = await active(f)
    args = {
        "operation_id": "edit",
        "base": ref,
        "body": body("Original MAIN refinement"),
        "reason": "Refine",
    }
    first, _ = await call(f, "plan_revise", **args)
    again, audit = await f.adapter.call(
        "plan_revise", json.dumps(args), call_id="new-call", model="actual-model"
    )
    assert json.loads(again)["receipt"] == first["receipt"]
    assert json.loads(again)["historical_receipt"]
    assert audit["plan_call_id"] == "new-call"
    assert f.adapter.pending_read is None
    with pytest.raises(DocumentError, match="operation"):
        await f.adapter.call(
            "plan_revise", json.dumps(args), call_id="third", model="different-model"
        )
    f.core.revise(
        f.user,
        "user",
        f.core.summary(f.user).head,
        PlanBody.model_validate(body("USER goal")),
        "Steering",
    )
    with pytest.raises(DocumentError, match="reread_required"):
        await call(f, "plan_revise", **args)
    assert f.core.version(f.user, f.core.summary(f.user).head).body.goal == "USER goal"


@pytest.mark.asyncio
async def test_status_cas_race_and_awaiting_approval_cannot_be_bypassed(fixture, monkeypatch):
    f = fixture
    ref, revision = await active(f)
    original = f.core.progress

    def raced(*args, **kwargs):
        assert kwargs["expected_status_revision"] == revision
        summary = f.core.summary(f.user)
        f.core.set_status(
            f.user,
            "wait",
            summary.head,
            summary.status_revision,
            summary.progress_cursor,
            "awaiting_approval",
            "USER review requested",
        )
        return original(*args, **kwargs)

    monkeypatch.setattr(f.core, "progress", raced)
    with pytest.raises(DocumentError, match="status"):
        await call(
            f,
            "plan_progress",
            operation_id="progress",
            base=ref,
            expected_step_cursor=0,
            expected_status_revision=revision,
            step_id="step-0",
            status="in_progress",
            note="Start",
        )
    assert f.core.current(f.user).steps[0].latest is None
    await read_and_deliver(f)
    for name, extra in [("plan_activate", {}), ("plan_finish", {"status": "abandoned"})]:
        summary = f.core.summary(f.user)
        with pytest.raises(DocumentError, match="real USER"):
            await call(
                f,
                name,
                operation_id=name,
                base=ref,
                expected_status_revision=summary.status_revision,
                expected_progress_cursor=summary.progress_cursor,
                reason="Try",
                **extra,
            )
    with pytest.raises(DocumentError, match="not active"):
        await f.adapter.check_dispatch("effect")
    await call(
        f, "plan_revise", operation_id="refine", base=ref, body=body("Clarify"), reason="Clarify"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["close", "aba"])
async def test_original_registry_lifetime_is_not_refreshed_after_change(fixture, change):
    f = fixture
    f.projects.close_session("p", "s")
    if change == "aba":
        f.projects.open_session("p", "s", dedicated=False)
    with pytest.raises(DocumentError, match="planning"):
        await made(f)
    with pytest.raises(DocumentError, match="planning"):
        await f.adapter.check_dispatch("effect")
    await f.adapter.check_dispatch("read")


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["second_session", "other_closed", "policy"])
async def test_other_project_changes_keep_this_runs_effect_tools(fixture, change):
    f = fixture
    if change in {"second_session", "other_closed"}:
        # A second workbook opens in the same project while this run continues.
        f.projects.open_session("p", "other", dedicated=False)
    if change == "other_closed":
        f.projects.close_session("p", "other")
    if change == "policy":
        # The worker enforces the project's current policy on each effect.
        f.projects.set_policy("p", "sha256:" + "b" * 64)
    await f.adapter.check_dispatch("effect")
    ref, _ = await active(f)
    assert ref["version"] == 1
    await f.adapter.check_dispatch("effect")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "arguments",
    [
        '{"role":"user"}',
        '{"view":"current","after":true}',
        '{"limit":9}',
        '{"limit":"1"}',
        '{"view":"current","ref":{"plan_id":"p","version":1}}',
        '{"view":"version"}',
        '{"view":"current","after":1}',
        '{"view":"version","ref":{"plan_id":"p","version":1},"after":1}',
        '"' + "x" * MAX_REQUEST_BYTES + '"',
        '{"view":"\ud800"}',
    ],
)
async def test_strict_authority_scope_cursor_and_utf8_request_bounds(fixture, arguments):
    with pytest.raises(DocumentError):
        await fixture.adapter.call("plan_read", arguments, call_id="bad", model=None)
    assert fixture.core.summary(fixture.adapter.authority) is None


@pytest.mark.asyncio
async def test_long_progress_notes_do_not_prevent_complete_compact_current_reread(fixture):
    f = fixture
    receipt = f.core.publish(
        f.adapter.authority, "large", PlanBody.model_validate(body(count=44)), "Large"
    )
    for i in range(44):
        f.core.progress(
            f.adapter.authority,
            f"progress-{i}",
            receipt.ref,
            f"step-{i}",
            0,
            "blocked",
            "😀" * 4096,
        )
    with pytest.raises(DocumentError, match="current plan read limit"):
        f.core.current(f.adapter.authority)
    await f.adapter.safe_point()
    result, _ = await call(f, "plan_read")
    assert len(result["steps"]) == 44
    assert all(
        s["status"] == "blocked" and s["latest"]["author"]["run_id"] == "main-run"
        for s in result["steps"]
    )
    assert len(json.dumps(result, ensure_ascii=False).encode()) < MAX_RESPONSE_BYTES
    await f.adapter.model_delivered()
    assert not f.adapter.requires_read
    page, _ = await call(f, "plan_read", view="progress", ref=receipt.ref.model_dump(mode="json"))
    assert page["more"]
    assert page["items"][0]["note"] == "😀" * 4096


@pytest.mark.asyncio
async def test_user_edit_during_closing_await_is_not_presented_as_current_answer(
    fixture, monkeypatch
):
    f = fixture
    await active(f)

    class Math:
        def __init__(self):
            self.seen = set()
            self.delivered = set()

        @asynccontextmanager
        async def active(self):
            yield

        def safe_point(self):
            pass

        async def closing(self):
            f.core.revise(
                f.user,
                "closing-edit",
                f.core.summary(f.user).head,
                PlanBody.model_validate(body("USER closing refinement")),
                "Steering",
            )
            return {"pending": False}

        async def notices(self):
            return []

    toolbox = tools(f)
    monkeypatch.setattr(toolbox, "math_checks", Math())
    result = await run_task(
        ScriptedModel([ModelTurn("Stale answer")]),
        toolbox,
        "Continue",
        run_id="main-run",
        max_turns=1,
    )
    assert result.stopped == "user_message"
    assert result.final_text is None
    assert "USER closing refinement" in f.core.current(f.user).version.body.goal


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["model", "cancel", "save"])
async def test_committed_progress_survives_later_failure_without_fabricating_completion(
    fixture, failure, monkeypatch
):
    f = fixture
    ref, revision = await active(f)
    first = turn(
        "plan_progress",
        operation_id="start",
        base=ref,
        step_id="step-0",
        expected_step_cursor=0,
        expected_status_revision=revision,
        status="in_progress",
        note="Actually started",
    )

    class Model:
        calls = 0

        async def complete(self, messages, tools):
            self.calls += 1
            if self.calls == 1:
                return first
            if failure == "cancel":
                raise asyncio.CancelledError
            if failure == "model":
                raise ModelError("test-secret-upstream-body")
            return ModelTurn("Partial progress")

    if failure == "cancel":
        with pytest.raises(asyncio.CancelledError):
            await run_task(Model(), tools(f), "Continue", run_id="main-run")
    else:
        result = await run_task(Model(), tools(f), "Continue", run_id="main-run")
        if failure == "model":
            assert result.failure == "model"
            assert result.final_text is None
        else:

            def failed_save(*args):
                raise OSError("synthetic fsync failure")

            monkeypatch.setattr(ProjectStore, "save_run", failed_save)
            now = datetime.now(UTC)
            assert (
                await save(
                    {
                        "run_id": "main-run",
                        "context": f.context,
                        "task": "Continue",
                        "started_at": now,
                        "ended_at": now,
                        "stopped": result.stopped,
                        "messages": result.messages,
                        "tools": result.tool_records,
                        "final_text": result.final_text,
                    },
                    0,
                    (),
                    projects=f.projects,
                    error=print,
                )
                == 1
            )
            assert f.projects.runs("p", "s") == []
    current = f.core.current(f.adapter.authority)
    assert current.head.status == "active"
    assert current.head.outcome is None
    assert current.steps[0].latest.status == "in_progress"


@pytest.mark.asyncio
async def test_cancelled_mutation_drains_actual_thread_before_store_lifetime_ends(
    fixture, monkeypatch
):
    f = fixture
    entered, release = threading.Event(), threading.Event()
    original = f.core.publish

    def blocked(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return original(*args, **kwargs)

    monkeypatch.setattr(f.core, "publish", blocked)
    task = asyncio.create_task(made(f))
    await asyncio.to_thread(entered.wait, 5)
    task.cancel()
    task.cancel()
    await asyncio.sleep(0.01)
    assert not task.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert f.core.summary(f.adapter.authority).status == "draft"


@pytest.mark.asyncio
async def test_cli_cancelled_constructor_closes_owner_published_before_thread_return(
    tmp_path, monkeypatch
):
    projects = ProjectStore(tmp_path)
    projects.create("p", "minimal", "sha256:" + "a" * 64)
    projects.open_session("p", "s", dedicated=False)
    entered, release = threading.Event(), threading.Event()
    owners = []
    closed = []
    original = PlanTools

    def factory(*args, **kwargs):
        owner = original(*args, **kwargs)
        owners.append(owner)
        entered.set()
        assert release.wait(5)
        return owner

    class Client:
        base_url = "http://unused.invalid"

        def __init__(self, *args):
            pass

        async def __aenter__(self):
            return self

        async def environment(self, environment_id):
            raise WorkerAPIError(ErrorKind.UNSUPPORTED, "a local worker manages no environments")

        async def __aexit__(self, *args):
            closed.append("client")

        async def capabilities(self, *args):
            return SimpleNamespace(
                languages=(),
                features=(),
                location=SimpleNamespace(
                    isolation_evidence=None,
                    pool_id="p",
                    backend="local",
                    sandbox_id="e",
                    hostname="test",
                ),
            )

    script = tmp_path / "script.json"
    script.write_text('[{"text":"Never called"}]')
    args = argparse.Namespace(
        project="p",
        session="s",
        environment=None,
        dedicated=False,
        research_template=None,
        script=script,
        run_id="run",
        worker="unused",
        token_file=None,
        allow_unisolated=True,
        state_dir=tmp_path,
        watcher_model=None,
        reviewer_model=None,
        math_node=None,
        watcher_calls_per_hour=1,
        watcher_first_check=1,
        watcher_interval=1,
        task="Continue",
        max_turns=1,
        transcript=None,
    )
    monkeypatch.setattr(harness, "PlanTools", factory)
    monkeypatch.setattr(cli, "WorkerClient", Client)
    monkeypatch.setattr(cli, "_read_secret", lambda _: "test-secret-unused")
    task = asyncio.create_task(cli._run(args))
    assert await asyncio.to_thread(entered.wait, 5)
    descriptor = owners[0].snapshot.descriptor
    task.cancel()
    task.cancel()
    await asyncio.sleep(0.01)
    assert not task.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    with pytest.raises(OSError, match="Bad file descriptor"):
        os.fstat(descriptor)
    assert closed == ["client"]
    assert projects.runs("p", "s") == []


@pytest.mark.asyncio
async def test_actual_same_batch_current_read_cannot_authorize_effect_until_next_model(
    fixture, monkeypatch
):
    f = fixture
    await active(f)
    f.core.revise(
        f.user,
        "steer",
        f.core.summary(f.user).head,
        PlanBody.model_validate(body("USER goal")),
        "Steering",
    )
    toolbox = tools(f)
    dispatched = []

    async def shell(request):
        dispatched.append(request.operation)
        return ToolOutcome('{"observed":"synthetic effect"}', {"observed": "synthetic effect"})

    monkeypatch.setitem(
        toolbox.entries, "shell", ToolEntry(toolbox.entries["shell"].spec, "effect", shell)
    )
    model = ScriptedModel(
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
    result = await run_task(model, toolbox, "Follow USER steering", run_id="main-run")
    assert result.stopped == "answered"
    assert len(dispatched) == 1
    assert "plan_reread_required" in model.requests[1][-1]["content"]
    assert result.tool_records[-1]["observed"] == "synthetic effect"


@pytest.mark.asyncio
async def test_scoped_event_scan_has_a_finite_hold_and_complete_read_can_clear_it(fixture):
    f = fixture
    ref, _ = await active(f)
    before = f.adapter.cursor
    for index in range(70):
        f.core.progress(
            f.user,
            f"u-{index}",
            f.core.summary(f.user).head,
            "step-0",
            index,
            "pending",
            "USER assessment",
        )
    notice = await f.adapter.safe_point()
    assert notice["event_scan_held"]
    assert f.adapter.cursor == before + 64
    with pytest.raises(DocumentError, match="reread_required"):
        await f.adapter.check_dispatch("effect")
    page, _ = await call(f, "plan_read")
    assert page["head"]["ref"] == ref
    assert page["steps"][0]["step_cursor"] == 70
    await f.adapter.model_delivered()
    assert not f.adapter.held
    assert f.adapter.cursor == f.core.summary(f.user).event_cursor


@pytest.mark.asyncio
async def test_repeated_unread_user_edits_do_not_create_unbounded_response_rounds(fixture):
    f = fixture
    await active(f)

    class Model:
        calls = 0

        async def complete(self, messages, tools):
            self.calls += 1
            f.core.revise(
                f.user,
                f"steer-{self.calls}",
                f.core.summary(f.user).head,
                PlanBody.model_validate(body(f"USER goal {self.calls}")),
                "Steering",
            )
            return ModelTurn("A superseded answer")

    model = Model()
    result = await run_task(model, tools(f), "Continue", run_id="main-run", max_turns=30)
    assert model.calls == 2
    assert result.stopped == "user_message"
    assert result.final_text is None
    assert f.core.summary(f.user).head.version == 3


@pytest.mark.asyncio
async def test_complete_large_plan_bypasses_generic_notice_clipping_without_marking_delivery(
    fixture,
):
    f = fixture
    f.core.publish(f.user, "wide", PlanBody.model_validate(body(count=128)), "USER plan")

    class Documents:
        specs = ()
        reads = 0

        async def notices(self):
            self.reads += 1
            return ["Unseen document notice"]

    class Jobs:
        reads = 0

        def __init__(self):
            self.seen = set()

        async def notices(self, **kwargs):
            self.reads += 1
            self.seen.add("job")
            return [{"job_id": "job"}]

    documents, jobs = Documents(), Jobs()
    toolbox = Toolbox(
        worker(),
        f.context,
        plans=f.adapter,
        features=("jobs",),
        documents=cast(DocumentTool, documents),
        jobs=cast(JobTools, jobs),
    )
    outcome = await toolbox.call(ToolCall("read", "plan_read", "{}"), "read")
    assert len(outcome.text) > toolbox.output_chars
    assert len(json.loads(outcome.text)["steps"]) == 128
    assert "platform_notices" not in json.loads(outcome.text)
    assert documents.reads == jobs.reads == 0
    assert jobs.seen == set()
    await f.adapter.model_delivered()
    assert not f.adapter.requires_read
