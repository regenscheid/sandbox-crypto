# SPDX-License-Identifier: MIT
"""A delegated run returns its result through ``submit_result`` (plans-and-delegation
spec, PD-17 to PD-19, PD-45, PD-46): the tool's schema is the run's brief's, a submission
is checked inside the call and answered with every problem by its place or with what was
recorded, and a run may submit as often as it needs. A closing answer with no result
recorded is told so, three times; an answer that holds a valid result as JSON is taken
for one release, as the lab's children wrote theirs, prose around it; without any, the
parent gets the platform's result, never stronger than the records. A limit keeps a
result, ``partial`` at the limit; the parent's closing names a returned task whose result
is the platform's."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Sequence
from pathlib import Path
from typing import Any, cast

import pytest
from pydantic import JsonValue

from sandbox_science.contracts import ExecutionContext
from sandbox_science.orchestrator import capabilities as caps
from sandbox_science.orchestrator.context.agent import AgentContext
from sandbox_science.orchestrator.context.history import body
from sandbox_science.orchestrator.delegation.hook import outcome, summarized
from sandbox_science.orchestrator.delegation.outcome import (
    CAME,
    LIMIT_OBSTACLE,
    NO_SUBMISSION,
    NUDGES,
    SUBMIT,
    Asked,
    Outcome,
    Target,
    candidates,
    check_schema,
    result_of,
    task_schema,
)
from sandbox_science.orchestrator.delegation.prompt import CHECK_PROMPT, TASK_PROMPT
from sandbox_science.orchestrator.delegation.records import Delegation, DelegationRecords, Ending
from sandbox_science.orchestrator.delegation.specs import SPECS
from sandbox_science.orchestrator.hooks import Notice, RunView, TurnHook
from sandbox_science.orchestrator.loop import IN_ORDER, Transcript, run_task
from sandbox_science.orchestrator.model import ModelTurn, ScriptedModel, ToolCall
from sandbox_science.orchestrator.notices import (
    DELEGATIONS_UNSUBMITTED,
    OUTCOME_LAST_CALL,
    OUTCOME_REPORT,
    OUTCOME_SUBMIT,
    REPORT,
)
from sandbox_science.orchestrator.projects import ProjectStore
from sandbox_science.orchestrator.responses import ResponsesModel
from sandbox_science.orchestrator.tools import HARNESS_TOOL_NAMES, Toolbox
from sandbox_science.orchestrator.worker_client import WorkerClient

from .control_versions import older_store
from .loop_harness import PROJECT, SESSION, Harness, Reply, call, own
from .loop_harness import gateway as gateway
from .loop_harness import harness as harness
from .loop_harness import worker as worker
from .test_delegation import (
    RESULT,
    children,
    configure,
    decide,
    delegation,
    started,
    summary_of,
    wait,
)
from .test_delegation import delegating as delegating
from .test_delegation_deliverables import (
    ASKED,
    D1,
    D2,
    D3,
    IN_AREA,
    RECHECK,
    WRITE,
    recorded,
    submit,
)
from .test_delegation_deliverables import TASK as DELIVERABLES_TASK
from .test_delegation_tasks import DONE, KEPT, TASK
from .test_parent_review import _store, _task
from .test_responses_model import Fake, response, said
from .test_responses_model import fake as fake
from .test_responses_model import gpt as gpt
from .test_tool_journal import toolbox

EACH_KIND = (
    Asked("d1", "The table", "artifact"),
    Asked("d2", "The notes", "file"),
    Asked("d3", "A task to recheck C-1", "entry"),
    Asked("d4", "Whether every value is 1", "answer"),
)
TARGET = Target("ent-1", "C-1", 1)
CHANGED = {"ledger": "ledger_change", "result": {"entries": [{"entry_id": "e-1", "handle": "T-1"}]}}
CONTEXT = ExecutionContext(project_id="p", session_id="s", environment_id="e", actor="agent")


def run(
    *, halted: str | None = None, turns: int = 1, max_turns: int = 100, messages: Any = ()
) -> Transcript:
    """A run as a hook sees it: at its report round when ``halted`` names a limit."""
    return Transcript(
        context=AgentContext(messages=list(messages)),
        halted=halted,
        turns=turns,
        max_turns=max_turns,
    )


async def made(asked: tuple[Asked, ...] = ASKED) -> Outcome:
    """A task's outcome whose run recorded table.txt at revision 1 and revised T-1."""
    found = Outcome(task=True, deliverables=asked)
    await found.observe_tool(run(), recorded("table.txt"))
    await found.observe_tool(run(), CHANGED)
    return found


