# SPDX-License-Identifier: MIT
"""The forms of a task's result that models keep giving, read as what they unambiguously
say (plans-and-delegation spec, PD-18 as amended 2026-11-01, PD-45): ``deliverables``
keyed by number becomes the list in the brief's order, and an artifact given by its path
alone gets the one revision the run's calls recorded of it, each with the platform's
note; every other form stays an error, and nothing is filled in. A submission the schema
refuses has its deliverables checked as far as they can be read, so that its refusal
names every problem, each by its place; and the brief shows each deliverable's produced
form, as submit_result's schema takes it."""

from __future__ import annotations

import json
from typing import Any, cast

import pytest
from pydantic import JsonValue

from sandbox_science.orchestrator.delegation.outcome import (
    ERROR_CHARS,
    KEYED_NOTE,
    MAX_PROBLEMS,
    TASK_BYTES,
    Asked,
    Outcome,
    parse,
    parse_task,
    task_schema,
)
from sandbox_science.orchestrator.delegation.prompt import (
    TASK_PROMPT,
    TASK_VERSION,
    produced_form,
    task_text,
)
from sandbox_science.orchestrator.delegation.records import RESULT_BYTES

from .test_delegation import RESULT as CHECKED
from .test_delegation_deliverables import ASKED, D1, D2, D3, listed_of, recorded, run, submit
from .test_delegation_tasks import DONE

PARTIAL = {**DONE, "status": "partial"}
CHANGED = {"ledger": "ledger_change", "result": {"entries": [{"entry_id": "e-1", "handle": "T-1"}]}}
TABLE = ("d1", "The table", "artifact")


def keyed(*items: dict[str, Any]) -> dict[str, Any]:
    """Deliverables as an object keyed by number, each value without its ``id``."""
    return {str(item["id"]): {k: v for k, v in item.items() if k != "id"} for item in items}


async def closed(answer: dict[str, Any] | str, *, asked: tuple[Asked, ...] = ASKED) -> Outcome:
    """An outcome after one submission, with table.txt recorded at revision 1 and the
    entry T-1 revised."""
    found = Outcome(task=True, deliverables=asked)
    await found.observe_tool(run(), recorded("table.txt"))
    await found.observe_tool(run(), CHANGED)
    await submit(found, answer)
    return found


def kept_of(found: Outcome) -> dict[str, JsonValue]:
    kept = found.kept()
    assert kept is not None, found.errors
    return kept


# --- Deliverables keyed by number ---------------------------------------------------


async def test_deliverables_keyed_by_number_are_listed_in_the_briefs_order_with_a_note() -> None:
    answer = {**PARTIAL, "deliverables": keyed(D3, D1, D2)}
    found = await closed(answer)
    assert found.errors == []
    kept = kept_of(found)
    assert listed_of(kept["deliverables"]) == [
        {**D1, "revision": None, "entry": None, "answer": None, "missing": None, "reason": None},
        {**D2, "path": None, "revision": None, "answer": None, "missing": None, "reason": None},
        {**D3, "path": None, "revision": None, "entry": None, "answer": None, "missing": None},
    ]
    assert kept["platform_notes"] == [KEYED_NOTE]
    assert kept["status"] == "partial"  # the child's own, untouched


async def test_a_keyed_value_naming_its_own_id_and_a_fenced_answer_are_listed() -> None:
    given = {**keyed(D1, D2, D3), "d2": D2}
    found = await closed("```json\n" + json.dumps({**PARTIAL, "deliverables": given}) + "\n```")
    assert kept_of(found)["platform_notes"] == [KEYED_NOTE]


async def test_a_listed_result_gets_no_note() -> None:
    found = await closed({**PARTIAL, "deliverables": [D1, D2, D3]})
    assert "platform_notes" not in kept_of(found)


@pytest.mark.parametrize(
    ("given", "error"),
    [
        ({**keyed(D1, D2, D3), "d1": {**D1, "id": "d2"}}, "key d1 holds another id"),
        ({**keyed(D1, D2), "d3": "not_produced"}, "d3 is no object"),
        ({**keyed(D1, D2), "d3": [D3]}, "d3 is no object"),
        ({"id": "d1", "status": "produced", "path": "table.txt"}, "id is no object"),
    ],
)
async def test_a_keyed_form_with_anything_to_choose_stays_an_error(
    given: dict[str, Any], error: str
) -> None:
    found = await closed({**PARTIAL, "deliverables": given})
    assert found.result is None
    assert "a list with one object for each deliverable" in found.errors[0]
    assert error in found.errors[0]
    assert found.notes == ()


