# SPDX-License-Identifier: MIT
"""A task's deliverables and lessons (plans-and-delegation spec, PD-45, PD-F13; delegated
agents spec, DA-27): what the brief lists, numbered by the platform; the result's
accounting for each, checked against the child's calls; one correction for one left
out, and the platform's ``blocked`` for a second omission; the notice to a brief that
lists none while the user's plan names outputs; and the child's lessons, its notes."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, cast

import pytest
from pydantic import JsonValue, ValidationError

from sandbox_science.orchestrator.agent_messages import ChildMessage
from sandbox_science.orchestrator.context.agent import AgentContext
from sandbox_science.orchestrator.context.history import body
from sandbox_science.orchestrator.delegation.hook import (
    LESSONS_FRAMING,
    Child,
    ChildEnd,
    outcome,
    summarized,
)
from sandbox_science.orchestrator.delegation.outcome import (
    TASK_BYTES,
    Asked,
    Outcome,
    parse,
    parse_task,
)
from sandbox_science.orchestrator.delegation.prompt import TASK_PROMPT
from sandbox_science.orchestrator.delegation.records import (
    RESULT_BYTES,
    Delegation,
    DelegationRecords,
    Ending,
)
from sandbox_science.orchestrator.delegation.specs import SPECS, TaskBrief, brief_of
from sandbox_science.orchestrator.loop import SYSTEM_PROMPT, Transcript
from sandbox_science.orchestrator.notices import OUTCOME_CORRECTION
from sandbox_science.orchestrator.session_plans import PlanOutput, SessionPlans

from .loop_harness import PROJECT, SESSION, Harness, Reply, call
from .loop_harness import gateway as gateway
from .loop_harness import harness as harness
from .loop_harness import worker as worker
from .test_delegation import REPORT, children, delegation, results, started, wait
from .test_delegation import RESULT as CHECKED
from .test_delegation import delegating as delegating
from .test_delegation_lifecycle import _delegations
from .test_delegation_tasks import DONE, KEPT
from .test_delegation_tracks import READ, active_plan
from .test_session_plans import actor, made
from .test_session_plans import body as plan_body
from .test_session_plans import plans as plans
from .test_session_plans import store as store

DELIVERABLES: list[dict[str, Any]] = [
    {
        "title": "The table of a^16 mod 17",
        "kind": "file",
        "description": "table.txt, one value for each a",
    },
    {"title": "A task to recheck C-1", "kind": "entry", "description": "A ledger task"},
    {"title": "Whether every value is 1", "kind": "answer", "description": "Yes or no"},
]
ASKED = (
    Asked("d1", "The table of a^16 mod 17", "file"),
    Asked("d2", "A task to recheck C-1", "entry"),
    Asked("d3", "Whether every value is 1", "answer"),
)
TASK = call(
    "delegate",
    task="Tabulate a^16 mod 17 into table.txt, record a task to recheck C-1, and say "
    "whether every value is 1.",
    deliverables=DELIVERABLES,
)
SMALL = call("delegate", task="Say whether 17 is prime.")
WRITE = call("write_file", path="table.txt", content="1 1 1 1 1 1 1 1 1 1 1 1 1 1 1 1\n")
RECHECK = call(
    "ledger_change",
    reason="Left for later.",
    changes=[
        {
            "type": "task",
            "title": "Recheck",
            "statement": "Recheck C-1 for p = 2.",
            "anchors": [{"entry": "C-1"}],
        }
    ],
)
D1 = {"id": "d1", "status": "produced", "path": "table.txt"}
D2 = {"id": "d2", "status": "produced", "entry": "T-1"}
D3 = {"id": "d3", "status": "not_produced", "reason": "I ran out of time to check a = 0."}
FIRST = {**DONE, "deliverables": [D1, D2]}
CORRECTED = {**DONE, "status": "partial", "deliverables": [D1, D2, D3]}
GIVEN_UP = [
    {"id": "d1", "status": "not_produced", "reason": "The kernel did not start."},
    {"id": "d2", "status": "not_produced", "reason": "Nothing to recheck yet."},
]
OMITTED = {**DONE, "status": "partial", "deliverables": GIVEN_UP}


def corrections(harness: Harness) -> list[dict[str, Any]]:
    """The correction notices the children's model calls were given, each once."""
    found: dict[str, dict[str, Any]] = {}
    for messages in harness.gateway.children:
        for message in messages:
            text = body(str(message.get("content", "")))
            if text.startswith(OUTCOME_CORRECTION.text):
                found[text] = json.loads(text[len(OUTCOME_CORRECTION.text) :])
    return list(found.values())


