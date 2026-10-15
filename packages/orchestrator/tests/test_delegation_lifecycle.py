# SPDX-License-Identifier: MIT
"""Delegation's edges: the plan fence, the parent's kernel sweep, waits, failures,
cancellation and lost leases, bounded handbacks, the budget, and outcomes that must be
written whatever happens (plans-and-delegation spec, PD-34 to PD-40; LG-13).

The end-to-end tests drive ``cli.main``, or the run service, against the in-process
worker and the scripted gateway, as ``test_delegation`` does.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from pathlib import Path
from typing import Any, Final

import pytest

from sandbox_science.contracts import ExecutionContext
from sandbox_science.orchestrator.delegation import hook
from sandbox_science.orchestrator.delegation.fence import ChildFence
from sandbox_science.orchestrator.delegation.hook import Child, ChildEnd, Delegations
from sandbox_science.orchestrator.delegation.records import (
    Delegation,
    DelegationRecords,
    Ending,
)
from sandbox_science.orchestrator.documents import USER, DocumentStore, store_path
from sandbox_science.orchestrator.ledger import Ledger
from sandbox_science.orchestrator.loop import Transcript
from sandbox_science.orchestrator.notices import DELEGATIONS
from sandbox_science.orchestrator.projects import ProjectStore, run_name
from sandbox_science.orchestrator.runstate import RunState
from sandbox_science.orchestrator.session_plans import PlanBody, SessionPlans
from sandbox_science.testing import eventually

from .loop_harness import PLAN, PROJECT, SESSION, USER_PLANS, Harness, Reply, call
from .loop_harness import gateway as gateway
from .loop_harness import harness as harness
from .loop_harness import worker as worker
from .test_delegation import (
    CHECK,
    REPORT,
    RESULT,
    children,
    delegation,
    name,
    results,
    started,
    wait,
)
from .test_delegation import delegating as delegating
from .test_run_service import serving

Message = dict[str, Any]
FENCED = {
    "entry": "C-1",
    "revision": 1,
    "status": "blocked",
    "verdict": "inconclusive",
    "summary": "I could not compute: the platform refused my calls.",
    "coverage": {"examined": ["the statement"], "not_examined": ["the computation"]},
    "obstacle": "The plan fence: the session's plan is a draft.",
}


def blocking() -> tuple[asyncio.Event, Reply]:
    """An event, and a child's answer that waits for it while the model thinks."""
    free = asyncio.Event()

    async def blocked() -> None:
        await free.wait()

    return free, Reply(REPORT, before=blocked)


def depth(harness: Harness, value: int) -> None:
    store = ProjectStore(harness.state)
    try:
        DelegationRecords(store.control).configure(PROJECT, depth=value)
    finally:
        store.close()


# --- The plan fence (PD-34) --------------------------------------------------------------


async def test_a_check_keeps_the_plan_fence(delegating: Harness) -> None:
    """A plan the user publishes while the child runs is a draft: the child's effects are
    refused, and the outcome says why."""

    async def plan() -> None:
        delegating.user_plan()

    root = await delegating.run(
        "root",
        [Reply(calls=[CHECK]), wait(), Reply(calls=[call("plan_read")]), Reply("Fenced.")],
        delegated=[
            Reply(calls=[call("write_file", path="check.txt", content="x")], before=plan),
            Reply(json.dumps(FENCED)),
        ],
    )
    (child,) = children(delegating, root)
    (refused,) = results(child)
    error = json.loads(refused)["error"]
    assert error["kind"] == "policy_denied"
    assert "draft" in error["message"]
    (delegation_id,) = started(root)
    outcome = delegation(delegating, delegation_id)
    assert outcome.refused is not None
    assert "draft" in outcome.refused
    assert child.summaries["fence"] == {"refused": outcome.refused}
    assert outcome.result is not None
    assert (outcome.result["status"], outcome.result["verdict"]) == ("blocked", "inconclusive")


