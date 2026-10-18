# SPDX-License-Identifier: MIT
"""Historical collections remain paged; response receipts precede observers."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from sandbox_science.orchestrator.harness import Finished, save
from sandbox_science.orchestrator.history.collections import Collection, Members, Slice
from sandbox_science.orchestrator.history.receipts import Receipts
from sandbox_science.orchestrator.hooks import Notice, TurnHook
from sandbox_science.orchestrator.loop import run_task
from sandbox_science.orchestrator.model import ModelTurn, ScriptedModel
from sandbox_science.orchestrator.projects import ProjectStore

from .test_tool_journal import CONTEXT, RUN, logger, toolbox
from .test_tool_journal import projects as projects


def test_collection_slices_and_members_are_scoped_and_reopen(projects: ProjectStore) -> None:
    values = Collection[dict[str, int]](projects.control, ("p", "s", RUN), "fixture")
    for i in range(500):
        values.append({"i": i})
    assert isinstance(values[100:400], Slice)
    assert list(values[100:103]) == [{"i": 100}, {"i": 101}, {"i": 102}]
    assert values[::-1][0] == {"i": 499}
    with pytest.raises(TypeError, match="immutable"):
        values[2] = {"i": 9}
    assert values[2] == {"i": 2}
    other = Collection[dict[str, int]](projects.control, ("p", "s", RUN + "x"), "fixture")
    assert not other
    first = Members(projects.control, ("p", "s", RUN), "a")
    second = Members(projects.control, ("p", "s", RUN), "b")
    first.update(str(i) for i in range(500))
    second.update(first)
    assert len(second) == 500
    assert "42" in second
    assert "42" not in Members(projects.control, ("p", "s", RUN + "x"), "b")
    projects.control.close()
    assert values[499] == {"i": 499}
    assert len(second) == 500


@pytest.mark.parametrize(
    "statement", ["UPDATE run_collections SET item='null'", "DELETE FROM run_collections"]
)
def test_collection_sql_mutation_is_refused(projects: ProjectStore, statement: str) -> None:
    values = Collection(projects.control, ("p", "s", RUN), "immutable")
    values.append({"exact": "original"})
    with (
        pytest.raises(sqlite3.IntegrityError, match="immutable"),
        projects.control.write() as tx,
    ):
        tx.sql.execute(statement)
    assert values[0] == {"exact": "original"}


def test_schema63_preserves_existing_collection_rows(
    projects: ProjectStore, tmp_path: Path
) -> None:
    from sandbox_science.orchestrator.control import ControlStore

    from .control_versions import older_store

    Collection(projects.control, ("p", "s", RUN), "fixture").append({"original": "\x00😀"})
    target = tmp_path / "version62.sqlite3"
    older_store(projects.control.path, target, 62)
    migrated = ControlStore(target)
    try:
        assert Collection(migrated, ("p", "s", RUN), "fixture")[0] == {"original": "\x00😀"}
        with pytest.raises(sqlite3.IntegrityError, match="immutable"), migrated.write() as tx:
            tx.sql.execute("DELETE FROM run_collections")
    finally:
        migrated.close()


async def test_response_receipts_survive_observer_failure_and_are_scoped(
    projects: ProjectStore,
) -> None:
    receipts = Receipts(projects.control, "p", "s", RUN)

    class Observer(TurnHook):
        name = "fixture"

        async def notices(self, point, run):
            return (
                (Notice("fixture", ("notice-1",), "Input", {"value": 1}),)
                if point == "start"
                else ()
            )

        async def model_returned(self, run):
            assert receipts.contains("fixture", "notice-1")
            raise RuntimeError("fixture observer failure")

    result = await run_task(
        ScriptedModel([ModelTurn("Done")]),
        toolbox(None),
        "Compute",
        run_id="run-1",
        hooks=(Observer(),),
        log=logger(projects),
    )
    assert result.stopped == "error"
    assert receipts.page("fixture") == ("notice-1",)
    assert Receipts(projects.control, "p", "s", RUN + "x").page("fixture") == ()
    assert isinstance(result.messages, Collection)
    assert result.messages[-1]["content"] == "Done"


async def test_compact_final_record_retains_history_reference(
    projects: ProjectStore,
    tmp_path: Path,
) -> None:
    errors = []
    result = await run_task(
        ScriptedModel([ModelTurn("x" * 150000)]),
        toolbox(None),
        "Compute",
        run_id="run-1",
        hooks=(),
        log=logger(projects),
    )
    from .test_model_usage_journal import START

    finished = Finished("run-1", CONTEXT, "Compute", START, result)
    fields = finished.fields()
    assert fields["messages"] == []
    reference = fields["stored_history"]
    assert isinstance(reference, dict)
    assert reference["run"] == RUN
    assert await save(fields, finished.status, (), projects=projects, error=errors.append) == 0
    assert not errors
    kept = projects.run("p", "s", RUN)
    assert kept.stored_history is not None
    assert kept.stored_history["run"] == RUN
    reopened = Collection(projects.control, ("p", "s", RUN), "messages")
    assert reopened[-1]["archive_message"].startswith("msg-")


def test_scratch_history_preserves_native_messages_and_adapter_state(tmp_path: Path) -> None:
    from sandbox_science.orchestrator.history.memory import RunMemory
    from sandbox_science.orchestrator.model import AssistantMessage

    memory = RunMemory.scratch(tmp_path / "scratch.sqlite3", ("p", "s", "r"))
    original = AssistantMessage(
        "Answer",
        native=b"\x00\xffexact",
        native_format="fixture",
        alias="a",
        model="m",
        gateway_version="v",
        prefix="prefix",
        adapter_state={"generation": 3},
    )
    try:
        messages = memory.messages([original])
        messages.append(original)
        for index in (0, 1):
            restored = messages[index]
            assert isinstance(restored, AssistantMessage)
            assert restored.native == original.native
            assert restored.native_format == original.native_format
            assert restored.alias == original.alias
            assert restored.model == original.model
            assert restored.gateway_version == original.gateway_version
            assert restored.prefix == original.prefix
            assert restored.adapter_state == original.adapter_state
    finally:
        memory.control.close()


async def test_tool_collection_persists_only_preview_during_live_observers(
    projects: ProjectStore,
) -> None:
    from sandbox_science.orchestrator.tools import ToolOutcome

    from .test_tool_journal import CALL

    original = {"text": "x" * 2_000_000}

    async def arithmetic(request):
        return ToolOutcome("4 (sample)", {"payload": original, "state": "succeeded"})

    class Observer(TurnHook):
        async def observe_tool(self, run, record):
            assert run.tool_records[-1]["payload"] is original
            persisted = Collection(projects.control, ("p", "s", RUN), "tools")[-1]
            assert "payload" not in persisted
            assert persisted["_record_storage"]["format"] == "tool-record-v1"

    result = await run_task(
        ScriptedModel([ModelTurn(None, tool_calls=(CALL,)), ModelTurn("Done")]),
        toolbox(arithmetic),
        "Compute",
        run_id="run-1",
        hooks=(Observer(),),
        log=logger(projects),
    )
    assert result.stopped == "answered"
    assert "payload" not in result.tool_records[-1]


@pytest.mark.parametrize("field", ["adapter_state", "tool_calls"])
def test_archive_page_counts_all_retained_payload_fields(
    projects: ProjectStore, field: str
) -> None:
    from sandbox_science.orchestrator.history import Archive, Entry
    from sandbox_science.orchestrator.history.limits import WorkingMemoryLimitError

    archive = Archive(projects.control)
    payload = "x" * (60 * 1024 if field == "adapter_state" else 3 * 1024 * 1024)
    for index in range(150 if field == "adapter_state" else 3):
        entry = (
            Entry("assistant", "assistant", "tiny", adapter_state={"payload": payload})
            if field == "adapter_state"
            else Entry(
                "assistant",
                "assistant",
                "tiny",
                tool_calls=({"id": str(index), "function": {"arguments": payload}},),
            )
        )
        row = archive.append("p", "s", RUN, entry)
        assert archive.payload_bytes("p", row.message_id) > len(payload)
    with pytest.raises(WorkingMemoryLimitError, match="working read limit"):
        archive.messages("p", "s", run=RUN, limit=200)


def test_native_payloads_count_toward_active_view_ceiling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sandbox_science.orchestrator import loop
    from sandbox_science.orchestrator.history.limits import WorkingMemoryLimitError
    from sandbox_science.orchestrator.history.memory import RunMemory
    from sandbox_science.orchestrator.loop import Transcript
    from sandbox_science.orchestrator.model import AssistantMessage

    # Exercise the accounting without writing over 64 MiB of synthetic evidence.
    monkeypatch.setattr(loop, "VIEW_BYTES", 9 * 1024 * 1024)
    memory = RunMemory.scratch(tmp_path / "native.sqlite3", ("p", "s", "r"))
    try:
        messages = memory.messages([{"role": "system", "content": "Research"}])
        for _ in range(2):
            messages.append(AssistantMessage("tiny", native=b"n" * (5 * 1024 * 1024)))
        transcript = Transcript(messages=messages, memory=memory)
        with pytest.raises(WorkingMemoryLimitError, match="active model payload"):
            _ = transcript.sent
        assert len(messages) == 3
    finally:
        memory.control.close()