def listed_of(value: object) -> list[dict[str, Any]]:
    """A JSON list of objects, as a test reads it."""
    assert isinstance(value, list)
    found = cast(list[object], value)
    assert all(isinstance(item, dict) for item in found)
    return cast(list[dict[str, Any]], found)


def run() -> Transcript:
    return Transcript(context=AgentContext(messages=[]))


def recorded(path: str, revision: int = 1) -> dict[str, Any]:
    entry = {"path": path, "artifact_id": "artifact-1", "revision": revision, "sha256": "ab" * 32}
    return {"recorded_files": {"files": [{**entry, "deleted": False, "content_retained": True}]}}


# --- PD-F13 ------------------------------------------------------------------------


async def test_an_omitted_deliverable_is_corrected_once_and_the_three_are_kept(
    delegating: Harness,
) -> None:
    """PD-F13: the first answer accounts for d1 and d2 only and gets one correction
    naming d3; the corrected result, d3 not produced with its reason, is kept with the
    three statuses, and the parent's notice counts them."""
    root = await delegating.run(
        "root",
        [Reply(calls=[TASK]), wait(), Reply("Done.")],
        delegated=[
            Reply(calls=[WRITE, RECHECK]),
            Reply(json.dumps(FIRST)),
            Reply(json.dumps(CORRECTED)),
        ],
    )
    began = json.loads(results(root)[0])
    assert began["deliverables"] == ["d1", "d2", "d3"]
    assert "notice" not in began
    (delegation_id,) = started(root)
    found = delegation(delegating, delegation_id)
    assert [d["id"] for d in listed_of(found.brief["deliverables"])] == ["d1", "d2", "d3"]
    first = delegating.gateway.children[0]
    assert "- d3 (answer): Whether every value is 1. What counts: Yes or no" in body(
        str(first[1]["content"])
    )
    (correction,) = corrections(delegating)
    assert correction["missing"] == [{"id": "d3", "title": "Whether every value is 1"}]
    assert "d3 (Whether every value is 1)" in correction["errors"][0]
    assert found.result is not None
    assert (found.ended, found.result["status"]) == ("returned", "partial")
    accounted = listed_of(found.result["deliverables"])
    assert [{k: v for k, v in d.items() if v is not None} for d in accounted] == [D1, D2, D3]
    assert "set_by" not in found.result
    assert summarized(found)["deliverables"] == "2 produced, 1 not produced"
    shown = outcome(found)
    assert "deliverables" not in listed_of([shown["result"]])[0]
    assert [(d["id"], d["title"], d["status"]) for d in listed_of(shown["deliverables"])] == [
        ("d1", "The table of a^16 mod 17", "produced"),
        ("d2", "A task to recheck C-1", "produced"),
        ("d3", "Whether every value is 1", "not_produced"),
    ]
    waited = json.loads(results(root)[1])["delegations"][0]
    assert waited["deliverables"][1] == {
        "id": "d2",
        "title": "A task to recheck C-1",
        "kind": "entry",
        "status": "produced",
        "entry": "T-1",
    }


