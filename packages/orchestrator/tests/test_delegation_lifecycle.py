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
import threading
from pathlib import Path
from typing import Any, Final

import pytest

from sandbox_science.contracts import ErrorKind, ExecutionContext
from sandbox_science.orchestrator.context.agent import AgentContext
from sandbox_science.orchestrator.context.history import body
from sandbox_science.orchestrator.delegation import hook
from sandbox_science.orchestrator.delegation.fence import ChildFence
from sandbox_science.orchestrator.delegation.hook import Child, ChildEnd, Delegations
from sandbox_science.orchestrator.delegation.outcome import Outcome
from sandbox_science.orchestrator.delegation.records import (
    Delegation,
    DelegationRecords,
    Ending,
)
from sandbox_science.orchestrator.documents import USER, DocumentStore, store_path
from sandbox_science.orchestrator.ledger import Ledger
from sandbox_science.orchestrator.loop import Transcript
from sandbox_science.orchestrator.notices import DELEGATIONS
from sandbox_science.orchestrator.projects import ProjectError, ProjectStore, run_name
from sandbox_science.orchestrator.runstate import RunState
from sandbox_science.orchestrator.service import RunService
from sandbox_science.orchestrator.session_plans import PlanBody, SessionPlans
from sandbox_science.testing import FakeClock, eventually

