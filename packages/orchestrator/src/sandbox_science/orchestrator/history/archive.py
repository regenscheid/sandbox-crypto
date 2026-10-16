# SPDX-License-Identifier: MIT
"""The sessions' archive (docs/specs/context-management.md, CM-3 to CM-7, CM-4a, CM-33).

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
``text``, or for a message the ``Entry.searched`` of its row. ``search`` ranks the
texts that hold every word of a query by BM25, those that also hold the whole query
first; then those that hold the whole query inside longer words, newest first; and,
when those are fewer than asked for, those that hold any of its words. ``coverage``
says what a scope's indexes hold. The retrievals' own calls and results are kept but
not indexed: they copy what the indexes cover.

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
from sandbox_science.orchestrator.documents.store import DocumentStore
from sandbox_science.orchestrator.messages import MESSAGES, thoughts
from sandbox_science.orchestrator.model import AssistantMessage

from .reads import Read, encoded

Json = dict[str, Any]
Role = Literal["user", "assistant", "tool"]

LABELS: Final = {
    "task": "task",
    "user": "user message",
    "comment": "comment",
    "notice": "notice",
    "outcome": "delegation outcome",
    "tool_result": "tool result",
    "state_block": "state block",
    "fold": "fold record",
}
"""Each kind of message the platform writes, as its header names it."""
KINDS: Final = (*LABELS, "assistant")
PAGE: Final = 200
"""The most messages ``messages`` returns at once."""
KEPT_BYTES: Final = 1024 * 1024
"""The largest saved file the archive keeps and the indexes cover (CM-7)."""
CANDIDATES: Final = 200
"""The best hits of each way of matching that a search ranks."""
COUNTED: Final = 1000
"""Hits counted past this are reported as this many."""
SNIPPET: Final = 400
SEARCH: Final = "history_search"
READ: Final = "history_read"
UNINDEXED: Final = frozenset({SEARCH, READ})
"""The retrievals, whose calls and results the archive keeps and the indexes leave out:
they would answer each search with the searches made before it."""

HEADER: Final = re.compile(r"\[(msg-[0-9a-f]{16}) · [^\]\n]*\]\n")
_LOOKALIKE: Final = re.compile(r"\[\s*msg\s*-", re.IGNORECASE)
"""A header as a reader may take it: ``[``, ``msg`` and a dash, with any spaces between
them."""
_FIRST: Final = re.compile(r"[^\W_]")
"""A line's first letter or digit."""
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
    "alias",
    "prefix",
    "adapter_state",
)
_COLUMNS: Final = ", ".join((*_PLACE, *_FIELDS))
_TERM: Final = re.compile(r'"([^"]*)"|(\S+)')
_JOINED: Final = (
    "JOIN archive_docs d ON d.doc = {0}.rowid JOIN archive_messages m "
    "ON m.project = d.project AND m.message_id = d.message_id WHERE {0} MATCH ? AND "
)
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
    after punctuation, spaces or quote markers, however many, in any case and with any
    spaces inside. Every line break Python knows ends a line. Known limits: a combining
    mark on a letter of ``msg``, or a letter from another script that looks like one,
    such as a Cyrillic em, is not caught."""
    return "".join(
        f"\\{line}" if _header_like(line) else line for line in text.splitlines(keepends=True)
    )


def _header_like(line: str) -> bool:
    # What comes before the first letter or digit, however long, counts only from its
    # last bracket on, so no run of spaces or punctuation can push a header out of view.
    first = _FIRST.search(line)
    start = len(line) if first is None else first.start()
    lead = _shown(line[:start])
    bracket = lead.rfind("[")
    if bracket < 0:
        return False
    return _LOOKALIKE.match(lead[bracket:] + _shown(line[start : start + 256])) is not None


def _shown(text: str) -> str:
    """``text`` as a reader sees it: compatibility characters (NFKC), without invisible
    formatting characters, and any dash a hyphen."""
    return "".join(
        "-" if char == "\u2212" or unicodedata.category(char) == "Pd" else char
        for char in unicodedata.normalize("NFKC", text)
        if unicodedata.category(char) != "Cf"
    )


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
    alias: str | None = None
    """The alias an answer was asked on (CM-42)."""
    prefix: str | None = None
    """The digest of what the model was given for an answer (``model.prefix_digest``)."""
    adapter_state: Json | None = None
    """What the adapter that made an answer keeps of its call (``ModelTurn``)."""
    saved: Mapping[str, str] = field(default_factory=dict[str, str], compare=False)
    """The texts of the files saved behind a tool result's sample, by path, as the run
    gives them to the archive; ``Archive.files`` reads them back."""

    @property
    def text(self) -> str:
        """Its whole text: its content, then an answer's tool calls, one JSON object a
        line."""
        return self._text(frozenset())

    @property
    def searched(self) -> str | None:
        """The text the indexes cover: ``text``, less the retrievals' calls and results
        and the state blocks and fold records, which copy what the indexes cover, and
        with the text of a Claude answer's thinking blocks that it does not hold, its
        reasoning summaries (CM-38)."""
        if self.kind in ("state_block", "fold") or (
            self.kind == "tool_result" and self.source in UNINDEXED
        ):
            return None
        text = self._text(UNINDEXED)
        if self.native_format == MESSAGES and self.native is not None:
            text = "\n".join([text, *(t for t in thoughts(self.native) if t not in text)])
        return text

    def _text(self, left_out: frozenset[str]) -> str:
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
            if cast(Json, call.get("function") or {}).get("name") not in left_out
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
class Hit:
    message: Archived
    path: str | None
    """The saved file the hit is in; ``None`` for the message itself."""
    match: str
    """``query`` (every word and the whole query), ``words`` (every word), ``substring``
    (the whole query, inside longer words) or ``any word``."""
    score: float | None
    """Its BM25 relevance, for a hit by words."""
    snippet: str


@dataclass(frozen=True)
class Found:
    hits: list[Hit]
    matched: dict[str, int]
    """How many texts of the scope each way of matching that ran found, up to
    ``COUNTED``: ``all_words``, ``whole_query`` and ``any_word``."""


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
        message with its header and its look-alikes escaped; an answer as the model
        wrote it, carrying what it came from (``AssistantMessage``), so that the adapter
        that made it can send it back as it did during the run."""
        entry = self.entry
        if entry.role == "assistant":
            return AssistantMessage(
                entry.content,
                [dict(call) for call in entry.tool_calls],
                native=entry.native,
                native_format=entry.native_format,
                alias=entry.alias,
                model=entry.model,
                gateway_version=entry.gateway_version,
                prefix=entry.prefix,
                adapter_state=entry.adapter_state,
            )
        content = header(self.message_id, entry.kind, entry.source) + escape(entry.content or "")
        message: Json = {"role": entry.role, "content": content}
        if entry.call_id is not None:
            message["tool_call_id"] = entry.call_id
        return message