async def test_a_second_omission_ends_the_task_blocked_by_the_platform(
    delegating: Harness,
) -> None:
    """PD-F13: the child leaves d3 out twice: the task ends returned, status blocked, the
    obstacle naming d3, both marked as the platform's, the child's status and answer kept."""
    root = await delegating.run(
        "root",
        [Reply(calls=[TASK]), wait(), Reply("Done.")],
        delegated=[Reply(json.dumps(OMITTED)), Reply(json.dumps(OMITTED))],
    )
    (delegation_id,) = started(root)
    found = delegation(delegating, delegation_id)
    assert len(corrections(delegating)) == 1
    assert found.ended == "returned"
    assert found.report == json.dumps(OMITTED)
    result = found.result
    assert result is not None
    assert (result["status"], result["obstacle"]) == (
        "blocked",
        "deliverables not accounted for: d3 (Whether every value is 1)",
    )
    assert result["set_by"] == {"status": "platform", "obstacle": "platform"}
    assert result["child_status"] == "partial"
    assert result["summary"] == DONE["summary"]
    assert summarized(found)["deliverables"] == "2 not produced, 1 not accounted for"
    shown = outcome(found)["deliverables"]
    assert isinstance(shown, list)
    assert shown[2] == {
        "id": "d3",
        "title": "Whether every value is 1",
        "kind": "answer",
        "status": "unaccounted",
    }
    (child,) = children(delegating, root)
    assert listed_of([child.summaries["outcome"]])[0]["result"] == result


async def test_a_small_task_lists_none_and_its_outcome_is_as_before(delegating: Harness) -> None:
    """PD-F13: a brief without deliverables is valid; its result has no deliverables
    field, its outcome is as before, and with no plan there is no notice."""
    root = await delegating.run(
        "root", [Reply(calls=[SMALL]), wait(), Reply("Done.")], delegated=[Reply(json.dumps(DONE))]
    )
    began = json.loads(results(root)[0])
    assert "notice" not in began
    assert "deliverables" not in began
    (delegation_id,) = started(root)
    found = delegation(delegating, delegation_id)
    assert found.result == KEPT
    assert "deliverables" not in found.brief
    assert "deliverables" not in summarized(found)
    waited = json.loads(results(root)[1])["delegations"][0]
    assert "deliverables" not in waited
    assert "deliverables_unaccounted" not in waited


async def test_a_brief_without_deliverables_is_told_of_the_users_outputs(
    delegating: Harness,
) -> None:
    """PD-45, PD-F13: the session's plan holds a desired output the user wrote; a task
    listing no deliverables is answered with a notice naming it, and still starts; one
    that lists any gets none."""
    active_plan(delegating)
    listed = call("delegate", task="Write the result up.", deliverables=DELIVERABLES[2:])
    root = await delegating.run(
        "root",
        [READ, Reply(calls=[SMALL]), wait(), Reply(calls=[listed]), wait(), READ, Reply("Done.")],
        delegated=[
            Reply(json.dumps(DONE)),
            Reply(json.dumps({**DONE, "deliverables": [{**D3, "id": "d1"}], "status": "partial"})),
        ],
    )
    began = json.loads(results(root)[1])
    assert began["state"] == "running"
    assert began["notice"]["outputs"] == ["Result"]
    assert "no deliverables" in began["notice"]["text"]
    small, other = started(root)
    assert delegation(delegating, small).result == KEPT
    assert "notice" not in json.loads(results(root)[3])
    assert delegation(delegating, other).ended == "returned"


def test_only_the_outputs_the_user_wrote_are_stated(plans: SessionPlans) -> None:
    """PD-45: an output an agent wrote is not on record until the user writes or edits
    it; an output the user wrote stays theirs through an agent's later versions."""
    user, agent = actor("user"), actor("main")
    first = made(plans, agent)
    assert plans.stated_outputs(user) == ()
    theirs = PlanOutput(title="Proof", description="A written proof", evidence_standard="Check")
    edited = plans.revise(
        user, "edit", first.ref, plan_body(outputs=(*first_outputs(plans, user), theirs)), "Add."
    )
    assert [o.title for o in plans.stated_outputs(user)] == ["Proof"]
    again = plans.revise(
        agent, "agent", edited.ref, plan_body(outputs=first_outputs(plans, user)), "Same."
    )
    assert [o.title for o in plans.stated_outputs(user)] == ["Proof"]
    reworded = PlanOutput(title="Proof", description="Reworded", evidence_standard="Check")
    plans.revise(agent, "reword", again.ref, plan_body(outputs=(reworded,)), "Reword.")
    assert plans.stated_outputs(user) == ()