PARTIAL = {**DONE, "status": "partial", "deliverables": [D1, D2, D3]}


def objects(schema: object) -> list[dict[str, Any]]:
    """Every object schema in ``schema``."""
    found: list[dict[str, Any]] = []
    if isinstance(schema, dict):
        node = cast(dict[str, Any], schema)
        if node.get("type") == "object":
            found.append(node)
        for value in node.values():
            found.extend(objects(value))
    elif isinstance(schema, list):
        for value in cast(list[object], schema):
            found.extend(objects(value))
    return found


# --- The schema ---------------------------------------------------------------------


def test_a_tasks_schema_takes_each_deliverable_in_its_kinds_form() -> None:
    """Each kind's item gives its own fields and no other's, its id among the brief's
    numbers of that kind; the list holds one item for each deliverable."""
    schema = task_schema(EACH_KIND)
    listed = schema["properties"]["deliverables"]
    assert (listed["type"], listed["minItems"], listed["maxItems"]) == ("array", 4, 4)
    branches = listed["items"]["oneOf"]
    shown = {
        branch["properties"]["id"]["enum"][0]: sorted(branch["properties"]) for branch in branches
    }
    assert shown == {
        "d1": sorted(["id", "status", "path", "revision", "missing", "reason"]),
        "d2": sorted(["id", "status", "path", "missing", "reason"]),
        "d3": sorted(["id", "status", "entry", "missing", "reason"]),
        "d4": sorted(["id", "status", "answer", "missing", "reason"]),
    }
    assert all(branch["required"] == ["id", "status"] for branch in branches)
    assert schema["required"] == ["status", "summary", "deliverables"]
    assert all(node["additionalProperties"] is False for node in objects(schema))
    # Two deliverables of one kind share its form; a brief with one kind needs no oneOf.
    two = task_schema((Asked("d1", "A", "file"), Asked("d2", "B", "file")))
    assert two["properties"]["deliverables"]["items"]["properties"]["id"]["enum"] == ["d1", "d2"]
    assert "deliverables" not in task_schema(())["properties"]
    assert task_schema(())["required"] == ["status", "summary"]


def test_a_checks_schema_is_its_results_naming_its_brief() -> None:
    schema = check_schema(TARGET)
    properties = schema["properties"]
    assert (properties["entry"]["enum"], properties["revision"]["enum"]) == (["C-1"], [1])
    assert schema["required"] == ["entry", "revision", "status", "verdict", "summary", "coverage"]
    assert properties["coverage"]["properties"]["examined"]["minItems"] == 1
    assert properties["verdict"]["enum"] == ["confirmed", "refuted", "inconclusive"]
    assert "lessons" not in properties
    assert "deliverables" not in properties
    assert all(node["additionalProperties"] is False for node in objects(schema))


async def test_the_gpt_route_sends_the_schema_whole_and_not_strict(
    fake: Fake, gpt: ResponsesModel
) -> None:
    """The Responses route declares every tool ``strict: false``, so the schema's
    ``oneOf`` goes as it is, with nothing the strict mode would refuse."""
    spec = Outcome(task=True, deliverables=EACH_KIND).spec
    fake.then(response(said("Hello.")))
    await gpt.complete([{"role": "user", "content": "Go."}], [spec])
    (body_,) = fake.bodies
    (tool,) = body_["tools"]
    assert (tool["name"], tool["strict"]) == (SUBMIT, False)
    assert tool["parameters"] == spec.parameters
    assert len(tool["parameters"]["properties"]["deliverables"]["items"]["oneOf"]) == 4


