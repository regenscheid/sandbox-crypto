# SPDX-License-Identifier: MIT
"""Folds (docs/specs/context-management.md, CM-17 to CM-20, CM-23, CM-24, CM-27, CM-43
to CM-46): CM-F1's folds, CM-F5's breaker, CM-F6, CM-F13 and CM-F27, as far as step
(f1)'s first part builds them, its folds skeleton-only. Runs go through ``cli.main``
against the in-process worker and the scripted gateway, whose token counts scale with
what each request sends; the engine is also driven directly on archives written through
the archive's own API."""

from __future__ import annotations

import json
from collections.abc import Iterator
from itertools import pairwise
from pathlib import Path
from typing import Any, cast

import pytest

from sandbox_science.contracts import ExecutionContext
from sandbox_science.orchestrator.control import ControlStore
from sandbox_science.orchestrator.harness import Harness as Harness_
from sandbox_science.orchestrator.harness import Role
from sandbox_science.orchestrator.history import (
    Archive,
    Archived,
    Entry,
    Folder,
    FoldRecord,
    Geometry,
    body,
    ceiling,
    geometry,
    session_view,
    settle,
)
from sandbox_science.orchestrator.history.folds import build, span
from sandbox_science.orchestrator.history.geometry import input_limit
from sandbox_science.orchestrator.history.reads import Read, Register
from sandbox_science.orchestrator.history.skeleton import _fit
from sandbox_science.orchestrator.loop import Transcript
from sandbox_science.orchestrator.model import Limits
from sandbox_science.orchestrator.notices import FOLD_REGION, STATE_BLOCK
from sandbox_science.orchestrator.projects import ModelUsage, ProjectStore, RunRecord, run_name
from sandbox_science.orchestrator.runstate import RunState
from sandbox_science.orchestrator.worker_client import dump

from .control_versions import older_store
from .loop_harness import PROJECT, SESSION, Harness, Reply
from .loop_harness import gateway as gateway
from .loop_harness import harness as harness
from .loop_harness import worker as worker
from .test_model_limits import TOO_LONG

Json = dict[str, Any]
CONTEXT = ExecutionContext(project_id=PROJECT, session_id=SESSION, environment_id="e")
SMALL = ["--context-ceiling", "100000", "--fold-chunk", "10000"]
"""C = 100,000 and K = 10,000: trigger 75,000, wall 90,000, floor 7,000, a record 1,000."""


@pytest.fixture
def archive(tmp_path: Path) -> Iterator[Archive]:
    projects = ProjectStore(tmp_path / "state")
    projects.create(PROJECT, "minimal", "sha256:" + "c" * 64)
    projects.open_session(PROJECT, SESSION, dedicated=False)
    yield Archive(projects.control)
    projects.close()


def name(record: RunRecord) -> str:
    return run_name(record.started_at, record.run_id)


def worked(archive: Archive, run: str, n: int, size: int, *, tool: str = "shell") -> str:
    """An answer that called ``tool`` once, and its result of about ``size`` characters;
    the result's ID."""
    calls = (
        {
            "id": f"{run}-{n}",
            "type": "function",
            "function": {"name": tool, "arguments": json.dumps({"script": f"part {n}"})},
        },
    )
    archive.append(
        PROJECT, SESSION, run, Entry("assistant", "assistant", f"Part {n}.", tool_calls=calls)
    )
    text = json.dumps({"state": "succeeded", "exit_code": 0, "stdout": f"{n}:" + "0" * size})
    result = Entry("tool", "tool_result", text, source=tool, call_id=f"{run}-{n}")
    return archive.append(PROJECT, SESSION, run, result).message_id


def seed(harness: Harness, count: int, size: int = 20_000, *, run: str = "seed") -> list[str]:
    """``count`` earlier calls of the session with results of ``size`` characters."""
    store = ProjectStore(harness.state)
    try:
        archive = Archive(store.control)
        return [worked(archive, run, n, size) for n in range(count)]
    finally:
        store.close()


def folds(record: RunRecord) -> Json:
    return cast(Json, record.summaries["folds"])


def stubs(messages: list[Json]) -> list[str]:
    """The archive IDs of the results ``messages`` show as stubs."""
    return [str(m["content"])[1:21] for m in messages if "\n(Stub: " in str(m.get("content"))]


# The geometry (CM-27; CM-F6).


