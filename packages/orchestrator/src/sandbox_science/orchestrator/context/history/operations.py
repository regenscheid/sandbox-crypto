# SPDX-License-Identifier: MIT
"""Durable tool evidence, independent of the final run record (RC-10, RC-14).

An intent records a planned toolbox call, not policy approval or proof of execution.
Without an archived result its outcome is uncertain: never replay it merely because
it is here.
The operation ID is the one passed to the toolbox; individual adapters may derive
further operation IDs. Reconciliation must understand that adapter's contract.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC
from typing import Any, Final, cast

from sandbox_science.contracts import SYSTEM_CLOCK, Clock, ExecutionContext
from sandbox_science.contracts.model import MAX_COUNTER
from sandbox_science.orchestrator.control import ControlError, ControlStore
from sandbox_science.orchestrator.control.errors import (
    CONFLICT,
    INTERNAL,
    INVALID_REQUEST,
    NOT_FOUND,
)
from sandbox_science.orchestrator.model import ToolCall

READ_BYTES: Final = 1024 * 1024
"""Default byte ceiling for one full intent or result read, checked before loading it."""
PAGE: Final = 100
CHUNK_BYTES: Final = 65536
"""Largest payload page, independent of the size of the original record."""


@dataclass(frozen=True)
class RecordChunk:
    """A byte range of immutable UTF-8 JSON; decode only after joining the bytes."""

    offset: int
    total: int
    data: bytes

    @property
    def next_offset(self) -> int | None:
        end = self.offset + len(self.data)
        return end if end < self.total else None


@dataclass(frozen=True)
class ToolRecord:
    record: dict[str, Any]
    operation_id: str | None = None
    """None for a report round's calls, which were refused without dispatch."""


@dataclass(frozen=True)
class PendingTool:
    seq: int
    operation_id: str
    """Stable identity for retrieval, not permission to replay."""


@dataclass(frozen=True)
class RecordRef:
    """Metadata only; null byte size identifies an older message without a record."""

    message_id: str
    position: int
    operation_id: str | None
    record_bytes: int | None


@dataclass(frozen=True)
class RecordPage:
    records: tuple[RecordRef, ...]
    next_after: int
    has_more: bool