def test_only_a_delegated_run_is_offered_submit_result_and_its_name_is_reserved() -> None:
    assert SUBMIT in caps.RESERVED_NAMES
    assert SUBMIT in IN_ORDER  # a later submission replaces an earlier one: in order
    assert SUBMIT in HARNESS_TOOL_NAMES
    child = Toolbox(cast(WorkerClient, object()), CONTEXT, delegated=True, outcome=Outcome())
    main = Toolbox(cast(WorkerClient, object()), CONTEXT)
    assert SUBMIT in child.entries
    assert SUBMIT not in main.entries
    entry = child.entries[SUBMIT]
    assert (entry.effect, entry.raw) == ("read", True)  # the plan fence lets it pass


# --- A submission ---------------------------------------------------------------------


async def test_a_submission_through_the_toolbox_is_recorded_and_echoed() -> None:
    found = await made()
    toolbox = Toolbox(cast(WorkerClient, object()), CONTEXT, delegated=True, outcome=found)
    arguments = {"purpose_note": "Submitting the task's result", **PARTIAL}
    outcome = await toolbox.call(ToolCall("c1", SUBMIT, json.dumps(arguments)), "op-1")
    echoed = json.loads(outcome.text)
    assert echoed["recorded"] == (
        "status partial, 3 deliverables accounted for (2 produced, 1 not produced), "
        "0 result files pinned; your run can end now"
    )
    assert echoed["values"]["deliverables"] == [D1, D2, {"id": "d3", "status": "not_produced"}]
    assert "submit again to change any" in echoed["next"].lower()
    assert outcome.record["submitted"] == {"submission": 1, "recorded": True}
    assert (found.submissions, found.source, found.valid) == (1, SUBMIT, True)
    closing = await found.closing(run(), "Done.")
    assert (closing.action, closing.notices) == ("accept", ())
    kept = found.kept()
    assert kept is not None
    assert "platform_notes" not in kept  # the tool's result carries no note


async def test_a_later_submission_replaces_an_earlier_one_and_a_refused_one_nothing() -> None:
    found = await made()
    assert "recorded" in await submit(found, PARTIAL)
    complete = {**DONE, "deliverables": [D1, D2, {"id": "d3", "status": "produced", "answer": "Y"}]}
    assert "recorded" in await submit(found, complete)
    refused = await submit(found, {**complete, "status": "finished"})
    assert refused["error"]["problems"][0]["at"] == "status"
    kept = found.kept()
    assert kept is not None
    assert kept["status"] == "complete"
    summary = cast(dict[str, Any], found.summary())
    assert summary["submissions"] == 3
    assert len(found.errors) == 1


async def test_a_checks_submission_names_its_briefs_entry_and_revision() -> None:
    found = Outcome(TARGET)
    refused = await submit(found, {**RESULT, "revision": 2})
    assert refused["error"]["problems"] == [
        {
            "at": "result",
            "problem": "the result names C-1 at revision 2; the brief names C-1 at revision 1",
        }
    ]
    assert refused["error"]["form"] == {
        "required": ["entry", "revision", "status", "verdict", "summary", "coverage"],
        "entry": "C-1",
        "revision": 1,
    }
    echoed = await submit(found, RESULT)
    assert echoed["recorded"].startswith("verdict confirmed, status complete, 2 parts examined")


# --- The closing answer ---------------------------------------------------------------

SUMMARY = {**DONE, "summary": "I tabulated a^16 mod 17 and saved table.txt."}
WHOLE = json.dumps(SUMMARY, indent=2)