def test_the_geometry_scales_its_budgets_on_small_ceilings_and_refuses_too_little() -> None:
    large = geometry(600_000, 30_000)
    assert (large.trigger, large.wall, large.chunk, large.scale) == (450_000, 540_000, 120_000, 1)
    assert (large.floor, large.state_block, large.region, large.record) == (
        84_000, 90_000, 90_000, 12_000,
    )  # fmt: skip
    small = geometry(168_000, 30_000)
    assert (small.trigger, small.wall, small.chunk) == (126_000, 151_200, 50_000)
    assert round(small.scale, 2) == 0.83
    assert abs(small.state_block - 20_950) < 50
    assert abs(small.floor - 29_100) < 50
    assert small.refused() is None
    # The steady state: both regions full, the floor and half a chunk fit under the trigger.
    assert 30_000 + small.state_block + small.region + small.floor + small.chunk // 2 <= (
        small.trigger + 1
    )
    assert "steady state" in str(geometry(100_000, 30_000).refused())  # λ about 0.31
    assert round(geometry(100_000, 30_000).scale, 2) == 0.31
    assert geometry(100_000, 20_000).refused() is None  # λ about 0.46
    assert "system message" in str(geometry(100_000, 21_000).refused())
    # The model's limit: a shared window less the output; an input limit as it is.
    assert ceiling(600_000, window=200_000, max_output=32_000) == 168_000
    assert ceiling(600_000, input_limit=168_000, max_output=32_000) == 168_000
    assert ceiling(600_000) == 600_000
    assert input_limit({"max_input_tokens": 200_000, "max_tokens": 64_000}) == 200_000
    assert input_limit({"max_tokens": 64_000}) is None


async def test_the_gateways_input_limit_caps_the_ceiling(harness: Harness) -> None:
    harness.gateway.limits = {"main": {"max_input_tokens": 90_000, "max_output_tokens": 8_000}}
    record = await harness.run("run-1", [Reply("Hi.")])
    assert folds(record)["geometry"]["ceiling"] == 90_000  # the output is not in it


async def test_a_claude_models_window_is_shared_with_its_answer(harness: Harness) -> None:
    """#459's review, M2, CM-27: a Claude model's ``max_input_tokens`` is a window input
    and output share, so the answer's room comes off it."""
    harness.gateway.limits = {
        "main": {
            "max_input_tokens": 200_000,
            "max_output_tokens": 64_000,
            "litellm_provider": "anthropic",
        }
    }
    record = await harness.run("run-1", [Reply("Hi.")])
    assert folds(record)["geometry"]["ceiling"] == 136_000


async def test_a_run_whose_system_message_is_too_large_for_its_ceiling_does_not_start(
    harness: Harness,
) -> None:
    record = await harness.run("run-1", [], status=3, options=["--context-ceiling", "60000"])
    assert harness.gateway.main == []
    assert (record.stopped, folds(record)["stopped"]) == ("context_limit", "start")
    store = ProjectStore(harness.state)
    try:
        stopped = RunState(store.control).events(PROJECT, SESSION, name(record))[-1]
    finally:
        store.close()
    assert (stopped.kind, stopped.data["reason"]) == ("stopped", "start")


# Folds at the trigger, stubs, and the next run (CM-24, CM-17; CM-F1, CM-F6).


async def test_nothing_folds_below_the_trigger(harness: Harness) -> None:
    harness.gateway.per_token = 4.0
    seed(harness, 8)
    record = await harness.run("run-1", [Reply("Done.")], options=SMALL)
    assert folds(record)["folds"] == []
    assert not any(FOLD_REGION.text in str(m["content"]) for m in harness.gateway.main[0])


async def test_past_the_trigger_the_oldest_span_folds_and_old_large_results_become_stubs(
    harness: Harness,
) -> None:
    """CM-F6, CM-F1, CM-24: a view past its trigger folds its oldest span, about a chunk,
    at a call boundary, before its next call; tool results older than the newest five and
    over 5,000 characters become stubs, a retrieval's never, and each reads back whole.
    The next run starts from the fold and its stubs."""
    harness.gateway.per_token = 4.0
    first = await harness.run("run-0", [Reply("Ready.")])
    results = seed(harness, 7)
    store = ProjectStore(harness.state)
    try:
        retrieval = worked(Archive(store.control), "seed", 50, 20_000, tool="history_read")
    finally:
        store.close()
    results += seed(harness, 7, run="later")
    record = await harness.run("run-1", [Reply("Done.")], options=SMALL)
    (made,) = folds(record)["folds"]
    assert made["state"] == "skeleton_only"
    assert made["freed"] > 0.1 * 86_000
    (request,) = harness.gateway.main
    block, region = request[1], request[2]
    assert body(str(block["content"])).startswith(
        f"{STATE_BLOCK.text}Folded: the messages at positions 1 to {made['span'][1]}. "
    )
    assert str(region["content"]).startswith(FOLD_REGION.text + f"--- fold {made['fold']} ")
    store = ProjectStore(harness.state)
    try:
        archive = Archive(store.control)
        # The span: run 0 and the oldest calls, about a chunk, ending with a call's result.
        spanned = archive.messages(PROJECT, SESSION, end=made["span"][1])
        assert {row.run for row in spanned} == {name(first), "seed"}
        assert spanned[-1].entry.kind == "tool_result"
        folded = {row.message_id for row in spanned}
        raw = [at for at in results if at not in folded]
        # Older than the newest five and over 5,000 characters: stubs; read back whole.
        assert stubs(request) == raw[:-5]
        assert made["stubs"] == len(raw) - 5
        assert retrieval not in folded
        assert retrieval not in stubs(request)
        assert all(len(archive.message(PROJECT, at).entry.content or "") > 20_000 for at in raw)
        events = RunState(store.control).events(PROJECT, SESSION, name(record))
    finally:
        store.close()
    assert [e.data["fold"] for e in events if e.kind == "fold"] == [made["fold"]]
    # The next run starts from the fold and its stubs.
    after = await harness.run("run-2", [Reply("Again.")], options=SMALL)
    view = cast(Json, after.summaries["view"])
    assert (view["folds"], view["stubs"]) == ([made["fold"]], raw[:-5])
    assert folds(after)["folds"] == []
    assert stubs(harness.gateway.main[0]) == raw[:-5]


