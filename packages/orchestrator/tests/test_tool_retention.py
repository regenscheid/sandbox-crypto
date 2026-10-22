# SPDX-License-Identifier: MIT
"""Large historical records leave RAM only after storage and live observation."""

from __future__ import annotations

import asyncio
import json
import tracemalloc
import weakref
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from sandbox_science.contracts import ExecutionContext
from sandbox_science.orchestrator.context.history import Archive, RunArchive
from sandbox_science.orchestrator.context.history.retention import (
    INLINE_BYTES,
    PREVIEW_BYTES,
    STORAGE,
    StoredToolRecord,
    retain,
)
from sandbox_science.orchestrator.harness import Finished, save
from sandbox_science.orchestrator.hooks import TurnHook
from sandbox_science.orchestrator.loop import run_task
from sandbox_science.orchestrator.model import ModelTurn, ScriptedModel, ToolCall, ToolSpec
from sandbox_science.orchestrator.projects import ProjectStore, run_name
from sandbox_science.orchestrator.tool_status import tool_outcome
from sandbox_science.orchestrator.tools import Toolbox, ToolEntry, ToolOutcome
from sandbox_science.orchestrator.worker_client import WorkerClient

CONTEXT = ExecutionContext(project_id="p", session_id="s", environment_id="env-p")
START = datetime(2026, 10, 16, tzinfo=UTC)
RUN = run_name(START, "run-1")
CALL = ToolCall("call-1", "arithmetic", '{ "n": 2 }')


class Payload(dict):
    """A weak-referenceable result; fixtures keep no strong reference outside the loop."""


def projects_at(path: Path) -> ProjectStore:
    store = ProjectStore(path)
    store.create("p", "minimal", "sha256:" + "a" * 64)
    store.open_session("p", "s", dedicated=False)
    return store


def tools_for(handler) -> Toolbox:
    box = Toolbox(WorkerClient("http://unused.invalid", "test-secret-unused"), CONTEXT)
    box.entries = {
        "arithmetic": ToolEntry(
            ToolSpec("arithmetic", "Fixture", {"type": "object"}), "read", handler
        )
    }
    return box


@pytest.mark.parametrize("ending", ["answer", "observer_error", "callback_error", "cancel"])
async def test_observers_see_original_then_final_save_keeps_preview_and_exact_reference(
    tmp_path: Path, ending: str
) -> None:
    store = projects_at(tmp_path)
    references = []
    seen = []
    stop = asyncio.Event()

    async def arithmetic(request):
        payload = Payload(text="\u00e9\U0001f680\x00" * 200000)
        references.append(weakref.ref(payload))
        return ToolOutcome(
            "4 (sample)",
            {"payload": payload, "value": 4, "state": "succeeded", "metadata": {"exact": "0004"}},
        )

    class Observer(TurnHook):
        async def observe_tool(self, run, record):
            assert run.tool_records[-1]["payload"] is record["payload"]
            assert len(record["payload"]["text"]) == 600000
            record["metadata"]["exact"] = "observer changed this"
            seen.append("observer")
            if ending == "observer_error":
                raise ValueError("synthetic observer failure")
            if ending == "cancel":
                stop.set()
                raise asyncio.CancelledError()

    async def callback(name, arguments, outcome):
        assert (name, arguments) == (CALL.name, CALL.arguments)
        assert len(outcome.record["payload"]["text"]) == 600000
        seen.append("callback")
        if ending == "callback_error":
            raise ValueError("synthetic callback failure")

    class Model(ScriptedModel):
        async def complete(self, messages, tools):
            if references:
                assert references[0]() is None  # released before the next model call
            return await super().complete(messages, tools)

    try:
        transcript = await run_task(
            Model([ModelTurn(None, tool_calls=(CALL,)), ModelTurn("Done")]),
            tools_for(arithmetic),
            "Compute",
            run_id="run-1",
            hooks=(Observer(),),
            log=RunArchive(Archive(store.control), CONTEXT, run=RUN, parent=None),
            on_tool=callback,
            stop=stop,
        )
        assert seen == (
            ["observer"] if ending in {"observer_error", "cancel"} else ["observer", "callback"]
        )
        (preview,) = transcript.tool_records
        assert "payload" not in preview
        assert preview["metadata"] == {"exact": "0004"}
        assert len(json.dumps(preview, ensure_ascii=False).encode()) <= PREVIEW_BYTES
        reference = StoredToolRecord.model_validate(preview[STORAGE])
        assert reference.project == "p"
        assert reference.omitted_fields == 1
        assert tool_outcome(preview) == {"state": "succeeded", "error": None, "exit_code": None}
        assert transcript.stopped == (
            "answered" if ending == "answer" else "cancelled" if ending == "cancel" else "error"
        )
        finished = Finished("run-1", CONTEXT, "Compute", START, transcript)
        errors = []
        export = tmp_path / "run.json"
        assert (
            await save(
                finished.fields(), 0, (), projects=store, error=errors.append, transcript=export
            )
            == 0
        )
        assert errors == []
        assert export.stat().st_size < 128 * 1024
        assert "payload" not in json.loads(export.read_text())["tools"][0]
    finally:
        store.close()
    reopened = ProjectStore(tmp_path)
    try:
        full = Archive(reopened.control).tool_record("p", reference.message, max_bytes=4 * 1024**2)
        assert full["payload"]["text"] == "\u00e9\U0001f680\x00" * 200000
        assert full["arguments"] == CALL.arguments
        assert reopened.run("p", "s", RUN).tools[0] == preview
    finally:
        reopened.close()


