# SPDX-License-Identifier: MIT
"""Delegation, one level: a main run's check delegations through ``cli.main``, against the
in-process worker and the scripted gateway (plans-and-delegation spec, PD-13 to PD-21 and
PD-32 to PD-39; project memory, PM-8).

A delegated run calls the main alias with its own system prompt; the gateway answers it
from the harness's ``delegated`` script. A child that must still be running when its
parent does something waits in its model call until the parent's script releases it.
"""

from __future__ import annotations

import asyncio
import json
import re
import sqlite3
from pathlib import Path
from typing import Any, cast

import pytest

from sandbox_science.contracts import ExecutionContext
from sandbox_science.orchestrator import chat, cli
from sandbox_science.orchestrator.chat import RunChat, SessionHeld
from sandbox_science.orchestrator.control import migrations
from sandbox_science.orchestrator.delegation.outcome import parse
from sandbox_science.orchestrator.delegation.records import (
    Delegation,
    DelegationRecords,
    Ending,
)
from sandbox_science.orchestrator.documents import USER
from sandbox_science.orchestrator.history import Archive, body, geometry, session_view
from sandbox_science.orchestrator.history.limits import WorkingMemoryLimitError
from sandbox_science.orchestrator.ledger import Ledger
from sandbox_science.orchestrator.loop import Transcript
from sandbox_science.orchestrator.memory import ProjectMemory
from sandbox_science.orchestrator.notices import (
    DELEGATIONS,
    OUTCOME_CORRECTION,
)
from sandbox_science.orchestrator.projects import ProjectStore, RunRecord, run_name
from sandbox_science.orchestrator.prompts import PromptPins
from sandbox_science.orchestrator.runstate import RunState
from sandbox_science.testing import eventually

from .control_versions import older_store
from .loop_harness import DELEGATED, PRIOR, PROJECT, SESSION, Harness, Reply, call, own
from .loop_harness import gateway as gateway
from .loop_harness import harness as harness
from .loop_harness import worker as worker

Message = dict[str, Any]
KNOWN = "Write finite fields as GF(p)."
LATER = "Install Python packages with provision."
CHECK = call(
    "delegate",
    check={"entry": "C-1", "revision": 1},
    note="It came from Fermat's little theorem.",
)
RESULT: dict[str, Any] = {
    "entry": "C-1",
    "revision": 1,
    "status": "complete",
    "verdict": "confirmed",
    "summary": "I computed a^16 mod 17 for a = 1..16: each is 1.",
    "coverage": {
        "examined": ["the statement", "a^16 mod 17 for each nonzero a"],
        "not_examined": [],
    },
    "deviations": [],
    "open_questions": [],
}
REPORT = json.dumps(RESULT)
_ID = re.compile(r"dlg-[0-9a-f]{24}")


@pytest.fixture
def delegating(harness: Harness) -> Harness:
    """A project whose main runs may delegate, with claim C-1 at revision 1."""
    store = ProjectStore(harness.state)
    try:
        DelegationRecords(store.control).configure(PROJECT, depth=1)
    finally:
        store.close()
    documents = harness.documents()
    try:
        claim = {
            "type": "claim",
            "origin": "project",
            "title": "Fermat for 17",
            "statement": "$a^{16} = 1$ for every nonzero $a$ in GF(17).",
            "fields": {"kind": "observation"},
        }
        Ledger(documents).change(
            PROJECT, {"operation_id": "c1", "reason": "Proved.", "changes": [claim]}, USER
        )
    finally:
        documents.close()
    return harness


def wait(mode: str = "all") -> Reply:
    """A call that waits on every delegation the run started so far."""

    def calls(messages: list[Message]) -> list[tuple[str, dict[str, Any]]]:
        found = [
            found
            for message in messages
            if message["role"] == "tool"
            for found in _ID.findall(str(message["content"]))
        ]
        return [call("wait_delegations", delegation_ids=list(dict.fromkeys(found)), mode=mode)]

    return Reply(calls_from=calls)


def about(name: str) -> Reply:
    """A call of ``name`` on the first delegation the run started."""

    def calls(messages: list[Message]) -> list[tuple[str, dict[str, Any]]]:
        found = next(
            match[0]
            for message in messages
            if message["role"] == "tool" and (match := _ID.search(str(message["content"])))
        )
        return [call(name, delegation_id=found)]

    return Reply(calls_from=calls)


def released() -> tuple[asyncio.Event, Reply]:
    """An event, and a reply that sets it as the model thinks."""
    event = asyncio.Event()

    async def release() -> None:
        event.set()

    return event, Reply(calls=[], before=release)


