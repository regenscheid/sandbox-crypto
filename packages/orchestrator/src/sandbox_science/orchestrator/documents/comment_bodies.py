# SPDX-License-Identifier: MIT
"""Immutable annotation body revisions and exact, field-local ownership.

Thread history and work requests retain their original entries. A body correction
appends a new history entry and a revision of one original create/reply entry.
Parsing and identity matching happen before taking the shared write lock.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass
from typing import Any, cast

from .blocks import parse
from .engine import attach, block_map
from .errors import CONFLICT, INTERNAL, NOT_FOUND, POLICY_DENIED, DocumentError
from .identity import Allocator, Mapped, carry, compare, fresh
from .models import Author

Json = dict[str, Any]
MAP_BYTES = 16 * 1024 * 1024


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
        "SELECT substr(CAST(t.thread_id AS BLOB),1,129),"
        "substr(CAST(d.document_id AS BLOB),1,129),"
        "substr(CAST(d.project_id AS BLOB),1,129) FROM annotation_entries e "
        "JOIN annotation_threads t ON t.thread_id=e.thread_id JOIN documents d "
        "ON d.document_id=t.document_id WHERE e.entry_id=? AND e.kind IN ('create','reply')",
        (entry,),
    ).fetchone()
    if row is None:
        raise DocumentError(NOT_FOUND, "no such original comment source")
    if any(not isinstance(v, bytes) or not 1 <= len(v) <= 128 for v in row):
        raise DocumentError(INTERNAL, "retained comment parent exceeds its bounds")
    try:
        thread, actual_document, actual_project = (v.decode() for v in row)
    except UnicodeDecodeError:
        raise DocumentError(INTERNAL, "retained comment parent is invalid") from None
    if (document is not None and document != actual_document) or (
        project is not None and project != actual_project
    ):
        raise DocumentError(INTERNAL, "comment source does not bind its canonical parent")
    return thread, actual_document, actual_project


def read(db: sqlite3.Connection, entry: str, revision: int | None = None) -> Json:
    """Clip persisted bytes before decoding, even for a damaged control store."""
    row = db.execute(
        "SELECT revision,coalesce(substr(CAST(body AS BLOB),1,16385),X''),"
        "substr(CAST(sha256 AS BLOB),1,65),substr(CAST(block_map AS BLOB),1,?),"
        "substr(CAST(author AS BLOB),1,8193),substr(CAST(owners AS BLOB),1,?),"
        "next_block,next_span,substr(CAST(history_entry AS BLOB),1,129) "
        "FROM annotation_body_revisions WHERE entry_id=? AND (? IS NULL OR revision=?) "
        "ORDER BY revision DESC LIMIT 1",
        (MAP_BYTES + 1, MAP_BYTES + 1, entry, revision, revision),
    ).fetchone()
    if row is None:
        raise DocumentError(NOT_FOUND, "no such immutable comment body revision")
    try:
        if (
            type(row[0]) is not int
            or not 1 <= row[0] <= 999999999
            or len(row[1]) > 16384
            or len(row[2]) != 64
            or re.fullmatch(b"[0-9a-f]{64}", row[2]) is None
            or len(row[3]) > MAP_BYTES
            or len(row[4]) > 8192
            or len(row[5]) > MAP_BYTES
            or any(type(n) is not int or not 0 <= n <= 999999999 for n in row[6:8])
            or len(row[8]) > 128
            or hashlib.sha256(row[1]).hexdigest().encode() != row[2]
        ):
            raise ValueError
        raw_map: object = json.loads(row[3])
        raw_owners: object = json.loads(row[5])
        if not isinstance(raw_map, dict) or not isinstance(raw_owners, dict):
            raise ValueError
        mapped, owners = cast(Json, raw_map), cast(Json, raw_owners)
        for raw_proof in owners.values():
            if not isinstance(raw_proof, dict):
                raise ValueError
            proof = cast(Json, raw_proof)
            if set(proof) != {"revision", "author"}:
                raise ValueError
            if proof["revision"] is not None and (
                type(proof["revision"]) is not int or not 1 <= proof["revision"] <= row[0]
            ):
                raise ValueError
            if proof["author"] is not None:
                Author.model_validate(proof["author"])
        return {
            "entry_id": entry,
            "revision": row[0],
            "body": row[1].decode(),
            "sha256": row[2].decode(),
            "block_map": mapped,
            "author": Author.model_validate_json(row[4]).model_dump(mode="json"),
            "owners": owners,
            "next_block": row[6],
            "next_span": row[7],
            "history_entry": row[8].decode(),
        }
    except (ValueError, TypeError, KeyError, RecursionError):
        raise DocumentError(INTERNAL, "retained comment body exceeds its exact schema") from None


def current(
    db: sqlite3.Connection, entry: str, *, full: bool = True, allow_legacy: bool = False
) -> Json:
    """Reconcile against immutable canonical history, including older writers.

    Legacy bodies without our identity history are an explicit unknown baseline.
    They remain readable/editable, but are not attributed to a guessed latest actor.
    """
    canonical_thread, _, _ = parent(db, entry)
    row = db.execute(
        "SELECT substr(CAST(e.thread_id AS BLOB),1,129),e.number,"
        "substr(CAST(e.author AS BLOB),1,8193),"
        "coalesce(substr(CAST(e.body AS BLOB),1,16385),X'') FROM annotation_entries e "
        "WHERE e.entry_id=? AND e.kind IN ('create','reply')",
        (entry,),
    ).fetchone()
    if row is None:
        raise DocumentError(NOT_FOUND, "name an original comment or reply entry")
    if (
        re.fullmatch(b"ann-[0-9a-f]{24}", row[0]) is None
        or type(row[1]) is not int
        or not 1 <= row[1] <= 999999999
        or len(row[2]) > 8192
        or len(row[3]) > 16384
    ):
        raise DocumentError(INTERNAL, "retained original comment exceeds its bounds")
    thread = row[0].decode()
    if thread != canonical_thread:
        raise DocumentError(INTERNAL, "comment parent changed")
    try:
        original = Author.model_validate_json(row[2]).model_dump(mode="json")
    except ValueError:
        raise DocumentError(INTERNAL, "retained original comment author is invalid") from None
    latest = db.execute(
        "SELECT substr(CAST(entry_id AS BLOB),1,129),coalesce(substr(CAST(body AS "
        "BLOB),1,16385),X'') "
        "FROM annotation_entries WHERE thread_id=? AND (entry_id=? OR "
        "(kind='edit' AND ?=1) OR (kind='edit_body' AND json_extract(payload,'$.entry')=?)) "
        "ORDER BY number DESC LIMIT 1",
        (thread, entry, row[1], entry),
    ).fetchone()
    assert latest is not None  # noqa: S101 - original is in its own lineage
    if len(latest[0]) > 128 or len(latest[1]) > 16384:
        raise DocumentError(INTERNAL, "retained comment lineage exceeds its bounds")
    body = latest[1].decode()
    saved = db.execute(
        "SELECT 1 FROM annotation_body_revisions WHERE entry_id=? LIMIT 1", (entry,)
    ).fetchone()
    if saved:
        if full:
            result = read(db, entry)
        else:
            header = db.execute(
                "SELECT revision,substr(CAST(sha256 AS BLOB),1,65),"
                "substr(CAST(author AS BLOB),1,8193),"
                "substr(CAST(history_entry AS BLOB),1,129),CASE WHEN "
                "json_type(block_map,'$.held')='text' THEN substr(CAST("
                "json_extract(block_map,'$.held') AS BLOB),1,1001) ELSE NULL END "
                "FROM annotation_body_revisions "
                "WHERE entry_id=? ORDER BY revision DESC LIMIT 1",
                (entry,),
            ).fetchone()
            assert header is not None  # noqa: S101 - saved above
            if (
                type(header[0]) is not int
                or not 1 <= header[0] <= 999999999
                or re.fullmatch(b"[0-9a-f]{64}", header[1]) is None
                or len(header[2]) > 8192
                or len(header[3]) > 128
                or (header[4] is not None and len(header[4]) > 1000)
            ):
                raise DocumentError(INTERNAL, "retained body header exceeds its bounds")
            result: Json = {
                "entry_id": entry,
                "revision": header[0],
                "sha256": header[1].decode(),
                "author": Author.model_validate_json(header[2]).model_dump(mode="json"),
                "history_entry": header[3].decode(),
                "body": body,
            }
            if header[4] is not None:
                result["held"] = header[4].decode()
        if full and result["block_map"].get("held"):
            result["held"] = result["block_map"]["held"]
        if result["history_entry"] != latest[0].decode() or result["sha256"] != sha(body):
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
                history_entry=latest[0].decode(),
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
            "history_entry": latest[0].decode(),
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
        if parent and parent["block_map"] and not parent["block_map"].get("held")
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
        parent["block_map"].get("identity_floor", 1)
        if parent and parent["block_map"] and not parent["block_map"].get("held")
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
    row = db.execute(
        "SELECT revision,substr(CAST(sha256 AS BLOB),1,65) "
        "FROM annotation_body_revisions WHERE entry_id=? "
        "ORDER BY revision DESC LIMIT 1",
        (entry,),
    ).fetchone()
    if row != (
        (prepared.base, prepared.base_sha256.encode() if prepared.base_sha256 else None)
        if prepared.base
        else None
    ):
        raise DocumentError(CONFLICT, "the comment body changed during preparation")
    revision = prepared.base + 1
    db.execute(
        "INSERT INTO annotation_body_revisions VALUES(?,?,?,?,?,?,?,?,?,?)",
        (
            entry,
            revision,
            history,
            prepared.body,
            prepared.sha256,
            prepared.stored,
            actor.model_dump_json(),
            prepared.owners,
            prepared.next_block,
            prepared.next_span,
        ),
    )
    db.execute(
        "INSERT INTO comment_math_sources(entry_id,document,project,target,blocked) "
        "VALUES(?,?,?,?,?) ON CONFLICT(entry_id) DO UPDATE SET "
        "target=excluded.target,blocked=excluded.blocked",
        (entry, document, project, revision, prepared.held),
    )
