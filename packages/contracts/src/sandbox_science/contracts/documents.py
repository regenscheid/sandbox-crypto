# SPDX-License-Identifier: MIT
"""Control-to-worker document catalogs. These records authorize no sandbox writes.

The completed target manifest, not any partial page, determines membership. Sequence
numbers order project publication; a document's immutable revision is independent.
"""

from __future__ import annotations

import base64
import hashlib
import json
from collections.abc import Sequence
from typing import Annotated, Final, Literal, Self

from pydantic import Field, StringConstraints, TypeAdapter, field_validator, model_validator

from .artifacts import Sha256
from .model import Contract, Identifier

MAX_DOCUMENT_BYTES: Final = 1024 * 1024
MAX_CATALOG_ENTRIES: Final = 10_000
MAX_CATALOG_BYTES: Final = 1024 * 1024 * 1024
MAX_CATALOG_PAGE: Final = 64
MAX_CATALOG_PAGE_BYTES: Final = 65_536
MAX_CONTENT_CHUNK: Final = 262_144
MAX_CONTENT_BODY: Final = 360_448
MAX_ENTRY_BYTES: Final = 2048
Counter = Annotated[int, Field(strict=True, ge=0, le=2**63 - 1)]
Revision = Annotated[int, Field(strict=True, ge=1, le=2**63 - 1)]
DocumentName = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")]
_COUNTER = TypeAdapter[Counter](Counter)
_IDENTIFIER = TypeAdapter[str](Identifier)
_SHA256 = TypeAdapter[str](Sha256)


class PublicationEntry(Contract):
    """Canonical identity/path and the immutable bytes of one current revision."""

    document_id: Identifier
    revision: Revision
    scope: Literal["project", "session"]
    session_id: Identifier | None = None
    name: DocumentName
    deleted: bool = Field(default=False, strict=True)
    content_sha256: Sha256 | None = None
    content_bytes: Annotated[int, Field(strict=True, ge=0, le=MAX_DOCUMENT_BYTES)] = 0

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if (self.scope == "session") != (self.session_id is not None):
            raise ValueError("only workbook documents have a session_id")
        if self.deleted:
            if self.content_sha256 is not None or self.content_bytes:
                raise ValueError("a tombstone carries no content")
        elif self.content_sha256 is None:
            raise ValueError("a live document needs a content digest")
        return self

    @property
    def path(self) -> str:
        """A managed path derived from canonical metadata, never a supplied path."""
        prefix = "documents" if self.scope == "project" else f"documents/sessions/{self.session_id}"
        return f"{prefix}/{self.name}.md"