@pytest.mark.parametrize(
    "answer",
    [
        # A sentence of prose, then the result in a fenced block.
        f"Everything is in place; the result follows.\n\n```json\n{WHOLE}\n```",
        # Prose, the fenced result, then prose after it.
        f"All checks pass.\n\n```json\n{WHOLE}\n```\n\nIn short:\n1. The table is saved.",
        # Prose, a fenced stub, more prose, then the whole result, unfenced.
        'Let me report.\n\n```json\n{"status": "complete"}\n```\n\nThe report:\n\n'
        + json.dumps(SUMMARY),
    ],
)
async def test_the_labs_first_answer_shapes_are_taken_through_the_fallback(answer: str) -> None:
    """Lab round 2: every child wrote prose before its JSON, which the strict reader
    refused; for one release the platform reads such an answer leniently and keeps its
    result, its note saying it came in the answer, not through the tool."""
    found = Outcome(task=True)
    closing = await found.closing(run(), answer)
    assert (closing.action, found.nudges) == ("accept", 0)
    kept = found.kept()
    assert kept is not None, found.errors
    assert kept["summary"] == SUMMARY["summary"]
    assert kept["platform_notes"] == [CAME["answer"]]
    assert found.source == "answer"


def test_the_lenient_reader_takes_the_last_fenced_block_then_the_last_object() -> None:
    assert candidates('No JSON here, only {braces} and a {"list": [1') == ()
    assert candidates('Done.\n```python\nprint({"a": 1})\n```') == ('{"a": 1}',)
    assert candidates('```json\n{"a": 1}\n```\n```\n{"b": 2}\n```\nThen {"c": 3}.') == (
        '{"b": 2}',
        '{"c": 3}',
    )
    assert candidates('{"a": {"b": 1}} and {"c": 2}') == ('{"c": 2}',)


async def test_an_answer_with_no_result_is_told_three_times_then_the_platform_writes_one() -> None:
    """PD-18: three notices restate that the result goes through submit_result, with the
    form; the fourth answer stands, and the result is the platform's: blocked, each
    deliverable not produced, no result files, the answer its summary and a note naming
    the files the run's calls wrote."""
    found = Outcome(task=True, deliverables=ASKED)
    await found.observe_tool(run(), recorded("work/dlg-1/table.txt", 2))
    for n in range(1, NUDGES + 1):
        closing = await found.closing(run(), f"Answer {n}: the table is saved.")
        assert closing.action == "continue"
        (notice,) = closing.notices
        assert notice.framing == OUTCOME_SUBMIT.text
        payload = cast(dict[str, Any], notice.payload)
        assert (payload["notice"], payload["of"], payload["tool"]) == (n, NUDGES, SUBMIT)
        assert payload["form"]["required"] == ["status", "summary", "deliverables"]
        assert "answer_problems" not in payload  # no JSON in the answer
    closing = await found.closing(run(), "I am done.")
    assert (closing.action, closing.notices) == ("accept", ())
    kept = found.kept()
    assert kept == {
        "status": "blocked",
        "summary": "I am done.",
        "obstacle": NO_SUBMISSION,
        "deviations": [],
        "open_questions": [],
        "dismissals": [],
        "obstructions": [],
        "result_files": [],
        "deliverables": [
            {
                "id": asked.id,
                "status": "not_produced",
                "path": None,
                "revision": None,
                "entry": None,
                "answer": None,
                "missing": None,
                "reason": NO_SUBMISSION,
            }
            for asked in ASKED
        ],
        "set_by": dict.fromkeys(("status", "obstacle", "summary", "deliverables"), "platform"),
        "platform_notes": [
            "no result was submitted: the platform wrote this result, its summary the run's "
            "closing answer as the run wrote it",
            "the run's calls wrote or published work/dlg-1/table.txt (revision 2)",
        ],
    }
    assert found.nudges == NUDGES


async def test_an_answers_json_that_does_not_check_is_named_in_the_notice() -> None:
    found = await made()
    closing = await found.closing(run(), "Here:\n" + json.dumps({**PARTIAL, "status": "done"}))
    payload = cast(dict[str, Any], closing.notices[0].payload)
    assert payload["answer_problems"][0]["at"] == "status"


async def test_a_notice_the_run_has_no_turn_for_leaves_the_answer_standing() -> None:
    found = Outcome(task=True)
    closing = await found.closing(run(halted="turn_limit"), "The table is saved.")
    assert (closing.action, found.nudges) == ("accept", 0)
    kept = result_of(found, "turn_limit", False)
    assert kept is not None
    assert kept["obstacle"] == "the run reached a limit (turn_limit) without submitting a result"


