# SPDX-License-Identifier: MIT
"""A run tree whose process ended (plans-and-delegation spec, PD-40; run-service spec,
RS-12): the run service ends a dead root's delegations ``interrupted`` and closes its
children's events, from their records when they saved one; a session's next main run
does the same for what a command-line run left; and the outcomes reach that run."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from sandbox_science.contracts import ExecutionContext
from sandbox_science.orchestrator.control import ControlError
from sandbox_science.orchestrator.delegation import records as records_module
from sandbox_science.orchestrator.delegation.records import (
    Delegation,
    DelegationRecords,
    Ending,
)
from sandbox_science.orchestrator.projects import ProjectStore, RunRecord, run_name
from sandbox_science.orchestrator.runstate import RunState
from sandbox_science.testing import FakeClock, eventually

from .loop_harness import PROJECT, SESSION, Harness, Reply
from .loop_harness import gateway as gateway
from .loop_harness import harness as harness
from .loop_harness import worker as worker
from .test_delegation import delegating as delegating
from .test_run_service import post, runs, serving, states

PAST = datetime.now(UTC) - timedelta(minutes=5)
ROOT = run_name(PAST, "crashed")


@pytest.mark.parametrize("selected", ["root", "child"])
def test_explicit_stop_survives_restart_and_applies_only_to_its_subtree(
    tmp_path: Path, selected: str
) -> None:
    first, sibling, leaf, completed = [_delegation(n) for n in range(4)]
    leaf = leaf.model_copy(update={"parent": first.run})
    with closing(ProjectStore(tmp_path)) as store:
        store.create(PROJECT, "minimal", "sha256:" + "a" * 64)
        store.open_session(PROJECT, SESSION, dedicated=False)
        store.open_session(PROJECT, "other", dedicated=False)
        state, records = RunState(store.control), DelegationRecords(store.control)
        for made in (first, sibling, leaf, completed):
            records.start(PROJECT, SESSION, made)
            state.append(PROJECT, SESSION, made.run, "started", {})
        records.end(PROJECT, SESSION, completed.delegation_id, Ending("returned", "answered"))
        state.cancel(PROJECT, SESSION, ROOT if selected == "root" else first.run)
    # Reopen before any cleanup or outcome: the accepted intent is already durable.
    with closing(ProjectStore(tmp_path)) as store:
        state, records = RunState(store.control), DelegationRecords(store.control)
        assert state.cancelled(PROJECT, SESSION, first.run)
        assert state.cancelled(PROJECT, SESSION, leaf.run)
        assert state.cancelled(PROJECT, SESSION, sibling.run) == (selected == "root")
        assert not state.cancelled(PROJECT, "other", first.run)
        assert not state.cancelled(PROJECT, SESSION, "unrelated")
        later = _delegation(4).model_copy(update={"parent": leaf.run})
        with pytest.raises(ControlError, match="explicit Stop"):
            records.start(PROJECT, SESSION, later)
        records.abandon(PROJECT, SESSION, parent=ROOT)
        for made in (first, leaf, sibling):
            expected = "cancelled" if made != sibling or selected == "root" else "interrupted"
            found = records.get(PROJECT, SESSION, made.delegation_id)
            assert (found.ended, found.stopped) == (expected, expected)
            assert state.events(PROJECT, SESSION, made.run)[-1].data["stopped"] == expected
        assert records.get(PROJECT, SESSION, completed.delegation_id).ended == "returned"
        assert records.abandon(PROJECT, SESSION, parent=ROOT) == []


async def test_service_recovery_preserves_a_stop_accepted_before_the_process_died(
    delegating: Harness,
) -> None:
    state, records = _dead_tree(delegating)
    state.cancel(PROJECT, SESSION, ROOT)
    async with serving(delegating):
        await eventually(lambda: runs(delegating).ended(PROJECT, SESSION, ROOT), timeout=10)
        await eventually(lambda: records.running(PROJECT, SESSION) == [], timeout=10)
    assert runs(delegating).events(PROJECT, SESSION, ROOT)[-1].data["stopped"] == "cancelled"
    assert records.get(PROJECT, SESSION, _delegation(0).delegation_id).ended == "cancelled"
    # A saved completed answer keeps its own event even though cleanup came later.
    assert (
        runs(delegating).events(PROJECT, SESSION, _delegation(1).run)[-1].data["stopped"]
        == "answered"
    )


def _delegation(n: int) -> Delegation:
    return Delegation.model_validate(
        {
            "delegation_id": f"dlg-{n:024x}",
            "parent": ROOT,
            "run": run_name(PAST + timedelta(seconds=n + 1), f"child-{n}"),
            "kind": "check",
            "brief": {"task": "Check."},
            "started_at": (PAST + timedelta(seconds=n + 1)).isoformat(),
        }
    )


def _saved(harness: Harness, made: Delegation) -> None:
    """A child that saved its record and died before its ``stopped`` event."""
    store = ProjectStore(harness.state)
    try:
        session = store.get(PROJECT).sessions[SESSION]
        context = ExecutionContext(
            project_id=PROJECT, session_id=SESSION, environment_id=session.environment_id
        )
        started = PAST + timedelta(seconds=2)
        store.save_run(
            RunRecord.model_validate(
                {
                    "run_id": "child-1",
                    "role": "delegated",
                    "parent": ROOT,
                    "delegation": made.delegation_id,
                    "context": context.model_dump(mode="json"),
                    "task": "Check.",
                    "started_at": started,
                    "ended_at": started,
                    "stopped": "answered",
                    "final_text": "x" * 20_000,
                    "messages": [],
                    "tools": [],
                }
            )
        )
    finally:
        store.close()


def _dead_tree(harness: Harness) -> tuple[RunState, DelegationRecords]:
    """A root whose process died with two children: one running, one that saved its
    record."""
    store = ProjectStore(harness.state)
    dead = RunState(store.control, clock=FakeClock(PAST))
    records = DelegationRecords(store.control, clock=FakeClock(PAST))
    dead.acquire(PROJECT, SESSION, ROOT, "a-dead-process")
    dead.append(PROJECT, SESSION, ROOT, "started", {"message_ids": []})
    for n in (0, 1):
        made = _delegation(n)
        records.start(PROJECT, SESSION, made)
        dead.append(PROJECT, SESSION, made.run, "started", {"role": "delegated"})
    _saved(harness, _delegation(1))
    return dead, records


async def test_the_run_service_ends_a_dead_roots_tree(delegating: Harness) -> None:
    _, records = _dead_tree(delegating)
    post(delegating, "m-1", "Are you there?")
    async with serving(delegating, Reply("Yes.")):
        await eventually(lambda: runs(delegating).ended(PROJECT, SESSION, ROOT), timeout=10)
        await eventually(lambda: states(delegating) == {"m-1": "delivered"}, timeout=20)
    state = runs(delegating)
    running, saved = _delegation(0), _delegation(1)
    assert records.get(PROJECT, SESSION, running.delegation_id).ended == "interrupted"
    # Had the root been alive, only slow, its cancelled child's outcome changes nothing.
    late = records.end(PROJECT, SESSION, running.delegation_id, Ending("cancelled", "cancelled"))
    assert late.ended == "interrupted"
    assert records.get(PROJECT, SESSION, saved.delegation_id).ended == "interrupted"
    (first,) = [e for e in state.events(PROJECT, SESSION, running.run) if e.kind == "stopped"]
    assert first.data == {
        "stopped": "interrupted",
        "failure": None,
        "final_text": None,
        "record": None,
    }
    (second,) = [e for e in state.events(PROJECT, SESSION, saved.run) if e.kind == "stopped"]
    assert (second.data["stopped"], second.data["record"]) == ("answered", saved.run)
    assert len(str(second.data["final_text"])) < 10_000  # the record keeps it whole
    # The next main run gets both outcomes, and acknowledges them.
    (root,) = [e for e in ProjectStore(delegating.state).runs(PROJECT, SESSION) if e.role == "main"]
    record = ProjectStore(delegating.state).run(PROJECT, SESSION, root.name)
    assert sorted(record.delivered["delegations"]) == [
        running.delegation_id,
        saved.delegation_id,
    ]


async def test_the_next_main_run_ends_what_a_command_line_run_left(delegating: Harness) -> None:
    dead, records = _dead_tree(delegating)
    with dead.control.write() as tx:  # the command line's process died: no service reaps it
        tx.sql.execute("DELETE FROM run_leases")
    later = await delegating.run("later", [Reply("Two checks were interrupted.")])
    running, saved = _delegation(0), _delegation(1)
    assert records.get(PROJECT, SESSION, running.delegation_id).ended == "interrupted"
    assert runs(delegating).ended(PROJECT, SESSION, running.run)
    stopped = runs(delegating).events(PROJECT, SESSION, saved.run)[-1]
    assert (stopped.kind, stopped.data["stopped"], stopped.data["record"]) == (
        "stopped",
        "answered",
        saved.run,
    )
    assert sorted(later.delivered["delegations"]) == [
        running.delegation_id,
        _delegation(1).delegation_id,
    ]


def test_a_dead_tree_ends_once_and_in_one_transaction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ending a dead tree writes each outcome and each run's stopped event once, all or
    nothing: a failure part way leaves it to be ended again."""
    store = ProjectStore(tmp_path)
    try:
        store.create(PROJECT, "minimal", "sha256:" + "a" * 64)
        store.open_session(PROJECT, SESSION, dedicated=False)
        state = RunState(store.control)
        records = DelegationRecords(store.control)
        state.append(PROJECT, SESSION, ROOT, "started", {})
        for n in (0, 1):
            records.start(PROJECT, SESSION, _delegation(n))
        state.append(PROJECT, SESSION, _delegation(0).run, "started", {})  # 1 wrote nothing

        def broken(*args: object) -> bool:
            raise sqlite3.OperationalError("disk I/O error")

        monkeypatch.setattr(records_module, "close_events", broken)
        with pytest.raises(sqlite3.OperationalError):
            records.abandon(PROJECT, SESSION, parent=ROOT)
        assert records.get(PROJECT, SESSION, _delegation(0).delegation_id).ended is None
        monkeypatch.undo()
        ended = records.abandon(PROJECT, SESSION, parent=ROOT)
        assert [d for d, _ in ended] == [_delegation(0).delegation_id, _delegation(1).delegation_id]
        assert records.abandon(PROJECT, SESSION, parent=ROOT) == []  # once
        kinds = [e.kind for e in state.events(PROJECT, SESSION, _delegation(0).run)]
        assert kinds == ["started", "stopped"]
        assert state.events(PROJECT, SESSION, _delegation(1).run) == []
    finally:
        store.close()


