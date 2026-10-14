# SPDX-License-Identifier: MIT
"""Ordinary explanation source revisions, separate from immutable decisions.

A correction changes displayed author source, never the original response action,
evidence, scientific finding CAS or reviewer audit. Only the session MAIN author
can correct an agent-authored explanation; findings grant no extra authority.
"""

from __future__ import annotations

import json
import sqlite3
from typing import TYPE_CHECKING, Any, cast

from pydantic import Field, ValidationError

from sandbox_science.contracts.model import Contract
from sandbox_science.orchestrator.documents.comment_bodies import PreparedBody, prepare
from sandbox_science.orchestrator.documents.errors import (
    CONFLICT,
    INTERNAL,
    INVALID_REQUEST,
    NOT_FOUND,
    POLICY_DENIED,
    DocumentError,
)
from sandbox_science.orchestrator.documents.models import Author
from sandbox_science.orchestrator.documents.project_macros import (
    head as macro_head,
)
from sandbox_science.orchestrator.documents.project_macros import (
    read as macros_read,
)
from sandbox_science.orchestrator.documents.project_macros import (
    response_anchors,
    response_view,
)

from .store import digest, dump, identifier, text

if TYPE_CHECKING:
    from .store import ReviewStore

Json = dict[str, Any]
BODY_BYTES = 32768
MAP_BYTES = 16 * 1024 * 1024


class ResponseEdit(Contract):
    finding: str = Field(pattern=r"^finding-[0-9a-f]{24}$")
    response_revision: int = Field(ge=1, le=999999999, strict=True)
    expected: int = Field(ge=1, le=999999999, strict=True)
    base: int = Field(ge=0, le=999999999, strict=True)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    body: str = Field(max_length=8192)
    operation_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


def source_key(finding: str, revision: int) -> str:
    identifier(finding, "finding")
    if type(revision) is not int or not 1 <= revision <= 999999999:
        raise DocumentError(INVALID_REQUEST, "invalid original response revision")
    return f"response:{finding}:r{revision}:explanation"


def original(
    db: sqlite3.Connection,
    finding: str,
    revision: int,
    *,
    project: str | None = None,
    session: str | None = None,
) -> Json:
    source = source_key(finding, revision)
    row = db.execute(
        "SELECT f.project,f.session,f.origin,t.actor,coalesce(t.reason,''),t.payload "
        "FROM review_transitions t JOIN review_findings f USING(finding_id) "
        "WHERE t.finding_id=? AND t.revision=?",
        (finding, revision),
    ).fetchone()
    if row is None:
        raise DocumentError(NOT_FOUND, "no such original finding response")
    p, s, origin, actor, body, raw = row
    try:
        payload = cast(Json, json.loads(raw))
        author = Author.model_validate(payload.get("author"))
    except (ValueError, TypeError, AttributeError):
        raise DocumentError(POLICY_DENIED, "original response author is unknown") from None
    if (project is not None and project != p) or (session is not None and session != s):
        raise DocumentError(NOT_FOUND, "response is outside this scope")
    if (
        origin != "review"
        or payload.get("action") not in ("fix", "rebut")
        or author.kind != "agent"
        or author.role != "main"
        or author.session_id != s
        or author.actor != actor
        or payload.get("explanation") != body
    ):
        raise DocumentError(POLICY_DENIED, "this prose is not an attributable MAIN response source")
    return {
        "source": source,
        "finding": finding,
        "response_revision": revision,
        "project": p,
        "session": s,
        "body": body,
        "original_author": author.model_dump(mode="json"),
    }


def read(
    db: sqlite3.Connection,
    finding: str,
    revision: int,
    body_revision: int | None = None,
    *,
    project: str | None = None,
    session: str | None = None,
    full: bool = True,
) -> Json:
    """A response body revision as it was written (the table checks its values then)."""
    parent = original(db, finding, revision, project=project, session=session)
    if not full:
        header = db.execute(
            "SELECT revision,body,sha256,author,identity_floor,held FROM review_response_bodies "
            "WHERE source=? AND finding_id=? AND decision_revision=? "
            "AND (? IS NULL OR revision=?) ORDER BY revision DESC LIMIT 1",
            (parent["source"], finding, revision, body_revision, body_revision),
        ).fetchone()
        if header is not None:
            return {
                **parent,
                "revision": header[0],
                "body": header[1],
                "sha256": header[2],
                "author": json.loads(header[3]),
                "identity_floor": header[4],
                "held": header[5] or None,
            }
    row = db.execute(
        "SELECT revision,body,sha256,block_map,author,owners,next_block,next_span,"
        "identity_floor,held FROM review_response_bodies WHERE source=? AND finding_id=? "
        "AND decision_revision=? AND (? IS NULL OR revision=?) ORDER BY revision DESC LIMIT 1",
        (parent["source"], finding, revision, body_revision, body_revision),
    ).fetchone()
    if row is None:
        if body_revision not in (None, 0):
            raise DocumentError(NOT_FOUND, "no such response body revision")
        return {
            **parent,
            "revision": 0,
            "sha256": digest(parent["body"]),
            "block_map": None,
            "owners": {},
            "author": None,
            "next_block": 0,
            "next_span": 0,
            "held": "Legacy source has no stable block identity yet",
        }
    return {
        **parent,
        "revision": row[0],
        "body": row[1],
        "sha256": row[2],
        "block_map": json.loads(row[3]),
        "owners": json.loads(row[5]),
        "author": json.loads(row[4]),
        "next_block": row[6],
        "next_span": row[7],
        "identity_floor": row[8],
        "held": row[9] or None,
    }