# --- At a limit -------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "verdict", "kept_status", "set_by"),
    [
        ("complete", "confirmed", "partial", {"obstacle": "platform", "status": "platform"}),
        ("partial", "inconclusive", "partial", {"obstacle": "platform"}),
    ],
)
async def test_a_result_at_a_limit_is_partial_and_the_platforms_mark_says_so(
    status: str, verdict: str, kept_status: str, set_by: dict[str, str]
) -> None:
    """PD-19: a limit no longer discards a result; one that said complete is partial,
    with an obstacle naming the limit, both the platform's, the child's own beside them;
    a check's confirmed, which only a complete check gives, is inconclusive."""
    task = await made()
    task_result = {**PARTIAL, "status": status} if status == "partial" else None
    complete = {**DONE, "deliverables": [D1, D2, {"id": "d3", "status": "produced", "answer": "Y"}]}
    assert "recorded" in await submit(task, task_result or complete)
    kept = result_of(task, "resource_report", False)
    assert kept is not None
    assert kept["status"] == kept_status
    assert kept["obstacle"] == LIMIT_OBSTACLE.format(limit="resource_report")
    assert kept["set_by"] == set_by
    assert ("child_status" in kept) == (status == "complete")
    check = Outcome(TARGET)
    coverage = RESULT["coverage"] if verdict == "confirmed" else {"examined": ["a"]}
    assert "recorded" in await submit(
        check, {**RESULT, "status": status, "verdict": verdict, "coverage": coverage}
    )
    shown = result_of(check, "turn_limit", False)
    assert shown is not None
    assert (shown["status"], shown["verdict"]) == (kept_status, "inconclusive")
    assert ("child_verdict" in shown) == (verdict == "confirmed")
    assert result_of(check, "cancelled", False) is None
    assert result_of(check, "turn_limit", True) is None


async def test_a_blocked_result_at_a_limit_keeps_its_own_obstacle() -> None:
    found = Outcome(task=True)
    blocked = {**DONE, "status": "blocked", "obstacle": "No sage."}
    assert "recorded" in await submit(found, blocked)
    kept = result_of(found, "turn_limit", False)
    assert kept is not None
    assert (kept["status"], kept["obstacle"]) == ("blocked", "No sage.")
    assert kept["platform_notes"] == ["the run reached a limit (turn_limit) before it ended"]


async def test_a_report_gives_the_json_it_would_have_submitted() -> None:
    """At a limit's report round, where no tool runs, the report's JSON is the result,
    replacing an earlier submission; a report call of submit_result, which does not
    run, is read the same way."""
    found = await made()
    assert "recorded" in await submit(found, PARTIAL)
    complete = {**DONE, "deliverables": [D1, D2, {"id": "d3", "status": "produced", "answer": "Y"}]}
    closing = await found.closing(
        run(halted="turn_limit"), "Out of turns; my result:\n" + json.dumps(complete)
    )
    assert closing.action == "accept"
    kept = result_of(found, "turn_limit", False)
    assert kept is not None
    assert (kept["status"], kept["child_status"]) == ("partial", "complete")
    assert kept["platform_notes"] == [CAME["report"]]
    other = await made()
    arguments = json.dumps({"purpose_note": "Submitting at the limit", **PARTIAL})
    reply = {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {"id": "c", "type": "function", "function": {"name": SUBMIT, "arguments": arguments}}
        ],
    }
    await other.model_returned(run(halted="turn_limit", messages=[reply]))
    assert other.source == "report"
    kept = result_of(other, "turn_limit", False)
    assert kept is not None
    assert kept["status"] == "partial"
    # A regular turn's reply is never read so.
    third = await made()
    await third.model_returned(run(messages=[reply]))
    assert third.result is None


