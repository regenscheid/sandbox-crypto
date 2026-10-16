# SPDX-License-Identifier: MIT
"""The review's evidence by its place in the session's archive (docs/specs/
consistency-review.md, CR-2 and CR-14 as the context management spec amends them).

Every message a run adds is archived with an ID and a position in its session
(``history.archive``). A review's transcript items name the archived message each copies,
by ID and position, and so do a delegated run's actions and executions, by the tool
result each call returned; a review's dispatch names the positions its snapshot covers.
A main run starts with its view of the session, earlier runs' messages among them: the
calls whose results it held are the earlier runs' actions the review lists, as context,
so that an answer telling what an earlier run did is matched against them.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, cast

from sandbox_science.orchestrator.control import ControlError
from sandbox_science.orchestrator.history.archive import HEADER, PAGE, Archive, Archived
from sandbox_science.orchestrator.model import Message
from sandbox_science.orchestrator.projects import run_id_of

Json = dict[str, Any]


def place(row: Archived) -> Json:
    """A message's place in the archive, as a locator names it."""
    return {"id": row.message_id, "position": row.position}


def _rows(archive: Archive, project: str, session: str, run: str, after: int) -> list[Archived]:
    """The archived messages of the run named ``run`` after position ``after``."""
    found: list[Archived] = []
    while True:
        page = archive.messages(project, session, start=after + 1, run=run)
        found += page
        if len(page) < PAGE:
            return found
        after = page[-1].position


def _header(message: Message) -> str | None:
    """The archive ID a platform message's header names, if it has one."""
    content = message.get("content")
    found = HEADER.match(content) if isinstance(content, str) else None
    return found.group(1) if found is not None else None


def places(
    archive: Archive,
    project: str,
    session: str,
    run: str,
    messages: Sequence[Message],
    start: int,
    after: int,
) -> dict[int, Json]:
    """The place of each of ``messages`` from index ``start`` on that the run named
    ``run`` archived after position ``after``: a platform message's by the ID its header
    names, and the model's answers, which have none, by their order, only while each
    archived answer is the message (``_same``): from the first that is not, no answer has
    a place, so that a shift gives none rather than a wrong one. A message with no header
    has none."""
    rows = _rows(archive, project, session, run, after)
    named = {row.message_id: row for row in rows}
    answers = [row for row in rows if row.entry.kind == "assistant"]
    found: dict[int, Json] = {}
    for index in range(start, len(messages)):
        message = messages[index]
        if message.get("role") == "assistant":
            row = answers.pop(0) if answers else None
            if row is not None and not _same(row, message):
                answers, row = [], None
        else:
            row = named.get(_header(message) or "")
        if row is not None:
            found[index] = place(row)
    return found


def _same(row: Archived, message: Message) -> bool:
    """Whether archived answer ``row`` is ``message``: the same tool calls by ID, or, for
    an answer without calls, the same text."""
    calls = [
        cast(Json, call).get("id") for call in cast(list[Any], message.get("tool_calls") or [])
    ]
    if calls or row.entry.tool_calls:
        return calls == [call.get("id") for call in row.entry.tool_calls]
    return (row.entry.content or "") == (message.get("content") or "")


def held_calls(
    archive: Archive, project: str, session: str, run: str, held: Sequence[Message]
) -> list[tuple[str, str]]:
    """The earlier runs' calls whose results the view ``held`` holds: each its run's ID
    and the call's ID, read from the archive by the ID each result's header names; the
    run named ``run``'s own are not earlier, nor are another session's."""
    found: list[tuple[str, str]] = []
    for message in held:
        identifier = _header(message) if message.get("role") == "tool" else None
        if identifier is None:
            continue
        try:
            row = archive.message(project, identifier)
        except ControlError:
            continue
        earlier = run_id_of(row.run)
        if row.session == session and row.run != run and earlier and row.entry.call_id:
            found.append((earlier, row.entry.call_id[:128]))
    return found


def results(
    archive: Archive, project: str, session: str, run: str, tools: Sequence[Json | None]
) -> list[Json | None]:
    """The place of the tool result of each of the run named ``run``'s tool records
    ``tools``: each call's result is archived in the order its records are kept, and the
    two are paired only while they agree on the tool and its arguments, as the answer
    that made the call archived them, so that a shift among calls of one tool is caught."""
    rows = _rows(archive, project, session, run, 0)
    asked = {
        str(call.get("id")): cast(Json, call.get("function") or {}).get("arguments")
        for row in rows
        for call in row.entry.tool_calls
    }
    returned = [row for row in rows if row.entry.kind == "tool_result"]
    found: list[Json | None] = [None] * len(tools)
    for index, (record, row) in enumerate(zip(tools, returned, strict=False)):
        if (
            record is None
            or row.entry.source != str(record.get("tool", ""))[:128]
            or asked.get(str(row.entry.call_id)) != record.get("arguments")
        ):
            break
        found[index] = place(row)
    return found
