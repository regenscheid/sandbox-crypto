# SPDX-License-Identifier: MIT
"""The context owner preserves canonical evidence independently of its model view."""

from pathlib import Path

import pytest

from sandbox_science.contracts import ExecutionContext
from sandbox_science.orchestrator.context.agent import AgentContext
from sandbox_science.orchestrator.context.history import Archive, RunArchive
from sandbox_science.orchestrator.hooks import Appended, MessageLog
from sandbox_science.orchestrator.model import Message, ModelTurn, ScriptedModel, ToolCall
from sandbox_science.orchestrator.projects import ProjectStore


async def test_fold_and_restore_keep_full_archived_history_and_answered_usage(tmp_path: Path):
    projects = ProjectStore(tmp_path)
    try:
        projects.create("p", "minimal", "sha256:" + "a" * 64)
        session = projects.open_session("p", "s", dedicated=False)
        scope = ExecutionContext(
            project_id="p", session_id="s", environment_id=session.environment_id
        )
        archive = Archive(projects.control)
        log = RunArchive(archive, scope, run="agent-run", parent=None)
        system: Message = {"role": "system", "content": "Pinned instructions."}
        context = AgentContext.start([system], log=log)
        await context.append({"role": "user", "content": "Original question."}, Appended("task"))
        question = log.last_message
        assert question is not None
        size = context.size
        reply = await context.complete(
            ScriptedModel([ModelTurn("Supported answer.", input_tokens=100, output_tokens=12)]),
            (),
        )
        context.record_reply(reply, input_size=size)
        await context.append(
            {"role": "assistant", "content": reply.text}, Appended("assistant", reply=reply)
        )
        answer = log.last_message
        assert answer is not None
        full_history = list(context.messages)
        summary: Message = {"role": "user", "content": "A compact account of the question."}
        context.rebuild([summary])
        await context.append({"role": "user", "content": "A follow-up."}, Appended("task"))
        assert context.sent == [system, summary, context.messages[-1]]
        assert list(context.messages[:-1]) == full_history
        assert context.base == 3
        assert context.since == 1
        assert archive.message("p", question).entry.text == "Original question."
        assert archive.message("p", answer).entry.text == "Supported answer."
        assert context.usage[0].input_tokens == 100
        assert context.usage[0].characters == size + len("Supported answer.")

        # Applying a transported fold is the same model view, not a second archive.
        restored = AgentContext(messages=list(context.messages), usage=list(context.usage))
        restored.restore(context.rebuilt, base=context.base, since=context.since)
        assert restored.sent == context.sent
        assert restored.size == context.size
    finally:
        projects.close()


class RefusingLog(MessageLog):
    async def start_tool(self, call: ToolCall, operation_id: str) -> None:
        raise AssertionError("this test dispatches no tools")

    async def append(self, message: Message, appended: Appended) -> Message:
        raise OSError("archive unavailable")

    def retained_tool(self, record):
        raise AssertionError("this test dispatches no tools")


async def test_archive_failure_never_adds_an_uncommitted_message_to_the_next_prompt():
    original: Message = {"role": "system", "content": "Pinned instructions."}
    context = AgentContext.start([original], log=RefusingLog())
    with pytest.raises(OSError, match="archive unavailable"):
        await context.append({"role": "user", "content": "Uncommitted."}, Appended("task"))
    assert context.sent == [original]
    assert not context.usage
