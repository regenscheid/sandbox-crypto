# SPDX-License-Identifier: MIT
"""The run service's records and a run's part in its session's chat (run-service spec).

The store is tested through ``RunState``; runs go through ``cli.main`` with the scripted
gateway and an in-process worker (``loop_harness``), and are read back through the
record, the events and the messages.
"""

from __future__ import annotations

import asyncio
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest

from sandbox_science.contracts import ExecutionContext
from sandbox_science.contracts.worker import TERMINAL_STATES
from sandbox_science.orchestrator import cli
from sandbox_science.orchestrator.chat import RunChat, session_lease
from sandbox_science.orchestrator.context.history import Archive, body
from sandbox_science.orchestrator.control import ControlError
from sandbox_science.orchestrator.jobstate import JobState
from sandbox_science.orchestrator.notices import REDELIVERED
from sandbox_science.orchestrator.projects import ProjectStore
from sandbox_science.orchestrator.runstate import RunState
from sandbox_science.orchestrator.worker_client import WorkerClient
from sandbox_science.testing import FakeClock, eventually

from .loop_harness import (
    API_KEY,
    PROJECT,
    SESSION,
    TASK,
    TOKEN,
    Harness,
    Reply,
    call,
    start_job,
    user_text,
)
from .loop_harness import gateway as gateway
from .loop_harness import harness as harness
from .loop_harness import worker as worker
from .scripted import PURPOSE


def store(tmp_path: Path, clock: FakeClock | None = None) -> tuple[ProjectStore, RunState]:
    projects = ProjectStore(tmp_path)
    projects.create("p", "minimal", "sha256:" + "a" * 64)
    projects.open_session("p", "s", dedicated=False)
    if clock is None:
        return projects, RunState(projects.control)
    return projects, RunState(projects.control, clock=clock)


def runs(harness: Harness) -> RunState:
    return RunState(ProjectStore(harness.state).control)


def kinds(state: RunState, run: str) -> list[str]:
    return [event.kind for event in state.events(PROJECT, SESSION, run)]


# --- The lease -------------------------------------------------------------------------


async def test_the_lease_is_held_while_the_context_is(tmp_path: Path) -> None:
    projects, state = store(tmp_path)
    context = ExecutionContext(project_id="p", session_id="s", environment_id="env")
    async with session_lease(state, context, "run-a"):
        with pytest.raises(ControlError):
            async with session_lease(state, context, "run-b"):
                pass
    assert state.lease("p", "s") is None
    projects.close()


# --- Runs --------------------------------------------------------------------------------


async def test_a_run_writes_its_events_as_it_goes(harness: Harness) -> None:
    seen: list[list[str]] = []

    async def look() -> None:
        state = runs(harness)
        lease = state.lease(PROJECT, SESSION)
        assert lease is not None
        seen.append(kinds(state, lease.run))

    record = await harness.run(
        "run-1", [Reply(calls=[call("plan_read")], before=look), Reply("Done.", before=look)]
    )
    state = runs(harness)
    (entry,) = ProjectStore(harness.state).runs(PROJECT, SESSION)
    events = state.events(PROJECT, SESSION, entry.name)
    # The tool call's result was an event before the second model call returned.
    assert seen == [
        ["started", "user", "model", "notice"],
        ["started", "user", "model", "notice", "assistant", "tool", "model", "notice"],
    ]
    assert [e.kind for e in events] == [
        "started",
        "user",
        "model",
        "notice",
        "assistant",
        "tool",
        "model",
        "notice",
        "assistant",
        "stopped",
    ]
    started, user, _, _, asked, tool, _, _, answered, stopped = (e.data for e in events)
    assert started["run_id"] == "run-1"
    # The root of its session's tree of runs (PD-32): no parent, no delegation.
    assert (started["role"], started["parent"], started["delegation"]) == ("main", None, None)
    assert (record.role, record.parent, record.delegation) == ("main", None, None)
    assert (entry.role, entry.parent) == ("main", None)
    assert started["prompt_provenance"] is not None
    assert user == {"message_id": None, "text": TASK}
    assert cast(list[dict[str, str]], asked["tool_calls"])[0]["name"] == "plan_read"
    assert asked["usage"] == record.usage[0].model_dump(mode="json")
    assert (tool["name"], tool["turn"]) == ("plan_read", 1)
    assert answered["text"] == "Done."
    assert stopped == {
        "stopped": "answered",
        "failure": None,
        "final_text": "Done.",
        "record": entry.name,
    }
    assert state.lease(PROJECT, SESSION) is None


