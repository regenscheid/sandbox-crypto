# SPDX-License-Identifier: MIT
"""A task on a track of the session's plan, and on an issue (plans-and-delegation spec,
PD-9, PD-12; ledger spec, LG-6, LG-7, LG-F8): its progress on its own track's steps,
under the plan version it started with, one task per track, and its outcome, what it
dismissed and the obstructions it claims, recorded on its issue as work."""

from __future__ import annotations

import json
from typing import Any

from sandbox_science.orchestrator.documents import USER
from sandbox_science.orchestrator.ledger.models import WorkRecord
from sandbox_science.orchestrator.session_plans import PlanBody, PlanRef, SessionPlans

from .ledger_support import AnchoredLedger as Ledger
from .loop_harness import PLAN, PROJECT, USER_PLANS, Harness, Reply, call
from .loop_harness import gateway as gateway
from .loop_harness import harness as harness
from .loop_harness import worker as worker
from .test_delegation import children, delegation, released, results, started, wait
from .test_delegation import delegating as delegating
from .test_delegation_tasks import DONE

TRACKED: dict[str, Any] = {
    **PLAN,
    "tracks": ({"track_id": "tables", "title": "Tables"},),
    "phases": (
        {
            "phase_id": "analysis",
            "title": "Analysis",
            "steps": (
                {
                    "step_id": "table",
                    "title": "Tabulate",
                    "description": "Tabulate a^16 mod 17",
                    "evidence_standard": "The table as a file",
                    "track_id": "tables",
                },
                {
                    "step_id": "write",
                    "title": "Write up",
                    "description": "Write it up",
                    "evidence_standard": "A document",
                },
            ),
        },
    ),
}


def active_plan(harness: Harness) -> PlanRef:
    """The user's plan with a track, made active; its reference."""
    documents = harness.documents()
    try:
        plans = SessionPlans(documents)
        made = plans.publish(USER_PLANS, "p1", PlanBody.model_validate(TRACKED), "Plan.")
        current = plans.current(USER_PLANS)
        assert current is not None
        plans.set_status(
            USER_PLANS, "p2", made.ref, current.head.status_revision,
            current.head.progress_cursor, "active", "Go.",
        )  # fmt: skip
        return made.ref
    finally:
        documents.close()


READ: Reply = Reply(calls=[call("plan_read")])
"""The root reads the plan the user made before its effects (the plan tools' rule)."""


def on_track(**more: Any) -> tuple[str, dict[str, Any]]:
    return call("delegate", task="Tabulate a^16 mod 17.", track="tables", **more)


def answer(record: Any, n: int) -> dict[str, Any]:
    found = json.loads(results(record)[n])
    return json.loads(found["tool_result"]) if "tool_result" in found else found


async def test_a_task_records_progress_on_its_own_tracks_steps(delegating: Harness) -> None:
    ref = active_plan(delegating)
    root = await delegating.run(
        "root",
        # The child's progress is another's plan event: the root reads the plan again.
        [READ, Reply(calls=[on_track()]), wait(), READ, Reply("Done.")],
        delegated=[
            Reply(
                calls=[
                    call("track_read"),
                    call("track_progress", step_id="table", status="completed", note="Done."),
                    call("track_progress", step_id="write", status="completed", note="Done."),
                ]
            ),
            Reply(json.dumps(DONE)),
        ],
    )
    began = answer(root, 1)
    assert began["track"] == "tables"
    first = delegating.gateway.children[0]
    assert "Your track: tables, of the session's plan." in str(first[1]["content"])
    offered = {t["function"]["name"] for t in delegating.gateway.tools["delegated"][0]}
    assert {"track_read", "track_progress"} <= offered
    assert not {"plan_read", "plan_progress"} & offered
    (child,) = children(delegating, root)
    read, progressed, refused = (json.loads(r) for r in results(child))
    assert read["plan"] == ref.model_dump(mode="json")
    assert [(s["step_id"], s["status"]) for s in read["steps"]] == [("table", "pending")]
    assert (progressed["receipt"]["action"], progressed["receipt"]["ref"]) == (
        "progress",
        ref.model_dump(mode="json"),
    )
    assert refused["error"]["kind"] == "not_found"
    documents = delegating.documents()
    try:
        current = SessionPlans(documents).compact_current(USER_PLANS)
    finally:
        documents.close()
    assert current is not None
    latest = {s.step_id: s.latest for s in current.steps}
    table = latest["table"]
    assert table is not None
    assert (table.status, table.author.role, table.author.track_id) == (
        "completed",
        "delegated",
        "tables",
    )
    assert latest["write"] is None
    (delegation_id,) = started(root)
    assert delegation(delegating, delegation_id).brief["track"] == {
        "track_id": "tables",
        "plan": ref.model_dump(mode="json"),
    }


async def test_a_track_has_one_running_task(delegating: Harness) -> None:
    """PD-12: a second task on the track is refused while the first runs."""
    active_plan(delegating)
    free, release = released()

    async def held() -> None:
        await free.wait()

    root = await delegating.run(
        "root",
        [
            READ,
            Reply(calls=[on_track(), on_track()]),
            Reply(calls_from=wait().calls_from, before=release.before),
            Reply("Done."),
        ],
        delegated=[Reply(json.dumps(DONE), before=held)],
    )
    refused = answer(root, 2)["error"]
    assert refused["kind"] == "conflict"
    assert refused["message"].startswith("track_busy")
    assert len(started(root)) == 1


