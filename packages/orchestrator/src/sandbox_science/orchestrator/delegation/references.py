# SPDX-License-Identifier: MIT
"""Context for a delegated task by reference (plans-and-delegation spec, PD-11, PD-13,
PD-44; context-management spec, CM-37b).

A parent gives its task records of its own work by reference instead of retelling
them: an archive message by its ID (CM-4a), a range of at most ``MAX_SPAN`` positions
of its own run, a ledger entry by its handle, or a session file by its path. ``resolve``
checks each item where the parent may itself read, its own run and its descendants or,
for a main run, its session, and refuses one outside, naming it. The delegation's brief,
or the agent message, keeps the items as resolved. A check takes none (LG-17, CM-37a).

What a task may read follows from those records alone (``sources``, ``readable``): its
brief's references, and those of each message from its parent once the message was
shown to it, as its run's archive keeps the message's notice, or its work acknowledged
it. ``state_block`` and ``message_part`` render them, as records of the parent's
earlier work and not instructions (CM-49), within ``BRIEF_CHARS`` from the brief and
``TOTAL_CHARS`` per child; what does not fit stays one line with its ID. References
grant no permission, and widen no tool, file or write area.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from typing import Annotated, Any, Final, Self, cast

from pydantic import Field, model_validator

from sandbox_science.contracts.model import Contract
from sandbox_science.orchestrator.agent_message_routes import received_by, recipient_target
from sandbox_science.orchestrator.control import ControlError
from sandbox_science.orchestrator.control.errors import INVALID_REQUEST, POLICY_DENIED
from sandbox_science.orchestrator.documents.agent_text import agent_text
from sandbox_science.orchestrator.notices import REFERENCED

Json = dict[str, Any]

MAX_REFERENCES: Final = 40
"""The most references a brief or a message carries (PD-11)."""
MAX_SPAN: Final = 50
"""The most positions a referenced range spans (PD-11)."""
MAX_PATH: Final = 512
BRIEF_CHARS: Final = 60_000
"""What a brief's references may take of the state block (CM-37b)."""
TOTAL_CHARS: Final = 120_000
"""What one child's references may take in all, its parent's messages' included."""
WHOLE_CHARS: Final = 20_000
"""The longest record shown whole: the tool output budget."""
HEAD_CHARS: Final = 2_000
"""The head of a longer record that is shown."""
MAILBOX: Final = "agent_messages"
"""The notice source of agent messages (``agent_messages.SOURCE``), whose archived
notices say a message was shown to its recipient's run."""
PREFIX: Final = "reference_refused: "

_Position = Annotated[int, Field(strict=True, ge=1, le=999_999_999_999)]


class ReferenceInput(Contract):
    """One reference as a parent gives it: exactly one of a message, a range (``from``
    and ``to``), an entry or a file."""

    message: Annotated[str, Field(pattern=r"^msg-[0-9a-f]{16}$")] | None = None
    start: _Position | None = Field(default=None, alias="from")
    end: _Position | None = Field(default=None, alias="to")
    entry: Annotated[str, Field(min_length=1, max_length=128)] | None = None
    file: Annotated[str, Field(min_length=1, max_length=MAX_PATH)] | None = None

    @model_validator(mode="after")
    def _one(self) -> Self:
        ranged = self.start is not None or self.end is not None
        named = [self.message is not None, ranged, self.entry is not None, self.file is not None]
        if sum(named) != 1:
            raise ValueError("a reference names one of message, from and to, entry or file")
        if ranged and (self.start is None or self.end is None):
            raise ValueError("a range of positions names both from and to")
        return self

    def named(self) -> str:
        if self.message is not None:
            return self.message
        if self.entry is not None:
            return self.entry
        if self.file is not None:
            return self.file[:80]
        return f"positions {self.start} to {self.end}"


References = Annotated[tuple[ReferenceInput, ...], Field(max_length=MAX_REFERENCES)]

