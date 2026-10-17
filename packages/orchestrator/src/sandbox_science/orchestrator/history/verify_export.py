# SPDX-License-Identifier: MIT
"""Verify a stored-evidence download in bounded memory, without interpreting its content.

Run ``uv run python -m sandbox_science.orchestrator.history.verify_export FILE``.
Checksums detect corruption/truncation; they do not authenticate who supplied a file.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
from typing import BinaryIO

from .export_format import (
    LINE,
    TABLE_FIELDS,
    Chunk,
    Complete,
    FieldStart,
    Manifest,
    Row,
    RowEnd,
    Value,
)

MAX_LINE = 96 * 1024


def verify(stream: BinaryIO) -> Manifest:
    """Validate framing, field lengths/hashes, row counts and the final stream hash."""
    digest, field_digest = hashlib.sha256(), hashlib.sha256()
    manifest: Manifest | None = None
    counts: dict[str, int] = {}
    positions: dict[str, int] = {}
    current: Row | None = None
    field: FieldStart | None = None
    names: set[str] = set()
    offset = lines = 0
    while raw := stream.readline(MAX_LINE + 1):
        if len(raw) > MAX_LINE or not raw.endswith(b"\n"):
            raise ValueError("oversized or incomplete evidence line")
        item = LINE.validate_json(raw, strict=True)
        if isinstance(item, Complete):
            if (
                manifest is None
                or current is not None
                or field is not None
                or item.lines != lines
                or item.sha256 != digest.hexdigest()
                or counts != {k: v.rows for k, v in manifest.tables.items()}
                or stream.read(1)
            ):
                raise ValueError("incomplete or corrupt evidence download")
            return manifest
        digest.update(raw)
        lines += 1
        if isinstance(item, Manifest):
            if manifest is not None or lines != 1:
                raise ValueError("the manifest must be the first line")
            manifest = item
            counts = dict.fromkeys(item.tables, 0)
            positions = dict.fromkeys(item.tables, 0)
            continue
        if manifest is None:
            raise ValueError("missing evidence manifest")
        if isinstance(item, Row):
            if (
                current is not None
                or item.table not in positions
                or not positions[item.table]
                < item.rowid
                <= manifest.tables[item.table].through_rowid
            ):
                raise ValueError("invalid evidence row order")
            current, names = item, set()
            positions[item.table] = item.rowid
            counts[item.table] += 1
        elif current is None:
            raise ValueError("field outside an evidence row")
        elif isinstance(item, RowEnd):
            if field is not None:
                raise ValueError("unfinished evidence field")
            if names != set(TABLE_FIELDS[manifest.format][current.table]):
                raise ValueError("incomplete evidence row fields")
            current = None
        elif isinstance(item, (FieldStart, Value)):
            if field is not None or item.field in names:
                raise ValueError("duplicate or unfinished evidence field")
            if item.field not in TABLE_FIELDS[manifest.format][current.table]:
                raise ValueError("unknown evidence field")
            names.add(item.field)
            if isinstance(item, FieldStart):
                field, offset, field_digest = item, 0, hashlib.sha256()
        elif isinstance(item, Chunk):
            if field is None or item.offset != offset:
                raise ValueError("invalid evidence chunk offset")
            data = base64.b64decode(item.data_base64, validate=True)
            offset += len(data)
            if offset > field.bytes:
                raise ValueError("evidence field exceeds its byte count")
            field_digest.update(data)
        else:  # The remaining line variant is FieldEnd.
            if field is None or offset != field.bytes or item.sha256 != field_digest.hexdigest():
                raise ValueError("incomplete or corrupt evidence field")
            field = None
    raise ValueError("missing evidence completion record")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("file")
    args = parser.parse_args()
    with open(args.file, "rb") as stream:
        manifest = verify(stream)
    print(
        f"Verified {manifest.project}/{manifest.session}/{manifest.run}; "
        f"{sum(t.rows for t in manifest.tables.values())} stored rows."
    )


if __name__ == "__main__":
    main()
