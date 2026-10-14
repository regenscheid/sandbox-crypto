# SPDX-License-Identifier: MIT
"""Immutable annotation body revisions and exact, field-local ownership.

Thread history and work requests retain their original entries. A body correction
appends a new history entry and a revision of one original create/reply entry.
Parsing and identity matching happen before taking the shared write lock.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from typing import Any, Final

from sandbox_science.orchestrator.control.texts import NewRevision, TextRevisions

from .blocks import parse
from .engine import attach, block_map
from .errors import CONFLICT, INTERNAL, NOT_FOUND, POLICY_DENIED, DocumentError
from .identity import Allocator, Mapped, carry, compare, fresh
from .models import Author

Json = dict[str, Any]
MAP_BYTES = 16 * 1024 * 1024
TEXTS: Final = TextRevisions("comment")
"""The revisions of comment bodies, each under its original entry."""


def dump(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def source_key(job: Json) -> str:
    if "response" in job:
        return job["response"]["source"]
    return "comment:" + job["comment"]["entry_id"] if "comment" in job else job["document"]


def parent(
    db: sqlite3.Connection, entry: str, document: str | None = None, project: str | None = None
) -> tuple[str, str, str]:
    """Derive scope from the immutable original, never the derived queue."""
    row = db.execute(
        "SELECT t.thread_id,d.document_id,d.project_id FROM annotation_entries e "
        "JOIN annotation_threads t ON t.thread_id=e.thread_id JOIN documents d "
        "ON d.document_id=t.document_id WHERE e.entry_id=? AND e.kind IN ('create','reply')",
        (entry,),
    ).fetchone()
    if row is None:
        raise DocumentError(NOT_FOUND, "no such original comment source")
    thread, actual_document, actual_project = row
    if (document is not None and document != actual_document) or (
        project is not None and project != actual_project
    ):
        raise DocumentError(INTERNAL, "comment source does not bind its canonical parent")
    return thread, actual_document, actual_project


def read(db: sqlite3.Connection, entry: str, revision: int | None = None) -> Json:
    """A body revision as it was written (the table checks its values then)."""
    found = TEXTS.read(db, entry, revision)
    if found is None:
        raise DocumentError(NOT_FOUND, "no such immutable comment body revision")
    return {
        "entry_id": entry,
        **{k: found[k] for k in _FIELDS},
        "history_entry": found["origin"],
    }


def _held(value: Json) -> bool:
    """Whether a body's block identity is held: its revision's held reason, which a
    block map written by ``prepare`` also carries (the column is the one read)."""
    return bool(value.get("held"))


_FIELDS: Final = (
    "revision",
    "body",
    "sha256",
    "block_map",
    "author",
    "owners",
    "next_block",
    "next_span",
    "identity_floor",
    "held",
)


def current(
    db: sqlite3.Connection, entry: str, *, full: bool = True, allow_legacy: bool = False
) -> Json:
    """Reconcile against immutable canonical history, including older writers.

    Legacy bodies without our identity history are an explicit unknown baseline.
    They remain readable/editable, but are not attributed to a guessed latest actor.
    """
    canonical_thread, _, _ = parent(db, entry)
    row = db.execute(
        "SELECT e.thread_id,e.number,e.author FROM annotation_entries e "
        "WHERE e.entry_id=? AND e.kind IN ('create','reply')",
        (entry,),
    ).fetchone()
    if row is None:
        raise DocumentError(NOT_FOUND, "name an original comment or reply entry")
    thread = row[0]
    if thread != canonical_thread:
        raise DocumentError(INTERNAL, "comment parent changed")
    original = json.loads(row[2])
    latest = db.execute(
        "SELECT entry_id,body FROM annotation_entries WHERE thread_id=? AND (entry_id=? OR "
        "(kind='edit' AND ?=1) OR (kind='edit_body' AND json_extract(payload,'$.entry')=?)) "
        "ORDER BY number DESC LIMIT 1",
        (thread, entry, row[1], entry),
    ).fetchone()
    assert latest is not None  # noqa: S101 - original is in its own lineage
    latest_entry, body = latest
    header = TEXTS.read(db, entry, full=False)
    if header is not None:
        if full:
            result = read(db, entry)
        else:
            result: Json = {
                "entry_id": entry,
                "revision": header["revision"],
                "sha256": header["sha256"],
                "author": header["author"],
                "history_entry": header["origin"],
                "body": body,
            }
            if header["held"] is not None:
                result["held"] = header["held"]
        if result["history_entry"] != latest_entry or result["sha256"] != sha(body):
            if not allow_legacy:
                raise DocumentError(
                    CONFLICT, "a legacy writer changed this body; math checks are held"
                )
            result.update(
                retained_sha256=result["sha256"],
                body=body,
                sha256=sha(body),
                block_map=None,
                owners={},
                author=None,
                history_entry=latest_entry,
                held="Legacy writer changed the body; unchanged ownership is unknown",
            )
    else:
        result = {
            "entry_id": entry,
            "revision": 0,
            "body": body,
            "sha256": sha(body),
            "block_map": None,
            "owners": {},
            "next_block": 0,
            "next_span": 0,
            "history_entry": latest_entry,
            "author": None,
        }
    return {
        **result,
        "thread": thread,
        "original_author": original,
    }


def authority(original: Author, current: Json, actor: Author) -> None:
    """Positive ordinary edit authority; a finding grants no additional rights."""
    if actor.kind == "user" and original.kind == "user" and actor.user == original.user:
        return
    if (
        actor.kind == "agent"
        and actor.role in ("main", "delegated")
        and original.kind == "agent"
        and original.role in ("main", "delegated")
        and actor.session_id == original.session_id
        and (
            actor.role == "main"
            or (
                actor.run_id == original.run_id
                and actor.role == original.role
                and current["author"] is not None
                and current["author"]["run_id"] == actor.run_id
            )
        )
        and all(
            p["author"] is None
            or (
                p["author"]["kind"] == "agent"
                and p["author"]["session_id"] == actor.session_id
                and p["author"]["role"] in ("main", "delegated")
                and (actor.role == "main" or p["author"]["run_id"] == actor.run_id)
            )
            for p in current["owners"].values()
        )
    ):
        return
    raise DocumentError(POLICY_DENIED, "this actor cannot revise that original comment body")


@dataclass(frozen=True)
class PreparedBody:
    body: str
    sha256: str
    mapped: Mapped | None
    stored: str
    owners: str
    next_block: int
    next_span: int
    base: int
    base_sha256: str | None
    held: str | None = None
    identity_floor: int = 1

    def revision(
        self,
        revision: int,
        actor: Author,
        *,
        origin: str | None = None,
        parent: str | None = None,
        parent_revision: int | None = None,
        macro_revision: int | None = None,
    ) -> NewRevision:
        """This body as text revision ``revision``, written by ``actor``."""
        return NewRevision(
            revision=revision,
            body=self.body,
            sha256=self.sha256,
            block_map=self.stored,
            author=actor.model_dump_json(),
            owners=self.owners,
            next_block=self.next_block,
            next_span=self.next_span,
            identity_floor=self.identity_floor,
            held=self.held,
            macro_revision=macro_revision,
            origin=origin,
            parent=parent,
            parent_revision=parent_revision,
        )


def prepare(body: str, actor: Author, parent: Json | None = None) -> PreparedBody:
    try:
        return _prepare(body, actor, parent)
    except DocumentError as error:
        # Annotation writes preceded this derived parser. Its stricter identity
        # bounds must not veto otherwise legal canonical Markdown or imply removal.
        revision = parent["revision"] + 1 if parent else 1
        reason = "Derived body identity is held: " + error.message[:300]
        return PreparedBody(
            body,
            sha(body),
            None,
            dump({"identity_floor": revision, "held": reason}),
            "{}",
            parent["next_block"] if parent else 0,
            parent["next_span"] if parent else 0,
            parent["revision"] if parent else 0,
            parent.get("retained_sha256", parent["sha256"]) if parent else None,
            reason,
            revision,
        )


def _prepare(body: str, actor: Author, parent: Json | None = None) -> PreparedBody:
    # Comments use the same Markdown/math identity rules as their displayed body.
    # Document macros apply at render time; front matter in a comment grants no macros.
    parsed = parse(body, documents=False)
    before = (
        attach(parent["body"], parent["block_map"], documents=False)
        if parent and parent["block_map"] and not _held(parent)
        else None
    )
    allocate = Allocator(
        parent["next_block"] if parent else 0, parent["next_span"] if parent else 0
    )
    unknown = False
    if parent and before is None:
        try:
            before = fresh(parse(parent["body"], documents=False), allocate)
        except DocumentError:
            unknown = True
    mapped = carry(before, parsed, allocate) if before else fresh(parsed, allocate)
    changes = compare(before, mapped)
    revision = parent["revision"] + 1 if parent else 1
    prior: Json = parent["owners"] if parent else {}
    owners: Json = {
        block: prior[block]
        if changes.blocks[block] in ("kept", "moved") and block in prior
        else {"revision": None, "author": None}
        if unknown or (parent and changes.blocks[block] in ("kept", "moved"))
        else {"revision": revision, "author": actor.model_dump(mode="json")}
        for block in mapped.block_ids
    }
    stored_map = block_map(mapped, changes)
    stored_map["identity_floor"] = (
        parent.get("identity_floor") or 1
        if parent and parent["block_map"] and not _held(parent)
        else revision
    )
    stored, encoded = dump(stored_map), dump(owners)
    if len(stored.encode()) > MAP_BYTES or len(encoded.encode()) > MAP_BYTES:
        raise DocumentError(INTERNAL, "comment body identity exceeds parser bounds")
    return PreparedBody(
        body,
        sha(body),
        mapped,
        stored,
        encoded,
        allocate.next_block,
        allocate.next_span,
        parent["revision"] if parent else 0,
        parent.get("retained_sha256", parent["sha256"]) if parent else None,
        identity_floor=stored_map["identity_floor"],
    )


def commit(
    db: sqlite3.Connection,
    entry: str,
    history: str,
    document: str,
    project: str,
    actor: Author,
    prepared: PreparedBody,
) -> None:
    if TEXTS.latest(db, entry) != (
        (prepared.base, prepared.base_sha256) if prepared.base else None
    ):
        raise DocumentError(CONFLICT, "the comment body changed during preparation")
    revision = prepared.base + 1
    TEXTS.append(db, entry, prepared.revision(revision, actor, origin=history))
    db.execute(
        "INSERT INTO text_math_sources(subject_type,subject_id,parent,project,target,blocked) "
        "VALUES('comment',?,?,?,?,?) ON CONFLICT(subject_type,subject_id) DO UPDATE SET "
        "target=excluded.target,blocked=excluded.blocked",
        (entry, document, project, revision, prepared.held),
    )