async def test_without_a_journal_the_only_full_record_stays_inline() -> None:
    async def arithmetic(request):
        return ToolOutcome("sample", {"payload": "x" * (INLINE_BYTES + 1)})

    transcript = await run_task(
        ScriptedModel([ModelTurn(None, tool_calls=(CALL,)), ModelTurn("Done")]),
        tools_for(arithmetic),
        "Compute",
        run_id="inline",
        hooks=(),
    )
    assert transcript.tool_records[0]["payload"] == "x" * (INLINE_BYTES + 1)
    assert STORAGE not in transcript.tool_records[0]


async def test_report_round_refusal_keeps_full_arguments_in_the_journal(tmp_path: Path) -> None:
    store = projects_at(tmp_path)
    oversized = ToolCall("report", "arithmetic", "x" * (INLINE_BYTES + 1))

    async def arithmetic(request):
        return ToolOutcome("4", {"value": 4})

    try:
        transcript = await run_task(
            ScriptedModel(
                [
                    ModelTurn(None, tool_calls=(CALL,)),
                    ModelTurn("Partial", tool_calls=(oversized,)),
                ]
            ),
            tools_for(arithmetic),
            "Compute",
            run_id="run-1",
            hooks=(),
            max_turns=1,
            log=RunArchive(Archive(store.control), CONTEXT, run=RUN, parent=None),
        )
        assert transcript.stopped == "turn_limit"
        preview = transcript.tool_records[-1]
        assert preview["not_run"] == "turn_limit"
        reference = StoredToolRecord.model_validate(preview[STORAGE])
        assert reference.shortened == ("arguments",)
        full = Archive(store.control).tool_record("p", reference.message)
        assert full["arguments"] == oversized.arguments
        assert full["not_run"] == "turn_limit"
    finally:
        store.close()


def test_small_records_and_complete_preview_fields_keep_their_values() -> None:
    small = {"tool": "arithmetic", "arguments": "{}", "value": "00001234", "nested": ["\u00e9", 4]}
    assert retain(small, project="p", message="msg-" + "a" * 16) == small
    large = {
        **small,
        "huge": "x" * INLINE_BYTES,
        "execution": {"state": "failed", "result": {"exit_code": 1, "stdout": "x" * INLINE_BYTES}},
    }
    preview = retain(large, project="p", message="msg-" + "a" * 16)
    assert {key: preview[key] for key in small} == small
    assert preview[STORAGE]["omitted_fields"] == 2
    assert tool_outcome(preview) == {"state": "failed", "error": None, "exit_code": 1}


def test_small_retained_records_are_detached_from_observer_mutation() -> None:
    record = {"tool": "arithmetic", "arguments": "{}", "nested": {"value": "0004"}}
    retained = retain(record, project="p", message="msg-" + "a" * 16)
    record["nested"]["value"] = "changed"
    assert retained["nested"] == {"value": "0004"}


def test_counting_omissions_does_not_copy_a_wide_record() -> None:
    record = {
        "tool": "arithmetic",
        "arguments": "{}",
        "huge": "x" * INLINE_BYTES,
        **{str(i): "value" for i in range(200000)},
    }
    tracemalloc.start()
    try:
        preview = retain(record, project="p", message="msg-" + "a" * 16)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert preview[STORAGE]["omitted_fields"] > 199900
    assert peak < 1024 * 1024


@pytest.mark.parametrize("shape", ["strings", "fields", "nested", "escaped"])
def test_preview_budget_covers_wide_deep_and_expanding_json(shape: str) -> None:
    payload: dict[str, Any] = {"tool": "arithmetic", "arguments": "\u00e9\x00" * INLINE_BYTES}
    if shape == "strings":
        payload["huge"] = "\U0001f680" * INLINE_BYTES
    elif shape == "fields":
        payload.update({str(i): {"x": "y" * 2000} for i in range(1000)})
    elif shape == "nested":
        nested: dict[str, Any] = {}
        for _ in range(40):
            nested = {"child": nested}
        payload["deep"] = nested
    else:
        payload.update({str(i): "\x00" * 500 for i in range(40)})
        payload["state"] = "\x00" * 1000
        payload["error"] = "\x00" * 1000
    preview = retain(payload, project="p", message="msg-" + "a" * 16)
    assert len(json.dumps(preview, ensure_ascii=False).encode()) <= PREVIEW_BYTES
    assert preview[STORAGE]["shortened"] == ["arguments"]
    assert preview[STORAGE]["omitted_fields"] > 0