def configure(harness: Harness, **changes: object) -> None:
    store = ProjectStore(harness.state)
    try:
        DelegationRecords(store.control).configure(PROJECT, **changes)
    finally:
        store.close()


def kept(harness: Harness) -> dict[str, RunRecord]:
    store = ProjectStore(harness.state)
    try:
        return {
            entry.run_id: store.run(PROJECT, SESSION, entry.name)
            for entry in store.runs(PROJECT, SESSION)
        }
    finally:
        store.close()


def children(harness: Harness, root: RunRecord) -> list[RunRecord]:
    return [r for r in kept(harness).values() if r.parent == name(root)]


def name(record: RunRecord) -> str:
    return run_name(record.started_at, record.run_id)


def delegation(harness: Harness, delegation_id: str) -> Delegation:
    store = ProjectStore(harness.state)
    try:
        return DelegationRecords(store.control).get(PROJECT, SESSION, delegation_id)
    finally:
        store.close()


def started(record: RunRecord) -> list[str]:
    summary = cast(dict[str, Any], record.summaries["delegations"])
    return cast(list[str], summary["started"])


def results(record: RunRecord) -> list[str]:
    """The record's tool results, in order."""
    return [body(str(m["content"])) for m in own(record) if m["role"] == "tool"]


def tools(record: RunRecord) -> tuple[str, ...]:
    pins = record.prompt_provenance
    assert isinstance(pins, PromptPins)
    return pins.tools.names


async def test_a_project_offers_no_delegation_until_its_setting_allows_it(
    harness: Harness,
) -> None:
    plain = await harness.run("run-1", [Reply("Done.")])
    assert not {"delegate", "wait_delegations"} & set(tools(plain))
    argv = ["project", "delegation", "--state-dir", str(harness.state), "--project", PROJECT]
    assert await asyncio.to_thread(cli.main, [*argv, "--depth", "1"]) == 0
    with pytest.raises(SystemExit):
        cli.main([*argv, "--depth", "3"])  # two levels at most (PD-15)
    allowed = await harness.run("run-2", [Reply("Done.")])
    assert {"delegate", "wait_delegations"} <= set(tools(allowed))


async def test_a_check_starts_fresh_and_its_outcome_reaches_its_parent(
    delegating: Harness,
) -> None:
    """PD-13, PD-17, PD-20, PD-35 and PM-8: the child sees its brief and the memory its
    root was given, and nothing of its parent's conversation; its report reaches the
    parent as the result of its wait, unchanged, and is acknowledged by the parent's
    saved record."""
    documents = delegating.documents()
    try:
        memory = ProjectMemory(documents)
        memory.change(PROJECT, {"operation_id": "u1", "action": "add", "text": KNOWN}, USER)
        memory.propose(PROJECT, {"action": "add", "text": LATER, "reason": "Lost."}, PRIOR, "p1")
    finally:
        documents.close()

    async def accept() -> None:
        documents = delegating.documents()
        try:
            change = {"operation_id": "u2", "action": "accept", "proposal": "prop-1"}
            ProjectMemory(documents).change(PROJECT, change, USER)
        finally:
            documents.close()

    root = await delegating.run(
        "root",
        [Reply(calls=[CHECK], before=accept), wait(), Reply("C-1 holds: the check confirms it.")],
        delegated=[Reply(REPORT)],
    )
    (child,) = children(delegating, root)
    (first,) = delegating.gateway.children
    assert str(first[0]["content"]).startswith(DELEGATED)
    assert KNOWN in str(first[0]["content"])
    assert LATER not in str(first[0]["content"])
    assert [m["role"] for m in first] == ["system", "user"]
    brief = body(str(first[1]["content"]))
    assert brief.startswith("Task: Check whether ledger entry C-1 holds at revision 1, ")
    assert "The requester's note, not evidence: It came from Fermat's little theorem." in brief
    assert "Characterize the loop." not in json.dumps(first)  # the parent's task

    (delegation_id,) = started(root)
    assert (child.role, child.parent, child.delegation) == ("delegated", name(root), delegation_id)
    assert (child.stopped, child.final_text) == ("answered", REPORT)
    assert child.ended_at <= root.ended_at  # a parent's record is kept after its children's
    pins = child.prompt_provenance
    assert isinstance(pins, PromptPins)
    assert pins.role == "delegated"
    assert pins.sections[0].id == "delegated-check"
    assert pins.memory is not None
    assert pins.memory.version == 1
    assert not {"delegate", "review_findings", "plan_read"} & set(pins.tools.names)
    assert {"ledger_read", "python", "document_read"} <= set(pins.tools.names)

    outcome = delegation(delegating, delegation_id)
    assert (outcome.ended, outcome.stopped, outcome.report) == ("returned", "answered", REPORT)
    assert outcome.result == {**RESULT, "obstacle": None, "refutation": None}
    assert (outcome.errors, outcome.alias, outcome.models) == ((), "main", ("scripted-main",))
    check = cast(dict[str, Any], outcome.brief["check"])
    assert (check["handle"], check["revision"]) == ("C-1", 1)
    assert outcome.acknowledged == name(root)
    waited = json.loads(results(root)[-1])
    assert waited["reason"] == "ended"
    assert waited["delegations"][0]["result"]["verdict"] == "confirmed"
    assert "answer" not in waited["delegations"][0]  # the result stands for it
    assert root.final_text == "C-1 holds: the check confirms it."

    # The child's events were written under its root's lease, and the session's view
    # holds the root's messages, the outcome as the root received it among them, and none
    # of the child's (CM-29a).
    store = ProjectStore(delegating.state)
    try:
        runs = RunState(store.control)
        events = runs.events(PROJECT, SESSION, name(child))
        assert events[0].kind == "started"
        assert events[0].data["role"] == "delegated"
        assert events[-1].kind == "stopped"
        view = session_view(
            Archive(store.control), delegating.context(), geometry=geometry(600_000, 0)
        )
    finally:
        store.close()
    assert {part.row.run for part in view.parts if part.row} == {name(root)}


