# SPDX-License-Identifier: MIT
"""Append-only document annotations and durable, scoped user work requests."""

# One logical store is split into modules; annotation transactions share its internals.
# pyright: reportPrivateUsage=false
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
from typing import Annotated, Any, Final, Literal

from pydantic import Field, TypeAdapter, ValidationError

from sandbox_science.contracts import Identifier, new_id
from sandbox_science.contracts.model import Contract
from sandbox_science.orchestrator.projects import ProjectError, ProjectStore

from .blocks import check_text
from .errors import CONFLICT, INVALID_REQUEST, NOT_FOUND, POLICY_DENIED, DocumentError
from .identity import Mapped, compare, increasing
from .models import Author
from .operations import unlay
from .store import DocumentStore

# SQLite payloads are heterogeneous JSON, validated at the mutation boundary.
Json = dict[str, Any]
ID = Annotated[str, Field(pattern=r"^b[0-9]{1,9}$")]
THREAD: Final = re.compile(r"ann-[0-9a-f]{24}")
SCHEMA: Final = (
    """CREATE TABLE IF NOT EXISTS annotation_threads (
      thread_id TEXT PRIMARY KEY, document_id TEXT NOT NULL REFERENCES documents(document_id),
      anchor TEXT NOT NULL, opener TEXT NOT NULL, created_at REAL NOT NULL) STRICT""",
    """CREATE TABLE IF NOT EXISTS annotation_entries (
      entry_id TEXT PRIMARY KEY, thread_id TEXT NOT NULL REFERENCES annotation_threads(thread_id),
      number INTEGER NOT NULL, kind TEXT NOT NULL, author TEXT NOT NULL, body TEXT NOT NULL,
      payload TEXT NOT NULL, created_at REAL NOT NULL, UNIQUE(thread_id, number)) STRICT""",
    """CREATE TABLE IF NOT EXISTS annotation_operations (
      project_id TEXT NOT NULL, actor TEXT NOT NULL, operation_id TEXT NOT NULL,
      fingerprint TEXT NOT NULL, result TEXT NOT NULL,
      PRIMARY KEY(project_id,actor,operation_id)) STRICT""",
    """CREATE TABLE IF NOT EXISTS annotation_requests (
      request_id TEXT PRIMARY KEY, project_id TEXT NOT NULL, session_id TEXT NOT NULL,
      thread_id TEXT NOT NULL REFERENCES annotation_threads(thread_id),
      entry_id TEXT NOT NULL REFERENCES annotation_entries(entry_id), state TEXT NOT NULL,
      claimed_run TEXT, claim_until REAL, delivered_run TEXT, delivered_at REAL,
      responded_entry TEXT, UNIQUE(entry_id)) STRICT""",
    (
        "CREATE INDEX IF NOT EXISTS annotation_pending ON "
        "annotation_requests(project_id,session_id,state)"
    ),
    *tuple(
        f"CREATE TRIGGER IF NOT EXISTS {table}_{verb.lower()} BEFORE {verb} ON {table} "
        "BEGIN SELECT RAISE(ABORT, 'annotations are append-only'); END"
        for table in ("annotation_threads", "annotation_entries")
        for verb in ("UPDATE", "DELETE")
    ),
)


class AnchorInput(Contract):
    revision: int = Field(ge=1, le=999999999, strict=True)
    block: ID
    math: Annotated[str, Field(pattern=r"^m[0-9]{1,9}$")] | None = None
    start: int | None = Field(default=None, ge=0, strict=True)
    end: int | None = Field(default=None, ge=1, strict=True)


class CreateComment(Contract):
    action: Literal["create"]
    anchor: AnchorInput
    body: str = Field(min_length=1, max_length=8192)
    addressed_to: Identifier | None = None
    operation_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


class ReplyComment(Contract):
    action: Literal["reply", "edit"]
    thread: str = Field(pattern=r"^ann-[0-9a-f]{24}$")
    expected: int = Field(ge=1, le=999999999, strict=True)
    body: str = Field(min_length=1, max_length=8192)
    addressed_to: Identifier | None = None
    revision: int | None = Field(default=None, ge=1, strict=True)
    request_id: str | None = Field(default=None, pattern=r"^req-[0-9a-f]{24}$")
    operation_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