def projection(value: Json) -> Json:
    return {
        k: value[k]
        for k in (
            "source",
            "finding",
            "response_revision",
            "revision",
            "body",
            "sha256",
            "original_author",
            "author",
            "held",
        )
        if k in value
    }


def pin(db: sqlite3.Connection, finding: str, revision: int) -> Json | None:
    try:
        body = read(db, finding, revision, full=False)
        result = projection(body)

        result["macro_context"] = {
            **macros_read(db, body["project"]),
            "anchors": response_anchors(db, body["source"]),
        }
        return result
    except DocumentError as error:
        if error.kind in (POLICY_DENIED, NOT_FOUND):
            return None
        raise


def commit(
    db: sqlite3.Connection,
    parent: Json,
    actor: Author,
    prepared: PreparedBody,
    *,
    legacy: bool = False,
) -> None:
    current = read(db, parent["finding"], parent["response_revision"], full=False)
    if (current["revision"], current["sha256"]) != (
        prepared.base,
        prepared.base_sha256 or digest(current["body"]),
    ):
        raise DocumentError(CONFLICT, "response body changed during preparation")
    db.execute(
        "INSERT INTO review_response_bodies VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            parent["source"],
            parent["finding"],
            parent["response_revision"],
            prepared.base + 1,
            prepared.body,
            prepared.sha256,
            prepared.stored,
            actor.model_dump_json(),
            prepared.owners,
            prepared.next_block,
            prepared.next_span,
            prepared.identity_floor,
            prepared.held,
            # The project macros it was saved with; unknown for a legacy writer's body.
            None if legacy else macro_head(db, parent["project"]),
        ),
    )
    db.execute(
        "INSERT INTO "
        "response_math_sources(source,finding_id,decision_revision,project,session,target,blocked) "
        "VALUES(?,?,?,?,?,?,?) ON CONFLICT(source) DO UPDATE SET "
        "target=excluded.target,blocked=excluded.blocked",
        (
            parent["source"],
            parent["finding"],
            parent["response_revision"],
            parent["project"],
            parent["session"],
            prepared.base + 1,
            prepared.held,
        ),
    )


