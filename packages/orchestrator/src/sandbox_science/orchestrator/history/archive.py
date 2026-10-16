# SPDX-License-Identifier: MIT
"""The sessions' archive (docs/specs/context-management.md, CM-3 to CM-7, CM-4a).

Every message a run adds to its conversation is kept here whole, as the run adds it,
and is never changed or removed: tasks, the user's messages, the comments, notices and
delegation outcomes the platform delivers, the model's answers with their tool calls,
and tool results. Each has an ID, ``msg-`` and 16 hexadecimal digits, unique in the
project, and a position in its session that only grows; a session's runs, delegated
runs included, share one sequence. The archive keeps each message's content as the run
added it. A message the platform writes is sent with a header line naming its ID and
kind, such as ``[msg-5f0c2e9a41d7b386 · tool result · sage]``, and with a backslash
before each line of its content that looks like one (``escape``): ``Archived.chat``
renders that, the same each time, so that what a model call was sent can be rebuilt.
Changing ``header`` or ``escape`` changes how earlier messages render, which the prompt
cache and Claude's thinking bind to, so either needs a new scheme rather than an edit.
A model's answer keeps its content as the model wrote it, beside the gateway's native
answer, the model and the gateway's version.

Two FTS5 indexes cover each message's text and each file saved behind a sample, kept up
to 1 MiB: one by words (``unicode61``, ranked by BM25), one by trigrams, which finds
substrings. They are contentless: they hold no copy of the text, and SQLite cannot
rebuild them from themselves. Rebuilding one means deleting its rows (``'delete-all'``)
and inserting each document of ``archive_docs`` again under its ``doc``: a saved file's
``text``, or for a message the ``Entry.text`` of its row.

The session view reads the archive with ``messages`` and ``message``: an ``Archived``
message is its ``entry`` as the run added it, with its ID, its position, its run, its
parent run (for a delegated run's) and its time, and ``chat()`` is the message as it was
sent.
"""

from __future__ import annotations

import json
import re
import secrets
import sqlite3
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC
from typing import Any, Final, Literal, cast

from sandbox_science.contracts import SYSTEM_CLOCK, Clock
from sandbox_science.orchestrator.control import ControlError, ControlStore
from sandbox_science.orchestrator.control.errors import INVALID_REQUEST, NOT_FOUND

Json = dict[str, Any]
Role = Literal["user", "assistant", "tool"]

LABELS: Final = {
    "task": "task",
    "user": "user message",
    "comment": "comment",
    "notice": "notice",
    "outcome": "delegation outcome",
    "tool_result": "tool result",
}
"""Each kind of message the platform writes, as its header names it."""
KINDS: Final = (*LABELS, "assistant")
PAGE: Final = 200
"""The most messages ``messages`` returns at once."""
KEPT_BYTES: Final = 1024 * 1024
"""The largest saved file the archive keeps and the indexes cover (CM-7)."""

HEADER: Final = re.compile(r"\[(msg-[0-9a-f]{16}) · [^\]\n]*\]\n")
_LOOKALIKE: Final = re.compile(r"(?:\W|_)*?\[\s*msg\s*-", re.IGNORECASE)
"""A header as a reader may take it: after any characters that are not letters or digits,
``[``, ``msg`` and a dash, with any spaces between them."""
_UNSAFE: Final = re.compile(r"[^A-Za-z0-9_.:-]")
_PLACE: Final = ("project", "message_id", "session", "position", "run", "parent", "created")
_FIELDS: Final = (
    "role",
    "kind",
    "content",
    "source",
    "refs",
    "call_id",
    "tool_calls",
    "files",
    "model",
    "gateway_version",
    "native_format",
    "native",
)
_COLUMNS: Final = ", ".join((*_PLACE, *_FIELDS))
_INDEXES: Final = ("archive_words", "archive_trigrams")


def header(message_id: str, kind: str, source: str | None = None) -> str:
    """The first line of a message the platform writes (CM-4a): its ID, its kind and,
    for a notice or a tool result, its source or tool."""
    parts = [message_id, LABELS[kind]]
    if source and kind in ("notice", "tool_result"):
        parts.append(_UNSAFE.sub("_", source)[:64])
    return "[" + " · ".join(parts) + "]\n"


def escape(text: str) -> str:
    """``text`` with a backslash before each line that begins like a header, so that
    only the platform's headers read as headers (CM-4a). A line looks like one when, read
    as compatibility characters (NFKC: a fullwidth bracket is a bracket) without invisible
    formatting characters, and with any dash taken for a hyphen, it begins with ``[msg-``
    after punctuation, spaces or quote markers, in any case and with any spaces inside.
    Every line break Python knows ends a line."""
    return "".join(
        f"\\{line}" if _header_like(line) else line for line in text.splitlines(keepends=True)
    )


