# SPDX-License-Identifier: MIT
"""An answer's adapter state: what the adapter that made it keeps of its call, carried by
the answer and archived with it, so a view rebuilt from the archive has it again
(docs/specs/context-management.md, CM-38)."""

from __future__ import annotations

import asyncio
import copy
import json
import logging
from pathlib import Path

import pytest

from sandbox_science.contracts import ExecutionContext
from sandbox_science.orchestrator.control import ControlStore
from sandbox_science.orchestrator.history import Archive, RunArchive
from sandbox_science.orchestrator.history.log import STATE_BYTES
from sandbox_science.orchestrator.hooks import Appended
from sandbox_science.orchestrator.model import AssistantMessage, ModelTurn
from sandbox_science.orchestrator.projects import ProjectStore

from .control_versions import older_store
from .loop_harness import PROJECT, SESSION
from .test_history_archive import state as state

STATE = {"tools": ["shell", "e-print-mcp_get_eprint"], "replay_off": "version changed"}
TURN = ModelTurn(
    text="Looking.", model="claude-x", native=b"{}", native_format="messages",
    alias="claude", adapter_state=STATE,
)  # fmt: skip


def archived(state: Path, turn: ModelTurn) -> AssistantMessage:
    """``turn``'s answer, archived by a run's message log and given back by the archive."""
    projects = ProjectStore(state)
    try:
        archive = Archive(projects.control)
        context = ExecutionContext(project_id=PROJECT, session_id=SESSION, environment_id="e")
        log = RunArchive(archive, context, run="run-1", parent=None)
        answer = AssistantMessage.of(turn)
        asyncio.run(log.append(answer, Appended("assistant", reply=turn)))
        found = archive.messages(PROJECT, SESSION)[-1].chat()
    finally:
        projects.close()
    assert isinstance(found, AssistantMessage)
    return found


def test_an_answer_carries_its_adapters_state_and_the_archive_gives_it_back(state: Path) -> None:
    answer = AssistantMessage.of(TURN)
    assert answer.adapter_state == STATE
    assert copy.deepcopy(answer).adapter_state == STATE
    # Another adapter, or a record, sees the message's keys only.
    assert json.loads(json.dumps(answer)) == {"role": "assistant", "content": "Looking."}
    rebuilt = archived(state, TURN)
    assert (rebuilt.adapter_state, rebuilt.native_format) == (STATE, "messages")
    assert archived(state, ModelTurn(text="Done.")).adapter_state is None


def test_a_state_too_large_or_not_json_is_left_out(
    state: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The archive keeps at most 64 KiB of JSON; the answer is archived without more, and
    its adapter does without it, as for an answer archived before states were kept."""
    with caplog.at_level(logging.WARNING):
        large = archived(state, ModelTurn(text="a", adapter_state={"x": "y" * STATE_BYTES}))
        odd = archived(state, ModelTurn(text="b", adapter_state={"x": {1, 2}}))
    assert large.adapter_state is None
    assert odd.adapter_state is None
    assert "over 65536 bytes" in caplog.text
    assert "not JSON" in caplog.text


def test_answers_archived_before_states_were_kept_have_none(state: Path, tmp_path: Path) -> None:
    """Version 49: a store from before it opens, its answers without a state."""
    archived(state, TURN)
    target = tmp_path / "older" / "control.sqlite3"
    target.parent.mkdir()
    older_store(state / "control.sqlite3", target, 48)  # before answers kept their state
    control = ControlStore(target)
    try:
        (before,) = Archive(control).messages(PROJECT, SESSION)
    finally:
        control.close()
    assert before.entry.adapter_state is None
    assert before.entry.native == b"{}"
