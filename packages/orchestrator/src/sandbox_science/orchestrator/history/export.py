# SPDX-License-Identifier: MIT
# ruff: noqa: S608 - table, column and predicate SQL comes only from this module's constants.
"""Stream one immutable run-evidence snapshot without materializing payloads (RC-16)."""

from __future__ import annotations

import base64
import hashlib
import json
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Final

from sandbox_science.orchestrator.control import ControlError, ControlStore
from sandbox_science.orchestrator.control.errors import INTERNAL, NOT_FOUND

from .export_format import LINE
from .operations import CHUNK_BYTES

FORMAT: Final = "sandbox-run-evidence-v1"
PAGE: Final = 100


@dataclass(frozen=True)
class _Table:
    name: str
    fields: tuple[str, ...]
    where: str


_RUN: Final = "project=? AND session=? AND run=?"
_TABLES: Final = (
    _Table("runs", ("record",), "project_id=? AND session_id=? AND name=?"),
    _Table(
        "archive_messages",
        (
            "project",
            "message_id",
            "session",
            "position",
            "run",
            "parent",
            "created",
            "role",
            "kind",
            "content",
            "source",
            "refs",
            "call_id",
            "tool_calls",
            "files",
            "model",
            "gateway_version",
            "native_format",
            "native",
            "alias",
            "prefix",
            "adapter_state",
        ),
        _RUN,
    ),
    _Table(
        "archive_docs",
        (
            "doc",
            "project",
            "message_id",
            "path",
            "size",
            "text",
            "left_out",
            "artifact_id",
            "revision",
        ),
        "EXISTS (SELECT 1 FROM archive_messages m WHERE m.project=archive_docs.project "
        "AND m.message_id=archive_docs.message_id AND m.project=? AND m.session=? AND m.run=?)",
    ),
    _Table(
        "tool_intents",
        (
            "seq",
            "project",
            "session",
            "run",
            "operation_id",
            "call_id",
            "tool",
            "arguments",
            "context",
            "created",
            "body_bytes",
        ),
        _RUN,
    ),
    _Table(
        "archive_tool_records",
        ("project", "message_id", "session", "run", "operation_id", "record_bytes", "record"),
        _RUN,
    ),
)


class EvidenceExport:
    """A point-in-time selection of immutable rows, with bounded, pull-driven reads.

    Construction checks existence without reading a body. Iteration opens a read
    transaction only to retrieve metadata or one chunk, then closes it before yield.
    The caller owns the store. No database connection or blob survives a yielded line.
    """

    def __init__(self, control: ControlStore, project: str, session: str, run: str) -> None:
        self.control, self.key = control, (project, session, run)
        with control.read() as tx:
            self.cutoffs = {
                table.name: tx.sql.execute(
                    f"SELECT coalesce(max(rowid),0),count(*) FROM {table.name} WHERE {table.where}",
                    self.key,
                ).fetchone()
                for table in _TABLES
            }
        if not any(count for _, count in self.cutoffs.values()):
            raise ControlError(NOT_FOUND, "this run has no stored evidence")

    def lines(self) -> Iterator[bytes]:
        """Checksummed NDJSON. The final checksum covers every preceding byte."""
        digest = hashlib.sha256()
        count = 0
        for value in self._items():
            line = _line(value)
            digest.update(line)
            count += 1
            yield line
        yield _line({"kind": "complete", "lines": count, "sha256": digest.hexdigest()})

    def _items(self) -> Iterator[dict[str, object]]:
        yield {
            "kind": "manifest",
            "format": FORMAT,
            "project": self.key[0],
            "session": self.key[1],
            "run": self.key[2],
            "chunk_bytes": CHUNK_BYTES,
            "tables": {
                name: {"through_rowid": last, "rows": count}
                for name, (last, count) in self.cutoffs.items()
            },
            "external_files_included": False,
            "scope": "This run's stored records only; other evidence remains referenced.",
        }
        for table in _TABLES:
            after, seen = 0, 0
            last, expected = self.cutoffs[table.name]
            while after < last:
                with self.control.read() as tx:
                    rows = tx.sql.execute(
                        f"SELECT rowid FROM {table.name} WHERE {table.where} "
                        "AND rowid>? AND rowid<=? ORDER BY rowid LIMIT ?",
                        (*self.key, after, last, PAGE),
                    ).fetchall()
                if not rows:
                    break
                for (rowid,) in rows:
                    yield from self._row(table, rowid)
                    after, seen = rowid, seen + 1
            if seen != expected:
                raise ControlError(INTERNAL, "the immutable evidence selection changed")

    def _row(self, table: _Table, rowid: int) -> Iterator[dict[str, object]]:
        with self.control.read() as tx:
            # typeof reads the storage class without loading a large column value.
            kinds = tx.sql.execute(
                f"SELECT {','.join(f'typeof({name})' for name in table.fields)} "
                f"FROM {table.name} WHERE rowid=? AND {table.where}",
                (rowid, *self.key),
            ).fetchone()
        if kinds is None:
            raise ControlError(INTERNAL, "an immutable evidence row disappeared")
        yield {"kind": "row", "table": table.name, "rowid": rowid}
        for name, kind in zip(table.fields, kinds, strict=True):
            if kind in ("text", "blob"):
                yield from self._field(table, rowid, name, kind)
            elif kind == "null":
                yield {"kind": "value", "field": name, "storage": kind, "value": None}
            elif kind in ("integer", "real"):
                with self.control.read() as tx:
                    (value,) = tx.sql.execute(
                        f"SELECT {name} FROM {table.name} WHERE rowid=?", (rowid,)
                    ).fetchone()
                yield {"kind": "value", "field": name, "storage": kind, "value": value}
            else:
                raise ControlError(INTERNAL, "unexpected evidence storage type")
        yield {"kind": "row_end"}

    def _field(
        self, table: _Table, rowid: int, name: str, kind: str
    ) -> Iterator[dict[str, object]]:
        offset, total = 0, None
        digest = hashlib.sha256()
        while total is None or offset < total:
            with (
                self.control.read() as tx,
                tx.sql.blobopen(table.name, name, rowid, readonly=True) as blob,
            ):
                size = len(blob)
                if total is not None and size != total:
                    raise ControlError(INTERNAL, "immutable evidence changed during export")
                blob.seek(offset)
                chunk = blob.read(CHUNK_BYTES)
            if total is None:
                total = size
                yield {"kind": "field", "field": name, "storage": kind, "bytes": total}
            if chunk:
                digest.update(chunk)
                yield {
                    "kind": "chunk",
                    "offset": offset,
                    "data_base64": base64.b64encode(chunk).decode("ascii"),
                }
            offset += len(chunk)
        yield {"kind": "field_end", "sha256": digest.hexdigest()}


def _line(value: dict[str, object]) -> bytes:
    LINE.validate_python(value, strict=True)
    return (json.dumps(value, ensure_ascii=True, separators=(",", ":")) + "\n").encode("ascii")
