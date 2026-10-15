# SPDX-License-Identifier: MIT
"""General delegations: a task the parent writes (plans-and-delegation spec, PD-11,
PD-17, PD-34; ledger spec, LG-13): its brief, checked as any agent text is, its own
prompt and result, a task's limits, and a delegated agent's authority."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from sandbox_science.orchestrator.delegation.outcome import TASK_FORMAT, parse_task
from sandbox_science.orchestrator.delegation.prompt import TASK_PROMPT
from sandbox_science.orchestrator.delegation.records import Delegation, DelegationRecords
from sandbox_science.orchestrator.documents import DocumentError
from sandbox_science.orchestrator.ledger import Ledger
from sandbox_science.orchestrator.projects import ProjectStore

from .control_versions import older_store
from .loop_harness import PROJECT, SESSION, Harness, Reply, call
from .loop_harness import gateway as gateway
from .loop_harness import harness as harness
from .loop_harness import worker as worker
from .test_delegation import children, delegation, results, started, wait
from .test_delegation import delegating as delegating
from .test_ledger import MAIN
from .test_ledger_delegated_check import link

TASK = call(
    "delegate",
    task="Tabulate a^16 mod 17 for a = 1..16 and save it as table.txt.",
    context="We need the table for C-1's proof.",
    inputs=["documents/notes"],
)
DONE: dict[str, Any] = {
    "status": "complete",
    "summary": "I computed the table and saved it as table.txt: every value is 1.",
    "deviations": [],
    "open_questions": [],
}


async def test_a_task_runs_with_its_own_prompt_brief_and_limits(delegating: Harness) -> None:
    """PD-11: the child is given the parent's task, context and inputs, under the task
    prompt and a task's limits; its result is a task's, kept as returned."""
    root = await delegating.run(
        "root",
        [Reply(calls=[TASK]), wait(), Reply("Done.")],
        delegated=[Reply(json.dumps(DONE))],
    )
    first = delegating.gateway.children[0]
    assert str(first[0]["content"]).startswith(TASK_PROMPT[:60])
    brief = str(first[1]["content"])
    assert brief == (
        "Task: Tabulate a^16 mod 17 for a = 1..16 and save it as table.txt.\n\n"
        "Context from your parent: We need the table for C-1's proof.\n\n"
        "Inputs:\n- documents/notes"
    )
    began = json.loads(results(root)[0])
    assert began["task"] == TASK[1]["task"]
    assert began["limits"] == {"turns": 20, "tokens": 200_000}
    (delegation_id,) = started(root)
    found = delegation(delegating, delegation_id)
    assert (found.kind, found.ended) == ("task", "returned")
    assert found.result == {**DONE, "obstacle": None}
    assert found.brief["context"] == "We need the table for C-1's proof."
    waited = json.loads(results(root)[1])["delegations"][0]
    assert (waited["task"], waited["result"]["status"]) == (TASK[1]["task"], "complete")
    (child,) = children(delegating, root)
    outcome = child.summaries["outcome"]
    assert isinstance(outcome, dict)
    assert outcome["format"] == TASK_FORMAT
    assert child.limits is not None
    assert child.limits["tokens"] == 200_000


async def test_a_task_has_a_delegated_agents_ledger_authority(delegating: Harness) -> None:
    """LG-13: unlike a check, a task records new entries in their first status, and
    changes no entry's status."""
    task = {"type": "task", "title": "Recheck", "statement": "Recheck C-1 for p = 2."}
    root = await delegating.run(
        "root",
        [Reply(calls=[TASK]), wait(), Reply("Done.")],
        delegated=[
            Reply(
                calls=[
                    call(
                        "ledger_change",
                        reason="I checked it.",
                        changes=[{"entry": "C-1", "expected": 1, "status": "established"}],
                    ),
                    call("ledger_change", reason="Left for later.", changes=[task]),
                ]
            ),
            Reply(json.dumps(DONE)),
        ],
    )
    (child,) = children(delegating, root)
    refused, made = (json.loads(r) for r in results(child))
    assert "not_permitted_for_role" in refused["error"]["message"]
    (entry,) = made["entries"]
    documents = delegating.documents()
    try:
        view = Ledger(documents).entry(PROJECT, entry["handle"])
        claim = Ledger(documents).entry(PROJECT, "C-1")
    finally:
        documents.close()
    assert (view.status, view.author["role"], view.author["run_id"]) == (
        "todo",
        "delegated",
        child.run_id,
    )
    assert (claim.status, claim.revision) == ("proposed", 1)