async def test_a_parent_cannot_answer_while_its_check_runs(delegating: Harness) -> None:
    """RC-5: a closing answer waits without a model turn, then incorporates the child."""
    free = asyncio.Event()

    async def blocked() -> None:
        await free.wait()

    task = asyncio.create_task(
        delegating.run(
            "root",
            [Reply(calls=[CHECK]), Reply("Done already."), Reply("Done now.")],
            delegated=[Reply(REPORT, before=blocked)],
        )
    )
    store = ProjectStore(delegating.state)
    state = RunState(store.control)

    def waiting() -> bool:
        held = state.lease(PROJECT, SESSION)
        return held is not None and any(
            event.kind == "waiting" and event.data.get("delegation_ids")
            for event in state.events(PROJECT, SESSION, held.run)
        )

    try:
        await eventually(waiting)
        assert len(delegating.gateway.main) == 2
        assert not task.done()
    finally:
        free.set()
        store.close()
    root = await task
    assert root.final_text == "Done now."
    third = delegating.gateway.main[2]
    assert any(
        m["role"] == "user" and body(str(m["content"])).startswith(DELEGATIONS.text) for m in third
    )
    assert [r.stopped for r in children(delegating, root)] == ["answered"]


async def awaiting_children(harness: Harness, *, delegated: bool = False) -> str:
    """Wait for a durable waiting event while the root's session lease is still held."""
    store = ProjectStore(harness.state)
    state, records = RunState(store.control), DelegationRecords(store.control)
    found: list[str] = []

    def waiting() -> bool:
        held = state.lease(PROJECT, SESSION)
        if held is None:
            return False
        candidates = (
            [d.run for d in records.listed(PROJECT, SESSION)[0]] if delegated else [held.run]
        )
        for run in candidates:
            if any(
                event.kind == "waiting" and event.data.get("delegation_ids")
                for event in state.events(PROJECT, SESSION, run)
            ):
                found.append(run)
                return True
        return False

    try:
        await eventually(waiting)
        return found[0]
    finally:
        store.close()


@pytest.mark.parametrize("answer", ["Done.", None])
async def test_a_limited_parent_keeps_its_check_and_the_next_run_gets_the_outcome(
    delegating: Harness,
    answer: str | None,
) -> None:
    """The parent's final call cannot cancel work with its own remaining allowance."""
    free = asyncio.Event()

    async def blocked() -> None:
        await free.wait()

    task = asyncio.create_task(
        delegating.run(
            "root",
            [Reply(calls=[CHECK]), Reply(answer)],
            delegated=[
                Reply(calls=[call("ledger_read", entry="C-1")], before=blocked),
                Reply(REPORT),
            ],
            max_turns=2,
            status=3,
        )
    )
    try:
        await awaiting_children(delegating)
        assert len(delegating.gateway.main) == 2
        assert not task.done()
        assert kept(delegating) == {}
    finally:
        free.set()
    root = await task
    assert (root.stopped, root.final_text) == ("turn_limit", None)
    (child,) = children(delegating, root)
    assert child.stopped == "answered"
    assert len(delegating.gateway.children) == 2
    assert child.ended_at <= root.ended_at
    (delegation_id,) = started(root)
    outcome = delegation(delegating, delegation_id)
    assert (outcome.ended, outcome.acknowledged) == ("returned", None)

    later = await delegating.run("later", [Reply("Noted: the check finished.")])
    first = delegating.gateway.main[0]
    handbacks = [m for m in first if body(str(m.get("content", ""))).startswith(DELEGATIONS.text)]
    assert len(handbacks) == 1
    assert delegation_id in str(handbacks[0]["content"])
    assert later.delivered["delegations"] == [delegation_id]
    assert delegation(delegating, delegation_id).acknowledged == name(later)