async def test_a_message_during_a_run_reaches_the_next_model_call(harness: Harness) -> None:
    async def write() -> None:
        runs(harness).post(PROJECT, SESSION, "m-1", "Also check $p = 7$.", author="user")

    record = await harness.run(
        "run-1", [Reply(calls=[call("plan_read")], before=write), Reply("Both done.")]
    )
    _, second = harness.gateway.main
    # The user's own words, after the tool result, before the second call.
    assert user_text(second[-1]) == "Also check $p = 7$."
    assert record.delivered["messages"] == ["m-1"]
    state = runs(harness)
    (message,) = state.messages(PROJECT, SESSION)
    (entry,) = ProjectStore(harness.state).runs(PROJECT, SESSION)
    assert (message.state, message.run) == ("delivered", entry.name)
    users = [e.data for e in state.events(PROJECT, SESSION, entry.name) if e.kind == "user"]
    assert users[-1] == {"message_id": "m-1", "text": "Also check $p = 7$."}


async def test_a_message_at_the_last_answer_continues_the_run(harness: Harness) -> None:
    async def write() -> None:
        runs(harness).post(PROJECT, SESSION, "m-1", "One more thing.", author="user")

    record = await harness.run(
        "run-1", [Reply("First answer.", before=write), Reply("The one more thing.")]
    )
    assert record.final_text == "The one more thing."
    assert user_text(harness.gateway.main[1][-1]) == "One more thing."
    assert record.delivered["messages"] == ["m-1"]


async def test_a_message_a_failed_call_saw_stays_queued(harness: Harness) -> None:
    async def write() -> None:
        runs(harness).post(PROJECT, SESSION, "m-1", "Did it work?", author="user")

    record = await harness.run(
        "run-1", [Reply(calls=[call("plan_read")], before=write), Reply(status=500)], status=1
    )
    assert record.failure == "model"
    assert "messages" not in record.delivered
    (message,) = runs(harness).messages(PROJECT, SESSION)
    assert message.state == "queued"
    again = await harness.run("run-2", [Reply("It did.")])
    # The failed run's view holds the message where it first arrived; the next run is
    # told to answer it there rather than given it twice (CM-29a).
    first = harness.gateway.main[0]
    (shown,) = [m for m in first if m["role"] == "user" and "Did it work?" in str(m["content"])]
    assert user_text(shown) == "Did it work?"
    archive_id = str(shown["content"])[1:21]
    assert body(str(first[-1]["content"])) == f'{REDELIVERED.text}"{archive_id}"'
    assert again.delivered["messages"] == ["m-1"]


async def running_job(harness: Harness) -> str:
    async with WorkerClient(harness.worker.url, TOKEN) as client:
        listing = await client.jobs(harness.context())
    (job,) = [j.execution_id for j in listing.jobs if j.state not in TERMINAL_STATES]
    return job


async def test_a_message_ends_a_job_wait(harness: Harness) -> None:
    waiting = Reply()
    jobs: list[str] = []

    async def wait_on_the_job() -> None:
        jobs.append(await running_job(harness))
        waiting.calls = [call("wait_jobs", job_ids=jobs)]

        async def write_once_waiting() -> None:
            await eventually(lambda: bool(JobState(harness.state).waits(harness.context())))
            runs(harness).post(PROJECT, SESSION, "m-1", "How far is it?", author="user")

        asyncio.get_running_loop().create_task(write_once_waiting())

    waiting.before = wait_on_the_job
    record = await harness.run(
        "run-1", [Reply(calls=[start_job()]), waiting, Reply("It is still running.")]
    )
    _, _, third = harness.gateway.main
    assert '"reason":"user_message"' in third[-2]["content"]
    assert user_text(third[-1]) == "How far is it?"
    assert record.delivered["messages"] == ["m-1"]
    state = runs(harness)
    (entry,) = ProjectStore(harness.state).runs(PROJECT, SESSION)
    waits = [e.data for e in state.events(PROJECT, SESSION, entry.name) if e.kind == "waiting"]
    assert waits == [{"job_ids": jobs, "mode": "all"}, {"job_ids": None, "mode": None}]
    await harness.end_job()


