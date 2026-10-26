# SPDX-License-Identifier: MIT
"""The parent reviews a task's result before the task closes (plans-and-delegation spec,
PD-46; child environments, CE-4 and CE-F3 as amended on 2026-10-26).

End to end, ``cli.main`` runs a root against the in-process worker and the scripted
gateway, as ``test_delegation`` does: PD-F14 in the session's environment, its kernel
holding a variable across the review, and in environments of their own, with
``test_delegation_environments``'s fake of the worker's child environment routes. The
rest are the records' and the hook's rules: the review's states, refusals, the closing
paths (``review_max``, the session's close, a stopped run's leftovers), the race between
an acceptance and a reopening message, and a crash during a reopening.
"""

from __future__ import annotations

import asyncio
import json
import re
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from sandbox_science.contracts import ExecutionContext
from sandbox_science.orchestrator.agent_messages import AgentMailbox, AgentMessageStore
from sandbox_science.orchestrator.control import ControlError
from sandbox_science.orchestrator.delegation.environments import ChildPlacement, reconcile
from sandbox_science.orchestrator.delegation.hook import Child, ChildEnd
from sandbox_science.orchestrator.delegation.records import (
    Delegation,
    DelegationRecords,
    Ending,
    ReopenPlan,
    notice_key,
    reopen_in,
)
from sandbox_science.orchestrator.delegation.review import Closer
from sandbox_science.orchestrator.hooks import Refusal, RunView
from sandbox_science.orchestrator.projects import ProjectStore, run_name
from sandbox_science.orchestrator.runstate import RunState
from sandbox_science.orchestrator.runtree import RunTrees
from sandbox_science.orchestrator.worker_client import WorkerAPIError
from sandbox_science.testing import FakeClock

from .control_versions import older_store
from .loop_harness import PROJECT, SESSION, Harness, Reply, call, own
from .loop_harness import gateway as gateway
from .loop_harness import harness as harness
from .test_delegation import children, delegation, results, started
from .test_delegation import delegating as delegating
from .test_delegation_environments import ChildService
from .test_delegation_environments import owning as owning
from .test_delegation_environments import service as service
from .test_delegation_environments import worker as worker
from .test_delegation_lifecycle import _delegations
from .test_delegation_tasks import DONE

pytestmark = pytest.mark.slow

Message = dict[str, Any]
_ID = re.compile(r"dlg-[0-9a-f]{24}")
FIRST = "Build a reduced lattice basis for the 128-bit parameters."
SECOND = "Tabulate a^16 mod 17."
FEEDBACK = "Also check the 256-bit parameters."
PARTIAL: dict[str, Any] = {
    **DONE,
    "status": "partial",
    "summary": "The 128-bit basis is built and kept in the kernel as basis.",
    "open_questions": ["The 256-bit parameters are not checked."],
}
COMPLETE: dict[str, Any] = {
    **DONE,
    "summary": "Checked the 256-bit parameters against the basis built before.",
}


def _ids(messages: list[Message]) -> list[str]:
    """The delegations the run's tool results named, in the order they started."""
    found = [
        found
        for message in messages
        if message["role"] == "tool"
        for found in _ID.findall(str(message["content"]))
    ]
    return list(dict.fromkeys(found))


def _on_first(name: str, **arguments: Any) -> Reply:
    """A call of ``name`` on the first delegation the run started."""
    return Reply(
        calls_from=lambda messages: [call(name, delegation_id=_ids(messages)[0], **arguments)]
    )


def _wait(count: int | None = None) -> Reply:
    """A wait on the first ``count`` delegations the run started, or all."""

    def calls(messages: list[Message]) -> list[tuple[str, dict[str, Any]]]:
        return [call("wait_delegations", delegation_ids=_ids(messages)[:count])]

    return Reply(calls_from=calls)


# --- PD-F14 end to end -------------------------------------------------------------


