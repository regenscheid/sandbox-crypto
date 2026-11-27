# SPDX-License-Identifier: MIT
"""Durations in the run's records: each tool call's and each model call's start and
length, and ``tool-mix``'s totals of where the runs' time went (run_timing)."""

from __future__ import annotations

import json
from collections.abc import Iterator, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import pytest

from sandbox_science.orchestrator import cli
from sandbox_science.orchestrator.continuity.models import ModelIntents, observed
from sandbox_science.orchestrator.continuity.records import WorkRecords
from sandbox_science.orchestrator.model import Message, ModelTurn, ToolSpec, timed
from sandbox_science.orchestrator.projects import ModelUsage, ProjectStore, RunRecord
from sandbox_science.orchestrator.run_timing import RunTiming, scan, scan_events
from sandbox_science.orchestrator.runstate import RunState
from sandbox_science.orchestrator.tool_entry import Request, ToolEntry, ToolOutcome
from sandbox_science.orchestrator.tools import Toolbox
from sandbox_science.orchestrator.worker_client import WorkerClient
from sandbox_science.testing import FakeClock

from .scripted import ToolCall
from .test_tool_journal import CONTEXT, RUN


class Slow:
    """A model whose answer takes ``seconds`` of the fake clock."""

    def __init__(self, clock: FakeClock, seconds: float) -> None:
        self.clock, self.seconds = clock, seconds

    async def complete(self, messages: Sequence[Message], tools: Sequence[ToolSpec]) -> ModelTurn:
        self.clock.advance(self.seconds)
        return ModelTurn("done")


async def test_a_model_call_records_when_it_was_sent_and_how_long_it_took() -> None:
    clock = FakeClock()
    sent = clock.now()
    turn = await timed(Slow(clock, 2.5).complete([], []), clock)
    assert (turn.text, turn.started_at, turn.duration_ms) == ("done", sent, 2500)
    # Older records, without either, still read.
    assert ModelUsage.model_validate({"model": "m"}).duration_ms is None


async def test_a_tool_call_records_its_start_and_duration() -> None:
    clock = FakeClock()
    box = Toolbox(cast(WorkerClient, object()), CONTEXT, clock=clock)

    async def slow(request: Request) -> ToolOutcome:
        clock.advance(1.25)
        return ToolOutcome("{}", {"path": "x"})

    box.entries["slow"] = ToolEntry(ToolSpec("slow", "Slow.", {"type": "object"}), "read", slow)
    started = clock.now()
    outcome = await box.call(ToolCall("c1", "slow", "{}"), "op-1")
    assert outcome.record["timing"] == {"started_at": started.isoformat(), "duration_ms": 1250}
    assert outcome.record["path"] == "x"


@pytest.fixture
def projects(tmp_path: Path) -> Iterator[ProjectStore]:
    store = ProjectStore(tmp_path)
    store.create("p", "minimal", "sha256:" + "a" * 64)
    store.open_session("p", "s", dedicated=False)
    yield store
    store.close()


def test_an_untimed_observation_takes_its_intents_time(projects: ProjectStore) -> None:
    clock = FakeClock()
    owner = WorkRecords(projects.control).admit("p", "s", RUN)
    intents = ModelIntents(projects.control, clock=clock)
    identity = intents.begin(owner, kind="watcher", alias="w")
    clock.advance(3)
    intents.observe(owner, identity, ModelUsage(model="w"))
    timed_identity = intents.begin(owner, kind="watcher", alias="w")
    with projects.control.write() as tx:
        observed(
            tx.sql,
            "p",
            "s",
            RUN,
            timed_identity,
            ModelUsage(model="w", duration_ms=42),
            clock=clock,
        )
    with projects.control.read() as tx:
        rows = dict(
            tx.sql.execute(
                "SELECT identity,usage FROM research_model_calls WHERE run=?", (RUN,)
            ).fetchall()
        )
    first = ModelUsage.model_validate_json(rows[identity])
    assert first.duration_ms == 3000
    assert first.started_at is not None
    assert ModelUsage.model_validate_json(rows[timed_identity]).duration_ms == 42


def _timing(ms: int, started: datetime) -> dict[str, Any]:
    return {"started_at": started.isoformat(), "duration_ms": ms}


