# SPDX-License-Identifier: MIT
"""The session view a main run starts from (docs/specs/context-management.md, CM-25,
CM-28 to CM-30, CM-29a to CM-29c): CM-F2, CM-F17, CM-F24, CM-F28 and CM-F29, as far as
step (e1) builds them, before folds. Runs go through ``cli.main`` against the in-process
worker and the scripted gateway; views are also built from archives written through the
archive's own API."""

from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
from collections.abc import Iterator, Sequence
from datetime import timedelta
from pathlib import Path
from typing import Any, cast

import pytest

from sandbox_science.contracts import ExecutionContext
from sandbox_science.orchestrator.control import ControlError, ControlStore
from sandbox_science.orchestrator.documents.tool import REQUESTS_TASK
from sandbox_science.orchestrator.history import (
    Archive,
    Entry,
    HistoryTool,
    View,
    body,
    session_view,
    settle,
)
from sandbox_science.orchestrator.history.view import ABORTED, SHARE, WORDS
from sandbox_science.orchestrator.notices import (
    DIGEST,
    KERNEL_LINES,
    REDELIVERED,
    SKILLS_WORDS,
    STATE_BLOCK,
)
from sandbox_science.orchestrator.projects import ModelUsage, ProjectStore, RunRecord, run_name
from sandbox_science.orchestrator.runstate import RunState
from sandbox_science.orchestrator.skills.tool import given
from sandbox_science.orchestrator.tokens import (
    chars_per_token,
    estimate,
    total_input,
    view_tokens,
)
from sandbox_science.orchestrator.worker_client import dump

from .loop_harness import PROJECT, SESSION, TASK, Harness, Reply, call, own
from .loop_harness import gateway as gateway
from .loop_harness import harness as harness
from .loop_harness import worker as worker
from .test_delegation import CHECK, REPORT, wait
from .test_delegation import delegating as delegating
from .test_skills_index import _pin
from .test_skills_index import pack as pack
from .test_skills_index import plain_git as plain_git

Json = dict[str, Any]
CONTEXT = ExecutionContext(project_id=PROJECT, session_id=SESSION, environment_id="e")
CONSTRAINT = "Only attacks with memory below 2^64, please."


@pytest.fixture
def archive(tmp_path: Path) -> Iterator[Archive]:
    """An archive whose project has the session, with nothing in it yet."""
    projects = ProjectStore(tmp_path / "state")
    projects.create(PROJECT, "minimal", "sha256:" + "c" * 64)
    projects.open_session(PROJECT, SESSION, dedicated=False)
    yield Archive(projects.control)
    projects.close()


def name(record: RunRecord) -> str:
    return run_name(record.started_at, record.run_id)


def said(archive: Archive, run: str, text: str, chat_id: str) -> str:
    entry = Entry("user", "user", text, source="messages", refs=(chat_id,))
    return archive.append(PROJECT, SESSION, run, entry).message_id


def answered(archive: Archive, run: str, *results: str) -> None:
    """An answer that called the shell once for each of ``results``, and their results."""
    calls = [
        {"id": f"{run}-c{n}", "type": "function", "function": {"name": "shell", "arguments": "{}"}}
        for n in range(len(results))
    ]
    entry = Entry("assistant", "assistant", "Working.", tool_calls=tuple(calls))
    archive.append(PROJECT, SESSION, run, entry)
    for n, text in enumerate(results):
        result = Entry("tool", "tool_result", text, source="shell", call_id=f"{run}-c{n}")
        archive.append(PROJECT, SESSION, run, result)


async def test_a_run_starts_from_its_sessions_view(harness: Harness) -> None:
    """CM-F17, CM-29, CM-29c: a second run's view holds the first run's messages as
    they were sent, its cell and its result among them, after a line naming the kernel
    the cell ran in and whether it still runs."""
    first = await harness.run(
        "run-1", [Reply(calls=[call("python", code="x = 41")]), Reply("Set x.")]
    )
    sent = [m for m in harness.gateway.main[-1] if m["role"] != "system"]
    second = await harness.run("run-2", [Reply("x is 41.")])
    (request,) = harness.gateway.main
    kernel_line, *view = request[1 : 1 + 1 + len(sent) + 1]
    assert view == [*sent, first.messages[-1]]
    assert str(kernel_line["content"]).startswith(KERNEL_LINES.text)
    line = json.loads(str(kernel_line["content"]).removeprefix(KERNEL_LINES.text))
    (kernel,) = line["kernels"]
    assert (line["run"], kernel["language"], kernel["now"]) == (name(first), "python", "running")
    assert body(str(request[len(view) + 2]["content"])) == TASK
    summary = cast(Json, second.summaries["view"])
    assert isinstance(summary, dict)
    assert summary["parts"] == len(view) + 1
    assert summary["positions"] == [1, len(view)]
    assert summary["left_out"] is None
    assert summary["kernel_lines"] == [kernel_line["content"]]  # CM-46
    # Each part's tokens at the session's characters per token, rounded up.
    assert summary["tokens"] >= estimate(summary["chars"], summary["chars_per_token"])


