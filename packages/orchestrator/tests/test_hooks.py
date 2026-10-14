# SPDX-License-Identifier: MIT
"""The loop's delivery rules for turn hooks, with hooks of the tests' own."""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from typing import cast

import pytest

from sandbox_science.contracts import ExecutionContext
from sandbox_science.orchestrator import cli
from sandbox_science.orchestrator.hooks import Closing, Notice, Point, RunView, TurnHook
from sandbox_science.orchestrator.loop import Transcript, run_task
from sandbox_science.orchestrator.model import ModelTurn, ScriptedModel, ToolCall
from sandbox_science.orchestrator.projects import ProjectStore, RunRecord
from sandbox_science.orchestrator.tools import Toolbox
from sandbox_science.orchestrator.worker_client import WorkerClient

CONTEXT = ExecutionContext(project_id="p", session_id="s", environment_id="env")


def toolbox() -> Toolbox:
    return Toolbox(cast(WorkerClient, object()), CONTEXT)


def call() -> ModelTurn:
    """A call of a tool that does not exist: its result is an error, made locally."""
    return ModelTurn(None, (ToolCall("c1", "no_such_tool", "{}"),))


def tool_result(transcript: Transcript) -> str:
    return next(str(m["content"]) for m in transcript.messages if m["role"] == "tool")


class AtTool(TurnHook):
    """Offers one notice at the first tool point."""

    def __init__(self, source: str, key: str, payload: object) -> None:
        self.name, self.key, self.payload = source, key, payload
        self.done = False

    async def notices(self, point: Point, run: RunView) -> tuple[Notice, ...]:
        if point != "tool" or self.done:
            return ()
        self.done = True
        return (Notice(self.name, (f"{self.name}-1",), self.key, self.payload),)


async def test_notices_at_a_tool_point_keep_their_payloads_and_the_result() -> None:
    first, second = AtTool("a", "notices", ["a"]), AtTool("b", "notices", ["b"])
    hijack = AtTool("c", "tool_result", "replaced")
    model = ScriptedModel([call(), ModelTurn("done")])
    transcript = await run_task(model, toolbox(), "go", run_id="r", hooks=[first, second, hijack])
    seen = json.loads(tool_result(transcript))
    # A key already taken is refused, so no payload is lost under it.
    assert seen["notices"] == ["a"]
    assert "error" in json.loads(seen["tool_result"])
    assert transcript.delivered_ids("a") == {"a-1"}
    assert transcript.delivered_ids("b") == transcript.delivered_ids("c") == frozenset()


async def test_a_notice_too_long_for_a_tool_result_is_not_attached() -> None:
    box = toolbox()
    big = AtTool("a", "big", "x" * box.output_chars)
    transcript = await run_task(
        ScriptedModel([call(), ModelTurn("done")]), box, "go", run_id="r", hooks=[big]
    )
    assert "error" in json.loads(tool_result(transcript))
    assert transcript.delivered_ids("a") == frozenset()


class Failing(TurnHook):
    name = "failing"

    async def notices(self, point: Point, run: RunView) -> tuple[Notice, ...]:
        if point == "tool":
            raise RuntimeError("the source is down")
        return ()


async def test_a_hook_failing_at_a_tool_point_costs_only_its_notices() -> None:
    model = ScriptedModel([call(), ModelTurn("done")])
    transcript = await run_task(
        model, toolbox(), "go", run_id="r", hooks=[Failing(), AtTool("a", "a", [1])]
    )
    assert (transcript.failure, transcript.final_text) == (None, "done")
    assert json.loads(tool_result(transcript))["a"] == [1]
    assert transcript.delivered_ids("a") == {"a-1"}


class Continues(TurnHook):
    def __init__(self, name: str, *, notices: bool) -> None:
        self.name, self.with_notices = name, notices
        self.asked = 0

    async def closing(self, run: RunView, answer: str | None) -> Closing:
        self.asked += 1
        if self.asked > 1 or run.turns_left <= 0:
            return Closing()
        found = (Notice(self.name, (f"{self.name}-1",), "New: ", [1]),) if self.with_notices else ()
        return Closing("continue", found)


async def test_a_continue_without_notices_accepts_and_later_hooks_are_asked() -> None:
    empty, later = Continues("empty", notices=False), Continues("later", notices=True)
    model = ScriptedModel([ModelTurn("first"), ModelTurn("second")])
    transcript = await run_task(model, toolbox(), "go", run_id="r", hooks=[empty, later])
    assert later.asked == 2
    assert transcript.final_text == "second"
    assert transcript.delivered_ids("later") == {"later-1"}