class Archive:
    """The sessions' archive in one control store, which its owner closes."""

    def __init__(self, store: ControlStore | DocumentStore, *, clock: Clock = SYSTEM_CLOCK) -> None:
        """``store``: the control store, or a document store, whose control store it
        reads, as the workspace holds one."""
        self.control = store if isinstance(store, ControlStore) else store.control
        self.clock = clock

    def append(
        self,
        project: str,
        session: str,
        run: str,
        entry: Entry,
        *,
        parent: str | None = None,
        reads: Sequence[Read] = (),
    ) -> Archived:
        """Keep a message ``run`` adds to its conversation, and index it, with the sources
        a tool result read (the read register, CM-12), in one change; the message as
        archived."""
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
            _document(db, project, message_id, entry.searched)
            for read in reads:
                _read(db, project, session, message_id, entry.source or "", read)
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

    def search(
        self,
        project: str,
        query: str,
        *,
        session: str | None = None,
        run: str | None = None,
        start: int | None = None,
        end: int | None = None,
        kind: str | None = None,
        limit: int = 10,
    ) -> Found:
        """The best hits for ``query`` among the project's messages and saved files, in
        ``session`` and ``run`` if given (CM-33): the texts that hold every word and
        quoted phrase, ranked by BM25, those that also hold the whole query, case aside,
        first; then the texts that hold the whole query inside longer words, such as
        part of a hash, newest first; and, when those are fewer than ``limit``, the texts
        that hold any of its words, by BM25."""
        terms, exact, needles = _query(query)
        if not terms and exact is None:
            raise ControlError(
                INVALID_REQUEST, "give a word, a quoted phrase or at least 3 characters"
            )
        where, args = _scope(project, session, run, start, end, kind)
        ranked: list[tuple[int, str, float | None]] = []
        matched: dict[str, int] = {}
        with self.control.read() as tx:
            db = tx.sql
            try:
                if terms:
                    scored = _ranked(db, " ".join(terms), where, args)
                    held: set[int] = set()
                    if exact is not None:
                        held = {
                            doc
                            for (doc,) in db.execute(
                                "SELECT rowid FROM archive_trigrams WHERE archive_trigrams "
                                "MATCH ? AND rowid IN (SELECT value FROM json_each(?))",
                                (exact, json.dumps([doc for doc, _ in scored])),
                            )
                        }
                    ranked += [(doc, "query", score) for doc, score in scored if doc in held]
                    ranked += [(doc, "words", score) for doc, score in scored if doc not in held]
                    matched["all_words"] = _count(db, "archive_words", " ".join(terms), where, args)
                if exact is not None:
                    seen = {doc for doc, _, _ in ranked}
                    inside = db.execute(
                        "SELECT d.doc FROM archive_trigrams "  # noqa: S608 - its own names
                        + _JOINED.format("archive_trigrams")
                        + f"{where} ORDER BY d.doc DESC LIMIT ?",
                        (exact, *args, CANDIDATES),
                    ).fetchall()
                    ranked += [(doc, "substring", None) for (doc,) in inside if doc not in seen]
                    matched["whole_query"] = _count(db, "archive_trigrams", exact, where, args)
                if len(terms) > 1 and len(ranked) < limit:
                    seen = {doc for doc, _, _ in ranked}
                    some = _ranked(db, " OR ".join(terms), where, args)
                    ranked += [(doc, "any word", score) for doc, score in some if doc not in seen]
                    matched["any_word"] = _count(
                        db, "archive_words", " OR ".join(terms), where, args
                    )
            except sqlite3.OperationalError as error:
                raise ControlError(
                    INVALID_REQUEST, f"the query cannot be searched ({error})"
                ) from None
            hits = [_hit(db, doc, how, score, needles) for doc, how, score in ranked[:limit]]
        return Found(hits, matched)

    def coverage(self, project: str, *, session: str | None = None, run: str | None = None) -> Json:
        """What the indexes cover in a scope (CM-7): its messages and their positions, the
        saved files indexed and those left out with why, and the runs whose messages the
        archive lacks, having run before it existed."""
        where, args = _scope(project, session, run, None, None, None)
        docs = (
            "FROM archive_docs d JOIN archive_messages m ON m.project = d.project "
            f"AND m.message_id = d.message_id WHERE {where} AND d.path IS NOT NULL"
        )
        with self.control.read() as tx:
            db = tx.sql
            count, first, last = db.execute(
                "SELECT count(*), min(position), max(position) "  # noqa: S608
                f"FROM archive_messages m WHERE {where}",
                args,
            ).fetchone()
            indexed, left = db.execute(
                "SELECT count(*) FILTER (WHERE d.left_out IS NULL), "
                f"count(*) FILTER (WHERE d.left_out IS NOT NULL) {docs}",
                args,
            ).fetchone()
            shown = db.execute(
                f"SELECT d.message_id, d.path, d.left_out {docs} "
                "AND d.left_out IS NOT NULL ORDER BY d.doc DESC LIMIT 10",
                args,
            ).fetchall()
            earlier = 0
            if run is None:
                (earlier,) = db.execute(
                    "SELECT count(*) FROM runs r WHERE r.project_id = ? AND (? IS NULL OR "
                    "r.session_id = ?) AND NOT EXISTS (SELECT 1 FROM archive_messages a "
                    "WHERE a.project = r.project_id AND a.session = r.session_id "
                    "AND a.run = r.name)",
                    (project, session, session),
                ).fetchone()
        found: Json = {"messages": count, "files_indexed": indexed}
        if session is not None and count:
            found["positions"] = [first, last]
        if left:
            found["files_left_out"] = left
            found["left_out"] = [
                {"message_id": message, "path": path, "reason": reason}
                for message, path, reason in shown
            ]
        if earlier:
            found["runs_not_archived"] = earlier
        return found