SCHEMA: Final[Json] = {
    "type": "array",
    "maxItems": MAX_REFERENCES,
    "items": {
        "type": "object",
        "properties": {
            name: {"type": "integer" if name in ("from", "to") else "string"}
            for name in ("message", "from", "to", "entry", "file")
        },
        "additionalProperties": False,
    },
}
"""A ``references`` parameter as a tool's schema gives it."""


def _refused(
    number: int, item: ReferenceInput, why: str, kind: str = POLICY_DENIED
) -> ControlError:
    return ControlError(kind, f"{PREFIX}reference {number} ({item.named()}) {why}")


def subtree(db: sqlite3.Connection, project: str, session: str, run: str) -> frozenset[str]:
    """``run`` and the runs it delegated, at any depth, from the delegation records."""
    rows = db.execute(
        "WITH RECURSIVE tree(run) AS (VALUES (?) UNION "
        "SELECT d.run FROM delegations d JOIN tree t ON d.parent=t.run "
        "WHERE d.project_id=? AND d.session_id=?) SELECT run FROM tree",
        (run, project, session),
    ).fetchall()
    return frozenset(str(name) for (name,) in rows)


def resolve(
    db: sqlite3.Connection,
    project: str,
    session: str,
    run: str,
    *,
    delegated: bool,
    items: Sequence[ReferenceInput],
) -> tuple[Json, ...]:
    """The references of ``run``, a parent, as its delegation or message keeps them,
    each checked where the parent may read (PD-13): a delegated parent's own run and its
    descendants, a main run's session; anything else is refused, naming the item."""
    if len(items) > MAX_REFERENCES:
        raise ControlError(INVALID_REQUEST, f"{PREFIX}at most {MAX_REFERENCES} references")
    runs = subtree(db, project, session, run) if delegated else None
    kept: list[Json] = []
    for number, item in enumerate(items, 1):
        found = _resolved(db, project, session, run, runs, number, item)
        if found not in kept:
            kept.append(found)
    return tuple(kept)


def _resolved(
    db: sqlite3.Connection,
    project: str,
    session: str,
    run: str,
    runs: frozenset[str] | None,
    number: int,
    item: ReferenceInput,
) -> Json:
    if item.message is not None:
        row = db.execute(
            "SELECT session, run FROM archive_messages WHERE project=? AND message_id=?",
            (project, item.message),
        ).fetchone()
        if row is None:
            raise _refused(number, item, "is no message of this project's archive")
        if row[0] != session:
            raise _refused(number, item, "is of another session: a parent references its own")
        if runs is not None and row[1] not in runs:
            raise _refused(
                number,
                item,
                "is not of your own run or of a run you delegated: a delegated parent "
                "references neither its parent's records nor its siblings'",
            )
        return {"message": item.message}
    if item.start is not None and item.end is not None:
        if item.end < item.start or item.end - item.start + 1 > MAX_SPAN:
            raise _refused(
                number, item, f"is no range of 1 to {MAX_SPAN} positions", INVALID_REQUEST
            )
        (last,) = db.execute(
            "SELECT coalesce(max(position), 0) FROM archive_messages WHERE project=? AND session=?",
            (project, session),
        ).fetchone()
        # Positions only grow: a range ends where the archive did, so the messages it
        # names never change.
        end = min(item.end, int(last))
        (count,) = db.execute(
            "SELECT count(*) FROM archive_messages WHERE project=? AND session=? AND run=? "
            "AND position BETWEEN ? AND ?",
            (project, session, run, item.start, end),
        ).fetchone()
        if not count:
            raise _refused(number, item, "holds no message of your own run")
        return {"range": [item.start, end], "run": run}
    if item.entry is not None:
        from sandbox_science.orchestrator.ledger.queries import entry_view  # no cycle

        try:
            view = entry_view(db, project, item.entry)
        except ControlError:
            raise _refused(number, item, "is no entry of this project's ledger") from None
        if view.session is not None and view.session != session:
            raise _refused(number, item, "is a decision meant for another session")
        return {"entry": view.entry_id, "handle": view.handle}
    assert item.file is not None  # noqa: S101 - the model names one target
    return {"file": _path(number, item, session)}


