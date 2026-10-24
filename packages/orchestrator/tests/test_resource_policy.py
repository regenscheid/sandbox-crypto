# SPDX-License-Identifier: MIT
"""Active time checks in (D102); storage stays recoverable and never widens hard capacity."""

from __future__ import annotations

import asyncio

import pytest

from sandbox_science.contracts.artifact_inventory import ArtifactPublicationSource
from sandbox_science.orchestrator.artifacts import ArtifactInventory, ArtifactLimits
from sandbox_science.orchestrator.context.agent import AgentContext
from sandbox_science.orchestrator.context.history import Archive, Entry
from sandbox_science.orchestrator.continuity.auto_recovery import AutoRecovery
from sandbox_science.orchestrator.continuity.resource_policy import continuation_allowed
from sandbox_science.orchestrator.continuity.resources import ResourceApprovalRequest
from sandbox_science.orchestrator.control import ControlError
from sandbox_science.orchestrator.delegation.records import DelegationRecords
from sandbox_science.orchestrator.documents import DocumentStore
from sandbox_science.orchestrator.hooks import Closing, Notice, TurnHook
from sandbox_science.orchestrator.loop import Transcript, run_task
from sandbox_science.orchestrator.model import ModelTurn, ScriptedModel
from sandbox_science.orchestrator.projects import run_id_of
from sandbox_science.orchestrator.resource_hooks import Resources
from sandbox_science.testing import FakeClock, eventually

from .test_artifact_inventory import record as publish
from .test_resource_requests import decide, setup
from .test_tool_journal import CONTEXT, RUN, toolbox
from .test_tool_journal import projects as projects
from .test_work_ownership import CHILD, record


def artifact(projects, run=RUN, data=b"new proof", operation="proof"):
    documents = DocumentStore(projects.control.path)
    run_id = run_id_of(run)
    assert run_id is not None
    source = ArtifactPublicationSource(
        context=CONTEXT,
        run_id=run_id,
        tool_call=operation,
        alias="fixture",
        model="fixture",
        worker_endpoint_sha256="a" * 64,
    )
    try:
        return publish(ArtifactInventory(documents), operation, data, source=source, path=operation)
    finally:
        documents.close()


def sample(store, owner, *, seconds, resource="elapsed_seconds"):
    """The active-time cap, if on, or the retained-artifact allowance, if on."""
    point = store.safe_point(owner, seconds=seconds, turns=300, tokens=None)
    if resource == "retained_bytes":
        return point.storage
    return tuple(
        (found, point.used["elapsed_seconds"], total, revision)
        for found, total, revision in point.caps
        if found == resource
    )


async def test_active_time_checks_in_without_a_call_then_the_parent_extends_it(projects):
    _, parent, child, store = setup(projects)
    DelegationRecords(projects.control).configure("p", research_seconds=10, time_ceiling_seconds=30)
    clock = FakeClock()
    hook = Resources(store, child, turns=300, tokens=20_000_000, delegated=True, clock=clock)
    clock.advance(8)
    assert await hook.halt(Transcript(context=AgentContext(messages=[]))) is None
    assert store.pending(parent) == []  # no warning below the cap
    clock.advance(2)
    waiting = asyncio.create_task(hook.halt(Transcript(context=AgentContext(messages=[]))))
    await eventually(lambda: bool(store.pending(parent)))
    (request,) = store.pending(parent)
    assert (request.resource, request.used, request.total) == ("elapsed_seconds", 10, 10)
    assert not waiting.done()
    assert decide(store, parent, request, action="extend_time").total == 30
    await eventually(lambda: clock.sleepers > 0)
    clock.advance(1)
    assert await asyncio.wait_for(waiting, 2) is None
    assert store.pending(parent) == []
    third = store.observe(child, "elapsed_seconds", used=30, total=30, revision=30, force=True)
    with pytest.raises(ControlError, match="ceiling"):
        decide(store, parent, third, action="extend_time", operation="next")