class Recording(TurnHook):
    def __init__(self, name: str, *, fails: bool = False) -> None:
        self.name, self.fails = name, fails
        self.acknowledged: list[list[str]] = []

    async def notices(self, point: Point, run: RunView) -> tuple[Notice, ...]:
        return (Notice(self.name, (f"{self.name}-1",), "Note: ", [1]),) if point == "start" else ()

    async def acknowledge(self, record: RunRecord) -> None:
        if self.fails:
            raise RuntimeError("unexpected")
        self.acknowledged.append(record.delivered[self.name])


async def test_save_acknowledges_every_source_from_the_record(
    tmp_path, capsys: pytest.CaptureFixture[str]
) -> None:
    projects = ProjectStore(tmp_path)
    projects.create("p", "minimal", "sha256:" + "a" * 64)
    projects.open_session("p", "s", dedicated=False)
    failing, other = Recording("failing", fails=True), Recording("other")
    transcript = await run_task(
        ScriptedModel([ModelTurn("x")]), toolbox(), "go", run_id="r", hooks=[failing, other]
    )
    now = datetime.now(UTC)
    fields = RunRecord(
        run_id="r",
        context=CONTEXT,
        task="go",
        started_at=now,
        ended_at=now,
        stopped=transcript.stopped,
        final_text=transcript.final_text,
        messages=transcript.messages,
        tools=[],
        delivered={source: sorted(ids) for source, ids in transcript.delivered.items()},
    ).model_dump(mode="json")
    args = argparse.Namespace(transcript=None, state_dir=tmp_path)
    assert await cli._save(args, fields, 0, [failing, other]) == 1
    assert "the run's failing could not be acknowledged" in capsys.readouterr().err
    (saved,) = projects.runs("p", "s")
    assert projects.run("p", "s", saved.name).delivered == {
        "failing": ["failing-1"],
        "other": ["other-1"],
    }
    assert other.acknowledged == [["other-1"]]


def test_a_legacy_record_reads_into_delivered_and_summaries() -> None:
    now = datetime.now(UTC).isoformat()
    legacy = {
        "run_id": "r",
        "context": CONTEXT.model_dump(mode="json"),
        "task": "go",
        "started_at": now,
        "ended_at": now,
        "stopped": "answered",
        "final_text": "x",
        "messages": [],
        "tools": [],
        "job_notice_ids": ["job-1"],
        "work_request_ids": [],
        "review_notice_ids": ["finding-1-r1"],
        "math_notice_ids": [],
        "review_summary": {"outcome": "findings"},
        "math_summary": {},
    }
    record = RunRecord.model_validate(legacy)
    assert record.delivered == {"jobs": ["job-1"], "reviews": ["finding-1-r1"]}
    assert record.summaries == {"reviews": {"outcome": "findings"}}
    dumped = record.model_dump(mode="json")
    # The workspace reads the review and math summaries by their own names.
    assert (dumped["review_summary"], dumped["math_summary"]) == ({"outcome": "findings"}, {})
    assert RunRecord.model_validate(dumped) == record


class Responds(TurnHook):
    """Asks for a response round at every closing answer, and counts its accepts."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.closings = self.accepted = 0

    async def closing(self, run: RunView, answer: str | None) -> Closing:
        self.closings += 1
        notice = Notice(self.name, (f"{self.name}-{self.closings}",), "Finding: ", [1])
        return Closing("respond", (notice,))

    async def accept(self, run: RunView) -> None:
        self.accepted += 1

    def summary(self) -> object | None:
        return {"closings": self.closings}


async def test_hooks_share_response_rounds_and_the_last_answer_stands() -> None:
    first, second = Responds("first"), Responds("second")
    model = ScriptedModel([ModelTurn("a"), ModelTurn("b"), ModelTurn("c")])
    transcript = await run_task(model, toolbox(), "go", run_id="r", hooks=[first, second])
    # Two rounds, each answering both hooks' findings; then the answer stands.
    assert (transcript.stopped, transcript.final_text) == ("answered", "c")
    assert (transcript.response_rounds, first.accepted, second.accepted) == (2, 1, 1)
    assert transcript.delivered_ids("first") == {"first-1", "first-2"}
    assert transcript.summaries == {"first": {"closings": 3}, "second": {"closings": 3}}


class Vetoes(TurnHook):
    """Refuses every closing answer once, then stops the run."""

    name = "veto"
    veto = True

    def __init__(self) -> None:
        self.closings = 0

    async def closing(self, run: RunView, answer: str | None) -> Closing:
        self.closings += 1
        return Closing("retry" if self.closings == 1 else "stop", stopped="user_message")


async def test_a_veto_is_asked_first_and_can_stop_the_run_without_an_answer() -> None:
    veto, later = Vetoes(), Continues("later", notices=True)
    model = ScriptedModel([ModelTurn("a"), ModelTurn("b")])
    transcript = await run_task(model, toolbox(), "go", run_id="r", hooks=[later, veto])
    assert (transcript.stopped, transcript.final_text) == ("user_message", None)
    assert (veto.closings, later.asked) == (2, 0)