def canonical_entry(entry: PublicationEntry) -> bytes:
    """Version-one manifest entry bytes, including a terminating newline."""
    return (
        json.dumps(entry.model_dump(mode="json"), sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")


def manifest_sha256(project_id: str, catalog_seq: int, entries: Sequence[PublicationEntry]) -> str:
    """Digest a complete live target, strictly ordered by document ID, with unique paths.

    The header binds project, cursor, count and aggregate content bytes. Tombstones
    belong in deltas, never in a complete live target. No content is buffered here.
    """
    _IDENTIFIER.validate_python(project_id)
    _COUNTER.validate_python(catalog_seq)
    if len(entries) > MAX_CATALOG_ENTRIES:
        raise ValueError("too many manifest entries")
    paths: set[str] = set()
    previous = ""
    total = 0
    for entry in entries:
        if entry.deleted or entry.document_id <= previous or entry.path in paths:
            raise ValueError("manifest must contain ordered unique live identities and paths")
        previous = entry.document_id
        paths.add(entry.path)
        total += entry.content_bytes
    if total > MAX_CATALOG_BYTES:
        raise ValueError("manifest content exceeds protocol limit")
    header = (
        json.dumps(
            {
                "protocol": 1,
                "project_id": project_id,
                "catalog_seq": catalog_seq,
                "entry_count": len(entries),
                "total_content_bytes": total,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        + b"\n"
    )
    digest = hashlib.sha256(header)
    for entry in entries:
        digest.update(canonical_entry(entry))
    return digest.hexdigest()


def generation_id(catalog_seq: int, manifest: str) -> str:
    """Bind a generation ID to its cursor and target, including after receipt eviction."""
    _COUNTER.validate_python(catalog_seq)
    _SHA256.validate_python(manifest)
    return f"catalog-{catalog_seq}-{manifest}"


class CatalogSnapshot(Contract):
    """Begin/commit a complete snapshot. Declared bytes reserve staging quota."""

    protocol: Literal[1] = 1
    project_id: Identifier
    generation_id: Identifier
    catalog_seq: Counter
    entry_count: Annotated[int, Field(strict=True, ge=0, le=MAX_CATALOG_ENTRIES)]
    total_content_bytes: Annotated[int, Field(strict=True, ge=0, le=MAX_CATALOG_BYTES)]
    manifest_sha256: Sha256

    @field_validator("protocol", mode="before")
    @classmethod
    def _protocol(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("protocol must be a JSON integer")
        return value

    @model_validator(mode="after")
    def _identity(self) -> Self:
        if self.generation_id != generation_id(self.catalog_seq, self.manifest_sha256):
            raise ValueError("generation_id must bind its cursor and manifest digest")
        if self.total_content_bytes > self.entry_count * MAX_DOCUMENT_BYTES:
            raise ValueError("declared content cannot fit the declared entry count")
        return self


class CatalogSnapshotPage(Contract):
    project_id: Identifier
    generation_id: Identifier
    start_index: Counter
    entries: tuple[PublicationEntry, ...] = Field(min_length=1, max_length=MAX_CATALOG_PAGE)

    @model_validator(mode="after")
    def _bounded(self) -> Self:
        if len(self.model_dump_json().encode()) > MAX_CATALOG_PAGE_BYTES:
            raise ValueError("catalog metadata page exceeds byte limit")
        if any(entry.deleted for entry in self.entries):
            raise ValueError("snapshot pages contain live entries only")
        return self


class CatalogChange(Contract):
    catalog_seq: Revision
    entry: PublicationEntry


class CatalogDelta(Contract):
    """Stage a bounded contiguous delta; the target commits through the snapshot protocol."""

    target: CatalogSnapshot
    after_seq: Counter
    changes: tuple[CatalogChange, ...] = Field(min_length=1, max_length=MAX_CATALOG_PAGE)

    @model_validator(mode="after")
    def _contiguous(self) -> Self:
        if len(self.model_dump_json().encode()) > MAX_CATALOG_PAGE_BYTES:
            raise ValueError("delta metadata exceeds byte limit")
        if [change.catalog_seq for change in self.changes] != list(
            range(self.after_seq + 1, self.after_seq + 1 + len(self.changes))
        ) or self.target.catalog_seq != self.after_seq + len(self.changes):
            raise ValueError("delta sequences must be contiguous through the target cursor")
        return self


class CatalogContentChunk(Contract):
    project_id: Identifier
    generation_id: Identifier
    content_sha256: Sha256
    offset: Annotated[int, Field(strict=True, ge=0, le=MAX_DOCUMENT_BYTES)]
    content_b64: str = Field(max_length=4 * ((MAX_CONTENT_CHUNK + 2) // 3))

    @field_validator("content_b64")
    @classmethod
    def _content(cls, value: str) -> str:
        decoded = base64.b64decode(value, validate=True)
        if not decoded or len(decoded) > MAX_CONTENT_CHUNK:
            raise ValueError("content chunks must contain 1 to 256 KiB")
        if base64.b64encode(decoded).decode("ascii") != value:
            raise ValueError("content must use canonical base64")
        return value


class CatalogStageStatus(Contract):
    generation_id: Identifier
    mode: Literal["snapshot", "delta"]
    catalog_seq: Counter
    received_entries: Counter
    entry_count: Counter
    received_content_bytes: Counter
    reserved_content_bytes: Counter
    expires_at: Annotated[float, Field(allow_inf_nan=False)]


class CatalogStatus(Contract):
    project_id: Identifier
    generation_id: Identifier | None = None
    catalog_seq: Counter | None = None
    entry_count: Counter = 0
    total_content_bytes: Counter = 0
    manifest_sha256: Sha256 | None = None
    staging: CatalogStageStatus | None = None
    replayed: bool = False
    acknowledged: CatalogSnapshot | None = None
    """A committed receipt may describe an older target; the fields above stay current."""
    publication: Literal["disabled"] = "disabled"
    """Catalog readiness is not proof that any sandbox's files were published."""
