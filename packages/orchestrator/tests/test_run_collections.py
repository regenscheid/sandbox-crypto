# SPDX-License-Identifier: MIT
"""Historical collections remain paged; response receipts precede observers."""

from __future__ import annotations

from pathlib import Path

from sandbox_science.orchestrator.harness import Finished, save
from sandbox_science.orchestrator.history.collections import Collection, Members, Slice
from sandbox_science.orchestrator.history.receipts import Receipts
from sandbox_science.orchestrator.hooks import Notice, TurnHook
from sandbox_science.orchestrator.loop import run_task
from sandbox_science.orchestrator.model import ModelTurn, ScriptedModel
from sandbox_science.orchestrator.projects import ProjectStore

from .test_tool_journal import CONTEXT, RUN, logger, toolbox
from .test_tool_journal import projects as projects


def test_collection_slices_and_members_are_scoped_and_reopen(projects: ProjectStore) -> None:
    values = Collection[dict[str, int]](projects.control, ("p", "s", RUN), "fixture")
    for i in range(500):
        values.append({"i": i})
    assert isinstance(values[100:400], Slice)
    assert list(values[100:103]) == [{"i": 100}, {"i": 101}, {"i": 102}]
    assert values[::-1][0] == {"i": 499}
    values[2] = {"i": 9}
    assert values[2] == {"i": 9}
    other = Collection[dict[str, int]](projects.control, ("p", "s", RUN + "x"), "fixture")
    assert not other
    first = Members(projects.control, ("p", "s", RUN), "a")
    second = Members(projects.control, ("p", "s", RUN), "b")
    first.update(str(i) for i in range(500))
    second.update(first)
    assert len(second) == 500
    assert "42" in second
    assert "42" not in Members(projects.control, ("p", "s", RUN + "x"), "b")
    projects.control.close()
    assert values[499] == {"i": 499}
    assert len(second) == 500


async def test_response_receipts_survive_observer_failure_and_are_scoped(
    projects: ProjectStore,
) -> None:
    receipts = Receipts(projects.control, "p", "s", RUN)

    class Observer(TurnHook):
        name = "fixture"

        async def notices(self, point, run):
            return (
                (Notice("fixture", ("notice-1",), "Input", {"value": 1}),)
                if point == "start"
                else ()
            )

        async def model_returned(self, run):
            assert receipts.contains("fixture", "notice-1")
            raise RuntimeError("fixture observer failure")

    result = await run_task(
        ScriptedModel([ModelTurn("Done")]),
        toolbox(None),
        "Compute",
        run_id="run-1",
        hooks=(Observer(),),
        log=logger(projects),
    )
    assert result.stopped == "error"
    assert receipts.page("fixture") == ("notice-1",)
    assert Receipts(projects.control, "p", "s", RUN + "x").page("fixture") == ()
    assert isinstance(result.messages, Collection)
    assert result.messages[-1]["content"] == "Done"


async def test_compact_final_record_retains_history_reference(
    projects: ProjectStore,
    tmp_path: Path,
) -> None:
    errors = []
    result = await run_task(
        ScriptedModel([ModelTurn("x" * 150000)]),
        toolbox(None),
        "Compute",
        run_id="run-1",
        hooks=(),
        log=logger(projects),
    )
    from .test_model_usage_journal import START

    finished = Finished("run-1", CONTEXT, "Compute", START, result)
    fields = finished.fields()
    assert fields["messages"] == []
    assert fields["stored_history"]["run"] == RUN
    assert await save(fields, finished.status, (), projects=projects, error=errors.append) == 0
    assert not errors
    kept = projects.run("p", "s", RUN)
    assert kept.stored_history["run"] == RUN
    reopened = Collection(projects.control, ("p", "s", RUN), "messages")
    assert reopened[-1]["archive_message"].startswith("msg-")