def first_outputs(plans: SessionPlans, authority: Any) -> tuple[PlanOutput, ...]:
    current = plans.current(authority)
    assert current is not None
    return current.version.body.outputs


@pytest.mark.parametrize(
    ("deliverables", "error"),
    [
        ([DELIVERABLES[0]] * 13, "at most 12"),
        ([{**DELIVERABLES[0], "title": "t" * 121}], "at most 120"),
        ([{**DELIVERABLES[0], "title": ""}], "at least 1"),
        ([{**DELIVERABLES[0], "title": "two\nlines"}], "one line"),
        ([{**DELIVERABLES[0], "description": "d" * 401}], "at most 400"),
        ([{**DELIVERABLES[0], "description": "a​b"}], "description"),
        ([{**DELIVERABLES[0], "kind": "document"}], "kind"),
        ([{"title": "T", "kind": "file"}], "description"),
    ],
)
def test_a_brief_past_the_limits_of_its_deliverables_is_refused(
    deliverables: list[dict[str, Any]], error: str
) -> None:
    """PD-F13: 13 deliverables, or a title of 121 characters, are refused."""
    with pytest.raises(ValidationError, match=error):
        brief_of({"task": "Go.", "deliverables": deliverables})


def test_twelve_deliverables_are_numbered_and_a_check_takes_none() -> None:
    brief = brief_of({"task": "Go.", "deliverables": [DELIVERABLES[0]] * 12})
    assert isinstance(brief, TaskBrief)
    assert len(brief.deliverables) == 12
    with pytest.raises(ValidationError, match="deliverables"):
        brief_of({"check": {"entry": "C-1", "revision": 1}, "deliverables": DELIVERABLES})


def test_a_message_to_a_child_cannot_change_its_deliverables() -> None:
    """PD-45: they are written once with the brief."""
    with pytest.raises(ValidationError, match="deliverables"):
        ChildMessage.model_validate(
            {"delegation_id": "dlg-1", "text": "Also d4.", "deliverables": DELIVERABLES}
        )


# --- The accounting, checked -------------------------------------------------------


async def test_a_file_the_childs_calls_did_not_write_is_invalid() -> None:
    """PD-F13: a deliverable naming a file its calls did not write is a validation error,
    corrected once; then the result is missing."""
    found = Outcome(task=True, deliverables=ASKED)
    answer = json.dumps({**DONE, "status": "partial", "deliverables": [D1, D2, D3]})
    await found.observe_tool(run(), {"ledger": "ledger_change", "result": {"entries": []}})
    closing = await found.closing(run(), answer)
    assert closing.action == "continue"
    assert found.result is None
    assert "d1: your calls wrote or published no file table.txt" in found.errors[0]
    assert "d2: your ledger changes made no revision or work record of T-1" in found.errors[0]
    await found.closing(run(), answer)
    assert (found.result, found.kept()) == (None, None)


async def test_a_reference_must_be_what_the_childs_calls_made() -> None:
    """An artifact names a revision the calls recorded; an entry is one they revised,
    or holds a work record they appended; a reference is of the deliverable's kind."""
    asked = (Asked("d1", "Table", "artifact"), Asked("d2", "Issue", "entry"))
    seen: list[tuple[str, frozenset[str]]] = []

    async def entry_made(reference: str, work: frozenset[str]) -> bool:
        seen.append((reference, work))
        return reference == "I-1"

    async def closed(*items: dict[str, Any]) -> Outcome:
        found = Outcome(task=True, deliverables=asked, entry_made=entry_made)
        await found.observe_tool(run(), recorded("table.txt", 2))
        changed = {"entries": [{"entry_id": "e-1", "handle": "T-1"}], "work": ["w-1"]}
        await found.observe_tool(run(), {"ledger": "ledger_change", "result": changed})
        await found.closing(run(), json.dumps({**DONE, "deliverables": list(items)}))
        return found

    artifact = {"id": "d1", "status": "produced", "path": "table.txt", "revision": 2}
    for entry in ("T-1", "e-1", "I-1"):
        found = await closed(artifact, {"id": "d2", "status": "produced", "entry": entry})
        assert found.result is not None, found.errors
    assert seen == [("I-1", frozenset({"w-1"}))]
    for wrong, error in (
        ({**artifact, "revision": 1}, "no revision 1 of table.txt"),
        ({"id": "d1", "status": "produced", "path": "table.txt"}, "gives path and revision"),
        ({**artifact, "entry": "T-1"}, "not entry"),
    ):
        found = await closed(wrong, {"id": "d2", "status": "produced", "entry": "T-1"})
        assert found.result is None
        assert error in found.errors[0]
    found = await closed(artifact, {"id": "d2", "status": "produced", "entry": "C-9"})
    assert "no revision or work record of C-9" in found.errors[0]


