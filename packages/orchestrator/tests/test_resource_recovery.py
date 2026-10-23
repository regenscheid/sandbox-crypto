# SPDX-License-Identifier: MIT
"""Recover committed resource decisions without renewing an allowance twice."""

from typing import cast

import pytest

from sandbox_science.orchestrator.context.history import Archive
from sandbox_science.orchestrator.context.history.operations import ToolJournal
from sandbox_science.orchestrator.continuity.auto_recovery import AutoRecovery
from sandbox_science.orchestrator.continuity.reconcile import Reconciler
from sandbox_science.orchestrator.continuity.recovery_tools import resource_decision_receipt
from sandbox_science.orchestrator.continuity.resources import (
    ResourceApprovalRequest,
    ResourceDecisionRequest,
)
from sandbox_science.orchestrator.control import ControlError
from sandbox_science.orchestrator.model import ToolCall
from sandbox_science.orchestrator.worker_client import WorkerClient

from .test_recovery_effects import PassiveWorker
from .test_resource_requests import setup
from .test_tool_journal import CONTEXT, RUN
from .test_tool_journal import projects as projects
from .test_work_ownership import CHILD


@pytest.mark.parametrize("receipt", ["matching", "missing", "changed_arguments"])
async def test_original_resource_receipt_restores_response_without_repeating_decision(
    projects, receipt
):
    works, parent, child, store = setup(projects)
    warning = store.observe(child, "turns", used=4, total=5, revision=0)
    assert warning is not None
    request = ResourceDecisionRequest(
        request_id=warning.request_id, action="extend_turns", reason="Verified useful progress"
    )
    intent = request
    if receipt == "changed_arguments":
        intent = request.model_copy(update={"action": "pause"})
    journal = ToolJournal(projects.control)
    journal.start(
        CONTEXT,
        RUN,
        "original-resource",
        ToolCall("resource-call", "resource_decide", intent.model_dump_json()),
    )
    decision = None
    if receipt != "missing":
        decision = store.decide(parent, request, operation="original-resource")
    works.interrupt("p", "s", RUN)
    works.interrupt("p", "s", CHILD)
    projects.control.close()
    worker = PassiveWorker(None)  # No execution or decision API is available.
    reconciler = Reconciler(projects.control, cast(WorkerClient, worker))
    recovery = AutoRecovery(projects.control, interrupted=True)
    assert AutoRecovery(projects.control).queue("p", "s", RUN) is None
    if receipt == "matching":
        assert recovery.queue("p", "s", RUN) is not None
        await reconciler.work(works.get("p", "s", RUN))
        await reconciler.work(works.get("p", "s", RUN))
        (message,) = Archive(projects.control).messages("p", "s", run=RUN)
        assert decision is not None
        assert message.entry.call_id == "resource-call"
        assert message.entry.content == decision.model_dump_json()
        assert journal.record("p", message.message_id)["resource_decision"] == decision.model_dump(
            mode="json"
        )
        assert journal.pending("p", "s", RUN) == []
        assert store.limits(child, turns=5, tokens=20_000_000)[:2] == (305, 305)
        with projects.control.read() as tx:
            assert tx.sql.execute(
                "SELECT count(*) FROM research_observations WHERE kind='resource_decision'"
            ).fetchone() == (1,)
    else:
        assert recovery.queue("p", "s", RUN) is None
        with pytest.raises(ControlError, match="unknown_resource_decision"):
            await reconciler.work(works.get("p", "s", RUN))
        assert len(journal.pending("p", "s", RUN)) == 1
    assert worker.lookups == []


@pytest.mark.parametrize("mismatch", ["project", "session", "run", "operation", "arguments"])
def test_receipt_requires_exact_original_scope_and_arguments(projects, mismatch):
    _, parent, child, store = setup(projects)
    warning = store.observe(child, "turns", used=4, total=5, revision=0)
    assert warning is not None
    request = ResourceDecisionRequest(
        request_id=warning.request_id, action="extend_turns", reason="Verified progress"
    )
    store.decide(parent, request, operation="resource-operation")
    values = {
        "project": "p",
        "session": "s",
        "run": RUN,
        "operation": "resource-operation",
        "arguments": request.model_dump_json(),
    }
    with projects.control.read() as tx:
        assert resource_decision_receipt(tx.sql, **values) is not None
        values[mismatch] = "another-scope-or-invalid-json"
        assert resource_decision_receipt(tx.sql, **values) is None


def test_user_approval_cannot_replace_an_agent_tool_receipt(projects):
    works, _, child, store = setup(projects)
    warning = store.observe(child, "turns", used=4, total=5, revision=0)
    assert warning is not None
    request = ResourceApprovalRequest(
        request_id=warning.request_id,
        action="extend_turns",
        reason="USER approved continuation",
        current=child.run,
        epoch=child.epoch,
        total=warning.total,
    )
    works.interrupt("p", "s", RUN)
    works.interrupt("p", "s", CHILD)
    decision = store.approve("p", "s", request, operator="USER")
    with projects.control.read() as tx:
        assert (
            resource_decision_receipt(
                tx.sql,
                "p",
                "s",
                decision.parent_run,
                decision.operation,
                decision.request.model_dump_json(),
            )
            is None
        )