async def test_between_rebuilds_a_request_only_grows_at_its_end(harness: Harness) -> None:
    """CM-F14, CM-32: after the fold at the run's first call, the next request sends the
    first one unchanged, then what was added; no stub is made between rebuilds."""
    harness.gateway.per_token = 4.0
    seed(harness, 14)
    record = await harness.run(
        "run-1", [Reply(calls=[("plan_read", {})]), Reply("Done.")], options=SMALL
    )
    assert len(folds(record)["folds"]) == 1
    first, second = harness.gateway.main
    assert second[: len(first)] == first
    assert stubs(second) == stubs(first)


# Overflow and loops (CM-43 to CM-45; CM-F5, CM-F13).


async def test_a_context_error_folds_once_and_tries_again_and_a_second_stops(
    harness: Harness,
) -> None:
    harness.gateway.per_token = 4.0
    seed(harness, 8)
    record = await harness.run(
        "run-1", [Reply(status=400, error=TOO_LONG), Reply("Done.")], options=SMALL
    )
    assert (record.stopped, record.final_text) == ("answered", "Done.")
    assert len(folds(record)["folds"]) == 1
    again = await harness.run(
        "run-2",
        [Reply(status=400, error=TOO_LONG), Reply(status=400, error=TOO_LONG)],
        status=3,
        options=SMALL,
    )
    assert (again.stopped, folds(again)["stopped"]) == ("context_limit", "overflow")
    assert len(harness.gateway.main) == 2


def folder(archive: Archive, top: int, chunk: int) -> tuple[Folder, Transcript]:
    """A folder for run ``r`` and its transcript, on the archive's view as it stands."""
    start = geometry(top, 0, chunk)
    view = session_view(archive, CONTEXT, geometry=start)
    made = Folder(archive, CONTEXT, run="r", view=view, start=start, ceiling=top, chunk=chunk)
    system = {"role": "system", "content": "s" * 400}
    return made, Transcript(messages=[system, *view.messages])


async def test_three_weak_folds_trip_the_breaker(archive: Archive) -> None:
    for n in range(40):
        worked(archive, "earlier", n, 1_000)
    worked(archive, "earlier", 99, 600_000)  # the newest, which no fold may take
    made, transcript = folder(archive, 160_000, 2_000)
    transcript.usage.append(ModelUsage(model="m"))  # the run has called: its folds count
    assert not await made.before_call(transcript)
    assert (made.weak, made.stopped) == (3, "breaker")
    assert len(made.folds) == 3


async def test_a_view_over_the_wall_with_nothing_to_fold_stops(archive: Archive) -> None:
    worked(archive, "earlier", 0, 700_000)
    made, transcript = folder(archive, 160_000, 50_000)
    assert not await made.before_call(transcript)
    assert (made.stopped, made.folds) == ("irreducible", [])
    assert transcript.rebuilt is None  # nothing changed


async def test_past_the_wall_it_folds_until_the_view_fits(archive: Archive) -> None:
    for n in range(200):
        worked(archive, "earlier", n, 4_000)  # too short to stub
    made, transcript = folder(archive, 160_000, 32_000)
    assert made.tokens(transcript) > made.start.wall
    assert await made.before_call(transcript)
    assert made.geometry is not None
    assert made.tokens(transcript) <= made.geometry.wall
    spans = [cast(list[int], fold["span"]) for fold in made.folds]
    assert len(spans) >= 2
    # Contiguous and never overlapping (CM-F1).
    assert all(later[0] > earlier[1] for earlier, later in pairwise(spans))
    sent = transcript.sent
    assert sent[0]["content"] == "s" * 400
    assert FOLD_REGION.text in str(sent[2]["content"])