def test_the_fence_holds_once_the_user_changes_an_active_plan(tmp_path: Path) -> None:
    projects = ProjectStore(tmp_path)
    documents = DocumentStore(store_path(tmp_path))
    try:
        projects.create(PROJECT, "minimal", "sha256:" + "a" * 64)
        session = projects.open_session(PROJECT, SESSION, dedicated=False)
        plans = SessionPlans(documents)
        first = plans.publish(USER_PLANS, "p1", PlanBody.model_validate(PLAN), "Plan.")
        current = plans.current(USER_PLANS)
        assert current is not None
        plans.set_status(
            USER_PLANS, "p2", first.ref, current.head.status_revision,
            current.head.progress_cursor, "active", "Go.",
        )  # fmt: skip
        context = ExecutionContext(
            project_id=PROJECT, session_id=SESSION, environment_id=session.environment_id
        )
        fence = ChildFence(plans, projects, context, run_id="child")
        assert asyncio.run(fence.gate("effect")) is None
        assert asyncio.run(fence.gate("read")) is None
        plans.revise(USER_PLANS, "p3", first.ref, PlanBody.model_validate(PLAN), "Edited.")
        refused = asyncio.run(fence.gate("effect"))
        assert refused is not None
        assert refused.message.startswith("plan_changed")
        assert asyncio.run(fence.gate("effect")) is not None  # it holds
        assert fence.summary() == {"refused": refused.message}
    finally:
        documents.close()
        projects.close()


# --- Kernels (PD-37) ---------------------------------------------------------------------


async def test_the_parents_kernel_sweep_leaves_a_running_childs_kernels(
    delegating: Harness,
) -> None:
    """The root's first observation fails, so its sweep of earlier runs' kernels comes
    later, while its child runs; the child's kernel and its state survive it."""
    lineage = delegating.worker.lineage
    lineage.fail = True
    computed, swept = asyncio.Event(), asyncio.Event()

    async def child_computed() -> None:
        computed.set()
        await swept.wait()

    async def observed_later() -> None:
        await computed.wait()
        lineage.fail = False  # the root's next turn observes, and sweeps

    async def after_the_sweep() -> None:
        swept.set()

    root = await delegating.run(
        "root",
        [
            Reply(calls=[CHECK]),
            Reply(calls=[call("plan_read")], before=observed_later),
            Reply(calls_from=wait().calls_from, before=after_the_sweep),
            Reply("Done."),
        ],
        delegated=[
            Reply(calls=[call("python", code="x = 41")]),
            Reply(calls=[call("python", code="print(x + 1)")], before=child_computed),
            Reply(REPORT),
        ],
    )
    (child,) = children(delegating, root)
    second = json.loads(results(child)[1])
    assert "42" in json.dumps(second), second


# --- Waits ---------------------------------------------------------------------------------


async def test_a_wait_ends_when_the_user_writes(delegating: Harness) -> None:
    """The user writes while the root asks for its wait: the wait ends with the message,
    which the root's next model call gets, and the child goes on."""
    free, answer = blocking()

    async def write() -> None:
        RunState(ProjectStore(delegating.state).control).post(
            PROJECT, SESSION, "m-1", "How is the check going?", author="user"
        )

    async def release() -> None:
        free.set()

    root = await delegating.run(
        "root",
        [
            Reply(calls=[CHECK]),
            Reply(calls_from=wait().calls_from, before=write),
            Reply(calls_from=wait().calls_from, before=release),
            Reply("It confirmed C-1."),
        ],
        delegated=[answer],
    )
    first, second = (json.loads(r) for r in results(root)[1:])
    assert first["reason"] == "user_message"
    assert first["delegations"][0]["state"] == "running"
    assert second["reason"] == "ended"
    assert root.delivered["messages"] == ["m-1"]


async def test_a_run_that_fails_after_a_wait_leaves_the_outcome_to_the_next(
    delegating: Harness,
) -> None:
    """A wait's result that no model call saw is not received: the outcome is not
    acknowledged, and the session's next main run gets it as a notice.

    The child ends only once the root asks for its wait, after that call's notices: a
    child that ended before would reach the root as a notice of a call that succeeded."""
    free, answer = blocking()

    async def release() -> None:
        free.set()

    root = await delegating.run(
        "root",
        [
            Reply(calls=[CHECK]),
            Reply(calls_from=wait().calls_from, before=release),
            Reply(status=500),
        ],
        delegated=[answer],
        status=1,
    )
    waited = json.loads(results(root)[-1])
    assert waited["reason"] == "ended"
    assert root.failure == "model"
    (delegation_id,) = started(root)
    assert delegation(delegating, delegation_id).acknowledged is None
    later = await delegating.run("later", [Reply("Noted.")])
    first = delegating.gateway.main[0]
    assert any(
        str(m.get("content", "")).startswith(DELEGATIONS.text) and delegation_id in m["content"]
        for m in first
    )
    assert delegation(delegating, delegation_id).acknowledged == name(later)


# --- Authority (LG-13) ---------------------------------------------------------------------


