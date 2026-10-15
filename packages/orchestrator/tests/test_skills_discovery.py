# SPDX-License-Identifier: MIT
"""Discovery (docs/specs/skills.md, SK-35, SK-36, SK-21; SK-F15, SK-F16): before a run's
first model call, and before the call that carries each user message, the skills that
match are shown in a notice; the call waits at most a bounded time; nothing is blocked,
and the run's record keeps each notice.

The pack is ``ss-test-skills``, written here under the MIT License, in a temporary git
repository.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import threading
from pathlib import Path
from typing import Any, cast

import pytest

from sandbox_science.orchestrator import cli
from sandbox_science.orchestrator.documents import USER
from sandbox_science.orchestrator.notices import SKILLS
from sandbox_science.orchestrator.projects import ProjectStore, RunRecord
from sandbox_science.orchestrator.runstate import RunState
from sandbox_science.orchestrator.skills import SkillSet, SkillStore, from_git, snapshot
from sandbox_science.orchestrator.skills import discovery as discovery_module
from sandbox_science.orchestrator.skills.discovery import SkillDiscovery, matches
from sandbox_science.orchestrator.skills.store import Entry
from sandbox_science.orchestrator.skills.tool import SkillTool
from sandbox_science.testing.clock import FakeClock

from .loop_harness import PROJECT, SESSION, Harness, Reply, call
from .loop_harness import gateway as gateway
from .loop_harness import harness as harness
from .loop_harness import worker as worker

ALPHA = "---\nname: alpha\ndescription: Says the marker.\n---\nSS-MARKER-ALPHA-1\n"
BETA = "---\nname: beta\ndescription: Counts the lines of the notes.\n---\nCount them.\n"


@pytest.fixture
def pinned(harness: Harness, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Harness:
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    root = tmp_path / "ss-test-skills"
    for path, text in {"skills/alpha/SKILL.md": ALPHA, "skills/beta/SKILL.md": BETA}.items():
        (root / path).parent.mkdir(parents=True, exist_ok=True)
        (root / path).write_text(text)
    for args in (["init", "-q", "-b", "main"], ["add", "-A"], ["commit", "-qm", "C1"]):
        git = ["git", "-c", "user.name=t", "-c", "user.email=t@example.org", *args]
        subprocess.run(git, cwd=root, check=True, capture_output=True)
    projects = ProjectStore(harness.state)
    try:
        store = SkillStore(projects.control)
        store.add("ss", snapshot(from_git(str(root))), USER)
        store.pin(PROJECT, "ss", USER)
    finally:
        projects.close()
    return harness


def _notices(messages: list[dict[str, Any]]) -> list[str]:
    return [
        cast(str, m["content"])
        for m in messages
        if m.get("role") == "user" and cast(str, m["content"]).startswith(SKILLS.text)
    ]


def _discoveries(record: RunRecord) -> list[dict[str, Any]]:
    summary = cast(dict[str, Any], record.summaries["skills"])
    return cast(list[dict[str, Any]], summary["discoveries"])


async def test_matching_skills_are_shown_for_the_task_and_each_message(pinned: Harness) -> None:
    """SK-F15."""

    async def write() -> None:
        state = RunState(ProjectStore(pinned.state).control)
        state.post(PROJECT, SESSION, "m-1", "What does the alpha marker say?", author="user")

    record = await pinned.run(
        "run-1",
        [Reply(calls=[call("plan_read")], before=write), Reply("Done.")],
        task="Count the lines of the notes file.",
    )
    first, second = pinned.gateway.main
    (shown,) = _notices(first)
    assert '"id": "ss/beta"' in shown or '"id":"ss/beta"' in shown
    assert "ss/alpha" not in shown
    # Nothing was blocked: the model's call ran without a load.
    assert record.tools[0]["tool"] == "plan_read"
    assert "error" not in record.tools[0]
    # The notice for the message comes just before it, and does not repeat beta.
    assert second[-1] == {"role": "user", "content": "What does the alpha marker say?"}
    later = cast(str, second[-2]["content"])
    assert later.startswith(SKILLS.text)
    assert "ss/alpha" in later
    assert "ss/beta" not in later
    found = _discoveries(record)
    assert [(d["answered"], [s["id"] for s in d["skills"]]) for d in found] == [
        ("task", ["ss/beta"]),
        ("message", ["ss/alpha"]),
    ]
    assert {d["scoring"] for d in found} == {"lexical"}
    # Each record names its notice, which the run's record says a model call saw, and
    # the message it answered (SK-21).
    assert [d["notice"] for d in found] == ["shown-1", "shown-2"]
    assert sorted(record.delivered["skills"]) == ["shown-1", "shown-2"]
    assert "messages" not in found[0]
    assert found[1]["messages"] == ["m-1"]


async def test_the_summary_keeps_the_start_loads_beside_the_discoveries(
    pinned: Harness,
) -> None:
    argv = ["skills", "settings", "--state-dir", str(pinned.state), "--project", PROJECT]
    assert cli.main([*argv, "--expected", "0", "--always", "ss/alpha"]) == 0
    record = await pinned.run("run-1", [Reply("Done.")], task="Count the lines of the notes.")
    summary = cast(dict[str, Any], record.summaries["skills"])
    assert [load["id"] for load in summary["loads"]] == ["ss/alpha"]
    assert [d["answered"] for d in summary["discoveries"]] == ["task"]


async def test_a_short_or_unrelated_message_gets_no_notice(pinned: Harness) -> None:
    record = await pinned.run("run-1", [Reply("Hello.")], task="Thanks!")
    assert _notices(pinned.gateway.main[0]) == []
    assert _discoveries(record) == [{"answered": "task", "scoring": "lexical", "skills": []}]


class _Run:
    """What discovery reads of a run: the notice IDs a model call saw."""

    def __init__(self, *seen: str) -> None:
        self.seen = frozenset(seen)

    def delivered_ids(self, source: str) -> frozenset[str]:
        return self.seen


async def test_scoring_that_does_not_finish_sends_the_call_on_the_clock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SK-F16: scoring that never finishes holds the call for the wait, on the run's
    clock, and then it goes out without a notice."""
    held = threading.Event()

    def never(tool: SkillTool, text: str, skip: set[str]) -> list[Entry]:
        held.wait(30)  # a peer that never answers, let go when the test ends
        return []

    monkeypatch.setattr(discovery_module, "matches", never)
    clock = FakeClock()
    found = SkillDiscovery(_tool(*_ENTRIES), "Count the lines of the notes.", clock=clock)
    waiting = asyncio.ensure_future(found.notices("start", cast(Any, _Run())))
    try:
        while clock.sleepers == 0:
            await asyncio.sleep(0.01)
        clock.advance(4.9)
        await asyncio.sleep(0.05)
        assert not waiting.done()
        clock.advance(0.1)
        assert await asyncio.wait_for(waiting, 5) == ()
    finally:
        held.set()
    assert found.records == [{"answered": "task", "scoring": "timed_out", "skills": []}]