async def test_each_rebuild_renders_the_research_state_as_it_is_then(archive: Archive) -> None:
    """CM-26, CM-F3: a fold's rebuild renders the digest again, within its third of the
    state block; between rebuilds the state block does not change."""
    for n in range(200):
        worked(archive, "earlier", n, 4_000)
    made, transcript = folder(archive, 160_000, 32_000)
    state = {"version": 1}
    made.digest = lambda budget: f"Research state {state['version']} in {budget} characters."
    state["version"] = 2
    assert await made.before_call(transcript)
    assert made.geometry is not None
    block = str(transcript.sent[1]["content"])
    assert f"Research state 2 in {made.geometry.digest * 4} characters." in block
    # Between rebuilds, the research state may change; the view does not.
    state["version"] = 3
    transcript.messages.append({"role": "user", "content": "More."})
    assert str(transcript.sent[1]["content"]) == block
    # Past the trigger again: the next safe point folds, and renders it again.
    folded = len(made.folds)
    transcript.messages.append({"role": "user", "content": "x" * 160_000})
    assert made.tokens(transcript) > made.geometry.trigger
    assert await made.before_call(transcript)
    assert len(made.folds) == folded + 1
    assert "Research state 3 in " in str(transcript.sent[1]["content"])


# Spans (CM-23).


def test_a_span_ends_where_the_user_spoke_when_near_its_size_and_keeps_the_floor(
    archive: Archive,
) -> None:
    for n in range(6):
        worked(archive, "earlier", n, 1_000)
    message = Entry("user", "user", "Now the other way.", source="messages", refs=("m-1",))
    archive.append(PROJECT, SESSION, "later", message)
    for n in range(6, 12):
        worked(archive, "later", n, 1_000)
    shape = Geometry(ceiling=1_000_000, prompt=0, chunk=7_000, scale=0.002)
    view = session_view(archive, CONTEXT, geometry=SHAPE)
    sizes = [sum(part.chars for part in unit) for unit in view.units]
    taken = span(view.units, shape, 1.0)
    # The user's message, near the chunk's size, ends the span rather than the nearest
    # boundary, five units in.
    assert taken == 6
    assert view.units[taken][0].kind == "user"
    assert abs(sum(sizes[:5]) - 7_000) < abs(sum(sizes[:6]) - 7_000)
    assert all(len(unit) == 2 for unit in view.units[:taken])  # calls with their results
    # Never below the floor: with a floor larger than the raw tail, nothing is taken.
    assert span(view.units, Geometry(1_000_000, 0, 30_000, 1.0), 1.0) == 0
    floor = Geometry(1_000_000, 0, 10_000, 1.0)  # a floor of 7,000
    taken = span(view.units, floor, 1.0)
    assert taken > 0
    assert sum(sizes[taken:]) >= floor.floor


SHAPE = Geometry(ceiling=10**7, prompt=0, chunk=10**6, scale=1.0)


# The skeleton and its records (CM-17, CM-18, CM-20; CM-F27).


def test_a_hundred_failed_calls_make_one_aggregate_a_tool_within_the_records_share(
    archive: Archive,
) -> None:
    rows: list[Archived] = []
    for n in range(100):
        tool = ("sage", "python", "shell")[n % 3]
        state = ("failed", "timed_out")[n % 2]
        calls = (
            {"id": f"c{n}", "type": "function", "function": {"name": tool, "arguments": "{}"}},
        )
        rows.append(
            archive.append(
                PROJECT, SESSION, "r", Entry("assistant", "assistant", None, tool_calls=calls)
            )
        )
        text = json.dumps({"state": state, "stdout": f"attempt {n} " + "x" * 300})
        rows.append(
            archive.append(
                PROJECT,
                SESSION,
                "r",
                Entry("tool", "tool_result", text, source=tool, call_id=f"c{n}"),
            )
        )
    shape = geometry(168_000, 30_000)
    record = build(rows, shape, 4.0)
    assert record is not None
    aggregates = [m for m in record.markers if m["kind"] == "failures"]
    assert sorted((m["subject"], m["count"]) for m in aggregates) == [
        ("python", 33), ("sage", 34), ("shell", 33),
    ]  # fmt: skip
    assert all(len(m["ids"]) == m["count"] for m in aggregates)
    assert record.chars <= shape.record * 4
    shown = record.text("msg-0123456789abcdef")
    assert "34 sage calls failed or timed out; their IDs are in fold msg-0123456789abcdef" in shown
    assert rows[1].message_id in record.text("this record", whole=True)