@pytest.mark.parametrize(
    ("deliverables", "status", "error"),
    [
        ([D1, D2, D3], "complete", "complete only when every deliverable was produced"),
        ([D1, D2, {**D3, "id": "d4"}], "partial", "the brief has no deliverable d4"),
        ([D1, D1, D2], "partial", "accounted for once"),
        ([D1, D2, {"id": "d3", "status": "partial"}], "partial", "says what is missing"),
        ([D1, D2, {"id": "d3", "status": "not_produced"}], "partial", "says why"),
        ([D1, D2, {**D3, "answer": "No."}], "partial", "names nothing it produced"),
        ([D1, D2, {"id": "d3", "status": "produced"}], "complete", "produced, it gives answer"),
    ],
)
async def test_the_accounting_agrees_with_the_brief_and_the_status(
    deliverables: list[dict[str, Any]], status: str, error: str
) -> None:
    found = Outcome(task=True, deliverables=ASKED)
    await found.observe_tool(run(), recorded("table.txt"))
    changed = {"entries": [{"entry_id": "e-1", "handle": "T-1"}]}
    await found.observe_tool(run(), {"ledger": "ledger_change", "result": changed})
    answer = {**DONE, "status": status, "deliverables": deliverables}
    closing = await found.closing(run(), json.dumps(answer))
    assert closing.action == "continue"
    assert found.result is None
    assert error in found.errors[0]


async def test_a_produced_answer_and_a_partial_file_are_accounted_for() -> None:
    found = Outcome(task=True, deliverables=ASKED)
    await found.observe_tool(run(), recorded("table.txt"))
    await found.observe_tool(
        run(), {"ledger": "ledger_change", "result": {"entries": [{"handle": "T-1"}]}}
    )
    partial = {"id": "d1", "status": "partial", "path": "table.txt", "missing": "a = 0"}
    answer = {"id": "d3", "status": "produced", "answer": "Yes, for every nonzero a."}
    result = {**DONE, "status": "partial", "deliverables": [partial, D2, answer]}
    assert (await found.closing(run(), json.dumps(result))).action == "accept"
    kept = found.kept()
    assert kept is not None
    statuses = [d["status"] for d in listed_of(kept["deliverables"])]
    assert statuses == ["partial", "produced", "produced"]


async def test_deliverables_in_the_result_of_a_brief_without_any_are_invalid() -> None:
    found = Outcome(task=True)
    await found.closing(run(), json.dumps({**DONE, "deliverables": [D3]}))
    assert found.result is None
    assert "the brief lists none" in found.errors[0]


async def test_with_no_turn_left_for_the_correction_the_platform_blocks_it() -> None:
    """PD-45: the first omission already holds the platform's block, which stands when
    the loop has no turn to give for the correction."""
    found = Outcome(task=True, deliverables=ASKED)
    closing = await found.closing(run(), json.dumps(OMITTED))
    assert closing.action == "continue"
    kept = found.kept()
    assert kept is not None
    assert (kept["status"], kept["child_status"]) == ("blocked", "partial")


