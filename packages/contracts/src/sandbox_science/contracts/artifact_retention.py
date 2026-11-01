# SPDX-License-Identifier: MIT
"""An exact control receipt authorizing offline release of a worker replica."""

from __future__ import annotations

from typing import Self

from pydantic import model_validator

from .artifact_inventory import ArtifactRevisionRef, ArtifactSnapshot
from .model import Identifier, StrictContract

MAX_RETENTION_ACK_BYTES = 16 * 1024
"""Maximum encoded acknowledgement request or retained audit record."""


class ArtifactRetentionAcknowledgement(StrictContract):
    """Durable control retention of the exact immutable worker snapshot.

    The authenticated control service chooses an immutable acknowledgement identity;
    the worker echoes this complete value after recording it durably.
    """

    acknowledgement_id: Identifier
    snapshot: ArtifactSnapshot
    revision: ArtifactRevisionRef

    @model_validator(mode="after")
    def _same_content(self) -> Self:
        for field in ("project_id", "sha256", "size", "media_type"):
            if getattr(self.snapshot, field) != getattr(self.revision, field):
                raise ValueError(f"snapshot and revision disagree on {field}")
        return self
