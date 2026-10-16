# SPDX-License-Identifier: MIT
"""The fold region within its bound (docs/specs/context-management.md, CM-21b, CM-45):
CM-F18's short forms, CM-F20's and CM-F21's reductions, spill records, and the
``research_state`` stop, as far as step (f2b) builds them; and the lab's shares
(CM-27). Records are built in memory or written through the archive's own API; no
gateway is called but the scripted one."""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

import pytest

from sandbox_science.orchestrator.digest import Rendered
from sandbox_science.orchestrator.history import Archive, Entry, Folder, FoldRecord
from sandbox_science.orchestrator.history.bound import bound
from sandbox_science.orchestrator.history.folds import CONSOLIDATE
from sandbox_science.orchestrator.history.geometry import Shares, geometry
from sandbox_science.orchestrator.history.view import session_view, settle
from sandbox_science.orchestrator.loop import Transcript
from sandbox_science.orchestrator.projects import ProjectStore

from .loop_harness import PROJECT, SESSION, Harness, Reply
from .loop_harness import gateway as gateway
from .loop_harness import harness as harness
from .loop_harness import worker as worker
from .test_history_folds import CONTEXT, folds, worked

Json = dict[str, Any]


@pytest.fixture
def archive(tmp_path: Path) -> Iterator[Archive]:
    projects = ProjectStore(tmp_path / "state")
    projects.create(PROJECT, "minimal", "sha256:" + "c" * 64)
    projects.open_session(PROJECT, SESSION, dedicated=False)
    yield Archive(projects.control)
    projects.close()


def record(markers: Sequence[Json], *, at: int = 1, outline: str = "") -> FoldRecord:
    return FoldRecord(level=1, first=(at, f"msg-{at:016x}"), last=(at, f"msg-{at:016x}"),
                      runs=("r",), dates=("d", "d"), skeleton=(outline,) if outline else (),
                      markers=tuple(markers), keys=())  # fmt: skip


def decided(handle: str, reason: str = "too slow", status: str = "rejected", **more: Any) -> Json:
    text = f"{handle} → {status}: {reason}"
    if "reopen" in more:
        text += f"; reopen if {more['reopen']}"
    return {"kind": "decision", "subject": handle, "text": text, "status": status,
            "reason": reason, "entry": handle, "at": "msg-1", "revision": 1, **more}  # fmt: skip


def caveat(handle: str, line: str) -> Json:
    return {"kind": "caveat", "subject": handle, "text": f"caveat on {handle}: {line}",
            "caveat": line, "entry": handle, "at": "msg-1", "revision": 1}  # fmt: skip


def failures(tool: str, count: int) -> Json:
    return {"kind": "failures", "subject": tool, "count": count, "ids": ["c1"]}


# Short forms (CM-21b; CM-F18).


def test_a_marker_is_short_only_while_the_digest_holds_the_same_exactly() -> None:
    """CM-F18: D-4's marker is short while the digest's line holds its status, reason and
    reopen condition, and in full again once D-4 leaves the digest or its line there says
    something else; a caveat line is short while its entry's line holds it."""
    d4 = decided("D-4", reopen="I-1 is resolved")
    c3 = caveat("C-3", "below 2^64")
    shown = Rendered(
        "…",
        lines={
            "D-4": "D-4 (rejected) Index calculus; reason: too slow; reopen if: I-1 is resolved "
            "[user] (ledger_read D-4)",
            "C-3": "C-3 (proposed) A bound; caveats: below 2^64; n odd",
        },
    )
    records = [("F-1", record([d4, c3]))]
    found = bound(records, {}, shown, 10_000, 10_000)
    text = found.texts[0][1]
    assert "- decision: D-4 → rejected, in the state block" in text
    assert "- caveat: C-3, its caveat in the state block" in text
    assert found.rendering == {"F-1": {"short": [0, 1]}}
    for lines in (
        {},  # D-4 left the digest
        {"D-4": "D-4 (rejected) Index calculus; reason: too sl… [user] (ledger_read D-4)"},
        {"D-4": "D-4 (in_effect) Index calculus; reason: too slow; reopen if: I-1 is resolved"},
    ):
        again = bound(records, {}, Rendered("…", lines=lines), 10_000, 10_000)
        assert (
            "- decision: D-4 → rejected: too slow; reopen if I-1 is resolved" in again.texts[0][1]
        )
    # The stored record is the same throughout.
    assert records[0][1].markers == (d4, c3)