def _path(number: int, item: ReferenceInput, session: str) -> str:
    """A referenced file's path as the child's file tools read it: in the session or
    ``shared/``, relative, without ``.`` or ``..`` parts (DA-16, DA-18)."""
    assert item.file is not None  # noqa: S101 - called for a file
    try:
        path = agent_text(item.file.strip(), "a referenced file")
    except ControlError as error:
        raise _refused(number, item, error.message, INVALID_REQUEST) from None
    parts = path.split("/")
    if (
        not path
        or path.startswith("/")
        or "\\" in path
        or any(part in ("", ".", "..") for part in parts)
    ):
        raise _refused(
            number,
            item,
            "is no path in the session or shared/: give it relative, without . or .. parts",
            INVALID_REQUEST,
        )
    if parts[0] == "sessions" and len(parts) > 1 and parts[1] == session:
        raise _refused(number, item, "names the session again: give it relative to the session")
    return path


@dataclass(frozen=True)
class Source:
    """A record that carries a child's references: its brief, or one of its parent's
    messages to it."""

    message: str | None
    """The agent message's ID; ``None`` for the brief."""
    sent_at: str | None
    references: tuple[Json, ...]
    shown: bool
    """Whether the child was shown it: always, for the brief; for a message, once its
    notice was archived in the child's run or its work acknowledged it."""


def _items(value: object) -> tuple[Json, ...]:
    if not isinstance(value, list):
        return ()
    listed = cast(list[object], value)
    return tuple(cast(Json, item) for item in listed if isinstance(item, dict))


def sources(
    db: sqlite3.Connection, project: str, session: str, delegation: str, run: str | None
) -> list[Source]:
    """What carries the references of ``delegation``, whose current run is ``run``: its
    brief, then its parent's messages that carry any, in the order they were sent, each
    marked shown when the records say the child saw it. A check has none (CM-37a)."""
    row = db.execute(
        "SELECT kind, brief FROM delegations WHERE project_id=? AND session_id=? "
        "AND delegation_id=?",
        (project, session, delegation),
    ).fetchone()
    if row is None or row[0] != "task":
        return []
    brief: object = json.loads(str(row[1]))
    briefed = cast(Json, brief).get("references") if isinstance(brief, dict) else None
    found = [Source(None, None, _items(briefed), True)]
    target, parameters = recipient_target(db, project, session, delegation)
    rows = db.execute(
        "SELECT message_id, created_at, refs, delivered_run FROM agent_messages "  # noqa: S608 - fixed predicate
        f"WHERE project_id=? AND session_id=? AND {target} AND refs <> '[]' ORDER BY rowid",
        (project, session, *parameters),
    ).fetchall()
    for message, sent, refs, delivered in rows:
        shown = run is not None and (
            (delivered is not None and received_by(db, project, session, delegation, delivered))
            or _noticed(db, project, session, run, str(message))
        )
        found.append(Source(str(message), str(sent), _items(json.loads(str(refs))), shown))
    return found


def _noticed(db: sqlite3.Connection, project: str, session: str, run: str, message: str) -> bool:
    """Whether ``run``'s archive holds the notice that showed it agent message ``message``."""
    return (
        db.execute(
            "SELECT 1 FROM archive_messages a WHERE a.project=? AND a.session=? AND a.run=? "
            "AND a.kind='notice' AND a.source=? AND EXISTS "
            "(SELECT 1 FROM json_each(a.refs) WHERE value=?) LIMIT 1",
            (project, session, run, MAILBOX, message),
        ).fetchone()
        is not None
    )


def readable(
    db: sqlite3.Connection, project: str, session: str, found: Sequence[Source]
) -> frozenset[str]:
    """The archive messages the shown references name: each referenced message, and the
    messages of each referenced range, which the child's ``history_read`` accepts and
    its ``observed`` changes may anchor on, besides its own run (CM-37b)."""
    ids: set[str] = set()
    for source in found:
        if not source.shown:
            continue
        for item in source.references:
            if isinstance(message := item.get("message"), str):
                ids.add(message)
            elif (span := _span(item)) is not None:
                ids.update(str(m) for (m,) in _ranged(db, project, session, *span, "message_id"))
    return frozenset(ids)


