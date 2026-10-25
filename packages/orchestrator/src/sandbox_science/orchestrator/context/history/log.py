# SPDX-License-Identifier: MIT
"""A run's message log (``hooks.MessageLog``): each message the run adds to its
conversation, kept in its session's archive as it adds it (docs/specs/
context-management.md, CM-4), and returned with its ID header (CM-4a). What a tool
result read of its sources goes into the read register in the same change (CM-12).

Another process's change can hold the control store for longer than a change waits for
it (``busy``). An archive write that meets one is tried again, waiting longer each time,
for about a minute, before it fails the run: a ``busy`` change wrote nothing, since it
fails as it begins, so trying again cannot write a message twice."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Mapping, Sequence, Set
from functools import partial
from typing import Any, Final, cast

from sandbox_science.contracts import SYSTEM_CLOCK, Clock, ExecutionContext
from sandbox_science.orchestrator.aio import threaded
from sandbox_science.orchestrator.control import ControlError
from sandbox_science.orchestrator.control.errors import BUSY
from sandbox_science.orchestrator.hooks import Appended
from sandbox_science.orchestrator.model import Message, ToolCall
from sandbox_science.orchestrator.notices import DELEGATIONS, REQUESTS
from sandbox_science.orchestrator.purpose import purpose_of, without

from .archive import Archive, Archived, Entry
from .operations import ToolJournal, ToolRecord
from .reads import Read, reads_of
from .retention import retain
from .usage import AnsweredUsage

Json = dict[str, Any]
_log = logging.getLogger(__name__)

STATE_BYTES: Final = 65536
"""The largest adapter state the archive keeps with an answer."""
PATIENCE: Final = 60.0
"""Seconds an archive write keeps trying while the control store is busy."""


class RunArchive:
    """One run's appends to its session's archive. ``message_ids`` are the user's chat
    messages the run's task stands for, if any, which its archived task names."""

    def __init__(
        self,
        archive: Archive,
        context: ExecutionContext,
        *,
        run: str,
        parent: str | None,
        message_ids: Sequence[str] = (),
        patience: float = PATIENCE,
        clock: Clock = SYSTEM_CLOCK,
    ) -> None:
        self.archive, self.context, self.run, self.parent = archive, context, run, parent
        self.message_ids, self.patience, self.clock = tuple(message_ids), patience, clock
        self.last_tool: str | None = None
        self.last_message: str | None = None

    async def start_tool(self, call: ToolCall, operation_id: str) -> None:
        await _write(
            partial(
                ToolJournal(self.archive.control, clock=self.clock).start,
                self.context,
                self.run,
                operation_id,
                call,
            ),
            run=self.run,
            patience=self.patience,
            clock=self.clock,
        )

    async def append(self, message: Message, appended: Appended) -> Message:
        self.last_tool = None
        entry = entry_of(message, appended, task_refs=self.message_ids)
        call, outcome = appended.call, appended.outcome
        reads = (
            reads_of(
                call.name,
                without(call.arguments),
                outcome.record,
                entry.content or "",
                outcome.saved,
            )
            if appended.kind == "tool_result" and call is not None and outcome is not None
            else []
        )
        archived = await patiently(
            self.archive,
            self.context,
            self.run,
            entry,
            parent=self.parent,
            reads=reads,
            tool_record=(
                ToolRecord(
                    {"tool": call.name, "arguments": without(call.arguments), **outcome.record},
                    appended.operation_id,
                )
                if appended.kind == "tool_result" and call is not None and outcome is not None
                else None
            ),
            consumed=appended.consumed,
            model_usage=(
                AnsweredUsage(
                    ordinal=appended.usage_index,
                    observed_at=self.clock.now(),
                    usage=appended.usage,
                    intent_id=appended.reply.intent_id if appended.reply is not None else None,
                )
                if appended.usage is not None and appended.usage_index is not None
                else None
            ),
            patience=self.patience,
            clock=self.clock,
        )
        self.last_message = archived.message_id
        # The model's answer goes on as it wrote it; the platform's messages with headers.
        if appended.kind == "tool_result" and call is not None and outcome is not None:
            self.last_tool = archived.message_id
        return message if entry.role == "assistant" else archived.chat()

    def retained_tool(self, record: Json) -> Json:
        if self.last_tool is None:
            raise RuntimeError("a retained tool record requires its committed result")
        message, self.last_tool = self.last_tool, None
        return retain(record, project=self.context.project_id, message=message)