async def test_memory_limited_parent_drains_its_paid_child_before_ending(
    delegating: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    free, child_entered = asyncio.Event(), asyncio.Event()
    original = Transcript.sent.fget
    assert original is not None
    tripped = False

    def guarded(run: Transcript):
        nonlocal tripped
        if not tripped and any(tool.get("tool") == "delegate" for tool in run.tool_records):
            tripped = True
            raise WorkingMemoryLimitError("synthetic parent working-state ceiling")
        return original(run)

    monkeypatch.setattr(Transcript, "sent", property(guarded))

    async def blocked() -> None:
        child_entered.set()
        await free.wait()

    task = asyncio.create_task(
        delegating.run(
            "root",
            [Reply(calls=[CHECK])],
            delegated=[
                Reply(calls=[call("ledger_read", entry="C-1")], before=blocked),
                Reply(REPORT),
            ],
            max_turns=5,
            status=3,
        )
    )
    try:
        await awaiting_children(delegating)
        await asyncio.wait_for(child_entered.wait(), 5)
        assert tripped
        assert len(delegating.gateway.main) == 1
        assert not task.done()
        assert kept(delegating) == {}
    finally:
        free.set()
    root = await task
    assert root.stopped == "memory_limit"
    assert root.final_text is None
    assert root.failure is None
    (child,) = children(delegating, root)
    assert child.stopped == "answered"
    assert child.failure is None
    assert len(delegating.gateway.children) == 2
    assert child.ended_at <= root.ended_at
    (delegation_id,) = started(root)
    assert delegation(delegating, delegation_id).ended == "returned"
    store = ProjectStore(delegating.state)
    try:
        with store.control.read() as tx:
            assert tx.sql.execute(
                "SELECT count(*) FROM research_model_calls WHERE usage IS NULL"
            ).fetchone() == (0,)
    finally:
        store.close()


async def test_a_check_is_refused_the_tools_its_role_withholds(delegating: Harness) -> None:
    """PD-16, PD-41: no review, plan, delegate or question tools; a call to one is refused
    for its role."""
    root = await delegating.run(
        "root",
        [Reply(calls=[CHECK]), wait(), Reply("Done.")],
        delegated=[
            Reply(
                calls=[
                    call("review_findings"),
                    call("plan_read"),
                    CHECK,
                    call("e-print-mcp_search_eprint", query="Ed448"),  # a connector's
                    call("delegation_status", delegation_id="dlg-" + "0" * 24),
                    call("cancel_delegation", delegation_id="dlg-" + "0" * 24),
                    call("ask_user", question="Which curve does C-1 mean?"),
                ]
            ),
            Reply("Verdict: inconclusive"),
        ],
    )
    (child,) = children(delegating, root)
    assert "ask_user" in tools(root)
    assert "ask_user" not in tools(child)
    refused = results(child)
    assert len(refused) == 7
    for result in refused:
        error = json.loads(result)["error"]
        assert error["kind"] == "policy_denied"
        assert error["message"].startswith("not_permitted_for_role")


async def test_a_session_runs_at_most_its_cap_of_checks_at_once(delegating: Harness) -> None:
    """PD-38: a delegate past the project's cap is refused, not queued."""
    configure(delegating, cap=1)
    free, release = released()

    async def blocked() -> None:
        await free.wait()

    root = await delegating.run(
        "root",
        [
            Reply(calls=[CHECK, CHECK]),
            Reply(calls_from=wait().calls_from, before=release.before),
            Reply("Done."),
        ],
        delegated=[Reply(REPORT, before=blocked)],
    )
    second = json.loads(results(root)[1])
    assert second["error"]["kind"] == "unavailable"
    assert second["error"]["message"].startswith("limit_reached")
    assert len(started(root)) == 1


async def test_a_check_stops_at_its_token_limit(
    delegating: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PD-19, PD-38: a child that used its tokens ends limit_reached, its record kept.
    Its first call, as estimated, is past the project's smallest token limit."""
    configure(delegating, check_tokens=50_000)
    monkeypatch.setattr(chat, "ANSWER_TOKENS", 60_000)
    root = await delegating.run(
        "root",
        [Reply(calls=[CHECK]), wait(), Reply("The check ran out of budget.")],
        status=3,
    )
    assert root.stopped == "unfinished_children"
    (child,) = children(delegating, root)
    assert child.stopped == "run_budget"
    assert child.tools == []
    (delegation_id,) = started(root)
    outcome = delegation(delegating, delegation_id)
    assert (outcome.ended, outcome.report) == ("limit_reached", None)


def test_a_delegations_outcome_is_written_once(tmp_path: Path) -> None:
    store = ProjectStore(tmp_path)
    try:
        store.create(PROJECT, "minimal", "sha256:" + "a" * 64)
        store.open_session(PROJECT, SESSION, dedicated=False)
        records = DelegationRecords(store.control)
        made = Delegation.model_validate(
            {
                "delegation_id": "dlg-" + "0" * 24,
                "parent": "20261015T120000000000Z-root",
                "run": "20261015T120001000000Z-child",
                "kind": "check",
                "brief": {"task": "Check."},
                "started_at": "2026-10-15T12:00:01+00:00",
            }
        )
        records.start(PROJECT, SESSION, made)
        assert records.handbacks(PROJECT, SESSION) == []
        long = "x" * 40_000
        first = records.end(
            PROJECT, SESSION, made.delegation_id, Ending("returned", "answered", long)
        )
        assert first.report is not None
        assert len(first.report) < 17_000
        assert first.report.startswith("x" * 100)
        assert "left out" in first.report
        again = records.end(PROJECT, SESSION, made.delegation_id, Ending("failed", "error"))
        assert again == first
        with (
            pytest.raises(sqlite3.IntegrityError, match="written once"),
            store.control.write() as tx,
        ):
            tx.sql.execute("UPDATE delegations SET ended = 'failed'")
        assert [d.delegation_id for d in records.handbacks(PROJECT, SESSION)] == [
            made.delegation_id
        ]
        records.acknowledge(PROJECT, SESSION, [made.delegation_id], "later")
        records.acknowledge(PROJECT, SESSION, [made.delegation_id], "another")
        assert records.get(PROJECT, SESSION, made.delegation_id).acknowledged == "later"
        assert records.handbacks(PROJECT, SESSION) == []
    finally:
        store.close()


async def test_a_delegated_run_acts_only_while_its_root_holds_the_session(
    tmp_path: Path,
) -> None:
    """PD-32: a child takes no lease; its events and its calls that may change something
    need its root to hold the session."""
    projects = ProjectStore(tmp_path)
    try:
        projects.create(PROJECT, "minimal", "sha256:" + "a" * 64)
        projects.open_session(PROJECT, SESSION, dedicated=False)
        state = RunState(projects.control)
        context = ExecutionContext(project_id=PROJECT, session_id=SESSION, environment_id="e")
        child = RunChat(state, context, run="child", run_id="c", task="Check.", holder="root")
        held = SessionHeld(child)
        state.acquire(PROJECT, SESSION, "root", "holder-1")
        assert await held.gate("effect") is None
        await child.write("model", {"turn": 1})
        state.release(PROJECT, SESSION, "holder-1")
        state.acquire(PROJECT, SESSION, "another", "holder-2")
        refused = await held.gate("effect")
        assert refused is not None
        assert refused.kind == "conflict"
        assert await held.gate("read") is None
        await child.write("model", {"turn": 2})  # logged and dropped: the root lost it
        assert [e.data["turn"] for e in state.events(PROJECT, SESSION, "child")] == [1]
    finally:
        projects.close()


def test_a_version_27_store_migrates_with_no_delegations(tmp_path: Path) -> None:
    """Version 28 adds the delegations and the projects' setting, and keeps the rest."""
    current = ProjectStore(tmp_path / "current")
    try:
        current.create(PROJECT, "minimal", "sha256:" + "a" * 64)
        current.open_session(PROJECT, SESSION, dedicated=False)
    finally:
        current.close()
    target = tmp_path / "old"
    target.mkdir()
    older_store(tmp_path / "current" / "control.sqlite3", target / "control.sqlite3", 27)
    with sqlite3.connect(target / "control.sqlite3") as db:
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master")}
    assert not {"delegations", "project_delegation"} & tables
    migrated = ProjectStore(target)
    try:
        assert set(migrated.get(PROJECT).sessions) == {SESSION}
        records = DelegationRecords(migrated.control)
        assert records.settings(PROJECT).depth == 0
        assert records.configure(PROJECT, depth=1).depth == 1
        assert records.handbacks(PROJECT, SESSION) == []
    finally:
        migrated.close()
    with sqlite3.connect(target / "control.sqlite3") as db:
        assert db.execute("PRAGMA user_version").fetchone() == (migrations.VERSION,)
    assert migrations.VERSION >= 28


@pytest.mark.parametrize(
    ("answer", "error"),
    [
        ("Verdict: confirmed", "Invalid JSON"),
        ("[1]", "Input should be an object"),
        (json.dumps({**RESULT, "extra": 1}), "extra: Extra inputs are not permitted"),
        (json.dumps({**RESULT, "verdict": "true"}), "verdict: Input should be"),
        (json.dumps({**RESULT, "status": "blocked"}), "names its obstacle"),
        (json.dumps({**RESULT, "obstacle": "None."}), "names its obstacle"),
        (
            json.dumps({**RESULT, "summary": "x" * 8001}),
            "summary: Value error, a text of 1 to 8000 characters, not 8001",
        ),
        (json.dumps({**RESULT, "status": "partial"}), "only a complete check confirms"),
        (
            json.dumps({**RESULT, "status": "blocked", "obstacle": "No Sage."}),
            "only a complete check confirms",
        ),
        (
            json.dumps({**RESULT, "summary": "   "}),
            "summary: Value error, a text of 1 to 8000 characters, not 0",
        ),
        (json.dumps({**RESULT, "summary": "Fine\u200b."}), "a character the user may not see"),
        (
            json.dumps({**RESULT, "open_questions": ["Why\u202e?"]}),
            "a character the user may not see",
        ),
        (json.dumps({**RESULT, "deviations": ["d"] * 11}), "deviations: Tuple should have at most"),
        (
            json.dumps({**RESULT, "deviations": ["d" * 1001]}),
            "deviations.0: Value error, a text of 1 to 1000 characters",
        ),
        (
            json.dumps({**RESULT, "summary": "x" * 8000, "deviations": ["d" * 1000] * 10}),
            "longer than 16384 bytes",
        ),
        # P3: coverage, and a refutation that cites what contradicts the entry.
        (
            json.dumps({k: v for k, v in RESULT.items() if k != "coverage"}),
            "coverage: Field required",
        ),
        (
            json.dumps({**RESULT, "coverage": {"examined": [], "not_examined": []}}),
            "coverage.examined: Tuple should have at least 1 item",
        ),
        (
            json.dumps({**RESULT, "coverage": {"examined": ["a"], "not_examined": ["the proof"]}}),
            "did not examine everything the entry rests on does not confirm it",
        ),
        (json.dumps({**RESULT, "verdict": "refuted"}), "a refuted check cites, in refutation"),
        (
            json.dumps({**RESULT, "verdict": "inconclusive", "refutation": "a = 3 fails."}),
            "and only a refuted one does",
        ),
    ],
)
def test_a_result_is_checked_against_its_schema(answer: str, error: str) -> None:
    with pytest.raises(ValueError, match=re.escape(error)):
        parse(answer)


def test_a_refutation_and_a_partial_coverage_are_kept() -> None:
    """P3: a refuted check names its counterexample; one that left parts unexamined is
    inconclusive at best."""
    refuted = parse(
        json.dumps({**RESULT, "verdict": "refuted", "refutation": "For a = 3, a^16 = 2 mod 17."})
    )
    assert refuted.refutation == "For a = 3, a^16 = 2 mod 17."
    coverage = {"examined": ["the statement"], "not_examined": ["the lemma's proof"]}
    partial = parse(json.dumps({**RESULT, "verdict": "inconclusive", "coverage": coverage}))
    assert partial.coverage.not_examined == ("the lemma's proof",)


def test_a_result_may_come_in_one_fenced_block() -> None:
    assert parse(f"```json\n{REPORT}\n```").verdict == "confirmed"
    assert parse(f"  {REPORT}\n").status == "complete"


async def test_an_invalid_result_gets_one_correction_and_stays_as_returned(
    delegating: Harness,
) -> None:
    """PD-F5, PD-18: one correction; then the answer stands, the result is missing, the
    errors are kept, and nothing is filled in."""
    root = await delegating.run(
        "root",
        [Reply(calls=[CHECK]), wait(), Reply("The check returned no result.")],
        delegated=[Reply("Verdict: confirmed"), Reply("Still not JSON.")],
    )
    first, second = delegating.gateway.children
    corrections = [
        m for m in second if body(str(m.get("content", ""))).startswith(OUTCOME_CORRECTION.text)
    ]
    assert len(corrections) == 1
    assert not any(
        body(str(m.get("content", ""))).startswith(OUTCOME_CORRECTION.text) for m in first
    )
    (child,) = children(delegating, root)
    assert [m["content"] for m in child.messages if m["role"] == "assistant"] == [
        "Verdict: confirmed",
        "Still not JSON.",
    ]
    (delegation_id,) = started(root)
    outcome = delegation(delegating, delegation_id)
    assert (outcome.ended, outcome.result, outcome.report) == ("returned", None, "Still not JSON.")
    assert len(outcome.errors) == 2
    received = json.loads(results(root)[-1])["delegations"][0]
    assert (received["result"], received["answer"]) == (None, "Still not JSON.")
    assert received["errors"] == list(outcome.errors)


async def test_a_corrected_result_is_kept(delegating: Harness) -> None:
    root = await delegating.run(
        "root",
        [Reply(calls=[CHECK]), wait(), Reply("Confirmed.")],
        delegated=[Reply("Verdict: confirmed"), Reply(f"```json\n{REPORT}\n```")],
    )
    (delegation_id,) = started(root)
    outcome = delegation(delegating, delegation_id)
    assert outcome.result is not None
    assert outcome.result["verdict"] == "confirmed"
    assert len(outcome.errors) == 1


async def test_a_blocked_check_names_its_obstacle_and_its_question(delegating: Harness) -> None:
    """PD-19, PD-41, PD-F6: blocked is the child's claim; the harness saw it return."""
    blocked = {
        **RESULT,
        "status": "blocked",
        "verdict": "inconclusive",
        "obstacle": "The capability sage is missing.",
        "open_questions": ["May I install Sage?"],
    }
    root = await delegating.run(
        "root",
        [Reply(calls=[CHECK]), wait(), Reply("The check is blocked.")],
        delegated=[Reply(json.dumps(blocked))],
    )
    (delegation_id,) = started(root)
    outcome = delegation(delegating, delegation_id)
    assert outcome.ended == "returned"
    assert outcome.result == {**blocked, "refutation": None}


async def test_a_parent_reads_and_cancels_its_check(delegating: Harness) -> None:
    """PD-33, PD-39: delegation_status, then cancel_delegation, which returns the outcome."""
    free = asyncio.Event()

    async def blocked() -> None:
        await free.wait()

    try:
        root = await delegating.run(
            "root",
            [
                Reply(calls=[CHECK]),
                about("delegation_status"),
                about("cancel_delegation"),
                about("cancel_delegation"),
                Reply("Cancelled."),
            ],
            delegated=[Reply(REPORT, before=blocked)],
        )
    finally:
        free.set()
    status, cancelled, again = (json.loads(r) for r in results(root)[1:])
    assert (status["state"], status["limits"]) == ("running", {"turns": 12, "tokens": 150_000})
    assert (cancelled["ended"], cancelled["cancelled"]) == ("cancelled", True)
    assert (again["ended"], again["cancelled"]) == ("cancelled", False)
    (child,) = children(delegating, root)
    assert child.stopped == "cancelled"
    assert root.final_text == "Cancelled."


def test_a_version_28_store_keeps_its_delegations_and_their_outcomes(tmp_path: Path) -> None:
    """Version 29 adds the result, errors, alias and models to an outcome, written once
    with the rest of it; an outcome ended before has none."""
    current = ProjectStore(tmp_path / "current")
    try:
        current.create(PROJECT, "minimal", "sha256:" + "a" * 64)
        current.open_session(PROJECT, SESSION, dedicated=False)
        records = DelegationRecords(current.control)
        for n in (0, 1):
            made = Delegation.model_validate(
                {
                    "delegation_id": f"dlg-{n:024x}",
                    "parent": "20261015T120000000000Z-root",
                    "run": f"20261015T12000{n}000000Z-child",
                    "kind": "check",
                    "brief": {"task": "Check."},
                    "started_at": "2026-10-15T12:00:01+00:00",
                }
            )
            records.start(PROJECT, SESSION, made)
        records.end(PROJECT, SESSION, f"dlg-{0:024x}", Ending("returned", "answered", "Done."))
    finally:
        current.close()
    target = tmp_path / "old"
    target.mkdir()
    older_store(tmp_path / "current" / "control.sqlite3", target / "control.sqlite3", 28)
    migrated = ProjectStore(target)
    try:
        records = DelegationRecords(migrated.control)
        ended = records.get(PROJECT, SESSION, f"dlg-{0:024x}")
        assert (ended.ended, ended.report, ended.result, ended.alias) == (
            "returned",
            "Done.",
            None,
            None,
        )
        with (
            pytest.raises(sqlite3.IntegrityError, match="written once"),
            migrated.control.write() as tx,
        ):
            tx.sql.execute("UPDATE delegations SET alias = 'late' WHERE ended IS NOT NULL")
        with (
            pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed: delegations"),
            migrated.control.write() as tx,
        ):
            tx.sql.execute("UPDATE delegations SET result = '{}' WHERE ended IS NULL")
        ending = Ending(
            "returned",
            "answered",
            REPORT,
            result=RESULT,
            errors=("an error",),
            alias="main",
            models=("m",),
        )
        later = records.end(PROJECT, SESSION, f"dlg-{1:024x}", ending)
        assert (later.result, later.errors, later.models) == (RESULT, ("an error",), ("m",))
    finally:
        migrated.close()


def test_a_version_31_store_keeps_its_outcomes_and_writes_reads_of_proofs_once(
    tmp_path: Path,
) -> None:
    """Version 32 adds whether a check read its proofs' targets: none for the outcomes
    written before, and once with the rest of an outcome after."""
    current = ProjectStore(tmp_path / "current")
    try:
        current.create(PROJECT, "minimal", "sha256:" + "a" * 64)
        current.open_session(PROJECT, SESSION, dedicated=False)
        records = DelegationRecords(current.control)
        for n in (0, 1):
            made = Delegation.model_validate(
                {
                    "delegation_id": f"dlg-{n:024x}",
                    "parent": "20261015T120000000000Z-root",
                    "run": f"20261015T12000{n}000000Z-child",
                    "kind": "check",
                    "brief": {"task": "Check."},
                    "started_at": "2026-10-15T12:00:01+00:00",
                }
            )
            records.start(PROJECT, SESSION, made)
        records.end(PROJECT, SESSION, f"dlg-{0:024x}", Ending("returned", "answered", "Done."))
    finally:
        current.close()
    target = tmp_path / "old"
    target.mkdir()
    older_store(tmp_path / "current" / "control.sqlite3", target / "control.sqlite3", 31)
    with sqlite3.connect(target / "control.sqlite3") as db:
        columns = {row[1] for row in db.execute("PRAGMA table_info(delegations)")}
    assert "read_proofs" not in columns
    migrated = ProjectStore(target)
    try:
        records = DelegationRecords(migrated.control)
        assert records.get(PROJECT, SESSION, f"dlg-{0:024x}").read_proofs is None
        for statement, error in (
            ("UPDATE delegations SET read_proofs = 1 WHERE ended IS NOT NULL", "written once"),
            ("UPDATE delegations SET read_proofs = 1 WHERE ended IS NULL", "CHECK constraint"),
        ):
            with (
                pytest.raises(sqlite3.IntegrityError, match=error),
                migrated.control.write() as tx,
            ):
                tx.sql.execute(statement)
        ending = Ending("returned", "answered", REPORT, read_entry=True, read_proofs=True)
        later = records.end(PROJECT, SESSION, f"dlg-{1:024x}", ending)
        assert (later.read_entry, later.read_proofs) == (True, True)
    finally:
        migrated.close()


async def test_a_checks_brief_names_the_entrys_open_flags(delegating: Harness) -> None:
    """The owner's rule applied to LG-29 (2026-10-15): the platform's brief names the open
    flags of the entry it checks, and keeps their IDs, so that a check that counts for
    clearing a flag on a settled entry addressed why it was flagged."""
    documents = delegating.documents()
    try:
        ledger = Ledger(documents)
        assumption = {"type": "assumption", "title": "p", "statement": "$p = 17$.", "key": "a"}
        resting = {
            "type": "claim",
            "origin": "project",
            "title": "Fermat for p",
            "statement": "$a^{p-1} = 1$.",
            "links": [{"kind": "assumes", "entry": "new:a"}],
        }
        ledger.change(
            PROJECT, {"operation_id": "a", "reason": "r", "changes": [assumption, resting]}, USER
        )
        revised = {"entry": "A-1", "expected": 1, "statement": "$p = 19$."}
        ledger.change(PROJECT, {"operation_id": "f", "reason": "r", "changes": [revised]}, USER)
        (flag,) = ledger.entry(PROJECT, "C-2").flags
    finally:
        documents.close()
    check = call("delegate", check={"entry": "C-2", "revision": 1})
    root = await delegating.run(
        "root", [Reply(calls=[check]), wait(), Reply("Done.")], delegated=[Reply(REPORT)]
    )
    first = delegating.gateway.children[0]
    brief = str(first[1]["content"])
    assert f"assumption revised, from A-1 ({flag.flag_id})" in brief
    (delegation_id,) = started(root)
    written = cast(dict[str, Any], delegation(delegating, delegation_id).brief["check"])
    assert written["flags"] == [flag.flag_id]
