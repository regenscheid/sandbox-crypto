# SPDX-License-Identifier: MIT
"""Published artifacts."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from pydantic import Field

from .model import Contract, Identifier, Sha256


class ArtifactRef(Contract):
    """An immutable published file: its content hash names the exact bytes."""

    artifact_id: Identifier
    project_id: Identifier
    sha256: Sha256
    size: Annotated[int, Field(ge=0)]
    media_type: str
    source_path: str
    """The workspace path the file was published from."""
    produced_by: Identifier | None = None
    """The attempt that wrote the file, when the worker knows it."""
    created_at: datetime