# Reductions (CM-21b; CM-F20, CM-F21).


def region(*, named: frozenset[str] = frozenset()) -> tuple[list[tuple[str, FoldRecord]], Any]:
    old = record([
        decided("D-1"), failures("sage", 40), caveat("C-1", "old " * 20),
        {"kind": "read", "subject": "a.pdf", "text": "a.pdf: read partial: pages 1-3 of 40"},
        caveat("C-2", "retired " * 10),
    ], at=1)  # fmt: skip
    new = record([decided("D-2"), caveat("C-9", "named " * 20), caveat("C-3", "new " * 20)], at=2)
    retired = {"F-1": {4: "C-2 is withdrawn"}}
    digest = Rendered("…", named=named, pinned=frozenset({"D-2"}))
    return [("F-1", old), ("F-2", new)], (retired, digest)


def test_the_region_reduces_in_cm_21b_order_stopping_as_soon_as_it_fits() -> None:
    """CM-F20, CM-F21: the index of retired markers collapses first; then retired markers
    and failure aggregates spill, oldest records first; then current caveats and reads,
    those the open questions name last; decision lines never spill."""
    records, (retired, digest) = region(named=frozenset({"C-9"}))
    whole = bound(records, retired, digest, 100_000, 100_000)
    assert whole.steps == ()
    assert "Retired markers, kept as an index" in whole.texts[0][1]
    collapsed = bound(records, retired, digest, whole.size - 40, 100_000)
    assert collapsed.steps == ("collapsed",)
    assert "Retired markers: 1 caveat; history_read F-1 returns them." in collapsed.texts[0][1]
    spilled = bound(records, retired, digest, collapsed.size - 30, 100_000)
    assert spilled.steps == ("collapsed", "spilled")
    assert spilled.rendering["F-1"] == {"spilled": [1, 4]}
    assert spilled.pointer == (
        "spilled: 1 retired markers and 1 failure aggregates of F-1 → history_read {spill}"
    )
    assert spilled.spill is not None
    assert "- failures: 40 sage calls failed" in spilled.spill
    assert "- caveat: C-2 → retired: C-2 is withdrawn" in spilled.spill
    # Caveats and reads next: the oldest first, the named one (C-9) last.
    tight = bound(records, retired, digest, 600, 100_000)
    assert tight.spilled[:3] == ("C-1", "a.pdf", "C-3")
    assert "C-9" not in tight.spilled or tight.spilled[-1] == "C-9"
    assert all("decision: D-" in text for _, text in tight.texts)
    assert tight.pointer is not None
    assert "the current caveats and reads on C-1, a.pdf, C-3" in tight.pointer