def test_time_samples_monotone_and_initial_allowance_pinned(projects):
    _, _, child, store = setup(projects)
    settings = DelegationRecords(projects.control)
    settings.configure("p", research_seconds=100)
    assert sample(store, child, seconds=80) == (("elapsed_seconds", 80, 100, 0),)
    settings.configure("p", research_seconds=1000)
    assert sample(store, child, seconds=40) == (("elapsed_seconds", 80, 100, 0),)
    settings.configure("p", research_seconds=None)
    assert sample(store, child, seconds=90) == ()


def test_retained_bytes_scoped_to_work_and_prior_attempts_not_all_session(projects):
    _, parent, child, store = setup(projects)
    DelegationRecords(projects.control).configure("p", storage_bytes=10, storage_ceiling_bytes=20)
    artifact(projects, CHILD, b"12345678")
    artifact(projects, RUN, b"unrelated root content", "other")
    assert sample(store, child, seconds=0, resource="retained_bytes") == (
        ("retained_bytes", 8, 10, 0),
    )
    warning = store.observe(child, "retained_bytes", used=8, total=10, revision=0)
    assert warning is not None
    assert decide(store, parent, warning, action="extend_storage").total == 20
    assert sample(store, child, seconds=0, resource="retained_bytes")[0][2:] == (20, 20)


def test_root_renewal_opt_in_requires_checkpoint_and_new_evidence(projects):
    _, parent, _, store = setup(projects)
    warning = store.observe(parent, "turns", used=4, total=5, revision=0)
    assert warning is not None
    with pytest.raises(ControlError, match="not enabled"):
        decide(store, parent, warning)
    DelegationRecords(projects.control).configure("p", root_resource_renewal=True)
    with pytest.raises(ControlError, match="checkpoint"):
        decide(store, parent, warning)
    Archive(projects.control).append(
        "p", "s", RUN, Entry("assistant", "assistant", "A proof is saved")
    )
    exhausted = store.observe(parent, "turns", used=5, total=5, revision=0, force=True)
    assert exhausted is not None
    artifact(projects)
    result = decide(store, parent, exhausted)
    assert result.total == 305
    assert result.progress == 1
    next_warning = store.observe(parent, "turns", used=244, total=305, revision=305)
    assert next_warning is not None
    with pytest.raises(ControlError, match="new research evidence"):
        decide(store, parent, next_warning, operation="second")
    artifact(projects, operation="new-proof")
    assert decide(store, parent, next_warning, operation="second").total == 605


def test_user_soft_approval_requires_idle_exact_attempt_never_tokens_or_stop(projects):
    works, _, child, store = setup(projects)
    warning = store.observe(child, "turns", used=5, total=5, revision=0)
    assert warning is not None
    request = ResourceApprovalRequest(
        request_id=warning.request_id,
        action="extend_turns",
        reason="Continue the saved proof",
        current=CHILD,
        epoch=1,
        total=5,
    )
    with pytest.raises(ControlError, match="active research tree"):
        store.approve("p", "s", request, operator="user")
    works.finish(record(CHILD, stopped="turn_limit"))
    works.finish(record(RUN))
    with pytest.raises(ControlError, match="allowance changed"):
        store.approve("p", "s", request.model_copy(update={"total": 6}), operator="user")
    decision = store.approve("p", "s", request, operator="user")
    assert decision.operator == "user"
    assert decision.total == 305
    assert store.approve("p", "s", request, operator="user") == decision
    assert works.queued() == []