async def test_review_before_acceptance_end_to_end(delegating: Harness) -> None:
    """PD-F14: the first task returns partial after building a value in its kernel; the
    parent's feedback reopens it, it reads the value still in its kernel and returns
    complete, and the parent accepts it; a later message to it goes back to the root as
    a delivery failure; the second task, never reviewed, is accepted as the parent's run
    ends."""
    root = await delegating.run(
        "root",
        [
            Reply(calls=[call("delegate", task=FIRST), call("delegate", task=SECOND)]),
            _wait(),
            _on_first("message_child", text=FEEDBACK),
            _wait(1),
            _on_first("accept_delegation", note="The 256-bit check settles it."),
            _on_first("message_child", text="One more question."),
            Reply("Both tasks are done."),
        ],
        routed={
            FIRST: [
                Reply(calls=[call("python", code="basis = [[3, 1], [1, 2]]")]),
                Reply(json.dumps(PARTIAL)),
                Reply(calls=[call("python", code="print('kept', sum(map(sum, basis)))")]),
                Reply(json.dumps(COMPLETE)),
            ],
            SECOND: [Reply(json.dumps(DONE))],
        },
    )
    first, second = started(root)
    found = delegation(delegating, first)
    # Two results kept, partial then complete, the latest the delegation's; accepted.
    assert (found.ended, found.review, found.results) == ("returned", "accepted", 2)
    assert (found.review_reason, found.review_note) == ("parent", "The 256-bit check settles it.")
    store = ProjectStore(delegating.state)
    try:
        kept = DelegationRecords(store.control).results(PROJECT, SESSION, first)
    finally:
        store.close()
    assert [r.number for r in kept] == [1, 2]
    assert [(r.result or {}).get("status") for r in kept] == ["partial", "complete"]
    assert kept[0].run != kept[1].run == found.run
    # The kernel held the value across the review: the reopened run read it.
    texts = [json.dumps(own(r)) for r in children(delegating, root)]
    assert any("kept 7" in text for text in texts), texts
    # The reopened run began with the platform's word and the parent's message.
    later = [r for r in children(delegating, root) if r.delegation == first]
    assert len(later) == 2
    continued = max(later, key=lambda r: r.started_at)
    seen = json.dumps(own(continued))
    assert "reopened this task" in seen
    assert FEEDBACK in seen
    assert continued.delivered["agent_messages"]
    # The message reopened it; the later one went back to the root as a failure.
    sent = [json.loads(r) for r in results(root) if '"message_id"' in r]
    assert sent[0]["reopened"]["delegation_id"] == first
    assert sent[0]["reopened"]["result"] == 2
    assert sent[1]["state"] == "recipient_ended"
    assert root.delivered["agent_messages"] == [sent[1]["message_id"]]
    # The second, never reviewed, was accepted as the parent's run ended.
    other = delegation(delegating, second)
    assert (other.review, other.review_reason, other.reviewed_by) == (
        "accepted",
        "parent_ended",
        "platform",
    )
    # Its acceptance reached the user as a notice of the root's run.
    store = ProjectStore(delegating.state)
    try:
        events = RunState(store.control).events(
            PROJECT, SESSION, run_name(root.started_at, root.run_id)
        )
    finally:
        store.close()
    notices = [e.data for e in events if e.kind == "notice"]
    assert any(
        n.get("source") == "delegations_accepted" and n.get("ids") == [second] for n in notices
    )


async def test_a_reopened_task_keeps_its_environment_and_is_released_on_acceptance(
    owning: Harness, service: ChildService
) -> None:
    """CE-F3 as amended: the returned task keeps its environment through the review; the
    reopened run works in it; it is handed back and released on acceptance, after its
    one hand-back; the second, accepted as the parent's run ends, is released too."""
    seen: list[list[str]] = []

    async def look() -> None:
        seen.append(list(service.ended))

    root = await owning.run(
        "root",
        [
            Reply(calls=[call("delegate", task=FIRST), call("delegate", task=SECOND)]),
            _wait(),
            _on_first("message_child", text=FEEDBACK),
            _wait(1),
            Reply(calls_from=_on_first("accept_delegation").calls_from, before=look),
            Reply("Both tasks are done.", before=look),
        ],
        routed={
            FIRST: [
                Reply(calls=[call("write_file", path="basis.txt", content="3 1\n1 2\n")]),
                Reply(json.dumps({**PARTIAL, "result_files": ["basis.txt"]})),
                Reply(calls=[call("read_file", path="basis.txt")], before=look),
                Reply(json.dumps({**COMPLETE, "result_files": ["basis.txt"]})),
            ],
            SECOND: [Reply(json.dumps(DONE))],
        },
    )
    first, second = started(root)
    found, other = delegation(owning, first), delegation(owning, second)
    assert found.environment_id is not None
    assert other.environment_id is not None
    # Nothing was ended while the first was under review, nor while it ran again.
    assert seen[0] == []
    assert seen[1] == []
    # Accepted: its one hand-back, then its release; the second's as the run ended.
    assert seen[2] == [found.environment_id]
    assert sorted(service.ended) == sorted([found.environment_id, other.environment_id])
    backs = [b for b in service.hand_backs if b.delegation_id == first]
    assert len(backs) == 1
    assert backs[0].files == ("basis.txt",)
    assert found.environment is not None
    assert found.environment["end"] == "released"
    assert other.environment is not None
    assert other.environment["end"] == "released"
    assert (found.review, other.review_reason) == ("accepted", "parent_ended")
    # The reopened run worked in the same environment.
    runs = [r for r in children(owning, root) if r.delegation == first]
    assert {r.context.environment_id for r in runs} == {found.environment_id}


async def test_a_check_returning_still_ends(delegating: Harness) -> None:
    """A check is not reviewed: its verdict ends it, and a message to it fails back to
    the root."""
    from .test_delegation import CHECK, REPORT

    root = await delegating.run(
        "root",
        [
            Reply(calls=[CHECK]),
            _wait(),
            _on_first("message_child", text="Look again."),
            Reply("Checked."),
        ],
        delegated=[Reply(REPORT)],
    )
    (identity,) = started(root)
    found = delegation(delegating, identity)
    assert (found.ended, found.review, found.results) == ("returned", None, 1)
    sent = json.loads(results(root)[2])
    assert sent["state"] == "recipient_ended"
    assert "reopened" not in sent


