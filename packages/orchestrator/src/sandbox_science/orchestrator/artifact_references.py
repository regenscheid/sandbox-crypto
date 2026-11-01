# SPDX-License-Identifier: MIT
"""Bounded pages of committed immutable content, without a transaction during I/O."""

from __future__ import annotations

import sqlite3
from collections.abc import Generator
from contextlib import closing
from pathlib import Path

from .artifact_blobs import Blob
from .documents import DocumentStore

_SOURCES = (
    ("artifact_revision_blobs", "artifact_blob_hash"),
    ("user_upload_blobs", "upload_blob_hash"),
)


def _next(sql: sqlite3.Connection, table: str, index: str, after: str) -> Blob | None:
    # Fixed internal table/index names only. First/last covering-index probes bound
    # duplicate-hash work too, rather than scanning every revision sharing a hash.
    row = sql.execute(
        f"SELECT substr(sha256,1,65),size FROM {table} INDEXED BY {index} "  # noqa: S608
        "WHERE sha256>? ORDER BY sha256,size LIMIT 1",
        (after,),
    ).fetchone()
    if row is None:
        return None
    blob = Blob(row[0], row[1])
    largest = sql.execute(
        f"SELECT size FROM {table} INDEXED BY {index} "  # noqa: S608
        "WHERE sha256=? ORDER BY size DESC LIMIT 1",
        (blob.sha256,),
    ).fetchone()
    if largest != (blob.size,):
        raise ValueError("artifact hash has inconsistent referenced sizes")
    return blob


def reference_page(database: Path, *, after: str = "", limit: int = 100) -> tuple[Blob, ...]:
    """Return only external retained references; no transfers, inline data or orphans."""
    if after:
        Blob(after, 0)
    if type(limit) is not int or not 1 <= limit <= 101:
        raise ValueError("invalid artifact reference page")
    with (
        closing(DocumentStore(database, readonly=True)) as documents,
        documents.control.read() as tx,
    ):
        heads = [_next(tx.sql, table, index, after) for table, index in _SOURCES]
        page: list[Blob] = []
        while any(heads) and len(page) < limit:
            blob = min((b for b in heads if b is not None), key=lambda b: b.sha256)
            matching = [i for i, head in enumerate(heads) if head and head.sha256 == blob.sha256]
            if any(heads[i] != blob for i in matching):
                raise ValueError("artifact hash has inconsistent referenced sizes")
            page.append(blob)
            if len(page) < limit:
                for i in matching:
                    table, index = _SOURCES[i]
                    heads[i] = _next(tx.sql, table, index, blob.sha256)
        return tuple(page)


def references(database: Path) -> Generator[Blob]:
    """Iterate pages without retaining the full list or holding SQL across consumers."""
    after = ""
    while page := reference_page(database, after=after):
        yield from page
        after = page[-1].sha256