async def test_a_reports_text_is_the_platforms_summary_when_a_hook_stops_the_closing() -> None:
    """A parent's stop ends the run at its report's closing before this hook is asked:
    the report's text, seen as the model returned it, is still the summary."""
    found = Outcome(task=True)
    reply = {"role": "assistant", "content": "Half the table is done."}
    await found.model_returned(run(halted="resource_report", messages=[reply]))
    kept = result_of(found, "resource_report", False)
    assert kept is not None
    assert kept["summary"] == "Half the table is done."


async def test_the_last_call_with_tools_before_the_turn_limit_says_to_submit() -> None:
    found = Outcome(task=True)
    assert await found.notices("turn", run(turns=3, max_turns=5)) == ()
    (notice,) = await found.notices("turn", run(turns=4, max_turns=5))
    assert notice.framing == OUTCOME_LAST_CALL.text
    assert cast(dict[str, Any], notice.payload)["tool"] == SUBMIT
    assert await found.notices("turn", run(turns=4, max_turns=5)) == ()  # once
    submitted = Outcome(task=True)
    assert "recorded" in await submit(submitted, DONE)
    assert await submitted.notices("turn", run(turns=4, max_turns=5)) == ()


async def test_a_task_stopped_by_its_parent_keeps_its_submitted_result_partial(
    delegating: Harness,
) -> None:
    """PD-19, D102: a task submits a complete result, goes on, checks in at its soft
    cap, and its parent stops it: its report round's answer is its report, and its
    result stands, partial at the limit, the platform's mark beside the child's status."""
    configure(delegating, depth=1, task_tokens=50_000, max_token_grant=100_000)
    root = await delegating.run(
        "root",
        [Reply(calls=[TASK]), wait(), decide("stop"), wait(), Reply("Done.")],
        delegated=[
            # Its first soft cap goes on for one step; its next waits for its parent.
            Reply(calls=[call(SUBMIT, **DONE)], tokens=60_000),
            Reply(calls=[call("no_such_tool")], tokens=60_000),
            Reply("Stopped: the table is saved."),
        ],
        status=3,
    )
    (delegation_id,) = started(root)
    found = delegation(delegating, delegation_id)
    assert (found.ended, found.stopped) == ("limit_reached", "resource_report")
    assert found.result == {
        **KEPT,
        "status": "partial",
        "obstacle": LIMIT_OBSTACLE.format(limit="resource_report"),
        "child_status": "complete",
        "set_by": {"obstacle": "platform", "status": "platform"},
    }
    (child,) = children(delegating, root)
    assert summary_of(child)["result"] == found.result


# --- The parent's side ----------------------------------------------------------------


async def test_a_parents_closing_names_a_returned_task_with_the_platforms_result(
    delegating: Harness,
) -> None:
    """PD-46: the parent's closing answer, with a returned task whose child submitted no
    result awaiting its review, gets one notice naming it, once; its run's end accepts it."""
    root = await delegating.run(
        "root",
        [Reply(calls=[TASK]), wait(), Reply("Done."), Reply("Reviewed: it wrote no result.")],
        delegated=[Reply("The table is saved.") for _ in range(NUDGES + 1)],
    )
    told = [
        json.loads(text[len(DELEGATIONS_UNSUBMITTED.text) :])
        for message in own(root)
        if (text := body(str(message.get("content", "")))).startswith(DELEGATIONS_UNSUBMITTED.text)
    ]
    (delegation_id,) = started(root)
    assert [[item["delegation_id"] for item in notice] for notice in told] == [[delegation_id]]
    assert root.final_text == "Reviewed: it wrote no result."
    found = delegation(delegating, delegation_id)
    assert found.result is not None
    assert cast(dict[str, Any], found.result["set_by"])["summary"] == "platform"
    assert (found.review, found.review_reason) == ("accepted", "parent_ended")
    assert summarized(found)["result_by"] == "platform"


