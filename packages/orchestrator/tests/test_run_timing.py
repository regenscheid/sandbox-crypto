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
    assert lines[1].startswith("  time 1m40s · model ")
    assert "tools 23.0s · waiting 20.0s" in lines[1]
    assert "shell 20.0s, e-print-mcp_search 3.0s" in lines[1]
    assert "calls untimed" in lines[1]
    assert cli.main(["tool-mix", "--state-dir", str(tmp_path), "--json"]) == 0
    found = json.loads(capsys.readouterr().out)
    assert found["total"]["time"]["user_ms"] == 8000
    assert found["sessions"][0]["time"]["approval_ms"] == 12000
