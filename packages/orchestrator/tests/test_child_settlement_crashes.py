# SPDX-License-Identifier: MIT
"""Abrupt process death at durable save and child-handback boundaries."""

from __future__ import annotations

import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

from sandbox_science.orchestrator.agent_messages import AgentMessageStore
from sandbox_science.orchestrator.continuity.child_settlement import ChildSettlement
from sandbox_science.orchestrator.continuity.plans import Plans
from sandbox_science.orchestrator.continuity.records import WorkRecords
from sandbox_science.orchestrator.delegation.records import DelegationRecords
from sandbox_science.orchestrator.documents import DocumentStore, store_path
from sandbox_science.orchestrator.ledger import Ledger
from sandbox_science.orchestrator.projects import ProjectStore
from sandbox_science.orchestrator.runstate import RunState

from .test_child_settlement import tree
from .test_tool_journal import RUN
from .test_work_ownership import CHILD, CONTINUATION, record

pytestmark = pytest.mark.slow

CRASH_STATUS = 86
ROOT = Path(__file__).resolve().parents[3]

# Exit without stack unwinding: an exception or clean close would miss the process
# death window, including the committed WAL that a new process must recover.
CRASH = """
import asyncio
import os
import sys
from pathlib import Path
from unittest.mock import patch

from sandbox_science.orchestrator import harness
from sandbox_science.orchestrator.agent_messages import AgentMailbox, AgentMessageStore
from sandbox_science.orchestrator.continuity.child_settlement import ChildSettlement
from sandbox_science.orchestrator.continuity.records import WorkRecords
from sandbox_science.orchestrator.documents import DocumentStore, store_path
from sandbox_science.orchestrator.ledger import Ledger
from sandbox_science.orchestrator.projects import ProjectStore, RunRecord

root, state, boundary, run = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3], sys.argv[4]
assert Path(harness.__file__).resolve().is_relative_to(root)
projects = ProjectStore(state)

async def crash():
    if boundary == "ledger":
        documents = DocumentStore(store_path(state), producer="crash-test")
        ledger = Ledger(documents)
        settlement = ChildSettlement(projects.control, ledger)
        target, method = ledger, "change"
    else:
        record = RunRecord.model_validate_json((state / "final-record.json").read_text())
        messages = AgentMessageStore(projects.control)
        mailbox = AgentMailbox(
            messages, record.context, run=run, delegation=record.delegation,
            may_message_child=False,
        )
        target, method = {
            "record": (projects, "save_run"),
            "acknowledgement": (messages, "acknowledge"),
            "finish": (WorkRecords, "finish"),
        }[boundary]

    original = getattr(target, method)

    def committed(*args, **kwargs):
        original(*args, **kwargs)
        os._exit(86)

    with patch.object(target, method, committed):
        if boundary == "ledger":
            await settlement.settle("p", "s", run)
        else:
            errors = []
            await harness.save(
                record.model_dump(mode="json"), 0, (mailbox,), projects=projects,
                error=errors.append,
            )
            raise AssertionError(f"save missed the crash boundary: {errors}")
    raise AssertionError("settlement missed the crash boundary")

asyncio.run(crash())
"""