async def test_the_parent_reads_why_a_childs_submissions_were_refused() -> None:
    """With the platform's result, the outcome a parent reads gives the problems of the
    child's submissions that did not check, beside the result."""
    found = await made()
    await submit(found, {**PARTIAL, "status": "done"})
    kept = found.kept()
    assert kept is not None
    record = Delegation.model_validate(
        {
            "delegation_id": "dlg-1",
            "parent": "root",
            "run": "child",
            "kind": "task",
            "brief": {"task": "Go."},
            "started_at": "2026-11-03T12:00:00+00:00",
            "ended": "returned",
            "stopped": "answered",
            "ended_at": "2026-11-03T12:01:00+00:00",
            "result": kept,
            "errors": found.errors,
        }
    )
    shown = outcome(record)
    assert shown["errors"] == found.errors
    assert summarized(record)["result_by"] == "platform"


def test_the_delegate_tool_says_when_to_delegate() -> None:
    (delegate,) = [spec for spec in SPECS if spec.name == "delegate"]
    text = delegate.description
    assert "a finite piece of work of another kind than yours" in text
    assert "not volume: fewer children is usually right" in text
    assert "message that child (message_child) rather than start a new one" in text
    assert "checks are the exception" in text


def test_the_prompts_give_one_rule_for_a_question_and_name_the_tool() -> None:
    """A delegated run asks its parent and keeps working; it submits blocked only when its
    parent does not answer; and its result goes through submit_result."""
    for prompt in (TASK_PROMPT, CHECK_PROMPT):
        assert "nobody answers questions" not in prompt
        assert "stop with status blocked" not in prompt
        assert "ask your parent with message_parent and keep working on what does not" in prompt
        assert "only when your parent does not answer" in prompt
        assert "Your result reaches your parent only through submit_result" in prompt
        assert "Your final answer is your result" not in prompt
        assert "the JSON object you would have submitted" in prompt


# --- The records --------------------------------------------------------------------------


def test_a_version_88_store_keeps_a_limit_reached_result_once_migrated(tmp_path: Path) -> None:
    """Version 89: a delegation that reached a limit keeps its result, and its numbered
    result too; any other ending still keeps none."""
    current = _store(tmp_path / "current")
    try:
        records = DelegationRecords(current.control)
        for n in range(3):
            records.start(PROJECT, SESSION, _task(n))
        records.end(PROJECT, SESSION, _task(0).delegation_id, Ending("returned", "answered"))
    finally:
        current.close()
    target = tmp_path / "old"
    target.mkdir()
    older_store(tmp_path / "current" / "control.sqlite3", target / "control.sqlite3", 88)
    result: dict[str, JsonValue] = {**KEPT, "status": "partial"}
    with sqlite3.connect(target / "control.sqlite3") as db, pytest.raises(sqlite3.IntegrityError):
        db.execute(
            "UPDATE delegations SET ended='limit_reached', stopped='turn_limit', ended_at='x', "
            "result=?, results=results+1 WHERE delegation_id=?",
            (json.dumps(result), _task(1).delegation_id),
        )
    migrated = ProjectStore(target)
    try:
        records = DelegationRecords(migrated.control)
        ended = records.end(
            PROJECT,
            SESSION,
            _task(1).delegation_id,
            Ending("limit_reached", "turn_limit", result=result),
        )
        assert ended.result == result
        (kept,) = records.results(PROJECT, SESSION, ended.delegation_id)
        assert kept.result == result
        assert records.get(PROJECT, SESSION, _task(0).delegation_id).results == 1
        failed = records.end(
            PROJECT, SESSION, _task(2).delegation_id, Ending("failed", "error", result=result)
        )
        assert failed.result is None
    finally:
        migrated.close()


# --- The report round's notice and the order of a reply's calls --------------------------


class Reporting(TurnHook):
    """A hook with a notice of its own for the report round."""

    name = "reporting"

    def __init__(self, size: int = 10) -> None:
        self.size = size
        self.asked: list[str] = []

    async def report_notices(self, run: RunView, limit: str) -> Sequence[Notice]:
        self.asked.append(limit)
        return (Notice(self.name, (), "Write the result: ", {"pad": "x" * self.size}),)


async def unused(request: Any) -> Any:
    raise AssertionError("no tool runs in these fixtures")


