# SPDX-License-Identifier: MIT
"""A check's outcome as the ledger's gate will trust it (plans-and-delegation spec,
PD-17 to PD-20; LG-17): a result names the entry and revision of its brief, stands only
for a run that ended with it, is kept only for a delegation that returned, and every
value of an outcome fits its table, so that writing it never fails."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path

import pytest

from sandbox_science.orchestrator.delegation.hook import Child, ChildEnd
from sandbox_science.orchestrator.delegation.outcome import Outcome, Target, parse, result_of
from sandbox_science.orchestrator.delegation.records import Delegation, Ending
from sandbox_science.orchestrator.loop import Transcript
from sandbox_science.orchestrator.worker_client import WorkerAPIError

from .loop_harness import PROJECT, SESSION, Harness, Reply, call
from .loop_harness import gateway as gateway
from .loop_harness import harness as harness
from .loop_harness import worker as worker
from .test_delegation import CHECK, REPORT, RESULT, children, delegation, started, wait
from .test_delegation import delegating as delegating
from .test_delegation_lifecycle import BRIEF, _delegations

TARGET = Target("ent-1", "C-1", 1)


def test_a_result_names_the_entry_and_revision_of_its_brief() -> None:
    assert parse(REPORT, TARGET).verdict == "confirmed"
    assert parse(json.dumps({**RESULT, "entry": "ent-1"}), TARGET).entry == "ent-1"
    for wrong in ({"revision": 2}, {"entry": "C-2"}):
        with pytest.raises(ValueError, match="the brief names C-1 at revision 1"):
            parse(json.dumps({**RESULT, **wrong}), TARGET)


async def test_a_result_for_another_revision_is_corrected_and_the_read_is_noted(
    delegating: Harness,
) -> None:
    root = await delegating.run(
        "root",
        [Reply(calls=[CHECK]), wait(), Reply("Confirmed.")],
        delegated=[
            Reply(calls=[call("ledger_read", entry="C-1", revision=1)]),
            Reply(json.dumps({**RESULT, "revision": 2})),
            Reply(REPORT),
        ],
    )
    (delegation_id,) = started(root)
    outcome = delegation(delegating, delegation_id)
    assert outcome.result is not None
    assert outcome.result["revision"] == 1
    assert len(outcome.errors) == 1
    assert "revision 2" in outcome.errors[0]
    assert outcome.read_entry is True


async def test_a_check_that_never_read_its_entry_says_so(delegating: Harness) -> None:
    root = await delegating.run(
        "root",
        [Reply(calls=[CHECK]), wait(), Reply("Confirmed.")],
        delegated=[Reply(calls=[call("ledger_read", entry="C-1", revision=9)]), Reply(REPORT)],
    )
    (delegation_id,) = started(root)
    assert delegation(delegating, delegation_id).read_entry is False
    (child,) = children(delegating, root)
    assert child.summaries["outcome"] == {
        "format": "sandbox-check-result-1",
        "result": {**RESULT, "obstacle": None},
        "errors": [],
        "read": False,
    }


@pytest.mark.parametrize(
    ("stopped", "failed", "kept"),
    [
        ("answered", False, True),
        ("turn_limit", False, False),
        ("cancelled", False, False),
        ("answered", True, False),
    ],
)
async def test_a_result_stands_only_for_a_run_that_ended_with_it(
    stopped: str, failed: bool, kept: bool
) -> None:
    """A valid answer, then a response round and a limit, keeps no result."""
    outcome = Outcome(TARGET)
    closing = await outcome.closing(Transcript(messages=[]), REPORT)
    assert closing.action == "accept"
    assert outcome.result is not None
    assert (result_of(outcome, stopped, failed) is not None) == kept


def test_a_result_is_kept_only_for_a_delegation_that_returned(tmp_path: Path) -> None:
    async def unused(child: Child, stop: asyncio.Event) -> ChildEnd:
        raise AssertionError

    _, records, projects, documents = _delegations(tmp_path, unused)
    try:
        for n, ended in enumerate(("cancelled", "returned")):
            records.start(PROJECT, SESSION, _made(n))
            found = records.end(
                PROJECT, SESSION, _made(n).delegation_id, Ending(ended, "x", result=RESULT)
            )
            assert (found.result is not None) == (ended == "returned")
            if ended == "cancelled":
                assert found.errors[-1].startswith("the result was not kept")
        records.start(PROJECT, SESSION, _made(2))
        with (
            pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed: delegations"),
            projects.control.write() as tx,
        ):
            tx.sql.execute(
                "UPDATE delegations SET ended = 'failed', stopped = 'error', ended_at = 'now', "
                "result = '{}' WHERE ended IS NULL"
            )
    finally:
        documents.close()
        projects.close()


def test_every_value_of_an_outcome_fits_its_table(tmp_path: Path) -> None:
    async def unused(child: Child, stop: asyncio.Event) -> ChildEnd:
        raise AssertionError

    _, records, projects, documents = _delegations(tmp_path, unused)
    try:
        records.start(PROJECT, SESSION, _made(0))
        wide = 'é\u0000"'  # two bytes, an escaped control character and a quote
        ending = Ending(
            "returned",
            "answered",
            report=wide * 30_000,
            refused=wide * 5_000,
            errors=tuple(wide * 3_000 for _ in range(50)),
            alias=wide * 1_000,
            models=tuple(wide * 500 for _ in range(50)),
            read_entry=True,
        )
        found = records.end(PROJECT, SESSION, _made(0).delegation_id, ending)
        assert (found.ended, found.read_entry) == ("returned", True)
        assert found.errors
        assert found.models
        assert found.alias is not None
        assert len(found.alias.encode()) <= 256
        assert found.refused is not None
        assert len(found.refused.encode()) <= 2048
    finally:
        documents.close()
        projects.close()


async def test_status_and_cancel_see_only_this_session_and_cancel_only_this_runs(
    tmp_path: Path,
) -> None:
    async def unused(child: Child, stop: asyncio.Event) -> ChildEnd:
        raise AssertionError

    made, records, projects, documents = _delegations(tmp_path, unused)
    try:
        projects.open_session(PROJECT, "other", dedicated=False)
        records.start(PROJECT, "other", _made(0))
        records.start(PROJECT, SESSION, _made(1))  # an earlier run's, which returned
        records.end(
            PROJECT, SESSION, _made(1).delegation_id, Ending("returned", "answered", REPORT)
        )
        elsewhere, earlier = _made(0).delegation_id, _made(1).delegation_id
        for name in ("delegation_status", "cancel_delegation"):
            for unknown in (elsewhere, "dlg-" + "f" * 24):
                with pytest.raises(WorkerAPIError, match="no delegation") as refused:
                    await made.call(name, {"delegation_id": unknown})
                assert refused.value.kind == "not_found"
        text, _ = await made.call("delegation_status", {"delegation_id": earlier})
        assert json.loads(text)["ended"] == "returned"
        with pytest.raises(WorkerAPIError, match="this run started no delegation"):
            await made.call("cancel_delegation", {"delegation_id": earlier})
    finally:
        documents.close()
        projects.close()


async def test_a_long_answer_alone_fits_a_tool_result(tmp_path: Path) -> None:
    async def unused(child: Child, stop: asyncio.Event) -> ChildEnd:
        raise AssertionError

    made, records, projects, documents = _delegations(tmp_path, unused)
    try:
        records.start(PROJECT, SESSION, _made(0))
        quotes = '"' * 16_000  # each doubles once escaped
        records.end(PROJECT, SESSION, _made(0).delegation_id, Ending("returned", "x", quotes))
        text, _ = await made.call("delegation_status", {"delegation_id": _made(0).delegation_id})
        shown = json.loads(text)
        assert len(text) <= made.output_chars
        assert shown["answer_cut"] is True
        assert shown["result"] is None
    finally:
        documents.close()
        projects.close()


def _made(n: int) -> Delegation:
    return Delegation.model_validate(
        {
            "delegation_id": f"dlg-{n:024x}",
            "parent": "20261015T110000000000Z-earlier",
            "run": f"20261015T11000{n}000000Z-child",
            "kind": "check",
            "brief": {**BRIEF, "check": {"handle": "C-1", "revision": 1}},
            "started_at": "2026-10-15T11:00:01+00:00",
        }
    )
