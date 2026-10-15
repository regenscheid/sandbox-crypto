# SPDX-License-Identifier: MIT
"""Append-only document annotations and durable, scoped user work requests."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Annotated, Any, Final, Literal

from pydantic import Field, TypeAdapter, ValidationError

from sandbox_science.contracts import Identifier, new_id
from sandbox_science.contracts.model import Contract
from sandbox_science.orchestrator.control import Receipts
from sandbox_science.orchestrator.control.notices import LeasedNotices, NoticeTable
from sandbox_science.orchestrator.projects import ProjectError, recorded

from . import comment_bodies
from .blocks import check_text
from .errors import CONFLICT, INVALID_REQUEST, NOT_FOUND, POLICY_DENIED, DocumentError
from .identity import Mapped, compare, increasing
from .models import Author, DocumentInfo
from .operations import unlay
from .store import DocumentStore

# SQLite payloads are heterogeneous JSON, validated at the mutation boundary.
Json = dict[str, Any]
ID = Annotated[str, Field(pattern=r"^b[0-9]{1,9}$")]
THREAD: Final = re.compile(r"ann-[0-9a-f]{24}")
_RECEIPTS: Final = Receipts("comment", "operation ID already names another annotation change")
REQUESTS: Final = NoticeTable(
    "annotation_requests",
    "project_id",
    "session_id",
    "{0}request_id",
    ("request_id",),
    ("request_id",),
)
"""Where the user's work requests are leased to the addressed session's runs."""


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


class EditBody(Contract):
    action: Literal["edit_body"]
    thread: str = Field(pattern=r"^ann-[0-9a-f]{24}$")
    expected: int = Field(ge=1, le=999999999, strict=True)
    entry: str = Field(pattern=r"^cmt-[0-9a-f]{24}$")
    base: int = Field(ge=0, le=999999999, strict=True)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    body: str = Field(max_length=8192)
    operation_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


CHANGE: Final = TypeAdapter(
    Annotated[
        CreateComment | ReplyComment | ResolveComment | EditBody, Field(discriminator="action")
    ]
)


def _body(value: object) -> str:
    """A change's comment text: empty for a resolve or reopen."""
    return value.body if isinstance(value, (CreateComment, ReplyComment, EditBody)) else ""


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


@dataclass(frozen=True, slots=True)
class _Prepared:
    """What a comment change works out before it takes the write lock."""

    document: str
    fingerprint: str
    body: str
    addressed: str | None
    anchor: Json | None = None
    """A new thread's anchor."""
    body_entry: str | None = None
    """The comment whose body an edit changes."""
    body_parent: Json | None = None
    """That body as the edit read it."""
    body_change: comment_bodies.PreparedBody | None = None
    report: Json | None = None
    """A reply's scope report on the revision it names."""


Change = CreateComment | ReplyComment | ResolveComment | EditBody


