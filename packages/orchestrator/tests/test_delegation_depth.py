# SPDX-License-Identifier: MIT
"""Two levels of delegation (plans-and-delegation spec, PD-9, PD-15, PD-32, PD-35,
PD-37 to PD-40): a main run's task it allows may delegate tasks once more, never checks
and on no track, with tokens carved from what it has left; the second level delegates
nothing; handbacks go to the direct parent, and no run takes another's; the root holds
the session for its whole tree and its kernel check spares it; the cap counts the whole
tree, even for delegations started at once; cancels, lost leases and turn limits go down
through every level, each within its parent's time; and the end of a dead tree goes
down through every level."""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest

from sandbox_science.contracts import ErrorKind, ExecutionContext
from sandbox_science.orchestrator.continuity.records import WorkRecords
from sandbox_science.orchestrator.control import ControlError
from sandbox_science.orchestrator.delegation import hook
from sandbox_science.orchestrator.delegation.hook import (
    Child,
    ChildEnd,
    ChildRunner,
    Deadline,
    Delegations,
)
from sandbox_science.orchestrator.delegation.records import (
    Delegation,
    DelegationRecords,
    DelegationSettings,
    Ending,
)
from sandbox_science.orchestrator.documents import DocumentStore, store_path
from sandbox_science.orchestrator.ledger import Ledger
from sandbox_science.orchestrator.loop import Transcript
from sandbox_science.orchestrator.projects import (
    ModelUsage,
    ProjectStore,
    RunRecord,
    run_id_of,
    run_name,
)
from sandbox_science.orchestrator.run_inputs import RunInputs
from sandbox_science.orchestrator.runstate import RunState
from sandbox_science.orchestrator.worker_client import WorkerAPIError
from sandbox_science.testing import eventually

