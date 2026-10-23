# SPDX-License-Identifier: MIT
"""Versioned, bounded NDJSON line shapes for the stored-evidence interchange."""

from typing import Annotated, Final, Literal, Self

from pydantic import Field, StringConstraints, TypeAdapter, model_validator

from sandbox_science.contracts import Contract, Identifier
from sandbox_science.contracts.model import Counter, Sha256
from sandbox_science.orchestrator.projects import RunName

from .operations import CHUNK_BYTES

Table = Literal[
    "runs",
    "archive_messages",
    "archive_docs",
    "tool_intents",
    "archive_tool_records",
    "run_inputs",
    "archive_model_usage",
    "run_collections",
    "response_receipts",
]
Name = Annotated[str, StringConstraints(pattern=r"^[a-z_]{1,64}$")]

Format = Literal[
    "sandbox-run-evidence-v1",
    "sandbox-run-evidence-v2",
    "sandbox-run-evidence-v3",
    "sandbox-run-evidence-v4",
]
# Versioned wire fields, independent of later database migrations. The exporter and
# verifier share these so missing/unknown fields cannot pass a checksummed download.
_V1_FIELDS: Final[dict[Table, tuple[str, ...]]] = {
    "runs": ("record",),
    "archive_messages": (
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
    "archive_docs": (
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
    "tool_intents": (
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
    "archive_tool_records": (
        "project",
        "message_id",
        "session",
        "run",
        "operation_id",
        "record_bytes",
        "record",
    ),
}
_V2_FIELDS: Final[dict[Table, tuple[str, ...]]] = {
    "run_inputs": ("project", "session", "run", "snapshot"),
    **_V1_FIELDS,
}
_V3_FIELDS: Final[dict[Table, tuple[str, ...]]] = {
    **_V2_FIELDS,
    "archive_model_usage": (
        "project",
        "message_id",
        "session",
        "run",
        "ordinal",
        "role",
        "observed",
        "usage",
    ),
}
TABLE_FIELDS: Final[dict[Format, dict[Table, tuple[str, ...]]]] = {
    "sandbox-run-evidence-v1": _V1_FIELDS,
    "sandbox-run-evidence-v2": _V2_FIELDS,
    "sandbox-run-evidence-v3": _V3_FIELDS,
    "sandbox-run-evidence-v4": {
        **_V3_FIELDS,
        "run_collections": ("project", "session", "run", "collection", "position", "item"),
        "response_receipts": ("project", "session", "run", "source", "notice", "message_id"),
    },
}


class Selection(Contract):
    through_rowid: Counter
    rows: Counter


class Manifest(Contract):
    kind: Literal["manifest"]
    format: Format
    project: Identifier
    session: Identifier
    run: RunName
    chunk_bytes: Literal[65536]
    tables: Annotated[dict[Table, Selection], Field(min_length=5, max_length=9)]
    external_files_included: Literal[False]
    scope: Annotated[str, StringConstraints(max_length=256)]

    @model_validator(mode="after")
    def _tables(self) -> Self:
        if set(self.tables) != TABLE_FIELDS[self.format].keys():
            raise ValueError("the manifest must name its version's complete table set")
        return self


class Row(Contract):
    kind: Literal["row"]
    table: Table
    rowid: Counter


class Value(Contract):
    kind: Literal["value"]
    field: Name
    storage: Literal["null", "integer", "real"]
    value: int | float | None


class FieldStart(Contract):
    kind: Literal["field"]
    field: Name
    storage: Literal["text", "blob"]
    bytes: Counter


class Chunk(Contract):
    kind: Literal["chunk"]
    offset: Counter
    data_base64: Annotated[str, StringConstraints(max_length=4 * ((CHUNK_BYTES + 2) // 3))]


class FieldEnd(Contract):
    kind: Literal["field_end"]
    sha256: Sha256


class RowEnd(Contract):
    kind: Literal["row_end"]


class Complete(Contract):
    kind: Literal["complete"]
    lines: Counter
    sha256: Sha256


LINE = TypeAdapter[Manifest | Row | Value | FieldStart | Chunk | FieldEnd | RowEnd | Complete](
    Annotated[
        Manifest | Row | Value | FieldStart | Chunk | FieldEnd | RowEnd | Complete,
        Field(discriminator="kind"),
    ]
)