WITHHELD: Final = {
    "ledger_change": {"reason": "I checked it.", "changes": []},
    "ledger_import": {"path": "results.jsonl", "reason": "Results."},
    "ledger_category": {"category": "key-recovery"},
    "document_create": {"name": "notes2", "title": "Notes", "text": "x"},
    "document_patch": {"document": "notes", "revision": 1, "operations": []},
    "document_delete": {"document": "notes", "reason": "Gone."},
    "document_comment": {"document": "notes", "block": "b1", "text": "x"},
    "document_reply": {"document": "notes", "comment": "c1", "text": "x"},
    "document_comment_edit": {"document": "notes", "comment": "c1", "text": "x"},
    "document_resolve": {"document": "notes", "comment": "c1"},
    "memory_propose": {"action": "add", "text": "A fact.", "reason": "Found."},
}
"""The tools a check is not offered: it changes no ledger entry, document or memory
(PD-34, LG-13, as the coordinator tightened them on 2026-10-15)."""


@pytest.mark.parametrize("tool", sorted(WITHHELD))
async def test_a_check_changes_no_entry_document_or_memory(delegating: Harness, tool: str) -> None:
    """D1: a check reads and recomputes; each tool that writes is withheld, and a call to
    it is refused."""
    root = await delegating.run(
        "root",
        [Reply(calls=[CHECK]), wait(), Reply("Done.")],
        delegated=[Reply(calls=[call(tool, **WITHHELD[tool])]), Reply(REPORT)],
    )
    (child,) = children(delegating, root)
    offered = {t["function"]["name"] for t in delegating.gateway.tools["delegated"][0]}
    assert tool not in offered
    assert {"ledger_read", "document_read", "memory_list", "shell", "write_file"} <= offered
    (refused,) = results(child)
    error = json.loads(refused)["error"]
    assert (error["kind"], error["message"].split(":")[0]) == (
        "policy_denied",
        "not_permitted_for_role",
    )


# --- Cancellation and the lease (PD-39) ---------------------------------------------------


async def test_the_users_cancel_cancels_the_live_check(delegating: Harness) -> None:
    free, answer = blocking()
    delegating.gateway.delegated = [answer]
    state = RunState(ProjectStore(delegating.state).control)
    state.post(PROJECT, SESSION, "m-1", "Check C-1.", author="user")
    try:
        async with serving(delegating, Reply(calls=[CHECK]), wait()):
            await eventually(lambda: delegating.gateway.children)
            held = state.lease(PROJECT, SESSION)
            assert held is not None
            state.cancel(PROJECT, SESSION, held.run)
            await eventually(lambda: state.ended(PROJECT, SESSION, held.run), timeout=20)
    finally:
        free.set()
    store = ProjectStore(delegating.state)
    try:
        root = store.run(PROJECT, SESSION, held.run)
    finally:
        store.close()
    (child,) = children(delegating, root)
    assert (root.stopped, child.stopped) == ("cancelled", "cancelled")
    (delegation_id,) = started(root)
    assert delegation(delegating, delegation_id).ended == "cancelled"


async def test_a_lost_lease_cancels_the_live_check(delegating: Harness) -> None:
    free, answer = blocking()
    delegating.gateway.delegated = [answer]
    state = RunState(ProjectStore(delegating.state).control)
    state.post(PROJECT, SESSION, "m-1", "Check C-1.", author="user")
    try:
        async with serving(delegating, Reply(calls=[CHECK]), wait(), lease_seconds=6.0):
            await eventually(lambda: delegating.gateway.children)
            held = state.lease(PROJECT, SESSION)
            assert held is not None
            with state.control.write() as tx:  # another holder takes the session
                tx.sql.execute("DELETE FROM run_leases")
            state.acquire(PROJECT, SESSION, "another", "thief")

            def cancelled() -> bool:
                found = [c.stopped for c in kept_children(delegating, held.run)]
                return found == ["cancelled"]

            await eventually(cancelled, timeout=20)
            # Its root lost the session, so the child could not write its stopped event:
            # the service writes it from the child's record (PD-40).
            (child,) = kept_children(delegating, held.run)
            name = run_name(child.started_at, child.run_id)
            await eventually(lambda: state.ended(PROJECT, SESSION, name), timeout=20)
    finally:
        free.set()
    records = DelegationRecords(ProjectStore(delegating.state).control)
    (outcome,) = records.handbacks(PROJECT, SESSION)
    assert (outcome.ended, outcome.parent) == ("cancelled", held.run)
    stopped = state.events(PROJECT, SESSION, name)[-1]
    assert (stopped.kind, stopped.data["stopped"], stopped.data["record"]) == (
        "stopped",
        "cancelled",
        name,
    )