def recorded(projects: ProjectStore) -> datetime:
    """A run of 100 s: two model calls of 30 s and 10 s, a shell call of 20 s, a
    connector call of 15 s of which 12 s waited for the user's approval, an ``ask_user``
    of 8 s and a call recorded before timing existed."""
    started = datetime(2026, 10, 16, 3, tzinfo=UTC)  # RUN's name
    owner = WorkRecords(projects.control).admit("p", "s", RUN)
    intents = ModelIntents(projects.control)
    for ms in (30000, 10000):
        identity = intents.begin(owner, kind="watcher", alias="w")
        intents.observe(owner, identity, ModelUsage(model="w", duration_ms=ms))
    tools = [
        {"tool": "shell", "arguments": "{}", "timing": _timing(20000, started)},
        {
            "tool": "e-print-mcp_search",
            "arguments": "{}",
            "timing": _timing(15000, started),
            "approval_wait_ms": 12000,
        },
        {"tool": "ask_user", "arguments": "{}", "timing": _timing(8000, started)},
        {"tool": "shell", "arguments": "{}"},
    ]
    projects.save_run(
        RunRecord.model_validate(
            {
                "run_id": "run-1",
                "context": CONTEXT.model_dump(mode="json"),
                "task": "Work.",
                "started_at": started,
                "ended_at": started + timedelta(seconds=100),
                "stopped": "answered",
                "final_text": "Done.",
                "messages": [],
                "tools": tools,
            }
        )
    )
    return started


def test_a_runs_time_is_split_into_model_tools_waiting_and_the_rest(
    projects: ProjectStore,
) -> None:
    recorded(projects)
    (entry,) = projects.runs("p", "s")
    assert entry.name == RUN
    timing = scan(projects.control, "p", "s", entry.name)
    assert timing.span_ms == 100000
    assert (timing.model_ms, timing.model_calls) == (40000, 2)
    assert timing.tool_ms == {"shell": 20000, "e-print-mcp_search": 3000}
    assert (timing.user_ms, timing.approval_ms) == (8000, 12000)
    assert (timing.tool_calls, timing.untimed_tool_calls) == (4, 1)


def test_the_rest_is_what_the_parts_leave_of_the_span() -> None:
    timing = RunTiming(span_ms=100000)
    for ms in (30000, 10000, None):
        timing.model(ms)
    timing.tool("shell", 20000, None)
    timing.tool("e-print-mcp_search", 15000, 12000)
    timing.tool("ask_user", 8000, None)
    summary = timing.summary()
    assert summary["model_ms"] == 40000
    assert summary["untimed_model_calls"] == 1
    assert summary["tools_ms"] == 23000
    assert summary["rest_ms"] == 100000 - 40000 - 23000 - 8000 - 12000
    assert timing.line().startswith("time 1m40s · model 40.0s · tools 23.0s · waiting 20.0s")
    # Calls that ran at once may sum to more than the span: the rest is then 0.
    timing.tool("read_file", 90000, None)
    assert timing.rest_ms == 0


def test_a_cut_short_run_is_timed_from_its_events(projects: ProjectStore) -> None:
    state = RunState(projects)
    state.append("p", "s", "cut", "started", {"role": "main", "run_id": "cut"})
    for name, ms in (("shell", 4000), ("python", 6000)):
        state.append(
            "p",
            "s",
            "cut",
            "tool",
            {
                "turn": 1,
                "call_id": name,
                "name": name,
                "result": "{}",
                "timing": {"duration_ms": ms},
            },
        )
    timing = scan_events(projects.control, "p", "s", "cut")
    assert timing.tool_ms == {"shell": 4000, "python": 6000}