def test_the_skeleton_keeps_decisions_caveats_reads_and_literal_keys(archive: Archive) -> None:
    change = {
        "reason": "Needs assumption Y",
        "changes": [
            {"entry": "D-4", "expected": 1, "status": "rejected",
             "fields": {"reopen_condition": "I-14 resolves"}},
            {"entry": "C-2", "expected": 2, "fields": {"caveats": ["Only below 2^64"]}},
        ],
    }  # fmt: skip
    entries = [
        {"entry_id": "e1", "handle": "D-4", "revision": 2, "status": "rejected"},
        {"entry_id": "e2", "handle": "C-2", "revision": 3, "status": "proposed"},
    ]
    result = {"operation_id": "op", "entries": [{**e, "created": False} for e in entries]}
    digest = "ab" * 32
    sample = {
        "state": "succeeded",
        "stdout": f"order = 1234567890123456789 {digest}",
        "sample": "Shown here only in part",
        "saved": {"stdout": "call-output/x.txt"},
    }

    def add(entry: Entry) -> Archived:
        return archive.append(PROJECT, SESSION, "r", entry)

    def called(ident: str, tool: str, arguments: str) -> Json:
        return {"id": ident, "type": "function", "function": {"name": tool, "arguments": arguments}}

    calls = (called("c1", "ledger_change", json.dumps(change)), called("c2", "shell", "{}"))
    rows = [
        add(Entry("user", "task", "Look at ePrint 2024/1234.")),
        add(Entry("assistant", "assistant", None, tool_calls=calls)),
        add(Entry("tool", "tool_result", json.dumps(result), source="ledger_change", call_id="c1")),
        add(Entry("tool", "tool_result", json.dumps(sample), source="shell", call_id="c2")),
    ]
    record = build(rows, geometry(600_000, 30_000), 4.0)
    assert record is not None
    texts = [str(m.get("text")) for m in record.markers]
    assert "D-4 → rejected: Needs assumption Y; reopen if I-14 resolves" in texts
    assert "caveat on C-2: Only below 2^64" in texts
    assert any(t.startswith("call-output/x.txt: only a sample was shown") for t in texts)
    keys = dict(record.keys)
    assert keys["2024/1234"] == rows[0].message_id
    assert keys[digest] == keys["1234567890123456789"] == rows[3].message_id
    assert "order = 1234567890123456789" in keys
    assert any("ledger_change: D-4 → rejected, C-2 → proposed" in line for line in record.skeleton)
    # Stored and read back as it was built.
    stored = FoldRecord.of(
        archive.append(PROJECT, SESSION, "r", Entry(
            "user", "fold", record.text("this record", whole=True), source="folds",
            native=record.stored(), native_format="sandbox-fold-1",
        ))
    )  # fmt: skip
    assert stored == record


async def test_a_rebuilt_view_shows_the_records_in_its_region_and_names_those_left_out(
    archive: Archive,
) -> None:
    for n in range(30):
        worked(archive, "earlier", n, 3_000)
    made, transcript = folder(archive, 160_000, 4_000)  # each fold frees over a tenth
    made.geometry = made.start
    for _ in range(6):
        assert await made._fold(transcript, made.tokens(transcript))
    view = session_view(  # a region of 1,500 characters
        archive, CONTEXT, geometry=Geometry(10_000, 0, 2_000, 1.0), per_token=1.0
    )
    assert view.folds_left_out
    assert view.folds[-1] == made.folds[-1]["fold"]  # never the newest
    settled = await settle(view, archive, CONTEXT, run="r", kernels=None)
    text = body(str(settled.parts[0].message["content"]))
    assert f"the fold records {', '.join(view.folds_left_out)}, which history_read" in text


FOLD_EVENTS = 52
"""The version that adds the ``fold`` event kind."""


def test_a_store_from_before_fold_events_keeps_its_events_and_takes_folds(
    archive: Archive, tmp_path: Path
) -> None:
    """Version 52: the run events table is made again with ``fold`` among its kinds,
    its rows as they were."""
    source = tmp_path / "state" / "control.sqlite3"
    RunState(archive.control).append(PROJECT, SESSION, "r", "started", {"run_id": "r"})
    target = tmp_path / "older" / "control.sqlite3"
    target.parent.mkdir()
    older_store(source, target, FOLD_EVENTS - 1)
    control = ControlStore(target)
    try:
        state = RunState(control)
        state.append(PROJECT, SESSION, "r", "fold", {"fold": "msg-0123456789abcdef"})
        assert [(e.kind, e.data) for e in state.events(PROJECT, SESSION, "r")] == [
            ("started", {"run_id": "r"}),
            ("fold", {"fold": "msg-0123456789abcdef"}),
        ]
    finally:
        control.close()


# The review of #459.


async def test_a_large_archive_folds_to_fit_before_the_first_call(archive: Archive) -> None:
    """#459's review, H1: the first run after folds were deployed sees its whole archive
    raw. Folds before the run's first call catch up, each well under a tenth of so large
    a view; they do not count toward the breaker, and the view ends under the wall."""
    for n in range(200):
        worked(archive, "earlier", n, 4_000)  # about 200,000 tokens, folded never
    made, transcript = folder(archive, 160_000, 16_000)
    assert made.tokens(transcript) > 200_000
    assert await made.before_call(transcript)
    assert made.geometry is not None
    assert (made.weak, made.stopped) == (0, None)
    assert made.tokens(transcript) <= made.geometry.wall
    assert len(made.folds) >= 3