def _header_like(line: str) -> bool:
    shown = "".join(
        "-" if char == "\u2212" or unicodedata.category(char) == "Pd" else char
        for char in unicodedata.normalize("NFKC", line[:256])
        if unicodedata.category(char) != "Cf"
    )
    return _LOOKALIKE.match(shown) is not None


def body(content: str) -> str:
    """A message's content after its header, for a reader that parses what follows."""
    found = HEADER.match(content)
    return content if found is None else content[found.end() :]


@dataclass(frozen=True)
class Entry:
    """A message as a run adds it, and as the archive keeps it: the content as the run
    added it, without the header it is sent with."""

    role: Role
    kind: str
    content: str | None
    source: str | None = None
    """A notice's source, or the tool a result answers."""
    refs: tuple[str, ...] = ()
    """The IDs a notice delivers, such as the user's chat message or a delegation."""
    call_id: str | None = None
    tool_calls: tuple[Json, ...] = ()
    """An answer's tool calls, as sent."""
    files: Json | None = None
    """What a tool result links to (CM-6): its saved files and recorded revisions."""
    model: str | None = None
    gateway_version: str | None = None
    native_format: str | None = None
    native: bytes | None = None
    """The gateway's answer, as it sent it (CM-5)."""
    saved: Mapping[str, str] = field(default_factory=dict[str, str], compare=False)
    """The texts of the files saved behind a tool result's sample, by path, as the run
    gives them to the archive; ``Archive.files`` reads them back."""

    @property
    def text(self) -> str:
        """Its whole text: its content, then an answer's tool calls, one JSON object a
        line."""
        calls = [
            json.dumps(
                {
                    "tool_call": call.get("id"),
                    "name": cast(Json, call.get("function") or {}).get("name"),
                    "arguments": cast(Json, call.get("function") or {}).get("arguments"),
                },
                ensure_ascii=False,
            )
            for call in self.tool_calls
        ]
        return "\n".join([*([self.content] if self.content else []), *calls])


@dataclass(frozen=True)
class SavedFile:
    """A file saved behind a sample (Shell 5), as the archive keeps it, with the artifact
    revision its call recorded it as (RA-27), if it did."""

    path: str
    size: int
    text: str | None
    left_out: str | None
    """Why the archive keeps no text of it."""
    artifact_id: str | None
    revision: int | None


@dataclass(frozen=True)
class Archived:
    """A message in the archive: its place, and the message itself."""

    project: str
    message_id: str
    session: str
    position: int
    run: str
    parent: str | None
    """For a delegated run's message, its parent run."""
    created: str
    entry: Entry

    def chat(self) -> Json:
        """The message as it was sent, in the chat format the harness uses: a platform
        message with its header and its look-alikes escaped, an answer as the model
        wrote it."""
        entry = self.entry
        content = entry.content
        if entry.role != "assistant":
            content = header(self.message_id, entry.kind, entry.source) + escape(content or "")
        message: Json = {"role": entry.role, "content": content}
        if entry.tool_calls:
            message["tool_calls"] = [dict(call) for call in entry.tool_calls]
        if entry.call_id is not None:
            message["tool_call_id"] = entry.call_id
        return message


