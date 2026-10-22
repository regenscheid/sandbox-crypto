# SPDX-License-Identifier: MIT
"""Recent complete call/result units stay raw while older bulk remains retrievable."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest

from sandbox_science.contracts import ExecutionContext
from sandbox_science.orchestrator.context.history import (
    Archive,
    Archived,
    Entry,
    FoldRecord,
    Geometry,
    HistoryTool,
    View,
    session_view,
)
from sandbox_science.orchestrator.context.history.record import FORMAT, stubbed
from sandbox_science.orchestrator.projects import ProjectStore

CONTEXT = ExecutionContext(project_id="p", session_id="s", environment_id="env")
SHAPE = Geometry(ceiling=1_000_000, prompt=0, chunk=60_000, scale=1.0)


@pytest.fixture
def archive(tmp_path: Path) -> Iterator[Archive]:
    projects = ProjectStore(tmp_path)
    projects.create("p", "minimal", "sha256:" + "c" * 64)
    projects.open_session("p", "s", dedicated=False)
    try:
        yield Archive(projects.control)
    finally:
        projects.close()


def calls(
    archive: Archive, number: int, *, results: int = 1, chars: int = 6_000, tool: str = "shell"
) -> tuple[Archived, ...]:
    arguments = tuple(
        {
            "id": f"c-{number}-{index}",
            "type": "function",
            "function": {"name": tool, "arguments": "{}"},
        }
        for index in range(results)
    )
    answer = archive.append(
        "p", "s", "r", Entry("assistant", "assistant", "Working.", tool_calls=arguments)
    )
    return (
        answer,
        *(
            archive.append(
                "p",
                "s",
                "r",
                Entry(
                    "tool",
                    "tool_result",
                    f"Exact output {number}:{index} " + "x" * chars,
                    source=tool,
                    call_id=f"c-{number}-{index}",
                ),
            )
            for index in range(results)
        ),
    )


def rows(view: View) -> tuple[Archived, ...]:
    return tuple(part.row for unit in view.units for part in unit if part.row is not None)


def test_recent_floor_keeps_more_than_five_results_and_crosses_only_whole_units(archive):
    for number in range(12):
        calls(archive, number)
    view = session_view(archive, CONTEXT, geometry=SHAPE, per_token=1.0)
    assert sum(part.chars for unit in view.units[-6:] for part in unit) < SHAPE.floor
    assert sum(part.chars for unit in view.units[-7:] for part in unit) >= SHAPE.floor
    protected = view.recent_ids(SHAPE.floor)
    assert protected == frozenset(
        part.row.message_id for unit in view.units[-7:] for part in unit if part.row is not None
    )
    results = tuple(row.message_id for row in rows(view) if row.entry.kind == "tool_result")
    assert stubbed(rows(view)) == results[:-5]
    assert stubbed(rows(view), protected=protected) == results[:-7]


def test_crossing_one_multi_call_unit_keeps_all_its_results_verbatim(archive):
    older = calls(archive, 0)
    latest = calls(archive, 1, results=10)
    view = session_view(archive, CONTEXT, geometry=SHAPE, per_token=1.0)
    protected = view.recent_ids(1)
    assert protected == frozenset(row.message_id for row in latest)
    assert stubbed(rows(view), protected=protected) == (older[-1].message_id,)
    assert all(part.message == part.row.chat() for part in view.units[-1] if part.row is not None)


def test_one_large_newest_result_stays_whole_instead_of_being_cut_to_the_floor(archive):
    for number in range(8):
        calls(archive, number)
    latest = calls(archive, 9, chars=300_000)
    view = session_view(archive, CONTEXT, geometry=SHAPE, per_token=4.0)
    assert view.recent_ids(SHAPE.floor) == frozenset(row.message_id for row in latest)
    protected = view.recent_ids(SHAPE.floor)
    assert latest[-1].message_id not in stubbed(rows(view), protected=protected)
    assert view.units[-1][-1].message == latest[-1].chat()


@pytest.mark.parametrize("floor", [1, 100_000, 1_000_000_000])
def test_short_tail_stays_whole_without_a_new_size_limit(archive, floor):
    latest = calls(archive, 0, chars=300_000)
    view = session_view(archive, CONTEXT, geometry=SHAPE, per_token=1.0)
    assert view.recent_ids(floor) == frozenset(row.message_id for row in latest)
    assert stubbed(rows(view), protected=view.recent_ids(floor)) == ()


@pytest.mark.parametrize("floor", [0, -1, 100_000])
def test_empty_tail_has_no_protected_archive_ids(floor):
    assert View().recent_ids(floor) == frozenset()


def test_zero_floor_keeps_existing_five_result_rule(archive):
    for number in range(8):
        calls(archive, number)
    view = session_view(archive, CONTEXT, geometry=SHAPE, per_token=1.0)
    assert view.recent_ids(0) == frozenset()
    assert stubbed(rows(view), protected=view.recent_ids(0)) == stubbed(rows(view))


def test_recent_protection_preserves_retrieval_and_existing_stub_exemptions(archive):
    results = []
    for number in range(10):
        tool = {1: "history_read", 2: "history_search"}.get(number, "shell")
        results.append(calls(archive, number, tool=tool)[-1])
    view = session_view(archive, CONTEXT, geometry=SHAPE, per_token=1.0)
    assert stubbed(
        rows(view),
        kept=(results[0].message_id,),
        protected=frozenset({results[3].message_id}),
    ) == (results[4].message_id,)


async def test_old_bulk_becomes_exact_history_pointer_while_recent_units_and_originals_survive(
    archive,
):
    first = archive.append("p", "s", "r", Entry("user", "task", "Find exact values."))
    for number in range(12):
        calls(archive, number)
    before = session_view(archive, CONTEXT, geometry=SHAPE, per_token=1.0)
    originals = {row.message_id: row for row in rows(before)}
    protected = before.recent_ids(SHAPE.floor)
    stubs = stubbed(rows(before), protected=protected)
    assert stubs
    record = FoldRecord(
        level=1,
        first=(first.position, first.message_id),
        last=(first.position, first.message_id),
        runs=("r",),
        dates=(first.created, first.created),
        skeleton=("Find exact values.",),
        markers=(),
        keys=(),
        stubs=stubs,
    )
    archive.append(
        "p",
        "s",
        "r",
        Entry(
            "user",
            "fold",
            record.text("fold", whole=True),
            native=record.stored(),
            native_format=FORMAT,
        ),
    )
    after = session_view(archive, CONTEXT, geometry=SHAPE, per_token=1.0)
    latest = {part.row.message_id: part for unit in after.units for part in unit if part.row}
    for identifier in protected:
        assert latest[identifier].message == originals[identifier].chat()
    for identifier in stubs:
        assert f"history_read {identifier}" in latest[identifier].message["content"]
        assert archive.message("p", identifier) == originals[identifier]
    retrieval = HistoryTool(archive, CONTEXT, run="later", delegated=False)
    for identifier in stubs:
        text, _ = await retrieval.call("history_read", {"message": identifier})
        assert json.loads(text)["text"] == originals[identifier].entry.content
