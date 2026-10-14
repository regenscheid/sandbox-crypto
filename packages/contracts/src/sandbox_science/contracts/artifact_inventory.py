# SPDX-License-Identifier: MIT
"""Canonical control inventory references, distinct from worker snapshot IDs."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal, Self

from pydantic import AwareDatetime, Field, field_validator, model_validator

from .artifacts import ArtifactRef
from .context import ExecutionContext
from .model import Contract, Counter, Identifier, Revision, Sha256
from .worker import MediaType

MAX_ARTIFACT_BYTES = 8 * 1024 * 1024
ShortText = Annotated[str, Field(max_length=256)]


def artifact_path(value: str) -> str:
    """A literal POSIX workspace path; never normalize away ambiguous segments."""
    if (
        not value
        or len(value.encode("utf-8")) > 4096
        or value.startswith("/")
        or any(ord(c) < 32 or ord(c) == 127 for c in value)
        or any(part in ("", ".", "..") for part in value.split("/"))
    ):
        raise ValueError("invalid artifact path")
    return value


class ArtifactSnapshot(ArtifactRef):
    """Strictly validated worker metadata, without changing the legacy worker schema."""

    size: Counter
    sha256: Sha256
    media_type: MediaType
    source_path: Annotated[str, Field(min_length=1, max_length=4096)]
    created_at: AwareDatetime

    _path = field_validator("source_path")(artifact_path)

    @field_validator("media_type")
    @classmethod
    def _media(cls, value: str) -> str:
        if any(ord(c) < 32 or ord(c) == 127 for c in value):
            raise ValueError("invalid media type")
        return value


class ArtifactRevisionRef(Contract):
    """An exact immutable control revision suitable for evidence."""

    mode: Literal["pinned"] = "pinned"
    artifact_id: Identifier
    project_id: Identifier
    revision: Revision
    sha256: Sha256
    size: Counter
    media_type: MediaType


class ArtifactLink(Contract):
    """Latest presentation reference, or explicitly pinned revision/hash."""

    artifact_id: Identifier
    project_id: Identifier
    mode: Literal["pinned", "latest"]
    revision: Revision | None = None
    sha256: Sha256 | None = None

    @model_validator(mode="after")
    def _mode(self) -> Self:
        if (self.mode == "pinned") != (self.revision is not None and self.sha256 is not None):
            raise ValueError("pinned links require revision and hash")
        if self.mode == "latest" and (self.revision is not None or self.sha256 is not None):
            raise ValueError("latest links cannot specify a revision or hash")
        return self


class ArtifactPublicationSource(Contract):
    """Known publisher attribution; it makes no claim about the file's actual writer."""

    kind: Literal["agent_publication"] = "agent_publication"
    context: ExecutionContext
    run_id: Identifier
    tool_call: Annotated[str, Field(min_length=1, max_length=128)]
    alias: ShortText | None = None
    model: ShortText | None = None
    worker_endpoint_sha256: Sha256
    observed_inputs: Literal["unknown"] = "unknown"
    producer: Literal["unknown"] = "unknown"


class ArtifactRevision(Contract):
    reference: ArtifactRevisionRef
    path: Annotated[str, Field(min_length=1, max_length=4352)]
    parent: Revision | None
    created_at: datetime
    publication: ArtifactPublicationSource
    worker_snapshot: ArtifactSnapshot
    worker_last_writer_hint: Identifier | None = None


class ArtifactReceipt(Contract):
    """Ingestion response; only a recorded receipt acknowledges retained control bytes.

    Reserved operations retain their latest hold observation when storage permits;
    admission/storage failures can only return a transient held response.
    """

    state: Literal["recorded", "held", "failed"]
    operation_id: Identifier
    worker_snapshot: ArtifactSnapshot | None = None
    revision: ArtifactRevisionRef | None = None
    reproduced: bool = False
    message: Annotated[str, Field(max_length=512)] = ""


class ArtifactEvent(Contract):
    seq: Revision
    project_id: Identifier
    kind: Literal["artifact.revision_created", "artifact.reproduced"]
    revision: ArtifactRevisionRef
    source: ArtifactPublicationSource
    worker_snapshot: ArtifactSnapshot
    created_at: datetime


class ArtifactPage(Contract):
    entries: tuple[ArtifactRevision, ...] = Field(max_length=10)
    next_after: Identifier | None = None


class ArtifactEventPage(Contract):
    entries: tuple[ArtifactEvent, ...] = Field(max_length=10)
    next_after: Counter
