# SPDX-License-Identifier: MIT
"""Response durability and budget precedence through archive and actual loop seams."""

from __future__ import annotations

import asyncio
import io
import json
import sqlite3
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from sandbox_science.orchestrator.context.history import Archive, Entry, RunArchive
from sandbox_science.orchestrator.context.history.export import EvidenceExport
from sandbox_science.orchestrator.context.history.usage import USAGE_BYTES, AnsweredUsage
from sandbox_science.orchestrator.context.history.verify_export import verify
from sandbox_science.orchestrator.control import ControlError, ControlStore
from sandbox_science.orchestrator.control.errors import BUSY
from sandbox_science.orchestrator.hooks import TurnHook
from sandbox_science.orchestrator.loop import run_task
from sandbox_science.orchestrator.model import ModelTurn, ScriptedModel
from sandbox_science.orchestrator.projects import ModelUsage, ProjectStore, RunRecord, run_name
from sandbox_science.orchestrator.runstate import RunState
from sandbox_science.orchestrator.tools import ToolOutcome
from sandbox_science.testing import FakeClock, eventually

from .control_versions import older_store
from .test_evidence_export import decode
from .test_tool_journal import CALL, CONTEXT, RUN, logger, toolbox
from .test_tool_journal import projects as projects

START = datetime(2026, 10, 16, 3, tzinfo=UTC)
USAGE = ModelUsage(input_tokens=10, output_tokens=2, cost=0.25, cost_source="model_info")


def append_usage(
    projects: ProjectStore,
    ordinal: int,
    usage: ModelUsage = USAGE,
    *,
    when: datetime = START,
    project: str = "p",
    session: str = "s",
    run: str = RUN,
    parent: str | None = None,
) -> str:
    return (
        Archive(projects.control)
        .append(
            project,
            session,
            run,
            Entry("assistant", "assistant", "Answer"),
            parent=parent,
            model_usage=AnsweredUsage(ordinal=ordinal, observed_at=when, usage=usage),
        )
        .message_id
    )


def final_record(usage: list[ModelUsage], *, started: datetime = START) -> RunRecord:
    return RunRecord(
        run_id="run-1",
        context=CONTEXT,
        task="Compute",
        started_at=started,
        ended_at=started + timedelta(seconds=5),
        stopped="answered",
        messages=[],
        tools=[],
        usage=usage,
    )


@pytest.mark.parametrize("parent", [None, "20261016T020000000000Z-parent"])
@pytest.mark.parametrize("cancel", [False, True])
async def test_usage_is_durable_before_observers_and_effects_without_events_or_final_record(
    projects: ProjectStore, tmp_path: Path, parent: str | None, cancel: bool
) -> None:
    role = "main" if parent is None else "delegated"
    reached: list[bool] = []

    async def never(request):
        pytest.fail("no tool may run after the response observer is interrupted")

    class Interrupted(TurnHook):
        async def model_returned(self, run):
            other = ProjectStore(tmp_path)
            try:
                assert RunState(other.control).model_costs("p") == {role: 0.25}
                assert other.runs("p", "s") == []
                assert RunState(other.control).events("p", "s", RUN) == []
                reached.append(True)
            finally:
                other.close()
            if cancel:
                raise asyncio.CancelledError
            raise RuntimeError("fixture observer failure")

    work = run_task(
        ScriptedModel(
            [
                ModelTurn(
                    None,
                    tool_calls=(CALL,),
                    native=b"exact response",
                    native_format="fixture",
                    input_tokens=10,
                    output_tokens=2,
                    cache_read_input_tokens=4,
                    cache_creation_input_tokens=0,
                    cost=0.25,
                    cost_source="model_info",
                )
            ]
        ),
        toolbox(never),
        "Compute",
        run_id="run-1",
        hooks=(Interrupted(),),
        log=logger(projects, parent=parent),
    )
    if cancel:
        with pytest.raises(asyncio.CancelledError):
            await work
    else:
        assert (await work).stopped == "error"
    assert reached == [True]
    projects.close()
    reopened = ProjectStore(tmp_path)
    try:
        encoded = list(EvidenceExport(reopened.control, "p", "s", RUN).lines())
        assert verify(io.BytesIO(b"".join(encoded))).format == "sandbox-run-evidence-v4"
        _, rows = decode(encoded)
        (kept,) = [row for table, row in rows if table == "archive_model_usage"]
        assert (kept["ordinal"], kept["role"]) == (1, role)
        assert json.loads(kept["usage"])["cost"] == 0.25
        assert json.loads(kept["usage"])["cache_read_input_tokens"] == 4
        assert json.loads(kept["usage"])["cache_creation_input_tokens"] == 0
        answer = Archive(reopened.control).message("p", kept["message_id"])
        assert answer.entry.native == b"exact response"
        assert answer.parent == parent
    finally:
        reopened.close()


