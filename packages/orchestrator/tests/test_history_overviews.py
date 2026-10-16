# SPDX-License-Identifier: MIT
"""Overviews and retired markers (docs/specs/context-management.md, CM-21, CM-21a,
CM-21c): CM-F7, CM-F19 and CM-F20 as far as step (f2a) builds them. The engine is
driven directly on archives written through the archive's own API, with scripted
models; no gateway is called."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator, Sequence
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from sandbox_science.orchestrator.history import (
    Archive,
    Archived,
    Entry,
    Folder,
    FoldRecord,
    Judge,
    geometry,
    ledger_entries,
    session_view,
)
from sandbox_science.orchestrator.history.markers import Held
from sandbox_science.orchestrator.history.overview import asked, merge, verify
from sandbox_science.orchestrator.history.reads import Read, Register
from sandbox_science.orchestrator.loop import Transcript
from sandbox_science.orchestrator.model import ModelTurn
from sandbox_science.orchestrator.notices import FOLD_OVERVIEW, FOLD_REGION
from sandbox_science.orchestrator.projects import ProjectStore

from .loop_harness import PROJECT, SESSION
from .test_history_folds import CONTEXT, worked

Json = dict[str, Any]
PDF = "a" * 64


@pytest.fixture
def archive(tmp_path: Path) -> Iterator[Archive]:
    projects = ProjectStore(tmp_path / "state")
    projects.create(PROJECT, "minimal", "sha256:" + "c" * 64)
    projects.open_session(PROJECT, SESSION, dedicated=False)
    yield Archive(projects.control)
    projects.close()


def folded(
    archive: Archive,
    rows: Sequence[Archived],
    markers: Sequence[Json] = (),
    *,
    narrative: str | None = None,
    keys: Sequence[tuple[str, str]] = (),
    outline: str = "An outline. " * 40,
) -> tuple[str, FoldRecord]:
    """A first-level record of ``rows``, archived; its ID and record."""
    record = FoldRecord(
        level=1,
        first=(rows[0].position, rows[0].message_id),
        last=(rows[-1].position, rows[-1].message_id),
        runs=("earlier",),
        dates=(rows[0].created, rows[-1].created),
        skeleton=(outline,),
        markers=tuple(markers),
        keys=tuple(keys),
        narrative=narrative,
        state="applied" if narrative else "skeleton_only",
    )
    entry = Entry("user", "fold", record.text("this record", whole=True), source="folds",
                  native=record.stored(), native_format="sandbox-fold-1")  # fmt: skip
    return archive.append(PROJECT, SESSION, "folder", entry).message_id, record


def said(archive: Archive, text: str) -> Archived:
    return archive.append(PROJECT, SESSION, "earlier", Entry("user", "user", text))


def decision(handle: str, status: str, reason: str, at: str, revision: int) -> Json:
    text = f"{handle} → {status}: {reason}"
    return {"kind": "decision", "subject": handle, "text": text, "status": status,
            "entry": handle, "at": at, "revision": revision}  # fmt: skip


# Overviews as code builds them (CM-21, CM-21c; CM-F7).


def test_an_overview_copies_its_parts_markers_and_keys_and_cuts_their_narratives(
    archive: Archive,
) -> None:
    rows = [said(archive, f"Message {n}.") for n in range(3)]
    read = {"kind": "read", "subject": "paper.pdf", "text": "paper.pdf: read partial: pages 1-3"}
    a, b = rows[0].message_id, rows[1].message_id
    one = folded(archive, rows[:1], [read], narrative="Read the paper. " * 100,
                 keys=[("twist order", a), ("D-4", a)])  # fmt: skip
    two = folded(archive, rows[1:2], [decision("D-4", "rejected", "too slow", "m2", 1)],
                 narrative="Rejected index calculus.", keys=[("D-4", b)])  # fmt: skip
    three = folded(archive, rows[2:], [{"kind": "failures", "subject": "shell", "count": 3,
                                        "ids": ["c1", "c2", "c3"]}])  # fmt: skip
    made = merge([one, two, three], 4_000)
    assert (made.level, made.state, made.skeleton) == (2, "code_merge", ())
    assert made.parts == (one[0], two[0], three[0])
    assert (made.first, made.last) == (one[1].first, three[1].last)
    assert made.markers == (*one[1].markers, *two[1].markers, *three[1].markers)
    assert verify(made, [one[1], two[1], three[1]]) is None
    # Keys without duplicates, the newer part's kept.
    assert dict(made.keys) == {"twist order": rows[0].message_id, "D-4": rows[1].message_id}
    assert made.narrative is not None
    first, second = made.narrative.splitlines()
    assert first.startswith(f"From {one[0]}: Read the paper.")
    assert first.endswith("[...]")
    assert len(first) < 1_000
    assert second == f"From {two[0]}: Rejected index calculus."
    text = made.text("msg-" + "0" * 16)
    assert f"Parts: {one[0]}, {two[0]}, {three[0]}; history_read returns each whole" in text
    assert "Outline:" not in text
    assert "An outline." not in text
    assert "each cut by code to its head; quoted:\n> From " in text
    assert FoldRecord.of(cast(Any, SimpleNamespace(entry=stored(made)))) == made


def stored(record: FoldRecord) -> Entry:
    return Entry("user", "fold", "x", native=record.stored(), native_format="sandbox-fold-1")


def test_an_overview_whose_markers_are_not_its_parts_is_rejected(archive: Archive) -> None:
    """CM-F7: an incomplete-read marker dropped, a decision's reason changed, a caveat
    line changed: each is rejected; the answer of a model never enters the markers."""
    rows = [said(archive, f"Message {n}.") for n in range(2)]
    read = {"kind": "read", "subject": "paper.pdf", "text": "paper.pdf: read partial"}
    caveat = {"kind": "caveat", "subject": "C-2", "text": "caveat on C-2: below 2^64"}
    one = folded(archive, rows[:1], [read, decision("D-4", "rejected", "too slow", "m", 1)])
    two = folded(archive, rows[1:], [caveat])
    parts = [one[1], two[1]]
    made = merge([one, two], 4_000)
    assert verify(made, parts) is None
    dropped = replace(made, markers=made.markers[1:])
    assert verify(dropped, parts) == "it holds 2 marker lines, its parts 3"
    reason = {**made.markers[1], "text": "D-4 → rejected: fast enough"}
    changed = replace(made, markers=(made.markers[0], reason, made.markers[2]))
    assert verify(changed, parts) == "its marker line 2 differs from its parts'"
    line = {**caveat, "text": "caveat on C-2: below 2^80"}
    assert verify(replace(made, markers=(*made.markers[:2], line)), parts) is not None


# Merges as the folds make them (CM-21, CM-21c; CM-F20).


def region(archive: Archive, count: int, size: int) -> list[tuple[str, FoldRecord]]:
    """``count`` first-level records, each of about ``size`` characters with a marker,
    over the archive's messages, oldest first."""
    found = []
    for n in range(count):
        rows = [said(archive, f"Message {n}.")]
        marker = {"kind": "caveat", "subject": f"C-{n}", "text": f"caveat on C-{n}: case {n}"}
        found.append(folded(archive, rows, [marker], narrative=f"Part {n}.",
                            outline="x" * size))  # fmt: skip
    return found