def test_an_outcome_and_a_tree_end_race_and_the_first_stands(tmp_path: Path) -> None:
    """A slow but alive root's child can be ended interrupted when the service reaps its
    root, and its own outcome then changes nothing; an outcome written first stays."""
    store = ProjectStore(tmp_path)
    try:
        store.create(PROJECT, "minimal", "sha256:" + "a" * 64)
        store.open_session(PROJECT, SESSION, dedicated=False)
        records = DelegationRecords(store.control)
        first, second = _delegation(0), _delegation(1)
        records.start(PROJECT, SESSION, first)
        records.start(PROJECT, SESSION, second)
        records.end(PROJECT, SESSION, second.delegation_id, Ending("cancelled", "cancelled"))
        assert [d for d, _ in records.abandon(PROJECT, SESSION)] == [first.delegation_id]
        late = records.end(PROJECT, SESSION, first.delegation_id, Ending("cancelled", "cancelled"))
        assert (late.ended, late.stopped) == ("interrupted", "interrupted")
        assert records.get(PROJECT, SESSION, second.delegation_id).ended == "cancelled"
    finally:
        store.close()


async def test_the_next_main_run_closes_a_lost_roots_children(delegating: Harness) -> None:
    """A command-line root that lost its lease cancelled its child, whose outcome was
    written but whose stopped event was not: the next main run writes it."""
    store = ProjectStore(delegating.state)
    state, records = RunState(store.control), DelegationRecords(store.control)
    state.append(PROJECT, SESSION, ROOT, "started", {})
    child = _delegation(1)
    records.start(PROJECT, SESSION, child)
    state.append(PROJECT, SESSION, child.run, "started", {"role": "delegated"})
    _saved(delegating, child)
    records.end(PROJECT, SESSION, child.delegation_id, Ending("cancelled", "cancelled"))
    await delegating.run("later", [Reply("Noted.")])
    stopped = state.events(PROJECT, SESSION, child.run)[-1]
    assert (stopped.kind, stopped.data["record"]) == ("stopped", child.run)
    assert records.get(PROJECT, SESSION, child.delegation_id).ended == "cancelled"


