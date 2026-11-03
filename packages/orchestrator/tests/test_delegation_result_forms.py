# SPDX-License-Identifier: MIT
"""The forms of a task's result that models keep giving, read as what they unambiguously
say (plans-and-delegation spec, PD-18 as amended 2026-11-01, PD-45): ``deliverables``
keyed by number becomes the list in the brief's order, and an artifact given by its path
alone gets the one revision the run's calls recorded of it, each with the platform's
note; every other form stays an error, and nothing is filled in. An answer the schema
refuses has its deliverables checked as far as they can be read, so that its one
correction names every problem; and the brief shows each deliverable's produced form."""

from __future__ import annotations

import json
from typing import Any, cast

import pytest
from pydantic import JsonValue

from sandbox_science.orchestrator.delegation.outcome import (
    ERROR_CHARS,
    KEYED_NOTE,
    MAX_ERRORS,
    TASK_BYTES,
    Asked,
    Outcome,
    parse,
    parse_task,
)
from sandbox_science.orchestrator.delegation.prompt import (
    TASK_PROMPT,
    TASK_VERSION,
    produced_form,
    task_text,
)
from sandbox_science.orchestrator.delegation.records import RESULT_BYTES

from .test_delegation import RESULT as CHECKED
from .test_delegation_deliverables import ASKED, D1, D2, D3, listed_of, recorded, run
from .test_delegation_tasks import DONE

PARTIAL = {**DONE, "status": "partial"}
CHANGED = {"ledger": "ledger_change", "result": {"entries": [{"entry_id": "e-1", "handle": "T-1"}]}}
TABLE = ("d1", "The table", "artifact")


def keyed(*items: dict[str, Any]) -> dict[str, Any]:
    """Deliverables as an object keyed by number, each value without its ``id``."""
    return {str(item["id"]): {k: v for k, v in item.items() if k != "id"} for item in items}


async def closed(answer: dict[str, Any] | str, *, asked: tuple[Asked, ...] = ASKED) -> Outcome:
    """An outcome after one closing answer, with table.txt recorded at revision 1 and the
    entry T-1 revised."""
    found = Outcome(task=True, deliverables=asked)
    await found.observe_tool(run(), recorded("table.txt"))
    await found.observe_tool(run(), CHANGED)
    await found.closing(run(), answer if isinstance(answer, str) else json.dumps(answer))
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
    await found.closing(run(), answer)
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
    await found.closing(run(), json.dumps({**DONE, "deliverables": [by_path]}))
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
    await found.closing(run(), json.dumps(longest))
    kept = kept_of(found)
    assert kept["status"] == "blocked"
    assert len(cast(list[str], kept["platform_notes"])) == 2
    assert len(json.dumps(kept, ensure_ascii=False, separators=(",", ":")).encode()) <= RESULT_BYTES


# --- One correction for every problem -----------------------------------------------

RESULTS = "work/dlg-1/out/results.json"
PRIMES = ("d1", "out/results.json", "file")


async def test_an_extra_field_and_a_file_given_a_revision_are_corrected_at_once() -> None:
    """The schema refuses the answer for its extra field, and its file deliverable gives a
    revision, which a file does not: the one correction names both, and an answer that
    still gives the revision is the result's last, missing with its error."""
    found = Outcome(task=True, deliverables=(Asked(*PRIMES),))
    await found.observe_tool(run(), recorded(RESULTS))
    item = {"id": "d1", "status": "produced", "path": RESULTS, "revision": 1}
    first = {
        **DONE,
        "content": {"primes": [2, 3, 5], "sum": 10},
        "deliverables": [item],
        "result_files": [RESULTS],
    }
    closing = await found.closing(run(), json.dumps(first))
    assert closing.action == "continue"
    (notice,) = closing.notices
    (error,) = cast(dict[str, Any], notice.payload)["errors"]
    assert error == (
        "content: Extra inputs are not permitted; d1 is a file: it gives path, not revision"
    )
    assert found.result is None
    second = {k: v for k, v in first.items() if k != "content"}
    closing = await found.closing(run(), json.dumps(second))
    assert (closing.action, closing.notices) == ("accept", ())
    assert (found.result, found.kept()) == (None, None)
    assert found.errors[-1] == "d1 is a file: it gives path, not revision"
    fixed = Outcome(task=True, deliverables=(Asked(*PRIMES),))
    await fixed.observe_tool(run(), recorded(RESULTS))
    await fixed.closing(run(), json.dumps({**second, "deliverables": [{**item, "revision": None}]}))
    assert fixed.result is not None, fixed.errors