async def test_a_session_whose_run_is_in_progress_refuses_another(
    harness: Harness, capsys: pytest.CaptureFixture[str]
) -> None:
    state = runs(harness)
    state.acquire(PROJECT, SESSION, "another-run", "another-holder")
    token, key = harness.tmp / "worker.token", harness.tmp / "gateway.key"
    token.write_text(TOKEN)
    key.write_text(API_KEY)
    argv = [
        "run", "--worker", harness.worker.url, "--token-file", str(token),
        "--project", PROJECT, "--session", SESSION, "--state-dir", str(harness.state),
        "--allow-unisolated", "--model-url", harness.gateway.url, "--api-key-file", str(key),
        "--model", "main", "--run-id", "run-1", TASK,
    ]  # fmt: skip
    assert await asyncio.to_thread(cli.main, argv) == 1
    assert "has a run in progress: another-run" in capsys.readouterr().err
    assert ProjectStore(harness.state).runs(PROJECT, SESSION) == []
    assert harness.gateway.main == []
    state.release(PROJECT, SESSION, "another-holder")


async def test_an_event_that_cannot_be_written_does_not_stop_the_run(
    harness: Harness, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def refuse(*args: object, **kwargs: object) -> int:
        raise ControlError("busy", "the store is held")

    monkeypatch.setattr(RunState, "append", refuse)
    record = await harness.run("run-1", [Reply("Still answered.")])
    assert (record.stopped, record.final_text) == ("answered", "Still answered.")
    assert "the started event of run" in caplog.text


CONTEXT = ExecutionContext(project_id="p", session_id="s", environment_id="env")


async def test_a_lease_renewed_on_the_injected_clock_outlives_its_first_minute(
    tmp_path: Path,
) -> None:
    clock = FakeClock(datetime.now(UTC))
    projects, state = store(tmp_path, clock)
    async with session_lease(state, CONTEXT, "run-a", clock=clock):
        for _ in range(12):  # three minutes, in steps under the renewal interval
            await eventually(lambda: clock.sleepers > 0)
            clock.advance(14)
            await asyncio.sleep(0.05)
        assert state.lease("p", "s") is not None
    assert state.lease("p", "s") is None
    projects.close()


@pytest.mark.parametrize("kind", ["conflict", "busy"])
async def test_a_lost_lease_stops_the_run_that_held_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    """RS-5: another holder took the session (conflict), or no renewal succeeded for
    half the lease (busy): the holder stops, and its release leaves the session free."""
    projects = ProjectStore(tmp_path)
    projects.create("p", "minimal", "sha256:" + "a" * 64)
    projects.open_session("p", "s", dedicated=False)
    state = RunState(projects.control, lease_seconds=0.4)

    def lost(*args: object) -> None:
        raise ControlError(kind, "the lease is gone")

    async def hold() -> None:
        async with session_lease(state, CONTEXT, "run-a"):
            monkeypatch.setattr(RunState, "renew", lost)
            await asyncio.sleep(30)

    with pytest.raises(ControlError) as stopped:
        await asyncio.wait_for(hold(), 5)
    assert stopped.value.kind == "conflict"
    assert "lost the lease" in stopped.value.message
    assert state.lease("p", "s") is None
    projects.close()


async def test_every_page_of_queued_messages_reaches_the_run(tmp_path: Path) -> None:
    projects, state = store(tmp_path)
    for number in range(201):
        state.post("p", "s", f"m-{number}", f"text {number}", author="user")
    chat = RunChat(state, CONTEXT, run="r", run_id="r", task=None)
    first = await chat.pending()
    assert len(first) == 201
    chat.offered.update(m.message_id for m in first[:200])
    assert [m.message_id for m in await chat.pending()] == ["m-200"]
    projects.close()


async def test_an_oversized_event_keeps_head_tail_and_usage(tmp_path: Path) -> None:
    projects, state = store(tmp_path)
    state.acquire("p", "s", "r", "holder")
    chat = RunChat(state, CONTEXT, run="r", run_id="r", task=None)
    await chat.write("tool", {"result": "a" + "x" * 100_000 + "z"})
    calls = [{"id": f"c{n}", "name": "shell", "arguments": "y" * 40} for n in range(3000)]
    usage = {"model": "m", "input_tokens": 1, "output_tokens": 2}
    await chat.write("assistant", {"turn": 1, "text": "t", "tool_calls": calls, "usage": usage})
    tool, assistant = state.events("p", "s", "r")
    result = cast(str, tool.data["result"])
    assert result.startswith("a")
    assert result.endswith("z")
    assert "characters omitted" in result
    assert assistant.data["usage"] == usage
    assert assistant.data["tool_calls"] == [{"omitted": 3000}]
    projects.close()


async def test_a_long_answer_has_bounded_previews_and_exact_journal_evidence(
    harness: Harness,
) -> None:
    answer = "BEGIN " + "y" * 40_000 + " END"
    record = await harness.run("run-1", [Reply(answer)])
    state = runs(harness)
    with closing(ProjectStore(harness.state)) as projects:
        (entry,) = projects.runs(PROJECT, SESSION)
        (original,) = Archive(projects.control).messages(
            PROJECT, SESSION, run=entry.name, kinds=("assistant",)
        )
        assert original.entry.content == answer
    (reply,) = [e for e in state.events(PROJECT, SESSION, entry.name) if e.kind == "assistant"]
    text = cast(str, reply.data["text"])
    assert record.final_text is not None
    assert record.final_text.startswith("BEGIN ")
    assert record.final_text.endswith(" END")
    assert "Preview; exact text is in stored evidence." in record.final_text
    assert len(record.final_text) < 17_000
    assert "characters omitted" in text
    assert text.startswith("BEGIN ")
    assert text.endswith(" END")
    assert len(text) < 17_000


# --- The result's gloss (B10, item 1) --------------------------------------------------


async def test_a_tool_event_glosses_its_result_and_carries_its_purpose(harness: Harness) -> None:
    calls = [
        call("shell", script="printf 'one\\ntwo\\n'", purpose_note="Counting the two lines"),
        call("shell", script="echo 'No such file' >&2; exit 1"),
        call("write_file", path="table.txt", content="1 1\n"),
        call("plan_read", purpose_note=None),
    ]
    await harness.run("run-1", [Reply(calls=calls), Reply("Done.")])
    state = runs(harness)
    (entry,) = ProjectStore(harness.state).runs(PROJECT, SESSION)
    tools = [e.data for e in state.events(PROJECT, SESSION, entry.name) if e.kind == "tool"]
    counted, failed, written, read = tools
    assert counted["purpose"] == "Counting the two lines"
    assert counted["gloss"] == {
        "outcome": "ok",
        "exit_code": 0,
        "lines": 2,
        "bytes": 8,
        "files": [],
        "more_files": 0,
        "error": None,
    }
    assert failed["purpose"] == PURPOSE
    assert failed["gloss"] == {
        "outcome": "failed",
        "exit_code": 1,
        "lines": 1,
        "bytes": 13,
        "files": [],
        "more_files": 0,
        "error": "No such file",
    }
    gloss = cast(dict[str, object], written["gloss"])
    assert (gloss["outcome"], gloss["files"], gloss["more_files"]) == ("ok", ["table.txt"], 0)
    # A call without a valid purpose has none to show; its row shows the tool instead.
    assert "purpose" not in read
    assert cast(dict[str, object], read["gloss"])["outcome"] == "refused"