async def test_a_cancelled_wait_cancels_the_work() -> None:
    """The run's cancellation during the wait ends the scoring work too: nothing goes on
    after the run, as an embedding would."""
    started, ended = asyncio.Event(), asyncio.Event()

    async def work() -> list[Entry]:
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            ended.set()
        return []

    found = SkillDiscovery(_tool(*_ENTRIES), None, clock=FakeClock())
    waiting = asyncio.ensure_future(found._bounded(work()))
    await asyncio.wait_for(started.wait(), 5)
    waiting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiting
    await asyncio.wait_for(ended.wait(), 5)


async def test_a_notice_no_call_saw_does_not_count_as_shown() -> None:
    """A notice the run dropped, as at a closing with no room for it, leaves its skills
    to be shown again; one a model call saw does not."""
    found = SkillDiscovery(_tool(*_ENTRIES), None)
    text = ["count the lines of the notes"]
    first = await found.about(text, ["m-1"])
    assert first is not None
    assert first.ids == ("shown-1",)
    await found.model_returned(cast(Any, _Run()))
    again = await found.about(text, ["m-2"])
    assert again is not None
    assert again.ids == ("shown-2",)
    await found.model_returned(cast(Any, _Run("shown-2")))
    assert await found.about(text, ["m-3"]) is None
    assert [record.get("notice") for record in found.records] == ["shown-1", "shown-2", None]


def _tool(*entries: Entry) -> SkillTool:
    class Store:
        def file(self, snapshot: str, path: str) -> bytes | None:
            return None

    return SkillTool(
        cast(SkillStore, Store()), SkillSet(entries, (), ()), role="main", alias="main"
    )


_ENTRIES = (
    Entry("p", "notes", "Counts the lines of the notes.", "0" * 64, "s", "r"),
    Entry("p", "lines", "Draws lines.", "0" * 64, "s", "r"),
    Entry("p", "files", "Opens files in the notes folder and counts things.", "0" * 64, "s", "r"),
)


def test_a_skill_needs_two_shared_words_and_half_the_best_score() -> None:
    tool = _tool(*_ENTRIES)
    found = [e.name for e in matches(tool, "count the lines of the notes", set())]
    assert found[0] == "notes"
    assert "lines" not in found  # one shared word
    assert matches(tool, "count the lines of the notes", {"p/notes", "p/files"}) == []
    assert matches(tool, "notes please", set()) == []  # under three words