def test_token_accounting_normalizes_each_api_and_counts_from_the_last_report() -> None:
    """CM-F24, CM-28: three APIs' usage give one total input, and a view's size is a
    fresh estimate until a call reports, then that report and what was added since."""
    claude = {"input_tokens": 2000, "cache_read_input_tokens": 400_000}
    claude["cache_creation_input_tokens"] = 10_000
    gpt = {"input_tokens": 412_000, "input_tokens_details": {"cached_tokens": 400_000}}
    assert total_input(claude, "anthropic_messages") == 412_000
    assert total_input(gpt, "openai_responses") == 412_000
    assert total_input({"prompt_tokens": 412_000}, "chat_completions") == 412_000
    assert total_input({"input_tokens": 5}, "anthropic_messages") == 5  # no cache used
    assert total_input({"completion_tokens": 5}, "chat_completions") is None
    calls = [ModelUsage(input_tokens=1000, output_tokens=100, characters=5500)]
    assert chars_per_token(calls) == 5.0
    assert chars_per_token([ModelUsage(characters=10)]) == 4.0  # nothing reported yet
    assert view_tokens(8000, [], 5.0) == 1600  # after a rebuild: a fresh estimate
    assert view_tokens(8000, calls, 5.0) == 1000 + 100 + 500  # then from the report


async def test_each_call_measures_its_view_and_a_model_calibrates_its_own(
    harness: Harness,
) -> None:
    """CM-F24, CM-28: a run's record keeps each call's estimate beside what the gateway
    reported; the first is a fresh estimate of the view, the next counts from that
    report. The characters per token come from the session's calls on the run's alias."""
    await harness.run("run-1", [Reply(calls=[call("plan_read")]), Reply("Read.")])
    second = await harness.run("run-2", [Reply(calls=[call("plan_read")]), Reply("Again.")])
    summary = cast(Json, second.summaries["view"])
    assert isinstance(summary, dict)
    first_call, next_call = summary["calls"]
    usage = second.usage[0]
    assert [call["reported"] for call in summary["calls"]] == [
        call.input_tokens for call in second.usage
    ]
    # The next call counts from the first one's report, and what was added since.
    assert next_call["estimated"] >= (usage.input_tokens or 0) + (usage.output_tokens or 0)
    assert first_call["estimated"] is not None
    projects = ProjectStore(harness.state)
    try:
        calls = projects.usage(PROJECT, SESSION, "main")
        assert projects.usage(PROJECT, SESSION, "another-model") == []
    finally:
        projects.close()
    assert len(calls) == 4
    assert summary["chars_per_token"] == round(chars_per_token(calls[:2]), 3)