@pytest.mark.parametrize(
    ("brief", "error"),
    [
        ({"task": "Go.", "check": {"entry": "C-1", "revision": 1}}, "a check or a task, not both"),
        ({"task": "Go.", "note": "Mind it."}, "note"),
        ({"task": "a‮b"}, "task"),
        ({"task": "Go.", "context": "c​c"}, "context"),
        ({"task": "Go.", "inputs": ["x" * 513]}, "512"),
        ({"task": "x" * 8001}, "8000"),
        ({"task": "   "}, "task"),
    ],
)
async def test_a_task_brief_is_checked_as_agent_text(
    delegating: Harness, brief: dict[str, Any], error: str
) -> None:
    root = await delegating.run("root", [Reply(calls=[call("delegate", **brief)]), Reply("No.")])
    refused = json.loads(results(root)[0])["error"]
    assert refused["kind"] == "invalid_request"
    assert error in refused["message"]


@pytest.mark.parametrize(
    ("answer", "error"),
    [
        (json.dumps({**DONE, "status": "blocked"}), "names its obstacle"),
        (json.dumps({**DONE, "obstacle": "None."}), "names its obstacle"),
        (json.dumps({**DONE, "verdict": "confirmed"}), "verdict: Extra inputs"),
        ("Done.", "result: Invalid JSON"),
    ],
)
def test_a_task_result_is_checked_against_its_schema(answer: str, error: str) -> None:
    with pytest.raises(ValueError, match=error):
        parse_task(answer)


async def test_a_task_that_answers_badly_is_corrected_once(delegating: Harness) -> None:
    root = await delegating.run(
        "root",
        [Reply(calls=[TASK]), wait(), Reply("Done.")],
        delegated=[Reply("I made the table."), Reply(json.dumps(DONE))],
    )
    (child,) = children(delegating, root)
    outcome = child.summaries["outcome"]
    assert isinstance(outcome, dict)
    assert outcome["errors"]
    (delegation_id,) = started(root)
    assert delegation(delegating, delegation_id).result == {**DONE, "obstacle": None}


async def test_a_task_is_no_check_for_the_gate(delegating: Harness) -> None:
    root = await delegating.run(
        "root", [Reply(calls=[TASK]), wait(), Reply("Done.")], delegated=[Reply(json.dumps(DONE))]
    )
    (delegation_id,) = started(root)
    documents = delegating.documents()
    try:
        with pytest.raises(DocumentError, match="did not check C-1"):
            Ledger(documents).change(
                PROJECT,
                {
                    "operation_id": "link",
                    "reason": "Linked.",
                    "changes": [
                        {"entry": "C-1", "expected": 1, "evidence": [link(delegation_id, 1)]}
                    ],
                },
                MAIN.model_copy(update={"session_id": SESSION}),
            )
    finally:
        documents.close()


def test_a_version_38_store_takes_task_delegations(tmp_path: Path) -> None:
    """Version 39 widens a delegation's kind to a task, keeping the rows and the
    triggers."""
    current = ProjectStore(tmp_path / "current")
    try:
        current.create(PROJECT, "minimal", "sha256:" + "a" * 64)
        current.open_session(PROJECT, SESSION, dedicated=False)
        DelegationRecords(current.control).start(PROJECT, SESSION, made(0, "check"))
    finally:
        current.close()
    target = tmp_path / "old"
    target.mkdir()
    older_store(tmp_path / "current" / "control.sqlite3", target / "control.sqlite3", 38)
    with (
        sqlite3.connect(target / "control.sqlite3") as db,
        pytest.raises(sqlite3.IntegrityError, match="CHECK constraint"),
    ):
        db.execute(
            "INSERT INTO delegations (delegation_id, project_id, session_id, parent, run, "
            "kind, brief, started_at) VALUES (?, ?, ?, ?, ?, 'task', '{}', ?)",
            ("dlg-" + "9" * 24, PROJECT, SESSION, "p", "r", "2026-10-15T12:00:01+00:00"),
        )
    migrated = ProjectStore(target)
    try:
        records = DelegationRecords(migrated.control)
        assert records.get(PROJECT, SESSION, made(0, "check").delegation_id).kind == "check"
        records.start(PROJECT, SESSION, made(1, "task"))
        assert records.get(PROJECT, SESSION, made(1, "task").delegation_id).kind == "task"
        with (
            pytest.raises(sqlite3.IntegrityError, match="written once"),
            migrated.control.write() as tx,
        ):
            tx.sql.execute("UPDATE delegations SET kind = 'check' WHERE kind = 'task'")
    finally:
        migrated.close()


def made(n: int, kind: str) -> Delegation:
    return Delegation.model_validate(
        {
            "delegation_id": f"dlg-{n:024x}",
            "parent": "20261015T120000000000Z-root",
            "run": f"20261015T12000{n}000000Z-child",
            "kind": kind,
            "brief": {"task": "Check."},
            "started_at": "2026-10-15T12:00:01+00:00",
        }
    )
