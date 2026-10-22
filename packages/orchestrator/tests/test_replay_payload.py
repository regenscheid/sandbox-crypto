# SPDX-License-Identifier: MIT
"""Echoed provider requests do not exhaust the live/rebuilt research view."""

import json
from pathlib import Path

import pytest

from sandbox_science.contracts import ExecutionContext
from sandbox_science.orchestrator.context.history import Archive, Entry, RunArchive
from sandbox_science.orchestrator.context.history.collections import Collection
from sandbox_science.orchestrator.context.history.geometry import Geometry
from sandbox_science.orchestrator.context.history.limits import WorkingMemoryLimitError
from sandbox_science.orchestrator.context.history.memory import RunMemory
from sandbox_science.orchestrator.context.history.view import session_view
from sandbox_science.orchestrator.loop import Transcript
from sandbox_science.orchestrator.model import AssistantMessage
from sandbox_science.orchestrator.projects import ProjectStore
from sandbox_science.orchestrator.replay_payload import replay_native


def test_live_and_rebuilt_context_exclude_echoes_but_archive_keeps_exact_originals(tmp_path: Path):
    store = ProjectStore(tmp_path)
    try:
        store.create("p", "minimal", "sha256:" + "a" * 64)
        session = store.open_session("p", "s", dedicated=False)
        archive = Archive(store.control)
        context = ExecutionContext(
            project_id="p", session_id="s", environment_id=session.environment_id
        )
        memory = RunMemory.archived(RunArchive(archive, context, run="run-test", parent=None))
        transcript = Transcript(messages=memory.messages([]), memory=memory)
        assert isinstance(transcript.messages, Collection)
        originals = []
        for index in range(40):
            output = [
                {"type": "reasoning", "encrypted_content": "opaque-" + str(index)},
                {"type": "message", "content": [{"type": "output_text", "text": "result"}]},
            ]
            native = json.dumps(
                {
                    "output": output,
                    "tools": [{"echo": "x" * (9000000 if index == 0 else 256000)}],
                    "usage": {"attribution": "y" * 16000},
                }
            ).encode()
            entry = Entry(
                "assistant",
                "assistant",
                "result",
                native=native,
                native_format="responses",
                alias="model",
                model="resolved",
            )
            row = archive.append("p", "s", "run-test", entry)
            originals.append((row.message_id, native))
            transcript.messages.append_encoded({"archive_message": row.message_id})
        sent = transcript.sent
        assert len(sent) == 40
        native_bytes = 0
        for message in sent:
            assert isinstance(message, AssistantMessage)
            native_bytes += len(message.native or b"")
        assert native_bytes < 20000
        view = session_view(
            archive,
            context,
            geometry=Geometry(ceiling=200000, prompt=20000, chunk=50000, scale=1.0),
        )
        assert len(view.parts) == 40
        rebuilt_bytes = 0
        for part in view.parts:
            assert part.row is not None
            rebuilt_bytes += len(part.row.entry.native or b"")
        assert rebuilt_bytes < 20000
        assert [
            (identifier, archive.message("p", identifier).entry.native)
            for identifier, _ in originals
        ] == originals
    finally:
        store.close()


def test_actual_replay_content_still_hits_working_ceiling(tmp_path: Path):
    memory = RunMemory.scratch(tmp_path / "scratch.sqlite3", ("p", "s", "run"))
    try:
        native = json.dumps(
            {"output": [{"type": "reasoning", "encrypted_content": "x" * 35000000}]}
        ).encode()
        message = AssistantMessage("result", native=native, native_format="responses")
        run = Transcript(messages=[message, message], memory=memory)
        with pytest.raises(WorkingMemoryLimitError, match="active model payload"):
            _ = run.sent
    finally:
        memory.control.close()


@pytest.mark.parametrize(
    ("native", "format_"),
    [(b"not-json", "responses"), (b"[]", "messages"), (b'{"new_field":"needed"}', "future")],
)
def test_unknown_or_malformed_native_is_not_silently_discarded(native, format_):
    assert replay_native(native, format_) == native