def readable_for(
    db: sqlite3.Connection, project: str, session: str, delegation: str, run: str
) -> frozenset[str]:
    """``readable`` for ``delegation``'s current run ``run``, from the records."""
    return readable(db, project, session, sources(db, project, session, delegation, run))


def _span(item: Json) -> tuple[str, int, int] | None:
    found, run = item.get("range"), item.get("run")
    if not isinstance(found, list) or not isinstance(run, str):
        return None
    bounds = cast(list[object], found)
    if len(bounds) != 2 or not all(type(b) is int for b in bounds):
        return None
    return run, cast(int, bounds[0]), cast(int, bounds[1])


def _ranged(
    db: sqlite3.Connection, project: str, session: str, run: str, start: int, end: int, columns: str
) -> list[tuple[object, ...]]:
    return db.execute(
        f"SELECT {columns} FROM archive_messages WHERE project=? AND session=? AND run=? "  # noqa: S608 - fixed columns
        "AND position BETWEEN ? AND ? ORDER BY position LIMIT ?",
        (project, session, run, start, end, MAX_SPAN),
    ).fetchall()


# --- Materialization ----------------------------------------------------------------


def state_block(
    db: sqlite3.Connection, project: str, session: str, delegation: str, run: str
) -> str | None:
    """The part of ``delegation``'s state block its references take: the brief's and
    those of each message it was shown, within the budgets; ``None`` with none."""
    found = sources(db, project, session, delegation, run)
    parts = [
        text
        for source, text in zip(found, _rendered(db, project, session, found), strict=True)
        if source.shown and source.references
    ]
    return REFERENCED.text + "\n\n" + "\n\n".join(parts) if parts else None


def message_part(
    db: sqlite3.Connection, project: str, session: str, delegation: str, message: str
) -> str | None:
    """Agent message ``message``'s references as its notice to ``delegation`` shows
    them, within what the brief and the earlier messages left of the budget."""
    found = sources(db, project, session, delegation, None)
    for source, text in zip(found, _rendered(db, project, session, found), strict=True):
        if source.message == message and source.references:
            return REFERENCED.text + "\n\n" + text
    return None


def _rendered(
    db: sqlite3.Connection, project: str, session: str, found: Sequence[Source]
) -> list[str]:
    """Each source's part, in order: the brief's within ``BRIEF_CHARS``, and all of
    them within ``TOTAL_CHARS``. The budget follows the order the messages were sent,
    whether shown yet or not, so each part is the same whenever it is rendered."""
    left = TOTAL_CHARS
    parts: list[str] = []
    for source in found:
        budget = min(left, BRIEF_CHARS) if source.message is None else left
        heading = (
            "From your parent's brief:"
            if source.message is None
            else f"From your parent's message {source.message}, sent {source.sent_at}:"
        )
        lines, used = [heading], 0
        for item in source.references:
            for text, line in _records(db, project, session, item):
                if text is not None and used + len(text) <= budget:
                    lines.append(text)
                    used += len(text)
                else:
                    lines.append(line)
                    if text is None or "range" in item:
                        # The rest of a range stays one line, which ``line`` says.
                        break
        left -= used
        parts.append("\n\n".join(lines))
    return parts


def _records(
    db: sqlite3.Connection, project: str, session: str, item: Json
) -> Iterator[tuple[str | None, str]]:
    """Each record an item stands for, as shown and as its one line past the budget."""
    if isinstance(message := item.get("message"), str):
        yield _message(db, project, message)
    elif (span := _span(item)) is not None:
        run, start, end = span
        rows = _ranged(db, project, session, run, start, end, "message_id, position")
        for index, (found, position) in enumerate(rows):
            text, _ = _message(db, project, str(found))
            rest = len(rows) - index
            yield (
                text,
                f"- {rest} more of positions {start} to {end} of run {run}, from position "
                f"{position} ({found}) on: past the budget; history_read reads them, with "
                f"from {position} and to {end}",
            )
    elif isinstance(entry := item.get("entry"), str):
        yield _entry(db, project, entry, str(item.get("handle", entry)))
    elif isinstance(path := item.get("file"), str):
        text = f"File {path}, in the session: read it with your file tools."
        yield text, text