# --- The records: states, refusals, the race, a crash ------------------------------


ROOT = "20261015T120000000000Z-root"


def _store(tmp_path: Path) -> ProjectStore:
    store = ProjectStore(tmp_path)
    store.create(PROJECT, "minimal", "sha256:" + "a" * 64)
    store.open_session(PROJECT, SESSION, dedicated=False)
    return store


def _task(n: int, kind: str = "task", parent: str = ROOT) -> Delegation:
    return Delegation.model_validate(
        {
            "delegation_id": f"dlg-{n:024x}",
            "parent": parent,
            "run": f"20261015T12000{n}000000Z-child{n}",
            "kind": kind,
            "brief": {"task": "Do."},
            "started_at": "2026-10-15T12:00:01+00:00",
        }
    )


def test_accept_delegation_is_refused_unless_a_returned_task_of_the_caller(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    try:
        records = DelegationRecords(store.control)
        for n, kind in enumerate(("task", "check", "task", "task")):
            records.start(PROJECT, SESSION, _task(n, kind))
        running, check, other, done = (f"dlg-{n:024x}" for n in range(4))
        records.end(PROJECT, SESSION, check, Ending("returned", "answered", result={"x": 1}))
        records.end(PROJECT, SESSION, other, Ending("returned", "answered"))
        records.end(PROJECT, SESSION, done, Ending("failed", "error"))

        def accept(identity: str, parent: str = ROOT) -> None:
            records.accept(PROJECT, SESSION, identity, by=parent, reason="parent", parent=parent)

        # A parent acts only while its tree's root holds the session.
        with pytest.raises(ControlError, match="no longer holds its session"):
            accept(other)
        RunState(store.control).acquire(PROJECT, SESSION, ROOT, "holder")

        with pytest.raises(ControlError, match=r"not_returned: .* is running"):
            accept(running)
        with pytest.raises(ControlError, match=r"not_returned: .* is returned"):
            accept(check)  # a check's verdict ended it
        with pytest.raises(ControlError, match=r"not_returned: .* is failed"):
            accept(done)
        with pytest.raises(ControlError, match="not_your_child"):
            accept(other, "20261015T120000000000Z-other")
        with pytest.raises(ControlError, match="has no delegation"):
            accept("dlg-" + "f" * 24)
        accept(other)
        with pytest.raises(ControlError, match=r"not_returned: .* is accepted"):
            accept(other)
        # The review is written once: no update reopens or re-closes an accepted task.
        with pytest.raises(sqlite3.IntegrityError), store.control.write() as tx:
            tx.sql.execute(
                f"UPDATE delegations SET review = 'awaiting' WHERE delegation_id = '{other}'"
            )
        with pytest.raises(sqlite3.IntegrityError), store.control.write() as tx:
            tx.sql.execute(
                f"UPDATE delegations SET review_note = 'x' WHERE delegation_id = '{other}'"
            )
    finally:
        store.close()


async def test_the_tool_refuses_and_accepts(tmp_path: Path) -> None:
    async def returns(child: Child, stop: asyncio.Event) -> ChildEnd:
        return ChildEnd("answered", False, json.dumps(DONE), result=DONE)

    made, records, projects, documents = _delegations(tmp_path, returns)
    RunState(projects.control).acquire(PROJECT, SESSION, made.parent, "holder")
    try:
        async with made.lease():
            text, _ = await made.call("delegate", {"task": "Sieve."})
            identity = json.loads(text)["delegation_id"]
            await asyncio.gather(*(c.task for c in made.children.values()))
            with pytest.raises(WorkerAPIError, match="has no delegation"):
                await made.call("accept_delegation", {"delegation_id": "dlg-" + "e" * 24})
            text, _ = await made.call(
                "accept_delegation", {"delegation_id": identity, "note": "  Fine.\n"}
            )
            shown = json.loads(text)
            assert (shown["review"], shown["reviewed"]["note"]) == ("accepted", "Fine.")
            with pytest.raises(WorkerAPIError, match="not_returned"):
                await made.call("accept_delegation", {"delegation_id": identity})
        assert records.get(PROJECT, SESSION, identity).review == "accepted"
    finally:
        documents.close()
        projects.close()


def _returned(
    store: ProjectStore,
    n: int = 0,
    *,
    environment_id: str | None = None,
    environment: dict[str, Any] | None = None,
    parent: str = ROOT,
    brief: dict[str, Any] | None = None,
) -> str:
    """A returned task whose work is the session tree's, as a run left it, with the child
    environment it holds, if any."""
    records = DelegationRecords(store.control)
    task = _task(n, parent=parent).model_copy(
        update={"environment_id": environment_id, "brief": brief or {"task": "Do."}}
    )
    with store.control.write() as tx:
        db = tx.sql
        db.execute(
            "INSERT OR IGNORE INTO research_sessions VALUES (?,?,?,1)", (PROJECT, SESSION, ROOT)
        )
        db.execute(
            "INSERT OR IGNORE INTO research_work "
            "VALUES (?,?,?,?,NULL,?,'main',NULL,'running',NULL)",
            (PROJECT, SESSION, ROOT, ROOT, ROOT),
        )
        db.execute(
            "INSERT INTO research_work VALUES (?,?,?,?,?,?,'delegated',?,'completed',NULL)",
            (PROJECT, SESSION, task.run, ROOT, ROOT, task.run, f"kernel{n}"),
        )
        db.execute(
            "INSERT INTO research_attempts VALUES (?,?,?,?,NULL,1,'answered')",
            (PROJECT, SESSION, task.run, task.run),
        )
    records.start(PROJECT, SESSION, task)
    records.end(
        PROJECT,
        SESSION,
        task.delegation_id,
        Ending("returned", "answered", result={"status": "partial"}, environment=environment),
    )
    return task.delegation_id


def test_an_acceptance_and_a_reopening_message_cannot_both_win(tmp_path: Path) -> None:
    """Both are one write transaction that only a task awaiting review passes: whichever
    commits first wins, and the other is refused, or goes to the root."""
    store = _store(tmp_path)
    try:
        records = DelegationRecords(store.control)
        RunState(store.control).acquire(PROJECT, SESSION, ROOT, "holder")
        first, second = _returned(store, 1), _returned(store, 2)
        mailbox = AgentMessageStore(store.control)
        plan = ReopenPlan("20261015T130000000000Z-again", cap=10, fenced=True)
        # Accepted first: the message is kept for the root, as a delivery failure.
        records.accept(PROJECT, SESSION, first, by=ROOT, reason="parent", parent=ROOT)
        sent = mailbox.deliver_child(
            PROJECT, SESSION, ROOT, None, first, "More.", "op-1", reopen=plan
        )
        assert sent.reopened is None
        assert mailbox.pending(PROJECT, SESSION, None) == [sent.message]
        # Reopened first: the acceptance is refused, and the task runs again.
        sent = mailbox.deliver_child(
            PROJECT, SESSION, ROOT, None, second, "More.", "op-2", reopen=plan
        )
        assert sent.reopened is not None
        assert sent.reopened.number == 2
        with pytest.raises(ControlError, match=r"not_returned: .* is running"):
            records.accept(PROJECT, SESSION, second, by=ROOT, reason="parent", parent=ROOT)
        reopened = records.get(PROJECT, SESSION, second)
        assert (reopened.run, reopened.ended, reopened.review) == (plan.run, None, None)
        assert records.results(PROJECT, SESSION, second)[0].run == _task(2).run
    finally:
        store.close()


def test_a_reopening_is_admitted_under_the_cap_and_its_track(tmp_path: Path) -> None:
    """PD-38's cap counts running delegated runs only: a returned task holds no place, and
    its reopening takes one, refused past the cap; so does its track (PD-9)."""
    store = _store(tmp_path)
    try:
        track = {"task": "Do.", "track": {"track_id": "t1"}}
        identity = _returned(store, brief=track)
        records = DelegationRecords(store.control)
        records.start(PROJECT, SESSION, _task(5), cap=1)  # the returned task does not count
        with store.control.write() as tx, pytest.raises(ControlError, match="limit_reached"):
            reopen_in(tx.sql, PROJECT, SESSION, identity, "20261015T130000000000Z-again", cap=1)
        records.start(PROJECT, SESSION, _task(6).model_copy(update={"brief": track}))
        with store.control.write() as tx, pytest.raises(ControlError, match="track_busy"):
            reopen_in(tx.sql, PROJECT, SESSION, identity, "20261015T130000000000Z-again", cap=5)
        assert records.get(PROJECT, SESSION, identity).review == "awaiting"
    finally:
        store.close()


def test_a_crash_during_a_reopening_ends_it_interrupted_at_the_next_run(tmp_path: Path) -> None:
    """The reopening committed, then the process stopped before its run started: the next
    main run's start ends it interrupted, as any delegation whose process ended (PD-40),
    its earlier result kept and its interruption the next."""
    store = _store(tmp_path)
    try:
        identity = _returned(store)
        with store.control.write() as tx:
            reopen_in(tx.sql, PROJECT, SESSION, identity, "20261015T130000000000Z-again", cap=None)
        records = DelegationRecords(store.control)
        assert records.abandon(PROJECT, SESSION) == [(identity, "20261015T130000000000Z-again")]
        found = records.get(PROJECT, SESSION, identity)
        assert (found.ended, found.review, found.results) == ("interrupted", None, 2)
        assert [r.ended for r in records.results(PROJECT, SESSION, identity)] == [
            "returned",
            "interrupted",
        ]
    finally:
        store.close()


def test_a_later_result_is_a_new_notice_and_acknowledged_alone(tmp_path: Path) -> None:
    store = _store(tmp_path)
    try:
        identity = _returned(store)
        records = DelegationRecords(store.control)
        first = records.get(PROJECT, SESSION, identity)
        assert notice_key(first) == identity
        with store.control.write() as tx:
            reopen_in(tx.sql, PROJECT, SESSION, identity, "20261015T130000000000Z-again", cap=None)
        # The parent saw the first; its acknowledgement does not take the second.
        records.acknowledge(PROJECT, SESSION, [identity], ROOT)
        later = records.end(
            PROJECT,
            SESSION,
            identity,
            Ending("returned", "answered"),
            "20261015T130000000000Z-again",
        )
        assert later.acknowledged is None
        assert notice_key(later) == f"{identity}.2"
        assert [d.delegation_id for d in records.handbacks(PROJECT, SESSION)] == [identity]
        records.acknowledge(PROJECT, SESSION, [f"{identity}.2"], ROOT)
        assert records.get(PROJECT, SESSION, identity).acknowledged == ROOT
        # The session's tree lists the earlier result beside the latest.
        tree = RunTrees(store.control).session(PROJECT, SESSION)
        assert [(r.delegation_id, r.number) for r in tree.earlier] == [(identity, 1)]
        assert [d.results for d in tree.delegations] == [2]
        # An earlier run's late outcome never writes over the latest.
        kept = records.end(PROJECT, SESSION, identity, Ending("failed", "error"), _task(0).run)
        assert (kept.ended, kept.results) == ("returned", 2)
    finally:
        store.close()


# --- Closing for the parent ------------------------------------------------------------


async def test_review_max_accepts_at_a_run_start(tmp_path: Path) -> None:
    async def unused(child: Child, stop: asyncio.Event) -> ChildEnd:
        raise AssertionError

    made, records, projects, documents = _delegations(tmp_path, unused)
    try:
        clock = FakeClock(datetime.now(UTC))
        made.clock = clock
        made.recovering = True  # a Resume's run: only review_max closes for it
        identity = _returned(projects, parent="20261015T110000000000Z-earlier")
        made.limits = records.configure(PROJECT, review_max=3600)
        async with made.lease():
            pass
        assert records.get(PROJECT, SESSION, identity).review == "awaiting"
        clock.advance(3601)
        async with made.lease():
            pass
        found = records.get(PROJECT, SESSION, identity)
        assert (found.review, found.review_reason) == ("accepted", "review_max")
    finally:
        documents.close()
        projects.close()


async def test_a_main_run_accepts_what_a_stopped_parent_left(tmp_path: Path) -> None:
    async def unused(child: Child, stop: asyncio.Event) -> ChildEnd:
        raise AssertionError

    made, records, projects, documents = _delegations(tmp_path, unused)
    try:
        identity = _returned(projects)
        async with made.lease():
            found = records.get(PROJECT, SESSION, identity)
            assert (found.review, found.review_reason) == ("accepted", "parent_ended")
    finally:
        documents.close()
        projects.close()


async def test_the_sessions_close_accepts_what_is_left(
    tmp_path: Path, service: ChildService
) -> None:
    """The close hands back and releases a held environment, as an acceptance does, and
    the session's record closes any it could not."""
    store = _store(tmp_path)
    try:
        context = ExecutionContext(project_id=PROJECT, session_id=SESSION, environment_id="e")
        place = ChildPlacement(service, context)
        made = await place.admit(_task(3).delegation_id, [])
        held = {
            "environment_id": made.environment_id,
            "end": "held",
            "plan": {"files": ["a.txt"], "retained": []},
        }
        identity = _returned(store, 3, environment_id=made.environment_id, environment=held)
        records = DelegationRecords(store.control)
        service.failing_hand_back = True  # the fake copies nothing; the end is what counts
        closed = await Closer(records, place).close_all(PROJECT, SESSION, reason="session_closed")
        assert [d.delegation_id for d in closed] == [identity]
        found = records.get(PROJECT, SESSION, identity)
        assert found.review_reason == "session_closed"
        assert service.ended == [made.environment_id]
        assert [b.files for b in service.hand_backs] == [("a.txt",)]
        assert found.environment is not None
        assert found.environment["end"] != "closing"
        # What the close could not hand back goes with the session's, its account says.
        second = await place.admit(_task(4).delegation_id, [])
        other = _returned(
            store,
            4,
            environment_id=second.environment_id,
            environment={**held, "environment_id": second.environment_id},
        )
        store.close_session(PROJECT, SESSION)
        left = records.get(PROJECT, SESSION, other)
        assert left.review_reason == "session_closed"
        assert left.environment is not None
        assert left.environment["end"] == "released"
        assert "plan" not in left.environment
    finally:
        store.close()


# --- The store ---------------------------------------------------------------------


def test_a_version_87_store_keeps_each_outcome_as_its_first_result(tmp_path: Path) -> None:
    current = _store(tmp_path / "current")
    try:
        records = DelegationRecords(current.control)
        records.start(PROJECT, SESSION, _task(0))
        records.start(PROJECT, SESSION, _task(1))
        records.end(PROJECT, SESSION, _task(0).delegation_id, Ending("returned", "answered"))
    finally:
        current.close()
    target = tmp_path / "old"
    target.mkdir()
    older_store(tmp_path / "current" / "control.sqlite3", target / "control.sqlite3", 87)
    with sqlite3.connect(target / "control.sqlite3") as db:
        columns = {row[1] for row in db.execute("PRAGMA table_info(delegations)")}
    assert "review" not in columns
    migrated = ProjectStore(target)
    try:
        records = DelegationRecords(migrated.control)
        ended = records.get(PROJECT, SESSION, _task(0).delegation_id)
        assert (ended.results, ended.review) == (1, None)
        assert [r.number for r in records.results(PROJECT, SESSION, ended.delegation_id)] == [1]
        assert records.get(PROJECT, SESSION, _task(1).delegation_id).results == 0
    finally:
        migrated.close()


# --- An earlier result's files when a reopened task ends otherwise (the review of PD-46) --


def _workspace(harness: Harness, environment: str) -> Path:
    return harness.tmp / "worker" / "environments" / environment / "project" / "sessions" / SESSION


async def test_a_reopened_task_cancelled_hands_back_its_first_results_files(
    owning: Harness, service: ChildService
) -> None:
    """Reopened, then cancelled by its parent while it runs again: its environment's
    hand-back still brings the first result's file into work/<delegation>/."""
    started_again, release = asyncio.Event(), asyncio.Event()

    async def holds() -> None:
        started_again.set()
        await release.wait()

    async def after_it_started() -> None:
        await asyncio.wait_for(started_again.wait(), 30)

    async def free() -> None:
        release.set()

    root = await owning.run(
        "root",
        [
            Reply(calls=[call("delegate", task=FIRST)]),
            _wait(),
            _on_first("message_child", text=FEEDBACK),
            Reply(calls_from=_on_first("cancel_delegation").calls_from, before=after_it_started),
            Reply("Cancelled.", before=free),
        ],
        routed={
            FIRST: [
                Reply(calls=[call("write_file", path="basis.txt", content="3 1\n1 2\n")]),
                Reply(json.dumps({**PARTIAL, "result_files": ["basis.txt"]})),
                Reply(calls=[call("list_files", path=".")], before=holds),
            ]
        },
    )
    (identity,) = started(root)
    found = delegation(owning, identity)
    assert (found.ended, found.results, found.review) == ("cancelled", 2, None)
    (back,) = [b for b in service.hand_backs if b.delegation_id == identity]
    assert back.files == ("basis.txt",)
    session = owning.context().environment_id
    assert (_workspace(owning, session) / "work" / identity / "basis.txt").read_text() == (
        "3 1\n1 2\n"
    )
    assert found.environment is not None
    assert found.environment["end"] == "released"


async def test_a_reopened_task_at_its_limit_hands_back_its_first_results_files(
    owning: Harness, service: ChildService
) -> None:
    """Reopened, then ended at its environment's wall time, a limit (CE-9): its end hands
    back the first result's file too."""

    async def expire() -> None:
        # The environment's wall time has passed by the time the task runs again.
        for found in list(service.made.values()):
            service.made[found.environment_id] = found.model_copy(
                update={"expires_at": datetime.now(UTC) - timedelta(seconds=1)}
            )

    root = await owning.run(
        "root",
        [
            Reply(calls=[call("delegate", task=FIRST)]),
            _wait(),
            Reply(calls_from=_on_first("message_child", text=FEEDBACK).calls_from, before=expire),
            _wait(1),
            Reply("Stopped at its limit."),
        ],
        routed={
            FIRST: [
                Reply(calls=[call("write_file", path="basis.txt", content="3 1\n1 2\n")]),
                Reply(json.dumps({**PARTIAL, "result_files": ["basis.txt"]})),
            ]
        },
    )
    (identity,) = started(root)
    found = delegation(owning, identity)
    assert (found.ended, found.stopped, found.results) == (
        "limit_reached",
        "environment_time",
        2,
    )
    (back,) = [b for b in service.hand_backs if b.delegation_id == identity]
    assert back.files == ("basis.txt",)
    session = owning.context().environment_id
    assert (_workspace(owning, session) / "work" / identity / "basis.txt").exists()


async def test_a_crash_during_a_reopening_still_hands_back_the_first_results_files(
    tmp_path: Path, service: ChildService
) -> None:
    """The reopening committed, then the process stopped: the next main run ends it
    interrupted, its environment kept with the first result's hand-back planned, and the
    sweep that releases it hands that back first (CE-4, CE-6)."""
    store = _store(tmp_path)
    try:
        context = ExecutionContext(project_id=PROJECT, session_id=SESSION, environment_id="e")
        place = ChildPlacement(service, context)
        made = await place.admit(_task(3).delegation_id, [])
        held = {
            "environment_id": made.environment_id,
            "end": "held",
            "plan": {"files": ["basis.txt"], "retained": []},
        }
        identity = _returned(store, 3, environment_id=made.environment_id, environment=held)
        with store.control.write() as tx:
            reopen_in(tx.sql, PROJECT, SESSION, identity, "20261015T130000000000Z-again", cap=None)
        records = DelegationRecords(store.control)
        records.abandon(PROJECT, SESSION)
        found = records.get(PROJECT, SESSION, identity)
        assert found.environment is not None
        assert (found.ended, found.environment["end"]) == ("interrupted", "kept")
        assert found.environment["plan"] == {"files": ["basis.txt"], "retained": []}
        settings = records.configure(PROJECT, child_idle_max=60)
        place.clock = FakeClock(datetime.now(UTC) + timedelta(hours=1))
        service.failing_hand_back = True  # the fake copies nothing: the request is what counts
        assert await reconcile(place, records, settings) == [made.environment_id]
        assert [b.files for b in service.hand_backs] == [("basis.txt",)]
        account = records.get(PROJECT, SESSION, identity).environment
        assert account is not None
        assert account["end"] == "retained"
        assert "plan" not in account
    finally:
        store.close()


async def test_a_closing_left_by_a_stopped_process_is_finished(
    tmp_path: Path, service: ChildService
) -> None:
    """Accepted, then the process stopped before the hand-back: the environment stays
    ``closing`` until a run's start finishes it (``Closer.finish``)."""
    store = _store(tmp_path)
    try:
        context = ExecutionContext(project_id=PROJECT, session_id=SESSION, environment_id="e")
        place = ChildPlacement(service, context)
        made = await place.admit(_task(5).delegation_id, [])
        held = {
            "environment_id": made.environment_id,
            "end": "held",
            "plan": {"files": ["basis.txt"], "retained": []},
        }
        identity = _returned(store, 5, environment_id=made.environment_id, environment=held)
        records = DelegationRecords(store.control)
        records.accept(PROJECT, SESSION, identity, by="platform", reason="parent_ended")
        assert [d.delegation_id for d in records.closing(PROJECT, SESSION)] == [identity]
        service.failing_hand_back = True
        await Closer(records, place).finish(PROJECT, SESSION)
        assert [b.files for b in service.hand_backs] == [("basis.txt",)]
        assert service.ended == [made.environment_id]
        assert records.closing(PROJECT, SESSION) == []
        account = records.get(PROJECT, SESSION, identity).environment
        assert account is not None
        assert account["end"] not in ("held", "closing")
    finally:
        store.close()


async def test_a_reopening_the_parents_plan_fence_refuses_is_refused(tmp_path: Path) -> None:
    """A reopening starts a run as delegate does: when the parent's fence would refuse
    delegate, the message is refused with the same reason, and the task stays returned."""

    async def returns(child: Child, stop: asyncio.Event) -> ChildEnd:
        return ChildEnd("answered", False, json.dumps(DONE), result=DONE)

    async def fence(tool: str) -> Refusal | None:
        assert tool == "delegate"
        return Refusal("policy_denied", "plan_unread: read the session plan before new work")

    made, records, projects, documents = _delegations(tmp_path, returns)
    RunState(projects.control).acquire(PROJECT, SESSION, made.parent, "holder")
    try:
        async with made.lease():
            text, _ = await made.call("delegate", {"task": "Sieve."})
            identity = json.loads(text)["delegation_id"]
            await asyncio.gather(*(c.task for c in made.children.values()))
            made.fence = fence
            mailbox = AgentMailbox(
                AgentMessageStore(projects.control),
                made.context,
                run=made.parent,
                delegation=None,
                may_message_child=True,
            )
            mailbox.reopener = made
            with pytest.raises(ControlError, match="plan_unread: read the session plan"):
                await mailbox.call(
                    "message_child", {"delegation_id": identity, "text": "Go on."}, "op-1"
                )
        found = records.get(PROJECT, SESSION, identity)
        assert (found.ended, found.review, found.results) == ("returned", "accepted", 1)
        assert found.review_reason == "parent_ended"  # accepted as the run ended
        assert AgentMessageStore(projects.control).listing(PROJECT, SESSION).messages == ()
    finally:
        documents.close()
        projects.close()


def test_only_the_parent_reopens_not_a_grandparent_or_a_sibling(tmp_path: Path) -> None:
    store = _store(tmp_path)
    try:
        RunState(store.control).acquire(PROJECT, SESSION, ROOT, "holder")
        child = _returned(store, 1)
        sibling = _task(2)
        records = DelegationRecords(store.control)
        records.start(PROJECT, SESSION, sibling)
        grandchild = _returned(store, 3, parent=_task(1).run)
        mailbox = AgentMessageStore(store.control)
        plan = ReopenPlan("20261015T130000000000Z-again", cap=10, fenced=True)
        with pytest.raises(ControlError, match="only a direct child"):
            mailbox.deliver_child(
                PROJECT, SESSION, ROOT, None, grandchild, "Go on.", "op-g", reopen=plan
            )
        with pytest.raises(ControlError, match="only a direct child"):
            mailbox.deliver_child(
                PROJECT, SESSION, sibling.run, sibling.delegation_id, child, "Go on.", "op-s",
                reopen=plan,
            )  # fmt: skip
        assert records.get(PROJECT, SESSION, child).review == "awaiting"
        assert records.get(PROJECT, SESSION, grandchild).review == "awaiting"
    finally:
        store.close()


def test_a_resume_continuation_of_the_parent_may_cancel_and_accept(tmp_path: Path) -> None:
    store = _store(tmp_path)
    try:
        identity = _returned(store)
        again = "20261015T140000000000Z-root-again"
        with store.control.write() as tx:
            # The root's first attempt, and a Resume's continuation of its work.
            tx.sql.execute(
                "INSERT INTO research_attempts VALUES (?,?,?,?,NULL,1,NULL)",
                (PROJECT, SESSION, ROOT, ROOT),
            )
            tx.sql.execute(
                "INSERT INTO research_attempts VALUES (?,?,?,?,?,1,NULL)",
                (PROJECT, SESSION, again, ROOT, ROOT),
            )
        RunState(store.control).acquire(PROJECT, SESSION, again, "holder")
        records = DelegationRecords(store.control)
        with pytest.raises(ControlError, match="not_your_child"):
            records.accept(PROJECT, SESSION, identity, by=again, reason="parent", parent=again)
        closed = records.accept(
            PROJECT, SESSION, identity, by=again, reason="parent", review="cancelled",
            parent=again, work=ROOT,
        )  # fmt: skip
        assert (closed.delegation.review, closed.delegation.reviewed_by) == ("cancelled", again)
    finally:
        store.close()


def test_the_store_refuses_what_no_transition_writes(tmp_path: Path) -> None:
    """The review's trigger, as the review's probe tried it: a close rewrites no account,
    an ended row takes no environment, and every outcome is its numbered result."""
    store = _store(tmp_path)
    try:
        identity = _returned(store)
        forged = (
            "UPDATE delegations SET review='accepted', reviewed_at='t', reviewed_by='x', "
            'review_reason=\'parent\', environment=\'{"end":"released","forged":1}\' '
            "WHERE delegation_id=?"
        )
        for sql in (forged, "UPDATE delegations SET environment_id='env-x' WHERE delegation_id=?"):
            with pytest.raises(sqlite3.IntegrityError), store.control.write() as tx:
                tx.sql.execute(sql, (identity,))
        with pytest.raises(sqlite3.IntegrityError), store.control.write() as tx:
            tx.sql.execute(
                "INSERT INTO delegation_results SELECT delegation_id, 7, project_id, session_id, "
                "'other-run', ended, stopped, report, ended_at, refused, result, errors, alias, "
                "models, read_entry, read_proofs, files, environment FROM delegations "
                "WHERE delegation_id=?",
                (identity,),
            )
        # A reopening and an end written by hand still keep the outcome as result 2.
        with store.control.write() as tx:
            tx.sql.execute(
                "UPDATE delegations SET run='r-x', ended=NULL, stopped=NULL, ended_at=NULL, "
                "report=NULL, result=NULL, errors=NULL, alias=NULL, models=NULL, "
                "read_entry=NULL, read_proofs=NULL, files=NULL, environment=NULL, "
                "acknowledged=NULL, review=NULL WHERE delegation_id=?",
                (identity,),
            )
            tx.sql.execute(
                "UPDATE delegations SET ended='returned', stopped='answered', "
                "ended_at='2026-10-26T12:00:00+00:00', "
                "results=results+1, review='awaiting' WHERE delegation_id=?",
                (identity,),
            )
        kept = DelegationRecords(store.control).results(PROJECT, SESSION, identity)
        assert [(r.number, r.run) for r in kept] == [(1, _task(0).run), (2, "r-x")]
    finally:
        store.close()


async def test_review_max_passes_during_a_long_parent_run(tmp_path: Path) -> None:
    async def unused(child: Child, stop: asyncio.Event) -> ChildEnd:
        raise AssertionError

    made, records, projects, documents = _delegations(tmp_path, unused)
    try:
        clock = FakeClock(datetime.now(UTC))
        made.clock = clock
        made.limits = records.configure(PROJECT, review_max=3600)
        async with made.lease():
            identity = _returned(projects, parent="20261015T110000000000Z-earlier")
            view = cast(RunView, SimpleNamespace(offered={}))
            await made.notices("turn", view)
            assert records.get(PROJECT, SESSION, identity).review == "awaiting"
            clock.advance(3601)
            await made.notices("turn", view)
            found = records.get(PROJECT, SESSION, identity)
            assert (found.review, found.review_reason) == ("accepted", "review_max")
    finally:
        documents.close()
        projects.close()
