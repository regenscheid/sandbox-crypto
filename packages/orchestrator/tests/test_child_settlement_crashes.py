# SPDX-License-Identifier: MIT
"""Abrupt process death at durable save and child-handback boundaries."""

from __future__ import annotations

import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import TypeAdapter

from sandbox_science.orchestrator.agent_messages import AgentMessageStore
from sandbox_science.orchestrator.continuity.child_settlement import ChildSettlement
from sandbox_science.orchestrator.continuity.plans import Plans
from sandbox_science.orchestrator.continuity.records import WorkRecords
from sandbox_science.orchestrator.delegation.hook import ChildEnd
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
from contextlib import nullcontext

from sandbox_science.orchestrator import harness
from sandbox_science.orchestrator.continuity.recovery import Recovery
from sandbox_science.orchestrator.delegation.hook import Child, ChildEnd
from sandbox_science.orchestrator.harness import Admitted, Finished, Harness, RunRequest, Stores
from sandbox_science.orchestrator.jobstate import JobState
from sandbox_science.orchestrator.loop import Transcript
from sandbox_science.orchestrator.model import ModelTurn, ScriptedModel
from sandbox_science.orchestrator.harness import Role
from sandbox_science.orchestrator.runstate import RunState
from pydantic import TypeAdapter
from sandbox_science.orchestrator.agent_messages import AgentMailbox, AgentMessageStore
from sandbox_science.orchestrator.continuity.child_settlement import ChildSettlement
from sandbox_science.orchestrator.continuity.records import WorkRecords
from sandbox_science.orchestrator.documents import DocumentStore, store_path
from sandbox_science.orchestrator.ledger import Ledger
from sandbox_science.orchestrator.projects import ProjectStore, RunRecord

root, state, boundary, run = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3], sys.argv[4]
caller = sys.argv[5]
assert Path(harness.__file__).resolve().is_relative_to(root)
projects = ProjectStore(state)

async def crash():
    if boundary == "ledger":
        documents = DocumentStore(store_path(state), producer="crash-test")
        ledger = Ledger(documents)
        settlement = ChildSettlement(projects.control, ledger)
        target, method = (ledger if caller == "save" else Ledger), "change"
    if boundary != "ledger" or caller != "save":
        record = RunRecord.model_validate_json((state / "final-record.json").read_text())
        messages = AgentMessageStore(projects.control)
        mailbox = AgentMailbox(
            messages, record.context, run=run, delegation=record.delegation,
            may_message_child=False,
        )
        save_target = {
            "record": (projects, "save_run"),
            "acknowledgement": (messages, "acknowledge"),
            "finish": (WorkRecords, "finish"),
        }
        if boundary != "ledger":
            target, method = save_target[boundary]

    original = getattr(target, method)

    def committed(*args, **kwargs):
        original(*args, **kwargs)
        os._exit(86)

    with patch.object(target, method, committed):
        if caller != "save":
            outcome = TypeAdapter(ChildEnd).validate_json(
                (state / "child-outcome.json").read_text()
            )

            class Completed(Finished):
                def fields(self):
                    return record.model_dump(mode="json")

            finished = Completed(
                record.run_id, record.context, record.task, record.started_at,
                Transcript(messages=[], stopped="answered", final_text=record.final_text),
                hooks=(mailbox,), parent=record.parent, delegation=record.delegation,
            )

            class PreparedHarness(Harness):
                async def start(self, admitted, sink, **kwargs):
                    return finished

                def child_outcome(self, completed, status):
                    return outcome

            class Worker:
                async def close_session(self, context, *, run=None):
                    pass

            documents = DocumentStore(store_path(state), producer="crash-test")
            prepared = PreparedHarness(
                Worker(), Role(ScriptedModel([ModelTurn("unused")]), "scripted"),
                stores=Stores(projects, documents, JobState(state), RunState(projects.control)),
            )
            request = RunRequest("p", "s", "Fixture")
            parent_record = projects.run("p", "s", record.parent)
            parent = Admitted(
                request, parent_record.run_id, record.context, (), (), True,
                parent_record.started_at,
            )
            if caller == "fresh":
                await prepared.run_child(
                    parent, None,
                    Child(
                        record.delegation, record.run_id, record.started_at, "Fixture", 1, 1, None
                    ),
                    asyncio.Event(),
                )
            else:
                recovery = Recovery(prepared, "p", "s", "fixture")
                recovery.started, recovery.epoch = True, 0
                recovery.run_name = record.parent
                recovery.sink = type("Sink", (), {"error": print})()

                async def ready():
                    return (run,)

                async def admitted(work):
                    return Admitted(
                        RunRequest(
                            "p", "s", "Fixture", parent=record.parent, delegation=record.delegation
                        ),
                        record.run_id, record.context, (), (), True, record.started_at,
                    )

                # Isolate the public branch's finalization protocol; full Resume
                # preparation and ownership are exercised in test_tree_recovery.
                recovery.plans.authority = lambda *args: nullcontext()
                recovery.next, recovery.prepare = ready, admitted
                assert await recovery.branch(run)
        elif boundary == "ledger":
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


def crash_after_commit(state: Path, boundary: str, caller: str = "save") -> None:
    # The shared development interpreter can have another editable checkout. Pin
    # every workspace source here, and verify the imported harness in the child.
    environment = os.environ.copy()
    environment["PYTHONPATH"] = os.pathsep.join(
        str(path) for path in sorted((ROOT / "packages").glob("*/src"))
    )
    result = subprocess.run(
        [sys.executable, "-c", CRASH, str(ROOT), str(state), boundary, CHILD, caller],
        env=environment,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == CRASH_STATUS, result.stdout + result.stderr


@pytest.mark.parametrize(
    ("caller", "boundary"),
    [
        (caller, boundary)
        for caller in ("save", "fresh", "resumed")
        for boundary in ("record", "acknowledgement", "finish", "ledger")
        if (caller, boundary) != ("save", "ledger")
    ],
)
async def test_hard_death_during_final_save_retains_truthful_delivery_and_outcome(
    tmp_path: Path, boundary: str, caller: str
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
        (tmp_path / "child-outcome.json").write_bytes(TypeAdapter(ChildEnd).dump_json(outcome))
    finally:
        documents.close()
        state.projects.close()

    crash_after_commit(tmp_path, boundary, caller)

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
            "completed" if boundary in ("finish", "ledger") else "running"
        )
        records = DelegationRecords(projects.control)
        assert records.get("p", "s", child.delegation_id).ended == (
            "returned" if boundary == "ledger" else None
        )
        settlement = ChildSettlement(projects.control, Ledger(documents))
        assert settlement.page("p", "s") == (CHILD,)

        await settlement.drain("p", "s")
        kept = records.get("p", "s", child.delegation_id)
        assert kept.report == outcome.final_text
        if boundary in ("finish", "ledger"):
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