from .loop_harness import PROJECT, SESSION, Harness, Reply, call
from .loop_harness import gateway as gateway
from .loop_harness import harness as harness
from .loop_harness import worker as worker
from .test_delegation import (
    CHECK,
    about,
    awaiting_children,
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
from .test_run_service import serving

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
    # Its tokens were carved from what its parent had left.
    given = [cast(dict[str, int], delegation(delegating, i).brief["limits"]) for i in (lead, part)]
    assert given[1]["tokens"] < given[0]["tokens"]
    briefs = [str(m[1]["content"]) for m in delegating.gateway.children]
    assert any("You may delegate tasks of your own" in b for b in briefs)
    store = ProjectStore(delegating.state)
    try:
        assert DelegationRecords(store.control).handbacks(PROJECT, SESSION) == []
        for record, may_delegate in ((root, False), (child, True), (grandchild, False)):
            inputs = RunInputs(store.control).get(PROJECT, SESSION, name(record))
            assert inputs.request["parent"] == record.parent
            assert inputs.request["delegation"] == record.delegation
            assert inputs.request["may_delegate"] is may_delegate
            assert inputs.pins == record.prompt_provenance
            assert "history" not in inputs.request
            assert "stop_by" not in inputs.request
    finally:
        store.close()


async def test_the_second_level_delegates_nothing_and_no_level_delegates_checks(
    delegating: Harness,
) -> None:
    """The lead may not let its own child delegate, nor give it a track: a track is its
    root's to grant, and the lead's is its own."""
    configure(delegating, depth=2)
    deeper = call("delegate", task="Go deeper.", delegate=True)
    tracked = call("delegate", task="Take a track.", track="analysis")
    root = await delegating.run(
        "root",
        [Reply(calls=[LEAD]), wait(), Reply("Done.")],
        routed={
            "Lead the work.": [
                Reply(calls=[CHECK, deeper, tracked, PART]),
                wait(),
                Reply(json.dumps(DONE)),
            ],
            "Do a part.": [Reply(calls=[PART]), Reply(json.dumps(DONE))],
        },
    )
    (child,) = children(delegating, root)
    refused = json.loads(results(child)[0])["error"]
    assert refused["kind"] == "policy_denied"
    assert refused["message"] == (
        "not_permitted_for_role: a delegated run delegates tasks, never checks"
    )
    deeper_refused = json.loads(results(child)[1])["error"]
    assert (deeper_refused["kind"], deeper_refused["message"].split(":")[0]) == (
        "policy_denied",
        "not_permitted_for_role",
    )
    no_track = json.loads(results(child)[2])["error"]
    assert no_track["message"] == "this run's tasks take no track"
    assert len(started(child)) == 1
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


def made(n: int, parent: str, run: str, **brief: Any) -> Delegation:
    return Delegation.model_validate(
        {
            "delegation_id": f"dlg-{n:024x}",
            "parent": parent,
            "run": run,
            "kind": "task",
            "brief": {"task": "Go.", **brief},
            "started_at": "2026-10-15T12:00:01+00:00",
        }
    )


ROOT = "20261015T120000000000Z-run-root"
CHILD = "20261015T120001000000Z-run-child"
GRANDCHILD = "20261015T120002000000Z-run-grandchild"
SIBLING = "20261015T120003000000Z-run-sibling"


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
        records.start(PROJECT, SESSION, made(3, ROOT, SIBLING))
        records.end(PROJECT, SESSION, made(3, ROOT, "x").delegation_id, Ending("failed", "error"))
        assert len(records.handbacks(PROJECT, SESSION)) == 2
    finally:
        store.close()


# --- Bounds checked as a delegation is written (PD-9, PD-38) ------------------------------


@dataclass
class Tree:
    records: DelegationRecords
    ledger: Ledger
    context: ExecutionContext

    def hook(
        self,
        runner: ChildRunner,
        *,
        parent: str = ROOT,
        depth: int = 0,
        tokens: int | None = None,
        stop_by: Deadline | None = None,
        **limits: Any,
    ) -> Delegations:
        return Delegations(
            self.records,
            self.ledger,
            self.context,
            parent=parent,
            runner=runner,
            max_turns=30,
            offer=True,
            limits=DelegationSettings.model_validate({"depth": 2, **limits}),
            depth=depth,
            tokens=tokens,
            stop_by=stop_by,
        )


@contextlib.contextmanager
def tree(tmp_path: Path) -> Iterator[Tree]:
    projects = ProjectStore(tmp_path)
    documents = DocumentStore(store_path(tmp_path))
    try:
        projects.create(PROJECT, "minimal", "sha256:" + "a" * 64)
        projects.open_session(PROJECT, SESSION, dedicated=False)
        context = ExecutionContext(project_id=PROJECT, session_id=SESSION, environment_id="e")
        yield Tree(DelegationRecords(projects.control), Ledger(documents), context)
    finally:
        documents.close()
        projects.close()


async def unused(child: Child, stop: asyncio.Event) -> ChildEnd:
    raise AssertionError


async def test_delegations_started_at_once_keep_the_cap(tmp_path: Path) -> None:
    """The cap is counted as the delegation is written: three started together, with
    room for two, start two."""
    free = asyncio.Event()

    async def held(child: Child, stop: asyncio.Event) -> ChildEnd:
        await free.wait()
        return ChildEnd("answered", False, json.dumps(DONE))

    with tree(tmp_path) as found:
        made = found.hook(held, cap=2)
        try:
            async with made.lease():
                outcomes = await asyncio.gather(
                    *(made.call("delegate", {"task": f"Part {n}."}) for n in range(3)),
                    return_exceptions=True,
                )
                refused = [o for o in outcomes if isinstance(o, BaseException)]
                assert len(refused) == 1
                assert isinstance(refused[0], WorkerAPIError)
                assert refused[0].kind == ErrorKind.UNAVAILABLE
                assert refused[0].message.startswith("limit_reached: 2 delegated runs")
                assert len(found.records.running(PROJECT, SESSION)) == 2
        finally:
            free.set()


async def test_nested_grant_refreshes_fold_usage_after_awaited_admission(tmp_path: Path) -> None:
    extra = 5000
    dispatched: list[int] = []

    async def auxiliary() -> int:
        return extra

    async def held(child: Child, stop: asyncio.Event) -> ChildEnd:
        dispatched.append(child.tokens)
        return ChildEnd("answered", False, json.dumps(DONE))

    async def gate() -> bool:
        nonlocal extra
        # A fold response plus its retry hold arrive during an earlier tool or
        # model-choice/admission await, without another foreground model return.
        extra += 40000
        return False

    with tree(tmp_path) as found:
        lead = found.hook(held, parent=CHILD, depth=1, tokens=100000)
        lead.auxiliary_tokens = auxiliary
        lead.over_budget = gate
        async with lead.lease():
            await lead.model_returned(
                Transcript(messages=[], usage=[ModelUsage(input_tokens=20000, output_tokens=0)])
            )
            text, _ = await lead.call("delegate", {"task": "Do a part."})
            assert json.loads(text)["limits"]["tokens"] == 35000
            with pytest.raises(WorkerAPIError, match="no tokens left"):
                await lead.call("delegate", {"task": "Another part."})
            await eventually(lambda: dispatched == [35000])


def test_a_track_is_busy_while_any_delegation_of_the_session_runs_on_it(tmp_path: Path) -> None:
    with tree(tmp_path) as found:
        records = found.records
        first = made(1, ROOT, CHILD, track={"track_id": "analysis"})
        records.start(PROJECT, SESSION, first)
        with pytest.raises(ControlError) as refused:
            records.start(
                PROJECT, SESSION, made(2, CHILD, GRANDCHILD, track={"track_id": "analysis"})
            )
        assert (refused.value.kind, refused.value.message.split(":")[0]) == (
            "conflict",
            "track_busy",
        )
        assert first.delegation_id in refused.value.message
        records.start(PROJECT, SESSION, made(3, ROOT, SIBLING, track={"track_id": "writing"}))
        records.end(PROJECT, SESSION, first.delegation_id, Ending("returned", "answered"))
        records.start(PROJECT, SESSION, made(2, ROOT, GRANDCHILD, track={"track_id": "analysis"}))
        assert records.running(PROJECT, SESSION) == [GRANDCHILD, SIBLING]


async def test_a_delegated_runs_children_get_at_most_the_tokens_it_has_left(
    tmp_path: Path,
) -> None:
    free = asyncio.Event()

    async def held(child: Child, stop: asyncio.Event) -> ChildEnd:
        await free.wait()
        return ChildEnd("answered", False, json.dumps(DONE))

    with tree(tmp_path) as found:
        lead = found.hook(held, parent=CHILD, depth=1, tokens=80_000)
        try:
            async with lead.lease():
                text, _ = await lead.call("delegate", {"task": "Do a part."})
                assert json.loads(text)["limits"]["tokens"] == 80_000
                used = ModelUsage(input_tokens=30_000, output_tokens=1_000)
                await lead.model_returned(Transcript(messages=[], usage=[used]))
                text, _ = await lead.call("delegate", {"task": "Do another."})
                assert json.loads(text)["limits"]["tokens"] == 49_000
                spent = ModelUsage(input_tokens=79_000, output_tokens=1_000)
                await lead.model_returned(Transcript(messages=[], usage=[spent]))
                with pytest.raises(WorkerAPIError) as refused:
                    await lead.call("delegate", {"task": "One more."})
                assert refused.value.message == (
                    "limit_reached: this run has no tokens left to give a delegated run"
                )
        finally:
            free.set()


# --- Each run's own outcomes (PD-35) -------------------------------------------------------


async def test_no_run_reads_or_takes_another_parents_outcome(tmp_path: Path) -> None:
    """The lead reads only its own children; the root may read the whole tree, but a
    grandchild's outcome it read stays its parent's to receive."""
    with tree(tmp_path) as found:
        records = found.records
        records.start(PROJECT, SESSION, made(1, ROOT, CHILD, delegate=True))
        sibling, part = made(2, ROOT, SIBLING), made(3, CHILD, GRANDCHILD)
        for delegation_ in (sibling, part):
            records.start(PROJECT, SESSION, delegation_)
            records.end(PROJECT, SESSION, delegation_.delegation_id, Ending("returned", "answered"))
        lead = found.hook(unused, parent=CHILD, depth=1)
        for name_, args in (
            ("delegation_status", {"delegation_id": sibling.delegation_id}),
            ("wait_delegations", {"delegation_ids": [sibling.delegation_id]}),
        ):
            with pytest.raises(WorkerAPIError) as refused:
                await lead.call(name_, args)
            assert refused.value.kind == ErrorKind.NOT_FOUND
        await lead.call("delegation_status", {"delegation_id": part.delegation_id})

        root = found.hook(unused)
        for read in (sibling, part):
            await root.call("delegation_status", {"delegation_id": read.delegation_id})
        await root.model_returned(Transcript(messages=[]))
        now = datetime.now(UTC)
        saved = RunRecord.model_validate(
            {
                "run_id": "run-root",
                "context": found.context.model_dump(mode="json"),
                "task": "Lead.",
                "started_at": now,
                "ended_at": now,
                "stopped": "answered",
                "final_text": "Done.",
                "messages": [],
                "tools": [],
                "summaries": {"delegations": root.summary()},
            }
        )
        await root.acknowledge(saved)
        assert records.get(PROJECT, SESSION, sibling.delegation_id).acknowledged == ROOT
        assert records.get(PROJECT, SESSION, part.delegation_id).acknowledged is None
        assert [d.run for d in records.handbacks(PROJECT, SESSION, parent=CHILD)] == [GRANDCHILD]
        # Once the lead ends, its notice names the outcome it never received.
        lead_id = made(1, ROOT, CHILD).delegation_id
        records.end(PROJECT, SESSION, lead_id, Ending("limit_reached", "turn_limit"))
        (notice,) = await root.notices("turn", Transcript(messages=[]))
        (shown,) = cast(list[dict[str, Any]], notice.payload)
        assert (shown["delegation_id"], shown["unreceived"]) == (lead_id, [part.delegation_id])
        # Nor does a delegated run take one of the root's.
        records.start(PROJECT, SESSION, made(4, ROOT, "20261015T120004000000Z-run-later"))
        later = made(4, ROOT, "x").delegation_id
        records.end(PROJECT, SESSION, later, Ending("returned", "answered"))
        records.acknowledge(PROJECT, SESSION, [later], CHILD, parent=CHILD)
        assert records.get(PROJECT, SESSION, later).acknowledged is None


# --- Stopping a tree (PD-37, PD-39) ---------------------------------------------------------


async def test_each_level_stops_its_children_within_its_parents_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A grandchild ignores its cancel: its parent records it ``interrupted`` within the
    time the root gave it, so the root records its child's own outcome. While they run,
    the root's kernel check spares the whole tree."""
    monkeypatch.setattr(hook, "STOP_SECONDS", 2.0)
    never, below = asyncio.Event(), asyncio.Event()

    async def stubborn(child: Child, stop: asyncio.Event) -> ChildEnd:
        below.set()
        while not never.is_set():
            with contextlib.suppress(asyncio.CancelledError):  # it ignores its cancel
                await never.wait()
        return ChildEnd("cancelled", False, None)

    with tree(tmp_path) as found:

        async def lead(child: Child, stop: asyncio.Event) -> ChildEnd:
            own = found.hook(
                stubborn,
                parent=run_name(child.started_at, child.run_id),
                depth=1,
                stop_by=child.deadline,
            )
            try:
                async with own.lease():
                    await own.call("delegate", {"task": "Do a part."})
                    await never.wait()  # until its parent cancels it
            except asyncio.CancelledError:
                if (current := asyncio.current_task()) is not None:
                    current.uncancel()
                return ChildEnd("cancelled", False, None)
            raise AssertionError

        root = found.hook(lead)
        loop = asyncio.get_running_loop()
        try:
            async with root.lease():
                text, _ = await root.call("delegate", {"task": "Lead.", "delegate": True})
                await asyncio.wait_for(below.wait(), 10)
                running = found.records.running(PROJECT, SESSION)
                assert len(running) == 2
                assert {run_id_of(run) for run in running} == await root.live()
                began = loop.time()
            took = loop.time() - began
        finally:
            never.set()
        records = found.records
        leading = records.get(PROJECT, SESSION, json.loads(text)["delegation_id"])
        (part,) = records.handbacks(PROJECT, SESSION, parent=leading.run)
        assert (leading.ended, part.ended) == ("interrupted", "interrupted")
        assert part.ended_at is not None
        assert leading.ended_at is not None
        assert part.ended_at <= leading.ended_at
        assert took < 2.0


def _working() -> tuple[asyncio.Event, asyncio.Event, Reply]:
    """Events for a grandchild in the middle of its model call, and its answer."""
    working, free = asyncio.Event(), asyncio.Event()

    async def held() -> None:
        working.set()
        await free.wait()

    return working, free, Reply(json.dumps(DONE), before=held)


def _below(harness: Harness, parent: str) -> list[RunRecord]:
    store = ProjectStore(harness.state)
    try:
        return [
            store.run(PROJECT, SESSION, entry.name)
            for entry in store.runs(PROJECT, SESSION)
            if entry.parent == parent
        ]
    finally:
        store.close()


async def test_a_leads_turn_limit_keeps_its_child_and_hands_the_unconsumed_outcome_up(
    delegating: Harness,
) -> None:
    """A limited lead waits for its child, whose unconsumed result accompanies its own."""
    configure(delegating, depth=2, task_turns=2)
    working, free, part_answer = _working()

    async def after_it_started() -> None:
        await working.wait()

    task = asyncio.create_task(
        delegating.run(
            "root",
            [Reply(calls=[LEAD]), wait(), Reply("Done.")],
            status=3,
            routed={
                "Lead the work.": [
                    Reply(calls=[PART]),
                    Reply("Finished.", before=after_it_started),
                ],
                "Do a part.": [part_answer],
            },
        )
    )
    try:
        await awaiting_children(delegating, delegated=True)
        assert not task.done()
        assert len(delegating.gateway.children) == 3
    finally:
        free.set()
    root = await task
    (child,) = children(delegating, root)
    (grandchild,) = children(delegating, child)
    assert (child.stopped, grandchild.stopped) == ("turn_limit", "answered")
    assert root.stopped == "unfinished_children"
    with contextlib.closing(ProjectStore(delegating.state)) as projects:
        works = WorkRecords(projects.control)
        assert (
            works.get(PROJECT, SESSION, run_name(child.started_at, child.run_id)).state == "paused"
        )
        assert (
            works.get(PROJECT, SESSION, run_name(grandchild.started_at, grandchild.run_id)).state
            == "completed"
        )
    assert grandchild.ended_at <= child.ended_at
    (part,) = started(child)
    assert (delegation(delegating, part).ended, delegation(delegating, part).acknowledged) == (
        "returned",
        None,
    )
    (waited,) = answer(root, 1)["delegations"]
    assert waited["ended"] == "limit_reached"
    (left,) = waited["unreceived"]
    assert (left["delegation_id"], left["ended"]) == (part, "returned")


async def test_the_users_cancel_of_the_root_stops_its_grandchild_mid_call_first(
    delegating: Harness,
) -> None:
    configure(delegating, depth=2)
    working, free, part_answer = _working()
    delegating.gateway.routed = {
        "Lead the work.": [Reply(calls=[PART]), wait()],
        "Do a part.": [part_answer],
    }
    store = ProjectStore(delegating.state)
    state = RunState(store.control)
    state.post(PROJECT, SESSION, "m-1", "Lead the work.", author="user")
    try:
        async with serving(delegating, Reply(calls=[LEAD]), wait()):
            await asyncio.wait_for(working.wait(), 20)
            await eventually(lambda: not delegating.gateway.routed["Lead the work."], timeout=20)
            held = state.lease(PROJECT, SESSION)
            assert held is not None
            state.cancel(PROJECT, SESSION, held.run)
            await eventually(lambda: state.ended(PROJECT, SESSION, held.run), timeout=20)
        root = store.run(PROJECT, SESSION, held.run)
    finally:
        free.set()
        store.close()
    (child,) = children(delegating, root)
    (grandchild,) = children(delegating, child)
    assert (root.stopped, child.stopped, grandchild.stopped) == ("cancelled",) * 3
    assert grandchild.ended_at <= child.ended_at <= root.ended_at
    (lead,), (part,) = started(root), started(child)
    assert [delegation(delegating, i).ended for i in (lead, part)] == ["cancelled"] * 2


async def test_a_lost_lease_stops_the_tree_with_its_grandchild_mid_call(
    delegating: Harness,
) -> None:
    configure(delegating, depth=2)
    working, free, part_answer = _working()
    delegating.gateway.routed = {
        "Lead the work.": [Reply(calls=[PART]), wait()],
        "Do a part.": [part_answer],
    }
    store = ProjectStore(delegating.state)
    state = RunState(store.control)
    records = DelegationRecords(store.control)
    state.post(PROJECT, SESSION, "m-1", "Lead the work.", author="user")
    try:
        async with serving(delegating, Reply(calls=[LEAD]), wait(), lease_seconds=6.0):
            await asyncio.wait_for(working.wait(), 20)
            await eventually(lambda: not delegating.gateway.routed["Lead the work."], timeout=20)
            held = state.lease(PROJECT, SESSION)
            assert held is not None
            with state.control.write() as tx:  # another holder takes the session
                tx.sql.execute("DELETE FROM run_leases")
            state.acquire(PROJECT, SESSION, "another", "thief")
            await eventually(lambda: len(_below(delegating, held.run)) == 1, timeout=20)
        (child,) = _below(delegating, held.run)
        (grandchild,) = _below(delegating, name(child))
    finally:
        free.set()
    try:
        assert (child.stopped, grandchild.stopped) == ("interrupted", "interrupted")
        assert grandchild.ended_at <= child.ended_at
        assert records.running(PROJECT, SESSION) == []
        ended = [d.ended for d in records.listed(PROJECT, SESSION)[0]]
        assert ended == ["interrupted", "interrupted"]
    finally:
        store.close()