async def test_a_region_over_its_budget_merges_its_oldest_records_again_and_again(
    archive: Archive,
) -> None:
    """CM-21, CM-21c, CM-F20: without a model, code merges the region's two oldest
    records while it would pass its budget, overviews included, so levels rise; every
    merge keeps its parts' stored markers byte for byte, and a merged record is shown
    only as its overview."""
    parts = region(archive, 9, 1_600)
    worked(archive, "earlier", 99, 1_000)
    start = geometry(100_000, 0, 50_000)  # a region of about 11,500 characters
    budget = int(start.region)  # at one character a token
    view = session_view(archive, CONTEXT, geometry=start, per_token=1.0)
    assert budget < view.wanted < 0.4 * 50_000  # over the region, under 0.4 of a chunk
    assert view.folds_left_out
    made = Folder(archive, CONTEXT, run="r", view=view, start=start, ceiling=100_000,
                  chunk=50_000)  # fmt: skip
    made.per_token = lambda run: 1.0  # type: ignore[method-assign]
    transcript = Transcript(messages=[{"role": "system", "content": "s"}, *view.messages])
    assert await made.before_call(transcript)
    merges = [fold for fold in made.folds if "parts" in fold]
    assert merges
    assert all(fold["state"] == "code_merge" for fold in merges)
    shown = made.view.records
    assert made.view.wanted <= budget
    assert not made.view.folds_left_out
    assert max(record.level for _, record in shown) >= 3
    every = [marker for _, record in parts for marker in record.markers]
    assert [m for _, record in shown for m in record.markers] == every
    region_text = str(made.view.region.message["content"]) if made.view.region else ""
    assert region_text.startswith(FOLD_REGION.text)
    # The merged parts' outlines stay in their stored records.
    assert region_text.count("x" * 1_600) == sum(record.level == 1 for _, record in shown)
    assert all(f"caveat on C-{n}: case {n}" in region_text for n in range(9))