def crash_after_commit(state: Path, boundary: str) -> None:
    # The shared development interpreter can have another editable checkout. Pin
    # every workspace source here, and verify the imported harness in the child.
    environment = os.environ.copy()
    environment["PYTHONPATH"] = os.pathsep.join(
        str(path) for path in sorted((ROOT / "packages").glob("*/src"))
    )
    result = subprocess.run(
        [sys.executable, "-c", CRASH, str(ROOT), str(state), boundary, CHILD],
        env=environment,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == CRASH_STATUS, result.stdout + result.stderr


@pytest.mark.parametrize("boundary", ["record", "acknowledgement", "finish"])
async def test_hard_death_during_final_save_retains_truthful_delivery_and_outcome(
    tmp_path: Path, boundary: str
) -> None:
    state, documents, _, _, _, child, outcome = tree(tmp_path, saved=False, finished=False)
    try:
        RunState(state.projects.control).acquire("p", "s", RUN, "fixture")
        sent = AgentMessageStore(state.projects.control).send_child(
            "p", "s", RUN, None, child.delegation_id, "Check the corrected target.", "correction"
        )
        final = record(CHILD).model_copy(
            update={
                "parent": RUN,
                "delegation": child.delegation_id,
                "role": "delegated",
                "final_text": outcome.final_text,
                "delivered": {"agent_messages": [sent.message_id]},
            }
        )
        (tmp_path / "final-record.json").write_text(final.to_json())
    finally:
        documents.close()
        state.projects.close()

    crash_after_commit(tmp_path, boundary)

    projects = ProjectStore(tmp_path)
    documents = DocumentStore(store_path(tmp_path), producer="recovery-test")
    try:
        # The saved record is durable even when no ACK or finish receipt committed.
        assert projects.run("p", "s", CHILD) == final
        messages = AgentMessageStore(projects.control)
        delivered = messages.status("p", "s", RUN, sent.message_id)
        assert delivered.delivered_run == (None if boundary == "record" else CHILD)
        assert messages.pending("p", "s", child.delegation_id) == (
            [sent] if boundary == "record" else []
        )
        works = WorkRecords(projects.control)
        assert works.get("p", "s", CHILD).state == (
            "completed" if boundary == "finish" else "running"
        )
        records = DelegationRecords(projects.control)
        assert records.get("p", "s", child.delegation_id).ended is None
        settlement = ChildSettlement(projects.control, Ledger(documents))
        assert settlement.page("p", "s") == (CHILD,)

        await settlement.drain("p", "s")
        kept = records.get("p", "s", child.delegation_id)
        assert kept.report == outcome.final_text
        if boundary == "finish":
            assert kept.ended == "returned"
            assert kept.result == outcome.result
            assert kept.errors == outcome.errors
            assert kept.stopped == outcome.stopped
        else:
            assert kept.ended == "failed"
            assert kept.stopped == "persistence_error"
            assert kept.result is None
            assert any("persistence not confirmed" in error for error in kept.errors)
        first = settlement.ledger.work("p", "C-1").records
        assert len(first) == 1
        await settlement.drain("p", "s")
        assert settlement.page("p", "s") == ()
        assert records.get("p", "s", child.delegation_id) == kept
        assert settlement.ledger.work("p", "C-1").records == first
        assert messages.status("p", "s", RUN, sent.message_id) == delivered
    finally:
        documents.close()
        projects.close()


async def test_hard_death_after_ledger_commit_keeps_parent_blocked_until_receipt(
    tmp_path: Path,
) -> None:
    state, documents, works, _, settlement, child, outcome = tree(tmp_path)
    try:
        works.interrupt("p", "s", RUN)
        request = works.request("p", "s")
        assert request is not None
        RunState(state.projects.control).acquire("p", "s", CONTINUATION, "fixture")
        root, _ = works.claim("p", "s", request, lease_run=CONTINUATION)
        plans = Plans(state.projects.control, "p", "s", request)
        plans.prepare(root, run=CONTINUATION, started=datetime.now(UTC))
        assert plans.ready() == ()
        assert settlement.page("p", "s") == (CHILD,)
    finally:
        documents.close()
        state.projects.close()

    crash_after_commit(tmp_path, "ledger")

    projects = ProjectStore(tmp_path)
    documents = DocumentStore(store_path(tmp_path), producer="recovery-test")
    try:
        records = DelegationRecords(projects.control)
        kept = records.get("p", "s", child.delegation_id)
        assert kept.ended == "returned"
        assert kept.result == outcome.result
        assert kept.report == outcome.final_text
        settlement = ChildSettlement(projects.control, Ledger(documents))
        plans = Plans(projects.control, "p", "s", request)
        # Both handback and ledger committed, but the missing receipt still vetoes
        # the parent. Recovery must replay the same operation, not add another one.
        assert settlement.page("p", "s") == (CHILD,)
        assert plans.ready() == ()
        first = settlement.ledger.work("p", "C-1").records
        assert len(first) == 1

        await settlement.drain("p", "s")
        assert plans.ready() == (RUN,)
        await settlement.drain("p", "s")
        assert plans.ready() == (RUN,)
        assert settlement.page("p", "s") == ()
        assert records.get("p", "s", child.delegation_id) == kept
        assert settlement.ledger.work("p", "C-1").records == first
        assert await settlement.settle("p", "s", CHILD) == outcome
    finally:
        documents.close()
        projects.close()