async def test_failed_usage_write_rolls_back_response_and_prevents_observers_and_tools(
    projects: ProjectStore,
) -> None:
    with projects.control.write() as tx:
        tx.sql.execute(
            "CREATE TRIGGER fixture_refuse_usage BEFORE INSERT ON archive_model_usage "
            "BEGIN SELECT RAISE(ABORT, 'fixture persistence failure'); END"
        )

    async def never(request):
        pytest.fail("effect ran without committed model usage")

    class Observer(TurnHook):
        async def model_returned(self, run):
            pytest.fail("observer ran without committed model usage")

    result = await run_task(
        ScriptedModel([ModelTurn(None, tool_calls=(CALL,), cost=1)]),
        toolbox(never),
        "Compute",
        run_id="run-1",
        hooks=(Observer(),),
        log=logger(projects),
    )
    assert result.stopped == "error"
    assert [m.entry.kind for m in Archive(projects.control).messages("p", "s")] == ["task"]
    assert RunState(projects.control).model_cost("p") is None


async def test_cancellation_during_archive_write_owns_the_commit_before_cleanup(
    projects: ProjectStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    committed, released = asyncio.Event(), threading.Event()
    loop = asyncio.get_running_loop()
    archive = Archive(projects.control)
    append = archive.append

    def held_write(*args, **kwargs):
        result = append(*args, **kwargs)
        if kwargs.get("model_usage") is not None:
            loop.call_soon_threadsafe(committed.set)
            assert released.wait(10)
        return result

    async def never(request):
        pytest.fail("tool ran after the archive caller was cancelled")

    class Observer(TurnHook):
        async def model_returned(self, run):
            pytest.fail("observer ran after the archive caller was cancelled")

    monkeypatch.setattr(archive, "append", held_write)
    task = asyncio.create_task(
        run_task(
            ScriptedModel([ModelTurn(None, tool_calls=(CALL,), cost=0.25)]),
            toolbox(never),
            "Compute",
            run_id="run-1",
            hooks=(Observer(),),
            log=RunArchive(archive, CONTEXT, run=RUN, parent=None),
        )
    )
    try:
        await committed.wait()
        task.cancel()
        released.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert RunState(projects.control).model_cost("p") == 0.25
        assert [m.entry.kind for m in archive.messages("p", "s")] == ["task", "assistant"]
    finally:
        released.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


def test_usage_size_scope_and_immutability(projects: ProjectStore) -> None:
    message = append_usage(projects, 1)
    with pytest.raises(ControlError, match="already recorded"):
        append_usage(projects, 1)
    with pytest.raises(ControlError, match="16 KiB"):
        append_usage(projects, 2, ModelUsage(model="m" * USAGE_BYTES))
    with pytest.raises(ControlError, match="assistant response"):
        Archive(projects.control).append(
            "p",
            "s",
            RUN,
            Entry("user", "task", "Wrong role"),
            model_usage=AnsweredUsage(ordinal=2, observed_at=START, usage=USAGE),
        )
    assert [m.message_id for m in Archive(projects.control).messages("p", "s")] == [message]
    for statement in (
        "UPDATE archive_model_usage SET usage='{}'",
        "DELETE FROM archive_model_usage",
    ):
        with (
            pytest.raises(sqlite3.IntegrityError, match="written once"),
            projects.control.write() as tx,
        ):
            tx.sql.execute(statement)


@pytest.mark.parametrize("unknown", [False, True])
def test_mixed_journal_and_fallback_ordinals_count_once_through_final_save(
    projects: ProjectStore, unknown: bool
) -> None:
    first = USAGE.model_copy(update={"cost": None, "cost_source": None}) if unknown else USAGE
    second = ModelUsage(input_tokens=20, output_tokens=1, cost=0.5, cost_source="gateway")
    state = RunState(projects.control)
    append_usage(projects, 1, first)
    for ordinal, used in enumerate((first, second), 1):
        state.append(
            "p", "s", RUN, "assistant", {"turn": ordinal, "usage": used.model_dump(mode="json")}
        )
    for saved in (False, True):
        if saved:
            projects.save_run(final_record([first, second]))
        assert state.model_costs("p") == {"main": 0.5 if unknown else 0.75}
        assert state.model_info_cost("p") == (None if unknown else 0.25)
        assert state.unpriced_calls("p", START - timedelta(seconds=1)) == int(unknown)
        state.set_cost_limit("p", 100)
        assert not state.over_cost_limit("p", START + timedelta(hours=1))


def test_budget_deduplication_is_scoped_by_project_and_session(projects: ProjectStore) -> None:
    projects.open_session("p", "other", dedicated=False)
    projects.create("other", "minimal", "sha256:" + "a" * 64)
    projects.open_session("other", "s", dedicated=False)
    append_usage(projects, 1, ModelUsage(cost=2), session="other")
    append_usage(projects, 1, ModelUsage(cost=4), project="other")
    state = RunState(projects.control)
    state.append("p", "s", RUN, "assistant", {"turn": 1, "usage": {"cost": 1}})
    assert state.model_costs("p") == {"main": 3}
    assert state.model_costs("other") == {"main": 4}
    projects.save_run(final_record([ModelUsage(cost=1)]))
    assert state.model_costs("p") == {"main": 3}


def test_daily_window_uses_response_time_for_long_running_attempts(projects: ProjectStore) -> None:
    observed = START + timedelta(days=2)
    append_usage(projects, 1, when=observed)
    state = RunState(projects.control)
    for saved in (False, True):
        if saved:
            projects.save_run(final_record([USAGE]))
        assert state.model_cost("p", observed - timedelta(days=1)) == 0.25
        assert state.model_info_cost("p", observed - timedelta(days=1)) == 0.25
        state.set_cost_limit("p", 0.1)
        assert state.over_cost_limit("p", observed)
        assert not state.over_cost_limit("p", observed + timedelta(days=1, seconds=1))


def test_journal_and_legacy_roles_preserve_fold_costs_through_final_save(
    projects: ProjectStore,
) -> None:
    state = RunState(projects.control)
    child = run_name(START, "child")
    append_usage(projects, 1)
    append_usage(projects, 1, ModelUsage(cost=0.5), run=child, parent=RUN)
    state.append("p", "s", child, "started", {"role": "delegated"})
    for ordinal, cost in ((1, 0.5), (2, 0.75)):
        state.append("p", "s", child, "assistant", {"turn": ordinal, "usage": {"cost": cost}})
    state.append("p", "s", RUN, "fold", {"calls": [{"cost": 2}]})
    for saved in (False, True):
        if saved:
            projects.save_run(
                final_record([USAGE]).model_copy(
                    update={"summaries": {"folds": {"calls": [{"cost": 2}]}}}
                )
            )
        assert state.model_costs("p") == {"main": 2.25, "delegated": 1.25}
        assert state.model_cost("p") == 3.5


@pytest.mark.parametrize("unknown", [False, True])
def test_expired_journal_never_reappears_via_a_newer_legacy_timestamp(
    projects: ProjectStore, unknown: bool
) -> None:
    # A wall-clock correction can put the recorded observation before run start.
    started = START + timedelta(hours=2)
    run = run_name(started, "run-1")
    used = USAGE.model_copy(update={"cost": None, "cost_source": None}) if unknown else USAGE
    append_usage(projects, 1, used, run=run)
    state = RunState(projects.control)
    state.append("p", "s", run, "assistant", {"turn": 1, "usage": used.model_dump(mode="json")})
    for saved in (False, True):
        if saved:
            projects.save_run(final_record([used], started=started))
        since = START + timedelta(hours=1)
        assert state.model_cost("p", since) is None
        assert state.model_info_cost("p", since) is None
        assert state.unpriced_calls("p", since) == 0


async def test_archive_retry_keeps_the_original_observation_time(
    projects: ProjectStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = FakeClock(START)
    archive = Archive(projects.control, clock=clock)
    append = archive.append
    attempted: list[bool] = []

    def busy_once(*args, **kwargs):
        if kwargs.get("model_usage") is not None and not attempted:
            attempted.append(True)
            raise ControlError(BUSY, "fixture writer is busy")
        return append(*args, **kwargs)

    monkeypatch.setattr(archive, "append", busy_once)
    log = RunArchive(archive, CONTEXT, run=RUN, parent=None, clock=clock)
    task = asyncio.create_task(
        run_task(
            ScriptedModel([ModelTurn("Done", cost=0.25)]),
            toolbox(None),
            "Compute",
            run_id="run-1",
            hooks=(),
            log=log,
        )
    )
    try:
        await eventually(lambda: clock.sleepers == 1)
        clock.advance(2 * 24 * 60 * 60)
        assert (await task).stopped == "answered"
        assert RunState(projects.control).model_cost("p") == 0.25
        assert RunState(projects.control).model_cost("p", clock.now() - timedelta(days=1)) is None
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_report_response_gets_the_next_usage_ordinal(projects: ProjectStore) -> None:
    async def arithmetic(request):
        return ToolOutcome("4", {})

    result = await run_task(
        ScriptedModel(
            [ModelTurn(None, tool_calls=(CALL,), cost=0.25), ModelTurn("Report", cost=0.5)]
        ),
        toolbox(arithmetic),
        "Compute",
        run_id="run-1",
        hooks=(),
        log=logger(projects),
        max_turns=1,
    )
    assert result.stopped == "turn_limit"
    _, rows = decode(EvidenceExport(projects.control, "p", "s", RUN).lines())
    assert [r["ordinal"] for table, r in rows if table == "archive_model_usage"] == [1, 2]
    assert RunState(projects.control).model_cost("p") == 0.75


def test_migration_keeps_legacy_usage_without_fabricating_journal_rows(
    projects: ProjectStore, tmp_path: Path
) -> None:
    Archive(projects.control).append("p", "s", RUN, Entry("assistant", "assistant", "Legacy"))
    RunState(projects.control).append(
        "p", "s", RUN, "assistant", {"turn": 1, "usage": {"cost": 0.5}}
    )
    target = tmp_path / "version59.sqlite3"
    older_store(projects.control.path, target, 59)
    store = ControlStore(target)
    try:
        store.initialize()
        manifest, _ = decode(EvidenceExport(store, "p", "s", RUN).lines())
        assert manifest["tables"]["archive_model_usage"]["rows"] == 0
        assert RunState(store).model_cost("p") == 0.5
    finally:
        store.close()
