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
from sandbox_science.orchestrator import cli
from sandbox_science.orchestrator.chat import RunChat, SessionHeld
from sandbox_science.orchestrator.control import ControlError, migrations
from sandbox_science.orchestrator.delegation import hook
from sandbox_science.orchestrator.delegation.outcome import parse
from sandbox_science.orchestrator.delegation.records import (
    Delegation,
    DelegationRecords,
    Ending,
)
from sandbox_science.orchestrator.documents import USER
from sandbox_science.orchestrator.ledger import Ledger
from sandbox_science.orchestrator.memory import ProjectMemory
from sandbox_science.orchestrator.notices import (
    DELEGATIONS,
    DELEGATIONS_RUNNING,
    OUTCOME_CORRECTION,
)
from sandbox_science.orchestrator.projects import ProjectStore, RunRecord, run_name
from sandbox_science.orchestrator.prompts import PromptPins
from sandbox_science.orchestrator.runstate import RunState
from sandbox_science.orchestrator.service import earlier_conversation

from .control_versions import older_store
from .loop_harness import DELEGATED, PRIOR, PROJECT, SESSION, Harness, Reply, call
from .loop_harness import gateway as gateway
from .loop_harness import harness as harness
from .loop_harness import worker as worker

Message = dict[str, Any]
KNOWN = "Write finite fields as GF(p)."
LATER = "Install Python packages with provision."
CHECK = call(
    "delegate",
    task="Check the claim from scratch.",
    context="It came from Fermat's little theorem.",
    inputs=["documents/notes"],
    check={"entry": "C-1", "revision": 1},
)
RESULT: dict[str, Any] = {
    "status": "complete",
    "verdict": "confirmed",
    "summary": "I computed a^16 mod 17 for a = 1..16: each is 1.",
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
        DelegationRecords(store.control).set_depth(PROJECT, 1)
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
    return [str(m["content"]) for m in record.messages if m["role"] == "tool"]


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
        cli.main([*argv, "--depth", "2"])  # depth 2 comes later (PD-43)
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
    brief = str(first[1]["content"])
    assert brief.startswith("Check ledger entry C-1 at revision 1.")
    assert "Task: Check the claim from scratch." in brief
    assert "- documents/notes" in brief
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
    assert outcome.result == {**RESULT, "obstacle": None}
    assert (outcome.errors, outcome.alias, outcome.models) == ((), "main", ("scripted-main",))
    check = cast(dict[str, Any], outcome.brief["check"])
    assert (check["handle"], check["revision"]) == ("C-1", 1)
    assert outcome.acknowledged == name(root)
    waited = json.loads(results(root)[-1])
    assert waited["reason"] == "ended"
    assert waited["delegations"][0]["result"]["verdict"] == "confirmed"
    assert "answer" not in waited["delegations"][0]  # the result stands for it
    assert root.final_text == "C-1 holds: the check confirms it."

    # The child's events were written under its root's lease, and the session's later
    # conversation holds the root's task and answer, not the child's.
    store = ProjectStore(delegating.state)
    try:
        runs = RunState(store.control)
        events = runs.events(PROJECT, SESSION, name(child))
        assert events[0].kind == "started"
        assert events[0].data["role"] == "delegated"
        assert events[-1].kind == "stopped"
        history = earlier_conversation(store, runs, PROJECT, SESSION, 100_000)
    finally:
        store.close()
    assert str(RESULT["summary"]) not in json.dumps(history)


async def test_a_parent_cannot_answer_while_its_check_runs(delegating: Harness) -> None:
    """PD-36: a closing answer while a child runs takes another turn, with a notice."""
    free, release = released()

    async def blocked() -> None:
        await free.wait()

    root = await delegating.run(
        "root",
        [
            Reply(calls=[CHECK]),
            Reply("Done already."),
            Reply(calls_from=wait().calls_from, before=release.before),
            Reply("Done now."),
        ],
        delegated=[Reply(REPORT, before=blocked)],
    )
    assert root.final_text == "Done now."
    third = delegating.gateway.main[2]
    (notice,) = [
        str(m["content"])
        for m in third
        if m["role"] == "user" and str(m["content"]).startswith(DELEGATIONS_RUNNING.text)
    ]
    assert _ID.search(notice)
    assert [r.stopped for r in children(delegating, root)] == ["answered"]


async def test_a_parents_end_cancels_its_check_and_the_next_run_gets_the_outcome(
    delegating: Harness,
) -> None:
    """PD-21, PD-35, PD-39: at its turn limit the parent stops without an answer, its
    running child ends cancelled with its record kept, and the outcome goes to the
    session's next main run, which acknowledges it."""
    free = asyncio.Event()

    async def blocked() -> None:
        await free.wait()

    try:
        root = await delegating.run(
            "root",
            [Reply(calls=[CHECK]), Reply("Done.")],
            delegated=[Reply(REPORT, before=blocked)],
            max_turns=2,
            status=3,
        )
    finally:
        free.set()  # lets the gateway answer the abandoned call
    assert (root.stopped, root.final_text) == ("turn_limit", None)
    (child,) = children(delegating, root)
    assert child.stopped == "cancelled"
    (delegation_id,) = started(root)
    outcome = delegation(delegating, delegation_id)
    assert (outcome.ended, outcome.acknowledged) == ("cancelled", None)

    later = await delegating.run("later", [Reply("Noted: the check was cancelled.")])
    first = delegating.gateway.main[0]
    handbacks = [m for m in first if str(m.get("content", "")).startswith(DELEGATIONS.text)]
    assert len(handbacks) == 1
    assert delegation_id in str(handbacks[0]["content"])
    assert later.delivered["delegations"] == [delegation_id]
    assert delegation(delegating, delegation_id).acknowledged == name(later)


async def test_a_check_is_refused_the_tools_its_role_withholds(delegating: Harness) -> None:
    """PD-16: no review, plan or delegate tools; a call to one is refused for its role."""
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
                ]
            ),
            Reply("Verdict: inconclusive"),
        ],
    )
    (child,) = children(delegating, root)
    refused = results(child)
    assert len(refused) == 4
    for result in refused:
        error = json.loads(result)["error"]
        assert error["kind"] == "policy_denied"
        assert error["message"].startswith("not_permitted_for_role")