class ResponseBodies:
    def __init__(self, store: ReviewStore) -> None:
        self.store = store

    def get(
        self,
        project: str,
        session: str,
        finding: str,
        revision: int,
        *,
        body_revision: int | None = None,
    ) -> Json:
        self.store.scope(project, session)
        if body_revision is not None and (
            type(body_revision) is not int or not 0 <= body_revision <= 999999999
        ):
            raise DocumentError(INVALID_REQUEST, "invalid response body revision")
        if self.store.documents.control.missing:
            raise DocumentError(NOT_FOUND, "no such response source")
        with self.store.documents.control.read() as tx:
            db = tx.sql
            result = projection(
                read(
                    db,
                    finding,
                    revision,
                    body_revision,
                    project=project,
                    session=session,
                    full=False,
                )
            )

        result["macro_context"] = response_view(
            self.store.documents, project, result["source"], body_revision=body_revision
        )
        return result

    def _replay(
        self,
        db: sqlite3.Connection,
        project: str,
        session: str,
        author: Author,
        value: ResponseEdit,
        fingerprint: str,
    ) -> Json | None:
        row = db.execute(
            "SELECT fingerprint,result FROM review_operations "
            "WHERE project=? AND actor=? AND operation_id=?",
            (project, author.actor, value.operation_id),
        ).fetchone()
        if row is None:
            return None
        if row[0] != fingerprint:
            raise DocumentError(CONFLICT, "operation ID already names another finding mutation")
        try:
            retained = cast(Json, json.loads(row[1]))
            canonical = projection(
                read(
                    db,
                    value.finding,
                    value.response_revision,
                    value.base + 1,
                    project=project,
                    session=session,
                    full=False,
                )
            )
            if retained != canonical or canonical["sha256"] != digest(value.body):
                raise ValueError
        except (ValueError, TypeError, KeyError, RecursionError):
            raise DocumentError(
                INTERNAL, "response receipt does not bind its immutable body"
            ) from None
        return {**canonical, "replayed": True}

    def edit(self, project: str, session: str, raw: object, author: Author) -> Json:
        from .findings import Findings

        self.store.scope(project, session)
        try:
            value = ResponseEdit.model_validate(raw)
        except ValidationError:
            raise DocumentError(INVALID_REQUEST, "invalid response source edit") from None
        if author.kind != "agent" or author.role != "main" or author.session_id != session:
            raise DocumentError(
                POLICY_DENIED, "only this session MAIN may edit its response source"
            )
        if value.body:
            text(value.body)
        fingerprint = digest(
            dump(["response_edit", session, value.model_dump(mode="json"), author.role])
        )
        findings = Findings(self.store)
        with self.store.documents.control.read() as tx:
            db = tx.sql
            replay = self._replay(db, project, session, author, value, fingerprint)
            if replay:
                return replay
            current = read(
                db, value.finding, value.response_revision, project=project, session=session
            )
        if (current["revision"], current["sha256"]) != (value.base, value.sha256):
            raise DocumentError(CONFLICT, "response source changed since this edit was drafted")
        prepared = prepare(value.body, author, current)
        with self.store.documents.control.write() as tx:
            db = tx.sql
            replay = self._replay(db, project, session, author, value, fingerprint)
            if replay:
                return replay
            found = findings.load(db, project, session, value.finding)
            if found["revision"] != value.expected:
                raise DocumentError(CONFLICT, "finding changed since this source edit was drafted")
            commit(db, current, author, prepared)
            result = projection(read(db, value.finding, value.response_revision, full=False))
            findings.record_operation(
                db, project, author.actor, value.operation_id, fingerprint, result
            )
        return result

    def reconcile(self, project: str) -> None:
        """Bounded legacy discovery; unknown proof is held, never a queue-wide veto."""
        with self.store.documents.control.read() as tx:
            db = tx.sql
            rows = list(db.execute(legacy_query(), (project,)))
        for seq, finding, revision in rows:
            try:
                with self.store.documents.control.read() as tx:
                    db = tx.sql
                    parent = read(db, finding, revision, project=project)
                actor = Author.model_validate(parent["original_author"])
                prepared = prepare(parent["body"], actor)
            except (DocumentError, ValueError, TypeError):
                # Store only the bounded canonical transition identity. Its full
                # immutable bytes remain the audit; unknown prose is not attributed.
                with self.store.documents.control.write() as tx:
                    db = tx.sql
                    db.execute(
                        "INSERT OR IGNORE INTO review_response_discovery "
                        "SELECT rowid,finding_id,revision,? FROM review_transitions WHERE rowid=?",
                        (
                            "Legacy response author/identity is unknown; math checking held",
                            seq,
                        ),
                    )
                continue
            with self.store.documents.control.write() as tx:
                db = tx.sql
                if (
                    db.execute(
                        "SELECT 1 FROM response_math_sources WHERE source=?", (parent["source"],)
                    ).fetchone()
                    is None
                ):
                    commit(db, parent, actor, prepared, legacy=True)


def legacy_query() -> str:
    return (
        "SELECT t.rowid,t.finding_id,t.revision "
        "FROM review_transitions t "
        "JOIN review_findings f USING(finding_id) WHERE f.project=? AND f.origin='review' "
        "AND typeof(t.payload)='text' AND CASE WHEN length(CAST(t.payload AS BLOB))<=131072 "
        "THEN CASE WHEN json_valid(t.payload) THEN json_extract(t.payload,'$.action') "
        "IN ('fix','rebut') AND json_extract(t.payload,'$.author.kind')='agent' "
        "AND json_extract(t.payload,'$.author.role')='main' ELSE 0 END ELSE 0 END "
        "AND NOT EXISTS(SELECT 1 FROM response_math_sources s WHERE s.finding_id=t.finding_id "
        "AND s.decision_revision=t.revision) AND NOT EXISTS(SELECT 1 FROM "
        "review_response_discovery d WHERE d.finding_id=t.finding_id AND "
        "d.decision_revision=t.revision) "
        "ORDER BY t.rowid LIMIT 2"
    )