class ResolveComment(Contract):
    action: Literal["resolve", "reopen"]
    thread: str = Field(pattern=r"^ann-[0-9a-f]{24}$")
    expected: int = Field(ge=1, le=999999999, strict=True)
    operation_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


CHANGE: Final = TypeAdapter(
    Annotated[CreateComment | ReplyComment | ResolveComment, Field(discriminator="action")]
)


def _thread_id(value: object, label: str) -> None:
    if not isinstance(value, str) or THREAD.fullmatch(value) is None:
        raise DocumentError(INVALID_REQUEST, f"invalid {label}")


def dump(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def own_text(mapped: Mapped, index: int) -> str:
    block = mapped.parsed.blocks[index]
    return unlay(block.text, block.continuation)


def anchor(mapped: Mapped, value: AnchorInput) -> Json:
    index = mapped.index(value.block)
    if index is None:
        raise DocumentError(INVALID_REQUEST, "the anchored revision has no such block")
    text = own_text(mapped, index)
    quote = text
    kind = "block"
    if value.math is not None:
        span = mapped.span_index(value.math)
        if span is None or mapped.parsed.spans[span].block != index:
            raise DocumentError(INVALID_REQUEST, "the formula must belong to the anchored block")
        if value.start is not None or value.end is not None:
            raise DocumentError(INVALID_REQUEST, "a math anchor cannot also name a text range")
        quote = unlay(mapped.parsed.spans[span].tex, mapped.parsed.blocks[index].continuation)
        kind = "math"
    elif value.start is not None or value.end is not None:
        if value.start is None or value.end is None or not value.start < value.end <= len(text):
            raise DocumentError(INVALID_REQUEST, "a range must lie inside the anchored block")
        if value.end - value.start > 2048:
            raise DocumentError(INVALID_REQUEST, "a range quote holds at most 2048 characters")
        quote = text[value.start : value.end]
        kind = "range"
    return {
        **value.model_dump(mode="json"),
        "kind": kind,
        "quote": quote[:2048],
        "quote_truncated": len(quote) > 2048,
        "sha256": digest(quote),
        "block_sha256": digest(text),
        "before": text[max(0, (value.start or 0) - 128) : value.start or 0],
        "after": text[value.end or len(text) : (value.end or len(text)) + 128],
    }


def attachment(original: Mapped, current: Mapped, saved: Json) -> Json:
    block = str(saved["block"])
    index = current.index(block)
    if index is None:
        return {"status": "orphaned", "quote": saved["quote"], "current": None}
    text = own_text(current, index)
    position: Json = {}
    if saved["kind"] == "math":
        span = current.span_index(str(saved["math"]))
        if span is None or current.parsed.spans[span].block != index:
            return {"status": "orphaned", "quote": saved["quote"], "current": None}
        text = unlay(current.parsed.spans[span].tex, current.parsed.blocks[index].continuation)
        old_span = original.span_index(str(saved["math"]))
        same = (
            digest(text) == saved["sha256"]
            and old_span is not None
            and original.parsed.spans[old_span].kind == current.parsed.spans[span].kind
        )
    elif saved["kind"] == "range":
        quote = str(saved["quote"])
        matches: list[int] = []
        start = text.find(quote)
        while start >= 0 and len(matches) < 2:
            matches.append(start)
            start = text.find(quote, start + 1)
        same = digest(text) == saved["block_sha256"] or len(matches) == 1
        if same:
            at = matches[0] if len(matches) == 1 else int(saved["start"])
            position = {"start": at, "end": at + len(quote)}
    else:
        same = digest(text) == saved["sha256"]
    positions = {b: i for i, b in enumerate(original.block_ids)}
    ordered = increasing(
        [(i, positions[b]) for i, b in enumerate(current.block_ids) if b in positions]
    )
    old_index = original.index(block)
    moved = block not in {current.block_ids[i] for i, _ in ordered} or (
        old_index is not None and original.parent_id(old_index) != current.parent_id(index)
    )
    return {
        "status": ("moved" if moved else "current") if same else "stale",
        "quote": saved["quote"],
        "current": text[:2048],
        **position,
    }


def scope_report(before: Mapped | None, after: Mapped, saved: Json, revision: int) -> Json:
    """Prove which changes stay inside the anchor; uncertain changes are disclosed.

    Compare immutable parent/revision source outside the anchored region. Container
    ancestors count as inside only when masking the region makes their bytes equal.
    Range replacements count as inside only when the original prefix and suffix agree.
    This is bounded linear work in two revisions, outside any write transaction.
    """
    changes = compare(before, after) if before is not None else None
    changed = (
        [b for b, state in changes.blocks.items() if state != "kept"]
        if changes
        else list(after.block_ids)
    )
    if before is None:
        return {
            "revision": revision,
            "changed_blocks": changed,
            "outside_anchor": changed,
            "anchor": {k: saved[k] for k in ("block", "math", "start", "end")},
        }
    block = str(saved["block"])
    old_index, new_index = before.index(block), after.index(block)
    region: list[tuple[int, int]] = []
    if old_index is not None and new_index is not None:
        old_block, new_block = before.parsed.blocks[old_index], after.parsed.blocks[new_index]
        if saved["kind"] == "block":
            region = [(old_block.start, old_block.end), (new_block.start, new_block.end)]
        elif saved["kind"] == "math":
            old_span, new_span = (
                before.span_index(str(saved["math"])),
                after.span_index(str(saved["math"])),
            )
            if old_span is not None and new_span is not None:
                spans = [before.parsed.spans[old_span], after.parsed.spans[new_span]]
                if all(
                    span.open_start is not None and span.close_end is not None for span in spans
                ):
                    region = [
                        (span.open_start, span.close_end)
                        for span in spans
                        if span.open_start is not None and span.close_end is not None
                    ]
        else:
            # Range offsets are in the block's unlaid text. In nested multiline
            # containers we cannot prove raw-source offsets without a provenance map.
            # Disclose those changes conservatively instead of hiding them.
            old_text, new_text = own_text(before, old_index), own_text(after, new_index)
            quote = str(saved["quote"])
            at = old_text.find(quote)
            if (
                at >= 0
                and old_text.count(quote) == 1
                and not old_block.continuation
                and not new_block.continuation
            ):
                prefix, suffix = old_text[:at], old_text[at + len(quote) :]
                if (
                    new_text.startswith(prefix)
                    and new_text.endswith(suffix)
                    and len(new_text) >= len(prefix) + len(suffix)
                ):
                    region = [
                        (old_block.start + at, old_block.start + at + len(quote)),
                        (new_block.start + len(prefix), new_block.end - len(suffix)),
                    ]
    inside: set[str] = set()
    old_indices = {b: i for i, b in enumerate(before.block_ids)}
    new_indices = {b: i for i, b in enumerate(after.block_ids)}
    if saved["kind"] == "block":
        old_allowed: set[str] = {block} if old_index is not None else set()
        new_allowed: set[str] = {block} if new_index is not None else set()
        if old_index is not None:
            old_allowed.update(before.block_ids[i] for i in before.parsed.descendants(old_index))
        if new_index is not None:
            new_allowed.update(after.block_ids[i] for i in after.parsed.descendants(new_index))
        for b in changed:
            if (
                (b in old_allowed and b in new_allowed)
                or (b in old_allowed and b not in new_indices)
                or (b in new_allowed and b not in old_indices)
            ):
                inside.add(b)
    if region and old_index is not None and new_index is not None:
        for b in changed:
            if b in inside:
                continue
            old_i, new_i = old_indices.get(b), new_indices.get(b)
            if old_i is None or new_i is None:
                continue
            masked: list[str] = []
            for mapped, i, (start, end) in zip(
                (before, after), (old_i, new_i), region, strict=True
            ):
                item = mapped.parsed.blocks[i]
                if not item.start <= start <= end <= item.end:
                    break
                masked.append(
                    mapped.parsed.text[item.start : start]
                    + "\0ANCHOR\0"
                    + mapped.parsed.text[end : item.end]
                )
            if len(masked) == 2 and masked[0] == masked[1]:
                inside.add(b)
    return {
        "revision": revision,
        "changed_blocks": changed,
        "outside_anchor": [b for b in changed if b not in inside],
        "anchor": {k: saved[k] for k in ("block", "math", "start", "end")},
    }


class Annotations:
    """Logical annotation store sharing DocumentStore's connection and transactions."""

    def __init__(self, store: DocumentStore) -> None:
        self.store = store

    def _available(self, db: sqlite3.Connection | None) -> bool:
        return (
            db is not None
            and db.execute("SELECT 1 FROM sqlite_master WHERE name='annotation_threads'").fetchone()
            is not None
        )

    def _thread(
        self, db: sqlite3.Connection, document: str, thread: str, before: int | None = None
    ) -> Json:
        row = db.execute(
            (
                "SELECT anchor,opener,created_at FROM annotation_threads WHERE "
                "document_id=? AND thread_id=?"
            ),
            (document, thread),
        ).fetchone()
        if row is None:
            raise DocumentError(NOT_FOUND, "no such document thread")
        head = db.execute(
            "SELECT max(number) FROM annotation_entries WHERE thread_id=?", (thread,)
        ).fetchone()[0]
        entries: list[Json] = []
        entry_bytes = 0
        for e in db.execute(
            "SELECT entry_id,number,kind,author,body,payload,created_at "
            "FROM annotation_entries WHERE thread_id=? AND number<? "
            "ORDER BY number DESC LIMIT 50",
            (thread, before or head + 1),
        ):
            payload = json.loads(e[5])
            for field in ("changed_blocks", "outside_anchor"):
                if field in payload:
                    payload[field + "_total"] = len(payload[field])
                    payload[field + "_truncated"] = len(payload[field]) > 100
                    payload[field] = payload[field][:100]
            entry = {
                "entry_id": e[0],
                "number": e[1],
                "kind": e[2],
                "author": json.loads(e[3]),
                "body": e[4],
                "payload": payload,
                "created_at": e[6],
            }
            size = len(dump(entry).encode())
            if entries and entry_bytes + size > 256 * 1024:
                break
            if size > 256 * 1024:
                raise DocumentError(INVALID_REQUEST, "entry exceeds the response bound")
            entries.append(entry)
            entry_bytes += size
        entries.reverse()
        decision = db.execute(
            (
                "SELECT kind FROM annotation_entries WHERE thread_id=? AND kind "
                "IN ('resolve','reopen') ORDER BY number DESC LIMIT 1"
            ),
            (thread,),
        ).fetchone()
        status = "resolved" if decision and decision[0] == "resolve" else "open"
        requests = [
            {
                "request_id": r[0],
                "session_id": r[1],
                "entry_id": r[2],
                "state": r[3],
                "delivered_run": r[4],
                "responded_entry": r[5],
            }
            for r in db.execute(
                (
                    "SELECT "
                    "request_id,session_id,entry_id,state,delivered_run,responded_entr"
                    "y FROM annotation_requests WHERE thread_id=? ORDER BY rowid DESC "
                    "LIMIT 50"
                ),
                (thread,),
            )
        ]
        return {
            "thread_id": thread,
            "anchor": json.loads(row[0]),
            "opener": json.loads(row[1]),
            "created_at": row[2],
            "revision": head,
            "entries_before": entries[0]["number"]
            if entries and entries[0]["number"] > 1
            else None,
            "status": status,
            "entries": entries,
            "requests": requests,
        }

    def list(
        self,
        project: str,
        reference: str,
        *,
        revision: int | None = None,
        after: str | None = None,
        limit: int = 10,
    ) -> Json:
        if type(limit) is not int or not 1 <= limit <= 20:
            raise DocumentError(INVALID_REQUEST, "annotation limit is 1 to 20")
        if after is not None:
            _thread_id(after, "annotation cursor")
        if revision is not None and (type(revision) is not int or not 1 <= revision <= 999999999):
            raise DocumentError(INVALID_REQUEST, "invalid document revision")
        info = self.store.document(project, reference)
        current = self.store.mapped(project, info.document_id, revision or info.head)
        with self.store._read() as db:
            if not self._available(db):
                return {"threads": [], "next_after": None}
            assert db is not None  # noqa: S101
            start = 0
            if after is not None:
                row = db.execute(
                    "SELECT rowid FROM annotation_threads WHERE document_id=? AND thread_id=?",
                    (info.document_id, after),
                ).fetchone()
                if row is None:
                    raise DocumentError(NOT_FOUND, "no such annotation cursor")
                start = row[0]
            ids = [
                r[0]
                for r in db.execute(
                    (
                        "SELECT thread_id FROM annotation_threads WHERE document_id=? AND "
                        "rowid>? ORDER BY rowid LIMIT ?"
                    ),
                    (info.document_id, start, limit + 1),
                )
            ]
        shown: list[Json] = []
        cursor = ids[limit - 1] if len(ids) > limit else None
        previous = after
        size = len(dump({"threads": [], "next_after": "ann-" + "f" * 24}).encode())
        for thread_id in ids[:limit]:
            with self.store._read() as db:
                assert db is not None  # noqa: S101
                thread = self._thread(db, info.document_id, thread_id)
            saved = thread["anchor"]
            if saved["revision"] > (revision or info.head):
                previous = thread_id
                continue
            original = (
                current
                if saved["revision"] == (revision or info.head)
                else self.store.mapped(project, info.document_id, saved["revision"])
            )
            thread["attachment"] = attachment(original, current, saved)
            thread["view_revision"] = revision or info.head
            added = len(dump(thread).encode()) + 1
            if size + added > 1024 * 1024:
                # Do not advance past the unseen thread. Its entry page is bounded,
                # so it always fits as the first result of the next page.
                cursor = previous
                break
            shown.append(thread)
            size += added
            previous = thread_id
        return {"threads": shown, "next_after": cursor}

    def read_thread(
        self, project: str, reference: str, thread: str, before: int | None = None
    ) -> Json:
        _thread_id(thread, "thread ID")
        if before is not None and (type(before) is not int or not 1 <= before <= 999999999):
            raise DocumentError(INVALID_REQUEST, "invalid entry cursor")
        info = self.store.document(project, reference)
        current = self.store.mapped(project, info.document_id, info.head)
        with self.store._read() as db:
            if not self._available(db):
                raise DocumentError(NOT_FOUND, "no such document thread")
            assert db is not None  # noqa: S101
            found = self._thread(db, info.document_id, thread, before)
        original = self.store.mapped(project, info.document_id, found["anchor"]["revision"])
        found["attachment"] = attachment(original, current, found["anchor"])
        found["view_revision"] = info.head
        return found

    def change(self, project: str, reference: str, raw: object, author: Author) -> Json:
        try:
            value = CHANGE.validate_python(raw)
        except ValidationError:
            raise DocumentError(INVALID_REQUEST, "invalid annotation change") from None
        if author.kind not in ("user", "agent"):
            raise DocumentError(POLICY_DENIED, "only users and agent runs comment")
        body = value.body if isinstance(value, (CreateComment, ReplyComment)) else ""
        check_text(body)
        if any(ord(char) < 32 and char not in "\n\r\t" for char in body):
            raise DocumentError(
                INVALID_REQUEST, "comment text contains an unsupported control character"
            )
        if len(body.encode()) > 16384:
            raise DocumentError(INVALID_REQUEST, "a comment holds at most 16 KiB of UTF-8")
        addressed = value.addressed_to if isinstance(value, (CreateComment, ReplyComment)) else None
        if addressed is not None:
            try:
                sessions = ProjectStore(self.store.path.parent).get(project).sessions
            except ProjectError:
                raise DocumentError(NOT_FOUND, "no such addressed project session") from None
            if addressed not in sessions:
                raise DocumentError(NOT_FOUND, "no such addressed project session")
        prepared: Json | None = None
        if isinstance(value, CreateComment):
            prepared = anchor(
                self.store.mapped(project, reference, value.anchor.revision), value.anchor
            )
        info = self.store.document(project, reference)
        report: Json = {}
        if isinstance(value, ReplyComment) and value.revision is not None:
            revised = self.store.revision(project, info.document_id, value.revision)
            if author.kind == "agent" and (
                revised.info.author.kind != "agent"
                or revised.info.author.run_id != author.run_id
                or revised.info.author.session_id != author.session_id
                or revised.info.author.role != author.role
            ):
                raise DocumentError(POLICY_DENIED, "name a revision this run made")
            thread = self.read_thread(project, info.document_id, value.thread)
            after = self.store.mapped(project, info.document_id, value.revision)
            before = (
                self.store.mapped(project, info.document_id, revised.info.parent)
                if revised.info.parent
                else None
            )
            report = scope_report(before, after, thread["anchor"], value.revision)
        fingerprint = digest(dump([info.document_id, value.model_dump(mode="json")]))
        with self.store._write() as db:
            replay = db.execute(
                (
                    "SELECT fingerprint,result FROM annotation_operations WHERE "
                    "project_id=? AND actor=? AND operation_id=?"
                ),
                (project, author.actor, value.operation_id),
            ).fetchone()
            if replay is not None:
                if replay[0] != fingerprint:
                    raise DocumentError(
                        CONFLICT, "operation ID already names another annotation change"
                    )
                return {**json.loads(replay[1]), "replayed": True}
            row = self.store._find(db, project, info.document_id)
            thread_id = new_id("ann") if isinstance(value, CreateComment) else value.thread
            payload: Json = {}
            if isinstance(value, CreateComment):
                assert prepared is not None  # noqa: S101
                db.execute(
                    "INSERT INTO annotation_threads VALUES(?,?,?,?,?)",
                    (
                        thread_id,
                        info.document_id,
                        dump(prepared),
                        author.model_dump_json(),
                        time.time(),
                    ),
                )
                number, kind = 1, "create"
            else:
                thread = self._thread(db, info.document_id, thread_id)
                if value.expected != thread["revision"]:
                    raise DocumentError(
                        CONFLICT,
                        "the thread changed; read it before replying",
                        {"current": thread["revision"]},
                    )
                if isinstance(value, ResolveComment) or value.action == "edit":
                    opener = Author.model_validate(thread["opener"])
                    if author.kind != "user" and (
                        opener.actor != author.actor or opener.role != author.role
                    ):
                        raise DocumentError(
                            POLICY_DENIED, "agents resolve or edit only threads they opened"
                        )
                    if value.action == "edit" and (
                        opener.actor != author.actor or opener.role != author.role
                    ):
                        raise DocumentError(POLICY_DENIED, "edit only your own opening comment")
                number, kind = thread["revision"] + 1, value.action
                payload.update(report)
            if addressed is not None:
                payload["addressed_to"] = addressed
            entry_id = new_id("cmt")
            if isinstance(value, ReplyComment) and value.request_id is not None:
                request = db.execute(
                    (
                        "SELECT session_id,thread_id FROM annotation_requests WHERE "
                        "project_id=? AND request_id=?"
                    ),
                    (project, value.request_id),
                ).fetchone()
                if (
                    author.kind != "agent"
                    or author.role != "main"
                    or request != (author.session_id, thread_id)
                ):
                    raise DocumentError(
                        POLICY_DENIED,
                        "only the addressed session's main agent answers this request",
                    )
                payload["request_id"] = value.request_id
            db.execute(
                "INSERT INTO annotation_entries VALUES(?,?,?,?,?,?,?,?)",
                (
                    entry_id,
                    thread_id,
                    number,
                    kind,
                    author.model_dump_json(),
                    body,
                    dump(payload),
                    time.time(),
                ),
            )
            request_id = None
            addressed = (
                value.addressed_to if isinstance(value, (CreateComment, ReplyComment)) else None
            )
            if addressed is not None and author.kind == "user":
                request_id = new_id("req")
                db.execute(
                    (
                        "INSERT INTO "
                        "annotation_requests(request_id,project_id,session_id,thread_id,en"
                        "try_id,state) VALUES(?,?,?,?,?,'queued')"
                    ),
                    (request_id, project, addressed, thread_id, entry_id),
                )
            if isinstance(value, ReplyComment) and value.request_id is not None:
                db.execute(
                    (
                        "UPDATE annotation_requests SET "
                        "state='responded',responded_entry=? WHERE request_id=?"
                    ),
                    (entry_id, value.request_id),
                )
            result = {
                "thread_id": thread_id,
                "entry_id": entry_id,
                "revision": number,
                "request_id": request_id,
                "outcome": kind,
            }
            self.store._event(
                db,
                "annotation.created" if number == 1 else "annotation.changed",
                row.info,
                author,
                {**result, "author": author.model_dump(mode="json"), "action": kind},
            )
            db.execute(
                "INSERT INTO annotation_operations VALUES(?,?,?,?,?)",
                (project, author.actor, value.operation_id, fingerprint, dump(result)),
            )
            return result

    def pending(
        self, project: str, session: str, run: str, *, seen: set[str], max_chars: int = 20000
    ) -> list[Json]:
        """Lease a bounded batch; acknowledge only after the enclosing run is durable."""
        if len(seen) >= 128:
            return []
        with self.store._write() as db:
            rows = db.execute(
                "SELECT r.request_id,r.thread_id,t.document_id,e.body,t.anchor,"
                "(SELECT max(number) FROM annotation_entries WHERE thread_id=t.thread_id) "
                "FROM annotation_requests r JOIN annotation_threads t USING(thread_id) "
                "JOIN annotation_entries e ON e.entry_id=r.entry_id "
                "WHERE r.project_id=? AND r.session_id=? AND delivered_run IS NULL "
                "AND state!='responded' AND (claimed_run IS NULL OR claim_until<? "
                "OR claimed_run=?) AND request_id NOT IN (SELECT value FROM json_each(?)) "
                "ORDER BY r.rowid LIMIT ?",
                (project, session, time.time(), run, dump(sorted(seen)), min(100, 128 - len(seen))),
            ).fetchall()
            values: list[Json] = []
            for request_id, thread_id, document, body, saved, head in rows:
                notice = {
                    "request_id": request_id,
                    "thread_id": thread_id,
                    "document_id": document,
                    "request_text": body,
                    "untrusted_context": {
                        "anchor": json.loads(saved),
                        "thread_revision": head,
                    },
                }
                if len(dump(notice)) + 2 > max_chars:
                    # Quoted document data can expand when JSON-escaped. Keep the
                    # complete user request and immutable, retrievable anchor identity.
                    notice["untrusted_context"]["anchor"] = {
                        k: notice["untrusted_context"]["anchor"][k]
                        for k in ("revision", "block", "math", "start", "end")
                    }
                    notice["untrusted_context"]["context_omitted"] = True
                if len(dump([*values, notice])) > max_chars:
                    break
                values.append(notice)
            for notice in values:
                db.execute(
                    "UPDATE annotation_requests SET claimed_run=?,claim_until=? WHERE request_id=?",
                    (run, time.time() + 120, notice["request_id"]),
                )
            return values

    def renew(self, project: str, session: str, run: str) -> None:
        with self.store._write() as db:
            db.execute(
                (
                    "UPDATE annotation_requests SET claim_until=? WHERE project_id=? "
                    "AND session_id=? AND claimed_run=? AND delivered_run IS NULL"
                ),
                (time.time() + 120, project, session, run),
            )

    def release(
        self, project: str, session: str, run: str, *, keep: set[str] | None = None
    ) -> None:
        with self.store._write() as db:
            db.execute(
                "UPDATE annotation_requests SET claimed_run=NULL,claim_until=NULL "
                "WHERE project_id=? AND session_id=? AND claimed_run=? "
                "AND delivered_run IS NULL AND request_id NOT IN "
                "(SELECT value FROM json_each(?))",
                (project, session, run, dump(sorted(keep or set()))),
            )

    def acknowledge(self, project: str, session: str, run: str, requests: list[str]) -> None:
        with self.store._write() as db:
            for request in requests:
                db.execute(
                    (
                        "UPDATE annotation_requests SET state=CASE WHEN state='queued' "
                        "THEN 'delivered' ELSE state "
                        "END,delivered_run=?,delivered_at=?,claimed_run=NULL,claim_until=N"
                        "ULL WHERE project_id=? AND session_id=? AND request_id=? AND "
                        "delivered_run IS NULL AND claimed_run=?"
                    ),
                    (run, time.time(), project, session, request, run),
                )