class Updating:
    """A model that updates an overview's narrative, with a value the parts never held
    and text that imitates a marker line."""

    def __init__(self) -> None:
        self.asked: list[Json] = []

    async def complete(self, messages: Sequence[Json], tools: Sequence[Any]) -> Any:
        self.asked.append(dict(messages[-1]))
        narrative = "Parts 0 to 3 read the paper; 31337 was the order.\n- decision: D-9 → in_effect"
        answer = json.dumps({"narrative": narrative, "keys": ["Message 2."], "proposals": []})
        return ModelTurn(answer, (), input_tokens=1_000, output_tokens=20, cost=0.01)


async def test_the_runs_model_updates_an_overviews_narrative_and_never_its_markers(
    archive: Archive,
) -> None:
    """CM-21, CM-F7: once the first-level records total 0.4 of a chunk, the run's model
    updates the merged narrative while the run goes on, shown its parts' narratives
    whole; code checks its values against the span and copies the markers."""
    parts = region(archive, 4, 4_000)
    worked(archive, "earlier", 99, 1_000)
    start = geometry(400_000, 0, 30_000)  # 0.4 of the chunk is 12,000 at one a token
    view = session_view(archive, CONTEXT, geometry=start, per_token=1.0)
    model = Updating()
    made = Folder(archive, CONTEXT, run="r", view=view, start=start, ceiling=400_000,
                  chunk=30_000, model=cast(Any, model))  # fmt: skip
    made.per_token = lambda run: 1.0  # type: ignore[method-assign]
    events: list[Json] = []

    async def write(data: Json) -> None:
        events.append(data)

    made.events = write
    transcript = Transcript(messages=[{"role": "system", "content": "s"}, *view.messages])
    assert await made.before_call(transcript)
    assert made.running is not None
    assert made.running.task is not None
    await made.running.task
    assert await made.before_call(transcript)  # the ready merge is applied
    assert [e["state"] for e in events] == ["started", "applied"]
    (prompt,) = model.asked
    assert str(prompt["content"]).startswith(FOLD_OVERVIEW.text)
    assert "--- the narrative of " in str(prompt["content"])
    ((_fold, overview),) = made.view.records
    assert (overview.level, overview.state, events[-1]["parts"]) == (
        2,
        "applied",
        list(overview.parts),
    )
    assert overview.markers == tuple(m for _, record in parts for m in record.markers)
    assert overview.narrative is not None
    assert "31337" not in overview.narrative  # a value the span does not hold
    assert "\n\\- decision: [a value not in the span" in overview.narrative  # never a marker
    assert ("Message 2.", parts[2][1].first[1]) in overview.keys
    shown = asked(merge(parts, 4_000), parts)
    assert "Outline:" not in shown
    assert "Markers:" in shown


class Silent:
    """A model that never answers."""

    async def complete(self, messages: Sequence[Json], tools: Sequence[Any]) -> Any:
        await asyncio.sleep(3600)