async def test_a_session_runs_at_most_its_cap_of_checks_at_once(
    delegating: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PD-38: a delegate past the cap is refused, not queued."""
    monkeypatch.setattr(hook, "MAX_RUNNING", 1)
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
    """PD-19, PD-38: a child that used its tokens ends limit_reached, its record kept."""
    monkeypatch.setattr(hook, "CHECK_TOKENS", 1)
    root = await delegating.run(
        "root",
        [Reply(calls=[CHECK]), wait(), Reply("The check ran out of budget.")],
        delegated=[Reply(calls=[call("ledger_read", entry="C-1")])],
    )
    (child,) = children(delegating, root)
    assert child.stopped == "run_budget"
    (delegation_id,) = started(root)
    outcome = delegation(delegating, delegation_id)
    assert (outcome.ended, outcome.report) == ("limit_reached", None)


def test_a_delegations_outcome_is_written_once(tmp_path: Path) -> None:
    store = ProjectStore(tmp_path)
    try:
        store.create(PROJECT, "minimal", "sha256:" + "a" * 64)
        store.open_session(PROJECT, SESSION, dedicated=False)
        records = DelegationRecords(store.control)
        assert records.depth(PROJECT) == 0
        with pytest.raises(ControlError, match="0 to 1"):
            records.set_depth(PROJECT, 2)
        with pytest.raises(ControlError, match="no project"):
            records.set_depth("other", 1)
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
        chat = RunChat(state, context, run="child", run_id="c", task="Check.", holder="root")
        held = SessionHeld(chat)
        state.acquire(PROJECT, SESSION, "root", "holder-1")
        assert await held.gate("effect") is None
        await chat.write("model", {"turn": 1})
        state.release(PROJECT, SESSION, "holder-1")
        state.acquire(PROJECT, SESSION, "another", "holder-2")
        refused = await held.gate("effect")
        assert refused is not None
        assert refused.kind == "conflict"
        assert await held.gate("read") is None
        await chat.write("model", {"turn": 2})  # logged and dropped: the root lost it
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
        assert records.depth(PROJECT) == 0
        records.set_depth(PROJECT, 1)
        assert records.depth(PROJECT) == 1
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
        (json.dumps({**RESULT, "summary": "x" * 8001}), "summary: String should have at most"),
    ],
)
def test_a_result_is_checked_against_its_schema(answer: str, error: str) -> None:
    with pytest.raises(ValueError, match=re.escape(error)):
        parse(answer)


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
        m for m in second if str(m.get("content", "")).startswith(OUTCOME_CORRECTION.text)
    ]
    assert len(corrections) == 1
    assert not any(str(m.get("content", "")).startswith(OUTCOME_CORRECTION.text) for m in first)
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
    assert outcome.result == blocked


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
            pytest.raises(sqlite3.IntegrityError, match="written once"),
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
