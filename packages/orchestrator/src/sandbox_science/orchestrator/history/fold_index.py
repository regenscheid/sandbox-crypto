# SPDX-License-Identifier: MIT
"""Discover current folds without retaining merged historical records or spill payloads."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import closing

from sandbox_science.contracts import ExecutionContext

from .archive import Archive, Archived
from .limits import METADATA_BYTES, METADATA_ROWS, WorkingMemoryLimitError
from .record import FoldRecord


def historical(
    archive: Archive, context: ExecutionContext, run: str | None
) -> Iterator[tuple[Archived, FoldRecord]]:
    for row in archive.scan(
        context.project_id, context.session_id, run=run, main_only=run is None, kinds=("fold",)
    ):
        if (record := FoldRecord.of(row)) is not None:
            yield row, record


def active(
    archive: Archive, context: ExecutionContext, run: str | None
) -> tuple[list[tuple[Archived, FoldRecord]], int]:
    """A temporary metadata index removes merged parts before loading live payloads."""
    with closing(sqlite3.connect("")) as db:
        db.execute("PRAGMA cache_size=-512")
        db.execute("CREATE TABLE folds (id TEXT PRIMARY KEY, first INTEGER)")
        db.execute("CREATE TABLE merged (id TEXT PRIMARY KEY)")
        end = 0
        for row, record in historical(archive, context, run):
            db.execute("INSERT INTO folds VALUES (?,?)", (row.message_id, record.first[0]))
            db.executemany(
                "INSERT OR IGNORE INTO merged VALUES (?)", ((part,) for part in record.parts)
            )
            end = max(end, record.last[0])
        records: list[tuple[Archived, FoldRecord]] = []
        used = 0
        for (identifier,) in db.execute(
            "SELECT id FROM folds WHERE NOT EXISTS "
            "(SELECT 1 FROM merged WHERE merged.id=folds.id) ORDER BY first"
        ):
            row = archive.message(context.project_id, identifier, max_bytes=METADATA_BYTES)
            used += len(row.entry.native or b"") + len((row.entry.content or "").encode()) + 1024
            if used > METADATA_BYTES or len(records) >= METADATA_ROWS:
                raise WorkingMemoryLimitError(
                    "current fold markers exceed the working-state ceiling; "
                    "original evidence is intact"
                )
            record = FoldRecord.of(row)
            if record is not None:
                records.append((row, record))
        return records, end