def _message(db: sqlite3.Connection, project: str, message: str) -> tuple[str | None, str]:
    from sandbox_science.orchestrator.context.history.archive import LABELS, escape  # no cycle

    row = db.execute(
        "SELECT kind, source, run, position, created, coalesce(length(content), 0), "
        "coalesce(length(tool_calls), 0) FROM archive_messages WHERE project=? AND message_id=?",
        (project, message),
    ).fetchone()
    if row is None:  # pragma: no cover - archived messages are never removed
        return None, f"- {message}: not found in the archive"
    kind, source, run, position, created, content_chars, calls_chars = row
    label = LABELS.get(str(kind), "answer")
    if source and kind in ("notice", "tool_result"):
        label += f" of {source}"
    about = f"{message} ({label}, run {run}, position {position}, {created})"
    line = f"- {about}: past the budget; history_read reads it"
    head = f"Record {about}:"
    if int(content_chars) + int(calls_chars) <= WHOLE_CHARS:
        content, calls = db.execute(
            "SELECT content, tool_calls FROM archive_messages WHERE project=? AND message_id=?",
            (project, message),
        ).fetchone()
        text = "\n".join([*([content] if content else []), *_calls(calls)])
        return f"{head}\n{escape(text)}", line
    (content,) = db.execute(
        "SELECT substr(coalesce(content, tool_calls, ''), 1, ?) FROM archive_messages "
        "WHERE project=? AND message_id=?",
        (HEAD_CHARS, project, message),
    ).fetchone()
    total = int(content_chars) + int(calls_chars)
    shown = (
        f"{head} its first {HEAD_CHARS} of {total} characters; history_read reads the rest\n"
        f"{escape(str(content))}"
    )
    return shown, line


def _calls(value: object) -> list[str]:
    """An answer's tool calls, one JSON object a line, as the archive's text gives them."""
    if not isinstance(value, str):
        return []
    found: object = json.loads(value)
    calls = cast(list[Json], found) if isinstance(found, list) else []
    return [
        json.dumps(
            {
                "tool_call": call.get("id"),
                "name": cast(Json, call.get("function") or {}).get("name"),
                "arguments": cast(Json, call.get("function") or {}).get("arguments"),
            },
            ensure_ascii=False,
        )
        for call in calls
    ]


_LEFT_OUT: Final = frozenset(
    {"incoming", "incoming_total", "findings", "pins", "figure_checks", "anchors", "author"}
)


def _entry(db: sqlite3.Connection, project: str, entry: str, handle: str) -> tuple[str | None, str]:
    """A referenced entry as its current view, its long parts clipped to the budget a
    record is shown whole within."""
    from sandbox_science.orchestrator.context.history.archive import escape  # no cycle
    from sandbox_science.orchestrator.ledger.queries import entry_view  # no cycle

    line = f"- ledger entry {handle}: past the budget; ledger_read reads it"
    try:
        view = entry_view(db, project, entry)
    except ControlError as error:  # pragma: no cover - entries are never removed
        return None, f"- ledger entry {handle}: {error.message}"
    shown = json.dumps(view.model_dump(mode="json", exclude=set(_LEFT_OUT)), ensure_ascii=False)
    head = f"Ledger entry {view.handle}, its current view (revision {view.revision}):"
    if len(shown) > WHOLE_CHARS:
        head = f"{head} its first {HEAD_CHARS} of {len(shown)} characters; ledger_read reads it"
        shown = shown[:HEAD_CHARS]
    return f"{head}\n{escape(shown)}", line
