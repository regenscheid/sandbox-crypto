# SPDX-License-Identifier: MIT
"""Actual MAIN planning, current-read fences and durable failure boundaries."""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import threading
from collections.abc import Sequence
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast

import pytest

from sandbox_science.contracts import ErrorKind, ExecutionContext
from sandbox_science.orchestrator import cli, harness
from sandbox_science.orchestrator.consistency.controller import ReviewController
from sandbox_science.orchestrator.consistency.store import ReviewStore
from sandbox_science.orchestrator.continuity.auto_continuation import continued
from sandbox_science.orchestrator.continuity.records import WorkRecords
from sandbox_science.orchestrator.documents import DocumentStore, DocumentTool
from sandbox_science.orchestrator.documents.errors import DocumentError
from sandbox_science.orchestrator.harness import save
from sandbox_science.orchestrator.hooks import Closing, Effect, Notice, RunView, TurnHook
from sandbox_science.orchestrator.jobs import JOB_SPECS, JobTools
from sandbox_science.orchestrator.loop import run_task
from sandbox_science.orchestrator.model import ModelError, ModelTurn
from sandbox_science.orchestrator.notices import PLANS, PLANS_HELD, PLANS_WENT_ON
from sandbox_science.orchestrator.plan_tools import (
    APPROVALS_SEEN,
    GUIDANCE,
    GUIDANCE_VERSION,
    MAX_CHANGES,
    MAX_REQUEST_BYTES,
    MAX_RESPONSE_BYTES,
    NAMES,
    PLAN_OPEN,
    REREAD,
    UNAVAILABLE,
    PlanNotice,
    Plans,
    PlanTools,
    approval_open,
)
from sandbox_science.orchestrator.plan_tools import went_on as went_on_id
from sandbox_science.orchestrator.projects import ProjectStore
from sandbox_science.orchestrator.questions import Questions
from sandbox_science.orchestrator.session_plans import (
    GoalOutcome,
    PlanAuthority,
    PlanBody,
    PlanRef,
    PlanScope,
    SessionPlans,
)
from sandbox_science.orchestrator.tools import Toolbox, ToolEntry, ToolOutcome
from sandbox_science.orchestrator.worker_client import WorkerAPIError, WorkerClient

from .scripted import ScriptedModel, ToolCall
from .test_tool_journal import RUN


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
    adapter.projects.close()
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


def shown(notice: Notice | PlanNotice) -> dict[str, Any]:
    """A plans notice's payload."""
    return cast(dict[str, Any], notice.payload)


