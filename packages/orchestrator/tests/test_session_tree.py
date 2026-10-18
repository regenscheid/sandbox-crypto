# SPDX-License-Identifier: MIT
"""A session's run tree, as the workspace reads it (plans-and-delegation spec, PD-42):
kept runs, running ones and cut-off ones, each with its parent and state, and the
session's delegations with their briefs and outcomes."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from sandbox_science.orchestrator.delegation.records import (
    Delegation,
    DelegationRecords,
    Ending,
)
from sandbox_science.orchestrator.projects import ProjectStore, run_name, run_started
from sandbox_science.orchestrator.runstate import RunState
from sandbox_science.orchestrator.runtree import RunTrees

from .loop_harness import PROJECT, SESSION, Harness, Reply
from .loop_harness import gateway as gateway
from .loop_harness import harness as harness
from .loop_harness import worker as worker
from .test_delegation import CHECK, REPORT, wait
from .test_delegation import delegating as delegating
from .test_run_tree import record

T0 = datetime(2026, 10, 15, 12, tzinfo=UTC)


def at(seconds: int) -> datetime:
    return T0 + timedelta(seconds=seconds)


KEPT = run_name(at(0), "kept")
DEAD = run_name(at(1), "dead")
ORPHAN = run_name(at(2), "orphan")
ROOT = run_name(at(3), "root")
RUNNING = run_name(at(4), "running")
DONE = run_name(at(5), "done")


@pytest.fixture
def store(tmp_path: Path) -> Iterator[ProjectStore]:
    projects = ProjectStore(tmp_path)
    projects.create("p", "minimal", "sha256:" + "a" * 64)
    projects.open_session("p", "s", dedicated=False)
    yield projects
    projects.close()


def started(state: RunState, name: str, parent: str | None = None, n: int = 0) -> None:
    data: dict[str, Any] = {
        "project": "p",
        "session": "s",
        "run_id": name.partition("-")[2],
        "role": "main" if parent is None else "delegated",
        "parent": parent,
        "delegation": None if parent is None else f"dlg-{n:024x}",
        "message_ids": [],
        "prompt_provenance": None,
    }
    state.append("p", "s", name, "started", data)


def delegation(records: DelegationRecords, n: int, parent: str, run: str) -> str:
    made = Delegation.model_validate(
        {
            "delegation_id": f"dlg-{n:024x}",
            "parent": parent,
            "run": run,
            "kind": "check",
            "brief": {"task": "Check.", "limits": {"turns": 12, "tokens": 150_000}},
            "started_at": run_started(run),
        }
    )
    records.start("p", "s", made)
    return made.delegation_id


def test_the_tree_holds_kept_running_and_cut_off_runs(store: ProjectStore) -> None:
    """A kept run is ended; the root that holds the lease and its child without a
    stopped event are running; a child that wrote its stopped event is ended; a root
    whose process died, and its child, are interrupted."""
    kept = record("kept", started=at(0)).model_copy(
        update={"alias": "main", "limits": {"max_turns": 20}}
    )
    store.save_run(kept)
    state = RunState(store.control)
    started(state, DEAD)
    started(state, ORPHAN, parent=DEAD, n=1)
    started(state, ROOT)
    state.acquire("p", "s", ROOT, "holder")
    started(state, RUNNING, parent=ROOT, n=2)
    started(state, DONE, parent=ROOT, n=3)
    state.append("p", "s", DONE, "stopped", {"stopped": "answered"})
    records = DelegationRecords(store.control)
    delegation(records, 1, DEAD, ORPHAN)
    delegation(records, 2, ROOT, RUNNING)
    done = delegation(records, 3, ROOT, DONE)
    records.end("p", "s", done, Ending("returned", "answered", "{}", alias="main"))

    tree = RunTrees(store).session("p", "s")
    assert [(r.name, r.role, r.parent, r.state, r.kept) for r in tree.runs] == [
        (KEPT, "main", None, "ended", True),
        (DEAD, "main", None, "interrupted", False),
        (ORPHAN, "delegated", DEAD, "interrupted", False),
        (ROOT, "main", None, "running", False),
        (RUNNING, "delegated", ROOT, "running", False),
        (DONE, "delegated", ROOT, "ended", False),
    ]
    first = tree.runs[0]
    assert (first.alias, first.limits, first.stopped) == ("main", {"max_turns": 20}, "answered")
    assert (tree.runs[4].delegation, tree.runs[5].stopped) == (f"dlg-{2:024x}", "answered")
    assert [d.delegation_id for d in tree.delegations] == [f"dlg-{n:024x}" for n in (1, 2, 3)]
    assert tree.delegations_total == 3
    assert tree.delegations[1].brief["limits"] == {"turns": 12, "tokens": 150_000}
    assert (tree.delegations[2].ended, tree.delegations[1].ended) == ("returned", None)
    assert [RunTrees(store).state("p", "s", n) for n in (KEPT, RUNNING, DONE, ORPHAN)] == [
        "ended",
        "running",
        "ended",
        "interrupted",
    ]
    # Once the root's process dies, its children are cut off with it.
    state.release("p", "s", "holder")
    tree = RunTrees(store).session("p", "s")
    assert {r.name: r.state for r in tree.runs}[RUNNING] == "interrupted"
    assert RunTrees(store).state("p", "s", RUNNING) == "interrupted"


def test_a_tree_shows_the_latest_delegations(store: ProjectStore) -> None:
    state = RunState(store.control)
    started(state, ROOT)
    records = DelegationRecords(store.control)
    for n in range(3):
        delegation(records, n, ROOT, run_name(at(10 + n), f"child{n}"))
    shown, total = records.listed("p", "s", limit=2)
    assert ([d.delegation_id for d in shown], total) == ([f"dlg-{1:024x}", f"dlg-{2:024x}"], 3)


async def test_a_delegated_check_is_in_its_sessions_tree(delegating: Harness) -> None:
    """Through a run: the root and its child, both ended, and the delegation with the
    brief the platform wrote, the limits it granted and its outcome."""
    await delegating.run(
        "root", [Reply(calls=[CHECK]), wait(), Reply("Done.")], delegated=[Reply(REPORT)]
    )
    store = ProjectStore(delegating.state)
    try:
        tree = RunTrees(store).session(PROJECT, SESSION)
    finally:
        store.close()
    root, child = tree.runs
    assert (root.role, root.state, child.role, child.parent, child.state) == (
        "main",
        "ended",
        "delegated",
        root.name,
        "ended",
    )
    (made,) = tree.delegations
    assert (made.run, made.parent, made.ended) == (child.name, root.name, "returned")
    assert made.brief["limits"] == {"turns": 300, "tokens": 20_000_000}
    assert child.limits is not None
    assert child.limits["max_turns"] == 300
