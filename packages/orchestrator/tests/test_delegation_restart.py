# SPDX-License-Identifier: MIT
"""A run tree whose process ended (plans-and-delegation spec, PD-40; run-service spec,
RS-12): the run service ends a dead root's delegations ``interrupted`` and closes its
children's events, from their records when they saved one; a session's next main run
does the same for what a command-line run left; and the outcomes reach that run."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from sandbox_science.contracts import ExecutionContext
from sandbox_science.orchestrator.delegation.records import Delegation, DelegationRecords
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
    running = _delegation(0)
    assert records.get(PROJECT, SESSION, running.delegation_id).ended == "interrupted"
    assert runs(delegating).ended(PROJECT, SESSION, running.run)
    assert sorted(later.delivered["delegations"]) == [
        running.delegation_id,
        _delegation(1).delegation_id,
    ]


def test_closing_a_runs_events_writes_one_stopped_event(tmp_path: Path) -> None:
    store = ProjectStore(tmp_path)
    try:
        store.create(PROJECT, "minimal", "sha256:" + "a" * 64)
        store.open_session(PROJECT, SESSION, dedicated=False)
        state = RunState(store.control)
        assert not state.close(PROJECT, SESSION, "20261015T000000000000Z-none")  # no events
        run = "20261015T000000000000Z-run"
        state.append(PROJECT, SESSION, run, "started", {})
        assert state.close(PROJECT, SESSION, run)
        assert not state.close(PROJECT, SESSION, run)  # once
        assert [e.kind for e in state.events(PROJECT, SESSION, run)] == ["started", "stopped"]
    finally:
        store.close()