async def test_a_gateway_that_cannot_be_asked_is_asked_again(tmp_path: Path) -> None:
    """#459's review, M3: a failed lookup of the model's limits is not kept: the next
    run's start asks again."""
    asked: list[int] = []

    class Gateway:
        async def input_limits(self) -> dict[str, Limits] | None:
            asked.append(1)
            return None if len(asked) == 1 else {"main": Limits(90_000, None, None)}

    made = Harness_(cast(Any, None), Role(cast(Any, None), "main"), gateway=cast(Any, Gateway()))
    assert await made._ceiling() == 600_000
    assert await made._ceiling() == 90_000
    assert await made._ceiling() == 90_000
    assert len(asked) == 2


async def test_a_context_error_caps_the_ceiling_at_what_the_model_took(archive: Archive) -> None:
    """#459's review, M3, CM-43: after a context error, the ceiling is at most the largest
    input the model took in the run."""
    for n in range(30):
        worked(archive, "earlier", n, 4_000)
    made, transcript = folder(archive, 160_000, 10_000)
    assert await made.before_call(transcript)
    transcript.usage.append(ModelUsage(model="m", input_tokens=40_000, output_tokens=10))
    assert await made.overflow(transcript)
    assert made.geometry is not None
    assert made.geometry.ceiling == 40_000
    assert cast(Json, made.summary())["capped"] == 40_000


def recorded(archive: Archive, rows: list[Archived], markers: tuple[Json, ...]) -> str:
    """A fold record of ``rows`` with ``markers``, archived; its ID."""
    record = FoldRecord(
        level=1,
        first=(rows[0].position, rows[0].message_id),
        last=(rows[-1].position, rows[-1].message_id),
        runs=("earlier",),
        dates=(rows[0].created, rows[-1].created),
        skeleton=("An outline. " * 40,),
        markers=markers,
        keys=(),
    )
    entry = Entry("user", "fold", record.text("this record", whole=True), source="folds",
                  native=record.stored(), native_format="sandbox-fold-1")  # fmt: skip
    return archive.append(PROJECT, SESSION, "folder", entry).message_id


async def test_a_record_left_out_of_the_region_keeps_its_markers(archive: Archive) -> None:
    """#459's review, M4: a fold record the region leaves out, until overviews, keeps its
    marker lines in the state block, so no decision or caveat is lost meanwhile."""
    first = [archive.append(PROJECT, SESSION, "earlier", Entry("user", "task", "Look."))]
    decision = {"kind": "decision", "subject": "D-4", "text": "D-4 → rejected: too slow"}
    old = recorded(archive, first, (decision,))
    second = [archive.append(PROJECT, SESSION, "earlier", Entry("user", "user", "More."))]
    recorded(archive, second, ({"kind": "caveat", "subject": "C-2", "text": "only below 2^64"},))
    view = session_view(archive, CONTEXT, geometry=Geometry(4_000, 0, 2_000, 1.0), per_token=1.0)
    assert view.folds_left_out == (old,)
    settled = await settle(view, archive, CONTEXT, run="r", kernels=None)
    text = body(str(settled.parts[0].message["content"]))
    assert f"the fold records {old}, which history_read returns whole; their marker lines" in text
    assert "- decision: D-4 → rejected: too slow" in text


def test_the_marker_kinds_cm_18_names(archive: Archive) -> None:
    """#459's review, M5, CM-F9: a source the read register says the span read in part,
    an evidence scope note, finite-test and numerical evidence, and a check that ended
    inconclusive each make a marker; a source read whole makes none."""

    def add(entry: Entry, reads: tuple[Read, ...] = ()) -> Archived:
        return archive.append(PROJECT, SESSION, "r", entry, reads=reads)

    def called(ident: str, tool: str, arguments: str) -> Json:
        return {"id": ident, "type": "function", "function": {"name": tool, "arguments": arguments}}

    change = {
        "reason": "Measured",
        "changes": [{"entry": "C-3", "expected": 1, "evidence": [
            {"role": "supports", "type": "finite_test", "scope": "n below 2^20"},
            {"role": "supports", "type": "proof", "scope": "prime fields only"},
        ]}],
    }  # fmt: skip
    result = {"operation_id": "o", "entries": [
        {"entry_id": "e", "handle": "C-3", "revision": 2, "status": "proposed", "created": False}
    ]}  # fmt: skip
    calls = (
        called("c1", "pdf_pages", "{}"),
        called("c2", "ledger_change", json.dumps(change)),
        called("c3", "pdf_pages", "{}"),
    )
    partial = Read("pdf", "a" * 64, "partial", {"pages": [1, 2, 3], "of": 40}, label="paper.pdf")
    whole = Read("pdf", "b" * 64, "partial", {"pages": [1], "of": 1}, label="note.pdf")
    outcome = {"delegations": [{"delegation_id": "dlg-1", "target": {"handle": "C-3"},
                                "result": {"verdict": "inconclusive"}}]}  # fmt: skip
    rows = [
        add(Entry("assistant", "assistant", None, tool_calls=calls)),
        add(Entry("tool", "tool_result", "pages", source="pdf_pages", call_id="c1"), (partial,)),
        add(Entry("tool", "tool_result", json.dumps(result), source="ledger_change", call_id="c2")),
        add(Entry("tool", "tool_result", "page 1", source="pdf_pages", call_id="c3"), (whole,)),
        add(Entry("user", "outcome", "Delegation outcomes: " + json.dumps(outcome),
                  source="delegations", refs=("dlg-1",))),
    ]  # fmt: skip
    reads = Register(archive.control).of_messages(PROJECT, [row.message_id for row in rows])
    record = build(rows, geometry(600_000, 30_000), 4.0, reads=reads)
    assert record is not None
    texts = [str(marker.get("text")) for marker in record.markers]
    assert (
        f"paper.pdf: read partial: pages 1-3 of 40 (result {rows[1].message_id}); not read whole"
        in texts
    )
    assert not any(text.startswith("note.pdf") for text in texts)
    assert "evidence on C-3 is a finite test, scoped to: n below 2^20" in texts
    assert "evidence on C-3 (proof) is scoped to: prime fields only" in texts
    assert f"check dlg-1 of C-3 ended inconclusive (in {rows[4].message_id})" in texts


