# SPDX-License-Identifier: MIT
"""Bounded reads of dispatch captures scoped to one work item's attempts."""

from __future__ import annotations

import json
from datetime import UTC, datetime

from sandbox_science.contracts import Contract
from sandbox_science.orchestrator.control import ControlStore
from sandbox_science.orchestrator.documents import DocumentError

from .capture import Origin
from .inspector import attempt_scope


class CapturedCall(Contract):
    cursor: int
    identity: str
    run: str
    at: str
    kind: str
    alias: str
    captured: bool
    omitted: str | None = None


class CapturedCalls(Contract):
    calls: tuple[CapturedCall, ...]
    next_before: int | None


class ContextPart(Contract):
    ordinal: int
    sha256: str
    bytes: int
    kind: str
    role: str
    format: str
    origin: Origin | None = None
    section: str | None = None
    version: str | None = None
    native_omitted: bool = False


class ContextParts(Contract):
    parts: tuple[ContextPart, ...]
    next_after: int | None
    coverage: str = (
        "Captured adapter-input messages and schemas in dispatch order. System sections are "
        "split only after matching prompt pins; join them with two newlines. JSON messages "
        "retain their exact visible values. Provider-native replay, hidden reasoning, and "
        "provider-side transformations are not captured; per-part token counts are unavailable."
    )


class ContextText(Contract):
    text: str
    offset: int
    next_offset: int | None
    total: int
    sha256: str


class CapturedContexts:
    def __init__(self, control: ControlStore) -> None:
        self.control = control

    def calls(
        self,
        project: str,
        session: str,
        run: str,
        *,
        continuations: bool = False,
        before: int = 0,
        limit: int = 20,
    ) -> CapturedCalls:
        if before < 0 or not 1 <= limit <= 50:
            raise ValueError("invalid capture cursor")
        scope, args = attempt_scope(project, session, run, continuations)
        with self.control.isolated_read() as tx:
            rows = tx.sql.execute(
                "SELECT rowid,identity,run,created,kind,alias,EXISTS(SELECT 1 FROM context_parts p "  # noqa: S608 - fixed scope
                "WHERE p.project=m.project AND p.session=m.session "
                "AND p.run=m.run AND p.identity=m.identity), "
                "(SELECT omitted FROM context_captures d WHERE d.project=m.project "
                "AND d.session=m.session AND d.run=m.run AND d.identity=m.identity) "
                "FROM research_model_calls m WHERE project=? AND session=? AND "
                f"{scope} AND (?=0 OR rowid<?) ORDER BY rowid DESC LIMIT ?",
                (project, session, *args, before, before, limit + 1),
            ).fetchall()
        calls = tuple(
            CapturedCall(
                cursor=r[0],
                identity=r[1],
                run=r[2],
                at=datetime.fromtimestamp(r[3], UTC).isoformat(),
                kind=r[4],
                alias=r[5],
                captured=bool(r[6]),
                omitted=r[7],
            )
            for r in rows[:limit]
        )
        return CapturedCalls(
            calls=calls, next_before=calls[-1].cursor if len(rows) > limit else None
        )

    def parts(
        self,
        project: str,
        session: str,
        run: str,
        identity: str,
        *,
        after: int = 0,
        limit: int = 50,
    ) -> ContextParts:
        if after < 0 or not 1 <= limit <= 50:
            raise ValueError("invalid part cursor")
        with self.control.isolated_read() as tx:
            rows = tx.sql.execute(
                "SELECT p.ordinal,p.sha256,b.bytes,p.metadata FROM context_parts p "
                "JOIN context_bodies b ON b.project=p.project AND b.sha256=p.sha256 "
                "WHERE p.project=? AND p.session=? AND p.run=? AND p.identity=? AND p.ordinal>=? "
                "ORDER BY p.ordinal LIMIT ?",
                (project, session, run, identity, after, limit + 1),
            ).fetchall()
        items = tuple(
            ContextPart(ordinal=r[0], sha256=r[1], bytes=r[2], **json.loads(r[3]))
            for r in rows[:limit]
        )
        return ContextParts(
            parts=items, next_after=items[-1].ordinal + 1 if len(rows) > limit else None
        )

    def text(
        self,
        project: str,
        session: str,
        run: str,
        identity: str,
        ordinal: int,
        *,
        offset: int = 0,
        limit: int = 16384,
    ) -> ContextText:
        if offset < 0 or ordinal < 0 or not 1 <= limit <= 32768:
            raise ValueError("invalid captured text cursor")
        with self.control.isolated_read() as tx:
            row = tx.sql.execute(
                "SELECT substr(CAST(b.text AS BLOB),?,?),b.bytes,b.sha256 FROM context_parts p "
                "JOIN context_bodies b ON b.project=p.project AND b.sha256=p.sha256 "
                "WHERE p.project=? AND p.session=? AND p.run=? AND p.identity=? AND p.ordinal=?",
                (offset + 1, limit, project, session, run, identity, ordinal),
            ).fetchone()
        if row is None:
            raise DocumentError("not_found", "no captured part in this attempt")
        raw = bytes(row[0] or b"")
        try:
            decoded = raw.decode("utf-8")
        except UnicodeDecodeError as error:
            if error.reason != "unexpected end of data" or error.start < len(raw) - 3:
                raise DocumentError(
                    "invalid_request", "offset is not a UTF-8 character boundary"
                ) from None
            raw = raw[: error.start]
            decoded = raw.decode("utf-8")
        if not raw and offset < row[1]:
            raise DocumentError("invalid_request", "text chunk is too small for the next character")
        end = offset + len(raw)
        return ContextText(
            text=decoded,
            offset=offset,
            next_offset=end if end < row[1] else None,
            total=row[1],
            sha256=row[2],
        )