class Archive:
    """The sessions' archive in one control store, which its owner closes."""

    def __init__(self, control: ControlStore, *, clock: Clock = SYSTEM_CLOCK) -> None:
        self.control, self.clock = control, clock

    def append(
        self, project: str, session: str, run: str, entry: Entry, *, parent: str | None = None
    ) -> Archived:
        """Keep a message ``run`` adds to its conversation, and index it, in one change;
        the message as archived."""
        if entry.kind not in KINDS or (entry.kind == "assistant") != (entry.role == "assistant"):
            raise ControlError(INVALID_REQUEST, f"no {entry.role} message is of kind {entry.kind}")
        created = self.clock.now().astimezone(UTC).isoformat()
        with self.control.write() as tx:
            db = tx.sql
            (last,) = db.execute(
                "SELECT coalesce(max(position), 0) FROM archive_messages "
                "WHERE project = ? AND session = ?",
                (project, session),
            ).fetchone()
            message_id = "msg-" + secrets.token_hex(8)
            while db.execute(
                "SELECT 1 FROM archive_messages WHERE project = ? AND message_id = ?",
                (project, message_id),
            ).fetchone():
                message_id = "msg-" + secrets.token_hex(8)
            archived = Archived(project, message_id, session, last + 1, run, parent, created, entry)
            try:
                db.execute(
                    f"INSERT INTO archive_messages ({_COLUMNS}) "  # noqa: S608 - its own names
                    f"VALUES ({', '.join('?' * (len(_PLACE) + len(_FIELDS)))})",
                    _row(archived),
                )
            except sqlite3.IntegrityError as error:
                raise ControlError(INVALID_REQUEST, f"the message is refused: {error}") from None
            _document(db, project, message_id, entry.text)
            recorded = {
                link.get("path"): link
                for link in cast(list[Json], (entry.files or {}).get("recorded") or [])
            }
            for path, text in entry.saved.items():
                link = recorded.get(path) or {}
                artifact, revision = link.get("artifact_id"), link.get("revision")
                size, left = len(text.encode()), None
                if size > KEPT_BYTES:
                    left = "larger than 1 MiB, so not kept: " + (
                        f"read artifact {artifact} at revision {revision}"
                        if artifact is not None
                        else "its call recorded no artifact revision of it"
                    )
                _document(
                    db,
                    project,
                    message_id,
                    text if left is None else None,
                    path=path,
                    size=size,
                    left=left,
                    artifact=artifact,
                    revision=revision,
                )
        return archived

    def message(self, project: str, message_id: str) -> Archived:
        """A message of the project, by its ID."""
        with self.control.read() as tx:
            row = tx.sql.execute(
                f"SELECT {_COLUMNS} FROM archive_messages "  # noqa: S608 - its own names
                "WHERE project = ? AND message_id = ?",
                (project, message_id),
            ).fetchone()
        if row is None:
            raise ControlError(NOT_FOUND, f"the project's archive has no message {message_id}")
        return _archived(row)

    def messages(
        self,
        project: str,
        session: str,
        *,
        start: int = 1,
        end: int | None = None,
        run: str | None = None,
        limit: int = PAGE,
    ) -> list[Archived]:
        """The session's messages from position ``start`` to ``end``, in order, at most
        ``limit`` of them; with ``run``, that run's alone."""
        with self.control.read() as tx:
            rows = tx.sql.execute(
                f"SELECT {_COLUMNS} FROM archive_messages "  # noqa: S608 - its own names
                "WHERE project = ? AND session = ? AND position >= ? "
                "AND (? IS NULL OR position <= ?) AND (? IS NULL OR run = ?) "
                "ORDER BY position LIMIT ?",
                (project, session, start, end, end, run, run, max(1, min(limit, PAGE))),
            ).fetchall()
        return [_archived(row) for row in rows]

    def files(self, project: str, message_id: str) -> list[SavedFile]:
        """The files saved behind a tool result's sample."""
        with self.control.read() as tx:
            rows = tx.sql.execute(
                "SELECT path, size, text, left_out, artifact_id, revision FROM archive_docs "
                "WHERE project = ? "
                "AND message_id = ? AND path IS NOT NULL ORDER BY doc",
                (project, message_id),
            ).fetchall()
        return [SavedFile(*row) for row in rows]


def _document(
    db: sqlite3.Connection,
    project: str,
    message_id: str,
    text: str | None,
    *,
    path: str | None = None,
    size: int | None = None,
    left: str | None = None,
    artifact: str | None = None,
    revision: int | None = None,
) -> None:
    """One text the indexes cover: a message's, whose text stays in its row, or a saved
    file's, kept here with its artifact revision; or a file left out, with why, which
    they do not cover."""
    doc = db.execute(
        "INSERT INTO archive_docs (project, message_id, path, size, text, left_out, "
        "artifact_id, revision) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            project,
            message_id,
            path,
            size,
            text if path is not None else None,
            left,
            artifact,
            revision,
        ),
    ).lastrowid
    if text is not None:
        for table in _INDEXES:
            db.execute(
                f"INSERT INTO {table} (rowid, body) VALUES (?, ?)",  # noqa: S608 - its own
                (doc, text),
            )


def _row(archived: Archived) -> tuple[object, ...]:
    entry = archived.entry
    encoded: dict[str, object] = {
        "refs": json.dumps(list(entry.refs)),
        "tool_calls": json.dumps(list(entry.tool_calls)) if entry.tool_calls else None,
        "files": json.dumps(entry.files) if entry.files is not None else None,
    }
    return (
        *(getattr(archived, name) for name in _PLACE),
        *(encoded[name] if name in encoded else getattr(entry, name) for name in _FIELDS),
    )


def _archived(row: Sequence[Any]) -> Archived:
    fields = dict(zip(_FIELDS, row[len(_PLACE) :], strict=True))
    fields["refs"] = tuple(json.loads(fields["refs"]))
    fields["tool_calls"] = tuple(json.loads(fields["tool_calls"] or "[]"))
    fields["files"] = json.loads(fields["files"]) if fields["files"] is not None else None
    return Archived(*row[: len(_PLACE)], entry=Entry(**fields))