@pytest.mark.parametrize("root", [False, True])
@pytest.mark.parametrize("resource", ["elapsed_seconds", "retained_bytes"])
def test_approved_soft_limit_automatically_resumes_original_tree(projects, root, resource):
    works, parent, child, store = setup(projects)
    owner = parent if root else child
    timed = resource == "elapsed_seconds"
    reason = "resource_time" if timed else "resource_storage"
    action = "extend_time" if timed else "extend_storage"
    settings = (
        {"research_seconds": 10, "time_ceiling_seconds": 30}
        if timed
        else {"storage_bytes": 10, "storage_ceiling_bytes": 30}
    )
    DelegationRecords(projects.control).configure("p", **settings)
    if not timed:
        artifact(projects, owner.run, b"0123456789")
    assert sample(store, owner, seconds=10, resource=resource)[0][1:3] == (10, 10)
    warning = store.observe(owner, resource, used=10, total=10, revision=0)
    assert warning is not None
    works.finish(record(CHILD, stopped="answered" if root else reason))
    works.finish(record(RUN, stopped=reason if root else "answered"))
    recovery = AutoRecovery(projects.control)
    assert recovery.candidates() == (("p", "s", RUN),)
    assert recovery.queue("p", "s", RUN) is None
    decision = store.approve(
        "p",
        "s",
        ResourceApprovalRequest(
            request_id=warning.request_id,
            action=action,
            reason="Continue from the preserved checkpoint",
            current=owner.run,
            epoch=owner.epoch,
            total=10,
        ),
        operator="user",
    )
    assert decision.total == 30
    queued = recovery.queue("p", "s", RUN)
    assert queued is not None
    assert works.queued() == [("p", "s", queued, RUN)]
    assert AutoRecovery(projects.control).queue("p", "s", RUN) is None


async def test_hard_retention_pressure_is_visible_not_renewable(projects):
    _, parent, child, store = setup(projects)
    artifact(projects, CHILD, b"12345678")
    documents = DocumentStore(projects.control.path)
    inventory = ArtifactInventory(documents, limits=ArtifactLimits(project_bytes=8))
    try:
        hook = Resources(store, child, turns=300, tokens=None, delegated=True, inventory=inventory)
        assert (
            await hook.halt(Transcript(context=AgentContext(messages=[]))) == "resource_retention"
        )
        request = store.pending(parent)[0]
        assert request.scope == "project"
        assert request.resource == "retention_bytes"
        assert request.renewable is False
        with pytest.raises(ControlError, match="not renewable"):
            decide(store, parent, request, action="extend_storage")
        with projects.control.read() as tx:
            assert not continuation_allowed(tx.sql, child, "resource_retention")
    finally:
        documents.close()


async def test_a_root_that_answers_within_its_turns_is_not_checked_in(projects):
    """The root's turn limit is a check-in the user answers (``test_root_turn_check_in.py``),
    and an answer that stands at the limit is neither a check-in nor a stop."""
    _, parent, _, store = setup(projects)

    class Continue(TurnHook):
        async def closing(self, run, answer):
            return Closing("continue", (Notice("fixture", (), "another step", {}),))

    async def unused(request):
        raise AssertionError("no tool call")

    hook = Resources(store, parent, turns=5, tokens=None, delegated=False)
    result = await run_task(
        ScriptedModel([ModelTurn(f"Progress {n}") for n in range(6)]),
        toolbox(unused),
        task="Research",
        run_id="root",
        max_turns=5,
        hooks=(hook, Continue()),
    )
    assert (result.stopped, result.turns) == ("answered", 5)  # its last answer stands
    assert store.page("p", "s").requests == ()


def test_old_session_evidence_does_not_enable_a_new_root_renewal(projects):
    _, parent, _, store = setup(projects)
    artifact(projects, operation="old-result")
    DelegationRecords(projects.control).configure("p", root_resource_renewal=True)
    sample(store, parent, seconds=0)
    Archive(projects.control).append("p", "s", RUN, Entry("assistant", "assistant", "Checkpoint"))
    warning = store.observe(parent, "turns", used=4, total=5, revision=0)
    with pytest.raises(ControlError, match="new research evidence"):
        decide(store, parent, warning)


def test_stop_is_not_overridden_by_a_parent_resource_extension(projects):
    works, parent, child, store = setup(projects)
    warning = store.observe(child, "turns", used=4, total=5, revision=0)
    works.stop("p", "s", CHILD)
    with pytest.raises(ControlError, match="ended or was stopped"):
        decide(store, parent, warning)