def _scope(
    project: str,
    session: str | None,
    run: str | None,
    start: int | None,
    end: int | None,
    kind: str | None,
) -> tuple[str, list[object]]:
    clauses, args = ["m.project = ?"], [cast(object, project)]
    for clause, value in (
        ("m.session = ?", session),
        ("m.run = ?", run),
        ("m.position >= ?", start),
        ("m.position <= ?", end),
        ("m.kind = ?", kind),
    ):
        if value is not None:
            clauses.append(clause)
            args.append(value)
    return " AND ".join(clauses), args


def _query(query: str) -> tuple[list[str], str | None, list[str]]:
    """``query``'s words and quoted phrases as FTS5 strings; the whole query, quotes
    aside, as one for the trigram index, when it has at least 3 characters; and the
    texts a snippet centers on, the whole query first."""
    found = [
        " ".join((phrase or word).split())
        for phrase, word in cast(list[tuple[str, str]], _TERM.findall(query))
    ]
    found = [term for term in found if any(c.isalnum() for c in term)]
    whole = " ".join(query.replace('"', " ").split())
    exact = f'"{whole}"' if len(whole) >= 3 else None
    return ['"' + term.replace('"', '""') + '"' for term in found], exact, [whole, *found]


def _ranked(
    db: sqlite3.Connection, match: str, where: str, args: list[object]
) -> list[tuple[int, float]]:
    """The scope's best texts by words for an FTS5 ``match``, by BM25."""
    return db.execute(
        "SELECT d.doc, -bm25(archive_words) FROM archive_words "  # noqa: S608 - its own
        + _JOINED.format("archive_words")
        + f"{where} ORDER BY bm25(archive_words) LIMIT ?",
        (match, *args, CANDIDATES),
    ).fetchall()