@pytest.mark.parametrize("size", [10, 200_000])
async def test_the_report_round_offers_a_hooks_notices_after_its_own(size: int) -> None:
    """RS-6f, PD-19: before the report round at a limit, each hook gives its notices for
    that call, which follow the report's own; with no room for them, the report's goes
    alone."""
    hook = Reporting(size)
    first = ModelTurn(None, tool_calls=(ToolCall("c1", "arithmetic", "{}"),))
    result = await run_task(
        ScriptedModel([first, ModelTurn("My report.")]),
        toolbox(unused),
        task="Fixture",
        run_id="run",
        max_turns=1,
        max_input_chars=150_000,
        hooks=(hook,),
    )
    assert (result.stopped, result.final_text) == ("turn_limit", "My report.")
    assert hook.asked == ["turn_limit"]
    told = [str(m.get("content")) for m in result.context.messages if m.get("role") == "user"]
    reported = [n for n, text in enumerate(told) if text.startswith(REPORT.text)]
    written = [n for n, text in enumerate(told) if text.startswith("Write the result: ")]
    assert len(reported) == 1
    assert written == ([reported[0] + 1] if size == 10 else [])


async def test_a_delegated_report_round_asks_for_the_json_and_takes_it(
    delegating: Harness,
) -> None:
    """PD-19: a task its parent stopped at a check-in, which submitted nothing, is told
    in its report round, after the report's notice, to write the JSON it would have
    submitted; the JSON its report gives is its result, partial at the limit."""
    configure(delegating, depth=1, task_tokens=50_000, max_token_grant=100_000)
    report = "Stopped here. My result:\n```json\n" + json.dumps(DONE) + "\n```"
    root = await delegating.run(
        "root",
        [Reply(calls=[TASK]), wait(), decide("stop"), wait(), Reply("Done.")],
        delegated=[
            Reply(calls=[call("no_such_tool")], tokens=60_000),
            Reply(calls=[call("no_such_tool")], tokens=60_000),
            Reply(report),
        ],
        status=3,
    )
    last = [
        body(str(m.get("content")))
        for m in delegating.gateway.children[-1]
        if m.get("role") == "user"
    ]
    assert last[-2].startswith(REPORT.text)
    assert last[-1].startswith(OUTCOME_REPORT.text)
    payload = json.loads(last[-1][len(OUTCOME_REPORT.text) :])
    assert (payload["limit"], payload["recorded"]) == ("resource_report", False)
    assert payload["form"]["required"] == ["status", "summary"]
    (delegation_id,) = started(root)
    found = delegation(delegating, delegation_id)
    assert (found.ended, found.report) == ("limit_reached", report)
    assert found.result is not None
    assert (found.result["status"], found.result["child_status"]) == ("partial", "complete")
    assert found.result["platform_notes"] == [CAME["report"]]


async def test_a_file_written_in_a_reply_is_recorded_before_its_submit_result_runs(
    delegating: Harness,
) -> None:
    """A reply that writes a file and records a ledger entry, then submits the result
    naming both, in the same reply: the calls that change something run alone, in the
    reply's order, and each is observed before the next runs, so the submission sees what
    they made (RS-6f's calls of one reply, PD-45)."""
    result = {**PARTIAL, "deliverables": [IN_AREA, D2, D3], "result_files": [IN_AREA["path"]]}
    root = await delegating.run(
        "root",
        [Reply(calls=[DELIVERABLES_TASK]), wait(), Reply("Done.")],
        delegated=[
            Reply(calls=[WRITE, RECHECK, call(SUBMIT, **result)]),
            Reply("Done: the table and the recheck task."),
        ],
    )
    (child,) = children(delegating, root)
    summary = summary_of(child)
    assert (summary["submissions"], summary["errors"], summary["source"]) == (
        1,
        [],
        "submit_result",
    )
    (delegation_id,) = started(root)
    found = delegation(delegating, delegation_id)
    assert found.result is not None
    assert found.result["status"] == "partial"
    assert found.files is not None
    (pinned,) = cast(list[dict[str, Any]], found.files["results"])
    assert pinned["path"] == f"work/{delegation_id}/table.txt"