async def test_an_omission_after_another_correction_is_blocked_and_with_other_errors_missing() -> (
    None
):
    found = Outcome(task=True, deliverables=ASKED)
    await found.closing(run(), "Not JSON.")
    await found.closing(run(), json.dumps(OMITTED))
    kept = found.kept()
    assert kept is not None
    assert kept["status"] == "blocked"
    other = Outcome(task=True, deliverables=ASKED)
    await other.closing(run(), json.dumps(OMITTED))
    await other.closing(run(), json.dumps({**OMITTED, "deliverables": [{**GIVEN_UP[0]}, D1]}))
    assert (other.result, other.kept()) == (None, None)


def test_a_task_without_a_result_accounts_for_nothing() -> None:
    """PD-45: a task that ends without a result lists the brief's deliverables as
    unaccounted for, the platform's observation."""
    found = Delegation.model_validate(
        {
            "delegation_id": "dlg-1",
            "parent": "root",
            "run": "child",
            "kind": "task",
            "brief": {
                "task": "Go.",
                "deliverables": [
                    {"id": a.id, **d} for a, d in zip(ASKED, DELIVERABLES, strict=True)
                ],
            },
            "started_at": "2026-10-25T12:00:00+00:00",
            "ended": "limit_reached",
            "stopped": "turn_limit",
            "ended_at": "2026-10-25T12:01:00+00:00",
        }
    )
    assert outcome(found)["deliverables_unaccounted"] == [
        {"id": a.id, "title": a.title} for a in ASKED
    ]
    assert summarized(found)["deliverables"] == "3 not accounted for"


# --- The records' bounds -----------------------------------------------------------


async def test_a_brief_at_every_limit_fits_its_record(tmp_path: Path) -> None:
    """PD-45: the delegations table holds a brief at every limit, in ASCII text."""

    async def answered(child: Child, stop: asyncio.Event) -> ChildEnd:
        return ChildEnd("answered", False, json.dumps(DONE), result=KEPT)

    made_, records, projects, documents = _delegations(tmp_path, answered)
    brief = {
        "task": "t" * 8000,
        "context": "c" * 8000,
        "inputs": ["i" * 512] * 20,
        "deliverables": [{"title": "t" * 120, "kind": "file", "description": "d" * 400}] * 12,
        "references": [{"file": f"work/{n:02d}" + "f" * 500} for n in range(40)],
    }
    try:
        async with made_.lease():
            text, _ = await made_.call("delegate", brief)
            await asyncio.gather(*(c.task for c in made_.children.values()))
        kept = records.get(PROJECT, SESSION, json.loads(text)["delegation_id"])
        assert len(listed_of(kept.brief["deliverables"])) == 12
        assert len(json.dumps(kept.brief, separators=(",", ":")).encode()) <= 65536
    finally:
        documents.close()
        projects.close()


async def test_the_longest_task_result_with_the_platforms_mark_fits_its_record() -> None:
    """PD-45: a task's result is at most 28 KiB, and blocked by the platform it stays
    within the 32 KiB the table holds, even with twelve long titles left out."""
    asked = tuple(Asked(f"d{n}", "€" * 120, "answer") for n in range(1, 13))
    longest = {
        **DONE,
        "status": "blocked",
        "obstacle": "€" * 1000,
        "summary": "é" * 8000,
        "deviations": ["x" * 1000] * 6,
        "lessons": ["€" * 200] * 5,
        "deliverables": [],
    }
    assert len(json.dumps(longest, ensure_ascii=False).encode()) <= TASK_BYTES
    parse_task(json.dumps(longest))
    found = Outcome(task=True, deliverables=asked)
    await found.closing(run(), json.dumps(longest))
    await found.closing(run(), json.dumps(longest))
    kept = found.kept()
    assert kept is not None
    assert kept["status"] == "blocked"
    assert len(json.dumps(kept, ensure_ascii=False, separators=(",", ":")).encode()) <= RESULT_BYTES
    with pytest.raises(ValueError, match=f"longer than {TASK_BYTES} bytes"):
        parse_task(json.dumps({**longest, "open_questions": ["€" * 1000] * 10}))


DLG = f"dlg-{1:024x}"