async def patiently(
    archive: Archive,
    context: ExecutionContext,
    run: str,
    entry: Entry,
    *,
    parent: str | None = None,
    reads: Sequence[Read] = (),
    tool_record: ToolRecord | None = None,
    model_usage: AnsweredUsage | None = None,
    consumed: Mapping[str, Set[str]] | None = None,
    patience: float = PATIENCE,
    clock: Clock = SYSTEM_CLOCK,
) -> Archived:
    """``entry`` appended to ``run``'s archive, with what it read (CM-12), tried again
    while the store is busy."""
    return await _write(
        partial(
            archive.append,
            context.project_id,
            context.session_id,
            run,
            entry,
            parent=parent,
            reads=reads,
            tool_record=tool_record,
            model_usage=model_usage,
            consumed=consumed,
        ),
        run=run,
        patience=patience,
        clock=clock,
    )


async def _write[T](call: Callable[[], T], *, run: str, patience: float, clock: Clock) -> T:
    """Retry only BUSY, which failed before BEGIN and therefore wrote nothing."""
    deadline, wait = clock.monotonic() + patience, 0.5
    while True:
        try:
            return await threaded(call)
        except ControlError as error:
            if error.kind != BUSY or clock.monotonic() + wait > deadline:
                raise
            _log.warning("the archive of run %s is busy; trying again", run)
            await clock.sleep(wait)
            wait = min(2 * wait, 8.0)


def entry_of(message: Message, appended: Appended, *, task_refs: Sequence[str] = ()) -> Entry:
    """A message the loop adds, as the archive takes it; a task names ``task_refs``."""
    content = message.get("content")
    if appended.kind == "assistant" and appended.reply is not None:
        reply = appended.reply
        native = reply.native if reply.native_format is not None else None
        return Entry(
            "assistant",
            "assistant",
            content if isinstance(content, str) else None,
            tool_calls=tuple(cast(list[Json], message.get("tool_calls") or [])),
            model=reply.model,
            gateway_version=reply.gateway_version,
            native_format=reply.native_format if native is not None else None,
            native=native,
            alias=reply.alias,
            prefix=reply.prefix,
            adapter_state=kept_state(reply.adapter_state),
        )
    text = str(content or "")
    if appended.kind == "tool_result" and appended.call is not None:
        call, outcome = appended.call, appended.outcome
        return Entry(
            "tool",
            "tool_result",
            text,
            source=call.name[:128],
            call_id=call.id[:128],
            purpose=purpose_of(call.arguments),
            files=links(outcome.record) if outcome is not None else None,
            saved=outcome.saved if outcome is not None else {},
        )
    notice = appended.notice
    if appended.kind == "notice" and notice is not None:
        kind = "notice"
        if notice.user:
            kind = "user"
        elif notice.source == REQUESTS.id:
            kind = "comment"
        elif notice.source == DELEGATIONS.id and notice.ids:
            kind = "outcome"
        return Entry("user", kind, text, source=notice.source, refs=notice.ids)
    return Entry("user", "task", text, refs=tuple(task_refs))


def kept_state(state: Mapping[str, Any] | None) -> Json | None:
    """An answer's adapter state as the archive keeps it: JSON of at most
    ``STATE_BYTES``. A larger one, or one that is not JSON, is left out with a warning:
    the adapter then does without it, as for an answer from before it was kept."""
    if state is None:
        return None
    try:
        text = json.dumps(dict(state), ensure_ascii=False)
    except (TypeError, ValueError):
        _log.warning("an answer's adapter state is not JSON; it is not archived")
        return None
    if len(text.encode()) > STATE_BYTES:
        _log.warning("an answer's adapter state is over %d bytes; it is not archived", STATE_BYTES)
        return None
    return cast(Json, json.loads(text))


def links(record: Json) -> Json | None:
    """What a tool result links to (CM-6): the files its output was saved in behind a
    sample (Shell 5), and the artifact revisions its call recorded (RA-27)."""
    found: Json = {}
    saved = cast(dict[str, str], record.get("saved_output") or {})
    if saved:
        found["saved"] = sorted(saved.values())
    recorded = cast(Json, record.get("recorded_files") or {}).get("files")
    kept = [
        {key: cast(Json, entry).get(key) for key in ("path", "artifact_id", "revision")}
        for entry in cast(list[object], recorded or [])
        if isinstance(entry, dict)
    ]
    if kept:
        found["recorded"] = kept
    return found or None