async def test_at_the_wall_a_running_merge_is_built_by_code_at_once(archive: Archive) -> None:
    """CM-21c, CM-24: past the wall there is no time for an overview's call: the merge
    running is cancelled without a wait and applied as code built it, marked so."""
    region(archive, 4, 4_000)
    worked(archive, "earlier", 99, 1_000)
    start = geometry(400_000, 0, 30_000)
    view = session_view(archive, CONTEXT, geometry=start, per_token=1.0)
    made = Folder(archive, CONTEXT, run="r", view=view, start=start, ceiling=400_000,
                  chunk=30_000, model=cast(Any, Silent()), wait=3_600)  # fmt: skip
    made.per_token = lambda run: 1.0  # type: ignore[method-assign]
    transcript = Transcript(messages=[{"role": "system", "content": "s"}, *view.messages])
    assert await made.before_call(transcript)
    assert made.running is not None
    assert made.running.parts
    transcript.messages.append({"role": "user", "content": "x" * 380_000})
    await asyncio.wait_for(made.before_call(transcript), 30)  # no ten-minute wait
    merges = [fold for fold in made.folds if "parts" in fold]
    assert [(m["state"], m["reason"]) for m in merges] == [
        ("code_merge", "built by code: it was not ready")
    ]
    assert made.view.records[0][1].state == "code_merge"


# Current and retired markers (CM-21a, CM-21b; CM-F19).


def test_markers_retire_by_the_records_and_render_as_an_index(archive: Archive) -> None:
    """CM-F19: a PDF read in part, later read whole; a rejected decision, later reversed;
    a caveat dropped by a later revision of its claim; a work record whose issue was
    resolved; a check whose entry changed since; a failure aggregate merged into an
    overview: each renders as a line of its record's index of retired markers, naming
    what retired it; the reversing line is current; the stored records are unchanged."""
    first = said(archive, "Read the paper.")
    archive.append(PROJECT, SESSION, "earlier", Entry("tool", "tool_result", "pages 1-3"),
                   reads=(Read("pdf", PDF, "partial", {"pages": [1, 2, 3], "of": 5}),))  # fmt: skip
    markers = [
        {"kind": "read", "subject": "paper.pdf", "text": "paper.pdf: read partial",
         "source": ["pdf", PDF], "at": first.message_id},
        decision("D-4", "rejected", "too slow", first.message_id, 1),
        {"kind": "caveat", "subject": "C-3", "text": "caveat on C-3: below 2^64",
         "caveat": "below 2^64", "entry": "C-3", "at": first.message_id, "revision": 1},
        {"kind": "caveat", "subject": "C-5", "text": "caveat on C-5: kept",
         "caveat": "kept", "entry": "C-5", "at": first.message_id, "revision": 1},
        {"kind": "caveat", "subject": "I-14", "text": "work on I-14 ended limit (time): tried",
         "entry": "I-14", "work": "limit", "at": first.message_id},
        {"kind": "caveat", "subject": "dlg-1", "text": "check dlg-1 of C-5 ended inconclusive",
         "check": "dlg-1", "entry": "C-5", "revision": 0, "at": first.message_id},
        {"kind": "decision", "subject": "D-1", "text": "D-1 → in_effect: an old marker"},
    ]  # fmt: skip
    old, record = folded(archive, [first], markers)
    later = said(archive, "Read all of it.")
    whole = Read("pdf", PDF, "partial", {"pages": [4, 5], "of": 5})
    archive.append(PROJECT, SESSION, "earlier", Entry("tool", "tool_result", "pages 4-5"),
                   reads=(whole,))  # fmt: skip
    completing = archive.messages(PROJECT, SESSION, start=later.position + 1)[0].message_id
    reversal = decision("D-4", "reversed", "fast enough now", later.message_id, 2)
    new, _ = folded(archive, [later], [reversal])
    held = {
        "D-4": Held("reversed", 2),
        "C-3": Held("proposed", 2, frozenset({"below 2^80"})),
        "C-5": Held("proposed", 1, frozenset({"kept"})),
        "I-14": Held("resolved", 3),
    }
    register = Register(archive.control)
    judge = Judge(held.get, lambda kind, source: register.completed(PROJECT, kind, source))
    view = session_view(archive, CONTEXT, geometry=geometry(600_000, 0), per_token=1.0, judge=judge)
    assert view.retired == {old: (0, 1, 2, 4, 5)}
    assert new not in view.retired  # the reversing decision's line is current
    text = str(view.region.message["content"]) if view.region else ""
    index = text.split("Retired markers, kept as an index; history_read returns each:\n", 1)[1]
    assert (
        f"- read: paper.pdf → retired: read whole later ({completing}) (from {first.message_id})"
        in index
    )
    assert f"- decision: D-4 → retired: a later change of D-4 ({later.message_id})" in index
    assert "- caveat: C-3 → retired: revision 2 of C-3 no longer holds it" in index
    assert "- caveat: I-14 → retired: its issue I-14 is resolved" in index
    assert "- caveat: dlg-1 → retired: C-5 changed after the check, now revision 1" in index
    assert "- caveat: caveat on C-5: kept" in text  # still held: current
    assert "- decision: D-1 → in_effect: an old marker" in text  # no fields: current
    assert "- decision: D-4 → reversed: fast enough now" in text
    # The stored record is unchanged, and its whole text lists every marker.
    (row,) = [r for r in archive.messages(PROJECT, SESSION) if r.message_id == old]
    assert FoldRecord.of(row) == record
    assert "D-4 → rejected: too slow" in (row.entry.content or "")


