# SPDX-License-Identifier: MIT
"""The project memory for the consistency reviewer and the job watcher
(docs/specs/project-memory.md, PM-5, PM-6, PM-8; PM-F4): the reviewer is given the
version the reviewed run started with, as guidance and not evidence; the watcher the
version current at each check. Each pins what it was given."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from collections.abc import Iterator, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest

from sandbox_science.orchestrator.consistency.prompt_bundle import (
    RECHECK_ASSEMBLY,
    assemble_reviewer,
    encode_provenance,
    recheck_pin,
)
from sandbox_science.orchestrator.consistency.rubric import (
    RECHECK,
    RECHECK_VERSION,
    RUBRIC,
    RUBRIC_VERSION,
)
from sandbox_science.orchestrator.consistency.runner import SPECS, ReviewerRunner
from sandbox_science.orchestrator.consistency.store import ReviewStore
from sandbox_science.orchestrator.documents import USER, DocumentStore, store_path
from sandbox_science.orchestrator.jobs import WATCHER_PROMPT, JobTools
from sandbox_science.orchestrator.jobstate import JobState
from sandbox_science.orchestrator.memory import GIVEN_CHARS, MemoryEntry, ProjectMemory
from sandbox_science.orchestrator.memory_prompt import (
    REVIEWER_FRAMING,
    WATCHER_FRAMING,
    for_reviewer,
    for_run,
    for_watcher,
)
from sandbox_science.orchestrator.model import Message, ModelTurn, ToolSpec
from sandbox_science.orchestrator.projects import ProjectStore
from sandbox_science.orchestrator.watcher_prompt import assemble_watcher
from sandbox_science.orchestrator.watcher_prompt import (
    encode_provenance as encode_watcher,
)
from sandbox_science.orchestrator.worker_client import WorkerClient
from sandbox_science.testing import FakeClock, eventually

from .test_jobs import CONTEXT, POLL, FakeWorker


@pytest.fixture
def state(tmp_path: Path) -> Iterator[tuple[DocumentStore, ProjectMemory]]:
    projects = ProjectStore(tmp_path)
    projects.create("p", "minimal", "sha256:" + "a" * 64)
    projects.open_session("p", "s", dedicated=False)
    projects.close()
    documents = DocumentStore(store_path(tmp_path))
    yield documents, ProjectMemory(documents)
    documents.close()


def add(memory: ProjectMemory, text: str) -> None:
    number = memory.memory("p").version + 1
    memory.change("p", {"operation_id": f"u{number}", "action": "add", "text": text}, USER)


async def test_the_reviewer_is_given_the_runs_memory_as_guidance(
    state: tuple[DocumentStore, ProjectMemory],
) -> None:
    documents, memory = state
    add(memory, "Write finite fields as GF(p).")
    given = for_reviewer(*memory.snapshot("p"))
    add(memory, "Accepted after the run started.")
    reviews = ReviewStore(documents, clock=lambda: 1000)
    reviews.observe("p", "s", "m1", "transcript", {"run": "main"}, {"content": "A claim."})
    review = reviews.dispatch("p", "s", "terminal", author_run="main", owner="o", alias="a")
    assert review is not None
    seen: list[Sequence[Message]] = []

    class Model:
        async def complete(self, messages: Sequence[Message], tools: Sequence[ToolSpec]):
            seen.append(messages)
            return ModelTurn('{"outcome":"passed","summary":"Checked.","cap_reached":false}')

    await ReviewerRunner(reviews, "p", "s", review, "o", Model(), memory=given).run()
    system = cast(str, seen[0][0]["content"])
    # The rubric, the re-check instructions, then the memory (format 3).
    assert system.startswith(RUBRIC + "\n\n" + RECHECK + "\n\n" + REVIEWER_FRAMING)
    assert "not evidence" in system
    assert "Write finite fields as GF(p)." in system
    assert "Accepted after the run started." not in system
    with sqlite3.connect(documents.path) as db:
        (call,) = db.execute("SELECT seq FROM review_calls").fetchone()
    pinned = reviews.call_provenance("p", "s", review, call)
    assert pinned.status == "complete"
    assert pinned.provenance is not None
    assert (pinned.provenance.format_version, pinned.provenance.assembly_version) == (
        3,
        RECHECK_ASSEMBLY,
    )
    assert pinned.provenance.memory == given.pin
    assert pinned.provenance.recheck == recheck_pin(RECHECK_VERSION, RECHECK)
    assert (given.pin.version, given.pin.entries) == (1, ("mem-1@1",))


def test_a_reviewer_pin_names_its_memory_or_none() -> None:
    plain = assemble_reviewer(RUBRIC, RUBRIC_VERSION, SPECS)
    assert (plain.provenance.format_version, plain.provenance.memory) == (1, None)
    assert plain.system == RUBRIC
    given = for_reviewer(0, ())
    empty = assemble_reviewer(RUBRIC, RUBRIC_VERSION, SPECS, given)
    assert (empty.provenance.format_version, empty.system) == (2, RUBRIC)
    for change in (
        {"memory": None},
        {"format_version": 1},
        {"assembly_version": "sandbox-reviewer-bundle-1"},
    ):
        with pytest.raises(ValueError, match="reviewer"):
            encode_provenance(empty.provenance.model_copy(update=change))
    with pytest.raises(ValueError, match="reviewer"):
        encode_provenance(
            plain.provenance.model_copy(update={"format_version": 2, "memory": given.pin})
        )
    moved = given.pin.model_copy(update={"version": 7})
    with pytest.raises(ValueError, match="reviewer"):
        encode_provenance(empty.provenance.model_copy(update={"memory": moved}))


def test_a_reviewer_pin_names_its_recheck_instructions_with_or_without_memory() -> None:
    """Format 3 (sandbox-reviewer-bundle-3) gives and pins the re-check instructions after
    the rubric and before the memory (CR-42, CR-43)."""
    recheck = recheck_pin(RECHECK_VERSION, RECHECK)
    plain = assemble_reviewer(RUBRIC, RUBRIC_VERSION, SPECS, None, recheck)
    assert (plain.provenance.format_version, plain.provenance.assembly_version) == (
        3,
        RECHECK_ASSEMBLY,
    )
    assert plain.system == RUBRIC + "\n\n" + RECHECK
    given = for_reviewer(0, ())
    empty = assemble_reviewer(RUBRIC, RUBRIC_VERSION, SPECS, given, recheck)
    assert (empty.provenance.format_version, empty.provenance.memory) == (3, given.pin)
    for change in (
        {"recheck": None},
        {"format_version": 2},
        {"assembly_version": "sandbox-reviewer-bundle-2"},
        {"recheck": recheck.model_copy(update={"text": RECHECK + " "})},
    ):
        with pytest.raises(ValueError, match=r"re-check|reviewer"):
            encode_provenance(plain.provenance.model_copy(update=change))


async def test_the_watcher_is_given_the_memory_as_it_is_at_each_check(
    tmp_path: Path, state: tuple[DocumentStore, ProjectMemory]
) -> None:
    documents, memory = state
    add(memory, "A sweep prints one line a minute; silence for ten is normal.")
    seen: list[Sequence[Message]] = []

    class Watcher:
        async def complete(self, messages: Sequence[Message], tools: Sequence[ToolSpec]):
            seen.append(messages)
            if len(seen) == 1:
                add(memory, "Accepted between the checks.")
                return ModelTurn('{"state":"fine","note":"Progress continues"}')
            return ModelTurn('{"state":"needs_attention","note":"Look at it"}')

    clock = FakeClock(datetime.now(UTC))
    jobs = JobTools(
        cast(WorkerClient, FakeWorker()),
        CONTEXT,
        run_id="run-1",
        state=JobState(tmp_path, clock=clock),
        watcher=Watcher(),
        watcher_alias="watcher",
        first_check_seconds=5,
        check_interval_seconds=60,
        poll_seconds=POLL,
        clock=clock,
        memory=lambda: for_watcher(*memory.snapshot("p")),
    )
    waiting = asyncio.create_task(jobs.wait({"job_ids": ["exe-1"]}, "op"))
    await eventually(lambda: len(seen) == 1 and clock.sleepers == 1)
    clock.advance(60)
    text, _ = await asyncio.wait_for(waiting, 5)
    assert json.loads(text)["reason"] == "needs_attention"
    first, second = (cast(str, messages[0]["content"]) for messages in seen)
    assert first.startswith(WATCHER_PROMPT + "\n\n" + WATCHER_FRAMING)
    assert "Accepted between the checks." not in first
    assert "Accepted between the checks." in second
    assert jobs.state is not None
    pins = [jobs.state.watcher_provenance(CONTEXT, call).provenance for call in (1, 2)]
    assert [(p.format_version, p.memory.version) for p in pins if p and p.memory] == [
        (2, 1),
        (2, 2),
    ]
    assert documents.control.missing is False


def test_a_watcher_pin_names_its_memory_or_none() -> None:
    plain = assemble_watcher(WATCHER_PROMPT, "data")
    assert (plain.provenance.format_version, plain.memory_text) == (1, None)
    assert "memory" not in json.loads(encode_watcher(plain.provenance))
    given = for_watcher(0, ())
    empty = assemble_watcher(WATCHER_PROMPT, "data", given)
    assert empty.messages()[0]["content"] == WATCHER_PROMPT
    for change in ({"memory": None}, {"format_version": 1}):
        with pytest.raises(ValueError, match="watcher"):
            encode_watcher(empty.provenance.model_copy(update=change))
    changed: dict[str, Any] = {"memory": given.pin.model_copy(update={"version": 3})}
    with pytest.raises(ValueError, match="watcher"):
        encode_watcher(empty.provenance.model_copy(update=changed))


@pytest.mark.parametrize("give", [for_run, for_reviewer, for_watcher])
def test_every_roles_section_stays_within_the_bound(give: Any) -> None:
    """PM-7: whatever the framing, the whole section as sent, the line on entries left
    out included, stays within the bound."""
    at = datetime(2026, 10, 15, tzinfo=UTC)
    entries = tuple(
        MemoryEntry(entry_id=f"mem-{n}", revision=999999999, text="x" * 1000, changed_at=at)
        for n in range(999999901, 1000000000)
    )
    section = give(999999999, entries).section
    assert section is not None
    assert "entries were left out" in section.text
    assert len(section.text) <= GIVEN_CHARS