async def test_a_key_given_twice_stays_an_error() -> None:
    inner = ", ".join(f'"{k}": {json.dumps(v)}' for k, v in keyed(D1, D2, D3).items())
    answer = json.dumps(PARTIAL)[:-1] + f', "deliverables": {{{inner}, "d1": {{"status": "x"}}}}}}'
    found = await closed(answer)
    assert found.result is None
    assert "a key given twice" in found.errors[0]


async def test_an_answer_nested_past_what_python_reads_is_an_error_not_a_crash() -> None:
    found = await closed('{"deliverables": ' + "[" * 100_000 + "]" * 100_000 + "}")
    assert found.result is None
    assert found.errors


async def test_a_keyed_deliverable_the_brief_lacks_is_refused_as_any_other() -> None:
    found = await closed({**PARTIAL, "deliverables": keyed(D1, D2, D3, {**D3, "id": "d4"})})
    assert found.result is None
    assert "the brief has no deliverable d4" in found.errors[0]


async def test_a_keyed_result_that_still_leaves_one_out_is_blocked_with_its_note() -> None:
    found = await closed({**PARTIAL, "deliverables": keyed(D1, D2)})
    kept = kept_of(found)
    assert (kept["status"], kept["child_status"]) == ("blocked", "partial")
    assert kept["platform_notes"] == [KEYED_NOTE]


async def test_a_check_result_is_not_listed() -> None:
    """A check's result has no list keyed by an ID: its lists are texts, whose keys and
    values could be read either way, so it is read as it is."""
    with pytest.raises(ValueError, match="examined"):
        parse(json.dumps({**CHECKED, "coverage": {"examined": {"statement": "read it"}}}))


async def test_a_keyed_answer_to_a_brief_without_deliverables_is_still_refused() -> None:
    answer = json.dumps({**DONE, "deliverables": keyed(D3)})
    assert [d.id for d in parse_task(answer).deliverables or ()] == ["d3"]
    found = Outcome(task=True)
    await submit(found, answer)
    assert found.result is None
    assert "the brief lists none" in found.errors[0]


# --- An artifact given by its path ---------------------------------------------------


async def test_an_artifact_by_its_path_gets_the_one_revision_its_calls_recorded() -> None:
    found = await closed(
        {**DONE, "deliverables": [{"id": "d1", "status": "produced", "path": "./table.txt"}]},
        asked=(Asked(*TABLE),),
    )
    kept = kept_of(found)
    (item,) = listed_of(kept["deliverables"])
    assert (item["path"], item["revision"]) == ("./table.txt", 1)
    notes = cast(list[str], kept["platform_notes"])
    assert len(notes) == 1
    assert notes[0].startswith("d1: each artifact gave its path")


async def test_a_path_with_several_revisions_or_none_stays_an_error() -> None:
    by_path = {"id": "d1", "status": "produced", "path": "table.txt"}
    found = Outcome(task=True, deliverables=(Asked(*TABLE),))
    await found.observe_tool(run(), recorded("table.txt", 1))
    await found.observe_tool(run(), recorded("table.txt", 2))
    await submit(found, {**DONE, "deliverables": [by_path]})
    assert found.result is None
    assert "produced, it gives path and revision" in found.errors[0]
    assert "your calls recorded revisions 1, 2 of it" in found.errors[0]
    other = await closed(
        {**DONE, "deliverables": [{**by_path, "path": "other.txt"}]}, asked=(Asked(*TABLE),)
    )
    assert other.result is None
    assert "your calls recorded no revision of it" in other.errors[0]


async def test_a_partial_artifact_or_a_file_by_its_path_is_kept_as_given() -> None:
    partial = {"id": "d1", "status": "partial", "path": "table.txt", "missing": "a = 0"}
    found = await closed({**PARTIAL, "deliverables": [partial]}, asked=(Asked(*TABLE),))
    (item,) = listed_of(kept_of(found)["deliverables"])
    assert item["revision"] is None
    assert "platform_notes" not in kept_of(found)
    file = await closed({**PARTIAL, "deliverables": [D1, D2, D3]})
    assert listed_of(kept_of(file)["deliverables"])[0]["revision"] is None


async def test_both_forms_get_both_notes() -> None:
    asked = (Asked(*TABLE), Asked("d2", "Recheck", "entry"))
    found = await closed(
        {
            **DONE,
            "deliverables": keyed({"id": "d1", "status": "produced", "path": "table.txt"}, D2),
        },
        asked=asked,
    )
    notes = cast(list[str], kept_of(found)["platform_notes"])
    assert notes[0] == KEYED_NOTE
    assert notes[1].startswith("d1: ")


