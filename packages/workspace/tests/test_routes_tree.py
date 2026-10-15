# SPDX-License-Identifier: MIT
"""A session's run tree through HTTP (plans-and-delegation spec, PD-42): its runs,
running children included, with their parents and states, its delegations, and a
delegated run's events, active while its root holds the session."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from sandbox_science.orchestrator.delegation.records import Delegation, DelegationRecords
from sandbox_science.orchestrator.projects import ProjectStore, run_started
from sandbox_science.orchestrator.runstate import RunState

from .route_support import ClientFactory, error_of, workspace
from .test_routes_chat import SESSION, runs

ROOT = "20261015T120000000000Z-root"
CHILD = "20261015T120001000000Z-child"


def started(state: RunState, name: str, parent: str | None = None) -> None:
    data: dict[str, Any] = {
        "run_id": name.partition("-")[2],
        "role": "main" if parent is None else "delegated",
        "parent": parent,
        "delegation": None if parent is None else "dlg-" + "0" * 24,
    }
    state.append("p1", "a", name, "started", data)


async def test_a_sessions_tree_shows_its_running_child_and_its_delegation(
    aiohttp_client: ClientFactory, state_dir: Path, static_dir: Path
) -> None:
    state = runs(state_dir)
    state.acquire("p1", "a", ROOT, "holder")
    started(state, ROOT)
    started(state, CHILD, parent=ROOT)
    store = ProjectStore(state_dir)
    try:
        brief = {"task": "Check.", "note": "", "limits": {"turns": 12, "tokens": 150000}}
        made = Delegation.model_validate(
            {
                "delegation_id": "dlg-" + "0" * 24,
                "parent": ROOT,
                "run": CHILD,
                "kind": "check",
                "brief": brief,
                "started_at": run_started(CHILD),
            }
        )
        DelegationRecords(store.control).start("p1", "a", made)
    finally:
        store.close()
    client = await workspace(aiohttp_client, state_dir, static_dir)
    tree = await (await client.get(SESSION + "/tree")).json()
    # The session's earlier runs are kept; the root and its child are running.
    *kept, root, child = tree["runs"]
    assert kept
    assert all((r["kept"], r["state"]) == (True, "ended") for r in kept)
    assert [(r["name"], r["role"], r["parent"], r["state"]) for r in (root, child)] == [
        (ROOT, "main", None, "running"),
        (CHILD, "delegated", ROOT, "running"),
    ]
    (shown,) = tree["delegations"]
    assert (shown["run"], shown["ended"], shown["brief"]["limits"]["turns"]) == (CHILD, None, 12)
    assert tree["delegations_total"] == 1
    events = SESSION + f"/runs/{CHILD}/events"
    assert (await (await client.get(events)).json())["state"] == "active"
    state.release("p1", "a", "holder")
    assert (await (await client.get(events)).json())["state"] == "interrupted"
    tree = await (await client.get(SESSION + "/tree")).json()
    assert [r["state"] for r in tree["runs"][-2:]] == ["interrupted", "interrupted"]
    state.append("p1", "a", CHILD, "stopped", {"stopped": "cancelled"})
    assert (await (await client.get(events)).json())["state"] == "ended"


async def test_the_tree_of_an_unknown_session_is_not_found(
    aiohttp_client: ClientFactory, state_dir: Path, static_dir: Path
) -> None:
    client = await workspace(aiohttp_client, state_dir, static_dir)
    await error_of(await client.get("/api/v0/projects/p1/sessions/nope/tree"), 404, "not_found")