async def test_a_refused_answer_names_what_it_leaves_out_and_stays_invalid() -> None:
    """A deliverable no item names is listed in the correction, but an answer the schema
    refuses is never the platform's block: it stays missing; an item that does not
    validate counts as given, and its own errors are the schema's."""
    found = Outcome(task=True, deliverables=ASKED)
    await found.observe_tool(run(), recorded("table.txt"))
    await found.observe_tool(run(), CHANGED)
    unread = {"id": "d2", "status": "produced", "entry": "T-1", "note": "x"}
    closing = await found.closing(
        run(), json.dumps({**PARTIAL, "extra": 1, "deliverables": [D1, unread]})
    )
    payload = cast(dict[str, Any], closing.notices[0].payload)
    assert payload["missing"] == [{"id": "d3", "title": "Whether every value is 1"}]
    (error,) = payload["errors"]
    assert "extra: Extra inputs are not permitted" in error
    assert "deliverables.1.note: Extra inputs are not permitted" in error
    assert (found.result, found.marked) == (None, None)
    await found.closing(run(), json.dumps({**PARTIAL, "deliverables": [D1, D2]}))
    assert found.kept() is not None  # the second answer alone is read for the block


async def test_a_refused_answer_whose_deliverables_cannot_be_read_names_only_its_errors() -> None:
    for deliverables in ("d1, d2, d3", None, 3):
        found = Outcome(task=True, deliverables=ASKED)
        answer = {**PARTIAL, "extra": 1, "deliverables": deliverables}
        closing = await found.closing(run(), json.dumps(answer))
        payload = cast(dict[str, Any], closing.notices[0].payload)
        assert payload["errors"][0].startswith("extra: Extra inputs are not permitted")
        assert ("missing" in payload) == (deliverables is None)


async def test_a_refused_answers_correction_keeps_its_bounds() -> None:
    asked = tuple(Asked(f"d{n}", "Table", "file") for n in range(1, 13))
    found = Outcome(task=True, deliverables=asked)
    items = [{"id": f"d{n}", "status": "produced", "path": "p" * 400} for n in range(1, 13)]
    await found.closing(run(), json.dumps({**DONE, "extra": 1, "deliverables": items}))
    (error,) = found.errors
    assert len(error) == ERROR_CHARS
    assert error.startswith("extra: Extra inputs are not permitted; d1: your calls wrote")
    wide = Outcome(task=True, deliverables=asked)
    short = [{**item, "path": "t"} for item in items]
    await wide.closing(run(), json.dumps({**DONE, "extra": 1, "deliverables": short}))
    assert wide.errors[0].count("; ") == MAX_ERRORS - 1


# --- What the child is told ----------------------------------------------------------


def test_the_prompt_and_the_brief_say_deliverables_is_a_list() -> None:
    assert "deliverables is a list, not an object keyed by number" in TASK_PROMPT
    assert '{"id": "d1", "status": "produced", ...}' in TASK_PROMPT
    assert "the revision number your calls" in TASK_PROMPT
    brief = task_text("Tabulate.", "", (), deliverables=[("d1", "The table", "file", "")])
    assert "your result's deliverables is a list with one object for each" in brief


def test_the_task_prompts_example_is_of_no_one_kind() -> None:
    """The prompt's one example gives no kind's fields, which a child copied into another
    kind's; it names each kind's fields in words, and the brief shows each form."""
    assert TASK_VERSION == "sandbox-delegated-task-19"
    assert '"revision": 2' not in TASK_PROMPT
    assert '"path"' not in TASK_PROMPT
    assert "in the fields of its kind and no others" in TASK_PROMPT
    assert "for a file its path alone" in TASK_PROMPT


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
        '- d1 (artifact): The table. Produced: {"id": "d1", "status": "produced", '
        '"path": "<its path>", "revision": <the revision your calls recorded>}',
        "- d2 (file): out/results.json. What counts: the primes and their sum. Produced: "
        '{"id": "d2", "status": "produced", "path": "<its path>"}',
        '- d3 (entry): A task to recheck C-1. Produced: {"id": "d3", "status": "produced", '
        '"entry": "<its handle>"}',
        '- d4 (answer): Whether every value is 1. Produced: {"id": "d4", "status": '
        '"produced", "answer": "<the answer itself>"}',
    ]
    filled = {"<its path>": "t", "<the revision your calls recorded>": "2"}
    filled |= {"<its handle>": "T-1", "<the answer itself>": "Yes."}
    for number, kind in enumerate(("artifact", "file", "entry", "answer"), 1):
        form = produced_form(f"d{number}", kind)
        for placeholder, value in filled.items():
            form = form.replace(placeholder, value)
        assert parse_task(json.dumps({**DONE, "deliverables": [json.loads(form)]})).deliverables