async def test_the_oldest_messages_are_left_out_and_the_users_words_kept(
    archive: Archive,
) -> None:
    """CM-F2, CM-26, CM-30, until folds: a view past its share leaves out its oldest
    messages, and its state block keeps the user's words from them, oldest first; past
    their share the oldest become one-line pointers, never the newest; a long message
    shows its head and tail; every message the view holds is whole or named."""
    budget = 40_000
    first = said(archive, "run-1", CONSTRAINT, "m-1")
    answered(archive, "run-1", "x" * 6_000)
    olds = [said(archive, f"run-{n}", f"Note {n}: " + "y" * 900, f"m-{n}") for n in (2, 3, 4)]
    long = said(archive, "run-5", "z" * 30_000 + " END-OF-LONG", "m-5")
    answered(archive, "run-5", "w" * 6_000)
    last = said(archive, "run-6", "The latest question.", "m-6")
    answered(archive, "run-6", "v" * 3_000)
    view = session_view(archive, CONTEXT, budget=budget)
    settled = await settle(view, archive, CONTEXT, run="run-7", kernels=None)
    block, *kept = settled.parts
    text = body(str(block.message["content"]))
    # The newest messages are whole in the raw tail; what went before is named.
    assert [p.row.message_id for p in kept if p.row and p.row.entry.kind == "user"] == [last]
    assert sum(len(dump(p.message)) for p in kept) <= budget * (SHARE - WORDS)
    assert view.left_out == (1, kept[0].row.position - 1 if kept[0].row else 0)
    assert text.startswith(STATE_BLOCK.text + "Left out: the messages at positions 1 to ")
    words = text[text.index("--- The user's own words ---") :]
    assert len(words) <= budget * WORDS + 200
    # The oldest became pointers; the newest left out is shown, a long one as head and tail.
    (pointer,) = [line for line in words.splitlines() if line.startswith(f"--- {first} ")]
    assert pointer.endswith(
        f'{len(CONSTRAINT)} characters, beginning "{CONSTRAINT}"; history_read returns it whole ---'
    )
    assert "END-OF-LONG" in words
    assert f"history_read {long} returns the message whole" in words
    assert all(old in words for old in olds)
    # The user's messages the view shows whole, raw tail or state block, are shown.
    assert view.shown["m-6"] == last
    assert view.shown["m-1"] == first  # a pointer that holds the whole message
    assert "m-2" not in view.shown  # a pointer to a longer one
    assert "m-5" not in view.shown  # shown only as its head and tail


async def test_a_pointer_reads_back_whole(archive: Archive) -> None:
    """CM-30: the message a pointer names reads back whole with history_read."""
    first = said(archive, "run-1", CONSTRAINT + " " + "q" * 5_000, "m-1")
    for n in range(2, 8):
        said(archive, f"run-{n}", f"Later {n}. " + "r" * 3_000, f"m-{n}")
        answered(archive, f"run-{n}", "s" * 5_000)
    view = await settle(
        session_view(archive, CONTEXT, budget=40_000), archive, CONTEXT, run="r", kernels=None
    )
    assert f"--- {first} · user message · " in str(view.parts[0].message["content"])
    tool = HistoryTool(archive, CONTEXT, run="r", delegated=False)
    text, _ = await tool.call("history_read", {"message": first})
    assert json.loads(text)["text"] == CONSTRAINT + " " + "q" * 5_000


async def test_the_main_view_holds_main_runs_and_answers_a_held_message_once(
    delegating: Harness,
) -> None:
    """CM-F28, CM-29a, RS-12: the next run's view holds the main runs' messages, the
    outcome the root received, and an interrupted call with an aborted result; none of
    the child's messages, no result without its call; and the held message once, where it
    first arrived, the new run told to answer it there."""
    root = await delegating.run(
        "root", [Reply(calls=[CHECK]), wait(), Reply("Checked.")], delegated=[Reply(REPORT)]
    )
    store = ProjectStore(delegating.state)
    try:
        archive = Archive(store.control)
        held = said(archive, "gone", "Did the check finish?", "m-9")
        call_ = {
            "id": "gone-1",
            "type": "function",
            "function": {"name": "shell", "arguments": "{}"},
        }
        archive.append(
            PROJECT, SESSION, "gone", Entry("assistant", "assistant", None, tool_calls=(call_,))
        )
        orphan = Entry("tool", "tool_result", "an orphan", source="shell", call_id="nobody")
        archive.append(PROJECT, SESSION, "gone", orphan)
        RunState(store.control).post(PROJECT, SESSION, "m-9", "Did the check finish?", author="u")
    finally:
        store.close()
    after = await delegating.run("after", [Reply("Yes, it confirmed C-1.")])
    (request,) = delegating.gateway.main
    text = json.dumps(request)
    assert "Check whether ledger entry C-1 holds" not in text  # the child's brief
    assert "an orphan" not in text
    assert text.count("Did the check finish?") == 1
    assert any(m["role"] == "tool" and m["content"] == ABORTED for m in request)
    for index, message in enumerate(request):
        for made in message.get("tool_calls") or []:
            assert any(m.get("tool_call_id") == made["id"] for m in request[index + 1 :])
    assert body(str(request[-1]["content"])) == f'{REDELIVERED.text}"{held}"'
    assert after.delivered["messages"] == ["m-9"]
    root_messages = [m for m in own(root) if m["role"] != "system"]
    # After the system message, the state block with the research state's digest (CM-26).
    assert DIGEST.text in str(request[1]["content"])
    assert request[2 : 2 + len(root_messages)] == [*root_messages[:-1], root.messages[-1]]