def kept_children(harness: Harness, parent: str) -> list[Any]:
    store = ProjectStore(harness.state)
    try:
        return [
            store.run(PROJECT, SESSION, entry.name)
            for entry in store.runs(PROJECT, SESSION)
            if entry.parent == parent
        ]
    finally:
        store.close()


# --- Bounds, the budget and the setting --------------------------------------------------


async def test_handbacks_are_summaries_and_waits_return_whole_outcomes_in_pages(
    delegating: Harness,
) -> None:
    long = json.dumps({**RESULT, "summary": "x" * 7_900})
    go = asyncio.Event()

    async def held() -> None:
        await go.wait()

    async def all_ended() -> None:
        go.set()
        store = ProjectStore(delegating.state)
        try:
            records = DelegationRecords(store.control)
            await eventually(lambda: len(records.handbacks(PROJECT, SESSION)) == 3, timeout=20)
        finally:
            store.close()

    root = await delegating.run(
        "root",
        [
            Reply(calls=[CHECK, CHECK, CHECK]),
            Reply(calls=[call("list_files")], before=all_ended),
            wait(),
            Reply("Three checks confirmed."),
        ],
        delegated=[Reply(long, before=held) for _ in range(3)],
    )
    attached = json.loads(results(root)[3])["delegation_notices"]
    assert len(attached) == 3
    assert len(json.dumps(attached)) <= 5000
    assert all((n["verdict"], n["status"]) == ("confirmed", "complete") for n in attached)
    waited = json.loads(results(root)[4])["delegations"]
    assert [d["result"]["summary"] for d in waited[:2]] == ["x" * 7_900] * 2
    assert "more" in waited[2]
    assert "result" not in waited[2] or waited[2].get("result") is None
    # The one a wait left out was received through its notice: all are acknowledged.
    assert {delegation(delegating, i).acknowledged for i in started(root)} == {name(root)}


async def test_a_project_that_stopped_delegating_still_gets_its_outcomes(
    delegating: Harness,
) -> None:
    free, answer = blocking()
    try:
        root = await delegating.run(
            "root", [Reply(calls=[CHECK]), Reply("Done.")], delegated=[answer],
            max_turns=2, status=3,
        )  # fmt: skip
    finally:
        free.set()
    depth(delegating, 0)
    later = await delegating.run("later", [Reply("Noted.")])
    (delegation_id,) = started(root)
    assert later.delivered["delegations"] == [delegation_id]


async def test_a_spent_budget_refuses_a_delegation(delegating: Harness) -> None:
    RunState(ProjectStore(delegating.state).control).set_budget(PROJECT, 1)
    root = await delegating.run("root", [Reply(calls=[CHECK]), Reply("No budget.")])
    refused = json.loads(results(root)[0])["error"]
    assert refused["kind"] == "unavailable"
    assert "model budget" in refused["message"]
    assert "delegations" not in root.summaries


@pytest.mark.parametrize(
    ("brief", "error"),
    [
        ({"task": "Confirm it."}, "task"),
        ({"inputs": ["documents/notes"]}, "inputs"),
        ({"context": "It holds."}, "context"),
        ({"note": "x" * 501}, "500"),
        ({"note": "a\u202eb"}, "note"),
    ],
)
async def test_a_check_brief_takes_no_task_or_inputs_and_a_short_note(
    delegating: Harness, brief: dict[str, str], error: str
) -> None:
    """LG-17: the platform writes a check's task and lists its inputs; the parent's note
    is at most 500 characters, which the user can see."""
    made = call("delegate", check={"entry": "C-1", "revision": 1}, **brief)
    root = await delegating.run("root", [Reply(calls=[made]), Reply("Refused.")])
    refused = json.loads(results(root)[0])["error"]
    assert refused["kind"] == "invalid_request"
    assert error in refused["message"]


# --- Outcomes that must be written ---------------------------------------------------------


def _delegations(
    tmp_path: Path, runner: Any, *, records: DelegationRecords | None = None
) -> tuple[Delegations, DelegationRecords, ProjectStore, DocumentStore]:
    projects = ProjectStore(tmp_path)
    projects.create(PROJECT, "minimal", "sha256:" + "a" * 64)
    projects.open_session(PROJECT, SESSION, dedicated=False)
    documents = DocumentStore(store_path(tmp_path))
    claim = {"type": "claim", "origin": "project", "title": "C", "statement": "S."}
    Ledger(documents).change(
        PROJECT, {"operation_id": "c", "reason": "r", "changes": [claim]}, USER
    )
    found = records or DelegationRecords(projects.control)
    context = ExecutionContext(project_id=PROJECT, session_id=SESSION, environment_id="e")
    made = Delegations(
        found,
        Ledger(documents),
        context,
        parent="20261015T120000000000Z-root",
        runner=runner,
        max_turns=30,
        offer=True,
    )
    return made, found, projects, documents