async def test_the_longest_result_with_the_notes_and_the_mark_fits_its_record() -> None:
    """A result at the 28 KiB bound, keyed, with six artifacts given by path and the six
    others of twelve titles of wide characters left out, keeps both notes and the
    platform's block within the 32 KiB the table holds."""
    asked = tuple(Asked(f"d{n}", "€" * 120, "artifact") for n in range(1, 13))
    found = Outcome(task=True, deliverables=asked)
    await found.observe_tool(run(), recorded("t.txt", 999_999_999))
    longest: dict[str, Any] = {
        **DONE,
        "status": "blocked",
        "obstacle": "€" * 1000,
        "summary": "é" * 8000,
        "lessons": ["€" * 200] * 5,
        "deliverables": {f"d{n}": {"status": "produced", "path": "t.txt"} for n in range(1, 7)},
        "open_questions": [],
    }

    def size() -> int:
        return len(parse_task(json.dumps(longest)).model_dump_json().encode())

    while size() < TASK_BYTES - 1000:
        longest["open_questions"].append("x" * 1000)
    longest["open_questions"].append("x" * (TASK_BYTES - size() - 20))
    assert TASK_BYTES - 20 <= size() <= TASK_BYTES
    await submit(found, longest)
    kept = kept_of(found)
    assert kept["status"] == "blocked"
    assert len(cast(list[str], kept["platform_notes"])) == 2
    assert len(json.dumps(kept, ensure_ascii=False, separators=(",", ":")).encode()) <= RESULT_BYTES


# --- A refusal names every problem -------------------------------------------------

RESULTS = "work/dlg-1/out/results.json"
PRIMES = ("d1", "out/results.json", "file")


async def test_an_extra_field_and_a_file_given_a_revision_are_refused_at_once() -> None:
    """The schema refuses the submission for its extra field, and its file deliverable
    gives a revision, which a file does not: the refusal names both, each by its place,
    and one that still gives the revision is refused again, until one gives the form."""
    found = Outcome(task=True, deliverables=(Asked(*PRIMES),))
    await found.observe_tool(run(), recorded(RESULTS))
    item = {"id": "d1", "status": "produced", "path": RESULTS, "revision": 1}
    first = {
        **DONE,
        "content": {"primes": [2, 3, 5], "sum": 10},
        "deliverables": [item],
        "result_files": [RESULTS],
    }
    refused = await submit(found, first)
    assert refused["error"]["problems"] == [
        {"at": "content", "problem": "Extra inputs are not permitted"},
        {"at": "deliverables[0]", "problem": "d1 is a file: it gives path, not revision"},
    ]
    assert refused["error"]["form"] == {
        "required": ["status", "summary", "deliverables"],
        "deliverables": ['{"id": "d1", "status": "produced", "path": "<its path>"}'],
        "otherwise": 'each one partial, with "missing", or not_produced, with "reason"',
    }
    assert found.result is None
    second = {k: v for k, v in first.items() if k != "content"}
    refused = await submit(found, second)
    assert [p["at"] for p in refused["error"]["problems"]] == ["deliverables[0]"]
    assert (found.result, found.submissions) == (None, 2)
    assert found.errors[-1] == "deliverables[0]: d1 is a file: it gives path, not revision"
    recorded_ = await submit(found, {**second, "deliverables": [{**item, "revision": None}]})
    assert recorded_["recorded"].startswith("status complete, 1 deliverables accounted for")
    assert recorded_["values"]["result_files"] == [{"path": RESULTS, "revision": 1}]
    assert found.result is not None, found.errors


async def test_a_refused_submission_names_what_it_leaves_out_and_stays_invalid() -> None:
    """A deliverable no item names is listed in the refusal, but a submission the schema
    refuses is never the platform's block: it replaces nothing; an item that does not
    validate counts as given, and its own errors are the schema's."""
    found = Outcome(task=True, deliverables=ASKED)
    await found.observe_tool(run(), recorded("table.txt"))
    await found.observe_tool(run(), CHANGED)
    unread = {"id": "d2", "status": "produced", "entry": "T-1", "note": "x"}
    refused = await submit(found, {**PARTIAL, "extra": 1, "deliverables": [D1, unread]})
    first, *schema = refused["error"]["problems"]
    assert first == {
        "at": "deliverables",
        "problem": "your result does not account for d3 (Whether every value is 1)",
    }
    # Then the schema's own errors, in the order pydantic reports them.
    assert sorted(schema, key=lambda problem: problem["at"]) == [
        {"at": "deliverables[1].note", "problem": "Extra inputs are not permitted"},
        {"at": "extra", "problem": "Extra inputs are not permitted"},
    ]
    assert (found.result, found.marked) == (None, None)
    await submit(found, {**PARTIAL, "deliverables": [D1, D2]})
    assert found.kept() is not None  # the second submission alone is read for the block


async def test_a_refused_submission_whose_deliverables_cannot_be_read_names_only_its_errors() -> (
    None
):
    for deliverables in ("d1, d2, d3", None, 3):
        found = Outcome(task=True, deliverables=ASKED)
        answer = {**PARTIAL, "extra": 1, "deliverables": deliverables}
        problems = (await submit(found, answer))["error"]["problems"]
        assert {"at": "extra", "problem": "Extra inputs are not permitted"} in problems
        missing = any("does not account for" in p["problem"] for p in problems)
        assert missing == (deliverables is None)


