# SPDX-License-Identifier: MIT
"""Completed child evidence survives while live task retention stays bounded."""

from __future__ import annotations

import asyncio
import json
import tracemalloc
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest

from sandbox_science.orchestrator.context.agent import AgentContext
from sandbox_science.orchestrator.context.history.export import EvidenceExport
from sandbox_science.orchestrator.delegation.hook import Child, ChildEnd
from sandbox_science.orchestrator.loop import Transcript
from sandbox_science.orchestrator.projects import RunRecord
from sandbox_science.orchestrator.runstate import RunState

from .loop_harness import PROJECT, SESSION
from .test_delegation_depth import ROOT, tree
from .test_delegation_tasks import DONE
from .test_evidence_export import decode


async def test_long_child_loop_retrieval_receipts_and_finalization(tmp_path: Path) -> None:
    async def finished(child: Child, stop: asyncio.Event) -> ChildEnd:
        return ChildEnd("answered", False, json.dumps(DONE))

    with tree(tmp_path) as found:
        hook = found.hook(finished, cap=2)
        transcript = Transcript(context=AgentContext(messages=[]))
        tracemalloc.start()
        peaks: list[int] = []
        first = ""
        async with hook.lease():
            for n in range(240):
                text, _ = await hook.call("delegate", {"task": f"Part {n}."})
                identity = str(json.loads(text)["delegation_id"])
                first = first or identity
                await hook.call("wait_delegations", {"delegation_ids": [identity]})
                # A response durably consumes the tool result before this hook runs.
                await hook.model_returned(transcript)
                assert not hook.children
                assert not hook.returned
                if n in (23, 239):
                    peaks.append(tracemalloc.get_traced_memory()[0])
            summary = cast(dict[str, Any], hook.summary())
            assert len(summary["started"]) == len(summary["received"]) == 100
            assert summary["started_history"]["count"] == 240
            assert summary["received_history"]["count"] == 240
            assert len(json.dumps(summary)) < 12_000
            assert len(hook.started) == len(hook.received) == 240
            assert peaks[-1] - peaks[0] < 2 * 1024 * 1024
            # Completed handles have gone, but durable read/cancel behavior remains: a
            # returned task that awaits review is closed as cancelled, once, while the
            # root holds the session (PD-46).
            RunState(found.records.control).acquire(PROJECT, SESSION, ROOT, "holder")
            text, _ = await hook.call("cancel_delegation", {"delegation_id": first})
            assert json.loads(text)["cancelled"] is True
            text, _ = await hook.call("cancel_delegation", {"delegation_id": first})
            assert json.loads(text)["cancelled"] is False
            now = datetime.now(UTC)
            saved = RunRecord(
                run_id="run-root",
                context=found.context,
                task="Many children",
                started_at=now,
                ended_at=now,
                stopped="answered",
                final_text="Done",
                messages=[],
                tools=[],
                summaries={"delegations": summary},
            )
            await hook.acknowledge(saved)
            assert found.records.handbacks(PROJECT, SESSION) == []
            assert found.records.get(PROJECT, SESSION, first).acknowledged == ROOT
        tracemalloc.stop()


async def test_unsettled_completed_task_is_kept_and_blocks_more_admission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def finished(child: Child, stop: asyncio.Event) -> ChildEnd:
        return ChildEnd("answered", False, json.dumps(DONE))

    with tree(tmp_path) as found:
        hook = found.hook(finished, cap=1)
        text, _ = await hook.call("delegate", {"task": "Part"})
        identity = str(json.loads(text)["delegation_id"])
        child = hook.children[identity]
        await child.task
        original = found.records.get

        def unfinished(*args: Any) -> Any:
            return original(*args).model_copy(update={"ended": None})

        monkeypatch.setattr(found.records, "get", unfinished)
        await hook._prune()
        assert identity in hook.children
        with pytest.raises(Exception, match="cleanup is not durably complete"):
            await hook.call("delegate", {"task": "Another"})


async def test_started_history_failure_rolls_back_admission_before_child_runs(
    tmp_path: Path,
) -> None:
    ran = False

    async def finished(child: Child, stop: asyncio.Event) -> ChildEnd:
        nonlocal ran
        ran = True
        return ChildEnd("answered", False, json.dumps(DONE))

    with tree(tmp_path) as found:
        hook = found.hook(finished)
        with found.records.control.write() as tx:
            tx.sql.execute(
                "CREATE TRIGGER fail_started BEFORE INSERT ON run_collections "
                "WHEN NEW.collection='delegations:started' "
                "BEGIN SELECT RAISE(ABORT,'started failure'); END"
            )
        with pytest.raises(Exception, match="started failure"):
            await hook.call("delegate", {"task": "Not admitted"})
        assert not ran
        assert not hook.children
        assert not hook.started
        assert found.records.running(PROJECT, SESSION) == []


async def test_reused_cli_run_id_keeps_full_attempt_history_and_export_scope(
    tmp_path: Path,
) -> None:
    async def finished(child: Child, stop: asyncio.Event) -> ChildEnd:
        return ChildEnd("answered", False, json.dumps(DONE))

    first = "20261015T120000000000Z-run-reused"
    second = "20261015T120001000000Z-run-reused"
    with tree(tmp_path) as found:
        hooks = [found.hook(finished, parent=parent) for parent in (first, second)]
        identities: list[str] = []
        for hook in hooks:
            async with hook.lease():
                text, _ = await hook.call("delegate", {"task": "Own attempt"})
                identity = str(json.loads(text)["delegation_id"])
                identities.append(identity)
                await hook.call("wait_delegations", {"delegation_ids": [identity]})
                await hook.model_returned(Transcript(context=AgentContext(messages=[])))
        for parent, hook, identity in zip((first, second), hooks, identities, strict=True):
            assert hook.started.scope == hook.received.scope == (PROJECT, SESSION, parent)
            assert list(hook.started) == list(hook.received) == [identity]
            _, rows = decode(
                EvidenceExport(found.records.control, PROJECT, SESSION, parent).lines()
            )
            (started,) = [row for table, row in rows if table == "run_collections"]
            assert started["run"] == parent
            assert started["collection"] == "delegations:started"
            assert json.loads(started["item"]) == identity