BRIEF = {"check": {"entry": "C-1", "revision": 1}}


async def test_a_child_that_outlives_its_cancel_is_interrupted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(hook, "STOP_SECONDS", 0.05)
    never = asyncio.Event()

    async def stubborn(child: Child, stop: asyncio.Event) -> ChildEnd:
        while not never.is_set():
            with contextlib.suppress(asyncio.CancelledError):  # it ignores its cancel
                await never.wait()
        return ChildEnd("cancelled", False, None)

    made, records, projects, documents = _delegations(tmp_path, stubborn)
    try:
        async with made.lease():
            text, _ = await made.call("delegate", BRIEF)
            await asyncio.sleep(0.01)  # it starts
        delegation_id = json.loads(text)["delegation_id"]
        found = records.get(PROJECT, SESSION, delegation_id)
        assert (found.ended, found.stopped) == ("interrupted", "cancelled")
    finally:
        never.set()
        for child in made.children.values():
            child.task.cancel()
        documents.close()
        projects.close()


async def test_an_outcome_is_written_again_after_a_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def quick(child: Child, stop: asyncio.Event) -> ChildEnd:
        return ChildEnd("answered", False, REPORT)

    made, records, projects, documents = _delegations(tmp_path, quick)
    real, calls = records.end, []

    def flaky(*args: Any) -> Any:
        calls.append(args)
        if len(calls) < 2:
            raise OSError("disk full")
        return real(*args)

    monkeypatch.setattr(records, "end", flaky)
    monkeypatch.setattr(made.clock, "sleep", lambda _: asyncio.sleep(0))
    try:
        async with made.lease():
            text, _ = await made.call("delegate", BRIEF)
            await asyncio.gather(*(c.task for c in made.children.values()))
        found = records.get(PROJECT, SESSION, json.loads(text)["delegation_id"])
        assert (found.ended, len(calls)) == ("returned", 2)
    finally:
        documents.close()
        projects.close()


def test_a_delegation_whose_process_ended_is_interrupted_at_the_next_run(tmp_path: Path) -> None:
    async def unused(child: Child, stop: asyncio.Event) -> ChildEnd:
        raise AssertionError

    made, records, projects, documents = _delegations(tmp_path, unused)
    try:
        records.start(
            PROJECT,
            SESSION,
            Delegation.model_validate(
                {
                    "delegation_id": "dlg-" + "1" * 24,
                    "parent": "20261015T110000000000Z-dead",
                    "run": "20261015T110001000000Z-child",
                    "kind": "check",
                    "brief": {},
                    "started_at": "2026-10-15T11:00:01+00:00",
                }
            ),
        )

        async def start() -> None:
            async with made.lease():
                pass

        asyncio.run(start())
        found = records.get(PROJECT, SESSION, "dlg-" + "1" * 24)
        assert (found.ended, found.acknowledged) == ("interrupted", None)
        assert records.end(PROJECT, SESSION, found.delegation_id, Ending("returned", "x")) == found
    finally:
        documents.close()
        projects.close()


async def test_a_halt_with_children_running_keeps_its_reason(tmp_path: Path) -> None:
    never = asyncio.Event()

    async def waiting(child: Child, stop: asyncio.Event) -> ChildEnd:
        await never.wait()
        raise AssertionError

    made, _, projects, documents = _delegations(tmp_path, waiting)
    try:
        async with made.lease():
            await made.call("delegate", BRIEF)
            transcript = Transcript(messages=[], halted="budget")
            transcript.room = False
            closing = await made.closing(transcript, "An answer.")
            assert (closing.action, closing.stopped) == ("retry", "budget")
            transcript.halted = None
            assert (await made.closing(transcript, "An answer.")).stopped == "context_limit"
    finally:
        documents.close()
        projects.close()


async def test_a_child_cancelled_before_it_starts_is_cancelled(tmp_path: Path) -> None:
    async def unused(child: Child, stop: asyncio.Event) -> ChildEnd:
        raise AssertionError

    made, records, projects, documents = _delegations(tmp_path, unused)
    try:
        async with made.lease():
            text, _ = await made.call("delegate", BRIEF)  # its parent ends at once
        found = records.get(PROJECT, SESSION, json.loads(text)["delegation_id"])
        assert (found.ended, found.stopped) == ("cancelled", "cancelled")
    finally:
        documents.close()
        projects.close()