async def test_a_refusal_names_every_problem_within_its_bounds() -> None:
    asked = tuple(Asked(f"d{n}", "Table", "file") for n in range(1, 13))
    found = Outcome(task=True, deliverables=asked)
    items = [{"id": f"d{n}", "status": "produced", "path": "p" * 600} for n in range(1, 13)]
    problems = (await submit(found, {**DONE, "extra": 1, "deliverables": items}))["error"][
        "problems"
    ]
    assert len(problems) == 13 <= MAX_PROBLEMS
    assert [p["at"] for p in problems[:3]] == ["extra", "deliverables[0]", "deliverables[1]"]
    assert all(len(p["problem"]) <= ERROR_CHARS for p in problems)
    (error,) = found.errors
    assert len(error) == ERROR_CHARS
    assert error.startswith("extra: Extra inputs are not permitted; deliverables[0]: d1: your")


# --- What the child is told ----------------------------------------------------------


def test_the_schema_and_the_brief_say_deliverables_is_a_list() -> None:
    """submit_result's schema takes ``deliverables`` as a list of one item for each, each
    of its kind's form, and the brief says so; the prompt names no kind's fields."""
    schema = task_schema(ASKED)
    listed = schema["properties"]["deliverables"]
    assert (listed["type"], listed["minItems"], listed["maxItems"]) == ("array", 3, 3)
    branches = {b["properties"]["id"]["enum"][0]: b for b in listed["items"]["oneOf"]}
    assert set(branches["d1"]["properties"]) == {"id", "status", "path", "missing", "reason"}
    assert set(branches["d2"]["properties"]) == {"id", "status", "entry", "missing", "reason"}
    assert all(b["additionalProperties"] is False for b in branches.values())
    brief = task_text("Tabulate.", "", (), deliverables=[("d1", "The table", "file", "")])
    assert "your submit_result accounts for each in its deliverables, one item for each" in brief


def test_the_task_prompts_rule_is_of_no_one_kind() -> None:
    """The prompt gives no kind's fields, which a child copied into another kind's: the
    schema and each line of the brief give each kind's form."""
    assert TASK_VERSION == "sandbox-delegated-task-21"
    assert '"revision": 2' not in TASK_PROMPT
    assert '"path"' not in TASK_PROMPT
    assert "in the fields of its kind and no others" in TASK_PROMPT


def test_the_brief_shows_each_kinds_produced_form() -> None:
    brief = task_text(
        "Tabulate.",
        "",
        (),
        deliverables=[
            ("d1", "The table", "artifact", ""),
            ("d2", "out/results.json", "file", "the primes and their sum"),
            ("d3", "A task to recheck C-1", "entry", ""),
            ("d4", "Whether every value is 1", "answer", ""),
        ],
    )
    lines = brief.splitlines()
    assert "produced in the form its line shows" in brief
    assert lines[-4:] == [
        '- d1 (artifact): The table. In submit_result, produced: {"id": "d1", "status": '
        '"produced", "path": "<its path>", "revision": <the revision your calls recorded>}',
        "- d2 (file): out/results.json. What counts: the primes and their sum. In "
        'submit_result, produced: {"id": "d2", "status": "produced", "path": "<its path>"}',
        '- d3 (entry): A task to recheck C-1. In submit_result, produced: {"id": "d3", '
        '"status": "produced", "entry": "<its handle>"}',
        '- d4 (answer): Whether every value is 1. In submit_result, produced: {"id": "d4", '
        '"status": "produced", "answer": "<the answer itself>"}',
    ]
    filled = {"<its path>": "t", "<the revision your calls recorded>": "2"}
    filled |= {"<its handle>": "T-1", "<the answer itself>": "Yes."}
    for number, kind in enumerate(("artifact", "file", "entry", "answer"), 1):
        form = produced_form(f"d{number}", kind)
        for placeholder, value in filled.items():
            form = form.replace(placeholder, value)
        assert parse_task(json.dumps({**DONE, "deliverables": [json.loads(form)]})).deliverables


def test_a_line_whose_parts_end_with_their_own_stop_is_joined_cleanly() -> None:
    """A title or a description that ends with a stop gets no second one: the brief once
    read "their sum.. Produced"."""
    brief = task_text(
        "Tabulate.",
        "",
        (),
        deliverables=[("d1", "Is every value 1?", "file", "the primes and their sum.")],
    )
    assert brief.splitlines()[-1].startswith(
        "- d1 (file): Is every value 1? What counts: the primes and their sum. In submit_result"
    )
    assert ".." not in brief