async def test_a_skill_body_the_view_holds_counts_as_loaded(harness: Harness, pack: Path) -> None:
    """CM-F29, CM-29b, SK-12: a skill run 1 loaded, whose body run 2's view still holds,
    loads in run 2 as the short note."""
    _pin(harness.state, pack)
    load = call("skills", action="load", names=["alpha"])
    await harness.run("run-1", [Reply(calls=[load]), Reply("Loaded.")])
    two = await harness.run("run-2", [Reply(calls=[load]), Reply("Again.")])
    (result,) = [m for m in own(two) if m["role"] == "tool"]
    assert "ss/alpha is already loaded; its text is earlier on." in str(result["content"])


def test_a_body_or_a_notice_left_out_of_the_view_counts_for_nothing(archive: Archive) -> None:
    """CM-29b: only the bodies and discovery notices the view holds count; one the view
    leaves out loads whole again, and its skills may be shown again."""
    load = (
        "ss/alpha at abc, body SHA-256 " + "a" * 64 + ": the pack's own text.\n"
        "<skill-0123>\nSS-MARKER-ALPHA-1\n</skill-0123>"
    )
    answered(archive, "run-1", load)
    archive.append(PROJECT, SESSION, "run-1", Entry("user", "notice", "x", source="skills"))
    assert given([load]) == {"ss/alpha": ("a" * 64, len("SS-MARKER-ALPHA-1"))}
    whole = session_view(archive, CONTEXT, budget=10**6)
    assert whole.loaded == {}  # the result's source is shell here, not the skills tool
    skills = Entry("tool", "tool_result", load, source="skills", call_id="run-2-c0")
    calls = {
        "id": "run-2-c0",
        "type": "function",
        "function": {"name": "skills", "arguments": "{}"},
    }
    archive.append(
        PROJECT, SESSION, "run-2", Entry("assistant", "assistant", None, tool_calls=(calls,))
    )
    archive.append(PROJECT, SESSION, "run-2", skills)
    listed = SKILLS_WORDS.text + '{"skills":[{"id":"ss/beta"}]}'
    shown = Entry("user", "notice", listed, source="skills")
    archive.append(PROJECT, SESSION, "run-2", shown)
    view = session_view(archive, CONTEXT, budget=10**6)
    assert view.loaded == {"ss/alpha": ("a" * 64, len("SS-MARKER-ALPHA-1"))}
    assert view.discovered == {"ss/beta"}
    answered(archive, "run-3", *["p" * 9_000] * 4)
    cut = session_view(archive, CONTEXT, budget=40_000)
    assert cut.left_out is not None
    assert (cut.loaded, cut.discovered) == ({}, frozenset())


async def test_a_run_whose_session_outgrew_its_share_starts_from_a_state_block(
    harness: Harness,
) -> None:
    """CM-26, CM-30, until folds: a run whose session's messages pass its view's share
    begins with an archived state block that names what it left out and keeps the user's
    words from it; the run's context limit still stops a run that does not fit."""
    output = "printf '%.0s0123456789' $(seq 1 3000)"
    await harness.run("run-1", [Reply(calls=[call("shell", script=output)] * 2), Reply("Printed.")])
    small = await harness.run("run-2", [], status=3, max_input_chars=30_000)
    assert small.stopped == "context_limit"
    block = small.messages[1]
    assert str(block["content"]).split("\n", 1)[0].endswith(" \u00b7 state block]")
    text = body(str(block["content"]))
    summary = cast(Json, small.summaries["view"])
    assert isinstance(summary, dict)
    assert str(summary["state_block"]).startswith("msg-")
    # Run 1's task, the user's latest words, is kept whole though its calls are left out,
    # and so is the newest unit, its answer.
    kept = summary["kept_apart"]
    assert text == f"{STATE_BLOCK.text}Left out: the messages at positions 2 to 5. "
    assert str(small.messages[2]["content"]).startswith(f"[{kept} \u00b7 task]")
    assert body(str(small.messages[2]["content"])) == TASK
    assert small.messages[3]["content"] == "Printed."


# The review of #454.


