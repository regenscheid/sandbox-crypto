# SPDX-License-Identifier: MIT
"""Bounded original-message browsing for an agent attempt's context inspector.

This reads the append-only archive, never the folded working view. Search examines
retained message content and tool-call arguments in SQLite, without loading message
bodies into the Python process. It is exact substring search, submitted explicitly by
the user; native provider payloads and separately stored tool-record bytes are not
searched.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Literal, cast

from sandbox_science.orchestrator.control import ControlStore
from sandbox_science.orchestrator.documents import DocumentStore


@dataclass(frozen=True)
class OriginalMessage:
    message: str
    position: int
    run: str
    parent: str | None
    created: str
    role: Literal["user", "assistant", "tool"]
    kind: str
    source: str | None
    snippet: str


@dataclass(frozen=True)
class OriginalPage:
    messages: tuple[OriginalMessage, ...]
    next_after: int | None
    unsearched_oversized_tool_calls: int = 0
    indexed: bool = False
    index_pending: bool = False


@dataclass(frozen=True)
class HistoryAttempt:
    run: str
    predecessor: str | None
    epoch: int | None
    outcome: str | None
    saved: bool


@dataclass(frozen=True)
class AttemptPage:
    attempts: tuple[HistoryAttempt, ...]
    next_after: str | None


def attempt_scope(
    project: str, session: str, run: str, continuations: bool
) -> tuple[str, tuple[object, ...]]:
    # Stable work identity, never the session tree: siblings and descendants have
    # distinct conversations. Legacy runs without ownership stay single-attempt.
    if not continuations:
        return "run=?", (run,)
    return (
        "(run=? OR run IN (SELECT run FROM research_attempts WHERE project=? AND session=? "
        "AND work=(SELECT work FROM research_attempts WHERE project=? AND session=? AND run=?)))",
        (run, project, session, project, session, run),
    )


class OriginalHistory:
    """Public repository read of retained original messages in one run attempt."""

    def __init__(self, store: ControlStore | DocumentStore) -> None:
        self.control = store if isinstance(store, ControlStore) else store.control

    def attempts(
        self, project: str, session: str, run: str, *, after: str = "", limit: int = 20
    ) -> AttemptPage:
        """Read immutable attempt ancestry in bounded pages, including unsaved attempts."""
        if not 1 <= limit <= 50:
            raise ValueError("invalid attempt page size")
        with self.control.isolated_read() as tx:
            owner = tx.sql.execute(
                "SELECT work FROM research_attempts WHERE project=? AND session=? AND run=?",
                (project, session, run),
            ).fetchone()
            if owner is None:
                saved = (
                    tx.sql.execute(
                        "SELECT 1 FROM runs WHERE project_id=? AND session_id=? AND name=?",
                        (project, session, run),
                    ).fetchone()
                    is not None
                )
                return AttemptPage(
                    (HistoryAttempt(run, None, None, None, saved),) if run > after else (), None
                )
            rows = tx.sql.execute(
                "SELECT run,predecessor,epoch,outcome,EXISTS(SELECT 1 FROM runs r "
                "WHERE r.project_id=a.project AND r.session_id=a.session AND r.name=a.run) "
                "FROM research_attempts a "
                "WHERE project=? AND session=? AND work=? AND run>? ORDER BY run LIMIT ?",
                (project, session, owner[0], after, limit + 1),
            ).fetchall()
        items = tuple(
            HistoryAttempt(row[0], row[1], row[2], row[3], bool(row[4])) for row in rows[:limit]
        )
        return AttemptPage(items, items[-1].run if len(rows) > limit else None)

    def page(
        self,
        project: str,
        session: str,
        run: str,
        *,
        after: int = 0,
        limit: int = 20,
        contains: str = "",
        role: Literal["user", "assistant", "tool"] | None = None,
        kind: str | None = None,
        include_derived: bool = False,
        continuations: bool = False,
    ) -> OriginalPage:
        """Page chronologically after a session position, filtering before limiting.

        The scan is SQL-side and keyset ordered. It can examine a long attempt for an
        explicit text query, but never transfers large archive bodies to the process.
        """
        if not 1 <= limit <= 50 or after < 0 or len(contains) > 128:
            raise ValueError("invalid original-history query")
        scope, scope_args = attempt_scope(project, session, run, continuations)
        # Size the next slice from SQLite's byte metadata, without materializing
        # text.  On older SQLite, blobopen supplies the same lengths on an isolated
        # connection.  An unbounded tool_calls field is excluded from text search
        # above 8 MiB and disclosed in the answer; content is schema-bound to 64 MiB.
        with self.control.isolated_read() as tx:
            # Bound the index walk before filtering agent/role/kind. Filtering a
            # sparse agent first can scan millions of other session messages just
            # to find 65 matches. Empty slices still advance a cursor.
            positions = tx.sql.execute(
                "SELECT position FROM archive_messages WHERE project=? AND session=? "
                "AND position>? ORDER BY position LIMIT 65",
                (project, session, after),
            ).fetchall()
            if not positions:
                return OriginalPage((), None)
            slice_stop = positions[min(64, len(positions)) - 1][0]
            session_more = len(positions) > 64
            prefix = (
                "SELECT position,coalesce(octet_length(content),0),"
                "coalesce(octet_length(tool_calls),0) "
                if sqlite3.sqlite_version_info >= (3, 43, 0)
                else "SELECT rowid,position,typeof(content),typeof(tool_calls) "
            )
            raw = tx.sql.execute(
                prefix + f"FROM archive_messages WHERE project=? AND session=? AND {scope} "
                "AND position>? AND position<=? AND (? IS NULL OR role=?) "
                "AND (? IS NULL OR kind=?) "
                "AND (?=1 OR kind NOT IN ('fold','spill','state_block')) "
                "ORDER BY position LIMIT 65",
                (
                    project,
                    session,
                    *scope_args,
                    after,
                    slice_stop,
                    role,
                    role,
                    kind,
                    kind,
                    int(include_derived),
                ),
            ).fetchall()
            candidates: list[tuple[int, int, int]]
            if sqlite3.sqlite_version_info >= (3, 43, 0):
                candidates = cast(list[tuple[int, int, int]], raw)
            else:
                candidates = []
                for rowid, position, content_type, calls_type in raw:
                    sizes: list[int] = []
                    for field, kind_name in (("content", content_type), ("tool_calls", calls_type)):
                        if kind_name == "null":
                            sizes.append(0)
                        else:
                            with tx.sql.blobopen(
                                "archive_messages", field, rowid, readonly=True
                            ) as blob:
                                sizes.append(len(blob))
                    candidates.append((cast(int, position), sizes[0], sizes[1]))
            if not candidates:
                return OriginalPage((), slice_stop if session_more else None)
            scanned = 0
            size = 0
            for _, content_bytes, calls_bytes in candidates[:64]:
                body_bytes = content_bytes + (calls_bytes if calls_bytes <= 8 * 1024 * 1024 else 0)
                if contains and scanned and size + body_bytes > 8 * 1024 * 1024:
                    break
                scanned += 1
                size += body_bytes
            stop = candidates[scanned - 1][0] if scanned < len(candidates) else slice_stop
            has_more = session_more or len(candidates) > scanned
            safe = tuple(
                position
                for position, _, calls_bytes in candidates[:scanned]
                if calls_bytes <= 8 * 1024 * 1024
            )
            skipped = (
                sum(calls_bytes > 8 * 1024 * 1024 for _, _, calls_bytes in candidates[:scanned])
                if contains
                else 0
            )
            safe_sql = ",".join("?" for _ in safe) or "NULL"
            rows = tx.sql.execute(
                "SELECT message_id, position, run, parent, created, role, kind, source, "  # noqa: S608
                "CASE WHEN ? != '' AND instr(coalesce(content,''), ?) > 0 "
                "THEN substr(content, max(1, instr(content, ?) - 80), 320) "
                f"WHEN ? != '' AND CASE WHEN position IN ({safe_sql}) "
                "THEN instr(coalesce(tool_calls,''), ?) > 0 ELSE 0 END "
                "THEN substr(tool_calls, max(1, instr(tool_calls, ?) - 80), 320) "
                "ELSE substr(coalesce(content, tool_calls, ''), 1, 320) END "
                f"FROM archive_messages WHERE project=? AND session=? AND {scope} "
                "AND position>? AND position<=? AND (?='' OR instr(coalesce(content,''),?)>0 "
                f"OR CASE WHEN position IN ({safe_sql}) "
                "THEN instr(coalesce(tool_calls,''),?)>0 ELSE 0 END) "
                "AND (? IS NULL OR role=?) AND (? IS NULL OR kind=?) "
                "AND (?=1 OR kind NOT IN ('fold','spill','state_block')) "
                "ORDER BY position LIMIT ?",
                (
                    contains,
                    contains,
                    contains,
                    contains,
                    *safe,
                    contains,
                    contains,
                    project,
                    session,
                    *scope_args,
                    after,
                    stop,
                    contains,
                    contains,
                    *safe,
                    contains,
                    role,
                    role,
                    kind,
                    kind,
                    int(include_derived),
                    limit + 1,
                ),
            ).fetchall()
        page = tuple(OriginalMessage(*row) for row in rows[:limit])
        if len(rows) > limit:
            counted = (
                sum(
                    calls_bytes > 8 * 1024 * 1024
                    for position, _, calls_bytes in candidates[:scanned]
                    if position <= page[-1].position
                )
                if contains
                else 0
            )
            return OriginalPage(page, page[-1].position, counted)
        return OriginalPage(page, stop if has_more else None, skipped)