def test_a_cut_skeleton_keeps_the_users_lines_and_ledger_changes() -> None:
    """#459's review: a skeleton over its share keeps every user line and ledger change,
    and cuts only other lines, each run of them named."""
    lines = [f"call {n} " + "x" * 200 for n in range(20)]
    lines[10] = "user: the constraint"
    lines[11] = "ledger_change: D-4 → rejected"
    kept = [n in (10, 11) for n in range(20)]
    fitted = _fit(lines, 1_500, kept)
    assert "user: the constraint" in fitted
    assert "ledger_change: D-4 → rejected" in fitted
    assert sum(len(line) + 1 for line in fitted) <= 1_500 + 200
    assert any(line.startswith("[... ") for line in fitted)


def test_a_record_frees_tokens_only_against_its_span_as_sent(archive: Archive) -> None:
    """#459's review: a record is no use if it is no smaller than its span as the view
    sends it, stubs and all."""
    rows = [archive.append(PROJECT, SESSION, "r", Entry("user", "task", "Look. " * 200))]
    shape = geometry(600_000, 30_000)
    assert build(rows, shape, 4.0) is not None
    assert build(rows, shape, 4.0, sent=100) is None


async def test_a_span_too_large_for_its_record_is_folded_smaller(archive: Archive) -> None:
    """#459's review: a span whose record passes its share, as many ledger changes kept
    whole make it, is built again of half its span until one fits."""
    for n in range(12):
        change = {"reason": f"Reason {n}: " + "r" * 300, "changes": []}
        function = {"name": "ledger_change", "arguments": json.dumps(change)}
        calls = ({"id": f"c{n}", "type": "function", "function": function},)
        asked = Entry("assistant", "assistant", None, tool_calls=calls)
        archive.append(PROJECT, SESSION, "earlier", asked)
        result = json.dumps({"operation_id": "o", "entries": []})
        done = Entry("tool", "tool_result", result, source="ledger_change", call_id=f"c{n}")
        archive.append(PROJECT, SESSION, "earlier", done)
    worked(archive, "earlier", 99, 40_000)
    made, transcript = folder(archive, 160_000, 4_000)
    made.geometry = made.start
    assert await made._fold(transcript, made.tokens(transcript))
    (fold,) = made.folds
    assert fold["span"][1] - fold["span"][0] + 1 < 24  # fewer than the chunk's 12 calls


# Fixtures at scale: a run's messages archived as it adds them, folded as it goes.


class Run:
    """A run on the archive: a folder from its session's view, and the messages it adds,
    archived as the run's log keeps them and added to its transcript."""

    def __init__(self, archive: Archive, name: str, made: Folder, system: int) -> None:
        self.archive, self.name, self.made = archive, name, made
        self.transcript = Transcript(
            messages=[{"role": "system", "content": "s" * system}, *made.view.messages]
        )
        self.calls = 0

    @classmethod
    async def start(cls, archive: Archive, name: str, top: int, chunk: int, system: int) -> Run:
        """A run as the harness starts one: from its session's view, settled."""
        start = geometry(top, 0, chunk)
        view = session_view(archive, CONTEXT, geometry=start)
        view = await settle(view, archive, CONTEXT, run=name, kernels=None)
        made = Folder(archive, CONTEXT, run=name, view=view, start=start, ceiling=top,
                      chunk=chunk)  # fmt: skip
        return cls(archive, name, made, system)

    def add(self, entry: Entry) -> Archived:
        row = self.archive.append(PROJECT, SESSION, self.name, entry)
        self.transcript.messages.append(row.chat())
        return row

    def work(self, size: int, *, answered: bool = True) -> None:
        """One call and, unless the run is interrupted, its result of ``size``."""
        self.calls += 1
        ident = f"{self.name}-{self.calls}"
        function = {"name": "shell", "arguments": json.dumps({"n": self.calls})}
        calls = ({"id": ident, "type": "function", "function": function},)
        self.add(Entry("assistant", "assistant", f"Step {self.calls}.", tool_calls=calls))
        if answered:
            text = json.dumps({"state": "succeeded", "stdout": "0" * size})
            self.add(Entry("tool", "tool_result", text, source="shell", call_id=ident))

    async def safe_point(self) -> bool:
        """A safe point before a model call, and the call made: it reports nothing, so the
        view is estimated afresh, as the run's own calls count toward the breaker."""
        found = await self.made.before_call(self.transcript)
        self.transcript.usage.append(ModelUsage(model="m"))
        return found