async def test_the_users_words_keep_their_tenth_however_many_there_are(
    archive: Archive,
) -> None:
    """CM-30: with about 2,000 of the user's messages left out, even their one-line
    pointers pass the words' share; the oldest become one line naming the range, which
    the history tools find, and the words keep to their tenth of the input."""
    budget = 100_000
    ids = [
        said(archive, f"run-{n // 20}", f"Note {n}: " + "y" * 60, f"m-{n}") for n in range(2_000)
    ]
    answered(archive, "run-100", "w" * 20_000)
    view = session_view(archive, CONTEXT, budget=budget)
    assert view.kept_apart is None  # the latest message is among the newest kept
    shown = [p.row.message_id for p in view.parts if p.row and p.row.entry.kind == "user"]
    assert shown == ids[-len(shown) :]
    out = ids[: -len(shown)]
    words = view.words or ""
    assert len(words) <= budget * WORDS + 100  # its framing lines
    lines = [line for line in words.splitlines() if line.startswith("--- ")]
    gone = int(lines[1].split()[1].replace(",", ""))
    assert lines[1] == (
        f"--- {gone:,} earlier messages, {ids[0]} to {ids[gone - 1]}; history_search and "
        "history_read find them ---"
    )
    # Then one line for each message after them, and the newest left out whole.
    pointers = lines[2:-2]
    assert [line.split()[1] for line in pointers] == out[gone:-1]
    newest = archive.message(PROJECT, out[-1])
    assert lines[-2] == f"--- {out[-1]} · user message · {newest.created} ---"
    assert f"{newest.entry.content}\n--- End of the user's own words ---" in words
    found = HistoryTool(archive, CONTEXT, run="r", delegated=False)
    text, _ = await found.call("history_read", {"message": ids[0]})
    assert json.loads(text)["text"] == "Note 0: " + "y" * 60


def test_the_newest_unit_and_the_users_latest_message_are_always_kept(
    archive: Archive,
) -> None:
    """CM-26, until folds: a newest unit larger than the view's room is kept whole, the
    context limit the backstop; and so is the user's latest message, though messages
    after it are left out."""
    said(archive, "run-1", "Old words.", "m-1")
    answered(archive, "run-1", "a" * 1_000)
    question = said(archive, "run-2", "The question.", "m-2")
    answered(archive, "run-2", "b" * 30_000)
    answered(archive, "run-3", "c" * 30_000)
    view = session_view(archive, CONTEXT, budget=40_000)
    kinds = [(p.kind, p.row.run if p.row else None) for p in view.parts]
    assert kinds == [("user", "run-2"), ("assistant", "run-3"), ("tool_result", "run-3")]
    assert view.left_out == (1, 6)
    assert view.kept_apart == question
    assert view.shown == {"m-2": question, "m-1": view.shown["m-1"]}
    assert "Old words." in (view.words or "")
    assert "The question." not in (view.words or "")
    # Without a message from the user before it, the newest unit alone.
    alone = session_view(archive, CONTEXT, budget=10_000)
    assert [p.kind for p in alone.parts] == ["user", "assistant", "tool_result"]


async def test_the_state_block_names_what_it_kept_apart(archive: Archive) -> None:
    said(archive, "run-1", "Old words.", "m-1")
    question = said(archive, "run-2", "The question.", "m-2")
    answered(archive, "run-2", "b" * 30_000)
    answered(archive, "run-3", "c" * 30_000)
    settled = await settle(
        session_view(archive, CONTEXT, budget=40_000), archive, CONTEXT, run="r", kernels=None
    )
    text = body(str(settled.parts[0].message["content"]))
    assert text.startswith(
        f"{STATE_BLOCK.text}Left out: the messages at positions 1 to 4, but for the user's "
        f"latest message, {question}, which is kept. "
    )
    assert settled.summary()["kept_apart"] == question


def test_results_are_paired_with_their_calls_by_id_within_each_run(archive: Archive) -> None:
    """CM-29a: a call's result follows it in the view however the archive ordered it,
    with a notice between or the results swapped; a call ID another run used again finds
    only its own run's result, and a run's reused ID each its next result."""

    def calls(*names: str) -> Entry:
        made = tuple(
            {"id": name, "type": "function", "function": {"name": "shell", "arguments": "{}"}}
            for name in names
        )
        return Entry("assistant", "assistant", None, tool_calls=made)

    def result(name: str, text: str) -> Entry:
        return Entry("tool", "tool_result", text, source="shell", call_id=name)

    add = lambda run, entry: archive.append(PROJECT, SESSION, run, entry)  # noqa: E731
    add("run-1", calls("c1", "c2"))
    add("run-1", Entry("user", "notice", "A notice between.", source="plans"))
    add("run-1", result("c2", "second"))
    add("run-1", result("c1", "first"))
    add("run-1", calls("c1"))
    add("run-1", result("c1", "again"))
    add("run-2", calls("c1"))  # its result never came
    view = session_view(archive, CONTEXT, budget=10**6)
    sent = [
        (m["role"], m.get("tool_call_id"), body(str(m.get("content") or ""))) for m in view.messages
    ]
    assert sent == [
        ("assistant", None, ""),
        ("tool", "c1", "first"),
        ("tool", "c2", "second"),
        ("user", None, "A notice between."),
        ("assistant", None, ""),
        ("tool", "c1", "again"),
        ("assistant", None, ""),
        ("tool", "c1", ABORTED),
    ]