def test_decisions_grow_the_region_to_its_cap_then_the_least_relevant_shorten() -> None:
    """CM-21b step 5, CM-F21: decision lines alone past the budget grow the region up to
    its cap; past it, the least relevant of this session's decisions shorten to one line,
    the oldest first and never a standing constraint; past that, the run stops."""
    reasons = [("why " * 80).strip() for _ in range(6)]
    marks = [decided(f"D-{n}", reasons[n]) for n in range(6)]
    records = [("F-1", record(marks[:3], at=1)), ("F-2", record(marks[3:], at=2))]
    digest = Rendered("…", named=frozenset({"D-0"}), pinned=frozenset({"D-5"}))
    whole = bound(records, {}, digest, 100_000, 100_000)
    grown = bound(records, {}, digest, whole.size // 2, whole.size)
    assert (grown.steps, grown.shortened, grown.stop) == (("grown",), (), None)
    short = bound(records, {}, digest, whole.size // 2, whole.size - 100)
    assert short.steps == ("shortened", "grown")
    assert short.shortened == ("D-1",)  # the oldest not named; D-0 is named, D-5 pinned
    assert "- decision: D-1 → rejected: why why" in short.texts[0][1]
    assert "(shortened; ledger_read D-1)" in short.texts[0][1]
    stop = bound(records, {}, digest, 450, 600)  # the outlines fit; the decisions do not
    assert stop.stop is not None
    assert "D-5" not in stop.shortened
    assert stop.stop.startswith("the session's current decisions do not fit")


# The view: spill records and the stop (CM-21b, CM-45).


def recorded(archive: Archive, markers: Sequence[Json], text: str) -> str:
    row = archive.append(PROJECT, SESSION, "earlier", Entry("user", "user", text))
    at = (row.position, row.message_id)
    made = replace(record(markers), first=at, last=at, dates=(row.created, row.created))
    entry = Entry("user", "fold", made.text("this record", whole=True), source="folds",
                  native=made.stored(), native_format="sandbox-fold-1")  # fmt: skip
    return archive.append(PROJECT, SESSION, "folder", entry).message_id


async def test_a_spill_record_is_archived_once_and_its_pointer_names_it(archive: Archive) -> None:
    """CM-21b: what a rebuild spills is archived as a spill record, kept out of the
    search indexes, which the region's pointer line names; a later rebuild that spills
    the same names the same record."""
    recorded(archive, [failures(f"tool{n}", 3) for n in range(40)], "Many failures.")
    recorded(archive, [decided("D-1")], "One decision.")
    worked(archive, "earlier", 1, 100)
    shape = geometry(20_000, 0, 4_000)  # a region of 3,000 characters
    view = session_view(archive, CONTEXT, geometry=shape, per_token=1.0)
    assert view.region is None  # its pointer waits for the spill record
    assert view.spill is not None
    settled = await settle(view, archive, CONTEXT, run="r", kernels=None)
    spills = [r for r in archive.messages(PROJECT, SESSION) if r.entry.kind == "spill"]
    (spill,) = spills
    assert spill.entry.searched is None
    assert settled.spilled == spill.message_id
    region = next(str(p.message["content"]) for p in settled.parts if p.kind == "fold_region")
    assert region.endswith(f"→ history_read {spill.message_id}")
    assert "failures: 3 tool0" not in region
    assert "- failures: 3 tool0 calls failed" in (spill.entry.content or "")
    again = session_view(archive, CONTEXT, geometry=shape, per_token=1.0)
    assert again.region is not None
    assert again.spilled == spill.message_id
    assert str(again.region.message["content"]).endswith(spill.message_id)
    assert settled.summary()["spill"] == spill.message_id


async def test_decisions_that_do_not_fit_stop_the_run_with_research_state(archive: Archive) -> None:
    """CM-45: when the session's current decisions do not fit the region even shortened,
    the run stops with ``research_state``, saying which are the least relevant and how
    to consolidate them."""
    recorded(archive, [decided(f"D-{n}", "why " * 30) for n in range(30)], "Decisions.")
    worked(archive, "earlier", 1, 100)
    start = geometry(4_000, 0, 1_000)
    view = session_view(archive, CONTEXT, geometry=start, per_token=1.0)
    assert view.stop is not None
    made = Folder(archive, CONTEXT, run="r", view=view, start=start, ceiling=4_000, chunk=1_000)
    made.per_token = lambda run: 1.0  # type: ignore[method-assign]
    transcript = Transcript(messages=[{"role": "system", "content": "s"}, *view.messages])
    assert not await made.before_call(transcript)
    assert made.stopped == "research_state"
    assert made.detail is not None
    assert made.detail.startswith("the session's current decisions do not fit")
    assert made.detail.endswith(CONSOLIDATE)
    summary = cast(Json, made.summary())
    assert (summary["stopped"], summary["detail"]) == ("research_state", made.detail)


# The lab's shares (CM-27).


def test_small_shares_scale_the_budgets_and_lambda_counts_them() -> None:
    small = Shares(words=0.02, digest=0.01, region=0.02)
    shape = geometry(250_000, 30_000, None, small)
    assert shape.scale == 1.0
    assert (shape.words, shape.digest, shape.region) == (5_000, 2_500, 5_000)
    assert shape.state_block == 7_500
    assert shape.summary()["shares"] == [0.02, 0.01, 0.02]
    assert "shares" not in geometry(250_000, 30_000).summary()
    with pytest.raises(ValueError, match="region share"):
        Shares(region=0.0)


async def test_the_shares_are_run_options(harness: Harness) -> None:
    options = ["--words-share", "0.02", "--digest-share", "0.01", "--region-share", "0.03"]
    record = await harness.run("run-1", [Reply("Done.")], options=options)
    assert folds(record)["geometry"]["shares"] == [0.02, 0.01, 0.03]
    with pytest.raises(SystemExit):
        await harness.run("run-2", [Reply("Done.")], options=["--region-share", "0.9"])