class Annotations:
    """Logical annotation store sharing DocumentStore's connection and transactions."""

    def __init__(self, store: DocumentStore, *, clock: Callable[[], float] = time.time) -> None:
        self.store, self.clock = store, clock
        self.leases = LeasedNotices(store.control, REQUESTS, clock)

    @staticmethod
    def _replay(
        db: sqlite3.Connection, project: str, author: Author, operation: str, fingerprint: str
    ) -> Json | None:
        """The first result of a change made under ``operation``, if it was made."""
        found = _RECEIPTS.find(db, project, author.actor, operation, fingerprint)
        return None if found is None else {**found, "replayed": True}

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
            if e[2] in ("create", "reply"):
                try:
                    source = comment_bodies.current(db, e[0], full=False, allow_legacy=True)
                    entry["body_source"] = {
                        k: source[k]
                        for k in (
                            "entry_id",
                            "revision",
                            "sha256",
                            "author",
                            "original_author",
                            "body",
                        )
                    }
                    if "held" in source:
                        entry["body_source"]["held"] = source["held"]
                except DocumentError as error:
                    entry["body_source"] = {"entry_id": e[0], "held": error.message}
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
        with self.store.control.read() as tx:
            db = tx.sql
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
            with self.store.control.read() as tx:
                db = tx.sql
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
        with self.store.control.read() as tx:
            db = tx.sql
            found = self._thread(db, info.document_id, thread, before)
        original = self.store.mapped(project, info.document_id, found["anchor"]["revision"])
        found["attachment"] = attachment(original, current, found["anchor"])
        found["view_revision"] = info.head
        return found

    def body(self, project: str, reference: str, entry: object) -> Json:
        if not isinstance(entry, str) or re.fullmatch(r"cmt-[0-9a-f]{24}", entry) is None:
            raise DocumentError(INVALID_REQUEST, "invalid original comment entry")
        info = self.store.document(project, reference)
        with self.store.control.read() as tx:
            db = tx.sql
            if (
                db.execute(
                    "SELECT 1 FROM annotation_entries e JOIN annotation_threads t USING(thread_id) "
                    "WHERE t.document_id=? AND e.entry_id=?",
                    (info.document_id, entry),
                ).fetchone()
                is None
            ):
                raise DocumentError(NOT_FOUND, "no such scoped comment body")
            value = comment_bodies.current(db, entry, full=False, allow_legacy=True)
            value["expected"] = db.execute(
                "SELECT max(number) FROM annotation_entries WHERE thread_id=?",
                (value["thread"],),
            ).fetchone()[0]
        return value

    def change(self, project: str, reference: str, raw: object, author: Author) -> Json:
        """Create, reply to, edit, resolve or reopen a comment, in four steps: check the
        request, work out the change outside the write lock, then (unless the operation
        ID was used already) apply it and record its event and receipt in one change."""
        value = self._parse(raw, author)
        prepared = self._prepare(project, reference, value, author)
        if isinstance(prepared, dict):
            return prepared  # the operation ID was used already
        with self.store.control.write() as tx:
            db = tx.sql
            replay = self._replay(db, project, author, value.operation_id, prepared.fingerprint)
            if replay is not None:
                return replay
            return self._apply(db, project, value, author, prepared)

    def _parse(self, raw: object, author: Author) -> Change:
        """The change, with its author and body checked."""
        try:
            value = CHANGE.validate_python(raw)
        except ValidationError:
            raise DocumentError(INVALID_REQUEST, "invalid annotation change") from None
        if author.kind not in ("user", "agent"):
            raise DocumentError(POLICY_DENIED, "only users and agent runs comment")
        if len(author.model_dump_json().encode()) > 8192:
            raise DocumentError(INVALID_REQUEST, "comment author provenance exceeds its bound")
        body = _body(value)
        check_text(body)
        if any(ord(char) < 32 and char not in "\n\r\t" for char in body):
            raise DocumentError(
                INVALID_REQUEST, "comment text contains an unsupported control character"
            )
        if len(body.encode()) > 16384:
            raise DocumentError(INVALID_REQUEST, "a comment holds at most 16 KiB of UTF-8")
        return value

    def _prepare(
        self, project: str, reference: str, value: Change, author: Author
    ) -> _Prepared | Json:
        """Work out the change outside the write lock: the addressed session, a new
        thread's anchor, an edited body's parent and new revision, and a reply's scope
        report. A change already made under its operation ID returns its receipt."""
        addressed = value.addressed_to if isinstance(value, (CreateComment, ReplyComment)) else None
        if addressed is not None:
            try:
                sessions = recorded(self.store.path.parent, project).sessions
            except ProjectError:
                raise DocumentError(NOT_FOUND, "no such addressed project session") from None
            if addressed not in sessions:
                raise DocumentError(NOT_FOUND, "no such addressed project session")
        anchored = (
            anchor(self.store.mapped(project, reference, value.anchor.revision), value.anchor)
            if isinstance(value, CreateComment)
            else None
        )
        info = self.store.document(project, reference)
        fingerprint = digest(dump([info.document_id, value.model_dump(mode="json")]))
        with self.store.control.read() as tx:
            replay = self._replay(tx.sql, project, author, value.operation_id, fingerprint)
            if replay is not None:
                return replay
        body_entry, body_parent = (
            self._body_parent(info.document_id, value, author)
            if isinstance(value, EditBody)
            or (isinstance(value, ReplyComment) and value.action == "edit")
            else (None, None)
        )
        body = _body(value)
        return _Prepared(
            document=info.document_id,
            fingerprint=fingerprint,
            body=body,
            addressed=addressed,
            anchor=anchored,
            body_entry=body_entry,
            body_parent=body_parent,
            body_change=comment_bodies.prepare(body, author, body_parent)
            if isinstance(value, (CreateComment, EditBody, ReplyComment))
            else None,
            report=self._scope_report(project, info.document_id, value, author)
            if isinstance(value, ReplyComment) and value.revision is not None
            else None,
        )

    def _body_parent(
        self, document: str, value: ReplyComment | EditBody, author: Author
    ) -> tuple[str, Json]:
        """The comment whose body an edit changes, and that body, which the edit's
        base must match and its author may edit."""
        with self.store.control.read() as tx:
            db = tx.sql
            if (
                db.execute(
                    "SELECT 1 FROM annotation_threads WHERE document_id=? AND thread_id=?",
                    (document, value.thread),
                ).fetchone()
                is None
            ):
                raise DocumentError(NOT_FOUND, "no such scoped comment thread")
            if isinstance(value, EditBody):
                entry = value.entry
            else:
                original = db.execute(
                    "SELECT entry_id FROM annotation_entries WHERE thread_id=? AND number=1",
                    (value.thread,),
                ).fetchone()
                if original is None:
                    raise DocumentError(NOT_FOUND, "no such original comment body")
                entry = original[0]
            parent = comment_bodies.current(db, entry, allow_legacy=True)
        if parent["thread"] != value.thread:
            raise DocumentError(NOT_FOUND, "the body belongs to another thread")
        if isinstance(value, EditBody) and (value.base, value.sha256) != (
            parent["revision"],
            parent["sha256"],
        ):
            raise DocumentError(CONFLICT, "the comment body changed; retain and review your draft")
        comment_bodies.authority(Author.model_validate(parent["original_author"]), parent, author)
        return entry, parent

    def _scope_report(
        self, project: str, document: str, value: ReplyComment, author: Author
    ) -> Json:
        """How the revision a reply names changed the thread's anchor; an agent names
        only a revision its own run made."""
        assert value.revision is not None  # noqa: S101 - the caller checked
        revised = self.store.revision(project, document, value.revision)
        if author.kind == "agent" and (
            revised.info.author.kind != "agent"
            or revised.info.author.run_id != author.run_id
            or revised.info.author.session_id != author.session_id
            or revised.info.author.role != author.role
        ):
            raise DocumentError(POLICY_DENIED, "name a revision this run made")
        thread = self.read_thread(project, document, value.thread)
        after = self.store.mapped(project, document, value.revision)
        before = (
            self.store.mapped(project, document, revised.info.parent)
            if revised.info.parent
            else None
        )
        return scope_report(before, after, thread["anchor"], value.revision)

    def _apply(
        self,
        db: sqlite3.Connection,
        project: str,
        value: Change,
        author: Author,
        prepared: _Prepared,
    ) -> Json:
        """The change in the open write: the thread, the entry, the body revision and
        the request, then its event and receipt."""
        found = self.store.find(db, project, prepared.document)
        payload: Json = {}
        if isinstance(value, CreateComment):
            thread_id = new_id("ann")
            assert prepared.anchor is not None  # noqa: S101 - prepared for a new thread
            db.execute(
                "INSERT INTO annotation_threads VALUES(?,?,?,?,?)",
                (
                    thread_id,
                    prepared.document,
                    dump(prepared.anchor),
                    author.model_dump_json(),
                    self.clock(),
                ),
            )
            number, kind = 1, "create"
        else:
            thread_id = value.thread
            number, kind = self._continue(db, prepared.document, value, author)
            payload.update(prepared.report or {})
            if isinstance(value, EditBody):
                payload.update(
                    entry=value.entry,
                    base=value.base,
                    sha256=value.sha256,
                    body_revision=value.base + 1,
                )
        if prepared.addressed is not None:
            payload["addressed_to"] = prepared.addressed
        entry_id = new_id("cmt")
        answered = value.request_id if isinstance(value, ReplyComment) else None
        if answered is not None:
            self._check_answer(db, project, answered, thread_id, author)
            payload["request_id"] = answered
        if prepared.body_parent is not None:
            latest = comment_bodies.current(
                db, prepared.body_entry or "", full=False, allow_legacy=True
            )
            if (latest["revision"], latest["sha256"]) != (
                prepared.body_parent["revision"],
                prepared.body_parent["sha256"],
            ):
                raise DocumentError(CONFLICT, "the comment body changed during preparation")
        db.execute(
            "INSERT INTO annotation_entries VALUES(?,?,?,?,?,?,?,?)",
            (
                entry_id,
                thread_id,
                number,
                kind,
                author.model_dump_json(),
                prepared.body,
                dump(payload),
                self.clock(),
            ),
        )
        if prepared.body_change is not None:
            comment_bodies.commit(
                db,
                prepared.body_entry or entry_id,
                entry_id,
                prepared.document,
                project,
                author,
                prepared.body_change,
            )
        request_id = None
        if prepared.addressed is not None and author.kind == "user":
            request_id = new_id("req")
            db.execute(
                "INSERT INTO annotation_requests(request_id,project_id,session_id,thread_id,"
                "entry_id,state) VALUES(?,?,?,?,?,'queued')",
                (request_id, project, prepared.addressed, thread_id, entry_id),
            )
        if answered is not None:
            db.execute(
                "UPDATE annotation_requests SET state='responded',responded_entry=? "
                "WHERE request_id=?",
                (entry_id, answered),
            )
        result = {
            "thread_id": thread_id,
            "entry_id": entry_id,
            "revision": number,
            "request_id": request_id,
            "outcome": kind,
        }
        self._record(db, project, found, author, value.operation_id, prepared.fingerprint, result)
        return result

    def _continue(
        self,
        db: sqlite3.Connection,
        document: str,
        value: ReplyComment | ResolveComment | EditBody,
        author: Author,
    ) -> tuple[int, str]:
        """The next entry's number and kind in an existing thread, whose revision the
        change must name; only its opener (or the user) resolves it, and only its
        opener edits the opening comment."""
        thread = self._thread(db, document, value.thread)
        if value.expected != thread["revision"]:
            raise DocumentError(
                CONFLICT,
                "the thread changed; read it before replying",
                {"current": thread["revision"]},
            )
        if isinstance(value, ResolveComment) or value.action == "edit":
            opener = Author.model_validate(thread["opener"])
            own = opener.actor == author.actor and opener.role == author.role
            if author.kind != "user" and not own:
                raise DocumentError(
                    POLICY_DENIED, "agents resolve or edit only threads they opened"
                )
            if value.action == "edit" and not own:
                raise DocumentError(POLICY_DENIED, "edit only your own opening comment")
        return thread["revision"] + 1, value.action

    @staticmethod
    def _check_answer(
        db: sqlite3.Connection, project: str, request: str, thread: str, author: Author
    ) -> None:
        """Only the addressed session's main agent answers a request, in its thread."""
        found = db.execute(
            "SELECT session_id,thread_id FROM annotation_requests WHERE "
            "project_id=? AND request_id=?",
            (project, request),
        ).fetchone()
        if author.kind != "agent" or author.role != "main" or found != (author.session_id, thread):
            raise DocumentError(
                POLICY_DENIED, "only the addressed session's main agent answers this request"
            )

    def _record(
        self,
        db: sqlite3.Connection,
        project: str,
        found: DocumentInfo,
        author: Author,
        operation: str,
        fingerprint: str,
        result: Json,
    ) -> None:
        """The change's event and the receipt its operation ID replays."""
        self.store.record_event(
            db,
            "annotation.created" if result["revision"] == 1 else "annotation.changed",
            found,
            author,
            {**result, "author": author.model_dump(mode="json"), "action": result["outcome"]},
        )
        _RECEIPTS.record(db, project, author.actor, operation, fingerprint, result)

    def pending(
        self, project: str, session: str, run: str, *, seen: set[str], max_chars: int = 20000
    ) -> list[Json]:
        """Lease a bounded batch; acknowledge only after the enclosing run is durable."""

        def notice(row: tuple[Any, ...]) -> Json:
            request_id, thread_id, document, body, saved, head = row
            found: Json = {
                "request_id": request_id,
                "thread_id": thread_id,
                "document_id": document,
                "request_text": body,
                "untrusted_context": {"anchor": json.loads(saved), "thread_revision": head},
            }
            if len(dump(found)) + 2 > max_chars:
                # Quoted document data can expand when JSON-escaped. Keep the
                # complete user request and immutable, retrievable anchor identity.
                found["untrusted_context"]["anchor"] = {
                    k: found["untrusted_context"]["anchor"][k]
                    for k in ("revision", "block", "math", "start", "end")
                }
                found["untrusted_context"]["context_omitted"] = True
            return found

        return self.leases.offer(
            run,
            seen,
            batch=100,
            max_chars=max_chars,
            select=(
                "SELECT n.request_id,n.thread_id,t.document_id,e.body,t.anchor,"
                "(SELECT max(number) FROM annotation_entries WHERE thread_id=t.thread_id) "
                "FROM annotation_requests n JOIN annotation_threads t USING(thread_id) "
                "JOIN annotation_entries e ON e.entry_id=n.entry_id "
                "WHERE n.project_id=? AND n.session_id=? AND n.state!='responded' "
                "AND {leased} ORDER BY n.rowid LIMIT ?"
            ),
            parameters=(project, session),
            project=notice,
        )

    def waiting(self, scope: tuple[str, str] | None = None) -> list[tuple[str, str, str]]:
        """The requests that wait for a run, oldest first, as (project, session, request
        ID), in every session or in ``scope``'s alone: not answered, delivered by no saved
        run, and held by no run. A waiting request starts a run for its session, as a
        queued message does, and ends a job wait (run-service spec RS-6, RS-7)."""
        where, parameters = "", tuple[str, ...]()
        if scope is not None:
            where, parameters = "AND project_id=? AND session_id=? ", scope
        with self.store.control.read() as tx:
            rows = tx.sql.execute(
                "SELECT project_id,session_id,request_id FROM annotation_requests "  # noqa: S608
                "WHERE state!='responded' AND delivered_run IS NULL "
                f"AND (claimed_run IS NULL OR claim_until<=?) {where}ORDER BY rowid",
                (self.clock(), *parameters),
            ).fetchall()
        return [(project, session, request) for project, session, request in rows]

    def claimed(self, project: str, session: str, run: str) -> list[str]:
        """The requests ``run`` was offered and no saved run delivered, its lease expired
        or not: those of a run that died (RS-12)."""
        with self.store.control.read() as tx:
            rows = tx.sql.execute(
                "SELECT request_id FROM annotation_requests WHERE project_id=? AND "
                "session_id=? AND claimed_run=? AND delivered_run IS NULL "
                "AND state!='responded' ORDER BY rowid",
                (project, session, run),
            ).fetchall()
        return [request for (request,) in rows]

    def renew(self, project: str, session: str, run: str) -> None:
        self.leases.renew(project, session, run)

    def release(
        self, project: str, session: str, run: str, *, keep: set[str] | None = None
    ) -> None:
        self.leases.release(project, session, run, keep)

    def acknowledge(self, project: str, session: str, run: str, requests: list[str]) -> None:
        with self.store.control.write() as tx:
            self.leases.acknowledge(
                tx.sql,
                project,
                session,
                run,
                requests,
                also="state=CASE WHEN state='queued' THEN 'delivered' ELSE state END,"
                "delivered_at=?,",
                parameters=(self.clock(),),
            )
