# SPDX-License-Identifier: MIT
"""Pinned export inputs only: no conversion, staging or execution authority."""

from __future__ import annotations

import hashlib
import json
from typing import Annotated, Literal, Self

from pydantic import Field, StringConstraints, field_validator, model_validator

from .artifacts import Sha256
from .model import Contract, Identifier

MAX_SOURCE_BYTES = 1024 * 1024
MAX_MAP_BYTES = 4 * 1024 * 1024
MAX_SNAPSHOT_BYTES = 4 * 1024 * 1024
MAX_PARSER_BYTES = 4096
Revision = Annotated[int, Field(strict=True, ge=1, le=2**63 - 1)]
Counter = Annotated[int, Field(strict=True, ge=0, le=2**63 - 1)]
DocumentID = Annotated[str, StringConstraints(pattern=r"^doc-[0-9a-f]{24}$")]
SnapshotID = Annotated[str, StringConstraints(pattern=r"^xpi-[0-9a-f]{24}$")]
Hold = Literal[
    "map_limit",
    "unsupported_profile",
    "unknown_historical_macros",
    "macro_limits",
    "unsupported_resources",
    "source_deleted",
]


def canonical(value: object) -> bytes:
    """Protocol-one digest bytes (UTF-8, sorted keys, without a trailing newline)."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


class ExportInputRequest(Contract):
    document_id: DocumentID
    revision: Revision | None = None
    """None pins current source/current settings; a number uses saved settings."""
    operation_id: Identifier


class ExportRequester(Contract):
    """Attribution copied from a trusted caller, never authority granted by this DTO."""

    kind: Literal["user", "agent"]
    user: str | None = Field(default=None, min_length=1, max_length=128)
    session_id: Identifier | None = None
    run_id: Identifier | None = None
    role: Literal["main", "delegated"] | None = None

    @model_validator(mode="after")
    def _identity(self) -> Self:
        if self.user is not None and len(self.user.encode()) > 128:
            raise ValueError("USER identity exceeds its UTF-8 byte bound")
        if self.kind == "user":
            if self.user is None or any((self.session_id, self.run_id, self.role)):
                raise ValueError("USER attribution needs only a user identity")
        elif self.user is not None or None in (self.session_id, self.run_id, self.role):
            raise ValueError("agent attribution needs session, run and ordinary role")
        return self

    @property
    def actor(self) -> str:
        return f"user:{self.user}" if self.kind == "user" else f"session:{self.session_id}"


class ExportSource(Contract):
    document_id: DocumentID
    revision: Revision
    session_id: Identifier | None
    scope: Literal["project", "session"]
    text: str = Field(max_length=MAX_SOURCE_BYTES)
    sha256: Sha256
    bytes: Annotated[int, Field(strict=True, ge=0, le=MAX_SOURCE_BYTES)]
    block_map_bytes: Counter
    block_map_sha256: Sha256 | None
    profile: str | None = Field(max_length=128)
    parser_json: str = Field(max_length=MAX_PARSER_BYTES)
    parser_sha256: Sha256

    @model_validator(mode="after")
    def _pins(self) -> Self:
        raw = self.text.encode()
        if len(raw) != self.bytes or hashlib.sha256(raw).hexdigest() != self.sha256:
            raise ValueError("source text does not match its byte/hash pin")
        if (self.scope == "session") != (self.session_id is not None):
            raise ValueError("source scope and workbook identity disagree")
        parser = self.parser_json.encode()
        if (
            len(parser) > MAX_PARSER_BYTES
            or hashlib.sha256(parser).hexdigest() != self.parser_sha256
        ):
            raise ValueError("parser metadata does not match its hash pin")
        return self


class ExportMathSpan(Contract):
    block_id: str = Field(pattern=r"^b[1-9][0-9]{0,18}$")
    span_id: str = Field(pattern=r"^m[1-9][0-9]{0,18}$")
    kind: Literal["inline", "display"]
    tex: str = Field(max_length=MAX_SOURCE_BYTES)
    sha256: Sha256
    open: str = Field(max_length=64)
    close: str = Field(max_length=64)
    positions: tuple[Counter, Counter, Counter, Counter] | None = None
    """Optional original-source code-point offsets; never fabricated when unavailable."""

    @model_validator(mode="after")
    def _hash(self) -> Self:
        if hashlib.sha256(self.tex.encode()).hexdigest() != self.sha256:
            raise ValueError("math body does not match its hash")
        if self.positions is not None and list(self.positions) != sorted(self.positions):
            raise ValueError("math positions are not ordered")
        return self


class ExportMacros(Contract):
    known: bool = Field(strict=True)
    project_revision: Counter | None
    project_sha256: Sha256 | None
    definitions: dict[str, str] = Field(max_length=200)
    sha256: Sha256

    @model_validator(mode="after")
    def _pins(self) -> Self:
        if self.known != (self.project_revision is not None and self.project_sha256 is not None):
            raise ValueError("unknown macro context must not assert setting pins")
        if (not self.known and self.definitions) or len(canonical(self.definitions)) > 256 * 1024:
            raise ValueError("invalid or oversized effective macros")
        if hashlib.sha256(canonical(self.definitions)).hexdigest() != self.sha256:
            raise ValueError("effective macros do not match their hash")
        return self


class ExportInputSnapshot(Contract):
    protocol: Literal[1] = 1
    snapshot_id: SnapshotID
    project_id: Identifier
    request: ExportInputRequest
    requested_by: ExportRequester
    status: Literal["prepared", "held"]
    holds: tuple[Hold, ...] = Field(max_length=6)
    source: ExportSource
    macros: ExportMacros
    spans: tuple[ExportMathSpan, ...] = Field(max_length=20_000)
    spans_complete: bool = Field(strict=True)
    resources_resolved: Literal[False] = False
    """No reference scheme or external-resource resolver exists in this slice."""

    @field_validator("protocol", mode="before")
    @classmethod
    def _protocol(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("protocol is a JSON integer")
        return value

    @model_validator(mode="after")
    def _complete(self) -> Self:
        if self.source.document_id != self.request.document_id or (
            self.request.revision is not None and self.source.revision != self.request.revision
        ):
            raise ValueError("snapshot does not bind its requested source")
        if self.status != ("held" if self.holds else "prepared") or len(set(self.holds)) != len(
            self.holds
        ):
            raise ValueError("held status requires distinct explicit reasons")
        if self.status == "prepared" and (
            not self.spans_complete or not self.macros.known or self.source.block_map_sha256 is None
        ):
            raise ValueError("prepared inputs require complete proved source and context")
        ids = [span.span_id for span in self.spans]
        if len(set(ids)) != len(ids) or any(
            span.positions is not None and span.positions[-1] > len(self.source.text)
            for span in self.spans
        ):
            raise ValueError("invalid span identities or source positions")
        return self

    @property
    def sha256(self) -> str:
        return hashlib.sha256(canonical(self.model_dump(mode="json"))).hexdigest()


class ExportInputReceipt(Contract):
    snapshot: ExportInputSnapshot
    sha256: Sha256
    replayed: bool = Field(default=False, strict=True)

    @model_validator(mode="after")
    def _digest(self) -> Self:
        if self.sha256 != self.snapshot.sha256:
            raise ValueError("snapshot digest does not match its canonical bytes")
        return self


class ExportQuota(Contract):
    bytes: Annotated[int, Field(strict=True, gt=0, le=2**40)]
    records: Annotated[int, Field(strict=True, gt=0, le=1_000_000)]


class ExportBudgets(Contract):
    """Logical retained-data budgets; there is no eviction or unresolved-pin expiry."""

    project: ExportQuota = ExportQuota(bytes=64 * 1024 * 1024, records=256)
    global_: ExportQuota = ExportQuota(bytes=256 * 1024 * 1024, records=1024)