READS = {
    "read_file",
    "list_files",
    "glob",
    "grep",
    "pdf_info",
    "pdf_pages",
    "pdf_search",
    "document_list",
    "document_read",
    "show_outline",
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
    assert "test-secret-failure-body" not in json.dumps(result.context.messages)


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
async def test_a_status_race_refuses_progress_and_a_draft_holds_effects(fixture, monkeypatch):
    f = fixture
    ref, revision = await active(f)
    original = f.adapter.plans.progress

    def raced(*args, **kwargs):
        assert kwargs["expected_status_revision"] == revision
        summary = f.core.summary(f.user)
        f.core.set_status(
            f.user,
            "wait",
            summary.head,
            summary.status_revision,
            summary.progress_cursor,
            "draft",
            "USER returns the plan to a draft",
        )
        return original(*args, **kwargs)

    monkeypatch.setattr(f.adapter.plans, "progress", raced)
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
@pytest.mark.parametrize("max_turns", [1, 3])
async def test_a_user_edit_during_the_closing_await_holds_the_answer_once_if_a_turn_remains(
    fixture, monkeypatch, max_turns
):
    """The answer of a run with no turn left stands: it was lost as ``plan_unread``. With a
    turn left, the plan comes with the held answer and the next answer is taken. The run's
    first answer at the open plan's state went on in the run (``Plans.goes_on``); the
    second has its closing."""
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
            if f.core.summary(f.user).head.version == 1:  # the USER edits once
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
    model = ScriptedModel(
        [ModelTurn("Status"), ModelTurn("First answer"), ModelTurn("Answer with the plan")]
    )
    if max_turns == 1:
        model = ScriptedModel([ModelTurn("First answer")])
    result = await run_task(model, toolbox, "Continue", run_id="main-run", max_turns=max_turns)
    assert "USER closing refinement" in f.core.current(f.user).version.body.goal
    if max_turns == 1:
        assert (result.stopped, result.final_text) == ("answered", "First answer")
        return
    assert (result.stopped, result.final_text) == ("answered", "Answer with the plan")
    held = model.requests[2][-1]["content"]
    payload = json.loads(held[len(PLANS_HELD.text) :])
    assert "USER closing refinement" in payload["plan"]
    assert payload["changed"].startswith("the user revised it to version 2")
    assert payload["answer_held"].startswith("Your answer was held once")


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
                        "messages": result.context.messages,
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
    original = f.adapter.plans.publish

    def blocked(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return original(*args, **kwargs)

    monkeypatch.setattr(f.adapter.plans, "publish", blocked)
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

        async def fence(self, request):
            return request

        async def environment(self, environment_id):
            raise WorkerAPIError(ErrorKind.UNSUPPORTED, "a local worker manages no environments")

        async def __aexit__(self, *args):
            closed.append("client")

        async def claim_environment(self, request):
            from sandbox_science.contracts.environment_claims import EnvironmentClaimResult

            return EnvironmentClaimResult(
                environment_id=request.context.environment_id, state="unmanaged"
            )

        async def capabilities(self, *args):
            return SimpleNamespace(
                languages=(),
                features=(),
                kernel_reason=None,
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
        review_token_budget=400000,
        review_calls_per_hour=240,
        watcher_first_check=1,
        watcher_interval=1,
        task="Continue",
        max_turns=1,
        max_input_chars=400_000,
        transcript=None,
    )
    monkeypatch.setattr(harness, "PlanTools", factory)
    monkeypatch.setattr(cli, "WorkerClient", Client)
    monkeypatch.setattr(cli, "read_secret", lambda _: "test-secret-unused")
    task = asyncio.create_task(cli._run(args))
    assert await asyncio.to_thread(entered.wait, 5)
    task.cancel()
    task.cancel()
    await asyncio.sleep(0.01)
    assert not task.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(owners) == 1
    assert closed == ["client"]
    assert projects.runs("p", "s") == []


@pytest.mark.asyncio
async def test_actual_same_batch_current_read_cannot_authorize_effect_until_next_model(
    fixture, monkeypatch
):
    f = fixture
    await active(f)
    toolbox = tools(f)
    dispatched = []

    async def shell(request):
        dispatched.append(request.operation)
        return ToolOutcome('{"observed":"synthetic effect"}', {"observed": "synthetic effect"})

    monkeypatch.setitem(
        toolbox.entries, "shell", ToolEntry(toolbox.entries["shell"].spec, "effect", shell)
    )

    class Model(ScriptedModel):
        async def complete(self, messages, tools):
            if not self.requests:  # the USER steers while the model works
                f.core.revise(
                    f.user,
                    "steer",
                    f.core.summary(f.user).head,
                    PlanBody.model_validate(body("USER goal")),
                    "Steering",
                )
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
    result = await run_task(model, toolbox, "Follow USER steering", run_id="main-run")
    assert result.stopped == "answered"
    assert len(dispatched) == 1
    refused, notice = model.requests[1][-2:]
    assert "plan_reread_required" in refused["content"]
    # The next turn's notice shows the USER's plan, and its delivery let the effect run.
    assert "USER goal" in notice["content"]
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
    assert notice is not None
    assert f.adapter.held
    assert shown(notice)["changed"].endswith("; and more changes than this lists")
    # The scan reached 64 events: the run's own activation, then 63 of the USER's.
    assert shown(notice)["changed"].startswith(
        f"after {63 - MAX_CHANGES} earlier changes, the user recorded progress on a step "
        f"{MAX_CHANGES} times"
    )
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
async def test_a_delivered_notice_clears_a_held_scan_as_a_current_read_does(fixture):
    f = fixture
    await active(f)
    for index in range(70):
        f.core.progress(
            f.user, f"u-{index}", f.core.summary(f.user).head, "step-0", index, "pending", "USER"
        )
    hook = Plans(f.adapter)
    (notice,) = await hook.notices("turn", cast(RunView, None))
    assert f.adapter.held
    with pytest.raises(DocumentError, match="reread_required"):
        await f.adapter.check_dispatch("effect")
    assert await hook.notices("turn", cast(RunView, None)) == ()  # shown once
    await hook.model_returned(cast(RunView, None))
    assert not f.adapter.held
    assert not f.adapter.requires_read
    assert f.adapter.cursor == f.core.summary(f.user).event_cursor
    await f.adapter.check_dispatch("effect")
    assert shown(notice)["steps"] == ["step-0 pending: Step 0"]


@pytest.mark.asyncio
async def test_repeated_unread_user_edits_hold_one_answer_once(fixture):
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
    assert (result.stopped, result.final_text) == ("answered", "A superseded answer")
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
        specs = JOB_SPECS

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


@pytest.mark.asyncio
async def test_plan_tools_aba_after_the_precheck_is_refused_in_the_transaction(
    fixture, monkeypatch, tmp_path
):
    """A close and reopen landing between the run's check and the plan transaction is
    refused in the transaction, and ends the run's planning."""
    original = fixture.adapter.snapshot

    def stale(project, session):
        other = ProjectStore(tmp_path / "state")
        try:
            other.close_session("p", "s")
            other.open_session("p", "s", dedicated=False)
        finally:
            other.close()
        return original  # what the check read just before the close and reopen

    monkeypatch.setattr(fixture.projects, "session_snapshot", stale)
    with pytest.raises(DocumentError, match="closed or reopened") as refused:
        await made(fixture)
    assert refused.value.kind == "policy_denied"
    assert fixture.adapter.invalid
    assert fixture.core.summary(fixture.user) is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "statuses", [("completed", "blocked"), ("skipped", "completed"), ("blocked", "blocked")]
)
async def test_a_plan_with_no_open_step_holds_no_answer_back(fixture, statuses):
    """Lab 41, B2: every run starts with the plan unread, and a plan whose steps were all
    done or blocked still discarded the answer of a run asked to answer without tools."""
    f = fixture
    receipt = f.core.publish(f.user, "plan", PlanBody.model_validate(body(count=2)), "Plan")
    for index, status in enumerate(statuses):
        f.core.progress(f.user, f"p-{index}", receipt.ref, f"step-{index}", 0, status, "Done")
    model = ScriptedModel([ModelTurn("The user's words, quoted.")])
    result = await run_task(model, tools(f), "Quote me, without tools", run_id="main-run")
    assert (result.stopped, result.final_text) == ("answered", "The user's words, quoted.")


@pytest.mark.asyncio
async def test_a_held_answer_shows_the_plan_and_the_next_answer_is_taken(fixture):
    """Lab 41, B2: the retry came with no notice, as the plan's state notice had not
    changed. Lab round 2: a second unread closing stopped the run as ``plan_unread`` and
    dropped its answer."""
    f = fixture
    receipt = f.core.publish(f.user, "plan", PlanBody.model_validate(body(count=2)), "Plan")

    class Model(ScriptedModel):
        async def complete(self, messages, tools):
            if not self.requests:  # the USER records progress while the model works
                f.core.progress(f.user, "p-0", receipt.ref, "step-0", 0, "completed", "Done")
            return await super().complete(messages, tools)

    model = Model([ModelTurn("First answer"), ModelTurn("Second answer")])
    result = await run_task(model, tools(f), "Answer", run_id="main-run", max_turns=5)
    assert (result.stopped, result.final_text) == ("answered", "Second answer")
    held = model.requests[1][-1]["content"]
    payload = json.loads(held[len(PLANS_HELD.text) :])
    assert payload["answer_held"].startswith("Your answer was held once")
    assert payload["steps"] == ["step-0 completed: Step 0", "step-1 pending: Step 1"]
    assert (payload["current"], payload["changed"]) == (
        "next open step: step-1",
        "the user recorded step-0 completed",
    )
    assert not f.adapter.requires_read  # the held notice's delivery read the plan


@pytest.mark.asyncio
async def test_a_report_round_answer_stands_although_the_plan_changed_unread(fixture):
    """At its turn limit a run's report is its last answer: a plan change it could not
    read no longer drops it."""
    f = fixture
    await active(f)

    class Model(ScriptedModel):
        async def complete(self, messages, tools):
            if len(self.requests) == 1:  # the USER steers during the report round
                f.core.revise(
                    f.user,
                    "steer",
                    f.core.summary(f.user).head,
                    PlanBody.model_validate(body("USER goal")),
                    "Steering",
                )
            return await super().complete(messages, tools)

    model = Model([turn("plan_read"), ModelTurn("The report so far")])
    result = await run_task(model, tools(f), "Continue", run_id="main-run", max_turns=1)
    assert (result.stopped, result.final_text) == ("turn_limit", "The report so far")


@pytest.mark.asyncio
async def test_the_first_notice_carries_the_plan_and_lets_the_first_effects_run(fixture):
    """Lab round 2: 43 calls refused as ``plan_reread_required``, 42 of them at a plan an
    earlier run had finished, each after a notice of hashes and cursors."""
    f = fixture
    earlier = PlanAuthority(
        scope=PlanScope(project_id="p", session_id="s"), role="main", run_id="earlier-run"
    )
    receipt = f.core.publish(earlier, "op-create", PlanBody.model_validate(body()), "Initial")
    summary = f.core.summary(earlier)
    f.core.set_status(earlier, "op-go", receipt.ref, summary.status_revision, 0, "active", "Begin")
    f.core.progress(earlier, "op-done", receipt.ref, "step-0", 0, "completed", "Done")
    summary = f.core.summary(earlier)
    f.core.set_status(
        earlier,
        "op-finish",
        receipt.ref,
        summary.status_revision,
        summary.progress_cursor,
        "completed",
        "Finished",
        outcome=GoalOutcome(status="achieved", summary="Checked", evidence=(), remaining_work=()),
    )
    toolbox = tools(f)
    dispatched = []

    async def shell(request):
        dispatched.append(request.operation)
        return ToolOutcome('{"ran":true}', {})

    toolbox.entries["shell"] = ToolEntry(toolbox.entries["shell"].spec, "effect", shell)
    model = ScriptedModel([turn("shell", script="true"), ModelTurn("Done")])
    result = await run_task(model, toolbox, "Extend the work", run_id="main-run")
    assert (result.stopped, len(dispatched)) == ("answered", 1)
    (notice,) = [m["content"] for m in model.requests[0] if PLANS.text in (m["content"] or "")]
    payload = json.loads(notice[notice.index(PLANS.text) + len(PLANS.text) :])
    assert payload == {
        "status": "completed",
        "plan": f"{receipt.ref.plan_id} version 1, completed: Check the claim",
        "steps": ["step-0 completed: Step 0"],
        "current": "none: the plan is completed",
        "changed": "last change: main run earlier-run set it completed",
        "outcome": "achieved: Checked",
    }
    # No digests, cursors or operation IDs: the record and plan_read keep those.
    assert not re.search(r"[0-9a-f]{64}|cursor|op-", notice)
    assert len(notice.encode()) < 1024
    # No notice after the read: the next turn and the closing bring none.
    assert [PLANS.text in (m["content"] or "") for m in model.requests[1]].count(True) == 1


@pytest.mark.asyncio
async def test_the_runs_own_changes_bring_no_notice_and_others_are_named(fixture):
    f = fixture
    hook = Plans(f.adapter)
    view = cast(RunView, None)
    assert await hook.notices("turn", view) == ()  # no plan: nothing to show
    ref, revision = await active(f)
    await call(
        f,
        "plan_progress",
        operation_id="start",
        base=ref,
        step_id="step-0",
        expected_step_cursor=0,
        expected_status_revision=revision,
        status="in_progress",
        note="Started",
    )
    assert await hook.notices("turn", view) == ()  # the run's own changes
    await f.adapter.check_dispatch("effect")
    other = PlanAuthority(
        scope=PlanScope(project_id="p", session_id="s"), role="main", run_id="other-run"
    )
    f.core.revise(
        f.user,
        "user-edit",
        PlanRef.model_validate(ref),
        PlanBody.model_validate(body("USER goal", count=3)),
        "Two more steps",
    )
    current = f.core.summary(f.user)
    f.core.progress(other, "other-done", current.head, "step-2", 0, "completed", "Done")
    with pytest.raises(DocumentError) as refused:
        await f.adapter.check_dispatch("effect")
    assert refused.value.message == REREAD
    (notice,) = await hook.notices("turn", view)
    assert shown(notice)["changed"] == (
        "the user revised it to version 2 (added step-1, step-2; reason: Two more steps); "
        "main run other-run recorded step-2 completed"
    )
    assert shown(notice)["steps"] == [
        "step-0 in_progress: Step 0",
        "step-1 pending: Step 1",
        "step-2 completed: Step 2",
    ]
    assert shown(notice)["current"] == "in progress: step-0"
    await hook.model_returned(view)
    await f.adapter.check_dispatch("effect")
    assert await hook.notices("turn", view) == ()


@pytest.mark.asyncio
async def test_a_childs_progress_on_its_track_is_named_with_its_track(fixture):
    f = fixture
    scope = PlanScope(project_id="p", session_id="s")
    tracked = body(count=2)
    tracked["tracks"] = ({"track_id": "tables", "title": "Tables"},)
    tracked["phases"][0]["steps"][1]["track_id"] = "tables"
    receipt = f.core.publish(f.user, "plan", PlanBody.model_validate(tracked), "Plan")
    summary = f.core.summary(f.user)
    f.core.set_status(f.user, "go", receipt.ref, summary.status_revision, 0, "active", "Go")
    hook = Plans(f.adapter)
    view = cast(RunView, None)
    await hook.notices("turn", view)
    await hook.model_returned(view)
    child = PlanAuthority(
        scope=scope, role="delegated", run_id="child-run", grant=receipt.ref, track_id="tables"
    )
    f.core.progress(child, "child-step", receipt.ref, "step-1", 0, "blocked", "No table")
    (notice,) = await hook.notices("turn", view)
    assert shown(notice)["changed"] == (
        "delegated run child-run (track tables) recorded step-1 blocked"
    )
    assert shown(notice)["steps"][1] == "step-1 [track tables] blocked: Step 1"


@pytest.mark.asyncio
async def test_an_unreadable_plan_is_said_once_and_holds_effects(fixture):
    f = fixture
    await active(f)
    await read_and_deliver(f)
    f.projects.close_session("p", "s")
    hook = Plans(f.adapter)
    view = cast(RunView, None)
    (notice,) = await hook.notices("turn", view)
    assert shown(notice) == {"status": "unavailable", "plan": UNAVAILABLE}
    assert await hook.notices("turn", view) == ()
    with pytest.raises(DocumentError, match="planning"):
        await f.adapter.check_dispatch("effect")


@pytest.mark.asyncio
async def test_a_revision_names_the_steps_it_added_changed_and_removed(fixture):
    f = fixture
    ref, revision = await active(f)
    await call(
        f,
        "plan_progress",
        operation_id="start",
        base=ref,
        step_id="step-0",
        expected_step_cursor=0,
        expected_status_revision=revision,
        status="in_progress",
        note="Started",
    )
    revised = body(count=11)
    steps = revised["phases"][0]["steps"]
    revised["phases"][0]["steps"] = (
        {**steps[0], "description": "Inspect more closely"},
        *steps[2:],
    )
    f.core.revise(
        f.user, "user-edit", PlanRef.model_validate(ref), PlanBody.model_validate(revised), "Wider"
    )
    (notice,) = await Plans(f.adapter).notices("turn", cast(RunView, None))
    assert shown(notice)["changed"] == (
        "the user revised it to version 2 (added step-2, step-3, step-4, step-5, step-6, "
        "step-7, step-8, step-9 and 1 more; changed step-0; reason: Wider)"
    )
    # Its progress was for the step before the edit: the step is open again.
    assert shown(notice)["steps"][0] == "step-0 pending (edited since it was in_progress): Step 0"
    assert shown(notice)["current"] == "next open step: step-0"


async def record(f, ref, revision, step, cursor, status, operation):
    result, _ = await call(
        f,
        "plan_progress",
        operation_id=operation,
        base=ref,
        step_id=step,
        expected_step_cursor=cursor,
        expected_status_revision=revision,
        status=status,
        note="Recorded",
    )
    return result["receipt"]


async def revise(f, ref, revised, operation="revise"):
    result, _ = await call(
        f, "plan_revise", operation_id=operation, base=ref, body=revised, reason="New request"
    )
    return result


ACHIEVED = {"status": "achieved", "summary": "Checked", "evidence": [], "remaining_work": []}


@pytest.mark.asyncio
async def test_a_revision_keeps_unchanged_finished_steps_and_names_the_changed_ones(fixture):
    """A revision that keeps a finished step's ID and text keeps its record; one that
    rewords a finished step says so in its result, by step ID, when the cost is incurred."""
    f = fixture
    ref, revision = await active(f)
    await record(f, ref, revision, "step-0", 0, "completed", "done")
    # A new step and a changed goal leave step-0 as it was: it stays completed.
    wider = body("Check the wider claim", count=2)
    kept = await revise(f, ref, wider)
    assert "finished_steps_changed" not in kept
    current = f.core.compact_current(f.adapter.authority)
    assert current.steps[0].matches_current_step
    assert current.steps[0].latest.status == "completed"
    second = kept["receipt"]["ref"]
    await record(f, second, revision, "step-1", 0, "blocked", "pause")
    # Rewording both finished steps reopens them, and the result names them.
    reworded = body("Check the wider claim", count=2)
    for step in reworded["phases"][0]["steps"]:
        step["description"] = "Inspect and check again"
    changed = await revise(f, second, reworded, "reword")
    assert changed["finished_steps_changed"] == ["step-0", "step-1"]
    assert "record each again before plan_finish" in changed["note"]
    third = changed["receipt"]["ref"]
    outcome = await tools(f).call(
        ToolCall(
            "c",
            "plan_finish",
            json.dumps(
                {
                    "operation_id": "finish",
                    "base": third,
                    "expected_status_revision": revision,
                    "expected_progress_cursor": 2,
                    "reason": "Finished",
                    "outcome": ACHIEVED,
                }
            ),
        ),
        "finish",
    )
    error = json.loads(outcome.text)["error"]
    assert error["kind"] == "conflict"
    assert error["message"] == (
        "the plan's steps are not all finished: step-0, step-1 changed by a revision after "
        "its last record, which was for the step as it was: record each again against "
        "version 3"
    )
    assert error["details"] == {
        "steps": ["step-0", "step-1"],
        "open": [],
        "changed": ["step-0", "step-1"],
        "not_completed": [],
    }
    # Recorded again against the new version, the steps let the plan finish.
    await record(f, third, revision, "step-0", 1, "completed", "again-0")
    await record(f, third, revision, "step-1", 2, "completed", "again-1")
    finished, _ = await call(
        f,
        "plan_finish",
        operation_id="finish",
        base=third,
        expected_status_revision=revision,
        expected_progress_cursor=4,
        reason="Finished",
        outcome=ACHIEVED,
    )
    assert finished["receipt"]["action"] == "status"


@pytest.mark.asyncio
async def test_a_refused_finish_says_each_steps_reason_and_bounds_its_details(fixture):
    f = fixture
    receipt = f.core.publish(f.user, "plan", PlanBody.model_validate(body(count=128)), "Plan")
    f.core.set_status(f.user, "go", receipt.ref, 1, 0, "active", "Go")
    f.core.progress(f.user, "a", receipt.ref, "step-0", 0, "blocked", "Paused by the user")
    f.core.progress(f.user, "b", receipt.ref, "step-1", 0, "completed", "Done")
    await read_and_deliver(f)
    outcome = await tools(f).call(
        ToolCall(
            "c",
            "plan_finish",
            json.dumps(
                {
                    "operation_id": "finish",
                    "base": receipt.ref.model_dump(mode="json"),
                    "expected_status_revision": 2,
                    "expected_progress_cursor": 2,
                    "reason": "Finished",
                    "outcome": ACHIEVED,
                }
            ),
        ),
        "finish",
    )
    error = json.loads(outcome.text)["error"]
    message = error["message"]
    assert message.startswith(
        "the plan's steps are not all finished: step-2, step-3, step-4, step-5, step-6, "
        "step-7, step-8, step-9, step-10, step-11, step-12, step-13, step-14, step-15, "
        "step-16, step-17 and 110 more open (no record, pending or in_progress): record "
        "each completed, blocked or skipped; step-0 blocked or skipped, which an achieved "
        "outcome cannot have"
    )
    # 126 open steps and one blocked one, named twice, are more than a failed call shows.
    details = error["details"]
    assert len(json.dumps(details, separators=(",", ":")).encode()) <= 2048
    shown = len(details["steps"]) + sum(len(details.get(k, [])) for k in ("open", "changed"))
    shown += len(details.get("not_completed", []))
    assert details["steps"][:2] == ["step-0", "step-2"]
    assert shown + details["omitted"] == 2 * 127


@pytest.mark.asyncio
async def test_a_step_the_user_pauses_is_blocked_and_resumes_in_progress(fixture):
    f = fixture
    ref, revision = await active(f)
    await record(f, ref, revision, "step-0", 0, "in_progress", "start")
    await record(f, ref, revision, "step-0", 1, "blocked", "pause")
    resumed = await record(f, ref, revision, "step-0", 2, "in_progress", "resume")
    assert resumed["progress_cursor"] == 3
    assert f.core.compact_current(f.adapter.authority).steps[0].latest.status == "in_progress"
    guidance = " ".join(GUIDANCE.split())
    assert "record the steps they hold blocked with their reason" in guidance
    assert "until the user resumes them, recorded in_progress again" in guidance
    assert "leave finished steps' text as it is" in guidance


class Closings(TurnHook):
    """Stands in for the final review: counts the closings it is asked, and keeps every
    closing answer from the user while it decides, as ``hold`` does, noting whether each
    reply without calls was kept back when the hooks heard its call returned."""

    name = "closings_fixture"

    def __init__(self) -> None:
        self.closings: list[str | None] = []
        self.withheld: list[bool] = []

    @property
    def holds_answers(self) -> bool:
        return True

    async def model_returned(self, run: RunView) -> None:
        last = run.context.messages[-1]
        if last.get("role") == "assistant" and not last.get("tool_calls"):
            self.withheld.append(run.withheld)

    async def closing(self, run: RunView, answer: str | None) -> Closing:
        self.closings.append(answer)
        return Closing()


async def open_plan_run(f, replies, *, max_turns=10):
    """A run of an active two-step plan, the run's own (read, created and activated by it),
    whose model gives ``replies``: the result, the model, and the closings it was asked."""
    await active(f)
    toolbox = tools(f)
    closings = Closings()
    model = ScriptedModel(replies)
    result = await run_task(
        model,
        toolbox,
        "Work the plan",
        run_id="main-run",
        max_turns=max_turns,
        hooks=(*toolbox.turn_hooks(), closings),
    )
    return result, model, closings


def plans_notice(message) -> dict[str, Any]:
    """The payload of the plans notice that carried a run past an answer."""
    content = message["content"]
    assert content.startswith(PLANS_WENT_ON.text)
    return json.loads(content[len(PLANS_WENT_ON.text) :])


@pytest.mark.asyncio
async def test_a_status_reply_while_the_plan_is_open_goes_on_in_the_same_run(fixture):
    """Baseline run 2, R1: the owner's status question, answered without a tool call while
    the plan had pending steps, ended the run, whose final review ran on the status
    reply, and a new run started with nothing cached. The reply is a message, and the run
    takes its next turn with the plan's notice; no closing is asked, nothing holds it."""
    f = fixture
    result, model, closings = await open_plan_run(
        f, [ModelTurn("Status: step-0 is next."), turn("plan_read"), ModelTurn("Done for now.")]
    )
    assert (result.stopped, result.final_text) == ("answered", "Done for now.")
    assert result.answer == "Done for now."
    # Only the run's real end had a closing, and only its answer was kept back meanwhile.
    assert closings.closings == ["Done for now."]
    assert closings.withheld == [False, True]
    went_on = plans_notice(model.requests[1][-1])
    assert went_on["plan_open"] == PLAN_OPEN
    assert went_on["next_steps"] == ["step-0"]
    assert went_on["steps"] == ["step-0 pending: Step 0"]
    assert [m["content"] for m in model.requests[1] if m["role"] == "assistant"][-1] == (
        "Status: step-0 is next."
    )
    summary = result.summaries["plans"]
    assert summary["went_on"] == 1
    assert len(summary["states"]) == 1
    assert result.delivered["plans"] == {went_on_id(tuple(summary["states"][0]))}


@pytest.mark.asyncio
async def test_a_second_bare_reply_at_the_same_plan_state_ends_the_run(fixture):
    """Once per plan state: the run goes on once, and an answer before the plan changes
    has its closing, the final review among it, and ends the run."""
    f = fixture
    result, model, closings = await open_plan_run(
        f, [ModelTurn("Status: step-0 is next."), ModelTurn("Still waiting.")]
    )
    assert (result.stopped, result.final_text) == ("answered", "Still waiting.")
    assert closings.closings == ["Still waiting."]
    assert len(model.requests) == 2
    assert result.summaries["plans"]["went_on"] == 1


@pytest.mark.asyncio
async def test_progress_gives_the_plan_another_continuation(fixture):
    f = fixture
    ref, revision = await active(f)
    toolbox = tools(f)
    closings = Closings()
    progress = turn(
        "plan_progress",
        operation_id="start",
        base=ref,
        step_id="step-0",
        expected_step_cursor=0,
        expected_status_revision=revision,
        status="in_progress",
        note="Started",
    )
    model = ScriptedModel(
        [ModelTurn("First status."), progress, ModelTurn("Second status."), ModelTurn("End.")]
    )
    result = await run_task(
        model,
        toolbox,
        "Work the plan",
        run_id="main-run",
        hooks=(*toolbox.turn_hooks(), closings),
    )
    assert (result.stopped, result.final_text) == ("answered", "End.")
    assert closings.closings == ["End."]
    assert result.summaries["plans"]["went_on"] == 2
    assert plans_notice(model.requests[3][-1])["next_steps"] == ["step-0"]


@pytest.mark.asyncio
@pytest.mark.parametrize("finished", ["completed", "abandoned", "blocked", "draft"])
async def test_a_plan_with_nothing_to_work_lets_the_answer_end_the_run(fixture, finished):
    """A finished plan, a draft, or one whose every open step is blocked: the answer has
    its closing, with the final review, and the run ends."""
    f = fixture
    toolbox = tools(f)
    if finished == "draft":
        await made(f)
    else:
        ref, revision = await active(f)
        if finished == "abandoned":
            await call(
                f,
                "plan_finish",
                operation_id="abandon",
                base=ref,
                expected_status_revision=revision,
                expected_progress_cursor=0,
                status="abandoned",
                reason="The user changed course",
            )
        else:
            done = "blocked" if finished == "blocked" else "completed"
            await record(f, ref, revision, "step-0", 0, done, "finish-step")
            if finished == "completed":
                await call(
                    f,
                    "plan_finish",
                    operation_id="finish",
                    base=ref,
                    expected_status_revision=revision,
                    expected_progress_cursor=1,
                    status="completed",
                    reason="Done",
                    outcome=GoalOutcome(
                        status="achieved", summary="Shown", evidence=(), remaining_work=()
                    ).model_dump(mode="json"),
                )
    assert f.core.summary(f.user).status == ("active" if finished == "blocked" else finished)
    closings = Closings()
    model = ScriptedModel([ModelTurn("The answer.")])
    result = await run_task(
        model, toolbox, "Answer", run_id="main-run", hooks=(*toolbox.turn_hooks(), closings)
    )
    assert (result.stopped, result.final_text) == ("answered", "The answer.")
    assert closings.closings == ["The answer."]
    assert "plans" not in result.summaries


@pytest.mark.asyncio
async def test_an_open_question_lets_the_answer_end_the_run(fixture):
    f = fixture
    Questions(f.documents).ask(f.context, "an-earlier-run", "call-q", "op-q", "Which branch?")
    result, _, closings = await open_plan_run(f, [ModelTurn("Waiting for your answer.")])
    assert (result.stopped, result.final_text) == ("answered", "Waiting for your answer.")
    assert closings.closings == ["Waiting for your answer."]


@pytest.mark.asyncio
async def test_a_run_with_no_turn_left_lets_the_answer_end_the_run(fixture):
    result, _, closings = await open_plan_run(fixture, [ModelTurn("Status.")], max_turns=1)
    assert (result.stopped, result.final_text) == ("answered", "Status.")
    assert closings.closings == ["Status."]


def test_a_permission_request_waits_until_the_same_call_gets_past_it():
    refused = {
        "tool": "shell",
        "arguments": '{"script":"pip install x"}',
        "error": "approval_required",
    }
    other = {"tool": "shell", "arguments": '{"script":"ls"}'}
    passed = {"tool": "shell", "arguments": '{"script":"pip install x"}'}
    assert approval_open([refused])
    assert approval_open([refused, other])
    assert not approval_open([refused, passed])
    assert approval_open([passed, refused])
    assert not approval_open([other])
    # Only the latest records are looked through.
    assert not approval_open([refused, *[other] * APPROVALS_SEEN])


@pytest.mark.asyncio
async def test_a_waiting_permission_request_lets_the_answer_end_the_run(fixture):
    f = fixture
    await active(f)

    async def held(request):
        # As a connector call that gave up waiting for the user's answer records it.
        message = {"error": {"kind": "approval_required", "message": "held for approval"}}
        return ToolOutcome(json.dumps(message), {"error": "approval_required"})

    toolbox = tools(f)
    toolbox.entries["shell"] = ToolEntry(toolbox.entries["shell"].spec, "effect", held)
    closings = Closings()
    model = ScriptedModel([turn("shell", script="fetch"), ModelTurn("Waiting for your approval.")])
    result = await run_task(
        model, toolbox, "Fetch", run_id="main-run", hooks=(*toolbox.turn_hooks(), closings)
    )
    assert result.tool_records[0]["error"] == "approval_required"
    assert (result.stopped, result.final_text) == ("answered", "Waiting for your approval.")
    assert closings.closings == ["Waiting for your approval."]


@pytest.mark.asyncio
async def test_a_response_rounds_answer_has_its_closing_though_the_plan_changed(fixture):
    """Once a closing's findings took a response round, the run is ending: the round's
    answer has its closing, even at a plan state that has had no continuation."""
    f = fixture
    ref, revision = await active(f)
    toolbox = tools(f)

    class Findings(Closings):
        async def closing(self, run: RunView, answer: str | None) -> Closing:
            self.closings.append(answer)
            if len(self.closings) == 1:
                notice = Notice("findings_fixture", ("f1",), "Findings: ", {"finding": "f1"})
                return Closing("respond", (notice,))
            return Closing()

    closings = Findings()
    progress = turn(
        "plan_progress",
        operation_id="fix",
        base=ref,
        step_id="step-0",
        expected_step_cursor=0,
        expected_status_revision=revision,
        status="in_progress",
        note="Answering the finding",
    )
    model = ScriptedModel(
        [ModelTurn("Status."), ModelTurn("Final."), progress, ModelTurn("Answered.")]
    )
    result = await run_task(
        model, toolbox, "Work", run_id="main-run", hooks=(*toolbox.turn_hooks(), closings)
    )
    assert closings.closings == ["Final.", "Answered."]
    assert (result.stopped, result.final_text) == ("answered", "Answered.")
    assert result.summaries["plans"]["went_on"] == 1


def with_root(f) -> None:
    """The session's root work, as the run service records it: a plan state's
    continuation is recorded against it, where the automatic continuation looks."""
    WorkRecords(f.projects.control).admit("p", "s", RUN)


def state_continued(f) -> bool:
    summary = f.core.summary(f.user)
    assert summary is not None
    with f.projects.control.read() as tx:
        return continued(tx.sql, "p", "s", summary)


class Ceiling(TurnHook):
    """A turn allowance lowered to the calls made once the first returned."""

    name = "ceiling_fixture"

    async def turn_limit(self, run: RunView) -> int | None:
        return run.turns if run.turns else None


class LastCall(TurnHook):
    """A limit that makes the next model call the run's last, from its ``at``-th ask."""

    name = "last_call_fixture"

    def __init__(self, at: int = 1) -> None:
        self.asked, self.at = 0, at

    async def last_call(self, run: RunView) -> str | None:
        self.asked += 1
        return "run_budget" if self.asked >= self.at else None


@pytest.mark.asyncio
@pytest.mark.parametrize("limit", [Ceiling, LastCall])
async def test_an_answer_the_run_cannot_go_on_from_spends_no_continuation(fixture, limit):
    """Review of de930eca: the state's continuation was claimed before the loop knew the
    run could take its next turn; the run then ended on the answer, which no hold kept
    back, and the automatic continuation found the state spent. Now nothing is claimed,
    the answer has its closing, held as any closing answer, and the state keeps its
    continuation for a new root."""
    f = fixture
    with_root(f)
    await active(f)
    toolbox = tools(f)
    closings = Closings()
    model = ScriptedModel([ModelTurn("Status."), ModelTurn("unused")])
    result = await run_task(
        model,
        toolbox,
        "Work",
        run_id="main-run",
        hooks=(*toolbox.turn_hooks(), limit(), closings),
    )
    assert (result.stopped, result.final_text) == ("answered", "Status.")
    assert closings.closings == ["Status."]
    assert closings.withheld == [True]
    assert "plans" not in result.summaries
    assert not state_continued(f)


@pytest.mark.asyncio
async def test_two_runs_at_one_plan_state_go_on_once(fixture):
    """The bound is the session's, kept in its records, not the run's."""
    f = fixture
    with_root(f)
    await active(f)
    first, _, _ = await _run_bare(f, "main-run")
    assert first.summaries["plans"]["went_on"] == 1
    assert state_continued(f)
    second, model, closings = await _run_bare(f, "main-run-2")
    assert closings.closings == ["Status."]
    assert len(model.requests) == 1
    assert "plans" not in second.summaries


async def _run_bare(f, run_id):
    adapter = PlanTools(f.core, f.projects, f.context, run_id=run_id, alias="main")
    toolbox = Toolbox(worker(), f.context, plans=adapter)
    closings = Closings()
    model = ScriptedModel([ModelTurn("Status."), ModelTurn("Ended.")])
    result = await run_task(
        model, toolbox, "Work", run_id=run_id, hooks=(*toolbox.turn_hooks(), closings)
    )
    return result, model, closings


class Fails(ScriptedModel):
    """The model call after the first answer fails, or is the user's cancel."""

    def __init__(self, turns, error: BaseException) -> None:
        super().__init__(turns)
        self.error = error

    async def complete(self, messages, tools):
        if self.requests:
            self.requests.append(list(messages))
            raise self.error
        return await super().complete(messages, tools)


class Halts(TurnHook):
    name = "halts_fixture"

    async def halt(self, run: RunView) -> str | None:
        return "budget" if run.turns else None


class Floods(TurnHook):
    """A notice past the run's room before its second call."""

    name = "floods_fixture"

    async def notices(self, point, run: RunView) -> Sequence[Notice]:
        if point == "turn" and run.turns:
            return (Notice(self.name, (), "Flood: ", "x" * 300_000),)
        return ()


@pytest.mark.asyncio
@pytest.mark.parametrize("ending", ["error", "cancelled", "halt", "out_of_room", "report"])
async def test_an_answer_the_run_went_on_from_stays_its_answer_whatever_ends_it(fixture, ending):
    """Review of de930eca: a run that went on past its answer and failed on its next
    call had no answer at all, though the user saw it."""
    f = fixture
    await active(f)
    toolbox = tools(f)
    stop = asyncio.Event()
    replies = [ModelTurn("Status: step-0 is next."), ModelTurn("unused")]
    extra: list[TurnHook] = []
    model: ScriptedModel = ScriptedModel(replies)
    if ending == "error":
        model = Fails(replies, ModelError("test-secret-failure-body"))
    elif ending == "cancelled":
        stop.set()
        model = Fails(replies, asyncio.CancelledError())
    elif ending == "halt":
        extra.append(Halts())
    elif ending == "out_of_room":
        extra.append(Floods())
    else:
        extra.append(LastCall(at=2))
    result = await run_task(
        model,
        toolbox,
        "Work",
        run_id="main-run",
        stop=stop,
        max_input_chars=200_000,
        hooks=(*toolbox.turn_hooks(), *extra),
    )
    assert result.summaries["plans"]["went_on"] == 1
    assert result.answer == "Status: step-0 is next."
    assert (
        result.stopped
        == {
            "error": "error",
            "cancelled": "cancelled",
            "halt": "budget",
            "out_of_room": "answered",
            "report": "run_budget",
        }[ending]
    )


def tracked(*tracks):
    """A plan whose step-<t> is on track <t>, and step-own on none."""
    value = body(count=0)
    value["tracks"] = tuple({"track_id": t, "title": f"Track {t}"} for t in tracks)
    value["phases"] = (
        {
            "phase_id": "analysis",
            "title": "Analysis",
            "steps": tuple(
                {
                    "step_id": f"step-{t}",
                    "title": f"Step {t}",
                    "description": "Inspect and check",
                    "evidence_standard": "Retain evidence",
                    **({"track_id": t} if t != "own" else {}),
                }
                for t in tracks
            ),
        },
    )
    return value


def delegated(f, track: str, n: int, *, returned: bool = False) -> None:
    """A delegated run of the session on ``track``: running, or returned and awaiting
    its parent's review."""
    ended = ("returned", "answered", "2026-11-27T12:05:00+00:00", "awaiting")
    with f.projects.control.write() as tx:
        tx.sql.execute(
            "INSERT INTO delegations (delegation_id, project_id, session_id, parent, run, "
            "kind, brief, started_at, ended, stopped, ended_at, review) "
            "VALUES (?, 'p', 's', 'main-run', ?, 'task', ?, ?, ?, ?, ?, ?)",
            (
                "dlg-" + f"{n:024x}",
                f"child-{n}",
                json.dumps({"track": {"track_id": track}}),
                "2026-11-27T12:00:00+00:00",
                *(ended if returned else (None, None, None, None)),
            ),
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("returned", [False, True])
async def test_steps_with_children_leave_the_closing_to_wait_for_them(fixture, returned):
    """Review of de930eca: an answer while children ran (\"waiting for the subagent\")
    was told to go on with the step a child was working on, and spent the state's
    continuation; the closing is where a parent waits for its children."""
    f = fixture
    with_root(f)
    receipt = f.core.publish(
        f.adapter.authority, "plan", PlanBody.model_validate(tracked("a")), "Plan"
    )
    summary = f.core.summary(f.user)
    f.core.set_status(
        f.adapter.authority, "go", receipt.ref, summary.status_revision, 0, "active", "Begin"
    )
    delegated(f, "a", 1, returned=returned)
    result, _, closings = await _run_bare(f, "main-run")
    assert closings.closings == ["Status."]
    assert "plans" not in result.summaries
    assert not state_continued(f)


@pytest.mark.asyncio
async def test_the_notice_names_only_the_steps_left_to_the_run(fixture):
    f = fixture
    receipt = f.core.publish(
        f.adapter.authority, "plan", PlanBody.model_validate(tracked("a", "own")), "Plan"
    )
    summary = f.core.summary(f.user)
    f.core.set_status(
        f.adapter.authority, "go", receipt.ref, summary.status_revision, 0, "active", "Begin"
    )
    delegated(f, "a", 1)
    result, model, closings = await _run_bare(f, "main-run")
    assert closings.closings == ["Ended."]
    notice = plans_notice(model.requests[1][-1])
    assert (notice["next_steps"], notice["with_children"]) == (["step-own"], ["step-a"])
    assert result.summaries["plans"]["went_on"] == 1