async def test_a_dead_root_with_a_long_answer_still_ends_its_tree(delegating: Harness) -> None:
    """The root's stopped event keeps the head and tail of a long answer, and its tree
    ends whatever happens to that event."""
    store = ProjectStore(delegating.state)
    dead = RunState(store.control, clock=FakeClock(PAST))
    records = DelegationRecords(store.control, clock=FakeClock(PAST))
    dead.acquire(PROJECT, SESSION, ROOT, "a-dead-process")
    dead.append(PROJECT, SESSION, ROOT, "started", {"message_ids": []})
    running = _delegation(0)
    records.start(PROJECT, SESSION, running)
    dead.append(PROJECT, SESSION, running.run, "started", {"role": "delegated"})
    context = ExecutionContext(
        project_id=PROJECT,
        session_id=SESSION,
        environment_id=store.get(PROJECT).sessions[SESSION].environment_id,
    )
    store.save_run(
        RunRecord.model_validate(
            {
                "run_id": "crashed",
                "context": context.model_dump(mode="json"),
                "task": "Check.",
                "started_at": PAST,
                "ended_at": PAST,
                "stopped": "answered",
                "final_text": "y" * 100_000,
                "messages": [],
                "tools": [],
            }
        )
    )
    async with serving(delegating):
        await eventually(lambda: runs(delegating).ended(PROJECT, SESSION, ROOT), timeout=10)
        await eventually(lambda: runs(delegating).ended(PROJECT, SESSION, running.run), timeout=10)
    stopped = runs(delegating).events(PROJECT, SESSION, ROOT)[-1]
    assert stopped.data["stopped"] == "answered"
    assert len(str(stopped.data["final_text"])) < 10_000
    assert records.get(PROJECT, SESSION, running.delegation_id).ended == "interrupted"