class ToolJournal:
    def __init__(self, control: ControlStore, *, clock: Clock = SYSTEM_CLOCK) -> None:
        self.control, self.clock = control, clock

    def start(self, context: ExecutionContext, run: str, operation_id: str, call: ToolCall) -> None:
        """Save before dispatch. Even an identical existing intent is refused: its
        outcome must be reconciled before another dispatch can be considered."""
        fields = (
            call.id,
            call.name,
            call.arguments,
            context.model_dump_json(),
            self.clock.now().astimezone(UTC).isoformat(),
        )
        with self.control.write() as tx:
            key = (context.project_id, context.session_id, run, operation_id)
            if tx.sql.execute(
                "SELECT 1 FROM tool_intents WHERE project=? AND session=? AND run=? "
                "AND operation_id=?",
                key,
            ).fetchone():
                raise ControlError(CONFLICT, "tool intent already exists; reconcile its outcome")
            try:
                tx.sql.execute(
                    "INSERT INTO tool_intents (project,session,run,operation_id,call_id,tool,"
                    "arguments,context,created,body_bytes) VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (*key, *fields, sum(len(value.encode()) for value in fields)),
                )
            except sqlite3.IntegrityError as error:
                raise ControlError(INVALID_REQUEST, f"tool intent refused: {error}") from None

    def pending(
        self, project: str, session: str, run: str, *, after: int = 0, limit: int = PAGE
    ) -> list[PendingTool]:
        """A bounded page of identities with no recorded result. No argument/output
        payloads are loaded; continue after the last seq even if new results arrive."""
        with self.control.read() as tx:
            rows = tx.sql.execute(
                "SELECT i.seq,i.operation_id FROM tool_intents i WHERE i.project=? "
                "AND i.session=? AND i.run=? AND i.seq>? AND NOT EXISTS "
                "(SELECT 1 FROM archive_tool_records r WHERE r.project=i.project "
                "AND r.session=i.session AND r.run=i.run AND r.operation_id=i.operation_id) "
                "ORDER BY i.seq LIMIT ?",
                (project, session, run, after, max(1, min(limit, PAGE))),
            ).fetchall()
        return [PendingTool(*row) for row in rows]

    def intent(
        self,
        project: str,
        session: str,
        run: str,
        operation_id: str,
        *,
        max_bytes: int = READ_BYTES,
    ) -> dict[str, Any]:
        """One original intent, including unparsed arguments and execution context."""
        key = (project, session, run, operation_id)
        with self.control.read() as tx:
            size = tx.sql.execute(
                "SELECT body_bytes FROM tool_intents WHERE "
                "project=? AND session=? AND run=? AND operation_id=?",
                key,
            ).fetchone()
            _size(size, max_bytes, "tool intent")
            row = tx.sql.execute(
                "SELECT call_id,tool,arguments,context,created FROM tool_intents WHERE "
                "project=? AND session=? AND run=? AND operation_id=?",
                key,
            ).fetchone()
        return dict(
            zip(
                ("call_id", "tool", "arguments", "context", "created"),
                (*row[:3], json.loads(row[3]), row[4]),
                strict=True,
            )
        )

    def record(
        self, project: str, message_id: str, *, max_bytes: int = READ_BYTES
    ) -> dict[str, Any]:
        """The full tool record for one archived message, scoped to its project.
        Older messages may have no journal record; nothing is fabricated for them."""
        with self.control.read() as tx:
            key = (project, message_id)
            size = tx.sql.execute(
                "SELECT record_bytes FROM archive_tool_records WHERE project=? AND message_id=?",
                key,
            ).fetchone()
            _size(size, max_bytes, "tool record")
            (text,) = tx.sql.execute(
                "SELECT record FROM archive_tool_records WHERE project=? AND message_id=?",
                key,
            ).fetchone()
        return cast(dict[str, Any], json.loads(text))

    def record_chunk(
        self, project: str, message_id: str, *, offset: int = 0, limit: int = CHUNK_BYTES
    ) -> RecordChunk:
        """A bounded byte page, even for records too large for ``record``. Resolve the
        row in the project scope; keep its read transaction and blob handle local to
        this one call, never across network waits or consumer cancellation."""
        if type(offset) is not int or not 0 <= offset <= MAX_COUNTER:
            raise ControlError(INVALID_REQUEST, "invalid tool record byte offset")
        if type(limit) is not int or not 1 <= limit <= CHUNK_BYTES:
            raise ControlError(INVALID_REQUEST, f"tool record limit must be 1 to {CHUNK_BYTES}")
        with self.control.isolated_read() as tx:
            row = tx.sql.execute(
                "SELECT rowid,record_bytes FROM archive_tool_records WHERE project=? "
                "AND message_id=?",
                (project, message_id),
            ).fetchone()
            if row is None:
                raise ControlError(NOT_FOUND, "no tool record in this scope")
            row_id, total = row
            if offset > total:
                raise ControlError(INVALID_REQUEST, "tool record offset exceeds its byte size")
            # SELECT/substr/cast of the TEXT can materialize it inside SQLite. Its
            # incremental byte interface reads only this range, including split UTF-8.
            with tx.sql.blobopen("archive_tool_records", "record", row_id, readonly=True) as blob:
                if len(blob) != total:
                    raise ControlError(INTERNAL, "tool record byte size differs from its metadata")
                blob.seek(offset)
                data = blob.read(min(limit, total - offset))
        return RecordChunk(offset, total, data)

    def records(
        self, project: str, session: str, run: str, *, after: int = 0, limit: int = PAGE
    ) -> RecordPage:
        """Discover evidence without reading transcript or result bodies. The cursor
        uses immutable session positions so appends cannot shift an earlier page.
        Include legacy tool messages to make their missing journal evidence explicit."""
        if type(after) is not int or not 0 <= after <= MAX_COUNTER:
            raise ControlError(INVALID_REQUEST, "invalid tool record position")
        if type(limit) is not int or not 1 <= limit <= PAGE:
            raise ControlError(INVALID_REQUEST, f"tool record page limit must be 1 to {PAGE}")
        with self.control.read() as tx:
            rows = tx.sql.execute(
                "SELECT m.message_id,m.position,r.operation_id,r.record_bytes "
                "FROM archive_messages m LEFT JOIN archive_tool_records r "
                "ON r.project=m.project AND r.message_id=m.message_id "
                "AND r.session=m.session AND r.run=m.run "
                "WHERE m.project=? AND m.session=? AND m.run=? AND m.position>? "
                "AND m.role='tool' AND m.kind='tool_result' ORDER BY m.position LIMIT ?",
                (project, session, run, after, limit + 1),
            ).fetchall()
        records = tuple(RecordRef(*row) for row in rows[:limit])
        return RecordPage(records, records[-1].position if records else after, len(rows) > limit)


def save_record(
    db: sqlite3.Connection,
    project: str,
    session: str,
    run: str,
    message_id: str,
    result: ToolRecord,
) -> None:
    """Join the archive append's transaction: message and full result commit together."""
    text = json.dumps(result.record, ensure_ascii=False, allow_nan=False)
    try:
        db.execute(
            "INSERT INTO archive_tool_records (project,message_id,session,run,operation_id,"
            "record,record_bytes) VALUES (?,?,?,?,?,?,?)",
            (
                project,
                message_id,
                session,
                run,
                result.operation_id,
                text,
                len(text.encode()),
            ),
        )
    except sqlite3.IntegrityError as error:
        raise ControlError(INVALID_REQUEST, f"tool record refused: {error}") from None


def _size(row: tuple[int] | None, limit: int, what: str) -> None:
    if row is None:
        raise ControlError(NOT_FOUND, f"no {what} in this scope")
    if row[0] > limit:
        raise ControlError(
            "unsupported",
            f"{what} exceeds the read byte limit",
            {"bytes": row[0], "max_bytes": limit},
        )