async def test_a_run_from_before_the_archive_keeps_the_users_words_from_its_record(
    harness: Harness,
) -> None:
    """RS-10, as the session view keeps it: a main run whose record was saved before the
    archive existed has no messages in it; its task joins the user's words in the next
    run's state block, from its saved record."""
    first = await harness.run("run-1", [Reply("Done.")])
    store = ProjectStore(harness.state)
    try:
        started = first.started_at - timedelta(hours=1)
        old = first.model_copy(
            update={"run_id": "run-0", "task": "An old question.", "started_at": started}
        )
        store.save_run(old)
    finally:
        store.close()
    second = await harness.run("run-2", [Reply("Again.")])
    text = body(str(second.messages[1]["content"]))
    assert text.startswith(
        f"{STATE_BLOCK.text}1 earlier runs ran before the archive existed: their messages are "
        "neither shown nor searched; the user's messages from them are among the user's "
        "words below, as their runs' records keep them. "
    )
    assert f"--- run {name(old)} · task ---\nAn old question." in text
    summary = cast(Json, second.summaries["view"])
    assert (summary["unarchived_runs"], summary["recorded_messages"]) == (1, 1)


def test_the_users_words_are_the_records_first_and_never_a_stand_in_task(
    archive: Archive,
) -> None:
    """CM-30, RS-10: the words from runs before the archive come first, as ``earlier``
    reads them for the runs the archive lacks; the stand-in task of a run started for
    comments is not the user's words, nor the user's latest message."""
    archive.append(PROJECT, SESSION, "run-1", Entry("user", "task", REQUESTS_TASK))
    said(archive, "run-1", "An earlier message.", "m-1")
    latest = said(archive, "run-1", "The latest message.", "m-2")
    answered(archive, "run-1", "a" * 30_000)
    archive.append(PROJECT, SESSION, "run-2", Entry("user", "task", REQUESTS_TASK))
    answered(archive, "run-2", "b" * 30_000)
    asked: list[list[str]] = []

    def earlier(runs: Sequence[str]) -> list[tuple[str, str, str]]:
        asked.append(list(runs))
        return [(run, "user message", f"Said in {run}.") for run in runs]

    view = session_view(
        archive, CONTEXT, budget=40_000, runs=["run-0", "run-1", "run-2"], earlier=earlier
    )
    assert asked == [["run-0"]]
    assert (view.unarchived, view.recorded, view.kept_apart) == (1, 1, latest)
    words = view.words or ""
    assert REQUESTS_TASK not in words
    assert words.index("--- run run-0 · user message ---\nSaid in run-0.") < words.index(
        "An earlier message."
    )


async def test_the_state_block_waits_for_a_busy_store(tmp_path: Path) -> None:
    """CM-4 for the state block: its write waits for a busy store, as a run's do."""
    projects = ProjectStore(tmp_path / "state")
    projects.create(PROJECT, "minimal", "sha256:" + "c" * 64)
    projects.open_session(PROJECT, SESSION, dedicated=False)
    projects.close()
    path = tmp_path / "state" / "control.sqlite3"
    control = ControlStore(path, timeout=0.05)
    holder = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
    try:
        holder.execute("BEGIN IMMEDIATE")
        threading.Timer(0.8, lambda: holder.execute("COMMIT")).start()
        view = View(words="\n(words)", unarchived=1)
        settled = await settle(view, Archive(control), CONTEXT, run="r", kernels=None)
        assert settled.parts[0].kind == "state_block"
        holder.execute("BEGIN IMMEDIATE")
        with pytest.raises(ControlError) as refused:
            await settle(view, Archive(control), CONTEXT, run="r", kernels=None, patience=0.6)
        assert refused.value.kind == "busy"
        holder.execute("COMMIT")
    finally:
        await asyncio.sleep(0)
        holder.close()
        control.close()