def spans_of(archive: Archive) -> list[list[int]]:
    rows: list[Archived] = []
    start = 1
    while True:
        page = archive.messages(PROJECT, SESSION, start=start)
        rows += page
        if len(page) < 200:
            break
        start = page[-1].position + 1
    return [[f.first[0], f.last[0]] for row in rows if (f := FoldRecord.of(row)) is not None]


async def test_thirty_folds_in_the_steady_state_never_trip_the_breaker(archive: Archive) -> None:
    """CM-F6's steady state, CM-27, CM-44: at C = 168,000 with 30,000 tokens of system
    message and tools, λ is about 0.83; with the user's words and the fold region full,
    every fold at the trigger takes at least 25,000 tokens and frees more than a tenth of
    the view, through 30 folds, and the breaker never trips."""
    run = await Run.start(archive, "run-1", 168_000, 50_000, 120_000)
    await run.safe_point()
    assert run.made.geometry is not None
    assert round(run.made.geometry.scale, 2) == 0.83
    while len(run.made.folds) < 30:
        run.add(Entry("user", "user", "A note of mine. " * 160, source="messages", refs=("m",)))
        for _ in range(8):
            run.work(4_500)
        before = len(run.made.folds)
        assert await run.safe_point()
        for fold in run.made.folds[before:]:
            first, last = fold["span"]
            taken = archive.messages(PROJECT, SESSION, start=first, end=last)
            assert sum(len(dump(row.chat())) for row in taken) / 4 >= 25_000
            assert fold["freed"] > 0
    assert (run.made.weak, run.made.stopped) == (0, None)
    assert all(fold["freed"] > 0.1 * 126_000 for fold in run.made.folds)
    assert run.made.view.folds_left_out  # the region filled: the oldest left out
    assert run.made.view.folded is not None


async def test_a_session_with_an_interrupted_run_folds_without_losing_a_message(
    archive: Archive,
) -> None:
    """CM-F1: across six runs, one interrupted midway, with a ceiling low enough for at
    least five folds: every request stays under C; folds are contiguous and never
    overlap; every message reads back whole by its ID; and the last view rebuilds from
    the archive as its record names it."""
    sizes: list[tuple[str, int]] = []
    largest = 0
    runs: list[Run] = []
    for number in range(1, 7):
        run = await Run.start(archive, f"run-{number}", 100_000, 10_000, 4_000)
        runs.append(run)
        task = run.add(Entry("user", "task", f"Task {number}."))
        sizes.append((task.message_id, len(task.entry.content or "")))
        for step in range(25):
            run.work(4_500, answered=not (number == 3 and step == 6))
            if number == 3 and step == 6:
                break  # the run is interrupted during a call
            assert await run.safe_point()
            largest = max(largest, run.made.tokens(run.transcript))
    assert largest <= 100_000
    spans = spans_of(archive)
    assert len(spans) >= 5
    assert all(a[1] < b[0] for a, b in pairwise(spans))  # contiguous, never overlapping
    folded = [position for first, last in spans for position in range(first, last + 1)]
    assert len(folded) == len(set(folded))
    for message, size in sizes:
        assert len(archive.message(PROJECT, message).entry.content or "") == size
    made = runs[-1].made
    rebuilt = session_view(archive, CONTEXT, geometry=made.geometry or made.start)
    last = made.rebuilds[-1]
    assert list(rebuilt.folds) == last["folds"]
    assert list(rebuilt.stubs) == last["stubs"]
    # The interrupted call went into a fold, with no result: the record says so.
    interrupted = archive.messages(PROJECT, SESSION, run="run-3")
    assert any(row.position <= spans[-1][1] for row in interrupted)


async def test_the_users_constraint_survives_twenty_runs_of_folds(archive: Archive) -> None:
    """CM-F2: a constraint in run 1's first message is in every request after every
    fold, byte for byte, in the raw tail or in the state block, through 20 runs."""
    constraint = "Only attacks with memory below 2^64, please."
    folds = 0
    for number in range(1, 21):
        run = await Run.start(archive, f"run-{number}", 100_000, 10_000, 4_000)
        run.add(Entry("user", "task", constraint if number == 1 else f"Go on, part {number}."))
        for _ in range(10):
            run.work(4_500)
            assert await run.safe_point()
            assert constraint in json.dumps(run.transcript.sent, ensure_ascii=False)
        folds += len(run.made.folds)
    assert folds >= 10
