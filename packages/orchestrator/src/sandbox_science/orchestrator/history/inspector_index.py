# SPDX-License-Identifier: MIT
"""Exact visible-text inspector search; never used by agent history tools.

Overlapping 64-KiB UTF-8 chunks cover every substring up to the API's 128-character
query bound. Legacy and oversized messages are indexed by explicit bounded maintenance,
not by a read request or a migration loading an entire historical transcript.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Literal

from sandbox_science.orchestrator.control import ControlStore

from .inspector import OriginalMessage, OriginalPage, attempt_scope

CHUNK = 65536
FIELDS: tuple[Literal["content", "tool_calls"], ...] = ("content", "tool_calls")


def _chunk(
    db: sqlite3.Connection,
    project: str,
    message: str,
    field: str,
    start: int,
    text: str,
    complete: bool,
    next_offset: int,
) -> None:
    if text:
        added = db.execute(
            "INSERT OR IGNORE INTO inspector_chunks(project,message,field,start,text) "
            "VALUES(?,?,?,?,?)",
            (project, message, field, start, text),
        )
        if added.rowcount:
            db.execute(
                "INSERT INTO inspector_trigrams(rowid,text) VALUES(?,?)",
                (added.lastrowid, text.replace("\x00", " ")),
            )
    db.execute(
        "INSERT INTO inspector_indexed VALUES(?,?,?,?,?) ON CONFLICT(project,message,field) "
        "DO UPDATE SET offset=excluded.offset,complete=excluded.complete",
        (project, message, field, next_offset, int(complete)),
    )


def append(
    db: sqlite3.Connection, project: str, message: str, content: str | None, calls: str | None
) -> None:
    """Index ordinary new appends in their existing transaction; bound extra work."""
    if len(content or "") + len(calls or "") > 262144:
        return
    for field, text in zip(FIELDS, (content or "", calls or ""), strict=True):
        raw = text.encode("utf-8")
        start = 0
        while True:
            text, step, complete = piece(raw[start : start + CHUNK + 1])
            _chunk(db, project, message, field, start, text, complete, start + step)
            if complete:
                break
            start += step


def piece(raw: bytes) -> tuple[str, int, bool]:
    """Decode one byte-bounded UTF-8 window with 127-character search overlap."""
    complete = len(raw) <= CHUNK
    # Source is valid UTF-8; a boundary can leave at most three trailing bytes.
    text = raw[:CHUNK].decode("utf-8", errors="ignore")
    step = len(text.encode("utf-8")) if complete else len(text[:-127].encode("utf-8"))
    return text, step, complete


@dataclass(frozen=True)
class IndexProgress:
    chunks: int
    pending: bool


class InspectorIndex:
    def __init__(self, control: ControlStore) -> None:
        self.control = control

    def _pending(
        self,
        db: sqlite3.Connection,
        project: str,
        session: str,
        scope: str,
        args: tuple[object, ...],
    ) -> bool:
        return (
            db.execute(
                "SELECT 1 FROM archive_messages a WHERE project=? AND session=? AND "  # noqa: S608 - fixed scope
                f"{scope} AND (NOT EXISTS(SELECT 1 FROM inspector_indexed i "
                "WHERE i.project=a.project "
                "AND i.message=a.message_id AND i.field='content' AND i.complete=1) OR "
                "NOT EXISTS(SELECT 1 FROM inspector_indexed i WHERE i.project=a.project "
                "AND i.message=a.message_id AND i.field='tool_calls' AND i.complete=1)) LIMIT 1",
                (project, session, *args),
            ).fetchone()
            is not None
        )

    def backfill(
        self, project: str, session: str, run: str, *, continuations: bool = False, chunks: int = 16
    ) -> IndexProgress:
        """Index at most 16 bounded source chunks, preserving every original byte."""
        if not 1 <= chunks <= 16:
            raise ValueError("invalid index batch")
        scope, args = attempt_scope(project, session, run, continuations)
        done = 0
        with self.control.write() as tx:
            for _ in range(chunks):
                row = tx.sql.execute(
                    "SELECT message_id FROM archive_messages a WHERE project=? AND session=? AND "  # noqa: S608 - fixed scope
                    f"{scope} AND (NOT EXISTS(SELECT 1 FROM inspector_indexed i "
                    "WHERE i.project=a.project "
                    "AND i.message=a.message_id AND i.field='content' AND i.complete=1) OR "
                    "NOT EXISTS(SELECT 1 FROM inspector_indexed i WHERE i.project=a.project "
                    "AND i.message=a.message_id AND i.field='tool_calls' AND i.complete=1)) "
                    "ORDER BY position LIMIT 1",
                    (project, session, *args),
                ).fetchone()
                if row is None:
                    break
                message = str(row[0])
                for field in FIELDS:
                    state = tx.sql.execute(
                        "SELECT offset,complete FROM inspector_indexed "
                        "WHERE project=? AND message=? AND field=?",
                        (project, message, field),
                    ).fetchone()
                    if state and state[1]:
                        continue
                    start = state[0] if state else 0
                    (text,) = tx.sql.execute(
                        f"SELECT substr(CAST(coalesce({field},'') AS BLOB),?,?) "  # noqa: S608 - fixed field
                        "FROM archive_messages "
                        "WHERE project=? AND message_id=?",
                        (start + 1, CHUNK + 1, project, message),
                    ).fetchone()
                    decoded, step, complete = piece(text or b"")
                    _chunk(tx.sql, project, message, field, start, decoded, complete, start + step)
                    done += 1
                    break
            pending = self._pending(tx.sql, project, session, scope, args)
        return IndexProgress(done, pending)

    def page(
        self,
        project: str,
        session: str,
        run: str,
        *,
        contains: str,
        after: int = 0,
        limit: int = 20,
        role: str | None = None,
        kind: str | None = None,
        include_derived: bool = False,
        continuations: bool = False,
    ) -> OriginalPage:
        if not 3 <= len(contains) <= 128 or "\x00" in contains or not 1 <= limit <= 50 or after < 0:
            raise ValueError("invalid indexed query")
        scope, args = attempt_scope(project, session, run, continuations)
        # MATCH is a candidate index only; instr enforces exact case-sensitive semantics.
        query = '"' + contains.replace('"', '""') + '"'
        with self.control.isolated_read() as tx:
            pending = self._pending(tx.sql, project, session, scope, args)
            rows = tx.sql.execute(
                "SELECT message_id,position,run,parent,created,role,kind,source,"  # noqa: S608 - fixed scope
                "c.text FROM archive_messages a "
                "JOIN inspector_chunks c ON c.project=a.project AND c.message=a.message_id "
                "JOIN inspector_trigrams f ON f.rowid=c.id "
                "WHERE inspector_trigrams MATCH ? AND instr(c.text,?)>0 "
                f"AND a.project=? AND a.session=? AND {scope} AND position>? "
                "AND (? IS NULL OR role=?) AND (? IS NULL OR kind=?) "
                "AND (?=1 OR kind NOT IN ('fold','spill','state_block')) "
                "GROUP BY a.project,a.message_id ORDER BY position LIMIT ?",
                (
                    query,
                    contains,
                    project,
                    session,
                    *args,
                    after,
                    role,
                    role,
                    kind,
                    kind,
                    int(include_derived),
                    limit + 1,
                ),
            ).fetchall()
        messages = tuple(
            OriginalMessage(
                row[0],
                row[1],
                row[2],
                row[3],
                row[4],
                row[5],
                row[6],
                row[7],
                row[8][max(0, row[8].find(contains) - 120) :][:320],
            )
            for row in rows[:limit]
        )
        return OriginalPage(
            messages,
            messages[-1].position if len(rows) > limit else None,
            indexed=True,
            index_pending=pending,
        )