def test_the_command_prints_where_the_time_went(
    projects: ProjectStore, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    recorded(projects)
    projects.close()
    assert cli.main(["tool-mix", "--state-dir", str(tmp_path)]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[1] == "  wall clock 1m40s"
    assert lines[2].startswith("  main (1 run): time 1m40s · model ")
    assert "tools 23.0s · waiting 20.0s" in lines[2]
    assert "shell 20.0s, e-print-mcp_search 3.0s" in lines[2]
    assert "calls untimed" in lines[2]
    assert cli.main(["tool-mix", "--state-dir", str(tmp_path), "--json"]) == 0
    found = json.loads(capsys.readouterr().out)
    assert found["total"]["time"]["main"]["user_ms"] == 8000
    assert found["sessions"][0]["time"]["main"]["approval_ms"] == 12000
    assert found["sessions"][0]["time"]["delegated"]["runs"] == 0


def test_the_reviewer_and_delegated_runs_are_counted_apart(
    projects: ProjectStore, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """R13: the reviewer's calls under the run they review are not its model time, a
    delegated run that ran beside its parent is on a line of its own, and the session's
    wall clock is from the first start to the last end, not the runs' spans summed."""
    started = recorded(projects)
    owner = WorkRecords(projects.control).owner("p", "s", RUN)
    assert owner is not None
    intents = ModelIntents(projects.control)
    for ms in (50000, 45000):
        identity = intents.begin(owner, kind="reviewer", alias="r")
        intents.observe(owner, identity, ModelUsage(model="r", duration_ms=ms))
    child = "20261016T030020000000Z-run-2"
    owner = WorkRecords(projects.control).admit("p", "s", child)
    identity = intents.begin(owner, kind="delegated", alias="d")
    intents.observe(owner, identity, ModelUsage(model="d", duration_ms=30000))
    projects.save_run(
        RunRecord.model_validate(
            {
                "run_id": "run-2",
                "context": CONTEXT.model_dump(mode="json"),
                "task": "Check.",
                "started_at": started + timedelta(seconds=20),
                "ended_at": started + timedelta(seconds=110),
                "stopped": "answered",
                "final_text": "Done.",
                "messages": [],
                "role": "delegated",
                "parent": RUN,
                "delegation": "dlg-" + "1" * 24,
                "tools": [],
            }
        )
    )
    main = scan(projects.control, "p", "s", RUN)
    assert (main.model_ms, main.reviewer_ms, main.reviewer_calls) == (40000, 95000, 2)
    assert main.rest_ms == 100000 - 40000 - 23000 - 8000 - 12000
    projects.close()
    assert cli.main(["tool-mix", "--state-dir", str(tmp_path), "--json"]) == 0
    time = json.loads(capsys.readouterr().out)["sessions"][0]["time"]
    assert time["wall_ms"] == 110000
    assert (time["reviewer_ms"], time["reviewer_calls"]) == (95000, 2)
    assert (time["main"]["runs"], time["main"]["model_ms"]) == (1, 40000)
    assert (time["delegated"]["runs"], time["delegated"]["span_ms"]) == (1, 90000)
    assert time["delegated"]["model_ms"] == 30000
    assert cli.main(["tool-mix", "--state-dir", str(tmp_path)]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[1] == "  wall clock 1m50s · reviewer 1m35s in 2 calls, beside the runs"
    assert lines[3].startswith("  delegated (1 run): time 1m30s · model 30.0s")


def test_the_platforms_time_between_calls_is_part_of_the_rest() -> None:
    """Each call's usage says how long the loop worked before it and after the call before
    it: their sum is the platform's time, which the rest includes."""
    timing = RunTiming(span_ms=100000)
    timing.model(30000, 1200, None)
    timing.model(10000, 800, 2500)
    timing.model(None)
    summary = timing.summary()
    assert summary["platform_ms"] == 4500
    assert summary["rest_ms"] == 60000
    assert "rest 1m00s (platform 4.5s)" in timing.line()


def test_the_reviewers_the_delegated_runs_and_the_platforms_time_show_together(
    projects: ProjectStore, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The reviewer's calls beside the main run, a delegated run on a line of its own and
    each role's platform time between calls, in one output: the reviewer's calls' own
    preparation is not the run's platform time."""
    started = recorded(projects)
    owner = WorkRecords(projects.control).owner("p", "s", RUN)
    assert owner is not None
    intents = ModelIntents(projects.control)
    identity = intents.begin(owner, kind="watcher", alias="w")
    intents.observe(
        owner, identity, ModelUsage(model="w", duration_ms=5000, prepared_ms=1500, recorded_ms=500)
    )
    identity = intents.begin(owner, kind="reviewer", alias="r")
    intents.observe(owner, identity, ModelUsage(model="r", duration_ms=20000, prepared_ms=9000))
    child = "20261016T030020000000Z-run-2"
    owner = WorkRecords(projects.control).admit("p", "s", child)
    identity = intents.begin(owner, kind="delegated", alias="d")
    intents.observe(owner, identity, ModelUsage(model="d", duration_ms=30000, prepared_ms=3000))
    projects.save_run(
        RunRecord.model_validate(
            {
                "run_id": "run-2",
                "context": CONTEXT.model_dump(mode="json"),
                "task": "Check.",
                "started_at": started + timedelta(seconds=20),
                "ended_at": started + timedelta(seconds=110),
                "stopped": "answered",
                "final_text": "Done.",
                "messages": [],
                "role": "delegated",
                "parent": RUN,
                "delegation": "dlg-" + "1" * 24,
                "tools": [],
            }
        )
    )
    projects.close()
    assert cli.main(["tool-mix", "--state-dir", str(tmp_path), "--json"]) == 0
    time = json.loads(capsys.readouterr().out)["sessions"][0]["time"]
    assert (time["wall_ms"], time["reviewer_ms"], time["reviewer_calls"]) == (110000, 20000, 1)
    main, delegated = time["main"], time["delegated"]
    assert (main["model_ms"], main["platform_ms"], main["reviewer_ms"]) == (45000, 2000, 20000)
    assert main["rest_ms"] == 100000 - 45000 - 23000 - 8000 - 12000
    assert (delegated["model_ms"], delegated["platform_ms"]) == (30000, 3000)
    assert delegated["rest_ms"] == 60000
    assert cli.main(["tool-mix", "--state-dir", str(tmp_path)]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[1] == "  wall clock 1m50s · reviewer 20.0s in 1 calls, beside the runs"
    assert lines[2].startswith("  main (1 run): time 1m40s · model 45.0s")
    assert "rest 12.0s (platform 2.0s)" in lines[2]
    assert lines[3] == "  delegated (1 run): time 1m30s · model 30.0s · tools 0.0s · " + (
        "waiting 0.0s · rest 1m00s (platform 3.0s)"
    )