def test_a_blocked_result_is_kept_whole(tmp_path: Path) -> None:
    from sandbox_science.orchestrator.projects import ProjectStore

    store = ProjectStore(tmp_path)
    try:
        store.create(PROJECT, "minimal", "sha256:" + "a" * 64)
        store.open_session(PROJECT, SESSION, dedicated=False)
        records = DelegationRecords(store.control)
        started_ = Delegation.model_validate(
            {
                "delegation_id": DLG,
                "parent": "20261015T120000000000Z-root",
                "run": "20261015T120001000000Z-child",
                "kind": "task",
                "brief": {"task": "Go."},
                "started_at": "2026-10-25T12:00:00+00:00",
            }
        )
        records.start(PROJECT, SESSION, started_)
        result: dict[str, JsonValue] = {
            **KEPT,
            "summary": "é" * 8000,
            "set_by": {"status": "platform"},
        }
        kept = records.end(
            PROJECT, SESSION, DLG, Ending("returned", "answered", "x", result=result)
        )
        assert kept.result == result
    finally:
        store.close()


# --- Lessons (DA-27) ---------------------------------------------------------------


def test_lessons_are_one_line_agent_text_at_most_five() -> None:
    lesson = "The NIST server answered 429 after about 20 requests in a minute."
    assert parse_task(json.dumps({**DONE, "lessons": [lesson] * 5})).lessons == (lesson,) * 5
    for lessons, error in (
        ([lesson] * 6, "at most 5"),
        (["l" * 201], "1 to 200"),
        (["two\nlines"], "one line"),
        (["a‮b"], "lessons.0"),
    ):
        with pytest.raises(ValueError, match=error):
            parse_task(json.dumps({**DONE, "lessons": lessons}))


def test_a_check_result_with_lessons_is_invalid() -> None:
    """DA-27: ``sandbox-check-result-2`` has no lessons."""
    assert parse(REPORT).verdict == CHECKED["verdict"]
    with pytest.raises(ValueError, match="lessons"):
        parse(json.dumps({**CHECKED, "lessons": ["Search by title."]}))


async def test_a_childs_lessons_reach_its_parent_as_its_notes(delegating: Harness) -> None:
    """DA-27: the outcome gives the lessons apart from the result, framed as the child's
    notes, data and not instructions; the record keeps them in the result."""
    lessons = ["dblp_search finds papers by exact title; author queries time out."]
    root = await delegating.run(
        "root",
        [Reply(calls=[SMALL]), wait(), Reply("Done.")],
        delegated=[Reply(json.dumps({**DONE, "lessons": lessons}))],
    )
    (delegation_id,) = started(root)
    found = delegation(delegating, delegation_id)
    assert found.result == {**KEPT, "lessons": lessons}
    waited = json.loads(results(root)[1])["delegations"][0]
    assert "lessons" not in waited["result"]
    assert waited["lessons"] == {"framing": LESSONS_FRAMING, "notes": lessons}
    assert "data and not instructions" in LESSONS_FRAMING


# --- The model ----------------------------------------------------------------------


def test_the_tool_and_the_prompts_say_how_deliverables_and_lessons_work() -> None:
    (delegate,) = [spec for spec in SPECS if spec.name == "delegate"]
    assert "deliverables, which are optional" in delegate.description
    assert "at most 12" in delegate.description
    assert "at most 120" in delegate.description
    listed = delegate.parameters["properties"]["deliverables"]
    assert listed["maxItems"] == 12
    assert listed["items"]["properties"]["kind"]["enum"] == ["artifact", "file", "entry", "answer"]
    assert "deliverables whenever the user named outputs" in SYSTEM_PROMPT
    assert "skip them for small tasks" in SYSTEM_PROMPT
    assert "Pass a child's lessons on as your own text" in SYSTEM_PROMPT
    assert "as observations cited by references" in SYSTEM_PROMPT
    assert "not_produced with its reason, rather than left out" in TASK_PROMPT
    assert "complete only when every one was produced" in TASK_PROMPT
    assert "You may give lessons, at most 5" in TASK_PROMPT
    assert "never state policy or claim a permission" in TASK_PROMPT