from .loop_harness import PLAN, PROJECT, SESSION, USER_PLANS, Harness, Reply, call
from .loop_harness import gateway as gateway
from .loop_harness import harness as harness
from .loop_harness import worker as worker
from .test_delegation import (
    CHECK,
    REPORT,
    RESULT,
    awaiting_children,
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


@pytest.mark.parametrize("failure", ["save", "acknowledge"])
async def test_child_persistence_failure_never_advertises_a_successful_result(
    delegating: Harness, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    original = ProjectStore.save_run

    def save(store, record):
        if record.parent is not None:
            raise ProjectError(ErrorKind.UNAVAILABLE, "fixture disk unavailable")
        return original(store, record)

    async def acknowledge(outcome, record):
        raise OSError("fixture delivery acknowledgement unavailable")

    if failure == "save":
        monkeypatch.setattr(ProjectStore, "save_run", save)
    else:
        monkeypatch.setattr(Outcome, "acknowledge", acknowledge)
    root = await delegating.run(
        "root",
        [Reply(calls=[CHECK]), wait(), Reply("The child's persistence failed.")],
        delegated=[Reply(REPORT)],
        status=3,
    )
    assert root.stopped == "unfinished_children"
    (delegation_id,) = started(root)
    outcome = delegation(delegating, delegation_id)
    assert (outcome.ended, outcome.stopped) == ("failed", "persistence_error")
    assert outcome.result is None
    assert outcome.report == REPORT
    assert "could not be saved" in outcome.errors[0]
    assert len(children(delegating, root)) == (0 if failure == "save" else 1)


async def test_child_model_failure_reaches_parent_as_actionable_notice(
    delegating: Harness,
) -> None:
    root = await delegating.run(
        "root",
        [Reply(calls=[CHECK]), wait(), Reply("The check needs recovery.")],
        delegated=[Reply(status=400)],
        status=3,
    )
    (delegation_id,) = started(root)
    failed = delegation(delegating, delegation_id)
    assert (failed.ended, failed.stopped) == ("failed", "error")
    assert "the child's model phase failed" in failed.errors
    assert any(
        "the child's model phase failed" in str(message.get("content", ""))
        for messages in delegating.gateway.main
        for message in messages
    )


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
    """A plan the user publishes while the child runs is a draft: the child's outward
    effects are refused, and the outcome says why, while its work in the session goes on
    (D99)."""

    async def plan() -> None:
        delegating.user_plan()

    root = await delegating.run(
        "root",
        [Reply(calls=[CHECK]), wait(), Reply(calls=[call("plan_read")]), Reply("Fenced.")],
        delegated=[
            Reply(calls=[call("write_file", path="check.txt", content="x")], before=plan),
            Reply(calls=[call("publish", path="check.txt")]),
            Reply(json.dumps(FENCED)),
        ],
    )
    (child,) = children(delegating, root)
    written, refused = results(child)
    assert "error" not in json.loads(written), written
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
        body(str(m.get("content", ""))).startswith(DELEGATIONS.text)
        and delegation_id in m["content"]
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


async def test_durable_stop_before_polling_survives_service_interruption(
    delegating: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    free, answer = blocking()
    delegating.gateway.delegated = [answer]
    poll = asyncio.Event()
    tick = RunService.tick

    async def first_tick(service: RunService) -> None:
        await tick(service)
        await poll.wait()  # The service cannot poll the subsequently accepted Stop.

    monkeypatch.setattr(RunService, "tick", first_tick)
    store = ProjectStore(delegating.state)
    state = RunState(store.control)
    state.post(PROJECT, SESSION, "m-1", "Check C-1.", author="user")
    try:
        async with serving(delegating, Reply(calls=[CHECK]), wait()):
            await eventually(lambda: delegating.gateway.children)
            held = state.lease(PROJECT, SESSION)
            assert held is not None
            state.cancel(PROJECT, SESSION, held.run)
        (child,) = kept_children(delegating, held.run)
        assert child.stopped == "cancelled"
        (outcome,) = DelegationRecords(store.control).handbacks(PROJECT, SESSION)
        assert (outcome.ended, outcome.stopped) == ("cancelled", "cancelled")
        assert state.events(PROJECT, SESSION, outcome.run)[-1].data["stopped"] == "cancelled"
        assert state.events(PROJECT, SESSION, held.run)[-1].data["stopped"] == "cancelled"
    finally:
        free.set()
        poll.set()
        store.close()


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
                return found == ["interrupted"]

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
    assert (outcome.ended, outcome.parent) == ("interrupted", held.run)
    stopped = state.events(PROJECT, SESSION, name)[-1]
    assert (stopped.kind, stopped.data["stopped"], stopped.data["record"]) == (
        "stopped",
        "interrupted",
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


async def test_stop_while_the_parent_is_at_its_limit_cancels_the_owned_child(
    delegating: Harness,
) -> None:
    free, answer = blocking()
    delegating.gateway.delegated = [answer]
    store = ProjectStore(delegating.state)
    state = RunState(store.control)
    state.post(PROJECT, SESSION, "m-1", "Check C-1.", author="user")
    try:
        async with serving(delegating, Reply(calls=[CHECK]), Reply("Done."), max_turns=2):
            run = await awaiting_children(delegating)
            assert len(delegating.gateway.main) == 2
            state.cancel(PROJECT, SESSION, run)
            await eventually(lambda: state.ended(PROJECT, SESSION, run), timeout=20)
        root = store.run(PROJECT, SESSION, run)
        (child,) = children(delegating, root)
        assert (root.stopped, child.stopped) == ("cancelled", "cancelled")
        assert child.ended_at <= root.ended_at
        assert len(delegating.gateway.main) == 2
    finally:
        free.set()
        store.close()


async def test_a_project_that_stopped_delegating_still_gets_its_outcomes(
    delegating: Harness,
) -> None:
    free, answer = blocking()
    task = asyncio.create_task(
        delegating.run(
            "root",
            [Reply(calls=[CHECK]), Reply("Done.")],
            delegated=[answer],
            max_turns=2,
            status=3,
        )
    )
    try:
        await awaiting_children(delegating)
    finally:
        free.set()
    root = await task
    depth(delegating, 0)
    later = await delegating.run("later", [Reply("Noted.")])
    (delegation_id,) = started(root)
    assert later.delivered["delegations"] == [delegation_id]


async def test_a_reached_cost_limit_refuses_a_delegation(delegating: Harness) -> None:
    RunState(ProjectStore(delegating.state).control).set_cost_limit(PROJECT, 0.01)
    root = await delegating.run("root", [Reply(calls=[CHECK], cost="0.02")], status=3)
    refused = json.loads(results(root)[0])["error"]
    assert refused["kind"] == "unavailable"
    assert "daily cost limit" in refused["message"]
    assert "delegations" not in root.summaries
    assert root.stopped == "gateway_budget"
    assert len(delegating.gateway.main) == 1


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


async def test_child_stop_commits_before_parent_interruption_keeps_siblings_separate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    committed, ready = asyncio.Event(), asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()
    cancel = RunState.cancel
    entered: list[str] = []
    ids: list[str] = []

    def committed_stop(state: RunState, project: str, session: str, run: str) -> None:
        cancel(state, project, session, run)
        loop.call_soon_threadsafe(committed.set)
        assert release.wait(10)

    async def cooperative(child: Child, stop: asyncio.Event) -> ChildEnd:
        entered.append(child.delegation_id)
        if len(entered) == 2:
            ready.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            return ChildEnd("cancelled", False, None)
        raise AssertionError

    made, records, projects, documents = _delegations(tmp_path, cooperative)
    monkeypatch.setattr(RunState, "cancel", committed_stop)

    async def parent() -> None:
        async with made.lease():
            for _ in range(2):
                text, _ = await made.call("delegate", BRIEF)
                ids.append(json.loads(text)["delegation_id"])
            await ready.wait()
            await made.call("cancel_delegation", {"delegation_id": ids[0]})

    task = asyncio.create_task(parent())
    try:
        await committed.wait()
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        outcomes = [records.get(PROJECT, SESSION, identity) for identity in ids]
        assert [(item.ended, item.stopped) for item in outcomes] == [
            ("cancelled", "cancelled"),
            ("interrupted", "interrupted"),
        ]
    finally:
        release.set()
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        documents.close()
        projects.close()


@pytest.mark.parametrize(("explicit", "durable"), [(False, False), (True, False), (False, True)])
async def test_a_child_that_outlives_its_cancel_is_interrupted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, explicit: bool, durable: bool
) -> None:
    monkeypatch.setattr(hook, "STOP_SECONDS", 0.05)
    never = asyncio.Event()

    async def stubborn(child: Child, stop: asyncio.Event) -> ChildEnd:
        while not never.is_set():
            with contextlib.suppress(asyncio.CancelledError):  # it ignores its cancel
                await never.wait()
        return ChildEnd("cancelled", False, None)

    made, records, projects, documents = _delegations(tmp_path, stubborn)
    made.stop = asyncio.Event()
    try:
        async with made.lease():
            text, _ = await made.call("delegate", BRIEF)
            await asyncio.sleep(0.01)  # it starts
            if explicit:
                made.stop.set()
            if durable:
                RunState(projects.control).cancel(PROJECT, SESSION, made.parent)
        delegation_id = json.loads(text)["delegation_id"]
        found = records.get(PROJECT, SESSION, delegation_id)
        assert (found.ended, found.stopped) == (
            "interrupted",
            "cancelled" if explicit or durable else "interrupted",
        )
    finally:
        never.set()
        for child in made.children.values():
            child.task.cancel()
        documents.close()
        projects.close()


@pytest.mark.parametrize("failure", [False, True])
async def test_stop_ancestry_read_keeps_cleanup_owned_on_cancel_or_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: bool
) -> None:
    reading, entered, cleaned = asyncio.Event(), asyncio.Event(), asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()
    read = RunState.cancelled_runs
    ids: list[str] = []

    def blocked_read(state: RunState, project: str, session: str, runs: list[str]) -> set[str]:
        result = read(state, project, session, runs)
        loop.call_soon_threadsafe(reading.set)
        assert release.wait(10)
        if failure:
            raise OSError("fixture Stop ancestry read failed")
        return result

    async def cooperative(child: Child, stop: asyncio.Event) -> ChildEnd:
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cleaned.set()
            return ChildEnd("cancelled", False, None)
        raise AssertionError

    made, records, projects, documents = _delegations(tmp_path, cooperative)
    monkeypatch.setattr(RunState, "cancelled_runs", blocked_read)

    async def parent() -> None:
        async with made.lease():
            text, _ = await made.call("delegate", BRIEF)
            ids.append(json.loads(text)["delegation_id"])
            await asyncio.Event().wait()

    task = asyncio.create_task(parent())
    try:
        await entered.wait()
        RunState(projects.control).cancel(PROJECT, SESSION, made.parent)
        task.cancel()
        await reading.wait()
        task.cancel()  # A second cancellation must not discard the read or skip cleanup.
        release.set()
        with pytest.raises(OSError if failure else asyncio.CancelledError):
            await task
        assert cleaned.is_set()
        found = records.get(PROJECT, SESSION, ids[0])
        reason = "interrupted" if failure else "cancelled"
        assert (found.ended, found.stopped) == (reason, reason)
    finally:
        release.set()
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, OSError):
            await task
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


async def test_parent_cannot_finish_when_child_outcome_was_not_saved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    writes: list[object] = []

    async def quick(child: Child, stop: asyncio.Event) -> ChildEnd:
        return ChildEnd("answered", False, REPORT)

    def failed(*args: Any) -> None:
        writes.append(args)
        raise OSError("fixture storage unavailable")

    made, records, projects, documents = _delegations(tmp_path, quick)
    clock = FakeClock()
    made.clock = clock
    monkeypatch.setattr(records, "end", failed)
    try:
        async with made.lease():
            text, _ = await made.call("delegate", BRIEF)
            closing = asyncio.create_task(
                made.closing(Transcript(context=AgentContext(messages=[])), "Done.")
            )
            for count, seconds in enumerate((0.5, 1.0), start=1):
                await eventually(lambda count=count: len(writes) == count and clock.sleepers == 1)
                clock.advance(seconds)
            result = await closing
            assert (result.action, result.stopped) == ("stop", "child_outcome_unavailable")
            assert records.get(PROJECT, SESSION, json.loads(text)["delegation_id"]).ended is None
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
            transcript = Transcript(context=AgentContext(messages=[]), halted="budget")
            transcript.room = False
            closing = await made.closing(transcript, "An answer.")
            assert (closing.action, closing.stopped) == ("retry", "budget")
            transcript.halted = None
            assert (await made.closing(transcript, "An answer.")).stopped == "context_limit"
    finally:
        documents.close()
        projects.close()


@pytest.mark.parametrize("wake_by", ["children", "user", "stop"])
async def test_parent_closing_waits_for_all_children_and_remains_interruptible(
    tmp_path: Path, wake_by: str
) -> None:
    free = [asyncio.Event(), asyncio.Event()]
    entered, user = asyncio.Event(), asyncio.Event()
    started: list[Child] = []
    transitions: list[object] = []

    async def runner(child: Child, stop: asyncio.Event) -> ChildEnd:
        index = len(started)
        started.append(child)
        await free[index].wait()
        return ChildEnd("answered", False, "Arithmetic done.")

    async def waiting(ids) -> None:
        transitions.append(ids)
        if ids:
            entered.set()

    async def wake() -> None:
        await user.wait()

    made, records, projects, documents = _delegations(tmp_path, runner)
    made.waiting, made.wake = waiting, wake
    try:
        async with made.lease():
            for _ in free:
                await made.call("delegate", BRIEF)
            closing = asyncio.create_task(
                made.closing(Transcript(context=AgentContext(messages=[])), "An answer.")
            )
            await entered.wait()
            await eventually(lambda: len(started) == 2)
            assert not closing.done()
            if wake_by == "children":
                free[0].set()
                await eventually(
                    lambda: records.get(PROJECT, SESSION, started[0].delegation_id).ended
                )
                assert not closing.done()
                free[1].set()
                result = await closing
                assert result.action == "continue"
                assert len(result.notices[0].ids) == 2
            elif wake_by == "user":
                user.set()
                assert (await closing).action == "retry"
            else:
                closing.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await closing
            assert transitions[-1] is None
            if wake_by != "children":
                assert all(
                    records.get(PROJECT, SESSION, c.delegation_id).ended is None for c in started
                )
        assert all(
            records.get(PROJECT, SESSION, c.delegation_id).ended is not None for c in started
        )
    finally:
        for event in free:
            event.set()
        documents.close()
        projects.close()


@pytest.mark.parametrize("durable", [False, True])
async def test_parent_cleanup_before_a_child_starts_keeps_its_reason(
    tmp_path: Path, durable: bool
) -> None:
    async def unused(child: Child, stop: asyncio.Event) -> ChildEnd:
        raise AssertionError

    made, records, projects, documents = _delegations(tmp_path, unused)
    try:
        async with made.lease():
            text, _ = await made.call("delegate", BRIEF)  # its parent ends at once
            if durable:
                RunState(projects.control).cancel(PROJECT, SESSION, made.parent)
        found = records.get(PROJECT, SESSION, json.loads(text)["delegation_id"])
        reason = "cancelled" if durable else "interrupted"
        assert (found.ended, found.stopped) == (reason, reason)
    finally:
        documents.close()
        projects.close()
