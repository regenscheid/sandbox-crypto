# SPDX-License-Identifier: MIT
"""Two levels of delegation (plans-and-delegation spec, PD-15, PD-32, PD-35, PD-38,
PD-40): a main run's task it allows may delegate tasks once more, never checks; the
second level delegates nothing; handbacks go to the direct parent; the root holds the
session for its whole tree; the cap counts the whole tree; and cancels and the end of a
dead tree go down through every level."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from sandbox_science.orchestrator.delegation.records import (
    Delegation,
    DelegationRecords,
    Ending,
)
from sandbox_science.orchestrator.projects import ProjectStore

from .loop_harness import PROJECT, SESSION, Harness, Reply, call
from .loop_harness import gateway as gateway
from .loop_harness import harness as harness
from .loop_harness import worker as worker
from .test_delegation import (
    CHECK,
    about,
    children,
    configure,
    delegation,
    kept,
    name,
    results,
    started,
    wait,
)
from .test_delegation import delegating as delegating
from .test_delegation_tasks import DONE

LEAD = call("delegate", task="Lead the work.", delegate=True)
PART = call("delegate", task="Do a part.")
WRITE = call("write_file", path="part.txt", content="A part.\n")


def answer(record: Any, n: int) -> dict[str, Any]:
    found = json.loads(results(record)[n])
    return json.loads(found["tool_result"]) if "tool_result" in found else found


def offered(harness: Harness, n: int) -> set[str]:
    return {t["function"]["name"] for t in harness.gateway.tools["delegated"][n]}


async def test_a_task_delegates_once_more_and_its_child_hands_back_to_it(
    delegating: Harness,
) -> None:
    configure(delegating, depth=2)
    root = await delegating.run(
        "root",
        [Reply(calls=[LEAD]), wait(), Reply("Done.")],
        routed={
            "Lead the work.": [Reply(calls=[PART]), wait(), Reply(json.dumps(DONE))],
            "Do a part.": [Reply(calls=[WRITE]), Reply(json.dumps(DONE))],
        },
    )
    assert answer(root, 0)["delegate"] is True
    (child,) = children(delegating, root)
    (grandchild,) = children(delegating, child)
    assert (grandchild.role, grandchild.parent) == ("delegated", name(child))
    # The root held the session for its grandchild: its write was made.
    (wrote,) = results(grandchild)
    assert "error" not in json.loads(wrote)
    # The root's wait returned its child alone; the child's, its own child.
    (waited,) = answer(root, 1)["delegations"]
    (lead,) = started(root)
    assert waited["delegation_id"] == lead
    (part,) = started(child)
    assert json.loads(results(child)[1])["delegations"][0]["delegation_id"] == part
    found = delegation(delegating, part)
    assert (found.parent, found.acknowledged) == (name(child), name(child))
    briefs = [str(m[1]["content"]) for m in delegating.gateway.children]
    assert any("You may delegate tasks of your own" in b for b in briefs)
    store = ProjectStore(delegating.state)
    try:
        assert DelegationRecords(store.control).handbacks(PROJECT, SESSION) == []
    finally:
        store.close()


async def test_the_second_level_delegates_nothing_and_no_level_delegates_checks(
    delegating: Harness,
) -> None:
    configure(delegating, depth=2)
    root = await delegating.run(
        "root",
        [Reply(calls=[LEAD]), wait(), Reply("Done.")],
        routed={
            "Lead the work.": [Reply(calls=[CHECK, PART]), wait(), Reply(json.dumps(DONE))],
            "Do a part.": [Reply(calls=[PART]), Reply(json.dumps(DONE))],
        },
    )
    (child,) = children(delegating, root)
    refused = json.loads(results(child)[0])["error"]
    assert refused["kind"] == "policy_denied"
    assert refused["message"] == (
        "not_permitted_for_role: a delegated run delegates tasks, never checks"
    )
    (grandchild,) = children(delegating, child)
    error = json.loads(results(grandchild)[0])["error"]
    assert error["message"].startswith("not_permitted_for_role")


async def test_only_a_main_run_at_depth_2_lets_a_task_delegate(delegating: Harness) -> None:
    root = await delegating.run("root", [Reply(calls=[LEAD]), Reply("No.")])
    refused = answer(root, 0)["error"]
    assert (refused["kind"], refused["message"].split(":")[0]) == (
        "policy_denied",
        "not_permitted_for_role",
    )
    assert "delegations" not in root.summaries


async def test_the_cap_counts_the_whole_tree(delegating: Harness) -> None:
    configure(delegating, depth=2, cap=2)
    root = await delegating.run(
        "root",
        [Reply(calls=[LEAD]), wait(), Reply("Done.")],
        routed={
            "Lead the work.": [Reply(calls=[PART, PART]), wait(), Reply(json.dumps(DONE))],
            "Do a part.": [Reply(json.dumps(DONE))],
        },
    )
    (child,) = children(delegating, root)
    refused = answer(child, 1)["error"]
    assert refused["message"].startswith("limit_reached: 2 delegated runs")


async def test_a_cancel_goes_down_through_every_level(delegating: Harness) -> None:
    """The root cancels its child while the grandchild runs: both end cancelled."""
    configure(delegating, depth=2)
    working, free = asyncio.Event(), asyncio.Event()

    async def held() -> None:
        working.set()
        await free.wait()

    async def started_below() -> None:
        await working.wait()

    cancel = about("cancel_delegation")
    try:
        root = await delegating.run(
            "root",
            [
                Reply(calls=[LEAD]),
                Reply(calls_from=cancel.calls_from, before=started_below),
                Reply("Cancelled."),
            ],
            routed={
                "Lead the work.": [Reply(calls=[PART]), wait()],
                "Do a part.": [Reply(json.dumps(DONE), before=held)],
            },
        )
    finally:
        free.set()
    (child,) = children(delegating, root)
    (grandchild,) = children(delegating, child)
    assert (child.stopped, grandchild.stopped) == ("cancelled", "cancelled")
    (lead,) = started(root)
    (part,) = started(child)
    assert (delegation(delegating, lead).ended, delegation(delegating, part).ended) == (
        "cancelled",
        "cancelled",
    )
    assert len(kept(delegating)) == 3


def made(n: int, parent: str, run: str) -> Delegation:
    return Delegation.model_validate(
        {
            "delegation_id": f"dlg-{n:024x}",
            "parent": parent,
            "run": run,
            "kind": "task",
            "brief": {"task": "Go."},
            "started_at": "2026-10-15T12:00:01+00:00",
        }
    )


ROOT = "20261015T120000000000Z-run-root"
CHILD = "20261015T120001000000Z-run-child"
GRANDCHILD = "20261015T120002000000Z-run-grandchild"


def test_a_dead_trees_end_and_its_handbacks_follow_its_levels(tmp_path: Path) -> None:
    store = ProjectStore(tmp_path)
    try:
        store.create(PROJECT, "minimal", "sha256:" + "a" * 64)
        store.open_session(PROJECT, SESSION, dedicated=False)
        records = DelegationRecords(store.control)
        records.start(PROJECT, SESSION, made(1, ROOT, CHILD))
        records.start(PROJECT, SESSION, made(2, CHILD, GRANDCHILD))
        assert records.running(PROJECT, SESSION) == [CHILD, GRANDCHILD]
        ended = records.abandon(PROJECT, SESSION, parent=ROOT)
        assert [run for _, run in ended] == [CHILD, GRANDCHILD]
        assert records.running(PROJECT, SESSION) == []
        # The main run takes its own children's outcomes; a child, its own.
        assert [d.run for d in records.handbacks(PROJECT, SESSION)] == [CHILD]
        assert [d.run for d in records.handbacks(PROJECT, SESSION, parent=CHILD)] == [GRANDCHILD]
        records.start(PROJECT, SESSION, made(3, ROOT, "20261015T120003000000Z-run-other"))
        records.end(PROJECT, SESSION, made(3, ROOT, "x").delegation_id, Ending("failed", "error"))
        assert len(records.handbacks(PROJECT, SESSION)) == 2
    finally:
        store.close()