def _count(db: sqlite3.Connection, table: str, match: str, where: str, args: list[object]) -> int:
    (found,) = db.execute(
        f"SELECT count(*) FROM (SELECT d.doc FROM {table} "  # noqa: S608 - its own names
        + _JOINED.format(table)
        + f"{where} LIMIT ?)",
        (match, *args, COUNTED),
    ).fetchone()
    return found


def _hit(
    db: sqlite3.Connection, doc: int, match: str, score: float | None, needles: Sequence[str]
) -> Hit:
    project, message_id, path, text = db.execute(
        "SELECT project, message_id, path, text FROM archive_docs WHERE doc = ?", (doc,)
    ).fetchone()
    row = db.execute(
        f"SELECT {_COLUMNS} FROM archive_messages "  # noqa: S608 - its own names
        "WHERE project = ? AND message_id = ?",
        (project, message_id),
    ).fetchone()
    message = _archived(row)
    found = message.entry.text if path is None else text or ""
    rounded = None if score is None else round(score, 3)
    return Hit(message, path, match, rounded, _snippet(found, needles))


def _snippet(text: str, needles: Sequence[str]) -> str:
    """About ``SNIPPET`` characters of ``text`` around the first needle it holds."""
    folded, at = text.lower(), 0
    for needle in needles:
        if (found := folded.find(needle.lower())) >= 0:
            at = found
            break
    start = max(0, min(at - SNIPPET // 4, len(text) - SNIPPET))
    part = text[start : start + SNIPPET]
    return ("\u2026" if start else "") + part + ("\u2026" if start + SNIPPET < len(text) else "")


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


def _read(
    db: sqlite3.Connection, project: str, session: str, message_id: str, tool: str, read: Read
) -> None:
    """Keep one read. A web page's continuation names its document, which the fetch that
    began it named: it counts for that page."""
    kind, source, label = read.kind, read.source, read.label
    if kind == "web" and source.startswith("document:"):
        found = db.execute(
            "SELECT source, label FROM source_reads WHERE project = ? AND session = ? AND "
            "kind = 'web' AND source NOT LIKE 'document:%' AND "
            "json_extract(detail, '$.document') = ? ORDER BY rowid DESC LIMIT 1",
            (project, session, source.removeprefix("document:")),
        ).fetchone()
        if found is not None:
            source, label = found
    db.execute(
        "INSERT INTO source_reads (project, session, message_id, tool, kind, source, label, "
        "extent, detail) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (project, session, message_id, (tool or "unknown")[:256], kind, source[:1024],
         label[:4096] if label else None, read.extent, encoded(read)),
    )  # fmt: skip


def _row(archived: Archived) -> tuple[object, ...]:
    entry = archived.entry
    encoded: dict[str, object] = {
        "refs": json.dumps(list(entry.refs)),
        "tool_calls": json.dumps(list(entry.tool_calls)) if entry.tool_calls else None,
        "files": json.dumps(entry.files) if entry.files is not None else None,
        "adapter_state": (
            json.dumps(entry.adapter_state) if entry.adapter_state is not None else None
        ),
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
    state = fields["adapter_state"]
    fields["adapter_state"] = json.loads(state) if state is not None else None
    return Archived(*row[: len(_PLACE)], entry=Entry(**fields))