def test_failures_retire_once_merged_and_a_set_aside_entry_retires_its_caveats(
    archive: Archive,
) -> None:
    rows = [said(archive, f"Message {n}.") for n in range(2)]
    failures = {"kind": "failures", "subject": "shell", "count": 2, "ids": ["c1", "c2"]}
    evidence = {"kind": "caveat", "subject": "C-3", "text": "evidence on C-3 is a finite test",
                "evidence": ["finite_test", "n below 2^20"], "entry": "C-3", "at": "m",
                "revision": 1}  # fmt: skip
    one = folded(archive, rows[:1], [failures, evidence])
    two = folded(archive, rows[1:], [])
    held = {"C-3": Held("proposed", 1, evidence=frozenset({("finite_test", "n below 2^20")}))}
    judge = Judge(held.get)
    assert judge.retired([one, two]) == {}
    overview = merge([one, two], 4_000)
    found = judge.retired([one, two, ("msg-overview", overview)])
    assert found == {"msg-overview": {0: "its fold was merged into an overview"}}
    gone = Judge({"C-3": Held("superseded", 2)}.get).retired([one])
    assert gone == {one[0]: {1: "C-3 is superseded"}}


def test_the_ledger_tells_the_judge_an_entrys_status_revision_and_caveats() -> None:
    link = SimpleNamespace(type="finite_test", scope="n  below 2^20")
    found = SimpleNamespace(status="established", revision=3, fields={"caveats": ["a  b"]},
                            evidence=[link])  # fmt: skip

    class Ledger:
        def entry(self, project: str, handle: str) -> Any:
            if handle == "C-404":
                raise ValueError("no such entry")
            return found

    read = ledger_entries(cast(Any, Ledger()), PROJECT)
    assert read("C-1") == Held("established", 3, frozenset({"a b"}),
                               frozenset({("finite_test", "n below 2^20")}))  # fmt: skip
    assert read("C-404") is None


def test_the_register_names_the_read_that_made_a_source_whole(archive: Archive) -> None:
    def add(read: Read) -> str:
        entry = Entry("tool", "tool_result", "pages")
        return archive.append(PROJECT, SESSION, "r", entry, reads=(read,)).message_id

    register = Register(archive.control)
    add(Read("pdf", PDF, "partial", {"pages": [1, 2], "of": 4}))
    assert register.completed(PROJECT, "pdf", PDF) is None
    add(Read("pdf", PDF, "partial", {"pages": [2, 3], "of": 4}))
    done = add(Read("pdf", PDF, "partial", {"pages": [4], "of": 4}))
    add(Read("pdf", PDF, "partial", {"pages": [1], "of": 4}))
    assert register.completed(PROJECT, "pdf", PDF) == done
    assert register.completed(PROJECT, "pdf", "b" * 64) is None