async def test_a_revised_plan_refuses_the_tasks_progress(delegating: Harness) -> None:
    """The coordinator's decision: the grant is the plan version the task started under;
    the outcome says the plan changed, so the parent delegates the track again."""
    ref = active_plan(delegating)

    async def revise() -> None:
        documents = delegating.documents()
        try:
            SessionPlans(documents).revise(
                USER_PLANS, "p3", ref, PlanBody.model_validate(TRACKED), "Edited."
            )
        finally:
            documents.close()

    root = await delegating.run(
        "root",
        [READ, Reply(calls=[on_track()]), wait(), READ, Reply("Done.")],
        delegated=[
            Reply(
                calls=[call("track_progress", step_id="table", status="completed", note="x")],
                before=revise,
            ),
            Reply(json.dumps({**DONE, "status": "blocked", "obstacle": "The plan changed."})),
        ],
    )
    (child,) = children(delegating, root)
    (refused,) = results(child)
    assert json.loads(refused)["error"]["message"].startswith("plan_changed")
    (delegation_id,) = started(root)
    outcome = delegation(delegating, delegation_id)
    assert outcome.refused is not None
    assert "plan_changed" in outcome.refused
    assert "delegate the track again" in outcome.refused


async def test_a_track_must_be_of_the_active_plan(delegating: Harness) -> None:
    root = await delegating.run("root", [Reply(calls=[on_track()]), Reply("No.")])
    assert answer(root, 0)["error"]["message"].startswith("a track is of the session's active")
    active_plan(delegating)
    other = call("delegate", task="Go.", track="nope")
    root = await delegating.run("again", [READ, Reply(calls=[other]), Reply("No.")])
    assert answer(root, 1)["error"]["kind"] == "not_found"


# --- Its outcome on its issue (LG-F8) -----------------------------------------------------


def an_issue(harness: Harness) -> str:
    """Issue I-1, open; its handle."""
    documents = harness.documents()
    try:
        made = Ledger(documents).change(
            PROJECT,
            {
                "operation_id": "issue",
                "reason": "Asked.",
                "changes": [
                    {"type": "issue", "title": "Fermat for 17", "statement": "Does it hold?"}
                ],
            },
            USER,
        )
        return made.entries[0].handle
    finally:
        documents.close()


def worked(harness: Harness, handle: str) -> list[WorkRecord]:
    documents = harness.documents()
    try:
        return list(reversed(Ledger(documents).work(PROJECT, handle).records))
    finally:
        documents.close()


async def test_a_blocked_tasks_dismissals_and_obstructions_are_work_on_its_issue(
    delegating: Harness,
) -> None:
    """LG-F8: the outcome is a record on the issue, ending limit; one dismissal for lack
    of time and one claimed obstruction are records part of it; the issue stays open."""
    issue = an_issue(delegating)
    blocked = {
        **DONE,
        "status": "blocked",
        "obstacle": "No time left for the general case.",
        "dismissals": [{"what": "The general case for every prime", "limit": "time"}],
        "obstructions": [
            {"claim": "No proof exists for p = 2 here", "evidence": "a^1 = 1 mod 2 is trivial."}
        ],
    }
    task = call("delegate", task="Settle the issue.", issue=issue)
    root = await delegating.run(
        "root",
        [Reply(calls=[task]), wait(), Reply("Done.")],
        delegated=[Reply(json.dumps(blocked))],
    )
    (delegation_id,) = started(root)
    first = delegating.gateway.children[0]
    assert f"Your issue: {issue}." in str(first[1]["content"])
    outcome, dismissal, obstruction = worked(delegating, issue)
    assert (outcome.ending, outcome.limit) == ("limit", "tools")
    assert outcome.tried == "A delegated task: Settle the issue."
    assert outcome.references[0]["delegation"] == delegation_id
    assert (dismissal.ending, dismissal.limit, dismissal.part_of) == (
        "limit",
        "time",
        outcome.record_id,
    )
    assert dismissal.tried == "The general case for every prime"
    assert (obstruction.ending, obstruction.part_of) == ("obstruction_claimed", outcome.record_id)
    assert obstruction.references == [
        {"delegation": delegation_id, "note": "a^1 = 1 mod 2 is trivial."}
    ]
    documents = delegating.documents()
    try:
        assert Ledger(documents).entry(PROJECT, issue).status == "open"
    finally:
        documents.close()


async def test_a_task_that_found_nothing_ran_out_of_ideas(delegating: Harness) -> None:
    issue = an_issue(delegating)
    task = call("delegate", task="Look for a counterexample.", issue=issue)
    await delegating.run(
        "root", [Reply(calls=[task]), wait(), Reply("Done.")], delegated=[Reply(json.dumps(DONE))]
    )
    (outcome,) = worked(delegating, issue)
    assert (outcome.ending, outcome.limit) == ("limit", "ideas")


async def test_a_task_that_made_entries_produced_them(delegating: Harness) -> None:
    issue = an_issue(delegating)
    made = {
        "type": "task",
        "title": "Recheck",
        "statement": "Recheck for p = 2.",
        "anchors": [{"entry": issue}],
    }
    task = call("delegate", task="Break it down.", issue=issue)
    await delegating.run(
        "root",
        [Reply(calls=[task]), wait(), Reply("Done.")],
        delegated=[
            Reply(calls=[call("ledger_change", reason="A subtask.", changes=[made])]),
            Reply(json.dumps(DONE)),
        ],
    )
    (outcome,) = worked(delegating, issue)
    assert outcome.ending == "produced"
    assert outcome.produced == ["T-1"]


async def test_a_tasks_issue_is_an_issue(delegating: Harness) -> None:
    task = call("delegate", task="Go.", issue="C-1")
    root = await delegating.run("root", [Reply(calls=[task]), Reply("No.")])
    refused = answer(root, 0)["error"]
    assert (refused["kind"], refused["message"]) == (
        "invalid_request",
        "C-1 is a claim, not an issue",
    )
